# Polymarket Paper Research Bot

A small Python service that polls public Polymarket Gamma market data, records observed outcome prices, and logs experimental price-momentum candidates to JSONL. It exposes a basic HTTP status endpoint for container health checks.

## Safety and limitations

- **Paper/research only:** no wallet integration, order submission, deposits, or withdrawals.
- Candidate signals are experimental observations, not investment advice or evidence of profitability.
- Public API connectivity, hosting, and long-running operation must be verified in the target environment.
- Backtests and paper results do not guarantee future performance.
- Never commit passwords, API tokens, private keys, or other secrets.

## Run locally

Requires Python 3.12+.

```bash
python app.py
```

The service listens on port 8080 by default. Routes:
- `/` and `/status`: current worker status
- `/health`: lightweight container health response

Environment variables for market selection:
- `GAMMA_DISCOVERY_LIMIT` (default `400`, bounded to 20–500 active markets fetched for ranking)
- `MARKETS_TO_TRACK` (default `20`, bounded to 1–100 markets monitored each poll)

The live worker and backtest both rank the discovered Gamma sample by reported 24-hour volume before selecting markets. Gamma's sample is not the entire market universe and volume rankings can change between runs.

Environment variables:
- `PORT` (default `8080`)
- `POLL_SECONDS` (default `60`, minimum `15`)
- `DATA_DIR` (default `data`)

The worker saves observed public market snapshots to `data/market_snapshots.jsonl` and experimental price-change candidates to `data/candidate_signals.jsonl`. These files are append-only and can grow over time.

## Run with Docker

```bash
docker build -t polymarket-paper-bot .
docker run --rm -p 8080:8080 -v polymarket-data:/app/data polymarket-paper-bot
```

## Research methodology

The initial worker compares the latest observed price with the previous observation for the same outcome token and records a candidate when the absolute change is at least 0.03. This is a simple exploratory signal, not a validated strategy. It does not account for fees, slippage, liquidity, or calibration. Do not interpret candidate counts as profit or portfolio P&L.

## Historical backtest

`backtest.py` downloads public CLOB price history and runs the same paper-only price-change rule in separate chronological in-sample and out-of-sample windows. It does not submit orders and uses temporary state files only.

```bash
python backtest.py --markets 20 --days 30 --fidelity 5 --train-ratio 0.7 --output backtest_report.json
```

Or pass specific public CLOB token IDs:

```bash
python backtest.py --tokens TOKEN_ID_1 TOKEN_ID_2 --days 30 --fidelity 60
```

The default scan samples 400 active Gamma markets, ranks them by reported 24-hour volume, then reads up to two outcome tokens from each of the 20 highest-volume markets in that sample. It makes one public price-history request per selected token, with a small delay between requests. The report includes sample sizes, split time, P&L, win rate, fees/slippage assumptions, fetch errors, price-move diagnostics, and forward-return event studies at 1h, 6h, and 24h compared with the same-window all-observation baseline. The scheduled workflow also runs a fixed threshold sensitivity grid (0.005, 0.01, 0.02, 0.03) on the same fetched series; these are exploratory comparisons, not an automatic selection of the most profitable threshold. It does not model historic order-book depth, resolution payouts, or guaranteed executable fills; treat it as a research diagnostic, not proof of an edge.

## Tests

```bash
python -m unittest discover -s tests -v
```

Automated tests should be run before deployment. The repository is not considered production-verified until tests, public API connectivity, and deployment health have all been checked.


## Deployment

Railway IaC is defined in `.railway/railway.ts`; the current config pins one worker service and a 1GB volume in `europe-west4`. See [the Hebrew Railway deployment guide](DEPLOY_RAILWAY_HE.md) for the authenticated `railway config plan` / `railway config apply` process and cost check. The config is type-checked in CI, but it has **not** been applied to a Railway account and there is no running cloud service yet. See [the latest backtest research summary](research/2026-10-24-backtest-summary-he.md) before changing any thresholds.

## Continuous integration

GitHub Actions is configured in `.github/workflows/tests.yml` to compile the application and run the unit tests on pushes and pull requests.


## Paper portfolio simulation

The polling service now maintains a persistent **simulated-only** portfolio through `paper_trading.py`. Defaults are:
- Starting cash: $100
- Maximum simulated position: $2
- Maximum aggregate open exposure: $10
- Maximum open positions: 5
- Candidate rule: observed outcome price rises by at least 0.03 between polling observations
- Simulated take-profit / stop-loss: +10% / -10% from the simulated entry price
- Maximum holding time: 24 hours
- Simulated slippage: 0.5% each side
- Fee assumption: 0 by default because applicable fees have not been verified; set `PAPER_FEE_RATE` to model a fee scenario
- Daily realized-loss guard: $5

These values are risk controls and research assumptions, not evidence of profitability. The signal uses a change between observations, not a validated predictive model. The service does not short outcomes, submit orders, or connect to an account.

Paper state is persisted in `data/paper_state.json`; audit events are appended to `data/paper_events.jsonl`. The status API exposes the simulated account at `/paper` and embeds summary metrics in `/status`. If the saved state file is malformed, the worker refuses to silently reset it.

Optional environment variables:
- `PAPER_STARTING_CASH`
- `PAPER_MAX_POSITION_USD`
- `PAPER_MAX_OPEN_EXPOSURE_USD`
- `PAPER_MAX_OPEN_POSITIONS`
- `PAPER_SIGNAL_DELTA`
- `PAPER_TAKE_PROFIT_PCT`
- `PAPER_STOP_LOSS_PCT`
- `PAPER_MAX_HOLD_HOURS`
- `PAPER_SLIPPAGE_RATE`
- `PAPER_FEE_RATE`
- `PAPER_DAILY_LOSS_LIMIT_USD`

Log files are bounded with automatic rotation to avoid unlimited disk growth:
- Snapshots: `SNAPSHOT_MAX_MB=25`, `SNAPSHOT_BACKUPS=4`
- Candidate signals: `SIGNAL_LOG_MAX_MB=5`, `SIGNAL_LOG_BACKUPS=3`
- Paper events: `PAPER_EVENTS_MAX_MB=5`, `PAPER_EVENTS_BACKUPS=3`

Backups use suffixes such as `.1`, `.2`. Rotated logs are for diagnostics and auditability, not a substitute for a database backup.


Run tests with `python -m unittest discover -s tests -v` (or `python -m pytest -q` when pytest is installed). The CI workflow checks compilation and the unit tests. A passing test suite does not verify live public API connectivity or cloud deployment.
