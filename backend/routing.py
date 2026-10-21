"""Where a finished invoice goes (1.4.4): run status, outcome, and whether a human must look.

Pure function, no I/O. The decision is already made by authorize(); this only turns it into workflow:

  readers could not resolve the invoice       needs_review  unreadable        review_required
  allow                                       completed     payment_proposed  no
  needs_approval (over_threshold)             needs_review  needs_approval    no: the approvals queue (2.2) owns it
  deny, a rule in REVIEW_RULES fired          completed     denied            YES
  deny, only routine rules fired              completed     denied            no (duplicate is logged)

review_required is a flag, not a status, so the state machine stays small and the console can filter on it later.
It looks at EVERY rule that fired, not just the first: an invoice that is blocked_vendor AND iban_mismatch must
still reach a human, because iban_mismatch is the likeliest trace of an injection or a diverted payment.

A reader's soft IBAN flag (one reader failed the checksum, the other won) is recorded and never routes.
An IBAN that no reader can produce with a valid checksum is a reader problem: unreadable, to a human.
Whether an IBAN is the vendor's current account is policy (the iban_mismatch rule), not decided here.
"""
from dataclasses import dataclass

from backend.authorize import ALLOW, DENY, NEEDS_APPROVAL, Decision
from backend.readers import Reconciled

REVIEW_RULES = frozenset({
    "bank_changed_recently",  # the SDS says denied pending review
    "iban_mismatch",          # the likeliest trace of an injection or a diverted payment
    "default_deny",           # policy could not evaluate (for example an unknown vendor): never fail silently
})
LOG_ONLY_RULES = frozenset({"duplicate_invoice"})  # denied without review, but worth a log line

OUTCOME_UNREADABLE = "unreadable"
OUTCOME_PROPOSED = "payment_proposed"
OUTCOME_DENIED = "denied"
OUTCOME_NEEDS_APPROVAL = "needs_approval"


@dataclass(frozen=True)
class Routing:
    run_status: str               # completed or needs_review
    outcome: str
    review_required: bool
    log_rules: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()  # why a human must look, in plain words


def route_unreadable(reconciled: Reconciled) -> Routing:
    return Routing("needs_review", OUTCOME_UNREADABLE, True, reasons=tuple(reconciled.problems))


def route_decision(decision: Decision) -> Routing:
    fired = set(decision.fired_rules) | {decision.rule_id}
    if decision.verdict == ALLOW:
        return Routing("completed", OUTCOME_PROPOSED, False)
    if decision.verdict == NEEDS_APPROVAL:
        return Routing("needs_review", OUTCOME_NEEDS_APPROVAL, False)
    if decision.verdict == DENY:
        review = sorted(fired & REVIEW_RULES)
        return Routing("completed", OUTCOME_DENIED, bool(review), tuple(sorted(fired & LOG_ONLY_RULES)),
                       tuple(f"{r} fired" for r in review))
    # Not a verdict we know: treat it as a denial that a human must see.
    return Routing("completed", OUTCOME_DENIED, True, reasons=(f"unknown verdict {decision.verdict!r}",))
