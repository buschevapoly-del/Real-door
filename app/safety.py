"""Safety guardrails shared across modules.

Document text is untrusted input (CH-SAFETY-001, DATA_USE_AND_SAFETY.md).
Nothing extracted from a PDF -- including any text that looks like an
instruction -- is ever executed, and the system never produces an
eligibility/approval/denial decision (CH-DECISION-001). This module holds
the small set of checks that enforce those two boundaries so every
caller shares one implementation instead of re-deriving it.
"""
import re

INJECTION_PATTERNS = [
    r"ignore (all )?(prior|previous|above) instructions",
    r"reveal the system prompt",
    r"you are now",
    r"disregard (the|your) (rules|instructions|guidelines)",
    r"act as (an?|the)",
]

DECISION_WORDS = {
    "approved",
    "denied",
    "eligible",
    "ineligible",
    "accept",
    "reject",
    "qualify",
    "qualifies",
    "disqualified",
}


def contains_injection_attempt(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(re.search(pattern, lowered) for pattern in INJECTION_PATTERNS)


def strip_decision_language(readiness_status: str) -> str:
    """Defensive check: only the two allowed readiness values may ever leave
    this system in that field. Anything else is a bug, not a valid output."""
    allowed = {"READY_TO_REVIEW", "NEEDS_REVIEW"}
    if readiness_status not in allowed:
        raise ValueError(
            f"Refusing to emit non-allowed readiness_status {readiness_status!r}; "
            f"only {allowed} are permitted (CH-DECISION-001)."
        )
    return readiness_status


DISCLAIMER_NO_DECISION = (
    "RealDoor does not determine eligibility, approval, denial, or priority. "
    "It reports a readiness status and calculations for a human reviewer."
)

DISCLAIMER_NOT_VACANCY_DATA = (
    "The HUD LIHTC property dataset describes projects and units; it is not a "
    "live vacancy, rent, waitlist, or application-status feed (HUD-DATA-001)."
)

DISCLAIMER_60_DAY_CONVENTION = (
    "The 60-day document currency window is a hackathon scoring convention "
    "for this challenge, not a universal LIHTC/HUD rule (RULES_README.md)."
)
