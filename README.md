# Polymarket Paper Research Bot

A Python research service that polls public Polymarket Gamma/CLOB market data, records observed outcome prices and order-book snapshots, and collects attributed global news from 28 configured RSS/Atom feeds in 12 languages plus the GDELT DOC 2.0 global media index. The public market WebSocket is also maintained continuously for market telemetry. Event-aware Paper decisions consume the shared news archive and fresh market snapshots; only English text is currently eligible for directional event classification. The service exposes health, portfolio, global-news coverage, status and research-only news-match endpoints.

## Safety and limitations

- **Authenticated user stream is read-only:** `user_order_stream.py` can be started separately with the official `polymarket-client` SDK and secure runtime credentials to observe account order events. It never creates, signs, posts, or cancels orders. It first subscribes to the authenticated user stream, then refreshes all pages of the REST open-order list and repeats that refresh every 240 seconds by default using a monotonic deadline, even while user events keep arriving. The configured interval is capped at 240 seconds, leaving a 60-second safety margin below the 300-second account-snapshot freshness gate. It validates venue order terms and stores cumulative fill snapshots. Orders missing from the open-order list are cross-checked against the authenticated account trade-history endpoint: a fully matched original size can be marked filled, and confirmed partial matches update cumulative fill size while terminal state remains unresolved. An order with no full-fill evidence is never guessed to be cancelled. Trade events are ignored for fill accounting to avoid double-counting. Any SDK queue drop stops the consumer; partial/missing orders still require authoritative terminal-state evidence (such as a confirmed cancellation event) before they can be closed.
- **Paper/research and read-only account monitoring only:** there is an optional authenticated read-only account monitor, but no order submission, signing, deposits, or withdrawals. The `/execution-readiness` endpoint is an informational checklist; no environment variable or API request can enable live execution. `live_order_preflight.py` validates proposed BUY limits, current collateral after estimated fees, position count, opposing-outcome exposure, book freshness, slippage, aggregate exposure, daily loss, drawdown, and explicit release gates without network access; even a passing preflight never authorizes signing or order submission. `order_lifecycle.py` is a local-only lifecycle helper: it records one initial attempt, keeps ambiguous outcomes blocked for reconciliation, and never authorizes automatic resubmission. It is not wired to a live API and does not assert that Polymarket supports its local client ID as a native idempotency key. The SQLite journal also stores cumulative fill observations monotonically, ignores exact duplicate snapshots, preserves partial fills when an order is cancelled, and sends identity mismatches to manual review.
- **Prepared intent staging is not execution:** `order_staging.py` requires a complete account-risk snapshot no older than 300 seconds; callers may shorten but cannot lengthen that safety window. It overrides caller-supplied collateral, open-position count, open exposure, unrealized PnL, and opposing-outcome status with the saved account snapshot. The resulting intent records the account snapshot ID and market condition ID; reusing the same client ID against a different snapshot is blocked. Exposure uses the higher of market value or deployed cost for each position, plus remaining BUY-order commitment. A passing report still creates only a local `prepared` intent: signing and submission remain disabled. Daily realized PnL and drawdown remain separately supplied gates and are not inferred from the account snapshot. A reused client ID must also match the saved preflight input evidence (book age, best ask, tick size, fee and risk inputs, and release gates); only the elapsed age counter of the same account snapshot is ignored. The journal validates snapshot schema and arithmetic consistency before persisting it, including collateral base units, reserved BUY commitments, risk exposure, counts, and condition-to-asset mappings. Staging repeats the economic consistency checks when reading the latest snapshot, so legacy rows created before schema hardening cannot bypass the new rules. The monitor also records the latest account-risk sync attempt in the same SQLite journal: a failed balance/position refresh immediately invalidates an older otherwise-fresh snapshot, and staging remains blocked until a newer complete snapshot is committed. Successful snapshots and failed attempts are ordered by attempt timestamp, so a delayed older fetch cannot overwrite a newer failure.
- Candidate signals are experimental observations, not investment advice or evidence of profitability.
- Public API connectivity, hosting, and long-running operation must be verified in the target environment.
- Backtests and paper results do not guarantee future performance.
- Never commit passwords, API tokens, private keys, or other secrets.

## Read-only account order monitor

The Docker image runs `service_supervisor.py` as PID 1. To opt in to account monitoring in the same persistent container, set `ENABLE_USER_ORDER_MONITOR=true` and provide `POLYMARKET_PRIVATE_KEY` through the deployment secret manager/runtime environment. The supervisor starts `app.py` without account secrets and starts `user_order_stream.py` with the secret only in its process environment. The monitor is disabled by default; the Railway IaC sets `ENABLE_USER_ORDER_MONITOR=false` explicitly.

For local isolated monitoring, it can still be run directly:

```bash
python user_order_stream.py
```

Do not put credentials in source files, committed `.env` files, command-line arguments, or chat. The SQLite journal defaults to `DATA_DIR/order_journal.sqlite3`; set `ORDER_JOURNAL_DB` to choose an explicit persistent path. Set `USER_ORDER_SNAPSHOT_INTERVAL_SECONDS` or pass `--snapshot-interval-seconds` to change the periodic REST refresh (positive values up to 240 seconds; higher values are rejected). The interval is measured against a monotonic deadline rather than as an idle timeout, so a busy stream cannot indefinitely postpone account reconciliation. The monitor subscribes to user events and reads open-order snapshots only. It does not place, sign, or cancel orders. A detected queue gap exits nonzero; the supervisor restarts the monitor with bounded exponential backoff so its startup snapshot and trade-history reconciliation can recover missed updates.

## Run locally

Requires Python 3.12+.

```bash
python service_supervisor.py
```

Run `python app.py` directly only when debugging the HTTP worker without process supervision. The Docker image uses `service_supervisor.py` as PID 1. It always runs the HTTP research worker and keeps the authenticated account monitor disabled by default. To supervise the read-only account monitor in a single persistent container, set `ENABLE_USER_ORDER_MONITOR=true` and provide `POLYMARKET_PRIVATE_KEY` through the cloud secret manager/runtime environment. The supervisor refuses that mode without the secret, never places credentials in command-line arguments or logs, and restarts a disconnected monitor with bounded backoff. If the HTTP worker exits, the container exits for platform restart. Keep one replica when using the SQLite journal and its persistent volume; do not mount the same SQLite file into multiple independent containers.

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
- `/event-paper`: sanitized status and paper portfolio metrics for the separately supervised event-aware paper engine; always research-only and non-executable
- `/perps-market`: sanitized status for the separate public Perps market-data worker
- `/perps-paper`: sanitized isolated-margin Perps paper-account state, open positions, risk halt, and fee-gated entry readiness; never submits orders
- `/perps-account-supervisor`: sanitized read-only account preflight/reconciliation heartbeat and blockers; never submits or cancels orders

Environment variables for market selection:
- `GAMMA_DISCOVERY_LIMIT` (default `400`, bounded to 20–500 active markets fetched for ranking)
- `MARKETS_TO_TRACK` (default `20`, bounded to 1–100 markets monitored each poll)

The live worker and backtest both rank the discovered Gamma sample by reported 24-hour volume before selecting markets. Gamma's sample is not the entire market universe and volume rankings can change between runs.

Environment variables:
- `PORT` (default `8080`)
- `POLL_SECONDS` (default `60`, minimum `15`)
- `DATA_DIR` (default `data`)
- `NEWS_POLL_SECONDS` (default `300`, bounded to 60–3600 seconds)
- `ORDERBOOK_TOKENS_PER_POLL` (default `60`, bounded to 0–100; set to `0` to disable CLOB order-book polling; 60 covers both outcome tokens for 30 binary Paper markets)
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

The Event Paper market provider reads up to three pages of 200 active Gamma candidates per cycle by default (`PAPER_GAMMA_PAGE_SIZE=200`, `PAPER_GAMMA_DISCOVERY_PAGES=3`), deduplicates them, and selects up to 30 binary markets for fresh CLOB checks (`PAPER_LIVE_MARKETS_LIMIT=30`). Selection is volume-ranked with a configurable per-category quota (`PAPER_MARKET_CATEGORY_MAX=8`) before filling any remaining slots by volume. Each selected YES/NO market requires two valid outcome-token books; `ORDERBOOK_TOKENS_PER_POLL=60` covers all 30 candidates. Partial or stale books are excluded from usable snapshots. If a Gamma page fails, successful pages are still used but the scan is explicitly marked partial. The worker and Control Center report pages attempted/succeeded/failed, discovered candidates, selected markets, requested tokens, usable snapshots and represented categories. This is a bounded multi-page sample, not the full Polymarket market universe. The market poller requests fresh public Gamma/CLOB metadata and two-sided order books on its configured 60-second cadence. It saves observed market snapshots to \`data/market_snapshots.jsonl\`, bounded book metrics to \`data/orderbook_snapshots.jsonl\`, and experimental price-change candidates to \`data/candidate_signals.jsonl\`. Book fields include best bid/ask, midpoint, spread, top-five-level depth and depth imbalance; they are point-in-time observations, not guaranteed executable prices or historical depth. A separate public CLOB WebSocket worker keeps bounded subscriptions to currently tracked outcome tokens and reconnects with backoff. The stream is telemetry; Paper entry/exit checks still require a fresh two-sided REST snapshot and depth validation, so a WebSocket event alone cannot trigger an entry.

The news worker polls 28 attributed RSS/Atom feeds covering 12 configured languages and multiple regions, and supplements them with GDELT DOC 2.0 across 18 topic lanes: geopolitics/security, macro/finance, business/trade, technology/AI/cyber, energy/commodities, health/biotech, climate/disasters, sports, science/space, entertainment/culture, law/regulation, transport/logistics, agriculture/food, labor/employment, education, consumer/retail, migration/demographics and infrastructure/utilities. The application news poll defaults to 60 seconds. GDELT queries a rotating bounded subset of topics per poll (six by code default; the sample \`.env.example\` sets four) over an overlapping one-hour search window. The topic cursor is persisted across restarts, per-topic failures are isolated, and duplicate article URLs are merged with topic labels.

The GDELT DOC client enforces a minimum five-second gap between topic requests (`GDELT_MIN_REQUEST_INTERVAL_SECONDS`, default 5); the number of lanes queried per poll and full rotation time depend on the configured polling interval. The application news worker is the **single GDELT network poller**. It writes the shared \`data/gdelt_news.jsonl\` archive; the separate event-aware Paper process reloads this archive and makes zero additional GDELT API requests. The read-only Control Center and \`/news\` endpoint show RSS language/region success, GDELT topic success/failure, languages and source countries seen, and the number of articles archived. GDELT is an aggregation/indexing layer over media coverage, not a primary source or proof that an event occurred; \`seendate\` is an index observation, not a publisher's original publication timestamp. The archive preserves that distinction and the Paper engine does not treat the index time as an article publication time.

The RSS/GDELT sources broaden global coverage but cannot include every publisher, country, language or breaking event. Original non-English headlines are archived and attributed for research, but the directional event classifier and VADER sentiment are currently English-only. Non-English text is explicitly kept unscored and cannot produce a YES/NO Paper direction until a translation pipeline with language detection, translation provenance, timestamp preservation and independent quality checks is added. The current system is therefore multilingual in collection and monitoring, not yet multilingual in signal interpretation.

## Run with Docker

```bash
docker build -t polymarket-paper-bot .
docker run --rm -p 8080:8080 -v polymarket-data:/app/data polymarket-paper-bot
```

## Publisher identity and source diversity

RSS sections from the same newsroom and GDELT article-domain results are normalized to a shared publisher identity where the outlet is known (including BBC, The Guardian, Al Jazeera, NPR, Deutsche Welle, Sky, France 24, Reuters, Associated Press, AFP, CNN, CNBC, Bloomberg, Financial Times, The New York Times, The Washington Post, and selected regional publishers). News-to-market match records retain this publisher key, and `/matches` counts unique publisher identities rather than treating each feed section as a separate publisher. Legacy records fall back to normalized source labels.

This is still only a source-diversity measure: it does not detect every syndicated wire story, prove editorial independence, verify that an event happened, or validate a trading signal. Source country and language are preserved separately from publisher identity.

## News ingestion limits

News records preserve publisher attribution, original links, language/coverage metadata, publisher publication time where supplied and collection/index observation time separately. RSS/Atom feeds are de-duplicated across polls and restarts, response sizes are bounded and feed failures are isolated. GDELT article URLs are de-duplicated across topic lanes and its index timestamp is never relabelled as the publisher's publication time. `/news` and the Control Center expose configured versus successful feeds, language/region coverage, GDELT topic health and archived article counts. Keyword-overlap `/matches` candidates are lexical rather than semantic; they do not estimate probabilities and are not validated trading signals. Headlines still require corroboration, timestamp-aware evaluation, source-independence checks, multilingual interpretation and out-of-sample testing before they can inform a strategy.

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

Read-only public CLOB order-book snapshots are fetched through the official `polymarket-client` Python SDK, pinned to `0.20.1` in `requirements.txt`, using `PublicClient`. The pinned build also exposes the official Perps account-session API; a local contract check verifies the documented read and order-lifecycle method surface before any Perps adapter can be considered compatible. Separately, `user_order_stream.py` can instantiate `AsyncSecureClient` for read-only account order events and open-order snapshots. Neither client path places, signs, or cancels orders. Public book snapshots retain a local receive timestamp for freshness checks and separately record the exchange timestamp/hash when supplied.

The SDK is in the 0.x beta series. The exact dependency pin prevents unreviewed minor-version API changes from silently changing the bot. A future authenticated execution adapter must be a separate, explicitly reviewed component; this read-only integration does not enable live trading.

## Independent feature-model evaluation

The `/model-forecasts` endpoint is a separate research-only inference layer. It fits only from at least 80 unique historical binary markets whose outcomes were resolved before the forecast timestamp and whose training set contains both classes. It scores fresh unresolved markets only (maximum feature age 60 minutes). Its model-vs-market probability differences are exploratory disagreements, not validated trading edges, and the endpoint always returns `NO_ACTION_RESEARCH_ONLY`.

The `/independent-model-evaluation` endpoint joins `research_features.jsonl` with resolved binary outcomes and selects one point-in-time feature row per market closest to 24 hours before resolution (12-hour tolerance). It compares the raw Gamma Yes probability with a fixed regularized logistic model using the market log-odds, the CLOB midpoint difference, book availability, top-five depth imbalance, log spread, recent lexical news-match count, mean headline keyword overlap, and log 24-hour volume.

The time split is based on the feature observation timestamp, not random sampling. Training rows are additionally purged unless their market outcome had already resolved before the first test forecast timestamp. Imputation and standardization parameters are learned only from the training window. The model is withheld until at least 80 leakage-safe training markets and 20 later test markets are available, and is also withheld if the training set contains only one outcome class. The L2 regularization setting and feature set are fixed rather than selected against the holdout.

The endpoint reports the original chronological holdout and a separate three-fold expanding-window walk-forward validation. Both scorecards now include equal-width reliability bins, expected and maximum calibration error, mean predicted probability, and observed outcome rate for the market baseline and feature model. Calibration is evaluated only on out-of-sample predictions and is descriptive at small sample sizes; it does not by itself justify trading. A market appearing more than once in an evaluation window is excluded in full rather than being allowed to overweight that market; the report counts excluded duplicate market IDs and samples. The walk-forward report additionally pools all out-of-sample predictions and computes paired 95% bootstrap intervals for the Brier and log-loss improvement versus the market baseline (2,000 deterministic market-level resamples). Positive improvement means the feature model scored better. These intervals are exploratory: correlated markets and shared news/time regimes violate the simple market-level resampling assumption and may make the intervals too narrow. Every walk-forward fold has its own preprocessing fit and a purge rule: a training market is eligible only if its outcome was already resolved before that fold's first test forecast. The preliminary consistency gate passes only when Brier score improves over the market baseline in every fold and log loss improves in at least 75% of folds. This is a research gate only, not proof of statistical significance, persistent predictive edge, profitability, or authorization to trade. The validator requires at least 80 training markets and 20 test markets per fold, and withholds the model if any fold lacks data or both outcome classes. It does not model executable fills, fees, slippage, position sizing, or portfolio risk. Real-money order execution, order-placement signing, and wallet transaction flows are not implemented. The optional account monitor authenticates only to read account order events and open orders. Insufficient historical data is an explicit status, not a reason to lower sample thresholds.

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

After depth-aware fill sizing, every proposed entry passes through `risk_manager.py` as an independent final hard gate immediately before the trade is inserted. It rechecks valid outcome price, requested notional, aggregate exposure, concurrent position count, available simulated cash after fees, daily equity loss (realized P&L plus the residual mark-to-market component), and peak-to-current portfolio drawdown. The simulator also prevents two outcomes of the same market from being held at once. The structured risk decision is included in successful entry results; rejected candidates carry a stable reason code. These are simulation safeguards, not a guarantee against losses or a validated strategy.


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


## Future order lifecycle safeguards (not connected to execution)

`order_lifecycle.py` provides pure, local state transitions for a future execution adapter. `order_journal.py` persists those transitions in a file-backed SQLite journal with atomic transactions and an append-only event history. Its `begin_submission` transaction commits `submission_pending` before returning to the caller, so network I/O cannot be started through this interface before the local attempt record is durable. A timeout, connection loss, invalid response, or other ambiguous result becomes `submission_unknown`; the helper forbids a second submission attempt and requires reconciliation using the exact local client order ID. A temporary "not found" lookup remains unknown because indexing can lag. A matching venue result can reconcile the order; a mismatched client ID moves it to manual review.

After a process restart, the operator must call `recover_interrupted_submissions()` during exclusive recovery before starting an execution worker. It converts any stranded `submission_pending` record to `submission_unknown`; it never resets an attempt to prepared. The journal serializes concurrent writers with SQLite transactions and rejects reuse of an ID with different order terms.

## Platform age and eligibility release gate

The execution-readiness report records the current Polymarket Terms of Use policy: the Site and Features are intended only for users aged **18 or older** (Terms effective 2026-08-11; [official terms](https://polymarket.com/es/tos)). Age and current platform/account eligibility require independent verification before any separate live-launch review. The policy is a blocker, not a runtime toggle, and cannot enable order submission. Applicable local law and geographic eligibility require separate qualified review.

## SQLite durability, integrity and backups

The journal requires SQLite WAL mode and configures full synchronous commits plus a 15-second busy timeout. Startup runs `PRAGMA quick_check` and `PRAGMA foreign_key_check` before schema migration; if SQLite refuses WAL or either check reports an error, the journal refuses to start rather than silently proceeding with a questionable database.

Create a consistent online backup while the journal is in use:

```bash
python backup_order_journal.py --destination /mnt/independent-backups/order-journal.sqlite3
```

The command requires an existing source journal, refuses to overwrite the live database, uses SQLite's online backup API (so committed WAL data is included), verifies the copied database and foreign keys, then atomically replaces the destination only after verification. A failed backup leaves a previous destination untouched. The output reports the backup path, byte size and completion time.

Restore a verified standalone backup into a **new, unused path** (do this with the service stopped and follow the deployment's exclusive-recovery procedure before replacing any configured live database path):

```bash
python restore_order_journal.py --source /mnt/independent-backups/order-journal.sqlite3 --destination /mnt/restore-staging/order-journal.sqlite3
```

The restore command opens the backup read-only, refuses backup files with SQLite sidecars, checks integrity, foreign keys, required journal schema, every persisted JSON state document, each order and audit event against the lifecycle state machine, event-label/target-state and predecessor-transition consistency, immutable order-term continuity, monotonic submission/reconciliation/fill counters, latest-event/current-state equality, venue-order index consistency, full historical account-risk snapshot schema/arithmetic checks and account-sync-to-snapshot timestamp binding, then copies into a temporary file and validates the copy again. Interrupted `submission_pending` records are preserved as-is; the operator must run exclusive startup recovery so they become ambiguous, never prepared. It publishes using an atomic no-overwrite operation: an existing destination is never replaced. After validation, open the restored file with the matching application version and verify the order/event/snapshot counts before using it. The command does not schedule backups, upload files, decrypt them, stop services, or switch the running application to the restored database.

This is a backup primitive, not a backup service: it does not schedule copies, encrypt or upload them, enforce retention, or prove that the destination survives loss of the app host/volume. Production operations must schedule this command to an independently durable off-host destination, protect access to the backup (it contains account/order metadata), retain multiple generations, and periodically test a restore. The Railway IaC currently defines no off-host backup target or scheduled backup job.

Any future resting-order adapter must also independently validate the venue's current heartbeat/dead-man-switch behavior, including what happens when heartbeats stop, how open orders are canceled (if applicable), and how order state is reconciled after a disconnect. The current repository does not implement or activate a venue order heartbeat. This is a separate release gate, not a substitute for local kill switches or reconciliation.

This remains a safety scaffold only. The repository has a read-only authenticated account monitor, but no order-placement adapter, signing workflow, or order-placement call. The local client order ID is not claimed to be a venue-supported idempotency field.



## Long-running authenticated stream behavior

The read-only account monitor keeps one pending user-stream receive alive while its periodic REST reconciliation runs. The refresh deadline is observed with `asyncio.wait()` rather than cancelling the in-flight `__anext__()` receive, avoiding cancellation-sensitive WebSocket iterator behavior. Periodic reconciliation diagnostics retain only a rolling window of the latest 20 compact summaries; full account snapshots and per-order unresolved details are not accumulated in process memory. This bound applies to the monitor's diagnostic return value only; SQLite remains the durable journal.


## Process supervisor status

When the service runs in Docker, the supervisor writes a sanitized atomic status record to `DATA_DIR/service_supervisor_status.json`. The read-only `/service-supervisor` endpoint shows whether the HTTP worker is starting/running/stopped, whether the authenticated monitor is enabled/running/restarting, the monitor restart count, last exit code and next restart time. The supervisor refreshes a heartbeat every 15 seconds, including while waiting to restart the monitor; `/service-supervisor` includes `status_fresh` and `status_age_seconds`, marking the record stale after 45 seconds. It deliberately excludes process arguments, environment variables, API credentials and wallet data. When running `app.py` directly without the supervisor, the endpoint reports `supervised=false` and `status_error=status_file_missing`; this is distinct from a healthy supervised account monitor. The endpoint does not prove that credentials are valid or that account data is current: use the account snapshot timestamps and monitor logs for that verification.

## Offline news-driven paper research engine

A separate local-only research runner adds fast English headline sentiment, publisher/time-decayed market matching, volatility-aware paper exits, fractional-equity risk sizing, a daily drawdown stop, and a conservative self-tuning loop. It writes entries, exits, sentiment scores, fees, and realized PnL to SQLite.

**This runner does not connect to Polymarket or any CLOB.** It reads a manually supplied JSON market snapshot from disk and fetches third-party RSS headlines only. It has no wallet, private-key, signer, or order-submission code. Live execution is hard-disabled in the module; setting DRY_RUN=false or LIVE_EXECUTION_ENABLED=true causes startup to fail.

Install and prepare a local snapshot:

```bash
python -m pip install -r requirements.txt
cp .env.example .env
mkdir -p data
cp market_snapshots.example.json data/market_snapshots.json
```

Edit data/market_snapshots.json with locally sourced/replay market quotes and a fresh UTC observed_at timestamp. The example file is synthetic, not a current market quote. Run one cycle:

```bash
python -m dotenv run -- python offline_news_paper_bot.py --once
```

Run repeatedly using PAPER_POLL_SECONDS:

```bash
python -m dotenv run -- python offline_news_paper_bot.py
```

The engine no longer uses generic positive/negative prose sentiment to choose YES or NO. Paper direction comes from a deliberately narrow, rule-based event-evidence layer that compares explicit headline actions with supported market-question types (for example, a rate cut/hold/hike or a bill passing/failing). It requires fresh publication timestamps, at least two distinct publishers, and a configurable publisher consensus ratio (default 75%). Speculative headlines, conflicting cues, unsupported question types, missing timestamps, and weak publisher consensus abstain and cannot open a paper position. VADER remains an English-oriented diagnostic feature only; it does not choose the market side. This is still lexical research evidence, not verified event truth, a probability estimate, or proof of a tradable edge. Hebrew/Arabic/non-English headlines remain unscored by VADER.

Dynamic stops use rolling absolute price-change volatility with bounded minimum/maximum distances. Position size is capped by a per-trade equity-risk budget, cash, and total paper exposure. New entries stop when daily or peak-to-trough drawdown gates are breached. Exits are simulated at the supplied best bid, entries at the supplied best ask, with a configurable two-sided fee assumption.

The paper-only learning loop reviews closed losing trades and can only raise the event-evidence entry-strength threshold; it cannot alter live settings. The active event-evidence strength threshold defaults to 0.75 and can be configured with `PAPER_EVENT_EVIDENCE_THRESHOLD`; the minimum publisher consensus is separately configured with `PAPER_MIN_EVENT_CONSENSUS_RATIO`. Tuning needs at least 60 valid closed paper trades, normalizes timezone-aware close timestamps to UTC, excludes invalid timestamps, duplicate trade IDs, and repeated market IDs (so a market resolution cannot be counted more than once), selects one stricter candidate using only the first 70% of chronological data, and then tests that same frozen candidate across three consecutive holdout windows in the final 30%. A change is applied to this offline simulator only when every holdout window has enough baseline and candidate trades, positive candidate net ROI and net PnL, at least 0.1 percentage points of net-ROI improvement, and candidate total net PnL no lower than the active policy; the pooled holdout must meet the same ROI and total-PnL constraints. Any failed or undersized fold prevents the update. This remains a paper-only, small-sample heuristic; it is not proof of a predictive edge. Each run records a SHA-256 fingerprint of normalized closed-trade inputs plus a separate fingerprint of the evaluation configuration, train/holdout time bounds, candidate training scores, fold-level net ROI/PnL comparisons, and explicit excluded-row counts. The latest sanitized audit is exposed in `/event-paper` alongside the current Paper portfolio, while the complete record remains in SQLite `tuning_runs`.

The legacy py-clob-client repository was archived on 25 May 2026 and its maintainers state that it is no longer functional; the newer official SDK is polymarket-client. This offline runner intentionally does not use either SDK. See https://github.com/Polymarket/py-clob-client and https://github.com/Polymarket/py-sdk.
## Gated live execution adapter (not armed)

The separate live gateway uses Polymarket's official unified Python SDK (`polymarket-client`) and supports only a single bounded BUY FAK order from a prepared SQLite intent. It rechecks the account snapshot/sync state, current public CLOB book, tick/lot size, account closed-only flag, collateral balance/allowance, live risk context, all required release gates, and `GET https://polymarket.com/api/geoblock` from the same host that would post the order. It signs locally, commits `submission_pending` to the journal, and only then posts the signed FAK order. The unfilled FAK remainder is not left resting. It never automatically retries; transport ambiguity becomes `submission_unknown` and requires reconciliation.

**This adapter is disabled by default and is not launch approval.** `LIVE_TRADING_ENABLED=false`, `LIVE_TRADING_ARMED=false`, and `DRY_RUN=true` are the defaults. A live path additionally requires a wallet allow-list match, a separate confirmation phrase, fresh risk input, six independent release gates, and a non-blocked geoblock result. Secrets belong only in an approved runtime secret manager; never place a private key, API credential, or seed phrase in chat, source control, CI logs, or issue comments.

The gateway intentionally does not implement SELL, GTC/resting, batch, cancel-all, or autonomous retries in this iteration. Do not arm it until legal/account eligibility, robust out-of-sample strategy evidence, independent security review, incident recovery and production monitoring are complete.

The built-in authenticated client factory is intentionally EOA-only for now. The official SDK's high-level `SecureClient.create()` can automatically deploy a not-yet-deployed Deposit Wallet during client initialization; this gateway blocks signer/wallet address mismatches rather than allowing an implicit on-chain deployment. Proxy/Deposit Wallet support requires a separately reviewed, explicit deployment-state check and is not silently enabled.


### Live public market data for paper research

The offline news paper engine now defaults to `PAPER_MARKET_DATA_SOURCE=live_clob`. It discovers a bounded set of active binary markets from the public Gamma API, maps explicit YES/NO outcomes to CLOB token IDs, and fetches both public order books through the official unauthenticated SDK. A market is withheld unless both books are two-sided, non-crossed, and fresh (maximum 30 seconds by default). The simulator marks entries at the current book midpoint, not stale Gamma outcome prices. This is read-only market data only: no account credentials, signing, order submission, or live execution are used. Set `PAPER_MARKET_DATA_SOURCE=local_json` to replay manually prepared snapshots. Live public data availability is dependent on Polymarket API reachability and rate limits; an empty/failed discovery cycle yields no eligible snapshots rather than a fallback to stale prices.


## Event-aware paper engine market-history archive

The event-aware paper engine also writes each acquired market snapshot to the versioned, append-only SQLite database at `MARKET_HISTORY_DATABASE_PATH` (default `./data/market_history.sqlite3`). It records the observed UTC timestamp, local capture timestamp, YES/NO best bid and ask, calculated midpoints, 24-hour volume when available, source labels, and the visible top-five bid/ask levels for both outcomes. The archive uses SQLite WAL, full synchronous commits, market/time indexes, a schema version, and idempotent inserts for duplicate market observations. A changed quote at the same timestamp is retained as a separate observation.

The archive is written before news signals or paper decisions are evaluated. If its write fails, that polling cycle stops rather than continuing without the market-data audit record. It is a local research archive; it does not turn midpoint marks into executable fills, reconstruct full historical order books, or enable real-money trading. Keep the SQLite file on a persistent single-writer volume and back it up with the rest of the research data.


## Depth-aware paper execution assumptions

The event-aware paper engine now simulates BUY orders by consuming the captured top-five ask levels and SELL exits by consuming the captured top-five bid levels. Entry sizing is rechecked against the resulting weighted-average entry price, and the paper ledger stores that weighted average rather than assuming every share filled at the best ask. A close is not booked unless the captured bid ladder can absorb the entire simulated position; insufficient depth is surfaced as a blocked close and the position remains open. Missing or inconsistent depth blocks a new entry.

This is a conservative visible-liquidity approximation. It does not model queue priority, hidden liquidity, latency, subsequent book movement, market impact beyond the recorded five levels, partial fills over time, or exchange-side fees beyond the configured paper fee assumption. It is not a substitute for historical tick-by-tick order-book replay or live execution evidence.


New entries also enforce `PAPER_MAX_ENTRY_IMPACT_BPS` (default 100 bps). The limit is checked against the simulated weighted-average fill price versus the best displayed ask; an entry whose visible depth would cause larger price impact is rejected before it is recorded. Exit fills still report their realized simulated book impact rather than suppressing a required risk exit.


## Continuous event-aware paper worker

The Docker image enables `event_paper_worker.py` by default through `ENABLE_EVENT_PAPER_WORKER=true`. The PID-1 supervisor runs it as a separate process beside the HTTP API. The event Paper worker runs a 60-second cycle by default, polls fresh public Gamma/CLOB snapshots, reads the shared RSS and GDELT archives populated by the application news worker, appends market observations to the market-history SQLite archive, evaluates event-aware Paper signals, and persists the simulated portfolio. The API is the single network poller for both RSS and GDELT: Paper reloads `NEWS_LOG_PATH` (default `DATA_DIR/news_events.jsonl`) and `GDELT_LOG_PATH` (default `DATA_DIR/gdelt_news.jsonl`) without making duplicate publisher or GDELT requests. Its status distinguishes a shared archive read from a live poll performed by that process.

The worker writes an atomic heartbeat file at `EVENT_PAPER_WORKER_STATUS_FILE` (default `DATA_DIR/event_paper_worker_status.json`). Its heartbeat is refreshed during the normal polling interval and retry waits, not only after a completed cycle. The HTTP `/service-supervisor` endpoint returns the sanitized worker state, last successful cycle, market count, blocked exits, failure count, and heartbeat freshness.

Transient polling errors use bounded exponential retry. After five consecutive failed cycles, the worker exits and the supervisor restarts it with capped backoff. The authenticated account monitor remains separately disabled by default. The supervisor removes `LIVE_*` and account credential values from the event worker environment and forcibly sets `DRY_RUN=true` and `LIVE_EXECUTION_ENABLED=false`.

Mount a persistent, single-writer volume at `/app/data` for Docker deployments; otherwise the paper portfolio and historical archive may be lost when the container is replaced. Do not mount the same SQLite database into multiple replicas. CI explicitly disables the event worker to keep the container smoke test isolated from external polling.


## Paper strategy performance report

The event-aware paper summary includes a descriptive report from CLOSED simulated trades only: net PnL, fee drag, expectancy per trade, net profit factor, average win/loss, win rate with a Wilson 95% interval, holding time, and maximum drawdown along the realized closed-trade sequence. It explicitly requires at least 60 closed trades before even labeling the in-sample sample size as sufficient.

This report is not a proof of edge or an out-of-sample validation. Realized drawdown excludes intratrade and open-position mark-to-market drawdown. Sharpe and Sortino are deliberately omitted because irregular trade outcomes are not a periodic return series. The report always sets `out_of_sample_validated=false` and `live_execution_eligible=false`; only a separate chronological walk-forward evaluation with enough resolved outcomes can support a future release review.


## Paper data backups and recovery

Run `python backup_paper_data.py` to create a consistent online backup of `PAPER_DATABASE_PATH` and `MARKET_HISTORY_DATABASE_PATH`, plus `PERPS_ORDER_JOURNAL_DB` when that optional journal exists (default `DATA_DIR/perps_order_journal.sqlite3`). Backups are written to `PAPER_BACKUP_DIRECTORY` (default `DATA_DIR/backups`), each SQLite copy is checked with `PRAGMA integrity_check` and `foreign_key_check`, and a SHA-256 manifest is written after successful verification. The script never overwrites an existing backup and keeps the newest `PAPER_BACKUP_RETAIN` snapshots per database (default 30).

The continuous event paper worker runs this backup automatically after the first successful cycle and then every `PAPER_BACKUP_INTERVAL_HOURS` (default 6, bounded 1–168). Failed backups are visible as a degraded worker/health component and retry with capped exponential delay. The CLI can also be run manually. Keep the backup directory on the persistent volume and replicate it off-host; a backup on the same volume does not protect against volume loss. Recovery should restore to a new, unused database path and be verified before switching the service to it; do not overwrite a live SQLite file.


## External deployment uptime monitoring

The optional `.github/workflows/paper-uptime-monitor.yml` checks the deployed service every 15 minutes once the repository variable `PAPER_SERVICE_URL` is configured to the public HTTPS base URL. It checks `/health` and `/event-paper`, requires a fresh event-worker heartbeat and at least one usable market in the last cycle, verifies the last paper backup covered both SQLite databases and is no older than eight hours, and confirms live order execution remains disabled. It does not need wallet or account credentials. Until the URL variable is set, the workflow explicitly skips cloud checks rather than reporting a false failure. This monitor is not a substitute for Railway process supervision or a dedicated alerting/incident-response system.

## Chronological event-paper market replay

```bash
python historical_replay.py \
  --paper-db data/offline_news_paper.sqlite3 \
  --market-history-db data/market_history.sqlite3 \
  --notional-usd 5 \
  --holding-hours 6 \
  --fee-rate 0.01 \
  --output data/historical-replay.json
```

The replay reads both SQLite databases in read-only mode and considers only decisions recorded as `ENTERED`. It selects the latest market snapshot whose local capture time **and** provider observation time are no later than the decision timestamp. It then uses the first fully eligible snapshot at or after the fixed holding horizon for the simulated exit. BUY and SELL fills consume the archived top-five visible levels; incomplete entries are excluded and incomplete exits remain unresolved. Entry and exit fees are applied separately using the configured proportional fee assumption.

This is a point-in-time execution-cost diagnostic, not a regenerated historical news strategy or a portfolio-level backtest. It uses fixed notional rather than the historical dynamic sizing/portfolio state, does not reconstruct stop-loss/take-profit paths, and does not model queue position, hidden depth, latency, or intraperiod book changes. The default `--fee-rate 0.01` is an explicit research assumption, not a claim about the venue's fee schedule; set it to the appropriate rate for the market being analyzed. The report always sets `out_of_sample_validated=false` and `live_execution_eligible=false`.

## Resolution-aligned event-paper evaluation

```bash
python resolved_event_evaluation.py \
  --paper-db data/offline_news_paper.sqlite3 \
  --market-history-db data/market_history.sqlite3 \
  --resolution-log data/resolved_markets.jsonl \
  --resolution-backups 3 \
  --settlement-fee-rate 0.0 \
  --output data/resolved-event-evaluation.json
```

This read-only evaluator links each recorded `ENTERED` event-paper decision to its matching paper trade, the last fresh archived YES/NO book captured and observed no later than the decision, and a definitive exact-binary market outcome from the existing Gamma resolution archive. It replays the top-five ask-side VWAP and verifies that the replayed fill matches the saved paper trade ledger before scoring. It models a binary settlement payout of one dollar per winning share and subtracts the recorded entry fee; `--settlement-fee-rate` is an explicit assumption (default zero), not a verified venue/withdrawal fee. Conflicting resolution records, disputed/unknown statuses, post-decision books, stale books, and ledger mismatches are withheld.

**Do not confuse this with actual realized Paper PnL.** The resolution score is a counterfactual hold-to-resolution result using the recorded entry. The Paper engine may already have exited at a stop, target, or time limit; the report includes that actual close/PnL separately for comparison, and its aggregate metrics report actual realized Paper PnL only for ledger trades marked CLOSED alongside the separate settlement counterfactual. One earliest eligible entry is retained per market so repeated signals on the same eventual binary outcome do not inflate the sample.

The expanding-window walk-forward selects only among stricter filters of entries already taken by the historical policy. It purges any training market whose resolution was not available before the next test fold begins. It reports each fold's training candidate scores and later test metrics; it never changes the running strategy threshold. A consistency-gate pass is exploratory only: the report always sets `out_of_sample_validated=false` and `live_execution_eligible=false`. Small samples, selection effects, related markets, shared news events, incomplete rotated resolution history, and the fixed one-dollar settlement assumption can all bias results. No result from this command alone authorizes real-money execution.


## Polymarket Perps public market-data pipeline

Polymarket Perps are separate leveraged perpetual contracts (for example crypto, equity, index, and commodity instruments), not binary Yes/No event tokens. The repository now includes a separate public-data collector. It calls the Perps instruments, tickers, and order-book REST endpoints at `https://api.perpetuals.polymarket.com`; it does not use the event-market Gamma/CLOB provider or the event-paper portfolio.

Enable it with `ENABLE_PERPS_MARKET_DATA_WORKER=true`. The PID-1 supervisor starts `perps_market_worker.py` as an isolated child, restarts it with bounded exponential backoff if it exits, and records its lifecycle in the sanitized `/service-supervisor` response. `/perps-market` exposes its allowlisted worker state. When enabled, the HTTP `/health` endpoint also requires a healthy, fresh Perps data heartbeat. Defaults are ten instruments, a 30-second collection interval, a maximum accepted exchange-data age of ten seconds, and a 14-day history retention window. `PERPS_MARKET_LIMIT` is bounded to 1–50, `PERPS_POLL_INTERVAL_SECONDS` to 15–300 seconds, and `PERPS_HISTORY_RETENTION_DAYS` to 1–365 days. The database prunes expired market snapshots and published funding settlements at most once every six hours so the persistent volume remains bounded. The worker status also reports how many instruments have a fresh public fee schedule. The market-data collector can remain healthy for risk-reducing exits during a fee-feed outage, but the Perps Paper engine is reported as `degraded` (and `/health` fails readiness) when zero instruments have a verified fee schedule; this is distinct from a collector outage. New entries remain blocked until verified fees return. The sanitized `/perps-market`, `/perps-paper`, and `/health` diagnostics report fee coverage as `unavailable`, `partial`, or `complete`; the Paper endpoint separately reports whether a fee-verified entry path is available, without implying that a strategy signal has passed the other risk gates.

The provider validates each instrument's market limits and risk tiers, instrument/ticker/book identity, exchange timestamps, positive finite prices and quantities, sorted two-sided depth, and non-crossed books. It records index, Mark, last and midpoint prices, open interest, current funding rate, next funding time, book sequence, top-ten visible bids/asks, and the full instrument/ticker metadata in `PERPS_MARKET_HISTORY_DB` (default `data/perps_market_history.sqlite3`). Repeated snapshots with the same instrument, exchange book timestamp and sequence are deduplicated. On every cycle, instrument IDs with open paper positions are supplied as priority fetches and are collected before the ordinary discovery slice; these positions do not consume `PERPS_MARKET_LIMIT`. If any open-position instrument has no fresh, usable snapshot, the paper engine is marked degraded, all new entries are blocked for that cycle, and the missing instrument IDs are exposed in sanitized worker status. Status is written atomically to `PERPS_MARKET_WORKER_STATUS_FILE`, and unexpected fields are dropped by the status reader.



Polymarket's official Perps fee notice schedules a fee/liquidation-fee change for **November 25, 2026**. The release gate now has a separate source-controlled `account_fee_tier_and_current_fee_schedule_verified=false` condition; setting an environment flag cannot satisfy it. The published rates and the actual account tier must be re-read and reconciled after the change before any later independent release review.

## Isolated-margin Perps paper portfolio

The public Perps collector is now wired to a separate SQLite-backed **paper-only** position engine (`perps_paper_engine.py`). The event-market YES/NO portfolio and its risk manager are not reused. The Perps engine opens simulated Long/Short positions from a simple momentum research signal after at least ten chronologically stored observations spanning at least 270 seconds, with a 20 bps signal threshold. This deliberately simple signal is an engineering baseline, not a validated trading edge.

The default simulated account starts with USD 1,000 and uses isolated margin with a maximum of 2x leverage, maximum USD 25 notional per position, USD 75 combined notional, and three open positions. It sizes positions under a 0.25% equity risk budget and a 0.75% stop distance; the default take-profit distance is 1.5%. New entries require fresh ticker and book timestamps, a non-crossed two-sided book, spread no wider than 20 bps, post-fill VWAP impact no greater than `PERPS_PAPER_MAX_ENTRY_IMPACT_BPS` (default 50 bps), valid instrument precision/minimum-notional/maximum-notional and post-fill risk-budget constraints, sufficient visible depth, available isolated margin and a non-halted account. Instrument leverage tiers cap the configured leverage further. The account stops opening new risk after a 3% daily-equity loss or 10% peak-to-current drawdown.

The simulator marks open positions to the exchange Mark Price. Long entries consume asks and Short entries consume bids; exits consume the opposing side of the visible book and are not recorded unless the entire position can be filled. Stop-loss and take-profit triggers use Mark Price. Maintenance margin is checked against isolated margin plus unrealized Mark PnL using the instrument's published maximum leverage (maintenance rate = 0.5 / instrument max leverage), not the selected position leverage or a lower risk-tier cap. Isolated losses are capped at the position's allocated collateral; if the modeled close would require free account collateral, the position is closed at the isolated-loss cap and the paper account is halted for reconciliation. A simulated liquidation applies the instrument's published liquidation-fee rate. The instrument's `close_only` flag prevents new exposure while keeping the instrument visible so existing positions can still be managed or exited.

Funding is reconciled from the public `/v1/info/funding` settlement-history endpoint, not estimated from the live ticker. The worker fetches the exact settlement window required by each open position and stores published settlement rates idempotently in the Perps market-history database. The engine charges funding only when it has a complete single-interval transition, a fresh post-settlement Mark Price observation within the configurable delay window (default 90 seconds), and one unambiguous published settlement at the expected timestamp. The worker waits up to a 60-second schedule-update grace period by default so the Perps ticker can publish the next funding time after the 30-second polling cycle. If a schedule regresses, fails to advance beyond that grace period, skips an interval, or a published settlement/Mark observation is missing or ambiguous, the engine records a reconciliation event and halts new risk rather than inventing a rate. These timing tolerances are configurable via `PERPS_PAPER_FUNDING_SCHEDULE_GRACE_SECONDS` and `PERPS_PAPER_MAX_POST_FUNDING_MARK_DELAY_SECONDS`; the 90-second Mark Price is a first-observation proxy, not a claim to have the exact venue settlement-time Mark Price. New entries also require a fresh public fee schedule; the simulator uses the highest published taker rate for the instrument category (no unverified personal volume discount). Existing positions retain their entry fee rate; exit fees use the more conservative of the stored and current published fee. The liquidation fee is taken from instrument metadata. The account and halt state persist across service restarts in `PERPS_PAPER_DB`.

Enable the simulator with `ENABLE_PERPS_PAPER_ENGINE=true` together with `ENABLE_PERPS_MARKET_DATA_WORKER=true`. Configuration lives under the `PERPS_PAPER_*` environment variables in `.env.example`. The process supervisor launches only the public data + paper worker, removes account credentials and `LIVE_*` settings from its child environment, forces `DRY_RUN=true` and `LIVE_EXECUTION_ENABLED=false`, and restarts the child with bounded backoff. The Perps collector itself retries transient failures with capped backoff and exits after ten consecutive failed cycles so the supervisor can restart a clean process. The PID-1 supervisor also watches the worker's atomic heartbeat file: a live but stalled process is terminated and restarted after a market-limit-aware timeout (default 330 seconds for ten discovery markets plus up to ten prioritized open positions, polled every 30 seconds). A heartbeat from an earlier process is never accepted as evidence that a restarted child is healthy, and the `/health` stale-data threshold uses the same timeout to avoid conflicting health decisions. Readiness also cross-checks the child heartbeat against the PID-1 supervisor state: a leftover fresh status file cannot report a healthy Perps worker while the supervisor says it is restarting, stopped, or otherwise not running. The `/perps-market` and `/perps-paper` diagnostics use the same supervised-state check, and Paper entry readiness fails closed while the worker is restarting or its heartbeat is stale. `/perps-paper` returns an allowlisted portfolio summary including current open notional, effective leverage, peak drawdown and daily PnL, plus closed-trade win rate, net expectancy, profit factor when losses exist, gross/net realized PnL, and entry/exit/liquidation fees plus funding paid. These are descriptive all-time Paper statistics only; the endpoint labels them as not out-of-sample validated and not live-execution eligible. `/perps-market` reports collector health, and `/health` requires the paper engine and its most recent verified backups to be healthy when enabled. Every six hours (and immediately after startup) the Perps worker uses SQLite's online backup API to back up `PERPS_PAPER_DB` and `PERPS_MARKET_HISTORY_DB`, and also `PERPS_ORDER_JOURNAL_DB` when that durable journal has been created. It checks the exact expected source names against the verified manifest, records whether the Perps journal existed and was included, and marks backup health degraded if an existing journal was omitted. Backups receive SQLite integrity/foreign-key checks and SHA-256 manifests. Backups are on the same Railway persistent volume; they are not off-site disaster recovery. Backup failures are retried with bounded backoff and fail the service health gate. A risk halt is visible as an unhealthy health component; it does not reset automatically after restart.

**This Paper engine does not execute real Perps orders.** A separate dependency-injected IOC/FOK gateway and a read-only authenticated account preflight exist, and the read-only account supervisor now has an opt-in worker and PID-1 watchdog integration. The account worker is disabled by default and requires a mounted, permission-restricted credentials file; no real account credentials or session have been used in this repository's tests, and no production deployment has been activated. The gateway is not connected to this Paper engine or to a live strategy. The account preflight can resolve a read-only authenticated `fee_tier` against a fresh public Perps fee ladder only when an already-open session and fee provider are explicitly injected. Cross Margin, maker queue position, hidden liquidity, partial-fill queue behavior, and venue-exact liquidation are not simulated. Polymarket supports both Isolated and Cross Margin, but this paper model intentionally implements Isolated Margin only. It does not simulate the venue's insurance-fund or auto-deleveraging backstops, and its visible-book close is an approximation rather than a venue-exact liquidation execution. Incomplete exit depth leaves a position open and audited; a liquidation that cannot be filled or would exceed isolated collateral halts the paper account. Funding-history gaps halt rather than being approximated. The momentum signal has not been proven profitable or validated out of sample; no live-trading eligibility is asserted. Do not enable real-money execution from these results.

Point-in-time Perps replay is available through `perps_historical_replay.py`. For example:

```bash
python perps_historical_replay.py \
  --database ./data/perps_market_history.sqlite3 \
  --from "2026-11-01T00:00:00+00:00" \
  --to "2026-11-22T00:00:00+00:00" \
  --max-snapshots 10000
```

The replay uses the same `PERPS_PAPER_*` risk and signal configuration as the supervised worker unless explicit research overrides are passed through the Python API. It filters both market snapshots and published funding settlements by their original local receipt time before using them in each decision, and returns a bounded closed-trade log plus gross/net PnL, win rate and profit factor. It runs in a fresh temporary portfolio database and never modifies the running paper account. The replay is an in-sample historical diagnostic; it does not certify an out-of-sample edge or enable live execution. Market and funding history are retained for 14 days by default.

A scheduled GitHub Actions check (`Perps public market-data smoke`) fetches live public instruments, tickers and books and verifies at least one current usable market. It is a point-in-time integration check, not continuous hosting or an execution test. The normal Tests workflow also compiles the new engine, runs its unit tests, and smoke-tests the `/perps-paper` endpoint with the simulator disabled.

## Point-in-time Perps historical replay

Run `python perps_historical_replay.py --database data/perps_market_history.sqlite3 --from 2026-11-22T08:00:00+00:00 --to 2026-11-22T12:00:00+00:00` to replay a bounded captured-data window. The CLI defaults to the path in `PERPS_MARKET_HISTORY_DB` and supports a maximum of 100,000 snapshots per run. The replay creates a fresh temporary paper ledger and never modifies the live Perps paper account or the source history database.

For every simulated decision, the engine sees only market snapshots and published funding settlements whose original `received_at` timestamp is no later than that decision time; the as-of filter is applied in SQL before the history limit. Late or unavailable funding settlements remain unavailable and cause the existing fail-closed funding halt rather than being backfilled into earlier decisions. The report includes closed-trade gross/net PnL after recorded fees and funding, win rate, profit factor when defined, the first entry time, and up to 500 trade records. This is a chronological replay of the captured sample, not an independently held-out out-of-sample test or evidence of profitable edge. An optional `--entry-start` timestamp keeps all earlier observations available for signal warmup but suppresses new entries before that time; `lookahead_protection.pre_entry_snapshots_are_warmup_only` records whether this mode was used.

## Frozen-policy Perps walk-forward diagnostics

The separate `perps_walk_forward_evaluation.py` evaluates one unchanged Paper strategy over contiguous, non-overlapping chronological test folds. Each fold gets a fresh temporary Paper ledger. Earlier archived observations are replayed only to warm the signal history; `perps_historical_replay.py --entry-start` suppresses entries until that fold's test interval begins. Market and funding history remain filtered by their original local receipt time and the venue timestamps must not be later than the simulated decision.

Example:

```bash
python perps_walk_forward_evaluation.py \
  --database ./data/perps_market_history.sqlite3 \
  --from "2026-11-01T00:00:00+00:00" \
  --to "2026-11-22T00:00:00+00:00" \
  --folds 3 \
  --warmup-fraction 0.30 \
  --minimum-closed-trades-per-fold 20 \
  --minimum-total-closed-trades 60
```

A fold consistency pass requires the minimum closed-trade count, positive realized net PnL after modeled entry/exit Taker fees and published funding, profit factor above one, peak drawdown within the configured threshold, and no open/pending-exit positions at the test-window end. Positions are not forcibly closed to make a fold look complete. This is a fixed-policy research diagnostic, not a parameter tuner, production backtest, or independent statistical guarantee: samples may still be regime-dependent, funding/Mark observations are captured snapshots, visible depth does not model queue position or hidden liquidity, and the liquidation model is not venue-exact. Even when every fold passes, the report permanently sets `out_of_sample_validated=false` and `live_execution_eligible=false`; a separate independent review must assess the evidence before any release gate can change.

## Perps order journal and SDK reconciliation bridge — no live execution

The repository also includes `perps_order_journal.py`, a separate file-backed SQLite WAL journal for future Perps order lifecycle work. It persists a validated order intent and an append-only transition log before any future submission adapter is permitted to perform network I/O. The caller must use one stable 32-character lowercase hexadecimal `client_order_id`; reusing that ID with different immutable order terms is rejected.

The journal models `prepared`, `submission_pending`, `submission_unknown`, `acknowledged_unreconciled`, `open`, `partially_filled`, `cancel_pending`, `filled`, `cancelled`, `rejected`, and `manual_review`. A transport timeout is not treated as rejection, an SDK placement acknowledgement is not treated as a fill, and a cancel acknowledgement is not treated as cancellation. Ambiguous submissions have a single persisted submission attempt and **automatic resubmission is disabled**; the same client order ID must be reconciled against authenticated account orders. In-flight records left as `submission_pending` by a process crash can be recovered to `submission_unknown` only after an external single-worker/exclusive lock is acquired; recovery is explicit, not automatically run by the journal constructor, so a healthy concurrent process is not mistaken for a crash.

`perps_sdk_reconciler.py` is a read-only bridge that accepts an already-open SDK Perps session by dependency injection. It queries `fetch_orders(client_order_id=...)`, maps documented SDK states such as `partial`, `auto_cancelled`, and `ioc_no_fill`, and calculates average execution price from matching account fills. If fills are delayed, duplicated, incomplete, or the order lookup is ambiguous, the local order remains unresolved rather than substituting the limit price or assuming execution. Unexpected remote order terms or future SDK statuses require manual review. The bridge does **not** open a wallet session, load credentials, sign, place, cancel, transfer collateral, or arm auto-cancel.

The pinned runtime dependency is `polymarket-client==0.20.1`. The SDK marks its Perps account-session surface experimental and warns it may change incompatibly, including in patch releases. `perps_sdk_contract.py` validates the exact package pin and public method/parameter surface without creating a client, reading credentials, connecting, signing, or trading. `perps_sdk_reconciler.py` remains limited to an injected, already-open read-only session. `perps_live_execution_gateway.py` now contains a deliberately narrow, dependency-injected IOC/FOK limit-order adapter. It requires the source-controlled release gate, explicit arming and acknowledgement, a local kill-switch check, a durable pre-send journal intent, server Auto-Cancel verification, read-only account preflight, target-instrument Isolated Margin/leverage confirmation, and an independent risk decision. Before order submission it rechecks a fresh account-tier-resolved public Perps fee schedule, includes the estimated entry Taker fee in the available-margin check, and requires the risk decision's maximum round-trip fee budget to cover both entry and exit at the current Taker rate. Ambiguous submit outcomes remain `submission_unknown` and are never automatically retried; the eventual venue order/fills must be reconciled using the same client order ID. This adapter is **not wired into a running supervisor, strategy signal, or deployment**, and the source-controlled release gate remains closed; no authenticated session, key, or order has been created. No private key or session credential belongs in repository files or chat; any future authorized deployment would use a managed secret store.

The disabled-by-default `perps_auto_cancel_guard.py` implements the SDK's one-shot 60-second Perps dead-man's switch lifecycle for a future manually authorized gateway. It refreshes the server-side deadline every 20 seconds, reads server status after each arm, checks the returned deadline within a strict two-second tolerance and the remaining daily trigger quota, and atomically writes a local Kill Switch file if arming or verification fails. It is hard-gated by `perps_live_release_gate.py` before any account modification. A graceful shutdown requires a flat portfolio, cancels resting orders, polls until none remain, then reads the portfolio again and verifies the order list is still empty immediately before disarming. If exposure reappears, the account state is unknown, cancellation fails/slow, open orders reappear, or disarming cannot be verified, it fails closed and leaves the server-side deadline armed. Its report never grants order permission. The single-order `perps_live_execution_gateway.py` also checks the same source-controlled release gate before it can reach `place_order`. All eight source-controlled release conditions remain false in this branch, so neither module can make a live account call or submit an order; neither is wired to an authenticated session supervisor or deployment.

The read-only `perps_account_preflight.py` module adds an account-level consistency check for a future supervised gateway. It reconciles submitted local intents first, then reads the portfolio, balances, open orders, instrument leverage configuration, and auto-cancel status. It blocks on stale/future portfolio snapshots, liquidation state, unavailable or nonpositive order margin, Cross Margin or leverage above the configured policy (default 2x), excessive positions/orders, unknown remote orders, missing venue-side open orders for locally active orders, unresolved local intents, invalid balances, or an unarmed/near-expiry/depleted auto-cancel schedule. Its result always includes `order_placement_authorized=false`; it is a read-only preflight, not a live order permission or a guarantee against market changes after the snapshot. All account reads and intent reconciliations have a strict bounded timeout; a slow or stalled SDK call fails closed. The preflight remains read-only and does not arm the dead-man's switch itself. It can also accept a public fee-schedule provider, retrieve the full `/v1/info/fees` tier ladder without blocking the event loop, and resolve the authenticated portfolio's `fee_tier` to the exact current Perps maker/taker rates. It requires a fresh timezone-aware fee snapshot (default maximum age 15 minutes), validates all tier thresholds/rates, and refuses missing, stale, out-of-range, or conflicting ladders. `PerpsPublicMarketDataProvider.load_fee_schedule_snapshot()` supplies this read-only public snapshot; a caller can inject that method directly as the preflight's synchronous `fee_schedule_provider`. The production/live gateway is not yet wired to instantiate that reader or session, so this is a tested preflight capability, not proof of live account fee configuration.

`perps_account_supervisor.py` wraps an injected `PerpsAccountPreflight` in a continuous, bounded read-only supervision loop. On startup it converts crash-interrupted `submission_pending` rows to `submission_unknown`, then polls account preflight on a configurable interval, publishes an atomic heartbeat/status file, records only allowlisted account telemetry, and retries transient API failures with capped backoff. The HTTP status endpoint independently checks heartbeat age (three poll intervals, at least 60 seconds), downgrading stale workers to `stale` and future-dated heartbeats to `invalid` rather than displaying an old `healthy` status. Its status is exposed by the read-only `/perps-account-supervisor` route and the Control Center. The status includes reconciliation blockers and does not publish wallet addresses, account identifiers, order IDs, raw SDK objects, or credentials.

**The supervisor requires the same `submission_lock_path` used by `PerpsLiveExecutionGateway`.** It holds that shared exclusive lock across startup recovery and each account inspection. If the lock already exists, it skips both recovery and preflight, reports `submission_lock_unavailable`, and retries later. A stale lock is never auto-deleted; an operator must first establish that the submitting process is stopped and reconcile the account/journal before clearing it. Startup recovery runs only while this lock is held, so an in-flight submission cannot be misclassified as a process crash. The supervisor's only account actions are read APIs; its local journal reconciliation is durable bookkeeping, not order placement or cancellation.

The repository now includes `perps_read_only_session.py`, a strict facade that exposes only the account read methods required by preflight/reconciliation plus `close()`; it does not forward the raw SDK session or expose order placement, cancellation, Auto-Cancel, leverage, margin, or other write methods. `perps_account_supervisor_worker.py` is an opt-in PID-1 child. It loads a strict JSON credential document only from a mounted file (regular non-symlink, at most 16 KiB, exact owner-only mode 0400 or 0600, owned by the account worker's effective UID), checks the expiry of existing delegated Perps credentials, and never calls `open_perps_session()` with missing credentials. **Security boundary:** this is only an application-level API allowlist. The SDK's delegated `PerpsCredentials` includes a private key and shared secret, while its raw `PerpsSession` supports write operations; Polymarket does not enforce a read-only credential scope here. Treat the bundle as capable of signing orders. Python name-mangling/facades and a strict child environment are not a security sandbox.

The pinned SDK contract now also checks the private construction hook `AsyncSecureClient._create(private_key, wallet, credentials, validate_credentials)`, required credential model fields, coroutine-vs-sync method semantics, and the sync fill/funding paginator contract. The worker uses that pinned path with supplied CLOB API credentials and `validate_credentials=False`, a fixed Deposit Wallet address, and existing delegated Perps credentials. This deliberately avoids the public client factory's API-key derivation fallback and default Deposit Wallet readiness/deployment flow. A failed/expired delegated session is an error; it is never silently recreated or re-signed. The use of the SDK's private construction hook is version-sensitive and therefore fails closed if the exact pinned signature/model surface changes.

The PID-1 supervisor starts this worker only when `ENABLE_PERPS_ACCOUNT_SUPERVISOR=true`, the public Perps worker is enabled, and a mounted secret file exists as a regular, non-symlink JSON file no larger than 16 KiB with exact owner-only modes `0400` or `0600` (no group or other-user access). Its child environment is a strict allowlist (data paths, timing limits, secret-file path, `DRY_RUN=true`, and `LIVE_EXECUTION_ENABLED=false`); API/private-key values are not passed to the HTTP, Paper, or public market-data workers. It shares the exact `PERPS_SUBMISSION_LOCK_PATH` with the future gateway, restarts with bounded backoff, and terminates/restarts a child whose heartbeat exceeds the configured watchdog timeout. The worker handles SIGTERM, closes its Perps and secure-client sessions, and leaves status in the allowlisted supervisor files. **However, the current Docker image runs the PID-1 supervisor and all its children under the same `appuser` UID.** Omitting the secret path from sibling environments does not stop a compromised same-UID process from opening a mounted file if it discovers the path. For that reason this worker must remain disabled in the shared app container; a production account observer needs a separately deployed service/container or another verified OS/network boundary, with the secret mount available only to that isolated principal. The current flag is an integration/test hook, not approval to mount real account secrets into the shared service.

The mounted JSON file must have exactly these fields: `owner_private_key`, `deposit_wallet`, `clob_api_credentials` (`key`, `secret`, `passphrase`), and `perps_session_credentials` (`proxy`, `private_key`, `secret`, `expires_at`). The delegated credential expiry must be a timezone-aware future ISO-8601 timestamp. The worker also rejects executable file modes and duplicate JSON keys, opens with `O_NOFOLLOW` where supported, and never logs file contents. Never provide this file or any of its values in chat or commit them to source control.

When enabled, the account worker becomes a required component for `/health`. The endpoint reports degraded/unhealthy when account reconciliation is blocked, the worker process is restarting/stopped, or the worker heartbeat is stale/future-dated. The Control Center and `/service-supervisor` expose the PID-1 process state and restart count separately from the worker's last read-only preflight result.

The account worker remains disabled by default and has not been run against a real account. A deployed secret-manager mount, an isolated account-observer service, and a production recovery test are not yet available in this repository environment. The SDK delegated session is **not** venue-scoped to read-only and its credential can sign write operations. The facade is an application-level allowlist that prevents ordinary callers from being handed write methods; it is not a security boundary against hostile code or code that introspects Python objects. All children currently run as the same `appuser` UID, and no OS-level process isolation is verified. Therefore do not mount actual account secrets or enable this worker in the shared app container. Production activation requires a separately isolated service/principal and verified filesystem/network boundaries, followed by a production account recovery exercise.

The shared `backup_paper_data.py` routine also includes `PERPS_ORDER_JOURNAL_DB` (default `DATA_DIR/perps_order_journal.sqlite3`) when present. It uses SQLite's online backup API, verifies integrity and foreign keys, publishes a SHA-256 manifest, and retains bounded per-database history. The backup test reopens the copied Perps journal through `PerpsOrderJournal`, verifies its integrity, and checks that the pending order state and append-only event history survived. This is a local restore smoke test, not an off-site backup or disaster-recovery drill; the independent recovery gate therefore remains closed.

The journal and SDK bridge are covered by unit tests and compile checks. They are not connected to the Perps momentum Paper signal and do not make the strategy live-eligible.
The gate status is also exposed through the read-only `/perps-live-gate` endpoint. It reports which release conditions are still blocked and does not expose credentials or initialize an authenticated SDK session.


`perps_live_release_gate.py` is an additional source-controlled safety barrier. Its release requirements cannot be overridden by environment variables: out-of-sample strategy validation, stress/failure tests, a verified auto-cancel supervisor, verified account fee tier/current fee schedule, real account integration, geoblock and jurisdiction review, managed secrets/deployment review, and an independent risk/recovery review must all be signed off. Runtime flags and a confirmation phrase are necessary but cannot override any source gate. The current result is deliberately `eligible_for_separate_gateway_review=false`; the module does not create an SDK session or submit orders.
\nThe Perps SDK contract workflow runs on pushes and pull requests. It installs the exact runtime lock and checks the SDK surface offline; it does not make API calls or exercise metered integration tests. Official Perps APIs remain experimental and this check is compatibility evidence only, not live-execution approval.\n

