import os
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure
from datetime import datetime, timezone
from rich.console import Console

console = Console()

# Singleton for the DB client so we don't reconnect on every query
_client = None

def get_db():
    global _client
    if _client is not None:
        try:
            return _client.get_database()
        except:
            pass

    mongo_uri = os.environ.get("MONGO_URI", "mongodb://localhost:27017/cryptopanic")
    try:
        _client = MongoClient(mongo_uri, serverSelectionTimeoutMS=2000)
        _client.admin.command('ping') # Test connection
        return _client.get_database()
    except Exception as e:
        _client = None
        return None

def is_news_seen(entry_id):
    db = get_db()
    if db is None:
        return None  # No database connection
    
    collection = db['news']
    return collection.count_documents({"entry_id": entry_id}) > 0

def save_news_item(entry_id, title, source, pub_date):
    db = get_db()
    if db is None:
        return False
    
    collection = db['news']
    collection.update_one(
        {"entry_id": entry_id},
        {"$set": {
            "entry_id": entry_id,
            "title": title,
            "source": source,
            "pub_date": pub_date,
            "inserted_at": datetime.now(timezone.utc)
        }},
        upsert=True
    )
    return True

def save_recommendation(recommendation, action, current_data=None, history_used=None):
    db = get_db()
    if db is None:
        return False
    
    try:
        collection = db['recommendations']
        doc = {
            "action": action,
            "best_pick": recommendation.get("best_pick"),
            "confidence": recommendation.get("confidence", 0.0),
            "reasoning": recommendation.get("reasoning", ""),
            "alternatives": recommendation.get("alternatives", []),
            "current_data": current_data or [],
            "history_used": history_used or [],
            "created_at": datetime.now(timezone.utc)
        }
        collection.insert_one(doc)
        return True
    except Exception:
        return False

def save_monitor_run(portfolio_data, watchlist_data, analysis, notifications=None):
    """Persist a full snapshot of one monitor run."""
    db = get_db()
    if db is None:
        return False

    try:
        collection = db['monitor_runs']
        collection.create_index([("created_at", -1)])
        collection.create_index([("symbols", 1), ("created_at", -1)])

        symbols = list(dict.fromkeys(
            coin.get("symbol")
            for coin in portfolio_data + watchlist_data
            if coin.get("symbol")
        ))
        collection.insert_one({
            "created_at": datetime.now(timezone.utc),
            "symbols": symbols,
            "portfolio": portfolio_data,
            "watchlist": watchlist_data,
            "analysis": analysis,
            "notifications": notifications or [],
        })
        return True
    except Exception:
        return False

def get_market_history(symbols, limit_runs=10):
    """Return the most recent runs that contain any of the requested symbols."""
    db = get_db()
    if db is None or not symbols or limit_runs <= 0:
        return []

    try:
        cursor = (
            db['monitor_runs']
            .find(
                {"symbols": {"$in": list(symbols)}},
                {"_id": 0, "created_at": 1, "portfolio": 1, "watchlist": 1,
                 "analysis": 1, "notifications": 1},
            )
            .sort("created_at", -1)
            .limit(limit_runs)
        )
        return list(cursor)
    except Exception:
        return []
