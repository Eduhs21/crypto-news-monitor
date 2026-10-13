#!/usr/bin/env python3
"""Fill MongoDB with fictional history so the dashboard can be explored at once.

The monitor normally needs a day or two of running before its charts say
anything. This script writes a synthetic week of snapshots, headlines and
recommendations instead, which is enough to click through every tab.

Everything written here is invented. Point it at a throwaway database.

Usage:
    python examples/seed_demo_data.py
    MONGO_URI=mongodb://localhost:27017/cryptopanic_demo python examples/seed_demo_data.py
"""
import math
import os
import random
from datetime import datetime, timedelta, timezone

from pymongo import MongoClient

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/cryptopanic")

#: Coins to simulate: symbol, CoinGecko id, starting price, daily volatility.
COINS = [
    ("BTC", "bitcoin", 67_400.0, 0.018),
    ("ETH", "ethereum", 2_480.0, 0.024),
    ("SOL", "solana", 148.0, 0.038),
    ("LINK", "chainlink", 11.80, 0.032),
    ("DOGE", "dogecoin", 0.1320, 0.045),
]
PORTFOLIO = {"BTC", "ETH"}

HEADLINES = [
    ("CoinDesk", "Spot ETF inflows hit a three-week high as institutions return"),
    ("Decrypt", "Layer-2 activity doubles after the latest fee cut"),
    ("Cointelegraph", "Exchange outflows suggest holders are moving to cold storage"),
    ("CoinDesk", "Regulator signals a softer stance on staking services"),
    ("Decrypt", "Validator client bug patched before it reached mainnet"),
    ("Cointelegraph", "Memecoin volume cools off after a record weekend"),
    ("CoinDesk", "Stablecoin supply grows for the fifth straight week"),
    ("Decrypt", "Hack drains $12M from a cross-chain bridge"),
    ("Cointelegraph", "Mining difficulty reaches another all-time high"),
    ("CoinDesk", "Derivatives funding rates flip negative across major venues"),
]

REASONINGS = [
    "Up {chg:.1f}% in the last hour against a flat market, with the three most "
    "recent headlines scoring positive on VADER and no negative catalyst.",
    "Steady climb over 24h, but the news flow is neutral, so confidence stays "
    "moderate rather than high.",
    "Momentum has stalled and sentiment turned mildly negative; the setup no "
    "longer justifies an entry.",
]

#: One snapshot every 40 minutes, matching the default MONITOR_INTERVAL_MINUTES.
INTERVAL_MINUTES = 40
DAYS = 7


def _walk(start: float, volatility: float, steps: int, rng: random.Random) -> list[float]:
    """Generate a plausible price series with drift, noise and a daily cycle."""
    prices, price = [], start
    for i in range(steps):
        cycle = math.sin(i / 18.0) * volatility * 0.4
        shock = rng.gauss(0, volatility * 0.22)
        price = max(price * (1 + cycle * 0.05 + shock), start * 0.55)
        prices.append(price)
    return prices


def main() -> int:
    rng = random.Random(20261013)  # fixed seed: the demo looks the same every run
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")
    db = client.get_database()

    for name in ("monitor_runs", "news", "recommendations"):
        db[name].delete_many({})

    steps = DAYS * 24 * 60 // INTERVAL_MINUTES
    now = datetime.now(timezone.utc)
    # End the series on "now" so the dashboard's freshness check stays green
    start = now - timedelta(minutes=INTERVAL_MINUTES * (steps - 1))
    series = {symbol: _walk(price, vol, steps, rng) for symbol, _, price, vol in COINS}

    runs = []
    for i in range(steps):
        created = start + timedelta(minutes=INTERVAL_MINUTES * i)
        portfolio, watchlist = [], []
        for symbol, cg_id, _, _ in COINS:
            price = series[symbol][i]
            prev_24h = series[symbol][max(0, i - 36)]
            prev_7d = series[symbol][0]
            entry = {
                "symbol": symbol,
                "coingecko_id": cg_id,
                "price": round(price, 6),
                "change_1h": round((price / series[symbol][max(0, i - 2)] - 1) * 100, 2),
                "change_24h": round((price / prev_24h - 1) * 100, 2),
                "change_7d": round((price / prev_7d - 1) * 100, 2),
                "headlines": [h for _, h in rng.sample(HEADLINES, rng.randint(1, 3))],
            }
            (portfolio if symbol in PORTFOLIO else watchlist).append(entry)

        runs.append({
            "created_at": created,
            "symbols": [c[0] for c in COINS],
            "portfolio": portfolio,
            "watchlist": watchlist,
            "analysis": {"mode": "scalp" if i % 3 else "conservative"},
            "notifications": [],
        })
    db.monitor_runs.insert_many(runs)

    news = []
    for i in range(140):
        source, title = HEADLINES[i % len(HEADLINES)]
        news.append({
            "entry_id": f"demo-{i}",
            "title": title,
            "source": source,
            "pub_date": now - timedelta(minutes=37 * i),
            "inserted_at": now - timedelta(minutes=37 * i),
        })
    db.news.insert_many(news)

    recommendations = []
    for i in range(24):
        symbol = rng.choice([c[0] for c in COINS])
        action = "buy" if i % 2 else "sell"
        recommendations.append({
            "action": action,
            "best_pick": symbol,
            "confidence": round(rng.uniform(0.32, 0.88), 2),
            "reasoning": rng.choice(REASONINGS).format(chg=rng.uniform(1.2, 6.4)),
            "created_at": now - timedelta(hours=6 * i),
        })
    db.recommendations.insert_many(recommendations)

    print(f"monitor_runs   : {db.monitor_runs.count_documents({})}")
    print(f"news           : {db.news.count_documents({})}")
    print(f"recommendations: {db.recommendations.count_documents({})}")
    print(f"\nDemo history written to {MONGO_URI}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
