"""Classify coins by where they are tradable on Binance.

Either the Spot market (a SYMBOLUSDT pair, i.e. a regular direct buy) or Binance Alpha
(pre-listing tokens, a separate tab in the app, with no official public API: this uses
the same endpoint the Binance app itself calls).

Both lists are cached in process memory with a short TTL. The volatility radar looks up
dozens of symbols per cycle, so each full list is fetched once and reused instead of
issuing one call per symbol.
"""
import logging
import time

import requests

_SPOT_URL = "https://api.binance.com/api/v3/ticker/price"
_ALPHA_URL = "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"
_CACHE_TTL_SECONDS = 300

_cache = {"spot": (0.0, {}), "alpha": (0.0, set())}


def _fetch_spot_prices(timeout: int) -> dict[str, float]:
    resp = requests.get(_SPOT_URL, timeout=timeout)
    resp.raise_for_status()
    prices = {}
    for entry in resp.json():
        symbol = entry.get("symbol", "")
        if symbol.endswith("USDT"):
            try:
                prices[symbol[:-4]] = float(entry["price"])
            except (KeyError, TypeError, ValueError):
                continue
    return prices


def _fetch_alpha_symbols(timeout: int) -> set[str]:
    resp = requests.get(_ALPHA_URL, timeout=timeout)
    resp.raise_for_status()
    data = resp.json().get("data", [])
    return {(entry.get("symbol") or "").upper() for entry in data if entry.get("symbol")}


def get_spot_prices(timeout: int = 10) -> dict[str, float]:
    """Return ``{symbol: USDT price}`` for every coin with a Spot pair on Binance.

    On failure the previous cache is kept (even if stale) instead of being cleared, so
    callers fail open."""
    fetched_at, prices = _cache["spot"]
    if time.time() - fetched_at < _CACHE_TTL_SECONDS:
        return prices
    try:
        prices = _fetch_spot_prices(timeout)
        _cache["spot"] = (time.time(), prices)
    except Exception as e:
        logging.warning(f"[Binance] Failed to refresh Spot pairs: {e}")
    return prices


def get_alpha_symbols(timeout: int = 10) -> set[str]:
    """Symbols listed on Binance Alpha (pre-listing). Same fail-open policy as Spot."""
    fetched_at, symbols = _cache["alpha"]
    if time.time() - fetched_at < _CACHE_TTL_SECONDS:
        return symbols
    try:
        symbols = _fetch_alpha_symbols(timeout)
        _cache["alpha"] = (time.time(), symbols)
    except Exception as e:
        logging.warning(f"[Binance] Failed to refresh the Alpha list: {e}")
    return symbols


def classify_modality(symbol: str, timeout: int = 10) -> str | None:
    """Return 'spot' when a USDT pair exists, 'alpha' when the coin only exists on Binance
    Alpha, and None when neither matched (endpoints down, or the coin is simply unlisted)."""
    symbol = (symbol or "").upper()
    if symbol in get_spot_prices(timeout):
        return "spot"
    if symbol in get_alpha_symbols(timeout):
        return "alpha"
    return None
