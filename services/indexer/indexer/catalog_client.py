from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable, Iterator

PAGE_SIZE = 100

Fetch = Callable[[str], list[dict]]


def http_get_json(url: str, timeout: float = 5.0) -> list[dict]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.load(res)


def fetch_all_products(base_url: str, fetch: Fetch = http_get_json, page_size: int = PAGE_SIZE) -> Iterator[dict]:
    """Page through GET /products until a short page comes back."""
    page = 0
    while True:
        batch = fetch(f"{base_url.rstrip('/')}/products?page={page}&size={page_size}")
        yield from batch
        if len(batch) < page_size:
            return
        page += 1
