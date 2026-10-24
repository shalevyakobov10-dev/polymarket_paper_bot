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

## Tests

```bash
python -m unittest discover -s tests -v
```

Automated tests should be run before deployment. The repository is not considered production-verified until tests, public API connectivity, and deployment health have all been checked.
