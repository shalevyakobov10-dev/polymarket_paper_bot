# Polymarket Paper Trading Bot

Research-first project for studying public Polymarket market data and evaluating strategies in paper-trading mode only.

## Safety
- No live orders or real-money trading.
- Use time-ordered train/test splits and guard against look-ahead leakage.
- Verify API connectivity, tests, and deployment in the target environment before claiming they work.
- Never commit API keys, passwords, or access tokens.

## Intended workflow
1. Collect and validate public market data.
2. Analyze historical data and evaluate strategies out of sample.
3. Record hypothetical paper trades and results.
4. Run automated tests and verify health endpoints.
5. Consider 24/7 cloud deployment only after research and operational checks pass.

Backtests and paper results do not guarantee future performance.