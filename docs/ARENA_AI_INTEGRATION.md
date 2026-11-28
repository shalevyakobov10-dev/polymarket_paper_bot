# ARENA-AI integration contract

Status: research and implementation plan; no cross-repository code has been imported by this document.

## Verified repository state (2026-11-28)

- Polymarket bot event/news branch: `research/event-aware-news-signals`.
- Polymarket bot PR: [#42](https://github.com/shalevyakobov10-dev/polymarket_paper_bot/pull/42), currently Draft and unmerged.
- ARENA-AI shared-risk chain currently contains Draft PRs:
  - [#9 shared Paper portfolio risk](https://github.com/shalevyakobov10-dev/ARENA-AI/pull/9)
  - [#10 combined mark-to-market stop](https://github.com/shalevyakobov10-dev/ARENA-AI/pull/10)
  - [#15 UTC day-start baseline](https://github.com/shalevyakobov10-dev/ARENA-AI/pull/15)
  - [#16 read-only unified risk CLI](https://github.com/shalevyakobov10-dev/ARENA-AI/pull/16)
  - [#17 synchronized cross-venue marks](https://github.com/shalevyakobov10-dev/ARENA-AI/pull/17)
  - [#18 Paper/live ledger isolation](https://github.com/shalevyakobov10-dev/ARENA-AI/pull/18)
  - [#22 manual PERPS risk parity](https://github.com/shalevyakobov10-dev/ARENA-AI/pull/22)
- The bot currently has separate SQLite persistence paths for the event-paper portfolio/history and the Perps paper engine. ARENA's unified-risk implementation expects a shared Store/SQLite ledger and a Linux `fcntl` advisory lock. These are not drop-in compatible.

## Integration boundary

Do not copy ARENA's `src/pmmb/unified_risk.py` directly into this repository. First introduce an explicit adapter boundary that reads both bot ledgers without changing either engine's schema or silently mixing Paper and Live records.

The adapter must return a typed, timestamped snapshot with:
- Polymarket Paper open positions, cost basis, fees, cash, realized PnL, and current mark/unrealized PnL;
- Perps Paper open positions, isolated margin, fees, funding, cash/equity, realized PnL, and current mark/unrealized PnL;
- UTC-day baseline identity and capture time;
- source database identity, schema/version, read completion time, and completeness flags;
- per-venue mark timestamps and measured cross-venue skew;
- a stable failure reason when either ledger is unavailable, malformed, stale, incomplete, or in the wrong mode.

A snapshot with missing, stale, incomplete, or unsynchronized data must deny new entries. It must never be interpreted as zero exposure or zero loss.

## Admission and concurrency contract

1. Both Paper engines call one shared admission interface immediately before committing an entry.
2. The interface serializes cross-engine admission with one OS-level lock that is supported by the actual deployment platform.
3. While holding the lock, it reads both ledgers, validates mode and snapshot completeness, computes combined capital utilization, open-position count, UTC-day net PnL including fees/funding and mark-to-market movement, and current drawdown.
4. It validates the proposed allocation and rechecks that the source snapshots are still within freshness/skew limits.
5. The lock remains held through the local durable entry commit. A failure to acquire the lock, read either ledger, or persist the entry fails closed.
6. Process crash recovery must not automatically replay an uncertain broker submission. Paper and Live ledgers remain strictly isolated.

## Required regression tests before implementation is accepted

- One venue database missing, corrupt, locked, or schema-incompatible -> deny.
- One ledger read succeeds while the other fails -> deny, never partial approve.
- Stale marks and cross-venue mark skew above the configured maximum -> deny.
- Disabled Perps Paper engine with existing Paper positions -> include those positions in combined risk.
- Perps configured Live while Paper admission is active -> never read Live positions into the Paper budget; reject an enabled non-Paper arm.
- Daily loss includes realized PnL, open mark-to-market PnL, entry/exit fees, and funding without double-counting prior-day fees.
- Concurrent Polymarket and Perps entries cannot exceed combined capital or position-count limits.
- Crash while holding the lock releases it; uncertain execution remains latched for manual reconciliation.
- UTC-day baseline is immutable and concurrency-safe.
- Windows behavior is explicit: either provide a tested portable lock implementation or declare Linux-only support. Do not claim cross-platform support while importing `fcntl` unconditionally.

## ARENA review notes

- ARENA PR #18's Paper/live ledger isolation is a prerequisite conceptually; the bot adapter must enforce the same boundary even though its stores are separate.
- ARENA PR #17's synchronized-mark check and #15's immutable UTC baseline are useful design references, but their Store-specific SQL must not be transplanted.
- ARENA PR #22 adds manual PERPS risk parity. Its acceptance must be based on its own current-head CI and dependency chain; a Draft PR is not proof of a merged or production-ready feature.
- The bot's current `risk_manager.py` is a final gate for the event-paper entry path, not yet a shared cross-engine ledger guard. Do not describe it as unified portfolio risk until the adapter and both engine integrations are implemented and tested.

## Safety and release gates

This integration remains Paper-only. No signing, live order placement, secret loading, live account activation, or production deployment is authorized by this plan. A successful CI run proves only the tested code paths; it does not prove profitability, venue-wide coverage, or live readiness.

## Adapter implementation status (2026-11-28)

A first read-only adapter has been added in `unified_paper_risk.py`, with synthetic regression coverage in `tests/test_unified_paper_risk.py`.

- It opens the event-paper account DB, Perps Paper account DB, and Perps market-history DB in SQLite read-only mode.
- It checks required schema fields, current UTC-day baselines, timestamped Polymarket observations, Perps mark agreement with public market history, maximum mark age, and cross-venue mark skew.
- It calculates combined equity, daily PnL from each ledger's stored day-start baseline, realized PnL, unrealized PnL, open exposure, Perps margin, and open-position count. Combined peak equity and combined drawdown remain unavailable until a shared high-water-mark ledger exists.
- A missing, malformed, stale, future-dated, skewed, or inconsistent data source raises `UnifiedPaperRiskError`; it is never treated as zero.
- The Perps ledger does not persist immutable initial capital in its current schema. The adapter therefore reports combined starting capital as unavailable instead of substituting peak equity or a daily baseline.
- A Perps halt is reported as account state; it is not silently cleared.

**Important limitation:** this is a read-only snapshot adapter, not yet a shared admission gate. Neither engine is wired to reserve capacity through a cross-process lock, and this code must not be used to authorize entries until serialized admission, atomicity, and concurrency tests are implemented. It does not change either ledger and has no order-execution capability.

### Risk-metric integrity follow-up

The adapter now exposes each engine's own peak equity separately and leaves combined peak equity / combined drawdown unavailable. The two engines have independent high-water-mark histories; summing their peaks could fabricate a combined peak that never existed at one point in time. A true combined drawdown requires a shared portfolio-level high-water-mark ledger updated atomically with both engines. Until then, do not use a synthetic combined drawdown for risk authorization.

### Durable admission coordinator foundation (2026-11-28)

`portfolio_admission.py` introduces a Paper-only SQLite reservation coordinator. It uses `BEGIN IMMEDIATE` to serialize competing reservations across processes that share the coordinator database, and persists each admission through these states:

- `PENDING`: capacity reserved before the venue ledger commit.
- `AWAITING_RECONCILIATION`: venue commit reported, but capacity remains reserved until a fresh ledger read confirms the entry.
- `RECONCILED`: the durable venue entry has been verified; snapshots captured before reconciliation still count the reservation to avoid a stale-read race.
- `RELEASED`: the simple release path is limited to `PENDING` reservations and requires a valid read-only venue-ledger check proving the candidate ID absent. `AWAITING_RECONCILIATION` is never released by this simple path.

Reservations are never automatically expired. A crash or uncertain outcome therefore consumes capacity until an operator/reconciler resolves it, rather than silently freeing risk. The regression suite covers cross-venue contention, concurrent reservations, idempotency, durable reopen, stale/future snapshots, exact Polymarket/Perps ledger-entry verification, notional mismatch, absent/present entry release checks, and the rule that uncertain `AWAITING_RECONCILIATION` reservations remain locked. Replaying an existing admission key is now explicitly denied as a new entry, including when the original reservation is already reconciled; idempotency must never authorize a duplicate venue-ledger write.

**Integration status: foundation only.** Neither Paper engine calls this coordinator yet. It does not atomically commit a venue entry, read the two source ledgers itself, establish a shared peak-equity history, or authorize trading. The caller-side sequence and crash-recovery reconciler still need implementation and tests before it can be used as an admission gate. It must remain disabled for order authorization until that work is complete.


### Entry-path integration review (2026-11-28)

A source-level review of the current entry paths found an important transaction-boundary constraint that must be handled before wiring the coordinator:

- Event/Polymarket: `OfflineNewsPaperBot.run_cycle()` owns an outer `with self.db:` transaction and calls `_open_trade()` from inside it. The durable `trades` INSERT and account cash update currently occur inside that transaction.
- Perps: `PerpsPaperEngine.process_snapshot()` owns an outer `with self._lock, self.db:` transaction and calls `_try_open()` from inside it. The position INSERT, account cash update, and `PAPER_POSITION_OPENED` event are part of that transaction.
- Therefore, simply adding `reserve()` inside either private entry helper is insufficient. It would create a coordinator reservation while the venue transaction is still open, but the caller would need a carefully defined commit/reconcile boundary and deterministic rollback/absence verification. The current event helper returns a trade object before the outer transaction commits; the Perps helper returns no durable entry identity until its outer transaction commits.

#### Required integration shape

1. Refactor each engine to produce a fully validated entry candidate without writing it.
2. Move the admission boundary to a coordinator/orchestrator layer that can obtain a fresh, complete read-only snapshot of both Paper ledgers before opening the venue write transaction.
3. Derive a stable idempotency key from venue + source snapshot/event identity + strategy version. A repeated key must be a hard no-new-entry result.
4. Reserve the candidate notional and one position through `PortfolioAdmissionCoordinator.reserve()`. The coordinator database must be on a durable shared filesystem visible to both workers; separate container-local SQLite files do not provide cross-process or cross-venue serialization.
5. Begin the venue's short write transaction, revalidate local cash, duplicate-position constraints, and any venue-specific limits, then insert the position/trade and its audit event. Commit before transitioning the reservation to `AWAITING_RECONCILIATION`.
6. Re-read the venue ledger through a fresh read-only connection and verify the exact immutable entry identity, valid lifecycle status, and reserved notional. Only then call `reconcile_from_ledger()` with the exact venue database path and ledger entry ID. The coordinator performs the row lookup and notional comparison itself.
7. The basic `release_from_ledger()` path may release only a still-`PENDING` reservation after a valid read-only ledger confirms the candidate ID is absent. It does not release `AWAITING_RECONCILIATION`; that state requires a separate recovery coordinator to establish writer quiescence/lock ownership first. If the outcome is uncertain, leave the reservation consuming capacity and surface an operator-visible reconciliation alert.
8. Startup recovery must scan all non-terminal reservations and both Paper ledgers under a recovery lock. It may reconcile exact identity/notional matches; it must keep `AWAITING_RECONCILIATION` rows blocked until writer quiescence is established and leave ambiguous/mismatched records for manual review.

#### Additional blockers surfaced by the review

- A read-only combined snapshot taken before reservation is not itself a lock. All new entry paths must use the same coordinator and shared database; any legacy/uncoordinated writer must be disabled or treated as a deployment blocker.
- The current coordinator checks total exposure and position count, but it does not yet enforce a shared daily-loss budget, a true combined peak/drawdown, or immutable combined starting capital. Those metrics need their own durable shared portfolio ledger before they can be claimed as enforced controls.
- The coordinator's SQLite guarantees apply only when both workers access the same database file on a filesystem with supported SQLite locking semantics. Deployment storage and restart behavior must be tested; do not place the coordinator DB on an unsupported network filesystem.
- The existing Polymarket outer transaction and Perps transaction must not be held open while waiting on network data, news, or the other venue. Candidate construction and market-data validation happen first; the durable write phase must remain short.
- A Paper admission coordinator must reject any attempt to point either worker at Live account state. No Live code path is part of this integration.

**Status:** review and implementation sequence documented; engine code is not yet wired to the coordinator. These are explicit release blockers, not completed features.
