# Kalshi Bitcoin Paper Trader V5

V5 runs a $500 simulated account for Kalshi’s KXBTC15M series. The worker and iPhone dashboard run together. Once hosted, the worker can run while your phone is closed. It uses public market data and requires no exchange account or trading keys.

## Deploy to Render from an iPhone or computer

1. Unzip this download. On iPhone, tap the ZIP in Files, then open the extracted folder.
2. Create a **new GitHub repository** for V5. Use **Add file → Upload files** and upload the files inside the extracted folder. Check that `render.yaml`, `app.py`, `engine.py`, `strategy.py` and `dashboard.html` are visible at the top level of the repository. Commit.
3. In Render choose **New → Blueprint**, connect the V5 repository, and review the proposed web service and persistent disk. The blueprint requests a paid Starter service and a 1 GB disk for the ledger and models. Review the price shown by Render before creating the service.
4. Once deployed, open the V5 service’s **Environment** page. Reveal and copy its generated `DASHBOARD_PASSWORD`. Open the service URL in Safari, paste the password into the sign-in form and tap **Open dashboard**. The page uses a secure session cookie for 12 hours.
5. Check for **Worker online** and a recent heartbeat. After roughly 3–4 minutes of fresh BTC samples and an eligible market window, the first observation can be recorded. Check the status line for the current reason when no order opens. Tap Safari’s Share → Add to Home Screen if desired.

V5 starts a separate ledger at `/var/data/kalshi_v5.sqlite3`. The nine V4 trades are not imported as training data. V5 needs a prediction and market snapshot taken before each outcome. Your deployed V4 service is independent. You can compare the two dashboards if you choose to run both.

**Daily loss setting:** V5’s new-service default is $250. On an existing service, set its `DAILY_LOSS_LIMIT` to 250 on Render. The bot reserves the full cost including estimated fees of every open position against today’s realized paper P/L limit (UTC) and blocks entries when the remaining allowance falls below the $20 starting budget. It continues observing and resumes eligible entries when the UTC day changes. Manual pause stays in effect until you resume.

## Run locally

Use Python 3.11 or later. No dependency installation is required.

```sh
python app.py
```

Open http://127.0.0.1:8501 on the same computer. Leave the process running. The default local database is `kalshi_v5.sqlite3` in the working directory. For the hosted single-instance process, use `python app.py` as the start command and mount the persistent disk as specified in `render.yaml`.

## Three-hour Bitcoin chart forecast

V5 downloads 36 consecutive completed five-minute BTC-USD candles from Coinbase. It loads history on startup and refreshes when each five-minute interval closes. The dashboard shows the direction forecast and the 15-minute, one-hour and three-hour price changes. Missing, incomplete or stale chart history blocks new entries; settlement runs before the candle request.

The forecast estimates short-term trend continuation. It combines log returns per five minutes across the three windows with weights of 50%, 30% and 20%, caps the drift using realized volatility, and reports MIXED for weak or conflicting short-term momentum. It projects the latest spot price to the contract’s close. An UP forecast can qualify for YES only if that projection reaches the strike; DOWN can qualify for NO only if it finishes below the strike. MIXED skips the trade. All existing edge, depth and risk checks still apply.

The direction rule is an unvalidated paper experiment. It can reduce trade frequency and is distinct from the existing settlement-probability model, which continues to learn. The baseline, candidate and active model use the direction filter on new decisions. Already recorded orders and historical validation results retain their original settings. Cash, paper trades, and learning state use the existing V5 database.

For an existing Render install, deploy the updated engine.py, strategy.py, app.py and dashboard.html together on the same service with the same persistent disk. No extra dependencies, credentials, settings or account reset are required.

## How V5 decides

- Every scan checks for settlement, samples the latest Coinbase BTC-USD tick, then looks for an eligible market. It waits for fresh price history, at least 75 seconds and at most 12 minutes remaining, and a quoted spread of 10¢ or less.
- V5 checks the first eligible quote, then checks again after crossing 5 and 2 minutes remaining. Each window can trigger at most one evaluation, and each market can receive only one trade. Previously recorded markets from older versions finish under the original rule. The learner counts each market once using its initial forecast; validation tracks the first qualifying $20 order for each policy across the same entry windows. Either side needs at least 12 percentage points of estimated advantage after the simulated fee and enough quoted size to fill the whole order. A skipped market still supplies a forecast to evaluate. V5 can hold up to five paper positions, one per distinct eligible ticker. It checks all eligible markets in closing-time order and settles each independently. Overlapping positions are possible when earlier contracts are still awaiting final results. The 15-minute BTC series may offer only one eligible contract at a time. Skipped markets are observed separately and use no position slot. Trade frequency varies with market conditions.
- The initial probability estimate uses the distance between spot and strike, recent volatility and time remaining. The learning model adjusts that estimate using standardized distance, market midpoint, short-term BTC movement and time remaining. Coinbase spot is a proxy for the official benchmark. Kalshi’s final result determines the payout.
- The simulated fee is estimated as 0.07 × contracts × price × (1 − price), rounded up to the cent. Quotes, top-of-book depth and fees are approximations of execution. All budgets include the estimated fee. Whole contracts are rounded down to stay inside the cap, so a trade with a $20 budget can cost slightly less than $20.
- Available cash excludes the cost of open positions. Realized P/L updates only at settlement. The dashboard shows when the worker is online, when entries are paused and why a trade was skipped.

## Learning and validation

Each binary settlement updates an online logistic calibration model inside the same database transaction that records the outcome and payout. A retry therefore cannot train twice or credit cash twice. The worker also learns from observed, skipped markets. Nonbinary final settlements with an official numeric settlement value are accounted for but excluded from binary training. A market awaiting a usable final result remains pending.

After 30 labeled markets, V5 freezes a copy of its candidate. It records the candidate, active model and baseline forecasts before each of the next 40 markets resolves. The online learner keeps updating separately. For each policy, the comparison simulates the same $20 budget, quotes, liquidity and fee rules. Each 40-market block is used once.

For promotion, the candidate must beat both the current model and the starting baseline on the new block. It needs at least 12 qualifying simulated trades, a mean squared probability error (Brier score) improvement of at least 0.005, higher net profit after estimated fees, and positive net profit even after excluding its best trade. These are initial design choices. A passing block is evidence from that block, not a guarantee of future performance.

The cap moves **$20 → $30 → $40 → $50**, at most one step per completed block. Further increases require a newly promoted model or a current learned model that passes the comparison against the baseline again. A failed validation drops the cap back to $20. The first possible increase is after 70 labeled markets. A failing challenger never becomes active.

**60% filter experiment:** The dashboard compares all actual settled V5 paper trades with the subset whose chosen side had a forecast of at least 60% at entry. Both views report count, win rate, P/L and drawdown at original trade sizes. This is a subset comparison and does not simulate reinvesting freed cash. The filter does not automatically change the strategy.

## History and verification

Download the trade CSV from the dashboard. Archived sessions retain their account numbers, forecasts, results and P/L. Archive and reset starts a new $500 account and a fresh learning history. To reset while a market is pending, pause entries and wait for its settlement.

Run the regression suite with `python -m unittest -v`. It exercises the HTTP dashboard, paper accounting, retry protection, simulated market scans, loss limits, validation rounds, and changes to the risk cap. Test outcomes are synthetic and do not measure trading performance.

Data references: [Kalshi public market data](https://docs.kalshi.com/getting_started/quick_start_market_data), [Kalshi market fields](https://docs.kalshi.com/api-reference/market/get-market), [Coinbase ticker](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-ticker), [Render persistent disks](https://render.com/docs/disks).
