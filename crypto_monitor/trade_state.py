"""Shared access to state.json (open position, trade counter, blocklist).

Used both by the automated cycle (``market_monitor``) and by the Telegram command
listener (``telegram_bot``): two separate processes reading and writing the same file,
which is why load/save take an ``fcntl.flock`` to avoid a JSON corrupted by concurrent
writes.
"""
import fcntl
import json
from pathlib import Path
from crypto_monitor.paths import CACHE_DIR

STATE_PATH = CACHE_DIR / "state.json"
_LOCK_PATH = STATE_PATH.with_suffix(".lock")


def _lock():
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(_LOCK_PATH, "w")
    fcntl.flock(lock_file, fcntl.LOCK_EX)
    return lock_file


def load_state() -> dict:
    lock_file = _lock()
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text())
        return {}
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


def save_state(state: dict):
    lock_file = _lock()
    try:
        STATE_PATH.write_text(json.dumps(state, indent=2))
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
