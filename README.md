# Polymarket Paper Research Bot

A Python research service that polls public Polymarket Gamma market data, records observed outcome prices, logs experimental price-momentum candidates, and separately collects attributed news headlines from 15 public RSS feeds across BBC, The Guardian, Al Jazeera, NPR, Deutsche Welle, Sky News, and France 24. It exposes health, paper-portfolio, status, recent-news, and research-only news-match endpoints.

## Safety and limitations

- **Paper/research only:** no wallet integration, order submission, deposits, or withdrawals. The `/execution-readiness` endpoint is an informational checklist; no environment variable or API request can enable live execution. `live_order_preflight.py` validates proposed BUY limits and risk constraints without network access, including rejection of non-positive minimum order sizes; even a passing preflight never authorizes signing or order submission.
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
- `/health`: readiness response with `healthy` / `degraded` / `unhealthy` status and freshness/worker checks for market data, RSS news, GDELT, resolutions, and the public market stream; the market-data poll is required while the other collectors are exposed as optional degraded components
- `/paper`: simulated portfolio and performance metrics
- `/news`: recent attributed news headlines, source links, feed health, and collection status
- `/matches`: recent headline-to-market keyword-overlap candidates; not probabilities or trade instructions
- `/resolutions`: health and ingestion counts for public resolved binary markets, simulated settlement counts, and explicit warnings/market IDs when conflicting or invalid resolution evidence is withheld from the paper ledger
- `/forecast-evaluation`: Brier score, log loss, accuracy, calibration bins and Brier skill against the sample base rate for archived pre-resolution Yes prices
- `/research-features`: latest joined point-in-time market features (Gamma Yes/No prices, public CLOB spread/depth, and timestamp-filtered lexical news matches)
- `/independent-model-evaluation`: leakage-aware comparison of a regularized feature model against the raw Polymarket probability on a purged chronological holdout
- `/model-forecasts`: research-only current-market probability estimates from a regularized model trained only on resolved labels available before forecast time; stale rows and unresolved training labels are excluded, and no actions are issued
- `/learning`: realized forecast feedback, rolling error metrics, and a restart-safe research champion/challenger governance ledger
- `/execution-readiness`: read-only, fail-closed diagnostics for prerequisites to any future live-launch review; never enables or submits orders

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
- `ORDERBOOK_FETCH_WORKERS` (default `4`, bounded to 1–8 concurrent public CLOB requests; concurrency is capped to avoid unbounded API bursts)
- `RESEARCH_FEATURES_SNAPSHOT_SECONDS` (default `900`, bounded to 60–3600; at most one persisted feature snapshot per market per interval)
- `RESEARCH_FEATURES_LOG_MAX_MB` (default `50`, bounded to 10–500 MB per file)
- `RESEARCH_FEATURES_LOG_BACKUPS` (default `5`, bounded to 1–10 rotated backups)
- `GDELT_QUERY` (optional override for a single GDELT DOC query; overrides topic rotation)
- `GDELT_TOPICS` (optional comma-separated subset of the 18 built-in topic lanes)
- `GDELT_TOPICS_PER_POLL` (default `6`; bounded to the configured topic count)
- `GDELT_MAX_RECORDS_PER_TOPIC` (default `75`, bounded to 1–250)
- `GDELT_TIMESPAN` (default `1h`; allowed values: `15min`, `30min`, `1h`, `2h`, `4h`, `6h`, `12h`, `1d`)
- `MODEL_LEARNING_POLL_SECONDS` (default `900`, bounded to 300–3600; retrains from resolved labels and scores matured saved forecasts)
- `MODEL_EVALUATION_SECONDS` (default `21600`, bounded to 3600–86400; periodic holdout/walk-forward evaluation interval)
- `RESOLUTION_POLL_SECONDS` (default `900`, bounded to 300–3600 seconds)
- `RESOLUTION_MARKETS_PER_POLL` (default `100`, bounded to 1–100 closed markets per page)
- `RESOLUTION_PAGES` (default `2`, bounded to 1–10 pages)
- `RESOLUTION_LOG_MAX_MB` (default `5`, maximum size before log rotation)
- `RESOLUTION_LOG_BACKUPS` (default `3`, retained rotated resolution logs)
- `PAPER_REQUIRE_ORDERBOOK` (default `true` in the live worker; new paper entries require a valid public CLOB book)
- `PAPER_MAX_ORDERBOOK_AGE_SECONDS` (default `180`; rejects older snapshots)
- `PAPER_MAX_ORDERBOOK_SPREAD_BPS` (default `1000`; rejects wider bid/ask spreads)
- `PAPER_MIN_ORDERBOOK_SIDE_DEPTH_USD` (default `5`; minimum visible notional on each side of the top five levels)

The market worker saves observed public market snapshots to `data/market_snapshots.jsonl`, bounded public CLOB order-book metrics to `data/orderbook_snapshots.jsonl`, and experimental price-change candidates to `data/candidate_signals.jsonl`. Order-book fields include best bid/ask, midpoint, spread, top-five-level depth and depth imbalance. These are point-in-time public snapshots, not guaranteed executable prices or historical depth. The collector requests a bounded number of unique outcome tokens per polling cycle, uses a capped worker pool (default four concurrent requests, maximum eight), preserves deterministic token ordering in its returned map, and isolates failures per token so one slow or failed book does not block the entire batch. The separate news worker saves new, de-duplicated headlines to `data/news_events.jsonl` and supplements RSS with GDELT DOC 2.0 article metadata in `data/gdelt_news.jsonl`. GDELT rotates across 18 tagged topic lanes (geopolitics/security, macro/finance, business/trade, technology/AI/cyber, energy/commodities, health/biotech, climate/disasters, sports, science/space, entertainment/culture, law/regulation, transport/logistics, agriculture/food, labor/employment, education, consumer/retail, migration/demographics, infrastructure/utilities). By default six lanes are queried per five-minute poll, so each lane is refreshed about every 15 minutes against an overlapping one-hour search window. An article found by multiple lanes is stored once with the matching topic labels. Per-lane failures are isolated and reported in `/news` and `/health`. GDELT is an aggregation/indexing layer over media coverage, not a primary source or proof that an event occurred; it may contain duplicates, omissions, and publication/indexing delays. Logs are rotated with bounded size. News-feed failures are tracked separately and do not stop the market-data worker. The RSS sources plus GDELT broaden coverage, but they do not cover every language, country, or breaking event and do not constitute a complete real-time view of the world. The 15 default RSS sources cover BBC top stories/World/Business/Technology/Science & Environment; The Guardian World/US/Business/Technology/Science; Al Jazeera English; NPR Top Stories; Deutsche Welle Top Stories; Sky News World; and France 24 English. Feed availability and publication latency vary; this is not a comprehensive feed of every world event.

## Run with Docker

```bash
docker build -t polymarket-paper-bot .
docker run --rm -p 8080:8080 -v polymarket-data:/app/data polymarket-paper-bot
```

## Publisher identity and source diversity

RSS sections from the same newsroom and GDELT article-domain results are normalized to a shared publisher identity where the outlet is known (including BBC, The Guardian, Al Jazeera, NPR, Deutsche Welle, Sky, France 24, Reuters, Associated Press, AFP, CNN, CNBC, Bloomberg, Financial Times, The New York Times, The Washington Post, and selected regional publishers). News-to-market match records retain this publisher key, and `/matches` counts unique publisher identities rather than treating each feed section as a separate publisher. Legacy records fall back to normalized source labels.

This is still only a source-diversity measure: it does not detect every syndicated wire story, prove editorial independence, verify that an event happened, or validate a trading signal. Source country and language are preserved separately from publisher identity.

## News ingestion limits

News records preserve source attribution, original links, publication timestamp when provided by the publisher, and collection timestamp. The collector handles RSS and Atom formats, de-duplicates IDs across restarts, limits response size, and isolates failures per feed. It records headlines and short summaries, then emits conservative keyword-overlap candidates for review at `/matches` and in `data/news_market_candidates.jsonl`. This matching is lexical rather than semantic, does not estimate probabilities, and is not a validated trading signal. It does not place or suggest live orders. Headlines need corroboration, timestamp-aware evaluation, and out-of-sample testing before they can inform any strategy.

## Research methodology

The initial worker compares the latest observed price with the previous observation for the same outcome token and records a candidate when the absolute change is at least 0.03. This is a simple exploratory signal, not a validated strategy. It does not account for fees, slippage, liquidity, or calibration. Do not interpret candidate counts as profit or portfolio P&L.

## Joined market research features

Each market polling cycle also writes a point-in-time row to `data/research_features.jsonl`. The feature builder joins binary Yes/No Gamma outcome prices by market ID, the public CLOB order book for the Yes token (best bid/ask, midpoint, spread and visible top-five depth), and existing lexical news-to-market candidates whose publisher timestamp falls inside the latest 24 hours. The `/research-features` endpoint returns the latest stored row per market and a coverage summary.

Publication timestamps that are missing or implausibly in the future are reported separately and are not counted as fresh news. Distinct source labels are only a descriptive count: they are not proof of independent reporting, event verification, or corroboration. The feature endpoint does not generate probabilities, forecasts, rankings, or trading instructions. These inputs must be checked for timestamp integrity, coverage and out-of-sample value before any forecasting model is developed. The feature collector persists at most one snapshot per market every 15 minutes by default (`RESEARCH_FEATURES_SNAPSHOT_SECONDS`), which is sufficiently granular for the 24-hour evaluation horizon while reducing duplicate records. The dedicated log keeps 50 MB per file and five rotated backups by default; these settings are configurable and should be sized to the persistent volume. Recent per-market timestamps are restored from the retained log after restart, so restarts do not normally bypass the sampling cadence. The feature log retention is separate from the shorter generic JSONL rotation settings.

## Resolved-market forecast evaluation

The resolution worker polls the public Gamma API and archives only strict final binary Yes/No outcomes in `data/resolved_markets.jsonl`: exact `1/0` outcome prices, a real `closedTime` or UMA end timestamp, and (when provided) an UMA status of `resolved` or `settled`. Near-final prices such as `0.99/0.01`, proposed/disputed statuses, and scheduled `endDate` alone are not accepted as settlement evidence. The `/forecast-evaluation` endpoint joins those outcomes to archived `market_snapshots.jsonl` observations by market condition ID. It currently selects one Yes-token probability observation closest to 24 hours before resolution, within a 12-hour tolerance band, and never uses a snapshot taken after resolution. Only markets with a decisive 1/0 outcome-price pair and a usable pre-resolution Yes observation are included.

The scorecard reports Brier score, log loss, 50% threshold accuracy, calibration bins, expected calibration error, and Brier skill versus the empirical base-rate forecast. Lower Brier and log loss are better; positive Brier skill means the archived market prices beat the simple base-rate reference on this same sample. It also reports a chronological holdout: the earlier 70% of resolved markets estimate a base-rate reference, and the later 30% are evaluated against that frozen reference. The holdout is withheld until at least 20 training and 10 test markets are available. The endpoint explicitly warns when there are too few resolved samples.

**Important:** this first evaluation stage measures the historical calibration of Polymarket's own implied Yes prices. It is not an independent news/sentiment model, it is not evidence of a profitable strategy, and it does not estimate executable trading returns. It currently supports binary Yes/No markets only. Historical samples accumulate only while the research worker is collecting snapshots; resolved markets that were never observed before resolution are excluded. This separation is intentional: first establish a reliable, leak-free baseline before attempting to train or evaluate an independent model.

## Official Polymarket SDK integration

Read-only CLOB order-book snapshots are fetched through the official `polymarket-client` Python SDK, pinned to `0.16.0` in `requirements.txt`. The adapter uses only `PublicClient`; it does not instantiate an authenticated client, read wallet credentials, sign orders, or submit orders. Every snapshot retains a local receive timestamp for freshness checks and separately records the exchange timestamp/hash when supplied.

The SDK is in the 0.x beta series. The exact dependency pin prevents unreviewed minor-version API changes from silently changing the bot. A future authenticated execution adapter must be a separate, explicitly reviewed component; this read-only integration does not enable live trading.

## Independent feature-model evaluation

The `/model-forecasts` endpoint is a separate research-only inference layer. It fits only from at least 80 unique historical binary markets whose outcomes were resolved before the forecast timestamp and whose training set contains both classes. It scores fresh unresolved markets only (maximum feature age 60 minutes). Its model-vs-market probability differences are exploratory disagreements, not validated trading edges, and the endpoint always returns `NO_ACTION_RESEARCH_ONLY`.

The `/independent-model-evaluation` endpoint joins `research_features.jsonl` with resolved binary outcomes and selects one point-in-time feature row per market closest to 24 hours before resolution (12-hour tolerance). It compares the raw Gamma Yes probability with a fixed regularized logistic model using the market log-odds, the CLOB midpoint difference, book availability, top-five depth imbalance, log spread, recent lexical news-match count, mean headline keyword overlap, and log 24-hour volume.

The time split is based on the feature observation timestamp, not random sampling. Training rows are additionally purged unless their market outcome had already resolved before the first test forecast timestamp. Imputation and standardization parameters are learned only from the training window. The model is withheld until at least 80 leakage-safe training markets and 20 later test markets are available, and is also withheld if the training set contains only one outcome class. The L2 regularization setting and feature set are fixed rather than selected against the holdout.

The endpoint reports the original chronological holdout and a separate three-fold expanding-window walk-forward validation. Both scorecards now include equal-width reliability bins, expected and maximum calibration error, mean predicted probability, and observed outcome rate for the market baseline and feature model. Calibration is evaluated only on out-of-sample predictions and is descriptive at small sample sizes; it does not by itself justify trading. A market appearing more than once in an evaluation window is excluded in full rather than being allowed to overweight that market; the report counts excluded duplicate market IDs and samples. The walk-forward report additionally pools all out-of-sample predictions and computes paired 95% bootstrap intervals for the Brier and log-loss improvement versus the market baseline (2,000 deterministic market-level resamples). Positive improvement means the feature model scored better. These intervals are exploratory: correlated markets and shared news/time regimes violate the simple market-level resampling assumption and may make the intervals too narrow. Every walk-forward fold has its own preprocessing fit and a purge rule: a training market is eligible only if its outcome was already resolved before that fold's first test forecast. The preliminary consistency gate passes only when Brier score improves over the market baseline in every fold and log loss improves in at least 75% of folds. This is a research gate only, not proof of statistical significance, persistent predictive edge, profitability, or authorization to trade. The validator requires at least 80 training markets and 20 test markets per fold, and withholds the model if any fold lacks data or both outcome classes. It does not model executable fills, fees, slippage, position sizing, or portfolio risk. Real-money order execution, wallet connection and signing are not implemented. Insufficient historical data is an explicit status, not a reason to lower sample thresholds.

## Research model candidate governance

The learning worker evaluates a candidate before it can be designated the research champion. The gate fails closed unless the chronological holdout is scored with at least 80 training and 20 test markets; the expanding-window evaluation has at least three fully scored folds, at least 80 train and 20 test markets per fold, and at least 90 pooled out-of-sample markets; Brier score improves in every fold; log loss improves in at least 75% of folds; and the paired bootstrap 95% lower bounds for both Brier and log-loss improvement are above zero. The holdout must also improve both Brier score and log loss versus the market-price baseline, while expected calibration error may not degrade by more than 0.025.

Every distinct evaluation is written to `data/model_governance.jsonl`. A rejected candidate leaves the previous research champion unchanged, and the champion designation is restored from the audit log after restart. Decisions and reason codes are exposed through `/learning`. The baseline remains the incumbent until a candidate passes every gate.

**Scope limitation:** Promotion means a research designation only. It does not switch live inference, route paper positions, change strategy parameters, or authorize orders. The current model artifact is not serialized by this ledger. The gate is deliberately stricter than a single holdout and remains an exploratory quality-control mechanism, not proof of profitability or permission to trade. Real-money execution remains unimplemented.

## Historical backtest

`backtest.py` downloads public CLOB price history and runs the same paper-only price-change rule in separate chronological in-sample and out-of-sample windows. It does not submit orders and uses temporary state files only.

```bash
python backtest.py --markets 20 --days 30 --fidelity 5 --train-ratio 0.7 --output backtest_report.json
python backtest.py --markets 20 --days 30 --fidelity 5 --train-ratio 0.7 --cost-sensitivity --output backtest_cost_stress.json
python backtest.py --markets 20 --days 90 --fidelity 5 --walk-forward --walk-forward-folds 5 --walk-forward-initial-train-ratio 0.5 --output backtest_walk_forward.json
```

Or pass specific public CLOB token IDs:

```bash
python backtest.py --tokens TOKEN_ID_1 TOKEN_ID_2 --days 30 --fidelity 60
```

The default scan samples 400 active Gamma markets, ranks them by reported 24-hour volume, then reads up to two outcome tokens from each of the 20 highest-volume markets in that sample. It makes one public price-history request per selected token, with a small delay between requests. The report includes sample sizes, split time, P&L, win rate, fees/slippage assumptions, fetch errors, price-move diagnostics, and forward-return event studies at 1h, 6h, and 24h compared with the same-window all-observation baseline. The scheduled workflow also runs a fixed threshold sensitivity grid (0.005, 0.01, 0.02, 0.03) on the same fetched series; these are exploratory comparisons, not an automatic selection of the most profitable threshold. It does not model historic order-book depth, resolution payouts, or guaranteed executable fills; treat it as a research diagnostic, not proof of an edge.
The optional `--cost-sensitivity` mode replays the unchanged strategy and chronological split across a predeclared 3×3 grid: flat fee assumptions of 0%, 0.5%, and 1% per side, and slippage assumptions of 0.5%, 1%, and 2% per side. It reports in-sample and out-of-sample returns/trade counts for every scenario. These are stress assumptions, not measured execution costs; the grid is not an optimizer and must not be used to select a holdout winner.

The backtest labels each segment and walk-forward aggregate against a 30-closed-trade minimum reporting floor. Below that floor, performance and win-rate summaries are explicitly marked as insufficient-sample; reaching the floor is not statistical significance or proof of an edge. Walk-forward summaries separate folds with completed trades from idle folds so that no-trade windows are not mistaken for evidence about strategy quality. When markets are auto-discovered, the report also discloses that the universe is selected from currently active markets using current 24-hour volume; resolved/delisted markets and historical point-in-time membership are not reconstructed, so survivorship and selection bias may remain.

The optional `--walk-forward` mode evaluates one fixed strategy configuration over expanding chronological history windows and consecutive, non-overlapping out-of-sample folds. It starts each fold with a fresh paper portfolio, carrying forward only the latest pre-test price per token to avoid manufacturing a first-observation signal. At least two unique timestamps are required in every test fold; insufficient history is an explicit error. The report includes fold boundaries, sample sizes, paper metrics, equal-weight fold-return summaries, weighted win rate, and snapshot-marked maximum drawdown in dollars and percent. Drawdown is sampled only at observed timestamps and may miss losses between snapshots. The folds are not compounded into one portfolio return, and no parameters are tuned on test folds. The weekly historical workflow runs this evaluation so that performance consistency is checked across multiple periods, not only one train/test split. This remains a candle-based simulation, not a record of executable historical fills or proof of profitability.

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

The paper portfolio uses a flat `PAPER_FEE_RATE` assumption when market fee metadata is unavailable. When Gamma supplies `feesEnabled` and a supported `feeSchedule` with exponent 1, the simulator estimates the market-specific taker fee using `fee = shares × rate × price × (1 - price)`. The fee is calculated in USDC and rounded to five decimal places using decimal half-up rounding; amounts below half of `0.00001` round to zero. In the paper ledger, buy-side market fees are converted to their share equivalent at the entry price and deducted from the shares received, while sell-side market fees are deducted from USDC proceeds. The public `/paper` balance, equity, P&L, exposure, and fee figures retain eight decimal places so small share-denominated fee and binary settlement effects are not hidden by display rounding. The fee schedule is archived with feature snapshots. A market explicitly marked fee-enabled but missing a supported schedule is blocked from new paper entries rather than silently treated as fee-free. Markets with unknown fee metadata use the configured flat fallback, which defaults to zero and is a cash-cost assumption, not a verified market quote. `/research-features` reports how many tracked markets have a known schedule, are explicitly fee-free, or are marked fee-enabled without a schedule. These estimates do not reproduce every protocol-side rounding, rebate, or fill detail; they are a documented approximation for research, not proof of the fee applied to a real fill. See the [Polymarket trading-fee documentation](https://help.polymarket.com/en/articles/13364478-trading-fees) and [Maker Rebates Program](https://help.polymarket.com/en/articles/13364471-maker-rebates-program) for fee precision and collection method.

The polling service now maintains a persistent **simulated-only** portfolio through `paper_trading.py`. Defaults are:
- Starting cash: $100
- Maximum simulated position: $2
- Maximum aggregate open exposure: $10
- Maximum open positions: 5
- Candidate rule: observed outcome price rises by at least 0.03 between polling observations
- Simulated take-profit / stop-loss: +10% / -10% from the simulated entry price
- Maximum holding time: 24 hours
- Simulated slippage: 0.5% each side
- Fee estimate: use the market's Gamma `feeSchedule` when present; otherwise use the explicitly labelled `PAPER_FEE_RATE` fallback (default `0.0`, not a verified quote)
- Daily realized-loss guard: $5

These values are risk controls and research assumptions, not evidence of profitability. The signal uses a change between observations, not a validated predictive model. The service does not short outcomes, submit orders, or connect to an account.

The paper risk engine also tracks a persistent all-time equity peak and snapshot-marked maximum drawdown. The default `PAPER_MAX_PORTFOLIO_DRAWDOWN_PCT=20` blocks all new entries once current equity is down 20% or more from the recorded peak; it never automatically liquidates existing positions. The public `/paper` response reports the current drawdown, maximum observed drawdown, peak equity, configured threshold, and whether the circuit breaker is active. Legacy paper-state files receive safe defaults on load. Drawdown is only as precise as the mark cadence and price/depth data; it can miss losses between snapshots and is not a guarantee against losses.

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
- `PAPER_MAX_PORTFOLIO_DRAWDOWN_PCT` (default `20`; hard all-time peak-to-current-equity drawdown stop, bounded to 0–100% exclusive of zero)
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


The paper simulator has a persistent, latched portfolio drawdown circuit breaker. Once the configured peak-to-current-equity drawdown limit is crossed, new simulated entries remain halted even if equity later recovers; the latch survives restarts and appears in the paper metrics and audit log. Legacy states whose recorded maximum drawdown already crossed the configured limit migrate into the halted state. The only reset currently available clears the complete paper portfolio state; there is no automatic release.

## Continuous model learning and forecast feedback

The background learning worker runs every 15 minutes by default:
- Fits the existing research model again using only resolved markets whose outcome labels are already known; model versions are fingerprinted from the training history and feature schema.
- Saves current model forecasts with model version, market probability, forecast timestamp and feature freshness to `data/model_forecasts.jsonl`.
- After a market resolves, finds the saved forecast nearest 24 hours before resolution (12-hour tolerance), provided it was produced before resolution. It stores Brier score and log loss for both the model and market baseline in `data/model_feedback.jsonl`.
- Keeps rolling accuracy summaries by model version and records periodic chronological holdout/walk-forward evaluation results in `data/model_learning_evaluations.jsonl`.
- Exposes worker health, sample counts, per-version realized scoring and latest evaluation at `/learning`; `/health` also reports learning-worker freshness.

This is periodic retraining and measured feedback, not unconstrained self-modifying AI. Hyperparameters and strategy rules are not changed automatically; improvements must be validated on later out-of-sample data. A better Brier score is forecast accuracy evidence only, not proof of executable trading edge or profitability. No real-money order placement, wallet connection or signing is enabled.

## Public market microstructure and global news data

The read-only data layer now combines:
- Gamma market discovery and outcome metadata.
- CLOB public order-book snapshots for a bounded set of unique outcome tokens.
- Spread, midpoint, bid/ask depth and depth-imbalance features from the top five visible price levels.
- Attributed RSS/Atom headlines plus GDELT DOC 2.0 article metadata, including source domain, source country, language, publication time and the topic query lane that found each article where supplied.
- Ten rotating GDELT topic lanes across politics/security, economics, business, technology, energy, health, climate, sports, science and culture; GDELT documents support multilingual coverage, but the feed is still not exhaustive and indexing can lag.

Official public-data references: [Polymarket market-data research guide](https://institute.polymarket.com/data), [Gamma API schema](https://gamma-api.polymarket.com/docs), and [GDELT DOC 2.0 API documentation](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/). All ingestion is read-only. News volume and keyword matches are not probabilities, independent corroboration, or trading signals.


## Public CLOB WebSocket observer

### Conservative paper-entry market-quality gate

The live worker requires a fresh, valid, two-sided CLOB book before opening a new simulated position. It rejects missing/failed books, invalid timestamps, snapshots older than 180 seconds, future timestamps beyond a small clock-skew allowance, crossed or incomplete books, spreads above 1,000 basis points, and less than $5 of visible top-five-level notional on either side. The limits are configurable through the environment variables above. Rejected candidates remain in the audit trail with stable reason codes such as `missing_orderbook`, `stale_orderbook`, `spread_too_wide`, and `insufficient_ask_depth`.

This gate checks displayed public liquidity; it does not prove that the displayed size would remain available, model market impact, or provide executable fills. The experimental price-change signal still uses Gamma outcome prices. For live paper entries with a valid book, the simulated entry reference is the best ask; ongoing marks and simulated exits use the best bid, with configured slippage and fees applied on top. When order-book checks are explicitly disabled or no usable book exists in a price-only research replay, Gamma is used as a clearly labelled fallback. Historical backtests currently use CLOB price-history points only and do not contain historical bid/ask depth, so they cannot reproduce this live paper fill model or establish executable historical performance.

When a tracked binary market receives a definitive Yes/No resolution from the public Gamma collector, matching open paper positions are settled at a simulated $1.00 or $0.00 payout per remaining share. Settlement is idempotent because a position is removed after the first settlement. It does not charge a second taker fee: share-denominated buy fees have already reduced the number of held shares. Retained resolution logs are checked for currently open positions after each successful poll, allowing recovery after a restart. Ambiguous, malformed, conflicting, future-dated, or pre-entry resolution records are rejected. This updates only the local simulated portfolio; no on-chain redemption, claim transaction, wallet, or live order is attempted.

The service also opens a read-only connection to Polymarket's public CLOB market channel. It subscribes to a bounded set of outcome token IDs selected from the tracked active markets, handles the application-level `PING` heartbeat, reconnects between bounded sessions, and persists normalized book, price-change and trade-price events to `market_stream_events.jsonl`. The current cap is controlled by `MARKET_WS_MAX_TOKENS` (default 80, maximum 200). The `/market-stream` endpoint reports connection and ingestion health.

This stream improves market-data latency for the selected tokens; it does not cover every Polymarket market, every world event, or guarantee zero-latency delivery. REST snapshots and the public stream are research inputs only. No account credentials or order-placement capability are used.


## Paper fill quality diagnostics

Every depth-aware paper fill now reports:
- `fill_ratio` and `completion_status` (`filled`, `partial`, or `unfilled`) so visible partial depth is explicit.
- `best_visible_price` and `book_walk_cost_bps`, measuring the adverse VWAP difference from the best displayed level when sweeping the visible book.
- `visible_depth_usd` for buys or `visible_depth_shares` for sells, limited to the configured top levels and any supplied price limit.

These are snapshot diagnostics, not measured market impact or a guarantee of an executable fill. They do not model queue priority, hidden liquidity, cancellations, latency, or replenishment. A partial or unfilled result remains ineligible: the paper portfolio still opens/closes only when the full requested size is visible under the current simulation rules.


## Paper execution diagnostics summary

The `/paper` response includes an `execution_diagnostics` summary derived from the retained `recent_events` window (up to the most recent 100 state events). It reports skipped candidate counts by reason, market-quality rejection reasons, entry fill completion states, and averages for rejected partial-fill ratios, book-walk cost and visible ask depth when those observations exist.

This is intentionally a bounded recent-window diagnostic, not a lifetime ledger or a strategy-performance metric. Missing assessments remain unreported rather than inferred. Opened positions and unrelated events are excluded from the skipped-candidate counts. All values remain paper/research only; `execution` is always `none`.


## Directional price-move event study

The historical backtest's `forward_return_study` now reports two separate descriptive hypotheses for each horizon: long after a positive price move (momentum) and long after a negative price move (contrarian/rebound). Both are compared with the same all-observation baseline, use the first observed price at or after the horizon, and exclude signals without a full in-window future observation from return statistics while retaining their detected-signal counts.

The original positive-move fields remain as backward-compatible aliases. This is an event study, not a simulated portfolio or executable fill model. It does not pick the better direction, tune thresholds, or change paper entries. Small event counts are explicitly not evidence of a predictive edge; any strategy change requires a newly collected chronological holdout and realistic cost/fill validation.


## Walk-forward directional event studies

Each expanding walk-forward test fold now includes its own `directional_forward_return_study` for positive price moves (momentum) and negative price moves (contrarian long). The first test observation is compared with the last observation available for that token in the fold's training history; only in-window future observations are used to score returns. This prevents the first test point from losing its prior context without importing a future value.

Fold-level studies remain descriptive and do not change the fixed paper strategy. Counts and returns are shown per fold rather than being used to promote or tune a direction. The event observations may be correlated, and small counts do not establish an edge.


## Event-sample concentration diagnostics

Each directional forward-return event study now reports how many unique outcome tokens and market IDs contributed to the scored observations, market-ID coverage, the share of events contributed by the most active token and top three tokens, and a market-equal-weighted mean return when market IDs are available. This helps reveal when a seemingly large event count is concentrated in one token or market. These are descriptive clustering diagnostics, not a statistical independence test; repeated events can remain correlated even across different IDs.

The scheduled/manual historical backtest workflow is also serialized with a shared concurrency group. A newer historical run cancels an older in-progress run so multiple expensive public-data backtests do not overlap unnecessarily.
