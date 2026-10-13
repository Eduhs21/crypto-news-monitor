#!/usr/bin/env python3
"""Telegram command listener (long polling) giving manual control over the trade cycle.

Runs as a process separate from ``market_monitor.py`` (its own service in
docker-compose), sharing the same ``state.json`` (through ``trade_state``, with a
file lock) and the same ``config.yaml``. Only messages coming from the configured
``TELEGRAM_CHAT_ID`` are answered; every other chat is silently ignored.
"""
import logging
import os
import sys
import time

import requests
from dotenv import load_dotenv

from crypto_monitor import market_monitor as mm
from crypto_monitor import trade_state
from crypto_monitor.price_fetcher import fetch_prices
from crypto_monitor.telegram_notifier import send_telegram
from crypto_monitor.paths import ENV_PATH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

TELEGRAM_GET_UPDATES = "https://api.telegram.org/bot{token}/getUpdates"


def get_updates(bot_token: str, offset: int | None, timeout: int = 30) -> list:
    """Long-poll the Telegram API for new updates."""
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    resp = requests.get(TELEGRAM_GET_UPDATES.format(token=bot_token), params=params, timeout=timeout + 10)
    resp.raise_for_status()
    return resp.json().get("result", [])


def _current_interval() -> int:
    return int(os.environ.get("MONITOR_INTERVAL_MINUTES", 40))


def cmd_help(config: dict, arg: str) -> str:
    """List the available commands."""
    return (
        "<b>Available commands:</b>\n"
        "/status - coin currently held, price and change\n"
        "/prices - current prices of the portfolio and watchlist\n"
        "/sell - close the open position now, without waiting for the model\n"
        "/skip - drop the coin of this trade and suggest another one\n"
        "/block SYMBOL - never suggest this coin for a buy again\n"
        "/unblock SYMBOL - remove the coin from the block list\n"
        "/blocked - list the currently blocked coins\n"
        "/market [spot|alpha] - show or change the tradable universe the radar accepts\n"
        "/balance - current Binance account balance (testnet or live, per config.yaml)"
    )


def cmd_balance(config: dict, arg: str) -> str:
    """Report the Binance account balance."""
    from crypto_monitor.binance_client import fetch_account_balances

    try:
        balances = fetch_account_balances(config)
    except Exception as e:
        return f"⚠️ Could not read the Binance balance: {e}"

    testnet = config.get("binance", {}).get("testnet", True)
    label = "TESTNET (paper balance)" if testnet else "LIVE ACCOUNT"
    if not balances:
        return f"💰 <b>Binance balance ({label})</b>\nNo asset holds a balance in this account."

    lines = [f"💰 <b>Binance balance ({label})</b>"]
    for b in sorted(balances, key=lambda x: x["asset"]):
        total = b["free"] + b["locked"]
        lines.append(f"• {b['asset']}: {total:.8f} (free {b['free']:.8f} / locked {b['locked']:.8f})")
    return "\n".join(lines)


def cmd_status(config: dict, arg: str) -> str:
    """Report the open position, its live price and its stop/target levels."""
    state = trade_state.load_state()
    position = state.get("position")
    if not position:
        return "No open trade right now. A buy will be suggested on the next cycle."

    symbol = position["symbol"]
    coingecko_id = position.get("coingecko_id")
    entry_price = position["entry_price"]
    operation_number = position.get("operation_number", "?")
    cycles_held = position.get("cycles_held", 1)

    current_price = entry_price
    if coingecko_id:
        try:
            prices = fetch_prices([coingecko_id], symbol_map={coingecko_id: symbol})
            current_price = prices.get(coingecko_id, {}).get("price", entry_price) or entry_price
        except Exception as e:
            logging.warning(f"Failed to fetch the current price of {symbol} for /status: {e}")

    change_pct = ((current_price - entry_price) / entry_price * 100) if entry_price else 0.0
    stop_loss_pct = config.get("trading", {}).get("stop_loss_pct", -8.0)
    stop_price = entry_price * (1 + stop_loss_pct / 100) if entry_price else 0.0
    lines = [
        f"📍 <b>Trade #{operation_number} - {symbol}</b>",
        f"Entry price: {mm._format_price(entry_price)}",
        f"Current price: {mm._format_price(current_price)}",
        f"Change: {change_pct:+.2f}%",
        f"Cycles open: {cycles_held}",
    ]

    take_profit_pct = position.get("take_profit_pct")
    if take_profit_pct is not None and entry_price:
        take_profit_price = entry_price * (1 + take_profit_pct / 100)
        lines.append(f"\n🎯 Take profit: {mm._format_price(take_profit_price)} (+{take_profit_pct:.2f}%)")
    lines.append(f"🛑 Stop loss: {mm._format_price(stop_price)} ({stop_loss_pct:.2f}%)")

    return "\n".join(lines)


def cmd_prices(config: dict, arg: str) -> str:
    """Report current prices for every configured coin."""
    portfolio_cfg = config.get("portfolio", [])
    watchlist_cfg = config.get("watchlist", [])
    all_coins = portfolio_cfg + watchlist_cfg
    all_ids = [c["coingecko_id"] for c in all_coins]
    if not all_ids:
        return "No coins configured in the portfolio or watchlist."

    symbol_map = {c["coingecko_id"]: c["symbol"] for c in all_coins}
    prices = fetch_prices(all_ids, symbol_map=symbol_map)
    sections = []
    for label, coins in (("Portfolio", portfolio_cfg), ("Watchlist", watchlist_cfg)):
        if not coins:
            continue
        lines = [f"<b>{label}</b>"]
        for c in coins:
            info = prices.get(c["coingecko_id"], {})
            price = mm._format_price(info.get("price", 0.0))
            chg24 = info.get("change_24h", 0.0)
            chg7 = info.get("change_7d", 0.0)
            lines.append(f"• {c['symbol']}: {price} (24h {chg24:+.2f}% | 7d {chg7:+.2f}%)")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def _fresh_price(coingecko_id: str | None, fallback: float, symbol: str | None = None) -> float:
    """Return a live price, falling back to *fallback* when the lookup fails."""
    if not coingecko_id:
        return fallback
    try:
        symbol_map = {coingecko_id: symbol} if symbol else None
        prices = fetch_prices([coingecko_id], symbol_map=symbol_map)
        return prices.get(coingecko_id, {}).get("price", fallback) or fallback
    except Exception:
        return fallback


def _rebuy_now(config: dict, state: dict, exclude_symbol: str | None = None) -> str | None:
    """Look for a new buy candidate to open right after a /sell or /skip.

    Returns None when the model is disabled or has no API key: in that case the
    command only closes the previous trade, without suggesting the next one.
    """
    api_key = config.get("gemini", {}).get("api_key") or os.environ.get("GEMINI_API_KEY")
    if not api_key or not config.get("gemini", {}).get("enabled", True):
        return None
    try:
        market_data = mm.fetch_market_data(config, state)
    except Exception as e:
        logging.error(f"Failed to fetch buy candidates: {e}")
        return "🟢 Could not look up a new coin right now - try again in a moment."

    buy_candidates = market_data["buy_candidates"]
    if exclude_symbol:
        buy_candidates = [c for c in buy_candidates if c["symbol"] != exclude_symbol]
    return mm._open_position(config, buy_candidates, api_key, _current_interval(), state)


def cmd_sell(config: dict, arg: str) -> str:
    """Force-close the open position and immediately look for the next buy."""
    state = trade_state.load_state()
    position = state.get("position")
    if not position:
        return "No open trade to sell."

    symbol = position["symbol"]
    entry_price = position["entry_price"]
    operation_number = position.get("operation_number", "?")
    current_price = _fresh_price(position.get("coingecko_id"), entry_price, symbol)
    change_pct = ((current_price - entry_price) / entry_price * 100) if entry_price else 0.0
    emoji = "✅" if change_pct >= 0 else "🔻"

    state["position"] = None
    sell_message = (
        f"{emoji} <b>Trade #{operation_number} closed - Sell {symbol}</b> (forced by you via /sell)\n"
        f"Change since buy: {change_pct:+.2f}%"
    )

    buy_message = _rebuy_now(config, state)
    trade_state.save_state(state)
    return f"{sell_message}\n\n{buy_message}" if buy_message else sell_message


def cmd_skip(config: dict, arg: str) -> str:
    """Discard the current suggestion and ask for a different coin."""
    state = trade_state.load_state()
    position = state.get("position")
    if not position:
        buy_message = _rebuy_now(config, state)
        trade_state.save_state(state)
        return buy_message or "🔍 No open trade and the model is disabled - nothing to suggest."

    symbol = position["symbol"]
    operation_number = position.get("operation_number", "?")
    state["position"] = None
    reject_message = (
        f"🔄 <b>Trade #{operation_number} discarded - not buying {symbol}</b> (at your request via /skip)"
    )

    buy_message = _rebuy_now(config, state, exclude_symbol=symbol)
    trade_state.save_state(state)
    return f"{reject_message}\n\n{buy_message}" if buy_message else reject_message


def cmd_block(config: dict, arg: str) -> str:
    """Add a symbol to the blocklist so it is never suggested again."""
    symbol = arg.strip().upper()
    if not symbol:
        return "Usage: /block SYMBOL (e.g. /block PEPE)"
    state = trade_state.load_state()
    blacklist = set(state.get("blacklist", []))
    blacklist.add(symbol)
    state["blacklist"] = sorted(blacklist)
    trade_state.save_state(state)
    return f"🚫 {symbol} blocked. It will not be suggested for a buy until you run /unblock {symbol}."


def cmd_unblock(config: dict, arg: str) -> str:
    """Remove a symbol from the blocklist."""
    symbol = arg.strip().upper()
    if not symbol:
        return "Usage: /unblock SYMBOL (e.g. /unblock PEPE)"
    state = trade_state.load_state()
    blacklist = set(state.get("blacklist", []))
    if symbol not in blacklist:
        return f"{symbol} is not blocked."
    blacklist.discard(symbol)
    state["blacklist"] = sorted(blacklist)
    trade_state.save_state(state)
    return f"✅ {symbol} unblocked."


def cmd_blocked(config: dict, arg: str) -> str:
    """List the currently blocked symbols."""
    state = trade_state.load_state()
    blacklist = state.get("blacklist", [])
    if not blacklist:
        return "No coin is blocked at the moment."
    return "🚫 <b>Blocked coins:</b>\n" + "\n".join(f"• {s}" for s in blacklist)


def cmd_market(config: dict, arg: str) -> str:
    """Show or change which Binance market the volatility radar accepts."""
    state = trade_state.load_state()
    arg = arg.strip().lower()

    if not arg:
        current = state.get("modality", "spot")
        return (
            f"Current radar market: <b>{current}</b>\n\n"
            "Usage: /market spot | /market alpha\n"
            "• spot - only suggests coins with a regular Binance pair (direct buy).\n"
            "• alpha - also suggests coins listed only on Binance Alpha (pre-listing, "
            "separate tab in the app, riskier)."
        )

    if arg not in ("spot", "alpha"):
        return "Invalid market. Usage: /market spot | /market alpha"

    state["modality"] = arg
    trade_state.save_state(state)
    return f"✅ Radar market set to <b>{arg}</b>."


COMMANDS = {
    "/status": cmd_status,
    "/prices": cmd_prices,
    "/sell": cmd_sell,
    "/skip": cmd_skip,
    "/block": cmd_block,
    "/unblock": cmd_unblock,
    "/blocked": cmd_blocked,
    "/market": cmd_market,
    "/balance": cmd_balance,
    "/help": cmd_help,
    "/start": cmd_help,
}


def dispatch_command(text: str, config: dict) -> str:
    """Route a raw Telegram message to its command handler."""
    parts = text.strip().split(maxsplit=1)
    command = parts[0].lower().split("@")[0]
    arg = parts[1].strip() if len(parts) > 1 else ""

    handler = COMMANDS.get(command)
    if not handler:
        return f"Unknown command: {command}\n\nSend /help to see the available commands."

    try:
        return handler(config, arg)
    except Exception as e:
        logging.error(f"Failed to run {command}: {e}")
        return f"Running {command} failed. Try again in a moment."


def main() -> None:
    """Poll Telegram and answer commands until interrupted."""
    load_dotenv(ENV_PATH)
    config = mm.load_config()

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or config.get("telegram", {}).get("bot_token", "")
    chat_id = str(os.environ.get("TELEGRAM_CHAT_ID") or config.get("telegram", {}).get("chat_id", ""))
    if not bot_token or not chat_id:
        logging.error("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are not set; shutting the command listener down.")
        return

    logging.info("Listening for Telegram commands...")
    while True:
        state = trade_state.load_state()
        offset = state.get("telegram_offset")
        try:
            updates = get_updates(bot_token, offset=offset, timeout=30)
        except Exception as e:
            logging.warning(f"Failed to fetch Telegram updates: {e}")
            time.sleep(5)
            continue

        for update in updates:
            state = trade_state.load_state()
            state["telegram_offset"] = update["update_id"] + 1
            trade_state.save_state(state)

            message = update.get("message") or update.get("edited_message") or {}
            msg_chat_id = str(message.get("chat", {}).get("id", ""))
            if msg_chat_id != chat_id:
                continue

            text = (message.get("text") or "").strip()
            if not text.startswith("/"):
                continue

            config = mm.load_config()
            reply = dispatch_command(text, config)
            if reply:
                send_telegram(bot_token, chat_id, reply)


if __name__ == "__main__":
    main()
