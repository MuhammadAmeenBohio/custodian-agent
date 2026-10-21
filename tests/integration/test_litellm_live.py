"""Real model calls through the gateway (1.2.3). Local only: `make test-llm`.

Needs the stack up, `make seed` run, and Ollama running with qwen2.5:7b and qwen2.5vl:3b. The first call to each
model can take a minute while Ollama loads it (6 GB of VRAM holds only one of them at a time).
"""
import base64
import json
import os
from pathlib import Path

import httpx
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.live_llm]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def chat():
    base = f"http://127.0.0.1:{os.getenv('LITELLM_PORT', '14000')}"
    headers = {"Authorization": f"Bearer {os.environ['LITELLM_MASTER_KEY']}"}

    def call(model: str, content, **extra) -> httpx.Response:
        body = {"model": model, "temperature": 0, "messages": [{"role": "user", "content": content}], **extra}
        return httpx.post(f"{base}/v1/chat/completions", headers=headers, json=body, timeout=300)

    return call


def test_reasoner_answers_through_the_gateway(chat):
    r = chat("reasoner", "Reply with exactly one word: pong", max_tokens=10)
    assert r.status_code == 200, r.text
    text = r.json()["choices"][0]["message"]["content"]
    assert text.strip(), "empty answer, so the checks below would prove nothing"
    assert "pong" in text.lower()


def test_vision_reads_an_invoice_number_through_the_gateway(chat):
    key = json.loads((REPO_ROOT / "eval" / "answer_key.jsonl").read_text().splitlines()[0])
    png = base64.b64encode((REPO_ROOT / "generated" / f"{key['invoice_id']}.png").read_bytes()).decode()
    content = [
        {"type": "text", "text": "What is the invoice number printed on this invoice? Reply with the number only."},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png}"}},
    ]
    r = chat("vision", content, max_tokens=30)
    assert r.status_code == 200, r.text
    answer = r.json()["choices"][0]["message"]["content"]
    assert key["invoice_number"] in answer, (key["invoice_number"], answer)


def test_identical_request_is_served_from_the_cache(chat):
    first = chat("reasoner", "Reply with exactly one word: cached", max_tokens=10)
    second = chat("reasoner", "Reply with exactly one word: cached", max_tokens=10)
    assert first.status_code == second.status_code == 200
    assert first.json()["choices"][0]["message"]["content"] == second.json()["choices"][0]["message"]["content"]
    assert second.headers.get("x-litellm-cache-key"), dict(second.headers)  # the gateway marks cache hits
