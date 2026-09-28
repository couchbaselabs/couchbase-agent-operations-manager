"""
Demonstrates the LLM response cache: the same prompt sent twice - and a
paraphrase of it - should turn a billed `miss` into a free `hit_exact` or
`hit_semantic`. See "Why route model calls through the SDK too" on the
appliance's Tools -> Developer SDK page for the cost/latency math behind
why this matters at scale.

Run with:
    AOM_BASE_URL=https://localhost:8090 AOM_API_KEY=demo-admin-4c56 python examples/llm_caching.py
"""
import os

from aom_sdk import AOMClient


def show(label: str, answer: dict) -> None:
    cache = answer["cache"]
    usage = answer["usage"]
    print(
        f"{label:<38} cache={cache['status']:<12} "
        f"tokens_spent={usage['total_tokens']:<6} "
        f"cost_usd={answer['cost_usd']:.5f} "
        f"latency_ms={answer['latency_ms']}"
    )


def main() -> None:
    client = AOMClient(
        base_url=os.environ.get("AOM_BASE_URL", "https://localhost:8090"),
        api_key=os.environ.get("AOM_API_KEY"),
        # The bundled appliance serves HTTPS with a self-signed certificate
        # by default (see quickstart.py) - AOM_VERIFY_SSL=true once you've
        # installed a real one.
        verify=os.environ.get("AOM_VERIFY_SSL", "false").lower() == "true",
    )

    prompt = "What is our policy on refunds for orders older than 30 days?"
    paraphrase = "Can a customer still get a refund if the order is more than a month old?"

    show("First call (expect miss)", client.complete(prompt))
    show("Exact repeat (expect hit_exact)", client.complete(prompt))
    show("Paraphrase (expect hit_semantic)", client.complete(paraphrase))
    show("Forced bypass (expect miss)", client.complete(prompt, bypass_cache=True))


if __name__ == "__main__":
    main()
