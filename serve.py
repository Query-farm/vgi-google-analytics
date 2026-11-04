# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "vgi-python[http,duckdb]>=0.48.2,<0.49",
#     "vgi-rpc>=0.52.4",
#     "httpx>=0.27",
#     "google-auth>=2.30",
#     "oauthlib>=3.3.1",
# ]
# ///
"""HTTP entry point for the Google Analytics 4 VGI worker (``uv run serve.py --port 8000``).

Does not fall back to the server's own Application Default Credentials unless
started with ``--allow-adc`` — see ``vgi_google_analytics.worker.main_http``.
"""

from __future__ import annotations

from vgi_google_analytics.worker import main_http

if __name__ == "__main__":
    main_http()
