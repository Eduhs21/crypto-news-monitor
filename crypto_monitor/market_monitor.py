#!/usr/bin/env python3
"""Automated collector: prices, headlines and an LLM buy/sell signal on every cycle."""
import os
import subprocess
import sys
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import yaml
from dotenv import load_dotenv

from crypto_monitor.binance_market import classify_modality
from crypto_monitor.price_fetcher import fetch_prices, fetch_top_movers
from crypto_monitor.telegram_notifier import send_telegram
from crypto_monitor.trade_state import load_state, save_state, STATE_PATH
from crypto_monitor import database
from crypto_monitor.paths import CONFIG_PATH, ENV_PATH

# Shared logging format
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)


DEFAULT_FEEDS = {
    "en": {
        "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "Cointelegraph": "https://cointelegraph.com/rss",
        "Decrypt": "https://decrypt.co/feed",
    },
    "pt": {
        "Portal do Bitcoin": "https://portaldobitcoin.uol.com.br/feed/",
        "InfoMoney": "https://www.infomoney.com.br/mercados/criptomoedas/feed/",
    },
}


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_feeds(config: dict) -> dict:
    return config.get("feeds", DEFAULT_FEEDS)


def get_coin_aliases(config: dict) -> dict:
    """Build headline-matching aliases from the configured portfolio and watchlist."""
    portfolio = config.get("portfolio", []) or []
    watchlist = config.get("watchlist", []) or []
    all_coins = portfolio + watchlist

    aliases = {}
    for coin in all_coins:
        sym = coin["symbol"]
        # Default aliases: lowercase symbol and lowercase CoinGecko id
        coin_aliases = [sym.lower(), coin["coingecko_id"].lower()]
        # Extra aliases declared in config.yaml, if any
        if "aliases" in coin and isinstance(coin["aliases"], list):
            coin_aliases.extend([a.lower() for a in coin["aliases"]])
        # Remove duplicados
        aliases[sym] = list(set(coin_aliases))
    return aliases


def fetch_headlines(lang: str, max_per_coin: int, feeds: dict, coin_aliases: dict) -> dict:
    """Return ``{symbol: [headlines]}`` by scanning the RSS feeds and matching aliases."""
    sources = {}
    if lang == "all":
        sources.update(feeds.get("en", {}))
        sources.update(feeds.get("pt", {}))
    else:
        sources.update(feeds.get(lang, {}))

    headlines_by_coin = {sym: [] for sym in coin_aliases}

    for source, url in sources.items():
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            logging.warning(f"Failed to fetch {source}: {e}")
            continue

        for entry in feed.entries[:15]:
            title = entry.title
            title_lower = title.lower()
            for symbol, aliases in coin_aliases.items():
                if any(a in title_lower for a in aliases):
                    if len(headlines_by_coin[symbol]) < max_per_coin:
                        headlines_by_coin[symbol].append(title)

    return headlines_by_coin


def build_coin_data(coins_cfg: list, prices: dict, headlines: dict) -> list:
    result = []
    for c in coins_cfg:
        price_info = prices.get(c["coingecko_id"], {})
        result.append({
            "symbol": c["symbol"],
            "price": price_info.get("price", 0.0),
            "change_1h": price_info.get("change_1h", 0.0),
            "change_24h": price_info.get("change_24h", 0.0),
            "change_7d": price_info.get("change_7d", 0.0),
            "headlines": headlines.get(c["symbol"], []),
            **c,  # inclui buy_price, quantity, alert_drop_pct etc quando existir
        })
    return result


def fetch_market_data(config: dict, state: dict | None = None) -> dict:
    """Fetch prices, headlines and the volatility radar.

    Returns ``{"portfolio_data", "watchlist_data", "buy_candidates"}``: the first two are
    persisted to MongoDB, the third (watchlist + radar, already stripped of the coins in
    ``state["blacklist"]``) feeds
    the trade cycle. Shared by the automated cycle and by the Telegram commands (/sell,
    /skip), which also need an up-to-date candidate pool.

    Raises if the price lookup fails; tolerates a failed volatility scan by falling back
    to the watchlist alone.
    """
    portfolio_cfg = config.get("portfolio", [])
    watchlist_cfg = config.get("watchlist", [])
    all_coins = portfolio_cfg + watchlist_cfg
    all_ids = [c["coingecko_id"] for c in all_coins]
    symbol_map = {c["coingecko_id"]: c["symbol"] for c in all_coins}

    if state is None:
        state = load_state()
    modality = state.get("modality", "spot")

    prices = fetch_prices(all_ids, symbol_map=symbol_map)

    movers_raw = []
    try:
        movers_raw = fetch_top_movers(exclude_ids=set(all_ids), top_n=8, modality=modality)
    except Exception as e:
        logging.warning(f"Volatility scan failed, continuing with the watchlist only: {e}")

    feeds = get_feeds(config)
    coin_aliases = get_coin_aliases(config)
    for m in movers_raw:
        coin_aliases[m["symbol"]] = list({m["symbol"].lower(), m["coingecko_id"].lower()})
    headlines = fetch_headlines(config["news"]["lang"], config["news"]["max_headlines"], feeds, coin_aliases)

    portfolio_data = build_coin_data(portfolio_cfg, prices, headlines)
    watchlist_data = build_coin_data(watchlist_cfg, prices, headlines)
    for c in watchlist_data:
        c["source"] = "watchlist"

    movers_data = [
        {**m, "headlines": headlines.get(m["symbol"], []), "source": "radar"}
        for m in movers_raw
    ]

    buy_candidates = watchlist_data + movers_data
    blacklist = set(state.get("blacklist", []))
    if blacklist:
        buy_candidates = [c for c in buy_candidates if c["symbol"] not in blacklist]

    return {
        "portfolio_data": portfolio_data,
        "watchlist_data": watchlist_data,
        "buy_candidates": buy_candidates,
    }


def _format_price(value: float) -> str:
    abs_value = abs(value)
    if abs_value == 0:
        return "$0.00"
    if abs_value < 0.01:
        return f"${value:,.8f}"
    if abs_value < 1:
        return f"${value:,.4f}"
    return f"${value:,.2f}"


def _score_headlines_vader(coin_data: list) -> list:
    """Annotate each coin with headlines and VADER scores, same shape the CLI uses."""
    from crypto_monitor.cli import analyzer

    for coin in coin_data:
        vader_headlines = []
        for h in coin.get("headlines", []):
            score = analyzer.polarity_scores(h)["compound"]
            sent = "🚀" if score >= 0.3 else "🩸" if score <= -0.3 else "⚪"
            vader_headlines.append(f'"{h}" (Sentimento VADER: {score:+.2f} {sent})')
        coin["vader_headlines"] = vader_headlines
    return coin_data


def get_trade_signal(config: dict, action: str, coin_data: list, api_key: str,
                      horizon_minutes: int | None = None) -> dict | None:
    """Run the same analysis as ``--buy``/``--sell``, unattended."""
    from crypto_monitor.cli import build_recommendation_prompt
    from crypto_monitor.gemini_client import call_gemini

    if not coin_data:
        return None

    coin_data = _score_headlines_vader(coin_data)
    if horizon_minutes:
        history_limit = config.get("history", {}).get("scalp_prompt_runs", 4)
    else:
        history_limit = config.get("history", {}).get("prompt_runs", 10)
    symbols = [c["symbol"] for c in coin_data]
    history = database.get_market_history(symbols, history_limit)

    prompt = build_recommendation_prompt(coin_data, action, history, horizon_minutes=horizon_minutes)
    model = config.get("gemini", {}).get("model", "gemini-3.5-flash")
    recommendation = call_gemini(prompt, api_key, model=model)
    database.save_recommendation(recommendation, action, coin_data, history)
    return recommendation


def build_position_prompt(position: dict, coin_data: dict, history: list, interval: int,
                          stop_loss_pct: float, max_cycles_held: int) -> str:
    """Build the prompt that decides whether to sell the open position or hold."""
    from crypto_monitor.cli import format_market_history

    symbol = position["symbol"]
    entry_price = position["entry_price"]
    current_price = coin_data.get("price", 0.0)
    change_since_entry = ((current_price - entry_price) / entry_price * 100) if entry_price else 0.0
    cycles_held = position.get("cycles_held", 1)
    take_profit_pct = position.get("take_profit_pct")
    news_block = "\n".join(f"  - {h}" for h in coin_data.get("vader_headlines", [])) or "  (no recent headlines)"
    history_block = format_market_history(history or [], {symbol})

    take_profit_line = ""
    if take_profit_pct is not None:
        take_profit_price = entry_price * (1 + take_profit_pct / 100) if entry_price else 0.0
        take_profit_line = (
            f"Take-profit set at entry: {_format_price(take_profit_price)} (+{take_profit_pct:.2f}%) "
            f"- once reached the sell is forced automatically, regardless of your answer\n"
        )

    return f"""You are a crypto scalping specialist.

A BUY signal was issued for {symbol} at {_format_price(entry_price)}. Decide now whether it is time to
SELL (take profit or cut the loss) or HOLD for one more cycle (~{interval} min) before deciding again.

Coin: {symbol}
Entry price (when the buy was signalled): {_format_price(entry_price)}
Current price: {_format_price(current_price)}
Change since entry: {change_since_entry:+.2f}%
Change 1h: {coin_data.get('change_1h', 0.0):.2f}%
Change 24h: {coin_data.get('change_24h', 0.0):.2f}%
Cycles with the position open: {cycles_held} (system limit: {max_cycles_held} - once reached the sell is forced automatically)
System stop loss: {stop_loss_pct:.2f}% (if the change falls to that limit the sell is forced automatically, regardless of your answer)
{take_profit_line}Recent headlines with VADER sentiment:
{news_block}

=== RECENT MONGODB HISTORY (newest first) ===
{history_block}

Answer STRICTLY in JSON, with no markdown and no ```json fences, exactly in this shape:

{{
  "decision": "sell" or "hold",
  "reasoning": "1-2 objective sentences linking the change since entry to the news sentiment.",
  "confidence": 0.0-1.0
}}

Rules:
- If the coin rose consistently since entry and there are signs of exhaustion (negative news or a trend reversal), prefer "sell" to take the profit.
- If the coin fell since entry but there is no concrete reason to panic (no relevant negative news) and the change is still far from the stop loss, prefer "hold" - do not sell at a loss without a reason.
- If the coin fell since entry AND relevant negative news reinforces the drop, or the change is approaching the stop loss, "sell" to cut the loss is acceptable and recommended.
- If the number of cycles with the position open is close to the system limit, prefer "sell" over holding for one more cycle without need.
- Base your answer exclusively on the data above. Be concise.
"""


def evaluate_open_position(config: dict, position: dict, api_key: str) -> dict | None:
    """Ask the model whether to sell the open position or hold it.

    Fetches the price straight from the coingecko_id stored in the position, so it does
    dela continuar na watchlist fixa nem entre os destaques de volatilidade do ciclo atual,
    not depend on the coin still being in the watchlist: it may have come from the
    volatility radar of an earlier cycle. Returns None only for a legacy position saved
    without a coingecko_id.
    """
    from crypto_monitor.gemini_client import call_gemini

    symbol = position["symbol"]
    coingecko_id = position.get("coingecko_id")
    if not coingecko_id:
        return None

    prices = fetch_prices([coingecko_id], symbol_map={coingecko_id: symbol})
    price_info = prices.get(coingecko_id, {})
    if not price_info.get("price"):
        raise RuntimeError(f"Could not fetch the current price of {symbol} ({coingecko_id}) from CoinGecko.")

    feeds = get_feeds(config)
    aliases = {symbol: list({symbol.lower(), coingecko_id.lower()})}
    headlines = fetch_headlines(
        config.get("news", {}).get("lang", "all"),
        config.get("news", {}).get("max_headlines", 6),
        feeds, aliases,
    )
    coin_data = {
        "symbol": symbol,
        "coingecko_id": coingecko_id,
        "price": price_info.get("price", 0.0),
        "change_1h": price_info.get("change_1h", 0.0),
        "change_24h": price_info.get("change_24h", 0.0),
        "change_7d": price_info.get("change_7d", 0.0),
        "headlines": headlines.get(symbol, []),
    }

    annotated = _score_headlines_vader([coin_data])[0]
    history_limit = config.get("history", {}).get("prompt_runs", 10)
    history = database.get_market_history([symbol], history_limit)
    interval = int(os.environ.get("MONITOR_INTERVAL_MINUTES", 40))
    trading_cfg = config.get("trading", {})
    stop_loss_pct = trading_cfg.get("stop_loss_pct", -8.0)
    max_cycles_held = trading_cfg.get("max_cycles_held", 12)
    prompt = build_position_prompt(position, annotated, history, interval, stop_loss_pct, max_cycles_held)
    model = config.get("gemini", {}).get("model", "gemini-3.5-flash")
    decision = call_gemini(prompt, api_key, model=model)
    return {"decision": decision, "coin_data": annotated}


def _next_operation_number(state: dict) -> int:
    """Number each open position (buy signal through sell) as "Trade #N".

    The number keeps a single Telegram conversation readable: it says which sell belongs
    to which buy."""
    number = state.get("last_operation_number", 0) + 1
    state["last_operation_number"] = number
    return number


def _open_position(config: dict, buy_candidates: list, api_key: str, interval: int, state: dict) -> str:
    """Evaluate the buy candidates and, if one is good enough, open a new
    position (mutating ``state``; the caller is responsible for calling save_state). Always returns
    a message ready to send to the user, even when there is no opportunity."""
    try:
        recommendation = get_trade_signal(config, "buy", buy_candidates, api_key, horizon_minutes=interval)
    except Exception as e:
        logging.error(f"Automated buy analysis failed: {e}")
        return (
            "🟢 Could not evaluate buy opportunities right now - the model timed out. "
            f"Retrying on the next cycle (~{interval} min)."
        )

    if not recommendation:
        return "🔍 No coins configured to evaluate for a buy."

    best_pick = recommendation.get("best_pick")
    try:
        confidence = float(recommendation.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    reasoning = recommendation.get("reasoning", "")

    min_confidence = config.get("notification", {}).get("min_opportunity_confidence", 0.65)
    valid_symbols = {c["symbol"] for c in buy_candidates}
    if not best_pick or best_pick not in valid_symbols or confidence < min_confidence:
        return (
            f"🔍 No buy opportunity with enough confidence right now "
            f"(top pick: {best_pick or 'N/A'}, confidence {confidence:.0%}, "
            f"minimum required {min_confidence:.0%}).\n"
            f"{reasoning}\n\n"
            f"⏱ Re-evaluating on the next cycle (~{interval} min)."
        )

    coin_data = next((c for c in buy_candidates if c["symbol"] == best_pick), None)
    entry_price = coin_data.get("price", 0.0) if coin_data else 0.0
    coingecko_id = coin_data.get("coingecko_id") if coin_data else None
    operation_number = _next_operation_number(state)

    try:
        take_profit_pct = float(recommendation.get("take_profit_pct"))
        if take_profit_pct <= 0:
            take_profit_pct = None
    except (TypeError, ValueError):
        take_profit_pct = None

    position = {
        "symbol": best_pick,
        "coingecko_id": coingecko_id,
        "entry_price": entry_price,
        "entry_time": datetime.now(timezone.utc).isoformat(),
        "cycles_held": 1,
        "operation_number": operation_number,
    }
    if take_profit_pct is not None:
        position["take_profit_pct"] = take_profit_pct
    state["position"] = position

    origin = coin_data.get("source", "watchlist") if coin_data else "watchlist"
    source_label = "volatility radar" if origin == "radar" else "your watchlist"
    stop_loss_pct = config.get("trading", {}).get("stop_loss_pct", -8.0)
    stop_price = entry_price * (1 + stop_loss_pct / 100) if entry_price else 0.0

    modality = classify_modality(best_pick)
    if modality == "spot":
        market_label = "Spot (regular direct buy on Binance)"
    elif modality == "alpha":
        market_label = "Alpha (not in the regular search - look under the Alpha tab in the Binance app)"
    else:
        market_label = "not found on Binance (neither Spot nor Alpha) - verify before buying"

    targets_line = f"🛑 Stop loss: {_format_price(stop_price)} ({stop_loss_pct:.2f}%)"
    if take_profit_pct is not None and entry_price:
        take_profit_price = entry_price * (1 + take_profit_pct / 100)
        targets_line = f"🎯 Take profit: {_format_price(take_profit_price)} (+{take_profit_pct:.2f}%)\n{targets_line}"

    return (
        f"🟢 <b>Trade #{operation_number} - Buy {best_pick}</b> "
        f"(confidence {confidence:.0%}, price {_format_price(entry_price)}, "
        f"1h change {coin_data.get('change_1h', 0.0):+.2f}%)\n"
        f"Source: {source_label}\n"
        f"📌 Market: {market_label}\n"
        f"{reasoning}\n\n"
        f"{targets_line}\n\n"
        f"⏱ Re-evaluating this position on the next cycle (~{interval} min)."
    )


def _in_active_window(config: dict) -> bool:
    """Tell whether now is inside the active window (``schedule`` in config.yaml).

    With no ``schedule`` section, or with ``enabled: false``, the monitor is always
    considered active."""
    schedule_cfg = config.get("schedule") or {}
    if not schedule_cfg.get("enabled", False):
        return True
    start_hour = schedule_cfg.get("start_hour", 6)
    end_hour = schedule_cfg.get("end_hour", 20)
    utc_offset = schedule_cfg.get("utc_offset_hours", -3)
    now_local = datetime.now(timezone.utc) + timedelta(hours=utc_offset)
    return start_hour <= now_local.hour < end_hour


def _check_position_mechanically(config: dict, position: dict, state: dict) -> str | None:
    """Outside the active window: check only stop-loss / take-profit on the open position,
    without calling the model. Returns a message only when a limit is hit (forced sell);
    otherwise stay silent so the bot does not wake anyone up at night."""
    symbol = position["symbol"]
    coingecko_id = position.get("coingecko_id")
    if not coingecko_id:
        return None

    try:
        prices = fetch_prices([coingecko_id], symbol_map={coingecko_id: symbol})
    except Exception as e:
        logging.warning(f"Failed to check the price of {symbol} outside the active window: {e}")
        return None

    current_price = prices.get(coingecko_id, {}).get("price")
    entry_price = position["entry_price"]
    if not current_price or not entry_price:
        return None

    change_pct = (current_price - entry_price) / entry_price * 100
    trading_cfg = config.get("trading", {})
    stop_loss_pct = trading_cfg.get("stop_loss_pct", -8.0)
    take_profit_pct = position.get("take_profit_pct")

    hit_target = take_profit_pct is not None and change_pct >= take_profit_pct
    hit_stop = change_pct <= stop_loss_pct
    operation_number = position.get("operation_number", "?")

    if hit_target or hit_stop:
        state["position"] = None
        emoji = "✅" if change_pct >= 0 else "🔻"
        reason = f"take profit of +{take_profit_pct:.2f}%" if hit_target else f"stop loss of {stop_loss_pct:.2f}%"
        start_hour = config.get("schedule", {}).get("start_hour", 6)
        return (
            f"{emoji} <b>Trade #{operation_number} closed - Sell {symbol}</b>\n"
            f"Change since buy: {change_pct:+.2f}%\n"
            f"Automatic sell on {reason} (mechanical check, no LLM - outside the active window).\n\n"
            f"🌙 Outside active hours - no new buy will be suggested until {start_hour}:00."
        )

    position["cycles_held"] = position.get("cycles_held", 1) + 1
    state["position"] = position
    return None


def run_trade_signals(config: dict, buy_candidates: list) -> str | None:
    """Fast-trade cycle: with no open position, suggest the best buy among the watchlist and
    the most volatile coins of the moment (``buy_candidates``); with a position open it re-evaluates
    the SAME coin (fetching a fresh price through the stored coingecko_id) and decides sell or hold.
    On a sell it opens the next position in the same cycle, without waiting for the next interval.
    Outside the active window (``schedule`` in config.yaml) it suggests no new buy, and an open
    open position is only checked mechanically against stop-loss / take-profit, with no LLM call."""
    if not config.get("gemini", {}).get("enabled", True):
        logging.info("gemini.enabled=false; skipping automated buy/sell signals (saves tokens).")
        return None

    api_key = config.get("gemini", {}).get("api_key") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logging.warning("GEMINI_API_KEY is not set; skipping automated buy/sell signals.")
        return None

    interval = int(os.environ.get("MONITOR_INTERVAL_MINUTES", 40))
    state = load_state()
    position = state.get("position")
    active_hours = _in_active_window(config)

    if not position:
        if not active_hours:
            logging.info("Outside the active window; skipping new buy suggestion.")
            return None
        message = _open_position(config, buy_candidates, api_key, interval, state)
        save_state(state)
        return message

    symbol = position["symbol"]
    operation_number = position.get("operation_number", "?")

    if not active_hours:
        message = _check_position_mechanically(config, position, state)
        save_state(state)
        return message

    try:
        result = evaluate_open_position(config, position, api_key)
    except Exception as e:
        logging.error(f"Failed to evaluate the position in {symbol}: {e}")
        return (
            f"⏳ <b>Trade #{operation_number} - {symbol} still open</b> "
            f"(entrada {_format_price(position['entry_price'])})\n"
            f"Could not evaluate now - the model or CoinGecko timed out. "
            f"Retrying on the next cycle (~{interval} min)."
        )

    if result is None:
        state["position"] = None
        save_state(state)
        return (
            f"⚠️ <b>Trade #{operation_number} - {symbol}</b>: legacy position with no stored id; "
            f"tracking was dropped without evaluating."
        )

    decision_data = result["decision"]
    coin_data = result["coin_data"]
    decision = str(decision_data.get("decision", "hold")).strip().lower()
    reasoning = decision_data.get("reasoning", "")
    try:
        confidence = float(decision_data.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0

    current_price = coin_data.get("price", 0.0)
    entry_price = position["entry_price"]
    change_pct = ((current_price - entry_price) / entry_price * 100) if entry_price else 0.0

    trading_cfg = config.get("trading", {})
    stop_loss_pct = trading_cfg.get("stop_loss_pct", -8.0)
    max_cycles_held = trading_cfg.get("max_cycles_held", 12)
    take_profit_pct = position.get("take_profit_pct")
    cycles_held = position.get("cycles_held", 1)

    if decision != "sell" and take_profit_pct is not None and change_pct >= take_profit_pct:
        decision = "sell"
        reasoning = (
            f"Forced sell on take profit: a {change_pct:+.2f}% change reached the "
            f"+{take_profit_pct:.2f}% target set at entry. Original model rationale (hold): {reasoning}"
        )
    elif decision != "sell" and change_pct <= stop_loss_pct:
        decision = "sell"
        reasoning = (
            f"Forced sell on stop loss: a {change_pct:+.2f}% change hit the "
            f"{stop_loss_pct:.2f}% limit. Original model rationale (hold): {reasoning}"
        )
    elif decision != "sell" and cycles_held >= max_cycles_held:
        decision = "sell"
        reasoning = (
            f"Forced sell on maximum holding time ({max_cycles_held} cycles). "
            f"Original model rationale (hold): {reasoning}"
        )

    if decision == "sell":
        state["position"] = None
        emoji = "✅" if change_pct >= 0 else "🔻"
        sell_message = (
            f"{emoji} <b>Trade #{operation_number} closed - Sell {symbol}</b> (confidence {confidence:.0%})\n"
            f"Change since buy: {change_pct:+.2f}%\n"
            f"{reasoning}"
        )
        buy_message = _open_position(config, buy_candidates, api_key, interval, state)
        save_state(state)
        return f"{sell_message}\n\n{buy_message}"
    else:
        position["cycles_held"] = cycles_held + 1
        state["position"] = position
        save_state(state)
        return (
            f"⏳ <b>Trade #{operation_number} - Hold, not time to sell {symbol} yet</b> "
            f"(confidence {confidence:.0%})\n"
            f"Change since buy: {change_pct:+.2f}%\n"
            f"{reasoning}\n\n"
            f"⏱ Re-evaluating on the next cycle (~{interval} min)."
        )


def should_notify(state: dict, key: str, cooldown_hours: int) -> bool:
    last = state.get(key)
    if not last:
        return True
    last_dt = datetime.fromisoformat(last)
    return datetime.now() - last_dt > timedelta(hours=cooldown_hours)


def notify(title: str, message: str, urgency: str = "normal"):
    """Send a desktop notification through ``notify-send``."""
    try:
        subprocess.run(["notify-send", "-u", urgency, title, message], check=True)
        return True
    except FileNotFoundError:
        logging.warning(f"[notify-send unavailable] {title}: {message}")
        return False
    except Exception as e:
        logging.error(f"[notify-send erro] {e}")
        return False


def notify_all(config: dict, title: str, message: str,
               urgency: str = "normal", telegram_html: str | None = None) -> dict:
    """Dispatch a notification to every configured channel."""
    channels = config.get("notification", {}).get("channels", ["desktop"])
    results: dict[str, bool] = {}

    # --- Desktop (notify-send) ---
    if "desktop" in channels:
        try:
            results["desktop"] = notify(title, message, urgency)
        except Exception as e:
            logging.error(f"[desktop erro] {e}")
            results["desktop"] = False

    # --- Telegram ---
    if "telegram" in channels:
        tg_cfg = config.get("telegram", {})
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or tg_cfg.get("bot_token", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID") or tg_cfg.get("chat_id", "")

        if not bot_token or not chat_id:
            logging.warning(
                "Telegram configurado como canal mas bot_token/chat_id "
                "are not set - skipping delivery."
            )
            results["telegram"] = False
        else:
            text = telegram_html if telegram_html else f"<b>{title}</b>\n{message}"
            try:
                results["telegram"] = send_telegram(bot_token, chat_id, text)
            except Exception as e:
                logging.error(f"[telegram erro] {e}")
                results["telegram"] = False

    return results


def main():
    load_dotenv(ENV_PATH)

    config = load_config()

    logging.info("Fetching prices, headlines and volatility radar...")
    try:
        market_data = fetch_market_data(config)
    except Exception as e:
        logging.error(f"Failed to fetch market data, aborting this cycle: {e}")
        return

    portfolio_data = market_data["portfolio_data"]
    watchlist_data = market_data["watchlist_data"]
    buy_candidates = market_data["buy_candidates"]

    if database.save_monitor_run(
        portfolio_data,
        watchlist_data,
        {"mode": "collection_only", "opportunities": [], "risks": []},
        [],
    ):
        logging.info("Snapshot saved to MongoDB (no LLM call).")
    else:
        logging.warning("MongoDB unavailable; this run was not persisted.")

    try:
        signals_html = run_trade_signals(config, buy_candidates)
        if signals_html:
            results = notify_all(config, "Trade signal", signals_html, telegram_html=signals_html)
            logging.info(f"Trade signal sent: {results}")
        else:
            logging.info("No trade signal to send this cycle.")
    except Exception as e:
        logging.error(f"Failed to send the trade signal: {e}")

    logging.info("Done.")


if __name__ == "__main__":
    main()
