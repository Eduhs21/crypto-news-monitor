#!/usr/bin/env python3
"""Command line interface: headline feed and on-demand LLM recommendations.

Usage:
    python -m crypto_monitor.cli --lang en --limit 15
    python -m crypto_monitor.cli --buy
    python -m crypto_monitor.cli --sell
"""
import argparse
import subprocess
import feedparser
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from rich.console import Console
from rich.table import Table
from rich import box
from rich.panel import Panel
from rich.text import Text
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
import os
import json
import yaml
from pathlib import Path
from crypto_monitor import database
from crypto_monitor.paths import CACHE_DIR, CONFIG_PATH, ENV_PATH

console = Console()
analyzer = SentimentIntensityAnalyzer()

CACHE_FILE = str(CACHE_DIR / "seen.json")

def load_cache():
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

def save_cache(cache):
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f)

# Built-in feeds, grouped by language. Overridable through the "feeds" key in config.yaml.
DEFAULT_FEEDS = {
    "en": {
        "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "Cointelegraph": "https://cointelegraph.com/rss",
        "Decrypt": "https://decrypt.co/feed",
        "Ethereum Foundation": "https://blog.ethereum.org/feed.xml"
    },
    "pt": {
        "Portal do Bitcoin": "https://portaldobitcoin.uol.com.br/feed/",
        "InfoMoney": "https://www.infomoney.com.br/mercados/criptomoedas/feed/"
    }
}

def load_feeds():
    config_path = CONFIG_PATH
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
                if config and "feeds" in config:
                    return config["feeds"]
        except:
            pass
    return DEFAULT_FEEDS

FEEDS = load_feeds()


def analyze_sentiment(text):
    """Return the colour, icon and raw VADER compound score for *text*."""
    score = analyzer.polarity_scores(text)['compound']
    if score >= 0.3:
        return "green", "🚀", score
    elif score <= -0.3:
        return "red", "🩸", score
    return "white", "⚪", score

def send_linux_notification(title, message, urgency="normal"):
    """Raise a native desktop notification (Linux, via ``notify-send``)."""
    try:
        # notify-send ships with most GNOME/KDE desktops
        subprocess.run(["notify-send", "-u", urgency, title, message], check=True)
    except FileNotFoundError:
        pass  # Silently skip when notify-send is not installed

def fetch_rss_news(lang, search_term, enable_notify):
    """Fetch RSS headlines, applying the language and keyword filters."""
    all_news = []
    
    seen_cache = load_cache()
    new_seen = {}
    
    # Pick the sources for the requested language
    sources_to_check = {}
    if lang == "all":
        sources_to_check.update(FEEDS["en"])
        sources_to_check.update(FEEDS["pt"])
    else:
        sources_to_check.update(FEEDS.get(lang, {}))

    with console.status(f"[bold green]Polling news sources ({lang})..."):
        for source, url in sources_to_check.items():
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries[:10]:
                    title = entry.title
                    
                    # Apply the keyword filter, if any
                    if search_term and search_term.lower() not in title.lower():
                        continue
                    
                    try:
                        pub_date = parsedate_to_datetime(entry.published)
                    except (AttributeError, TypeError):
                        pub_date = datetime.now(timezone.utc)
                        
                    entry_id = getattr(entry, 'id', getattr(entry, 'link', title))
                    
                    db_seen = database.is_news_seen(entry_id)
                    if db_seen is not None:
                        is_new = not db_seen
                        if is_new:
                            database.save_news_item(entry_id, title, source, pub_date)
                    else:
                        is_new = entry_id not in seen_cache
                        if is_new:
                            new_seen[entry_id] = datetime.now(timezone.utc).isoformat()
                    
                    all_news.append({
                        "title": title,
                        "source": source,
                        "date": pub_date,
                        "is_new": is_new,
                        "id": entry_id
                    })
            except Exception as e:
                console.print(f"[bold red]Failed to fetch from {source}:[/bold red] {e}")

    # Newest headline first
    all_news.sort(key=lambda x: x["date"], reverse=True)
    
    # Persist the "already seen" cache
    if new_seen:
        seen_cache.update(new_seen)
        save_cache(seen_cache)
        
    # Raise desktop alerts for extreme headlines
    if enable_notify and all_news:
        for item in all_news:
            if not item["is_new"]:
                continue
            
            color, icon, score = analyze_sentiment(item["title"])
            
            # Alert rule: very negative sentiment or a critical keyword
            is_urgent = score < -0.6 or any(w in item["title"].lower() for w in ["hack", "urgente", "urgent", "crash"])
            
            if is_urgent:
                send_linux_notification(
                    f"🚨 Critical alert: {item['source']}", 
                    item["title"], 
                    urgency="critical"
                )

    return all_news

def display_news(news_items, limit):
    """Render the headline table with its sentiment colouring."""
    if not news_items:
        console.print("[yellow]No headlines matched these filters.[/yellow]")
        return

    table = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold cyan")
    table.add_column("Time", style="dim", width=8)
    table.add_column("Source", style="magenta", width=17)
    table.add_column("Headline (sentiment)")

    for item in news_items[:limit]:
        time_str = item["date"].strftime("%H:%M")
        raw_title = item["title"]
        
        # Colour the headline by sentiment
        color, icon, _ = analyze_sentiment(raw_title)
        formatted_title = f"[{color}]{raw_title}[/{color}] {icon}"
            
        table.add_row(time_str, item["source"], formatted_title)

    console.print(table)

def format_market_history(history: list, target_symbols: set[str]) -> str:
    """Compact historical snapshots so the LLM prompt stays small."""
    if not history:
        return "(no history available in MongoDB)"

    blocks = []
    seen_headlines = {symbol: set() for symbol in target_symbols}
    for run in history:
        created_at = run.get("created_at")
        date_label = created_at.isoformat(timespec="minutes") if created_at else "unknown date"
        coins = run.get("portfolio", []) + run.get("watchlist", [])
        coin_lines = []
        for coin in coins:
            if coin.get("symbol") not in target_symbols:
                continue
            coin_lines.append(
                f"{coin['symbol']}: price {_format_price_precise(coin.get('price', 0))}, "
                f"24h {coin.get('change_24h', 0):+.2f}%, "
                f"7d {coin.get('change_7d', 0):+.2f}%"
            )
            for headline in coin.get("headlines", [])[:3]:
                # The same RSS item reappears across runs. History arrives newest
                # first, so keeping the first occurrence keeps the most recent one.
                headline_key = " ".join(headline.lower().split())
                symbol_headlines = seen_headlines.setdefault(coin["symbol"], set())
                if headline_key in symbol_headlines:
                    continue
                symbol_headlines.add(headline_key)
                score = analyzer.polarity_scores(headline)['compound']
                coin_lines.append(
                    f"  headline {coin['symbol']} (VADER {score:+.2f}): {headline}"
                )
        analysis = run.get("analysis", {})
        past_signals = []
        for item in analysis.get("opportunities", []):
            if item.get("symbol") in target_symbols:
                past_signals.append(
                    f"buy {item.get('symbol')}={item.get('signal')} "
                    f"(confidence {float(item.get('confidence', 0)):.0%})"
                )
        for item in analysis.get("risks", []):
            if item.get("symbol") in target_symbols:
                past_signals.append(
                    f"risk {item.get('symbol')}={item.get('risk_level')} "
                    f"(confidence {float(item.get('confidence', 0)):.0%})"
                )
        if past_signals:
            coin_lines.append("  previous LLM analysis: " + "; ".join(past_signals))
        if coin_lines:
            blocks.append(f"{date_label} — " + "; ".join(coin_lines))
    return "\n".join(blocks) or "(no history for the evaluated coins)"


def _format_price_precise(value: float) -> str:
    """Format a price with enough decimals for sub-cent coins (PEPE, SHIB, ...)."""
    abs_value = abs(value)
    if abs_value == 0:
        return "$0.00"
    if abs_value < 0.01:
        return f"${value:,.8f}"
    if abs_value < 1:
        return f"${value:,.4f}"
    return f"${value:,.2f}"


def build_recommendation_prompt(coin_data: list, action: str, history: list | None = None,
                                horizon_minutes: int | None = None) -> str:
    """Build the LLM prompt for a buy or sell recommendation.

    Args:
        coin_data: Coins to evaluate, each with price, variations and
            VADER-scored headlines.
        action: ``"buy"`` (evaluate the watchlist) or ``"sell"`` (evaluate the
            portfolio).
        history: Previous monitor runs, used only to spot a trend or reversal.
        horizon_minutes: When set, switches the persona to short-term scalping
            and asks for a take-profit target.

    Returns:
        The full prompt string, which instructs the model to answer in strict
        JSON.
    """
    def format_coin_block(coin):
        news_block = "\n".join(f'  - {h}' for h in coin.get("vader_headlines", [])) or "  (no recent headlines)"
        details = ""
        if action == "sell":
            details = (
                f"Buy price: {_format_price_precise(coin.get('buy_price', 0.0))}\n"
                f"Quantity: {coin.get('quantity', 0.0)}\n"
            )
        return f"""
Coin: {coin['symbol']}
Current price: {_format_price_precise(coin['price'])}
Change 1h: {coin.get('change_1h', 0.0):.2f}%
Change 24h: {coin['change_24h']:.2f}%
Change 7d: {coin.get('change_7d', 0.0):.2f}%
{details}Recent headlines with VADER sentiment score:
{news_block}
"""

    coins_block = "\n".join(format_coin_block(c) for c in coin_data)
    symbols = {coin["symbol"] for coin in coin_data}
    history_block = format_market_history(history or [], symbols)
    valid_symbols = ", ".join(sorted(symbols))

    target_action = "BUY (watchlist)" if action == "buy" else "SELL (portfolio)"
    action_rules = (
        "identify the BEST immediate BUY opportunity" if action == "buy" else
        "identify which portfolio coin carries the HIGHEST RISK or is the best candidate to SELL "
        "and realise profit"
    )

    is_scalp_buy = bool(horizon_minutes) and action == "buy"

    if horizon_minutes:
        persona = (
            f"You are a crypto SCALPING specialist with an AGGRESSIVE, risk-tolerant profile. "
            f"The target is a trade with a horizon of roughly {horizon_minutes} minutes, not a "
            f"medium or long term investment.\n"
            f"Analyse the market and sentiment data below and {action_rules}."
        )
        risk_rule = (
            '- PRIORITISE the 1-HOUR change (very short term momentum) over 24h/7d: it is the most '
            'relevant signal for this horizon.\n'
            '- "Calm and stable" coins (low 1h change) are BAD for this goal even though they look '
            'safer, because they do not move enough to produce a profit in a few minutes. Prefer '
            'coins with strong, recent price movement even if they are riskier (memecoins, low '
            'market-cap altcoins).\n'
            '- Be permissive with "confidence": strong 1h momentum combined with non-negative '
            'sentiment already justifies moderate/high confidence, even on riskier coins.'
        )
    else:
        persona = (
            f"You are a crypto investment specialist with a CONSERVATIVE risk profile.\n"
            f"Analyse the market and sentiment data below and {action_rules}."
        )
        risk_rule = (
            '- Be conservative with "confidence": only assign high confidence (>0.7) when the price '
            'change AND the news sentiment clearly point the same way. Neutral or inconclusive '
            'sentiment, or small changes with no relevant news, must result in low "confidence" '
            '(<0.4).'
        )

    if is_scalp_buy:
        # No "alternatives" here: the automated cycle never reads that field (only the manual
        # --buy/--sell command renders it), so asking for it would burn output tokens.
        json_schema = """{
  "best_pick": "SYMBOL",
  "reasoning": "2-3 objective sentences linking the recent price change to the VADER sentiment of the supplied headlines.",
  "confidence": 0.0-1.0,
  "take_profit_pct": 0.0
}"""
        take_profit_rule = (
            f'- "take_profit_pct": the upside percentage (positive number, e.g. 8.5) you recommend '
            f'as the sell target to lock in profit on this trade, based on the recent volatility '
            f'(1h/24h) of the chosen coin. Pick a realistic target for a ~{horizon_minutes} minute '
            f'horizon: not so tight that normal market noise triggers it, not so wide that it is '
            f'unlikely to be reached in that window.\n'
        )
    else:
        json_schema = """{
  "best_pick": "SYMBOL",
  "reasoning": "2-3 objective sentences linking the recent price change to the VADER sentiment of the supplied headlines.",
  "confidence": 0.0-1.0,
  "alternatives": [
    {"symbol": "SYMBOL", "reasoning": "One short sentence of secondary rationale"}
  ]
}"""
        take_profit_rule = ""

    return f"""{persona}
Answer STRICTLY in JSON, with no markdown and no ```json fences, exactly in this shape:

{json_schema}

Rules:
- "best_pick" MUST be exactly one of these symbols, no exceptions: {valid_symbols}. Never invent a symbol outside that list.
- Base your verdict exclusively on the data supplied below (price, change and VADER-scored headlines). Do not invent news or data that was not provided.
- Use the history only to identify a trend, acceleration or reversal; weight current data more heavily.
- Do not treat historical snapshots as real-time prices.
{risk_rule}
{take_profit_rule}- If no coin has a plausible case for the action, still return the relatively best pick (to fill "best_pick") but with low "confidence". The system consuming this answer only acts above a minimum confidence threshold, so do not inflate confidence just to sound decisive.
- Be concise.

=== CRYPTO DATA FOR {target_action} ANALYSIS ===
{coins_block}

=== RECENT MONGODB HISTORY (newest first) ===
{history_block}
"""


def display_recommendation(recommendation: dict, action: str):
    best_pick = recommendation.get("best_pick", "N/A")
    reasoning = recommendation.get("reasoning", "No reasoning provided.")
    try:
        confidence = float(recommendation.get("confidence", 0.0))
    except:
        confidence = 0.0
    alternatives = recommendation.get("alternatives", [])
    
    color = "green" if action == "buy" else "red"
    action_title = "BUY" if action == "buy" else "SELL"
    icon = "🚀" if action == "buy" else "🩸"
    
    # Confidence bar
    bar_width = 20
    filled = int(confidence * bar_width)
    bar = "█" * filled + "░" * (bar_width - filled)
    
    # Panel body
    text = Text()
    text.append(f"\n🏆 Top pick: ", style="bold")
    text.append(f"{best_pick}\n", style=f"bold {color}")
    text.append(f"🎯 Model confidence: {confidence:.0%} [{bar}]\n\n", style="cyan")
    text.append(f"📝 Reasoning:\n", style="bold underline")
    text.append(f"{reasoning}\n\n", style="italic")
    
    if alternatives:
        text.append(f"🔄 Runners-up:\n", style="bold")
        for alt in alternatives:
            alt_sym = alt.get("symbol", "N/A")
            alt_reason = alt.get("reasoning", "")
            text.append(f"  • {alt_sym}: ", style=f"bold {color}")
            text.append(f"{alt_reason}\n")
            
    panel = Panel(
        text,
        title=f"[bold {color}]{icon} BEST {action_title} RIGHT NOW ({best_pick})[/bold {color}]",
        border_style=color,
        expand=False
    )
    console.print(panel)


def get_gemini_recommendation(action: str):
    """Run an on-demand LLM buy/sell analysis and print the recommendation."""
    from dotenv import load_dotenv
    load_dotenv(ENV_PATH)
    
    config_path = CONFIG_PATH
    if not config_path.exists():
        console.print("[bold red]Error:[/bold red] config.yaml not found.")
        return
        
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except Exception as e:
        console.print(f"[bold red]Failed to load config.yaml:[/bold red] {e}")
        return
        
    api_key = config.get("gemini", {}).get("api_key") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        console.print("[bold red]Error:[/bold red] Gemini API key not set in config.yaml or .env.")
        return

    portfolio_cfg = config.get("portfolio", []) or []
    watchlist_cfg = config.get("watchlist", []) or []
    
    if action == "buy":
        if not watchlist_cfg:
            console.print("[yellow]No coins configured in the watchlist (config.yaml).[/yellow]")
            return
        coins_to_evaluate = watchlist_cfg
    else:
        if not portfolio_cfg:
            console.print("[yellow]No coins configured in the portfolio (config.yaml).[/yellow]")
            return
        coins_to_evaluate = portfolio_cfg
        
    from crypto_monitor.price_fetcher import fetch_prices
    from crypto_monitor.market_monitor import get_feeds, get_coin_aliases, fetch_headlines, build_coin_data
    from crypto_monitor.gemini_client import call_gemini
    
    all_ids = [c["coingecko_id"] for c in coins_to_evaluate]
    symbol_map = {c["coingecko_id"]: c["symbol"] for c in coins_to_evaluate}

    with console.status("[bold green]Fetching prices and recent headlines..."):
        try:
            prices = fetch_prices(all_ids, symbol_map=symbol_map)
        except Exception as e:
            console.print(f"[bold red]Failed to fetch prices from CoinGecko:[/bold red] {e}")
            return
            
        feeds = get_feeds(config)
        coin_aliases = get_coin_aliases(config)
        
        # Narrow the alias map down to the coins being evaluated
        target_aliases = {c["symbol"]: coin_aliases.get(c["symbol"], [c["symbol"].lower()]) for c in coins_to_evaluate}
        
        headlines = fetch_headlines(config.get("news", {}).get("lang", "all"), 8, feeds, target_aliases)
        coin_data = build_coin_data(coins_to_evaluate, prices, headlines)

    # Score every headline with VADER
    for coin in coin_data:
        vader_headlines = []
        for h in coin.get("headlines", []):
            score = analyzer.polarity_scores(h)['compound']
            sent = "🚀" if score >= 0.3 else "🩸" if score <= -0.3 else "⚪"
            vader_headlines.append(f'"{h}" (Sentimento VADER: {score:+.2f} {sent})')
        coin["vader_headlines"] = vader_headlines

    history_limit = config.get("history", {}).get("prompt_runs", 10)
    symbols = [coin["symbol"] for coin in coin_data]
    history = database.get_market_history(symbols, history_limit)
    if history:
        console.print(f"[dim]Using {len(history)} historical run(s) from MongoDB.[/dim]")
    else:
        console.print("[dim]No MongoDB history; analysis based on current data only.[/dim]")

    prompt = build_recommendation_prompt(coin_data, action, history)

    with console.status("[bold green]Asking the Gemini model..."):
        try:
            model = config.get("gemini", {}).get("model", "gemini-3.5-flash")
            recommendation = call_gemini(prompt, api_key, model=model)
        except Exception as e:
            console.print(f"[bold red]Gemini call failed:[/bold red] {e}")
            return

    display_recommendation(recommendation, action)
    if not database.save_recommendation(recommendation, action, coin_data, history):
        console.print("[yellow]Warning: could not save the recommendation to MongoDB.[/yellow]")


def main() -> None:
    """Entry point for ``python -m crypto_monitor.cli``."""
    parser = argparse.ArgumentParser(description="Crypto market intelligence monitor")
    parser.add_argument("-l", "--limit", type=int, default=15, help="Maximum number of headlines to display")
    parser.add_argument("-s", "--search", type=str, help="Filter headlines by keyword")
    parser.add_argument("--lang", type=str, choices=["en", "pt", "all"], default="all", help="Filter sources by language")
    parser.add_argument("--notify", action="store_true", help="Send desktop pop-ups for critical or very negative headlines")
    parser.add_argument("--buy", action="store_true", help="Ask the LLM for the best buy in your watchlist right now")
    parser.add_argument("--sell", action="store_true", help="Ask the LLM for the riskiest position / best sell in your portfolio")
    
    args = parser.parse_args()

    if args.buy:
        get_gemini_recommendation("buy")
    elif args.sell:
        get_gemini_recommendation("sell")
    else:
        try:
            news = fetch_rss_news(args.lang, args.search, args.notify)
            display_news(news, args.limit)
        except KeyboardInterrupt:
            console.print("\n[dim]Cancelled.[/dim]")


if __name__ == "__main__":
    main()
