import { defineRailway, github, project, service, volume } from "railway/iac";

/**
 * Railway project definition for the paper-only public market research worker.
 *
 * Review with `railway config plan` before applying. This file defines
 * infrastructure, but does not deploy it until an authenticated user applies it.
 */
export default defineRailway(() => {
  const data = volume("polymarket-paper-data", {
    region: "europe-west4",
    sizeMB: 1024
  });

  const worker = service("polymarket-paper-bot", {
    source: github("shalevyakobov10-dev/polymarket_paper_bot", { branch: "main" }),
    // Railway auto-detects the repository-root Dockerfile.
    replicas: { "europe-west4": 1 },
    healthcheck: "/health",
    healthcheckTimeout: 60,
    volumeMounts: {
      "/app/data": data
    },
    env: {
      DATA_DIR: "/app/data",
      POLL_SECONDS: "60",
      NEWS_POLL_SECONDS: "300",
      GAMMA_DISCOVERY_LIMIT: "400",
      MARKETS_TO_TRACK: "20",
      PAPER_STARTING_CASH: "100",
      PAPER_MAX_POSITION_USD: "2",
      PAPER_MAX_OPEN_EXPOSURE_USD: "10",
      PAPER_MAX_OPEN_POSITIONS: "5",
      PAPER_SIGNAL_DELTA: "0.03",
      PAPER_TAKE_PROFIT_PCT: "0.10",
      PAPER_STOP_LOSS_PCT: "0.10",
      PAPER_MAX_HOLD_HOURS: "24",
      PAPER_SLIPPAGE_RATE: "0.005",
      PAPER_FEE_RATE: "0",
      PAPER_DAILY_LOSS_LIMIT_USD: "5"
    }
  });

  return project("Polymarket Paper Bot", {
    resources: [worker, data]
  });
});
