from __future__ import annotations

import logging
import os
import signal
import threading

import redis

from . import core, telemetry
from .catalog_client import fetch_all_products
from .health import HealthState, serve

log = logging.getLogger("indexer")


def initial_index(client: redis.Redis, catalog_url: str, state: HealthState, stop: threading.Event) -> None:
    delay = 1.0
    while not stop.is_set():
        state.beat()
        try:
            count = core.full_reindex(client, fetch_all_products(catalog_url))
            log.info("full reindex done: %d products", count)
            return
        except Exception as exc:  # catalog or redis not up yet: retry with backoff
            log.warning("full reindex failed, retrying in %.0fs: %s", delay, exc)
            stop.wait(delay)
            delay = min(delay * 2, 30.0)


def run() -> None:
    telemetry.configure_logging()
    telemetry.configure()
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    catalog_url = os.getenv("CATALOG_URL", "http://catalog:8080")
    health_port = int(os.getenv("HEALTH_PORT", "8081"))

    client = redis.Redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=2.0)
    state = HealthState()
    telemetry.observe_readiness(lambda: state.ready)
    server = serve(state, health_port)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    initial_index(client, catalog_url, state, stop)
    state.ready = True

    while not stop.is_set():
        state.beat()
        try:
            applied = core.consume_once(client, timeout_seconds=5)
            if applied is not None:
                outcome = "applied" if applied else "dead-lettered"
                telemetry.CHANGES.add(1, {"outcome": outcome})
                log.info("change %s", outcome)
        except redis.RedisError as exc:
            log.warning("redis error, retrying: %s", exc)
            stop.wait(2.0)

    log.info("shutting down")
    server.shutdown()
    telemetry.shutdown()


if __name__ == "__main__":
    run()
