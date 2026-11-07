"""PID-1 supervisor for the HTTP service and isolated research/account workers.

The event-aware paper worker uses public market/news data only and is enabled by
default. The authenticated account monitor remains separately opt-in and
read-only. Every child is restarted with bounded backoff; the HTTP worker remains
the main service process whose failure exits the container.
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

from account_risk_policy import (
    DEFAULT_ACCOUNT_RISK_REFRESH_INTERVAL_SECONDS,
    MAX_ACCOUNT_RISK_REFRESH_INTERVAL_SECONDS,
)
from supervisor_status import build_supervisor_status, write_supervisor_status


LOGGER = logging.getLogger("polymarket.supervisor")


@dataclass(frozen=True)
class SupervisorConfig:
    app_command: tuple[str, ...]
    monitor_command: tuple[str, ...] | None
    app_env: dict[str, str]
    monitor_env: dict[str, str] | None
    paper_worker_command: tuple[str, ...] | None
    paper_worker_env: dict[str, str] | None
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
    paper_enabled_raw = str(source.get("ENABLE_EVENT_PAPER_WORKER", "false")).strip().lower()
    if paper_enabled_raw not in {"true", "false"}:
        raise ValueError("ENABLE_EVENT_PAPER_WORKER must be exactly true or false")
    paper_worker_enabled = paper_enabled_raw == "true"

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
        "POLYMARKET_DEPOSIT_WALLET",
        "LIVE_EXPECTED_WALLET",
        "LIVE_TRADING_CONFIRMATION",
    ):
        app_env.pop(secret_name, None)
    for environment_name in tuple(app_env):
        if environment_name.startswith("LIVE_"):
            app_env.pop(environment_name, None)

    # The research worker receives only the public worker environment, never
    # account-monitor or future execution credentials.
    paper_worker_env = dict(app_env)
    paper_worker_env["DATA_DIR"] = str(data_dir)
    paper_worker_env.setdefault("PAPER_DATABASE_PATH", str(data_dir / "offline_news_paper.sqlite3"))
    paper_worker_env.setdefault("MARKET_HISTORY_DATABASE_PATH", str(data_dir / "market_history.sqlite3"))
    paper_worker_env.setdefault("NEWS_LOG_PATH", str(data_dir / "offline_news_events.jsonl"))
    paper_worker_env.setdefault("MARKET_SNAPSHOT_FILE", str(data_dir / "market_snapshots.json"))
    paper_worker_env["DRY_RUN"] = "true"
    paper_worker_env["LIVE_EXECUTION_ENABLED"] = "false"
    paper_worker_env["EVENT_PAPER_WORKER_STATUS_FILE"] = str(
        source.get("EVENT_PAPER_WORKER_STATUS_FILE", str(data_dir / "event_paper_worker_status.json"))
    )

    executable = python_executable or sys.executable
    app_command = (executable, "app.py")
    paper_worker_command: tuple[str, ...] | None = (
        (executable, "event_paper_worker.py") if paper_worker_enabled else None
    )
    monitor_command: tuple[str, ...] | None = None

    if monitor_enabled:
        private_key = source.get("POLYMARKET_PRIVATE_KEY")
        if not isinstance(private_key, str) or not private_key.strip():
            raise ValueError(
                "ENABLE_USER_ORDER_MONITOR=true requires POLYMARKET_PRIVATE_KEY "
                "from the runtime secret manager"
            )
        interval = _positive_finite(
            source.get("USER_ORDER_SNAPSHOT_INTERVAL_SECONDS", str(DEFAULT_ACCOUNT_RISK_REFRESH_INTERVAL_SECONDS)),
            "USER_ORDER_SNAPSHOT_INTERVAL_SECONDS",
        )
        if interval > MAX_ACCOUNT_RISK_REFRESH_INTERVAL_SECONDS:
            raise ValueError(
                "USER_ORDER_SNAPSHOT_INTERVAL_SECONDS must not exceed "
                f"{MAX_ACCOUNT_RISK_REFRESH_INTERVAL_SECONDS} seconds"
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
        paper_worker_command=paper_worker_command,
        paper_worker_env=paper_worker_env if paper_worker_enabled else None,
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
    """Run the HTTP worker and independently supervise optional child workers."""
    log = logger or LOGGER
    stopping = stop_event or threading.Event()
    monitor_enabled = config.monitor_command is not None
    monitor_state = "starting" if monitor_enabled else "disabled"
    paper_worker_enabled = config.paper_worker_command is not None
    paper_worker_state = "starting" if paper_worker_enabled else "disabled"
    service_state = "starting"
    monitor_process = None
    paper_worker_process = None
    app_process = None
    monitor_failures = 0
    paper_worker_failures = 0
    last_monitor_exit_code: int | None = None
    next_restart_at: str | None = None
    last_paper_worker_exit_code: int | None = None
    paper_worker_next_restart_at: str | None = None
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
                event_paper_worker_enabled=paper_worker_enabled,
                event_paper_worker_state=paper_worker_state,
                event_paper_worker_restart_count=paper_worker_failures,
                event_paper_worker_last_exit_code=last_paper_worker_exit_code,
                event_paper_worker_next_restart_at=paper_worker_next_restart_at,
            )
            write_supervisor_status(config.status_path, payload)
        except (OSError, TypeError, ValueError) as exc:
            # Status visibility must not be able to terminate either worker.
            log.error("could not persist sanitized supervisor status: %s", type(exc).__name__)

    def wait_with_heartbeat(seconds: float) -> bool:
        remaining = seconds
        while remaining > 0 and not stopping.is_set():
            # A 15-second status refresh makes stale supervisor files detectable,
            # including while the account monitor is waiting to restart.
            chunk = min(remaining, 15.0)
            started = time.monotonic()
            if stopping.wait(chunk):
                return True
            elapsed = time.monotonic() - started
            # threading.Event.wait(False) returns after the requested timeout.
            # Treat an immediate false result from a nonblocking test double as
            # a completed wait slice rather than spinning until wall-clock time.
            if elapsed < 0.001:
                elapsed = chunk
            remaining = max(0.0, remaining - elapsed)
            publish_status()
        return stopping.is_set()

    publish_status()
    try:
        app_process = popen_factory(
            list(config.app_command),
            env=dict(config.app_env),
        )
        log.info("started paper HTTP worker")
        if paper_worker_enabled:
            paper_worker_process = popen_factory(
                list(config.paper_worker_command or ()),
                env=dict(config.paper_worker_env or {}),
            )
            paper_worker_state = "running"
            log.info("started event-aware paper research worker")
        else:
            paper_worker_state = "disabled"
            log.info("event-aware paper research worker is disabled")
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
                paper_worker_state = "stopped" if paper_worker_enabled else "disabled"
                app_exit_code = int(app_code)
                next_restart_at = None
                paper_worker_next_restart_at = None
                publish_status()
                return int(app_code) if int(app_code) != 0 else 1

            if paper_worker_process is not None:
                paper_code = paper_worker_process.poll()
                if paper_code is not None:
                    paper_worker_failures += 1
                    last_paper_worker_exit_code = int(paper_code)
                    delay = monitor_restart_delay(
                        paper_worker_failures,
                        base_seconds=config.monitor_restart_base_seconds,
                        max_seconds=config.monitor_restart_max_seconds,
                    )
                    paper_worker_next_restart_at = (
                        datetime.now(timezone.utc) + timedelta(seconds=delay)
                    ).isoformat()
                    paper_worker_state = "restarting"
                    publish_status()
                    log.warning(
                        "event-aware paper worker exited with code %s; restart %s in %.1f seconds",
                        paper_code,
                        paper_worker_failures,
                        delay,
                    )
                    if wait_with_heartbeat(delay):
                        break
                    paper_worker_process = popen_factory(
                        list(config.paper_worker_command or ()),
                        env=dict(config.paper_worker_env or {}),
                    )
                    paper_worker_state = "running"
                    paper_worker_next_restart_at = None
                    publish_status()
                    continue

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
        paper_worker_state = "stopped" if paper_worker_enabled else "disabled"
        next_restart_at = None
        paper_worker_next_restart_at = None
        publish_status()
        raise
    finally:
        _stop_child(monitor_process)
        _stop_child(paper_worker_process)
        _stop_child(app_process)
        if service_state != "app_failed" and service_state != "startup_failed":
            service_state = "stopped"
            monitor_state = "stopped" if monitor_enabled else "disabled"
            paper_worker_state = "stopped" if paper_worker_enabled else "disabled"
            next_restart_at = None
            paper_worker_next_restart_at = None
            publish_status()


def main() -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
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
