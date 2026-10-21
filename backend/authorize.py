"""TEMPORARY STUB. Delete this file in WBS 2.2, when the Cedar policy-service replaces it.

Policy must not live in the backend. This module exists only so the pipeline can be written against the final
contract now: authorize(action, facts) -> Decision. The graph never reads a rule directly; in 2.2 this function
becomes an HTTP call to the policy service and nothing in the graph changes.

Rules (5):  blocked_vendor, duplicate_invoice, iban_mismatch, bank_changed_recently  -> deny
            over_threshold                                                        -> needs_approval
Precedence: deny > needs_approval > allow. Deny by default: an unknown action, unknown vendor, missing or
badly typed fact, or any exception gives a deny with rule_id "default_deny". Allow needs every fact present
and no rule firing.
"""
import uuid
from dataclasses import dataclass
from decimal import Decimal

POLICY_VERSION = "stub-0.1-TEMPORARY"
APPROVAL_THRESHOLD = Decimal("10000.00")  # strictly above this needs a human
RECENT_CHANGE_DAYS = 30                   # bank details changed this many days ago or fewer: deny

ALLOW, DENY, NEEDS_APPROVAL = "allow", "deny", "needs_approval"
ACTION_PROPOSE_PAYMENT = "propose_payment"

# fact name -> type check
_REQUIRED = {
    "vendor_status": lambda v: v in ("active", "blocked"),
    "amount": lambda v: isinstance(v, (Decimal, int)) and not isinstance(v, bool),
    "iban_matches_current": lambda v: isinstance(v, bool),
    "days_since_bank_change": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "duplicate": lambda v: isinstance(v, bool),
}


@dataclass(frozen=True)
class Decision:
    verdict: str
    rule_id: str
    reason: str
    decision_id: str
    policy_version: str
    fired_rules: tuple[str, ...] = ()  # every rule that fired, deny first; rule_id is the first of them


def _decision(verdict: str, rule_id: str, reason: str, fired_rules: tuple[str, ...] = ()) -> Decision:
    return Decision(verdict, rule_id, reason, str(uuid.uuid4()), POLICY_VERSION, fired_rules)


def _fired(f: dict) -> list[tuple[str, str, str]]:
    """(rule_id, verdict, reason) for every rule that fires, in precedence order."""
    fired = []
    if f["vendor_status"] == "blocked":
        fired.append(("blocked_vendor", DENY, "vendor is blocked"))
    if f["duplicate"]:
        fired.append(("duplicate_invoice", DENY, "this vendor and invoice number were already paid"))
    if not f["iban_matches_current"]:
        fired.append(("iban_mismatch", DENY, "invoice IBAN is not the vendor's current account"))
    if f["days_since_bank_change"] <= RECENT_CHANGE_DAYS:
        fired.append(("bank_changed_recently", DENY,
                      f"vendor bank details changed {f['days_since_bank_change']} days ago"))
    if f["amount"] > APPROVAL_THRESHOLD:
        fired.append(("over_threshold", NEEDS_APPROVAL, f"amount {f['amount']} is above {APPROVAL_THRESHOLD}"))
    return fired


def authorize(action: str, facts: dict) -> Decision:
    try:
        if action != ACTION_PROPOSE_PAYMENT:
            return _decision(DENY, "default_deny", f"unknown action {action!r}")
        if not isinstance(facts, dict) or facts.get("vendor_exists") is not True:
            return _decision(DENY, "default_deny", "unknown vendor")
        missing = [k for k in _REQUIRED if facts.get(k) is None]
        if missing:
            return _decision(DENY, "default_deny", f"missing facts: {', '.join(missing)}")
        bad = [k for k, ok in _REQUIRED.items() if not ok(facts[k])]
        if bad:
            return _decision(DENY, "default_deny", f"invalid facts: {', '.join(bad)}")
        fired = _fired(facts)
        every = tuple(r[0] for r in fired)
        for verdict in (DENY, NEEDS_APPROVAL):  # precedence: deny > needs_approval > allow
            hits = [r for r in fired if r[1] == verdict]
            if hits:
                reason = hits[0][2] + (f" (also: {', '.join(r[0] for r in hits[1:])})" if len(hits) > 1 else "")
                return _decision(verdict, hits[0][0], reason, every)
        return _decision(ALLOW, "allow_clean_payment", "no rule fired and every required fact is present", every)
    except Exception as e:  # noqa: BLE001  deny by default: nothing may escape into the pipeline
        return _decision(DENY, "default_deny", f"policy error: {type(e).__name__}")
