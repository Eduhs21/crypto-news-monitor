"""Authenticated Binance account integration - read-only balances for now.

This module never places an order. The public (keyless) API used for live prices lives
in ``price_fetcher`` and is independent of this.
"""
import os

from binance.client import Client


def get_client(config: dict) -> Client | None:
    """Return an authenticated Client, or None when ``binance.enabled`` is false or the
    key/secret are missing. ``binance.testnet`` (default true) decides whether it points
    at the Binance Testnet (paper balance) or at the live account."""
    binance_cfg = config.get("binance", {}) or {}
    if not binance_cfg.get("enabled", False):
        return None

    api_key = os.environ.get("BINANCE_API_KEY") or binance_cfg.get("api_key")
    api_secret = os.environ.get("BINANCE_API_SECRET") or binance_cfg.get("api_secret")
    if not api_key or not api_secret:
        return None

    testnet = binance_cfg.get("testnet", True)
    return Client(api_key, api_secret, testnet=testnet)


def fetch_account_balances(config: dict) -> list[dict]:
    """Return the assets holding a balance > 0 as ``[{"asset", "free", "locked"}]``.

    Reads the testnet or the live account according to ``binance.testnet``. Raises
    RuntimeError when the integration is disabled or has no credentials, and propagates
    the original Binance API exception when authentication or the query fails."""
    client = get_client(config)
    if client is None:
        raise RuntimeError(
            "Binance integration is disabled, or BINANCE_API_KEY/BINANCE_API_SECRET are "
            "not configured (see .env and config.yaml)."
        )

    account = client.get_account()
    balances = []
    for entry in account.get("balances", []):
        free = float(entry["free"])
        locked = float(entry["locked"])
        if free + locked > 0:
            balances.append({"asset": entry["asset"], "free": free, "locked": locked})
    return balances
