"""Eval runner: replays every case in eval/cases.py against the REAL Claude +
Snowflake stack (same connection path app.py uses), scores each turn with the
deterministic checks in eval/checks.py, and prints + writes a scorecard.

Not run in CI: costs real API/warehouse usage and needs live secrets in
.streamlit/secrets.toml. Run manually before a submission, or after changing
a prompt/model, to get an instant answer to "did that help or hurt" instead
of re-reading transcripts by hand.

Usage:
    python -m eval.run_eval
"""

import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from anthropic import Anthropic

from agent.guardrails import OFF_TOPIC_REPLY, UNSAFE_REPLY, classify_message
from agent.llm_client import run_agent_turn
from agent.tools import get_connection
from eval.cases import EVAL_CASES
from eval.checks import CheckResult, TurnResult
from eval.judge import judge_check

REPO_ROOT = Path(__file__).resolve().parents[1]


def _attach_judge_checks(client: Anthropic) -> None:
    """Judge checks need a live client, so they're attached at runtime rather
    than at case-definition time - see eval/judge.py for why these are
    advisory-only and reported separately from the deterministic checks."""
    by_id = {c.id: c for c in EVAL_CASES}
    by_id["austin_not_a_county"].turns[0].advisory_checks.append(
        judge_check(
            "Does the response explain WHY it can't give an exact figure for Austin "
            "(not just that it can't), and offer Travis County as a clearly labeled approximation?",
            client,
        )
    )
    by_id["texas_median_income_aggregation_trap"].turns[0].advisory_checks.append(
        judge_check(
            "Does the response explain WHY medians can't be summed/averaged across block groups "
            "(not just assert that they can't)?",
            client,
        )
    )


def _extract_tool_calls(messages: list[dict], start_index: int) -> list[dict]:
    calls = []
    for m in messages[start_index:]:
        if m.get("role") != "assistant":
            continue
        for block in m["content"]:
            if getattr(block, "type", None) == "tool_use":
                calls.append({"sql": block.input.get("sql"), "reasoning": block.input.get("reasoning")})
    return calls


def run_turn(client: Anthropic, conn, messages: list[dict], question: str) -> TurnResult:
    start = time.monotonic()
    label = classify_message(client, question)

    if label in ("OFF_TOPIC", "UNSAFE"):
        answer = OFF_TOPIC_REPLY if label == "OFF_TOPIC" else UNSAFE_REPLY
        elapsed = time.monotonic() - start
        # Matches app.py: guardrail-rejected turns are never added to the real
        # conversation history sent to the model.
        return TurnResult(question, label, answer, agent_was_invoked=False, tool_calls=[], latency_seconds=elapsed)

    before = len(messages)
    messages.append({"role": "user", "content": question})
    answer, messages = run_agent_turn(client, conn, messages)
    tool_calls = _extract_tool_calls(messages, before)
    elapsed = time.monotonic() - start
    return TurnResult(question, label, answer, agent_was_invoked=True, tool_calls=tool_calls, latency_seconds=elapsed)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    with open(REPO_ROOT / ".streamlit" / "secrets.toml", "rb") as f:
        secrets = tomllib.load(f)

    client = Anthropic(api_key=secrets["ANTHROPIC_API_KEY"])
    conn = get_connection(secrets)
    _attach_judge_checks(client)

    report_lines = []
    total_checks = 0
    total_passed = 0
    advisory_total = 0
    advisory_passed = 0

    def emit(line: str = "") -> None:
        print(line)
        report_lines.append(line)

    emit(f"# Eval run — {datetime.now(timezone.utc).isoformat()}Z\n")

    for case in EVAL_CASES:
        emit(f"## {case.id}  [{case.category}]")
        emit(f"{case.description}\n")
        messages: list[dict] = []

        for turn in case.turns:
            result = run_turn(client, conn, messages, turn.question)
            emit(f"Q: {turn.question}")
            emit(f"A: {result.answer_text}")
            emit(
                f"(guardrail={result.guardrail_label}, tool_calls={len(result.tool_calls)}, "
                f"latency={result.latency_seconds:.1f}s)"
            )

            for check in turn.checks:
                outcome: CheckResult = check(result)
                total_checks += 1
                total_passed += int(outcome.passed)
                mark = "PASS" if outcome.passed else "FAIL"
                emit(f"  [{mark}] {outcome.detail}")

            for check in turn.advisory_checks:
                outcome = check(result)
                advisory_total += 1
                advisory_passed += int(outcome.passed)
                mark = "PASS" if outcome.passed else "FAIL"
                emit(f"  [ADVISORY {mark}] {outcome.detail}")

            emit()

    emit("---")
    emit(f"Deterministic checks: {total_passed}/{total_checks} passed")
    if advisory_total:
        emit(f"Advisory (LLM judge, not authoritative): {advisory_passed}/{advisory_total} passed")

    results_dir = REPO_ROOT / "eval_results"
    results_dir.mkdir(exist_ok=True)
    out_path = results_dir / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.md"
    out_path.write_text("\n".join(report_lines), encoding="utf-8")
    print(f"\nReport written to {out_path}")

    if total_passed < total_checks:
        sys.exit(1)


if __name__ == "__main__":
    main()
