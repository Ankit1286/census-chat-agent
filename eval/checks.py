"""Deterministic, free, repeatable scoring primitives for the eval system.

These never call an LLM - they're plain Python checks against the recorded
result of one conversation turn. This is the authoritative scoring layer;
eval/judge.py is a separate, clearly-advisory layer for the handful of
things a keyword check can't capture.
"""

import re
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class TurnResult:
    question: str
    guardrail_label: str  # "ON_TOPIC" / "OFF_TOPIC" / "UNSAFE"
    answer_text: str
    agent_was_invoked: bool  # did we call run_agent_turn at all (cost-relevant)
    tool_calls: list[dict] = field(default_factory=list)  # {"sql": ..., "reasoning": ...} per call
    latency_seconds: float = 0.0


@dataclass
class CheckResult:
    passed: bool
    detail: str


Check = Callable[[TurnResult], CheckResult]

_NUMBER_RE = re.compile(r"-?\$?\d[\d,]*(?:\.\d+)?")


def _extract_numbers(text: str) -> list[float]:
    numbers = []
    for match in _NUMBER_RE.findall(text):
        cleaned = match.replace(",", "").replace("$", "")
        try:
            numbers.append(float(cleaned))
        except ValueError:
            continue
    return numbers


def contains_number_near(expected: float, tolerance: float = 0.03) -> Check:
    """Passes if any number in the answer is within `tolerance` (relative) of `expected`.

    This is the main grounding-accuracy check: it proves the answer cites a real,
    correct figure rather than a plausible-sounding wrong one.
    """

    def check(result: TurnResult) -> CheckResult:
        candidates = _extract_numbers(result.answer_text)
        for n in candidates:
            if expected == 0:
                if n == 0:
                    return CheckResult(True, f"found {n} matching expected {expected}")
                continue
            if abs(n - expected) / abs(expected) <= tolerance:
                return CheckResult(True, f"found {n:,.0f} within {tolerance:.0%} of expected {expected:,.0f}")
        return CheckResult(
            False,
            f"no number within {tolerance:.0%} of expected {expected:,.0f} - numbers found: {candidates[:8]}",
        )

    return check


def mentions_any(keywords: list[str]) -> Check:
    def check(result: TurnResult) -> CheckResult:
        text = result.answer_text.lower()
        hits = [k for k in keywords if k.lower() in text]
        return CheckResult(bool(hits), f"matched keywords: {hits}" if hits else f"none of {keywords} found")

    return check


def not_mentions_any(keywords: list[str]) -> Check:
    def check(result: TurnResult) -> CheckResult:
        text = result.answer_text.lower()
        hits = [k for k in keywords if k.lower() in text]
        return CheckResult(not hits, f"unexpectedly found: {hits}" if hits else "none of the forbidden terms found")

    return check


def tool_was_called(min_calls: int = 1) -> Check:
    def check(result: TurnResult) -> CheckResult:
        n = len(result.tool_calls)
        return CheckResult(n >= min_calls, f"{n} tool call(s) made (need >= {min_calls})")

    return check


def tool_was_not_called() -> Check:
    def check(result: TurnResult) -> CheckResult:
        n = len(result.tool_calls)
        return CheckResult(n == 0, f"{n} tool call(s) made (expected 0 - proves the fast-fail path skipped the agent)")

    return check


def guardrail_classified_as(label: str) -> Check:
    def check(result: TurnResult) -> CheckResult:
        return CheckResult(
            result.guardrail_label == label,
            f"guardrail said {result.guardrail_label!r} (expected {label!r})",
        )

    return check


def within_latency(seconds: float) -> Check:
    def check(result: TurnResult) -> CheckResult:
        return CheckResult(
            result.latency_seconds <= seconds,
            f"took {result.latency_seconds:.1f}s (budget {seconds:.0f}s)",
        )

    return check


def nonempty_answer() -> Check:
    def check(result: TurnResult) -> CheckResult:
        ok = bool(result.answer_text and result.answer_text.strip())
        return CheckResult(ok, "answer is empty" if not ok else "answer is non-empty")

    return check
