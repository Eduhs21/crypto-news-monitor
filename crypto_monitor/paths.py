"""Project-level paths.

Every module resolves ``config.yaml`` and ``.env`` through here so the package
keeps working whether it runs from a source checkout, from a Docker image or
from an installed wheel.

Both locations can be overridden with environment variables, which is what the
Docker image does:

    CRYPTO_MONITOR_CONFIG=/app/config.yaml
    CRYPTO_MONITOR_ENV=/app/.env
"""
import os
from pathlib import Path

#: Repository root (the directory that holds ``config.yaml``).
PROJECT_ROOT = Path(__file__).resolve().parent.parent

CONFIG_PATH = Path(os.environ.get("CRYPTO_MONITOR_CONFIG", PROJECT_ROOT / "config.yaml"))
ENV_PATH = Path(os.environ.get("CRYPTO_MONITOR_ENV", PROJECT_ROOT / ".env"))

#: Cache directory for "already seen" headlines and open-position state.
CACHE_DIR = Path(os.environ.get("CRYPTO_MONITOR_CACHE", Path.home() / ".cache" / "crypto_monitor"))
