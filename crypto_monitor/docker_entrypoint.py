#!/usr/bin/env python3
import os
import time
import subprocess
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

interval_minutes = int(os.environ.get("MONITOR_INTERVAL_MINUTES", 30))
logging.info(f"Starting the monitor loop (interval: {interval_minutes} minutes)...")

while True:
    try:
        logging.info("Running the market monitor...")
        # Run the monitor with the very same interpreter
        subprocess.run([sys.executable, "-m", "crypto_monitor.market_monitor"], check=True)
    except subprocess.CalledProcessError as e:
        logging.error(f"Monitor run failed: {e}")
    except Exception as e:
        logging.error(f"Unexpected error in the entrypoint: {e}")

    logging.info(f"Waiting {interval_minutes} minutes until the next run...")
    time.sleep(interval_minutes * 60)
