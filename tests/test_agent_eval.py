"""Control-flow tests for the tool-use loop in agent/llm_client.py.

These simulate the model's behavior (direct answer, one tool call, tool
error, runaway tool-calling) with a fake Anthropic client, so the loop logic
itself is exercised deterministically in CI. They are a complement to, not a
replacement for, manually running real natural-language questions against the
live deployed app (see scripts/smoke_test.py) - that's the only way to judge
actual answer quality from the real model.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.llm_client import run_agent_turn
from agent.tools import SnowflakeUnavailableError, SqlValidationError


def _text_block(text):
    return SimpleNamespace(type="text", text=text)


def _tool_block(tool_id, sql):
    return SimpleNamespace(
        type="tool_use", id=tool_id, name="query_census_data", input={"sql": sql, "reasoning": "test"}
    )


def _malformed_tool_block(tool_id):
    """Simulates a tool_use block cut off mid-generation (e.g. hit max_tokens
    while the model was still writing its arguments) - present, but with no
    usable "sql" field."""
    return SimpleNamespace(type="tool_use", id=tool_id, name="query_census_data", input={})


def _response(content, stop_reason):
    return SimpleNamespace(content=content, stop_reason=stop_reason)


def test_direct_text_answer_skips_tool_use():
    client = MagicMock()
    client.messages.create.return_value = _response([_text_block("The population is 42.")], "end_turn")
    conn = MagicMock()

    text, _ = run_agent_turn(client, conn, [{"role": "user", "content": "hi"}])

    assert text == "The population is 42."
    assert client.messages.create.call_count == 1


def test_executes_tool_then_answers(monkeypatch):
    client = MagicMock()
    client.messages.create.side_effect = [
        _response([_tool_block("tool_1", "SELECT population FROM t LIMIT 1")], "tool_use"),
        _response([_text_block("Population is 100.")], "end_turn"),
    ]
    monkeypatch.setattr(
        "agent.llm_client.run_query",
        lambda conn, sql: SimpleNamespace(columns=["POPULATION"], rows=[(100,)], truncated=False),
    )

    conn = MagicMock()
    text, _ = run_agent_turn(client, conn, [{"role": "user", "content": "population?"}])

    assert text == "Population is 100."
    assert client.messages.create.call_count == 2


def test_ambiguous_follow_up_retains_history(monkeypatch):
    """Multi-turn: a second call reuses the growing message list rather than starting fresh."""
    client = MagicMock()
    client.messages.create.return_value = _response([_text_block("Which Washington - the state or a county?")], "end_turn")

    conn = MagicMock()
    messages = [{"role": "user", "content": "What's the population of Washington?"}]
    text, messages = run_agent_turn(client, conn, messages)

    assert "which washington" in text.lower()
    assert len(messages) == 2  # original user turn + assistant clarification


def test_snowflake_unavailable_returns_graceful_message(monkeypatch):
    client = MagicMock()
    client.messages.create.return_value = _response([_tool_block("tool_1", "SELECT 1")], "tool_use")

    def _raise(*_args, **_kwargs):
        raise SnowflakeUnavailableError("no route to host")

    monkeypatch.setattr("agent.llm_client.run_query", _raise)

    conn = MagicMock()
    text, _ = run_agent_turn(client, conn, [{"role": "user", "content": "population?"}])

    assert "trouble connecting" in text.lower()


def test_gives_up_gracefully_after_max_rounds(monkeypatch):
    client = MagicMock()
    client.messages.create.return_value = _response([_tool_block("tool_1", "SELECT 1")], "tool_use")
    monkeypatch.setattr(
        "agent.llm_client.run_query",
        lambda conn, sql: SimpleNamespace(columns=["X"], rows=[(1,)], truncated=False),
    )

    conn = MagicMock()
    text, _ = run_agent_turn(client, conn, [{"role": "user", "content": "??"}])

    assert "rephrase" in text.lower()


def test_empty_final_text_surfaces_graceful_message():
    """Regression test: a response with no text block (e.g. the model got cut off
    mid- "thinking" block before producing any answer text) must not surface as a
    blank chat bubble."""
    client = MagicMock()
    client.messages.create.return_value = _response(
        [SimpleNamespace(type="thinking", thinking="...")], "max_tokens"
    )
    conn = MagicMock()

    text, _ = run_agent_turn(client, conn, [{"role": "user", "content": "??"}])

    assert text
    assert "trouble" in text.lower() or "rephrasing" in text.lower()


def test_partial_tool_calls_still_execute_on_max_tokens_cutoff(monkeypatch):
    """Regression test: if the model gets cut off (stop_reason="max_tokens") while
    firing several tool calls at once, the well-formed ones must still run rather
    than the whole turn being discarded just because stop_reason != "tool_use"."""
    client = MagicMock()
    client.messages.create.side_effect = [
        _response(
            [_tool_block("tool_1", "SELECT population FROM t LIMIT 1"), _malformed_tool_block("tool_2")],
            "max_tokens",
        ),
        _response([_text_block("Population is 100.")], "end_turn"),
    ]
    def fake_run_query(conn, sql):
        if not sql:
            # Mirrors what the real validate_sql() does for an empty/missing query.
            raise SqlValidationError("Only a single SQL statement is allowed.")
        return SimpleNamespace(columns=["POPULATION"], rows=[(100,)], truncated=False)

    monkeypatch.setattr("agent.llm_client.run_query", fake_run_query)

    conn = MagicMock()
    text, messages = run_agent_turn(client, conn, [{"role": "user", "content": "population?"}])

    assert text == "Population is 100."
    # Both tool_use blocks must get a matching tool_result (API contract), even
    # though only one had real SQL to run.
    tool_result_msg = messages[-2]
    assert len(tool_result_msg["content"]) == 2
    assert "rejected" in tool_result_msg["content"][1]["content"].lower()


def test_bad_sql_is_reported_to_model_not_raised(monkeypatch):
    """A validation error should come back as a tool_result the model can react to,
    not blow up the whole turn."""
    client = MagicMock()
    client.messages.create.side_effect = [
        _response([_tool_block("tool_1", "DROP TABLE t")], "tool_use"),
        _response([_text_block("I couldn't run that query safely.")], "end_turn"),
    ]

    conn = MagicMock()
    text, messages = run_agent_turn(client, conn, [{"role": "user", "content": "??"}])

    assert text == "I couldn't run that query safely."
    tool_result_content = messages[-2]["content"][0]["content"]
    assert "rejected" in tool_result_content.lower()
