"""Small PID-1 supervisor for the paper worker and optional read-only account monitor.

The authenticated monitor is opt-in and never receives credentials in argv.
When enabled, it inherits secrets only from the runtime environment; its SQLite
journal is persisted on the configured DATA_DIR volume. A monitor disconnect
restarts that process with bounded exponential backoff. If the main HTTP worker
exits, the container exits so the platform can restart the whole service.
"""
from __future__ import annotations

import logging
import math
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from supervisor_status import build_supervisor_status, write_supervisor_status


LOGGER = logging.getLogger("polymarket.supervisor")


@dataclass(frozen=True)
class SupervisorConfig:
    app_command: tuple[str, ...]
    monitor_command: tuple[str, ...] | None
    app_env: dict[str, str]
    monitor_env: dict[str, str] | None
    status_path: Path
    poll_interval_seconds: float = 1.0
    monitor_restart_base_seconds: float = 2.0
    monitor_restart_max_seconds: float = 60.0


def _positive_finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a positive finite number") from None
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return parsed


def build_supervisor_config(
    env: Mapping[str, str] | None = None,
    *,
    python_executable: str | None = None,
) -> SupervisorConfig:
    """Build child commands without placing credentials in arguments."""
    source = dict(os.environ if env is None else env)
    enabled_raw = str(source.get("ENABLE_USER_ORDER_MONITOR", "false")).strip().lower()
    if enabled_raw not in {"true", "false"}:
        raise ValueError("ENABLE_USER_ORDER_MONITOR must be exactly true or false")
    monitor_enabled = enabled_raw == "true"

    data_dir = Path(source.get("DATA_DIR", "data")).expanduser()
    database_path = Path(
        source.get("ORDER_JOURNAL_DB", str(data_dir / "order_journal.sqlite3"))
    ).expanduser()
    status_path = Path(
        source.get(
            "SERVICE_SUPERVISOR_STATUS_FILE",
            str(data_dir / "service_supervisor_status.json"),
        )
    ).expanduser()
    monitor_env = dict(source)
    monitor_env["DATA_DIR"] = str(data_dir)
    monitor_env["ORDER_JOURNAL_DB"] = str(database_path)
    monitor_env["SERVICE_SUPERVISOR_STATUS_FILE"] = str(status_path)

    # The public HTTP research worker has no need to inherit account secrets.
    app_env = dict(monitor_env)
    for secret_name in (
        "POLYMARKET_PRIVATE_KEY",
        "POLYMARKET_API_KEY",
        "POLYMARKET_API_SECRET",
        "POLYMARKET_API_PASSPHRASE",
        "POLYMARKET_SECRET",
    ):
        app_env.pop(secret_name, None)

    executable = python_executable or sys.executable
    app_command = (executable, "app.py")
    monitor_command: tuple[str, ...] | None = None

    if monitor_enabled:
        private_key = source.get("POLYMARKET_PRIVATE_KEY")
        if not isinstance(private_key, str) or not private_key.strip():
            raise ValueError(
                "ENABLE_USER_ORDER_MONITOR=true requires POLYMARKET_PRIVATE_KEY "
                "from the runtime secret manager"
            )
        interval = _positive_finite(
            source.get("USER_ORDER_SNAPSHOT_INTERVAL_SECONDS", "300"),
            "USER_ORDER_SNAPSHOT_INTERVAL_SECONDS",
        )
        monitor_command = (
            executable,
            "user_order_stream.py",
            "--database",
            str(database_path),
            "--snapshot-interval-seconds",
            str(interval),
        )

    poll_interval = _positive_finite(
        source.get("SERVICE_SUPERVISOR_POLL_SECONDS", "1"),
        "SERVICE_SUPERVISOR_POLL_SECONDS",
    )
    return SupervisorConfig(
        app_command=app_command,
        monitor_command=monitor_command,
        app_env=app_env,
        monitor_env=monitor_env if monitor_enabled else None,
        status_path=status_path,
        poll_interval_seconds=min(poll_interval, 10.0),
    )


def monitor_restart_delay(
    failure_count: int,
    *,
    base_seconds: float = 2.0,
    max_seconds: float = 60.0,
) -> float:
    """Return capped exponential delay; failure_count starts at one."""
    if isinstance(failure_count, bool) or not isinstance(failure_count, int) or failure_count < 1:
        raise ValueError("failure_count must be a positive integer")
    base = _positive_finite(base_seconds, "base_seconds")
    maximum = _positive_finite(max_seconds, "max_seconds")
    if maximum < base:
        raise ValueError("max_seconds cannot be less than base_seconds")
    return min(maximum, base * (2 ** min(failure_count - 1, 20)))


def _stop_child(process: Any, *, timeout_seconds: float = 10.0) -> None:
    if process is None:
        return
    try:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=timeout_seconds)
    except ProcessLookupError:
        return


def run_supervisor(
    config: SupervisorConfig,
    *,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    stop_event: Any | None = None,
    logger: logging.Logger | None = None,
) -> int:
    """Run the HTTP worker and optionally supervise the authenticated read-only monitor."""
    log = logger or LOGGER
    stopping = stop_event or threading.Event()
    monitor_enabled = config.monitor_command is not None
    monitor_state = "starting" if monitor_enabled else "disabled"
    service_state = "starting"
    monitor_process = None
    app_process = None
    monitor_failures = 0
    last_monitor_exit_code: int | None = None
    next_restart_at: str | None = None
    app_exit_code: int | None = None

    def publish_status() -> None:
        try:
            payload = build_supervisor_status(
                service_state=service_state,
                monitor_enabled=monitor_enabled,
                monitor_state=monitor_state,
                restart_count=monitor_failures,
                last_exit_code=last_monitor_exit_code,
                app_exit_code=app_exit_code,
                next_restart_at=next_restart_at,
            )
            write_supervisor_status(config.status_path, payload)
        except (OSError, TypeError, ValueError) as exc:
            # Status visibility must not be able to terminate either worker.
            log.error("could not persist sanitized supervisor status: %s", type(exc).__name__)

    def wait_with_heartbeat(seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while not stopping.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            # A 15-second status refresh makes stale supervisor files detectable,
            # including while the account monitor is waiting to restart.
            if stopping.wait(min(remaining, 15.0)):
                return True
            publish_status()
        return True

    publish_status()
    try:
        app_process = popen_factory(
            list(config.app_command),
            env=dict(config.app_env),
        )
        log.info("started paper HTTP worker")
        if not monitor_enabled:
            log.info("authenticated account monitor is disabled")
        else:
            monitor_process = popen_factory(
                list(config.monitor_command or ()),
                env=dict(config.monitor_env or {}),
            )
            monitor_state = "running"
            log.info("started optional read-only account order monitor")
        service_state = "running"
        publish_status()

        while not stopping.is_set():
            app_code = app_process.poll()
            if app_code is not None:
                log.error("paper HTTP worker exited with code %s", app_code)
                service_state = "app_failed"
                monitor_state = "stopped" if monitor_enabled else "disabled"
                app_exit_code = int(app_code)
                next_restart_at = None
                publish_status()
                return int(app_code) if int(app_code) != 0 else 1

            if monitor_process is not None:
                monitor_code = monitor_process.poll()
                if monitor_code is not None:
                    monitor_failures += 1
                    last_monitor_exit_code = int(monitor_code)
                    delay = monitor_restart_delay(
                        monitor_failures,
                        base_seconds=config.monitor_restart_base_seconds,
                        max_seconds=config.monitor_restart_max_seconds,
                    )
                    next_restart_at = (
                        datetime.now(timezone.utc) + timedelta(seconds=delay)
                    ).isoformat()
                    monitor_state = "restarting"
                    publish_status()
                    # Never log the child environment or any credential material.
                    log.warning(
                        "read-only account monitor exited with code %s; restart %s in %.1f seconds",
                        monitor_code,
                        monitor_failures,
                        delay,
                    )
                    if wait_with_heartbeat(delay):
                        break
                    monitor_process = popen_factory(
                        list(config.monitor_command or ()),
                        env=dict(config.monitor_env or {}),
                    )
                    monitor_state = "running"
                    next_restart_at = None
                    publish_status()
                    continue

            if wait_with_heartbeat(config.poll_interval_seconds):
                break

        return 0
    except Exception:
        service_state = "startup_failed"
        monitor_state = "stopped" if monitor_enabled else "disabled"
        next_restart_at = None
        publish_status()
        raise
    finally:
        _stop_child(monitor_process)
        _stop_child(app_process)
        if service_state != "app_failed" and service_state != "startup_failed":
            service_state = "stopped"
            monitor_state = "stopped" if monitor_enabled else "disabled"
            next_restart_at = None
            publish_status()


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        config = build_supervisor_config()
    except ValueError as exc:
        LOGGER.error("invalid service supervisor configuration: %s", exc)
        return 2

    stopping = threading.Event()

    def request_shutdown(signum: int, _frame: Any) -> None:
        LOGGER.info("received shutdown signal %s", signum)
        stopping.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    return run_supervisor(config, stop_event=stopping)


if __name__ == "__main__":
    raise SystemExit(main())
