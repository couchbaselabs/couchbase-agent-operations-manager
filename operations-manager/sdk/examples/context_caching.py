"""
Demonstrates the context cache: caching arbitrary data an agent fetches
from its own data source (a warehouse row, an API response, a catalog
lookup) rather than something an LLM generated. Unlike the LLM cache, this
is exact-key matching only - your own key, your own opaque value - and the
value you get back on a hit is the exact value you stored, not a
paraphrase-tolerant near-match. See "Context Cache" on the appliance's
Tools -> Developer SDK page (and the Context Cache dashboard under LLM
Caching in the left nav) for how this shows up once real agents are
calling it.

Run with:
    AOM_BASE_URL=https://localhost:8090 AOM_API_KEY=demo-admin-4c56 python examples/context_caching.py
"""
import os
import time

from aom_sdk import AOMClient


def fetch_supplier_quote(item_id: str) -> dict:
    """Stands in for a real, slow lookup - a warehouse query, a REST call
    to a supplier API, whatever an agent would otherwise repeat on every
    request for the same item."""
    time.sleep(0.35)
    return {"item_id": item_id, "unit_price": 42.50, "supplier": "Acme Fasteners"}


def main() -> None:
    client = AOMClient(
        base_url=os.environ.get("AOM_BASE_URL", "https://localhost:8090"),
        api_key=os.environ.get("AOM_API_KEY"),
        # The bundled appliance serves HTTPS with a self-signed certificate
        # by default (see quickstart.py) - AOM_VERIFY_SSL=true once you've
        # installed a real one.
        verify=os.environ.get("AOM_VERIFY_SSL", "false").lower() == "true",
    )

    item_id = "sku-77213"

    # -- Option 1: context_get()/context_set(), called explicitly ----------
    # Useful when you want to see the miss/hit verdict yourself, or the
    # fetch and the cache write don't happen in the same place.
    hit = client.context_get(item_id, namespace="procurement-quotes")
    if hit["hit"]:
        print(f"context_get: HIT - {hit['value']} ({hit['latency_ms']}ms)")
    else:
        start = time.time()
        value = fetch_supplier_quote(item_id)
        elapsed_ms = round((time.time() - start) * 1000)
        client.context_set(item_id, value, namespace="procurement-quotes", source_latency_ms=elapsed_ms)
        print(f"context_get: MISS - fetched fresh in {elapsed_ms}ms and cached it")

    # -- Option 2: cached_context(), the get-or-compute pattern most agents
    # actually want in one call. `fetch` only runs on a miss.
    value = client.cached_context(
        item_id,
        lambda: fetch_supplier_quote(item_id),
        namespace="procurement-quotes",
    )
    print(f"cached_context: {value} (this call was instant if the block above already cached it)")


if __name__ == "__main__":
    main()
