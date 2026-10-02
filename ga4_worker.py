# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "vgi-python[http,haybarn]>=0.37.3",
#     "vgi-rpc>=0.47.2",
#     "httpx>=0.27",
#     "google-auth>=2.30",
# ]
# ///
"""Stdio entry point for the Google Analytics 4 VGI worker (``uv run``).

ATTACH 'ga4' (TYPE vgi, LOCATION 'uv run ga4_worker.py');
"""

from __future__ import annotations

from vgi_google_analytics.worker import main

if __name__ == "__main__":
    main()
