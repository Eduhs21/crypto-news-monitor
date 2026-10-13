"""Streamlit dashboard over the MongoDB history collected by the monitor.

Read-only: it renders snapshots already stored by ``crypto_monitor.market_monitor``
and never calls the LLM, so opening the dashboard costs nothing in API tokens.

Run with:
    streamlit run dashboard.py
"""
import os
from datetime import datetime, timezone

import pandas as pd
import plotly.express as px
import streamlit as st
from pymongo import MongoClient

st.set_page_config(page_title="Crypto Monitor Dashboard", page_icon="📈", layout="wide")

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://mongodb:27017/cryptopanic")
#: Timezone used to render every timestamp. Override with DASHBOARD_TIMEZONE.
DISPLAY_TIMEZONE = os.environ.get("DASHBOARD_TIMEZONE", "UTC")


@st.cache_resource
def get_database():
    """Open (and cache) the MongoDB connection."""
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
    client.admin.command("ping")
    return client.get_database()


@st.cache_data(ttl=60)
def load_data():
    """Load monitor runs, headlines and recommendations from MongoDB."""
    db = get_database()
    runs = list(
        db.monitor_runs.find(
            {},
            {"_id": 0, "created_at": 1, "portfolio": 1, "watchlist": 1,
             "analysis.mode": 1},
        ).sort("created_at", 1)
    )
    news = list(db.news.find({}, {"_id": 0}).sort("pub_date", -1).limit(200))
    recommendations = list(
        db.recommendations.find(
            {},
            {"_id": 0, "created_at": 1, "action": 1, "best_pick": 1,
             "confidence": 1, "reasoning": 1},
        ).sort("created_at", -1).limit(100)
    )
    return runs, news, recommendations


def to_local(series):
    """Convert a UTC datetime series to the configured display timezone."""
    return pd.to_datetime(series, utc=True).dt.tz_convert(DISPLAY_TIMEZONE)


def build_prices(runs):
    """Flatten the monitor runs into one row per coin per snapshot."""
    rows = []
    for run in runs:
        for group in ("portfolio", "watchlist"):
            for coin in run.get(group, []):
                rows.append({
                    "date": run.get("created_at"),
                    "group": group,
                    "coin": coin.get("symbol"),
                    "price": coin.get("price"),
                    "change 24h": coin.get("change_24h"),
                    "change 7d": coin.get("change_7d"),
                    "headlines": len(coin.get("headlines", [])),
                })
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame["date"] = to_local(frame["date"])
    return frame


st.title("📈 Crypto Monitor Dashboard")
st.caption("History collected automatically. This page makes no LLM API calls.")

try:
    runs, news, recommendations = load_data()
except Exception as exc:
    st.error(f"Could not connect to MongoDB: {exc}")
    st.stop()

prices = build_prices(runs)
latest_at = prices["date"].max() if not prices.empty else None
now = datetime.now(timezone.utc)
latest_utc = latest_at.tz_convert("UTC").to_pydatetime() if latest_at is not None else None
minutes_since = (now - latest_utc).total_seconds() / 60 if latest_utc else None

c1, c2, c3, c4 = st.columns(4)
c1.metric("Snapshots", len(runs))
c2.metric("Headlines", len(news))
c3.metric("Recommendations", len(recommendations))
c4.metric(
    "Last collection",
    latest_at.strftime("%d/%m %H:%M") if latest_at is not None else "No data",
    f"{minutes_since:.0f} min ago" if minutes_since is not None else None,
    delta_color="normal" if minutes_since is not None and minutes_since <= 20 else "inverse",
)

if minutes_since is not None and minutes_since > 20:
    st.warning("The last collection ran more than 20 minutes ago. Check the crypto-monitor container.")

tab_market, tab_news, tab_ai, tab_raw = st.tabs(
    ["Market", "Headlines", "Recommendations", "Raw snapshots"]
)

with tab_market:
    if prices.empty:
        st.info("No market snapshot yet.")
    else:
        all_coins = sorted(prices["coin"].dropna().unique())
        f1, f2 = st.columns([1, 2])
        selected = f1.multiselect("Coins", all_coins, default=all_coins)
        period = f2.selectbox("Period", ["24 hours", "7 days", "30 days", "All history"])
        days = {"24 hours": 1, "7 days": 7, "30 days": 30}.get(period)
        filtered = prices[prices["coin"].isin(selected)]
        if days:
            cutoff = pd.Timestamp.now(tz=DISPLAY_TIMEZONE) - pd.Timedelta(days=days)
            filtered = filtered[filtered["date"] >= cutoff]

        fig = px.line(
            filtered, x="date", y="price", color="coin", markers=True,
            labels={"date": "Date", "price": "Price (USD)", "coin": "Coin"},
        )
        st.plotly_chart(fig, use_container_width=True)

        latest_rows = (
            filtered.sort_values("date").groupby("coin", as_index=False).tail(1)
            .sort_values("coin")
        )
        st.dataframe(
            latest_rows[["coin", "price", "change 24h", "change 7d", "headlines", "date"]],
            use_container_width=True, hide_index=True,
            column_config={
                "price": st.column_config.NumberColumn(format="$ %.4f"),
                "change 24h": st.column_config.NumberColumn(format="%.2f%%"),
                "change 7d": st.column_config.NumberColumn(format="%.2f%%"),
                "date": st.column_config.DatetimeColumn(format="DD/MM/YYYY HH:mm"),
            },
        )

with tab_news:
    news_df = pd.DataFrame(news)
    if news_df.empty:
        st.info("No headline stored in the news collection yet.")
    else:
        if "pub_date" in news_df:
            news_df["pub_date"] = to_local(news_df["pub_date"])
        if "source" in news_df:
            sources = sorted(news_df["source"].dropna().unique())
            selected_sources = st.multiselect("Sources", sources, default=sources)
            if selected_sources:
                news_df = news_df[news_df["source"].isin(selected_sources)]
        columns = [c for c in ["pub_date", "source", "title"] if c in news_df]
        st.dataframe(news_df[columns], use_container_width=True, hide_index=True)

with tab_ai:
    rec_df = pd.DataFrame(recommendations)
    if rec_df.empty:
        st.info("No manual recommendation yet.")
    else:
        rec_df["created_at"] = to_local(rec_df["created_at"])
        st.dataframe(
            rec_df, use_container_width=True, hide_index=True,
            column_config={
                "confidence": st.column_config.ProgressColumn(min_value=0, max_value=1),
                "created_at": st.column_config.DatetimeColumn(format="DD/MM/YYYY HH:mm"),
            },
        )

with tab_raw:
    if prices.empty:
        st.info("No market snapshot yet.")
    else:
        st.dataframe(
            prices.sort_values("date", ascending=False),
            use_container_width=True, hide_index=True,
        )

if st.button("Refresh now"):
    st.cache_data.clear()
    st.rerun()
