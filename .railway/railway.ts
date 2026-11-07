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
      DRY_RUN: "true",
      LIVE_EXECUTION_ENABLED: "false",
      LIVE_TRADING_ENABLED: "false",
      LIVE_TRADING_ARMED: "false",
      ENABLE_USER_ORDER_MONITOR: "false",
      ENABLE_EVENT_PAPER_WORKER: "true",
      EVENT_PAPER_WORKER_STATUS_FILE: "/app/data/event_paper_worker_status.json",
      PAPER_MARKET_DATA_SOURCE: "live_clob",
      PAPER_DATABASE_PATH: "/app/data/offline_news_paper.sqlite3",
      MARKET_HISTORY_DATABASE_PATH: "/app/data/market_history.sqlite3",
      NEWS_LOG_PATH: "/app/data/offline_news_events.jsonl",
      PAPER_LIVE_MARKETS_LIMIT: "10",
      PAPER_GAMMA_PAGE_SIZE: "100",
      PAPER_POLL_SECONDS: "300",
      PAPER_EVENT_EVIDENCE_THRESHOLD: "0.75",
      NEWS_MINIMUM_PUBLISHERS: "2",
      PAPER_MIN_EVENT_CONSENSUS_RATIO: "0.75",
      PAPER_RISK_PER_TRADE_PCT: "0.01",
      PAPER_MAX_DAILY_DRAWDOWN_PCT: "0.05",
      PAPER_MAX_PORTFOLIO_DRAWDOWN_PCT: "0.20",
      PAPER_MAX_SPREAD_BPS: "500",
      PAPER_MAX_ENTRY_IMPACT_BPS: "100",
      PAPER_AUTO_TUNE_ENABLED: "true",
      PAPER_AUTO_TUNE_MINIMUM_TRADES: "60",
      PAPER_AUTO_TUNE_INTERVAL_HOURS: "24",
      PAPER_BACKUP_DIRECTORY: "/app/data/backups",
      PAPER_BACKUP_RETAIN: "30",
      PAPER_BACKUP_INTERVAL_HOURS: "6",
      POLL_SECONDS: "60",
      NEWS_POLL_SECONDS: "300",
      GDELT_TOPICS_PER_POLL: "4",
      GDELT_MAX_RECORDS_PER_TOPIC: "75",
      GDELT_TIMESPAN: "1h",
      MODEL_LEARNING_POLL_SECONDS: "900",
      MODEL_EVALUATION_SECONDS: "21600",
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
      PAPER_FEE_RATE: "0.01",
      PAPER_DAILY_LOSS_LIMIT_USD: "5"
    }
  });

  return project("Polymarket Paper Bot", {
    resources: [worker, data]
  });
});
