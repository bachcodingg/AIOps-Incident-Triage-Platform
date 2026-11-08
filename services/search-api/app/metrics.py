"""Request metrics for canary analysis.

Names and labels follow the OpenTelemetry semantic conventions, so the analysis
queries keep working when week 4 swaps this for the OTel SDK.
"""

from __future__ import annotations

import time

from fastapi import Request
from prometheus_client import Histogram

REQUEST_DURATION = Histogram(
    "http_server_request_duration_seconds",
    "Duration of API requests served by search-api.",
    ["http_request_method", "http_route", "http_response_status_code"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1, 2.5),
)


async def track_request(request: Request, call_next):
    # The API is /search only. Probes and /metrics would drown out real traffic.
    if request.url.path != "/search":
        return await call_next(request)
    start = time.perf_counter()
    status = 500  # an exception in the handler escapes as a 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        REQUEST_DURATION.labels(request.method, "/search", str(status)).observe(time.perf_counter() - start)
