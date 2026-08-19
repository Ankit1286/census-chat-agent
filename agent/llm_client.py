"""Claude tool-use loop: turns one user message into a grounded answer by letting
the model call query_census_data zero or more times before responding."""

import json

from agent.prompts import build_system_prompt
from agent.tools import SnowflakeUnavailableError, SqlValidationError, run_query

MODEL = "claude-sonnet-5"
# One "round" is one model call. A task needing N tool calls needs N+1 rounds
# total - N to gather data, plus one more for the model to read the last
# tool's results and actually write the answer. Found via the eval system:
# a task needing exactly the old limit (4) of tool calls could never finish,
# since the final answering round was never budgeted for.
MAX_TOOL_ROUNDS = 6

TOOL_DEFINITION = {
    "name": "query_census_data",
    "description": (
        "Run a single read-only SELECT query against the US Open Census Data tables in "
        "Snowflake and return the results. Always include a LIMIT."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "A single SELECT statement."},
            "reasoning": {
                "type": "string",
                "description": "One sentence on what this query is trying to find and why.",
            },
        },
        "required": ["sql", "reasoning"],
    },
}


class AgentError(Exception):
    """Raised for failures that should surface as a graceful chat message."""


def _execute_tool(conn, tool_input: dict) -> str:
    sql = tool_input.get("sql", "")
    try:
        result = run_query(conn, sql)
    except SqlValidationError as exc:
        return json.dumps({"error": f"Query rejected: {exc}"})
    except SnowflakeUnavailableError as exc:
        raise AgentError(
            "I'm having trouble connecting to the Census data right now. Please try again in a moment."
        ) from exc
    except Exception as exc:  # bad/unsupported SQL, etc. - let the model see and retry
        return json.dumps({"error": f"Query failed: {exc}"})

    if not result.rows:
        return json.dumps({"columns": result.columns, "rows": [], "note": "No matching rows found."})

    return json.dumps(
        {"columns": result.columns, "rows": [list(r) for r in result.rows], "truncated": result.truncated},
        default=str,
    )


def run_agent_turn(client, conn, messages: list[dict]) -> tuple[str, list[dict]]:
    """Run one user turn through the Claude tool-use loop.

    `messages` is the full running conversation in Anthropic message format
    (already includes the new user message). Returns (final_text, updated_messages).
    """
    system = build_system_prompt()

    for _ in range(MAX_TOOL_ROUNDS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=system,
            tools=[TOOL_DEFINITION],
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})

        # Judge by whether there are tool_use blocks to run, not by stop_reason alone.
        # The model can get cut off mid-turn (stop_reason="max_tokens") while trying to
        # fire several tool calls at once, leaving some well-formed tool_use blocks
        # alongside one truncated/empty one. Executing every tool_use block found -
        # regardless of the exact stop_reason - means the good calls still run instead
        # of the whole turn being discarded. _execute_tool already turns a missing/empty
        # "sql" into a graceful rejection message rather than crashing, so a truncated
        # block degrades safely instead of losing the other, valid results.
        tool_use_blocks = [b for b in response.content if getattr(b, "type", None) == "tool_use"]

        if not tool_use_blocks:
            text = "".join(block.text for block in response.content if block.type == "text")
            if not text:
                # Cut off before producing any answer text (e.g. hit max_tokens mid-"thinking"
                # block) - surface this as a graceful failure rather than an empty chat bubble.
                return (
                    "I ran into trouble putting together a full answer to that one - could you "
                    "try rephrasing or asking a narrower question?",
                    messages,
                )
            return text, messages

        tool_results = []
        for block in tool_use_blocks:
            try:
                output = _execute_tool(conn, block.input)
            except AgentError as exc:
                return str(exc), messages
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
        messages.append({"role": "user", "content": tool_results})

    return (
        "I wasn't able to find a reliable answer after a few attempts - could you rephrase or "
        "narrow your question?",
        messages,
    )
