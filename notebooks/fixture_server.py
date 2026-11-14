"""A deliberately small synthetic GA4 report API; no external forwarding."""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class FixtureServer:
    """Group explicit synthetic events with exact distinct-user semantics."""

    def __init__(self, path: Path) -> None:
        fixture = json.loads(path.read_text())
        self.requests: list[dict] = []
        requests = self.requests
        metadata = {
            "name": "properties/1001/metadata",
            "dimensions": [
                {
                    "apiName": "date",
                    "uiName": "Date",
                    "description": "Synthetic property calendar date",
                    "category": "Time",
                    "customDefinition": False,
                }
            ],
            "metrics": [
                {
                    "apiName": name,
                    "uiName": label,
                    "description": label,
                    "type": "TYPE_INTEGER",
                    "category": "Fixture",
                }
                for name, label in [
                    ("totalUsers", "Distinct synthetic user IDs"),
                    ("eventCount", "Synthetic event records"),
                ]
            ],
        }

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def respond(self, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def authorized(self) -> bool:
                if self.headers.get("Authorization") != "Bearer synthetic-notebook-token":
                    self.send_error(403, "Only the synthetic fixture token is accepted")
                    return False
                return True

            def do_GET(self) -> None:
                if not self.authorized():
                    return
                if self.path != "/data/v1beta/properties/1001/metadata":
                    self.send_error(404, "No such fixture route")
                    return
                requests.append({"kind": "metadata"})
                self.respond(metadata)

            def do_POST(self) -> None:
                if not self.authorized():
                    return
                if self.path != "/data/v1beta/properties/1001:runReport":
                    self.send_error(405, "Only the read-only runReport fixture route is supported")
                    return
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                dims = [x["name"] for x in body.get("dimensions", [])]
                metrics = [x["name"] for x in body.get("metrics", [])]
                ranges = body.get("dateRanges", [])
                if (
                    dims not in [[], ["date"]]
                    or metrics != ["totalUsers", "eventCount"]
                    or len(ranges) != 1
                    or "dimensionFilter" in body
                    or "metricFilter" in body
                ):
                    self.send_error(400, "Request outside the synthetic report contract")
                    return
                start, end = ranges[0]["startDate"], ranges[0]["endDate"]
                if start != "2026-09-01" or end not in {"2026-09-03", "2026-09-05"}:
                    self.send_error(400, "Date range outside fixture coverage")
                    return
                groups = defaultdict(list)
                for event in fixture["events"]:
                    if start <= event["date"] <= end:
                        groups[(event["date"],) if dims else ()].append(event)
                rows = []
                for key, events in sorted(groups.items()):
                    rows.append(
                        {
                            "dimensionValues": [{"value": v.replace("-", "")} for v in key],
                            "metricValues": [
                                {"value": str(len({e["user"] for e in events}))},
                                {"value": str(len(events))},
                            ],
                        }
                    )
                offset, limit = int(body.get("offset", 0)), int(body.get("limit", 100000))
                if offset < 0 or limit < 1:
                    self.send_error(400, "Invalid pagination")
                    return
                page = rows[offset : offset + limit]
                requests.append(
                    {
                        "kind": "report",
                        "dimensions": dims,
                        "offset": offset,
                        "limit": limit,
                        "rows": len(page),
                        "total_rows": len(rows),
                        "start_date": start,
                        "end_date": end,
                    }
                )
                self.respond(
                    {
                        "dimensionHeaders": [{"name": d} for d in dims],
                        "metricHeaders": [{"name": m, "type": "TYPE_INTEGER"} for m in metrics],
                        "rows": page,
                        "rowCount": len(rows),
                        "metadata": {
                            "timeZone": fixture["property_time_zone"],
                            "subjectToThresholding": False,
                            "dataLossFromOtherRow": False,
                        },
                    }
                )

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        """Stop only this fixture server."""
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
