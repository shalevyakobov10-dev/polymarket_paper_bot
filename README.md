# Polymarket Paper Research Bot

A Python research service that polls public Polymarket Gamma market data, records observed outcome prices, logs experimental price-momentum candidates, and separately collects attributed news headlines from 15 public RSS feeds across BBC, The Guardian, Al Jazeera, NPR, Deutsche Welle, Sky News, and France 24. It exposes health, paper-portfolio, status, recent-news, and research-only news-match endpoints.

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
- `/health`: health response that checks for a recent successful market poll
- `/paper`: simulated portfolio and performance metrics
- `/news`: recent attributed news headlines, source links, feed health, and collection status
- `/matches`: recent headline-to-market keyword-overlap candidates; not probabilities or trade instructions
- `/resolutions`: health and ingestion counts for public resolved binary markets
- `/forecast-evaluation`: Brier score, log loss, accuracy, calibration bins and Brier skill against the sample base rate for archived pre-resolution Yes prices

Environment variables for market selection:
- `GAMMA_DISCOVERY_LIMIT` (default `400`, bounded to 20–500 active markets fetched for ranking)
- `MARKETS_TO_TRACK` (default `20`, bounded to 1–100 markets monitored each poll)

The live worker and backtest both rank the discovered Gamma sample by reported 24-hour volume before selecting markets. Gamma's sample is not the entire market universe and volume rankings can change between runs.

Environment variables:
- `PORT` (default `8080`)
- `POLL_SECONDS` (default `60`, minimum `15`)
- `DATA_DIR` (default `data`)
- `NEWS_POLL_SECONDS` (default `300`, bounded to 60–3600 seconds)
- `ORDERBOOK_TOKENS_PER_POLL` (default `20`, bounded to 0–100; set to `0` to disable CLOB order-book polling)
- `GDELT_QUERY` (optional override for the GDELT DOC global-news topic query)
- `RESOLUTION_POLL_SECONDS` (default `900`, bounded to 300–3600 seconds)
- `RESOLUTION_MARKETS_PER_POLL` (default `100`, bounded to 1–100 closed markets per page)
- `RESOLUTION_PAGES` (default `2`, bounded to 1–10 pages)
- `RESOLUTION_LOG_MAX_MB` (default `5`, maximum size before log rotation)
- `RESOLUTION_LOG_BACKUPS` (default `3`, retained rotated resolution logs)
- `PAPER_REQUIRE_ORDERBOOK` (default `true` in the live worker; new paper entries require a valid public CLOB book)
- `PAPER_MAX_ORDERBOOK_AGE_SECONDS` (default `180`; rejects older snapshots)
- `PAPER_MAX_ORDERBOOK_SPREAD_BPS` (default `1000`; rejects wider bid/ask spreads)
- `PAPER_MIN_ORDERBOOK_SIDE_DEPTH_USD` (default `5`; minimum visible notional on each side of the top five levels)

The market worker saves observed public market snapshots to `data/market_snapshots.jsonl`, bounded public CLOB order-book metrics to `data/orderbook_snapshots.jsonl`, and experimental price-change candidates to `data/candidate_signals.jsonl`. Order-book fields include best bid/ask, midpoint, spread, top-five-level depth and depth imbalance. These are point-in-time public snapshots, not guaranteed executable prices or historical depth. The collector requests a bounded number of unique outcome tokens per polling cycle and isolates failures per token. The separate news worker saves new, de-duplicated headlines to `data/news_events.jsonl` and supplements the RSS feeds with GDELT DOC 2.0 article metadata from a rolling one-hour search window in `data/gdelt_news.jsonl`. GDELT is an aggregation/indexing layer over media coverage, not a primary source or proof that an event occurred; it may contain duplicates, omissions, and publication/indexing delays. Logs are rotated with bounded size. News-feed failures are tracked separately and do not stop the market-data worker. The RSS sources plus GDELT broaden coverage, but they do not cover every language, country, or breaking event and do not constitute a complete real-time view of the world. The 15 default RSS sources cover BBC top stories/World/Business/Technology/Science & Environment; The Guardian World/US/Business/Technology/Science; Al Jazeera English; NPR Top Stories; Deutsche Welle Top Stories; Sky News World; and France 24 English. Feed availability and publication latency vary; this is not a comprehensive feed of every world event.

## Run with Docker

```bash
docker build -t polymarket-paper-bot .
docker run --rm -p 8080:8080 -v polymarket-data:/app/data polymarket-paper-bot
```

## News ingestion limits

News records preserve source attribution, original links, publication timestamp when provided by the publisher, and collection timestamp. The collector handles RSS and Atom formats, de-duplicates IDs across restarts, limits response size, and isolates failures per feed. It records headlines and short summaries, then emits conservative keyword-overlap candidates for review at `/matches` and in `data/news_market_candidates.jsonl`. This matching is lexical rather than semantic, does not estimate probabilities, and is not a validated trading signal. It does not place or suggest live orders. Headlines need corroboration, timestamp-aware evaluation, and out-of-sample testing before they can inform any strategy.

## Research methodology

The initial worker compares the latest observed price with the previous observation for the same outcome token and records a candidate when the absolute change is at least 0.03. This is a simple exploratory signal, not a validated strategy. It does not account for fees, slippage, liquidity, or calibration. Do not interpret candidate counts as profit or portfolio P&L.

## Resolved-market forecast evaluation

The resolution worker polls the public Gamma API for recently closed markets and archives only decisive binary Yes/No outcomes in `data/resolved_markets.jsonl`. The `/forecast-evaluation` endpoint joins those outcomes to archived `market_snapshots.jsonl` observations by market condition ID. It currently selects one Yes-token probability observation closest to 24 hours before resolution, within a 12-hour tolerance band, and never uses a snapshot taken after resolution. Only markets with a decisive 1/0 outcome-price pair and a usable pre-resolution Yes observation are included.

The scorecard reports Brier score, log loss, 50% threshold accuracy, calibration bins, expected calibration error, and Brier skill versus the empirical base-rate forecast. Lower Brier and log loss are better; positive Brier skill means the archived market prices beat the simple base-rate reference on this same sample. The endpoint explicitly warns when there are too few resolved samples.

**Important:** this first evaluation stage measures the historical calibration of Polymarket's own implied Yes prices. It is not an independent news/sentiment model, it is not evidence of a profitable strategy, and it does not estimate executable trading returns. It currently supports binary Yes/No markets only. Historical samples accumulate only while the research worker is collecting snapshots; resolved markets that were never observed before resolution are excluded. This separation is intentional: first establish a reliable, leak-free baseline before attempting to train or evaluate an independent model.

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
- `PAPER_REQUIRE_ORDERBOOK` (overrides the live-worker default when explicitly set to true/false)
- `PAPER_MAX_ORDERBOOK_AGE_SECONDS`
- `PAPER_MAX_ORDERBOOK_SPREAD_BPS`
- `PAPER_MIN_ORDERBOOK_SIDE_DEPTH_USD`

Log files are bounded with automatic rotation to avoid unlimited disk growth:
- Snapshots: `SNAPSHOT_MAX_MB=25`, `SNAPSHOT_BACKUPS=4`
- Candidate signals: `SIGNAL_LOG_MAX_MB=5`, `SIGNAL_LOG_BACKUPS=3`
- Paper events: `PAPER_EVENTS_MAX_MB=5`, `PAPER_EVENTS_BACKUPS=3`
- News records: `NEWS_LOG_MAX_MB=5`, `NEWS_LOG_BACKUPS=3`

Backups use suffixes such as `.1`, `.2`. Rotated logs are for diagnostics and auditability, not a substitute for a database backup.


Run tests with `python -m unittest discover -s tests -v` (or `python -m pytest -q` when pytest is installed). The CI workflow checks compilation and the unit tests. A passing test suite does not verify live public API connectivity or cloud deployment.


## Centralized paper risk gate

New entries are evaluated by `risk_manager.py` before the simulator opens a position. The gate checks valid outcome prices, requested position size, aggregate exposure, concurrent position count, available simulated cash after fees, a daily loss stop that combines realized P&L with current unrealized P&L, and whether an opposing outcome from the same market is already open. The simulator will not hold two different outcomes of the same market at once. Rejected candidates carry a stable reason code. These are simulation safeguards, not a guarantee against losses or a validated strategy.


## Public market microstructure and global news data

The read-only data layer now combines:
- Gamma market discovery and outcome metadata.
- CLOB public order-book snapshots for a bounded set of unique outcome tokens.
- Spread, midpoint, bid/ask depth and depth-imbalance features from the top five visible price levels.
- Attributed RSS/Atom headlines plus GDELT DOC 2.0 article metadata, including source domain, source country and publication time where supplied.

Official public-data references: [Polymarket market-data research guide](https://institute.polymarket.com/data), [Gamma API schema](https://gamma-api.polymarket.com/docs), and [GDELT DOC 2.0 API documentation](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/). All ingestion is read-only. News volume and keyword matches are not probabilities, independent corroboration, or trading signals.


## Public CLOB WebSocket observer

### Conservative paper-entry market-quality gate

The live worker requires a fresh, valid, two-sided CLOB book before opening a new simulated position. It rejects missing/failed books, invalid timestamps, snapshots older than 180 seconds, future timestamps beyond a small clock-skew allowance, crossed or incomplete books, spreads above 1,000 basis points, and less than $5 of visible top-five-level notional on either side. The limits are configurable through the environment variables above. Rejected candidates remain in the audit trail with stable reason codes such as `missing_orderbook`, `stale_orderbook`, `spread_too_wide`, and `insufficient_ask_depth`.

This gate checks displayed public liquidity; it does not prove that the displayed size would remain available, model market impact, or provide executable fills. The experimental price-change signal still uses Gamma outcome prices. For live paper entries with a valid book, the simulated entry reference is the best ask; ongoing marks and simulated exits use the best bid, with configured slippage and fees applied on top. When order-book checks are explicitly disabled or no usable book exists in a price-only research replay, Gamma is used as a clearly labelled fallback. Historical backtests currently use CLOB price-history points only and do not contain historical bid/ask depth, so they cannot reproduce this live paper fill model or establish executable historical performance.

The service also opens a read-only connection to Polymarket's public CLOB market channel. It subscribes to a bounded set of outcome token IDs selected from the tracked active markets, handles the application-level `PING` heartbeat, reconnects between bounded sessions, and persists normalized book, price-change and trade-price events to `market_stream_events.jsonl`. The current cap is controlled by `MARKET_WS_MAX_TOKENS` (default 80, maximum 200). The `/market-stream` endpoint reports connection and ingestion health.

This stream improves market-data latency for the selected tokens; it does not cover every Polymarket market, every world event, or guarantee zero-latency delivery. REST snapshots and the public stream are research inputs only. No account credentials or order-placement capability are used.
