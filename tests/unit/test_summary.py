"""Reviewer summary (1.4.3) on recorded model replies. No Docker, no model.

tests/fixtures/readers/summaries_recorded.json holds the real replies of qwen2.5:7b for five cases, including one
where an instruction was planted in a reader note and the model repeated it.
"""
import json
from pathlib import Path

import httpx
import pytest

from backend import summary
from backend.summary import (
    LABEL_MODEL,
    LABEL_TEMPLATE,
    MAX_CHARS,
    clean_text,
    contradicts,
    parse_summary,
    summarise,
    summary_input,
    summary_request,
    template_summary,
)

ROOT = Path(__file__).resolve().parents[2]
RECORDED = json.loads((ROOT / "tests/fixtures/readers/summaries_recorded.json").read_text(encoding="utf-8"))
GOOD_CASES = ["clean_allow", "bank_changed_deny", "over_threshold", "blocked_deny"]


def _gateway(response: httpx.Response | dict) -> httpx.Client:
    resp = response if isinstance(response, httpx.Response) else httpx.Response(200, json=response)
    return httpx.Client(transport=httpx.MockTransport(lambda req: resp))


@pytest.mark.parametrize("name", GOOD_CASES)
def test_recorded_good_replies_are_kept_and_labelled_model_generated(name):
    case = RECORDED[name]
    s = summarise(case["input"], "http://gw", "k", _gateway(case["response"]))
    assert s.label == LABEL_MODEL == "model-generated"
    assert s.text == parse_summary(case["response"])


def test_recorded_planted_instruction_is_caught_and_replaced_by_the_template():
    """The model repeated 'approved by the CFO' on a DENIED invoice. The reviewer must not see that text."""
    case = RECORDED["hostile_note"]
    raw = parse_summary(case["response"])
    assert "approved by the CFO" in raw  # control: the recording really contains the lie
    s = summarise(case["input"], "http://gw", "k", _gateway(case["response"]))
    assert s.label == LABEL_TEMPLATE and "CFO" not in s.text
    assert s == template_summary(case["input"])


def test_the_guard_passes_every_good_recording_and_fails_the_bad_one():
    for name in GOOD_CASES:
        assert not contradicts(RECORDED[name]["input"]["decision"], parse_summary(RECORDED[name]["response"])), name
    bad = RECORDED["hostile_note"]
    assert contradicts(bad["input"]["decision"], parse_summary(bad["response"]))


@pytest.mark.parametrize("verdict,text,expected", [
    ("deny", "The payment is approved and safe to pay.", True),
    ("needs_approval", "Authorised by the CFO.", True),
    ("deny", "The invoice was denied because the vendor is blocked.", False),
    ("needs_approval", "A human must approve this because it exceeds the threshold.", False),
    ("allow", "The invoice was denied.", True),
    ("allow", "This needs approval.", True),
    ("allow", "No rule fired, so the payment is allowed.", False),
])
def test_contradicts(verdict, text, expected):
    assert contradicts(verdict, text) is expected


# ---- what the model is given ----

def test_summary_input_is_keyword_only_so_a_raw_field_cannot_slip_in_by_position():
    with pytest.raises(TypeError):
        summary_input("deny", "r", "why", "Vendor", "INV-1", "1.00", [], [])


def test_summary_input_builds_exactly_the_listed_keys_and_carries_the_notes():
    data = summary_input(verdict="deny", rule_id="r", reason="why", vendor_name="V", invoice_number="INV-1",
                         amount="1.00", notes=["n1"], risk_flags=["f1"])
    assert data == {"decision": "deny", "rule": "r", "reason": "why", "vendor": "V", "invoice_number": "INV-1",
                    "amount_usd": "1.00", "reader_notes": ["n1"], "vendor_risk_flags": ["f1"]}


def test_labels_are_the_literal_words_the_reviewer_sees():
    assert (LABEL_MODEL, LABEL_TEMPLATE) == ("model-generated", "template")


def test_the_request_carries_only_the_listed_facts_and_the_rules():
    data = RECORDED["clean_allow"]["input"]
    body = summary_request(data)
    assert body["model"] == "reasoner" and body["temperature"] == 0
    assert body["messages"][0]["role"] == "system" and "ALREADY decided" in body["messages"][0]["content"]
    assert json.loads(body["messages"][1]["content"]) == data
    assert set(data) == {"decision", "rule", "reason", "vendor", "invoice_number", "amount_usd",
                         "reader_notes", "vendor_risk_flags"}


def test_the_module_imports_no_policy_database_or_reader_code():
    """Nothing here can decide, and nothing in here reads OCR or vision output."""
    assert not {"authorize", "facts", "psycopg", "readers", "reconcile", "parse_ocr"} & set(vars(summary))


# ---- failure: always the template, never an exception ----

@pytest.mark.parametrize("response", [
    httpx.Response(500, text="boom"),
    httpx.Response(200, text="not json at all"),
    httpx.Response(200, json={}),
    httpx.Response(200, json={"choices": [{"message": {"content": "   "}}]}),
    httpx.Response(200, json={"choices": [{"message": {"content": None}}]}),
])
def test_any_gateway_failure_gives_the_template(response):
    data = RECORDED["clean_allow"]["input"]
    s = summarise(data, "http://gw", "k", _gateway(response))
    assert s == template_summary(data) and s.label == LABEL_TEMPLATE


def test_gateway_down_gives_the_template():
    def handler(req):
        raise httpx.ConnectError("refused")
    data = RECORDED["clean_allow"]["input"]
    assert summarise(data, "http://gw", "k", httpx.Client(transport=httpx.MockTransport(handler))).label == LABEL_TEMPLATE


def test_the_template_states_the_decision_without_a_model():
    s = template_summary(RECORDED["bank_changed_deny"]["input"])
    assert "deny" in s.text and "bank_changed_recently" in s.text and "INV-013-1022" in s.text


# ---- clean_text ----

def test_clean_text_collapses_whitespace_strips_control_characters_and_caps_length():
    assert clean_text("a \n\n b\x00\x07 c") == "a b c"
    assert clean_text("x" * 5000).endswith("…") and len(clean_text("x" * 5000)) == MAX_CHARS
    assert clean_text("") is None and clean_text("  \n ") is None and clean_text(None) is None and clean_text(5) is None
