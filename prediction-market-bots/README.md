# prediction-market-bots

Paper-trading bots for [Kalshi](https://kalshi.com) prediction markets. **They never place real orders.** They read Kalshi's public market data and simulate, as honestly as public data allows, what would have happened if a real resting order had been placed.

> **Not financial advice.** This is an educational project. Paper results can differ from live trading, and past paper results don't predict future returns.

## What's here

| Folder | What it is |
|---|---|
| [`mentions_bot/`](mentions_bot/) | **Mention-market long-shot maker.** A paper bot with a realistic fill simulator and a live dashboard. |
| [`tests/`](tests/) | Offline unit tests for the fill engine, sizing caps and settlement. |

Everything uses the Python standard library only. No API key, no login and no `pip install` are needed.

## The mentions bot

Kalshi lists "mention" markets such as *"Will the speaker say **Tariff** during this event?"*. Many words trade as cheap long shots at 2¢–30¢ for YES.

**The idea:** across betting and prediction markets, cheap long shots tend to win *less* often than their price implies (the favourite–longshot bias). Resting orders (makers) also tend to do better than orders that cross the spread (takers). So the bot **sells YES at the ask**, which is the same as bidding NO at 1 − ask. It joins the back of the queue and holds to settlement.

**What it does each day:**
1. Finds every open mention series (Trump speeches, earnings calls, press briefings, and so on).
2. At **8:00 AM ET** on an event's day, it snapshots the event once. For every word with a YES ask between 2¢ and 30¢, it rests a paper NO bid at the best NO bid price.
3. Every minute it re-reads the order book and the public trade tape to decide how much of each order would really have filled.
4. When the market settles, it books P&L on **filled contracts only**, using Kalshi's actual result and the series' own fee schedule.

### How fills are simulated

This is the part most paper bots get wrong. A resting order is not filled just because the price touched it.

- **Queue position.** At placement, the bot records how many contracts already rest at its price. Under price-time priority, it is behind all of them.
- **Trades at our price** eat the queue ahead of us first. Only what's left over fills us, so partial fills are tracked.
- **A trade *through* our price** means the whole level cleared, so the order is a full fill.
- **Book changes.** If the level grows, the new orders are behind us. If it shrinks with no trade to explain it, those are cancels, split pro-rata between the orders ahead of and behind us.
- Unfilled contracts are cancelled at close. They cost nothing and earn nothing.

Four fill models are reported side by side so you can see how sensitive the results are to the assumptions:

| Model | Assumption |
|---|---|
| **QUEUE** | Realistic: queue tracking as above. This is the one to decide on. |
| QUEUE-SAFE | Worst case: every cancel is assumed to be behind us. |
| STRICT | Filled only if a trade printed through our price. |
| TOUCH | Optimistic: filled the moment any trade touches our price. |

### Risk controls (paper account)

- $500 starting bankroll (`START_BANK`).
- Quarter-Kelly sizing on current equity, capped at 5% per order.
- Resting orders reserve cash the way Kalshi does. A signal is skipped if there isn't enough free cash.
- All words in **one event** can reserve at most 20% of equity, cheapest words first.
- "Event does not qualify" (cancellation) markets are skipped.
- If the computer sleeps or the network drops, the bot records the gap and reports it. It doesn't pretend it was watching.

## Running it

Requires Python 3.9+.

```bash
cd mentions_bot
python3 kalshi_mentions_bot.py selftest   # check connectivity and parsing of live data
python3 kalshi_mentions_bot.py run        # run forever (Ctrl-C to stop, safe to restart)
./run.sh                                  # same, auto-restarts, keeps a Mac awake
python3 kalshi_mentions_bot.py report     # full report: 4 fill models, groups, calibration
python3 kalshi_mentions_bot.py report --csv   # also writes orders.csv
```

While `run` is going, open **http://127.0.0.1:8765** for the dashboard. It shows balance, edge with a confidence interval, the equity curve and drawdown, open bets by event, daily P&L, results by speaker group, calibration, and a trade log.

Results are stored in `paper_mentions.db` (SQLite) next to the script. Delete that file to start over.

**macOS SSL error?** Run `/Applications/Python 3.x/Install Certificates.command`, or `python3 -m pip install --user certifi`.

## Reading the report

- **Edge ¢/contract** is average profit per filled contract after fees. The 90% range comes from a bootstrap that resamples whole *events*, because words in the same speech are correlated.
- **Proven?** only turns green when the bottom of that range is above zero. Until then, treat the results as noise.
- **Calibration** compares how often YES actually happened with the price it traded at, for each price bucket. That is the long-shot bias the strategy relies on.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The tests run offline. They cover queue consumption, partial and full fills, trade-through, duplicate trades, the order of book and tape updates, pro-rata cancels, Kelly sizing, the per-event cap, settlement P&L, fees, and skipping cancellation markets.

## Disclaimer

This software only simulates trades and never sends orders. It is provided as-is, without warranty. Nothing here is investment advice. Trading prediction markets involves risk of loss. Check that you're allowed to trade on Kalshi where you live before trading real money.
