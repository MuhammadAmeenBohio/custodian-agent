"""The invoice pipeline (1.4.4): a LangGraph graph over readers, vendor facts, authorize(), the summary and routing.

    load_invoice -> read_invoice -> reconcile --(problems)--> route_unreadable -> END
                                        |
                                        +--(resolved)--> vendor_facts -> authorize -> summarise -> route -> END

Code decides, the model reads and explains. The models appear in exactly two nodes: read_invoice (the vision
reader) and summarise (the reviewer note). The decision is made in `authorize`, from facts built by code, and the
`route` node reads only that decision. The summary is stored for a human and read by nothing that decides.

Every node is one row in agent.steps, written by the `step` wrapper, so a node cannot forget to persist itself.
Everything outside the graph (object store, Tesseract, gateway, vendor database, clock) arrives through `Deps`, so
tests run the whole graph with fakes.
"""
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, TypedDict
from uuid import UUID

import psycopg
from langgraph.graph import END, START, StateGraph

from backend import store
from backend.authorize import Decision, authorize
from backend.facts import VendorRecord, build_facts
from backend.readers import OcrFields, Reconciled, parse_ocr, reconcile
from backend.routing import Routing, route_decision, route_unreadable
from backend.summary import Summary, summary_input, template_summary
from backend.tracing import start_trace

log = logging.getLogger(__name__)


@dataclass
class Deps:
    load_png: Callable[[str], bytes]                      # object key -> PNG bytes
    ocr: Callable[[bytes], str]                           # PNG -> Tesseract text
    vision: Callable[[bytes], dict | None]                # PNG -> the vision model's fields, or None
    vendor_by_name: Callable[[str], VendorRecord | None]  # vendors_ro lookup
    summarise: Callable[[dict], Summary]                  # summary input -> Summary (never raises)
    today: Callable[[], date] = field(default=date.today)


class State(TypedDict, total=False):
    run_id: UUID
    object_key: str
    png: bytes
    ocr: OcrFields
    vision: dict | None
    reconciled: Reconciled
    vendor: VendorRecord | None
    facts: dict
    decision: Decision
    summary: Summary
    routing: Routing


@dataclass
class StepResult:
    input: dict
    output: dict
    update: dict
    decision_id: str | None = None


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x) for x in v]
    return v


def build_graph(deps: Deps, conn: psycopg.Connection):
    def step(name: str, fn: Callable[[State], StepResult]):
        """Wrap a node: write the step row before and after it; a failure is recorded on the step and re-raised."""
        def node(state: State) -> dict:
            row = store.start_step(conn, state["run_id"], name)
            try:
                result = fn(state)
            except Exception as e:
                store.finish_step(conn, row["step_id"], error=f"{type(e).__name__}: {e}"[:500])
                raise
            store.finish_step(conn, row["step_id"], output=_jsonable(result.output), decision_id=result.decision_id,
                              input=_jsonable(result.input))
            return result.update
        return node

    def load_invoice(s: State) -> StepResult:
        png = deps.load_png(s["object_key"])
        return StepResult({"object_key": s["object_key"]}, {"bytes": len(png)}, {"png": png})

    def read_invoice(s: State) -> StepResult:
        ocr = parse_ocr(deps.ocr(s["png"]))
        vision = deps.vision(s["png"])
        return StepResult({}, {"ocr": ocr.__dict__, "vision": vision}, {"ocr": ocr, "vision": vision})

    def do_reconcile(s: State) -> StepResult:
        r = reconcile(s["ocr"], s["vision"])
        return StepResult({}, {"fields": r.fields, "problems": r.problems, "notes": r.notes}, {"reconciled": r})

    def vendor_facts(s: State) -> StepResult:
        f = s["reconciled"].fields
        vendor = deps.vendor_by_name(f["vendor_name"])
        facts = build_facts(vendor, f["invoice_number"], Decimal(f["amount"]), f["iban"], deps.today())
        return StepResult({"vendor_name": f["vendor_name"]}, {"facts": facts}, {"vendor": vendor, "facts": facts})

    def do_authorize(s: State) -> StepResult:
        d = authorize("propose_payment", s["facts"])
        return StepResult(
            {"action": "propose_payment"},
            {"verdict": d.verdict, "rule_id": d.rule_id, "reason": d.reason, "fired_rules": list(d.fired_rules),
             "policy_version": d.policy_version},
            {"decision": d}, decision_id=d.decision_id)

    def summarise(s: State) -> StepResult:
        data = summarise_input_from(s["decision"], s["reconciled"], s["vendor"])
        try:
            summary = deps.summarise(data)
        except Exception as e:  # noqa: BLE001  the summary is optional; the decision is already made
            log.warning("summary failed: %s", type(e).__name__)
            summary = template_summary(data)
        return StepResult(data, {"text": summary.text, "label": summary.label}, {"summary": summary})

    def route(s: State) -> StepResult:
        routing = route_decision(s["decision"])
        for rule in routing.log_rules:
            log.info("run %s denied by routine rule %s", s["run_id"], rule)
        return StepResult({"verdict": s["decision"].verdict}, _routing_dict(routing), {"routing": routing})

    def unreadable(s: State) -> StepResult:
        routing = route_unreadable(s["reconciled"])
        return StepResult({}, _routing_dict(routing), {"routing": routing})

    g = StateGraph(State)
    for name, fn in [("load_invoice", load_invoice), ("read_invoice", read_invoice), ("reconcile", do_reconcile),
                     ("vendor_facts", vendor_facts), ("authorize", do_authorize), ("summarise", summarise),
                     ("route", route), ("route_unreadable", unreadable)]:
        g.add_node(name, step(name, fn))
    g.add_edge(START, "load_invoice")
    g.add_edge("load_invoice", "read_invoice")
    g.add_edge("read_invoice", "reconcile")
    g.add_conditional_edges("reconcile", lambda s: "route_unreadable" if s["reconciled"].problems else "vendor_facts",
                            ["route_unreadable", "vendor_facts"])
    g.add_edge("vendor_facts", "authorize")
    g.add_edge("authorize", "summarise")
    g.add_edge("summarise", "route")
    g.add_edge("route", END)
    g.add_edge("route_unreadable", END)
    return g.compile()


# Reader notes in plain words for the reviewer summary. Fed raw, the model took "iban: vision value failed the
# checksum" to mean the INVOICE's IBAN was bad, when one reader had simply misread it and the other's was used.
_PLAIN_NOTES = {
    "iban: ocr value failed the checksum": "the text reader misread the IBAN; the vision reader's IBAN was valid and used",
    "iban: vision value failed the checksum": "the vision reader misread the IBAN; the text reader's IBAN was valid and used",
    "iban: single reader": None,  # said by the two notes above, or means one reader returned nothing: not worth a line
    "invoice_number: single reader": "the invoice number was read by only one of the two readers",
    "amount: vision disagrees (OCR used)": "the vision reader's amount differed; the text reader's amount was used",
}


def plain_notes(notes: list[str]) -> list[str]:
    """Known notes in plain words; unknown ones are dropped, so unexpected text never reaches the model."""
    return [text for n in notes if (text := _PLAIN_NOTES.get(n))]


def summarise_input_from(d: Decision, r: Reconciled, vendor: VendorRecord | None) -> dict:
    """What the reviewer-summary model may see. The vendor name is the database's, never the one read off the invoice."""
    return summary_input(
        verdict=d.verdict, rule_id=d.rule_id, reason=d.reason,
        vendor_name=vendor.name if vendor else "unknown vendor",
        invoice_number=r.fields.get("invoice_number", ""), amount=r.fields.get("amount", ""),
        notes=plain_notes(r.notes), risk_flags=vendor.risk_flags if vendor else [])


def _routing_dict(r: Routing) -> dict:
    return {"run_status": r.run_status, "outcome": r.outcome, "review_required": r.review_required,
            "log_rules": list(r.log_rules), "reasons": list(r.reasons)}


def run_pipeline(conn: psycopg.Connection, run_id: UUID, deps: Deps) -> None:
    """Execute one queued run to the end. Never raises: a failure marks the run failed and keeps the reason."""
    run = store.get_run(conn, run_id)
    if run is None or run["status"] != "queued":
        return
    store.set_run_status(conn, run_id, "running")
    trace = start_trace(run_id)  # off unless configured (backend/tracing.py); never raises
    try:
        if trace.trace_id:
            try:
                store.set_trace_id(conn, run_id, trace.trace_id)
            except Exception:  # fail open: a run without a stored trace id is still a good run
                log.warning("run %s: could not store the trace id", run_id, exc_info=True)
        final = build_graph(deps, conn).invoke(
            {"run_id": run_id, "object_key": run["invoice_object_key"]}, config={"callbacks": trace.callbacks})
        routing: Routing = final["routing"]
        flags = list(final["reconciled"].notes)
        store.finish_run(conn, run_id, routing.run_status, routing.outcome, routing.review_required, flags)
    except Exception as e:
        log.exception("run %s failed", run_id)
        store.set_run_status(conn, run_id, "failed", f"{type(e).__name__}: {e}"[:500])
    finally:
        trace.flush()  # after the run is saved, and bounded in time
