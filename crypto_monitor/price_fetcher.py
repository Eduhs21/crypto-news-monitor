import time
import logging
import requests

from crypto_monitor.binance_market import get_alpha_symbols, get_spot_prices

COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/markets"


def _get_with_retry(params: dict, timeout: int, max_retries: int) -> list:
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(COINGECKO_URL, params=params, timeout=timeout)
            # Handle 429 (rate limit) and 5xx server errors
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
                wait = 2 ** attempt * 5  # 5s, 10s, 20s
                logging.warning(f"[CoinGecko] HTTP {resp.status_code}, retrying in {wait}s... ({attempt + 1}/{max_retries})")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            if attempt == max_retries:
                raise e
            wait = 2 ** attempt * 5
            logging.warning(f"[CoinGecko] Request error ({e}), retrying in {wait}s... ({attempt + 1}/{max_retries})")
            time.sleep(wait)
    return []


def fetch_prices(coingecko_ids: list[str], symbol_map: dict[str, str] | None = None,
                  timeout: int = 15, max_retries: int = 3) -> dict:
    """Fetch live prices and variations from CoinGecko.

    Returns ``{coingecko_id: {"price", "change_1h", "change_24h", "change_7d"}}`` and
    retries up to *max_retries* times with exponential backoff on transient errors or
    rate limits (429).

    When ``symbol_map`` ({coingecko_id: symbol}) is supplied, the price (only the price;
    the percentages still come from CoinGecko) is overridden by the Binance SYMBOLUSDT
    pair whenever the exchange lists it, because that quote is closer to real time. With
    no matching pair, or with Binance down, the CoinGecko price is kept as a fallback.
    """
    params = {
        "ids": ",".join(coingecko_ids),
        "vs_currency": "usd",
        "price_change_percentage": "1h,24h,7d",
    }
    raw = _get_with_retry(params, timeout, max_retries)

    entries_by_id = {entry.get("id"): entry for entry in raw}
    result = {}
    for cg_id in coingecko_ids:
        entry = entries_by_id.get(cg_id, {})
        result[cg_id] = {
            "price": entry.get("current_price", 0.0) or 0.0,
            "change_1h": entry.get("price_change_percentage_1h_in_currency", 0.0) or 0.0,
            "change_24h": entry.get("price_change_percentage_24h", 0.0) or 0.0,
            "change_7d": entry.get("price_change_percentage_7d_in_currency", 0.0) or 0.0,
            "price_source": "coingecko",
        }

    if symbol_map:
        binance_prices = get_spot_prices(timeout)
        if binance_prices:
            for cg_id, symbol in symbol_map.items():
                if cg_id not in result or not symbol:
                    continue
                if symbol.upper() in binance_prices:
                    result[cg_id]["price"] = binance_prices[symbol.upper()]
                    result[cg_id]["price_source"] = "binance"

    return result


def fetch_top_movers(exclude_ids: set[str], top_n: int = 8, universe_size: int = 250,
                      timeout: int = 15, max_retries: int = 3,
                      modality: str = "spot") -> list[dict]:
    """Scan the market for the sharpest short-term movers (the "volatility radar").

    Walks the ``universe_size`` largest coins by market cap and returns the ``top_n``
    with the largest absolute price change in the last hour: fast-trade candidates
    outside the fixed watchlist. Each item is
    ``{symbol, coingecko_id, price, change_1h, change_24h, change_7d}``.

    ``modality`` decides which tradable universe is accepted:

    - "spot" (default): only coins with a SYMBOLUSDT pair on Binance, i.e. a regular
      direct buy.
    - "alpha": also accepts coins listed only on Binance Alpha (pre-listing, a separate
      tab in the app) on top of the spot ones, which is a wider and riskier universe.

    Survivors carry the Binance price when available, since it is closer to real time
    than CoinGecko. If Binance is down the filter is skipped (fail open) and the radar
    proceeds over the whole CoinGecko universe.
    """
    params = {
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": universe_size,
        "page": 1,
        "price_change_percentage": "1h,24h,7d",
    }
    raw = _get_with_retry(params, timeout, max_retries)

    spot_prices = get_spot_prices(timeout)
    alpha_symbols = get_alpha_symbols(timeout) if modality == "alpha" else set()

    candidates = []
    skipped_no_pair = []
    for entry in raw:
        cg_id = entry.get("id")
        if not cg_id or cg_id in exclude_ids:
            continue
        change_1h = entry.get("price_change_percentage_1h_in_currency")
        if change_1h is None:
            continue
        symbol = (entry.get("symbol") or "").upper()
        candidate = {
            "symbol": symbol,
            "coingecko_id": cg_id,
            "price": entry.get("current_price", 0.0) or 0.0,
            "change_1h": change_1h,
            "change_24h": entry.get("price_change_percentage_24h", 0.0) or 0.0,
            "change_7d": entry.get("price_change_percentage_7d_in_currency", 0.0) or 0.0,
            "price_source": "coingecko",
        }
        if spot_prices:
            if symbol in spot_prices:
                candidate["price"] = spot_prices[symbol]
                candidate["price_source"] = "binance"
            elif modality == "alpha" and symbol in alpha_symbols:
                pass  # no Spot pair, but accepted via Alpha (price stays the CoinGecko one)
            else:
                skipped_no_pair.append(symbol or cg_id)
                continue
        candidates.append(candidate)

    if not spot_prices:
        logging.warning("[Radar] Binance unavailable; the radar ran WITHOUT filtering by tradable pair.")
    elif skipped_no_pair:
        mostrados = ", ".join(sorted(skipped_no_pair)[:15])
        reticencias = ", ..." if len(skipped_no_pair) > 15 else ""
        logging.info(
            f"[Radar] {len(skipped_no_pair)} coins excluded from the radar (market '{modality}'): "
            f"{mostrados}{reticencias}"
        )

    candidates.sort(key=lambda c: abs(c["change_1h"]), reverse=True)
    return candidates[:top_n]
