# 60% probability paper bot

This standalone paper bot starts with $500 and follows Kalshi's live KXBTC15M contracts. Requires at least a 60% estimated win probability for the purchased side and more than one minute remaining. It requires a 12¢ estimated edge over the selected side's ask. For example, a 60% forecast at 48¢ qualifies if there is enough depth. Each paper trade costs $20–$50. No fee is applied to sizing, entry decisions, model evaluation or P/L.

The initial forecast uses 30–90 seconds of the current contract's YES midpoint history. It projects recent movement for up to two minutes with a maximum adjustment of 20 percentage points. After settlement, the bot learns a correction using quote movement, midpoint, spread and remaining time. Candidate models are tested on future settled observations before promotion. Successful reviews raise the sizing target from $20 in $10 steps to $50. A failed review resets the target to $20. The forecast is experimental.

Quotes and settlement come from Kalshi. The graph shows contract prices in cents. The BTC dollar target comes from the contract. Paper fills assume the displayed price and size were available when observed. Quotes from different tickers are kept separate.

## Deploy on Render

Extract this ZIP. Upload the files inside to a new GitHub repository. In Render create a new Blueprint and select that repository. The blueprint requests a paid Starter web service with a persistent disk. Review the cost before confirming. Use the new service URL and generated DASHBOARD_PASSWORD to open the dashboard. Use a separate service and disk for each bot. PAPER_DB_PATH is set in the blueprint. Keep the existing V5 service in place.

## Local setup

Run `python app.py` using Python 3.11 or later, then visit http://127.0.0.1:8503. Each worker polls about every ten seconds. The database is stored in the working directory by default. Run `python -m unittest -v` for offline tests.

### 30-minute paper entries

Every paper entry restarts a rolling 30-minute clock. Once due, the worker can bypass signal thresholds and the normal strategy window. It chooses the affordable side with the highest estimated edge, using the quote midpoint when a model estimate is unavailable. These entries use the smallest whole-contract size reaching $20 and appear as “30-min” in trade history. Market availability, valid quotes, worker uptime, entry cutoff and paper risk limits can delay a fill.
