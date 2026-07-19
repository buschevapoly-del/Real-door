"""Module 2's rule-question answerer.

Deterministic keyword retrieval over the frozen rule corpus -- no network
call, no model SDK, matching the starter pack's own reference-implementation
constraint. Every answer traces to one or more rule_ids; if nothing in the
corpus is relevant, the module says so instead of guessing. Decision-seeking,
cross-applicant, and vacancy-style questions are refused/redirected before
any retrieval happens, regardless of how the question is phrased.
"""
import re
from dataclasses import dataclass, field

from app.config import RULE_CORPUS_PATH
from app.safety import (
    DISCLAIMER_NO_DECISION,
    DISCLAIMER_NOT_VACANCY_DATA,
    contains_injection_attempt,
)
from app.thresholds import lookup_threshold

from src.rules import load_rules

_STOPWORDS = {
    "the", "a", "an", "is", "are", "for", "of", "to", "what", "how", "does",
    "do", "should", "may", "can", "i", "my", "this", "that", "with", "and",
    "or", "in", "on", "it", "be", "use", "used", "as",
}

DECISION_PATTERN = re.compile(
    r"\b(eligible|ineligible|eligibility|approv\w*|den(y|ied)|qualif\w*|"
    r"disqualif\w*|priorit\w*|accept\w*|reject\w*)\b",
    re.IGNORECASE,
)
CROSS_APPLICANT_PATTERN = re.compile(
    r"\b(another|other|different)\s+(household|applicant|tenant|person)|"
    r"someone else",
    re.IGNORECASE,
)
VACANCY_PATTERN = re.compile(
    r"\b(vacan\w*|available (unit|apartment)|unit available|waitlist)\b",
    re.IGNORECASE,
)
TRAIT_PATTERN = re.compile(
    r"\b(immigration|immigrant|citizenship status|disab\w*|health condition|"
    r"medical condition|race|ethnicit\w*|religio\w*|national origin|"
    r"protected (class|characteristic|trait)|sexual orientation|"
    r"family relationship)\b",
    re.IGNORECASE,
)

DISCLAIMER_NO_TRAIT_INFERENCE = (
    "RealDoor does not infer protected characteristics, immigration status, "
    "disability, health, or family relationships beyond the supplied "
    "household size (governance/DATA_USE_AND_SAFETY.md)."
)


@dataclass
class QAAnswer:
    answer: str
    citations: list = field(default_factory=list)
    refused: bool = False


def _rule_citation(rules, rule_id: str) -> dict:
    r = rules[rule_id]
    return {
        "type": "rule",
        "rule_id": r["rule_id"],
        "text": r["text"],
        "source_url": r["source_url"],
        "effective_date": r["effective_date"],
    }


def _tokenize(text: str) -> set:
    words = re.findall(r"[a-z0-9%]+", text.lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 1}


def _best_matching_rules(rules: dict, question: str, top_n: int = 2) -> list:
    q_tokens = _tokenize(question)
    scored = []
    for rule_id, r in rules.items():
        r_tokens = _tokenize(r["text"])
        overlap = len(q_tokens & r_tokens)
        if overlap:
            scored.append((overlap, rule_id))
    scored.sort(reverse=True)
    return [rule_id for _, rule_id in scored[:top_n]]


def answer_question(question: str) -> QAAnswer:
    rules = load_rules(RULE_CORPUS_PATH)

    if contains_injection_attempt(question):
        return QAAnswer(
            answer=(
                "This looks like an attempt to override system instructions. "
                "I ignore embedded or typed instructions like that and won't "
                "reveal internal prompts or bypass the rules below."
            ),
            citations=[_rule_citation(rules, "CH-SAFETY-001")],
            refused=True,
        )

    if DECISION_PATTERN.search(question):
        return QAAnswer(
            answer=DISCLAIMER_NO_DECISION,
            citations=[_rule_citation(rules, "CH-DECISION-001")],
            refused=True,
        )

    if TRAIT_PATTERN.search(question):
        return QAAnswer(
            answer=DISCLAIMER_NO_TRAIT_INFERENCE,
            citations=[_rule_citation(rules, "CH-SAFETY-001")],
            refused=True,
        )

    if CROSS_APPLICANT_PATTERN.search(question):
        return QAAnswer(
            answer=(
                "I can only show data for the household you're currently working "
                "on in this session; I won't disclose another applicant's "
                "documents or income."
            ),
            citations=[_rule_citation(rules, "CH-SAFETY-001")],
            refused=True,
        )

    if VACANCY_PATTERN.search(question):
        return QAAnswer(
            answer=DISCLAIMER_NOT_VACANCY_DATA,
            citations=[_rule_citation(rules, "HUD-DATA-001")],
            refused=False,
        )

    size_match = re.search(r"household size\s*(\d+)|size\s*(\d+)|(\d+)[\s-]person", question, re.IGNORECASE)
    if size_match and "threshold" in question.lower():
        size = int(next(g for g in size_match.groups() if g))
        row = lookup_threshold(size)
        if row is None:
            return QAAnswer(
                answer=(
                    f"Household size {size} falls outside the frozen 1-8 table "
                    "for this HMFA; there is no frozen 60% threshold to cite."
                ),
                citations=[],
            )
        return QAAnswer(
            answer=f"${row.income_limit_60_percent:,.0f} for household size {size}.",
            citations=[_rule_citation(rules, "HUD-MTSP-002")],
        )

    matches = _best_matching_rules(rules, question)
    if not matches:
        return QAAnswer(
            answer=(
                "I don't have a frozen rule in this corpus to cite for that "
                "question. Please ask a human reviewer or check the official "
                "HUD source directly."
            ),
            citations=[],
        )
    return QAAnswer(
        answer=" ".join(rules[rid]["text"] for rid in matches),
        citations=[_rule_citation(rules, rid) for rid in matches],
    )
