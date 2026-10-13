"""Crypto market intelligence monitor.

Collects crypto headlines from public RSS feeds, scores their sentiment,
pulls live prices, asks an LLM for a buy/sell read and pushes alerts to
Telegram and the Linux desktop.
"""

__version__ = "1.0.0"
