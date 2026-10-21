"""LiteLLM gateway, no model calls (1.2.3). Needs `docker compose up -d`.

These prove the gateway is up, requires the key, and exposes the two model names. They never touch Ollama,
so they are safe in CI. Real model calls live in test_litellm_live.py (marker live_llm).
"""
import os

import httpx
import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def base():
    return f"http://127.0.0.1:{os.getenv('LITELLM_PORT', '14000')}"


@pytest.fixture
def key():
    k = os.environ["LITELLM_MASTER_KEY"]
    assert k.startswith("sk-"), "LiteLLM needs a master key that starts with sk-"
    return k


def test_gateway_is_alive(base):
    assert httpx.get(f"{base}/health/liveliness", timeout=5).status_code == 200


def test_models_listed_with_the_key(base, key):
    """Allowed case: the control for the denials below."""
    r = httpx.get(f"{base}/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=5)
    assert r.status_code == 200, r.text
    assert {m["id"] for m in r.json()["data"]} == {"vision", "reasoner"}


def test_request_without_a_key_is_denied(base):
    assert httpx.get(f"{base}/v1/models", timeout=5).status_code == 401


def test_request_with_a_wrong_key_is_denied(base):
    r = httpx.get(f"{base}/v1/models", headers={"Authorization": "Bearer sk-not-the-key"}, timeout=5)
    assert r.status_code == 400  # observed on 1.83.14: a wrong key is 400, a missing key is 401


def test_unknown_model_is_refused_before_any_model_runs(base, key):
    r = httpx.post(
        f"{base}/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        timeout=10,
    )
    assert r.status_code == 400, (r.status_code, r.text[:200])  # observed: 400 'Invalid model name'; no cloud model exists
