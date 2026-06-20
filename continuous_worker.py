"""Optional 24/7 runner for a real always-on host.

GitHub Actions is the free fallback and checks every five minutes. This file
runs exactly the same scanner every 45 seconds when deployed on an always-on
service. It uses the same secrets and JSON state files.
"""
import os
import time
from stock_alert_bot_v4 import main

POLL_SECONDS = max(30, int(os.getenv("POLL_SECONDS", "45")))

if __name__ == "__main__":
    while True:
        try:
            main()
        except Exception as exc:
            print("worker error:", exc)
        time.sleep(POLL_SECONDS)
