# 60% probability paper bot

This standalone paper bot starts with $500 and follows Kalshi's live KXBTC15M contracts. Requires at least a 60% estimated win probability for the purchased side and more than one minute remaining. It requires a 12¢ estimated edge over the selected side's ask. For example, a 60% forecast at 48¢ qualifies if there is enough depth. Each paper trade costs $20–$50. No fee is applied to sizing, entry decisions, model evaluation or P/L.

The bot uses 36 completed five-minute Coinbase BTC candles. It estimates how likely the chart trend is to carry BTC above or below the Kalshi target by expiry, using the latest completed close and historical volatility. It waits for a qualifying prediction even after extended inactivity. Candidate models are tested on future settled observations before promotion. Successful reviews raise the sizing target from $20 in $10 steps to $50. A failed review resets the target to $20. The forecast is experimental.

Quotes and settlement come from Kalshi. The graph shows contract prices in cents. The BTC dollar target comes from the contract. Paper fills assume the displayed price and size were available when observed. Quotes from different tickers are kept separate.

## Deploy on Render

Existing deployment: replace the project files and redeploy with the same disk and database. Cash, positions and trade history carry over. The chart model starts fresh at a $20 target.

For a new deployment, extract this ZIP. Upload the files inside to a new GitHub repository. In Render create a new Blueprint and select that repository. The blueprint requests a paid Starter web service with a persistent disk. Review the cost before confirming. Use the new service URL and generated DASHBOARD_PASSWORD to open the dashboard. Use a separate service and disk for each bot. PAPER_DB_PATH is set in the blueprint. Keep the existing V5 service in place.

## Local setup

Run `python app.py` using Python 3.11 or later, then visit http://127.0.0.1:8503. Each worker polls about every ten seconds. The database is stored in the working directory by default. Run `python -m unittest -v` for offline tests.
