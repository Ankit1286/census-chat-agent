"""A small, clearly-advisory LLM-as-judge check.

Used only for the handful of cases where a keyword match genuinely can't
capture what "good" means (e.g. "did it explain *why* a median can't be
averaged, not just assert that it can't"). This layer is inherently less
reliable than eval/checks.py's deterministic checks - an LLM judging an LLM
is not ground truth - so results from this module are reported separately in
the scorecard and never count toward the authoritative pass/fail total.

Mirrors the pattern in agent/guardrails.py's classify_message: a small model,
a tight token budget, and fail-open (treated as "skip", not "fail") on error.
"""

from anthropic import Anthropic

from eval.checks import Check, CheckResult, TurnResult

JUDGE_MODEL = "claude-haiku-4-5-20251001"

JUDGE_PROMPT = """You are grading one chatbot response against a single rubric question.
Answer with exactly one word first (PASS or FAIL), then a hyphen, then a short reason
(under 20 words). Example: "PASS - explains the weighting and labels it an approximation"

Rubric: {rubric}

Chatbot response:
---
{answer}
---

Your grade:"""


def judge_check(rubric: str, client: Anthropic) -> Check:
    def check(result: TurnResult) -> CheckResult:
        try:
            response = client.messages.create(
                model=JUDGE_MODEL,
                max_tokens=60,
                messages=[
                    {
                        "role": "user",
                        "content": JUDGE_PROMPT.format(rubric=rubric, answer=result.answer_text),
                    }
                ],
            )
            text = "".join(b.text for b in response.content if b.type == "text").strip()
            passed = text.upper().startswith("PASS")
            return CheckResult(passed, text or "(empty judge response)")
        except Exception as exc:
            return CheckResult(True, f"judge unavailable, skipped ({exc})")

    return check
