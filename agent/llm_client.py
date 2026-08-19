"""Claude tool-use loop: turns one user message into a grounded answer by letting
the model call its tools one or more times before responding."""

import json

from agent.prompts import build_system_prompt
from agent.tools import SnowflakeUnavailableError, SqlValidationError, run_query
from retrieval.search import list_columns_for_concept, search_concepts

MODEL = "claude-sonnet-5"
# One "round" is one model call. A task needing N tool calls needs N+1 rounds
# total - N to gather data, plus one more for the model to read the last
# tool's results and actually write the answer. Found via the eval system:
# a task needing exactly the old limit (4) of tool calls could never finish,
# since the final answering round was never budgeted for.
MAX_TOOL_ROUNDS = 6

QUERY_TOOL_DEFINITION = {
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

SEARCH_CONCEPTS_TOOL_DEFINITION = {
    "name": "search_census_concepts",
    "description": (
        "Stage 1 of finding a column for a concept NOT in your curated headline list: search for "
        "the right high-level concept (e.g. 'Travel Time To Work', 'Sex By Age') given a "
        "plain-English description of what you want to measure. Returns several ranked candidate "
        "concepts, each with its TABLE_NUMBER - you must judge which one actually matches the "
        "question (don't blindly take the first result) before calling list_concept_columns on it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Plain-English description of the concept you're looking for."},
        },
        "required": ["query"],
    },
}

LIST_CONCEPT_COLUMNS_TOOL_DEFINITION = {
    "name": "list_concept_columns",
    "description": (
        "Stage 2: once you've picked a TABLE_NUMBER from search_census_concepts, list every column "
        "it contains (e.g. every specific age/sex bracket, or every specific time-of-commute "
        "bracket) so you can pick the exact one matching the question, then use its column code in "
        "query_census_data."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "table_number": {"type": "string", "description": "e.g. 'B01001' - from search_census_concepts."},
            "year": {"type": "string", "description": "'2019' or '2020'."},
        },
        "required": ["table_number", "year"],
    },
}

TOOL_DEFINITIONS = [QUERY_TOOL_DEFINITION, SEARCH_CONCEPTS_TOOL_DEFINITION, LIST_CONCEPT_COLUMNS_TOOL_DEFINITION]


class AgentError(Exception):
    """Raised for failures that should surface as a graceful chat message."""


def _execute_query_tool(conn, tool_input: dict) -> str:
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


def _execute_search_concepts_tool(tool_input: dict) -> str:
    query = tool_input.get("query", "")
    if not query:
        return json.dumps({"error": "No search query provided."})
    results = search_concepts(query)
    return json.dumps(
        [{"table_number": r.table_number, "table_title": r.table_title, "description": r.text} for r in results]
    )


def _execute_list_concept_columns_tool(tool_input: dict) -> str:
    table_number = tool_input.get("table_number", "")
    year = tool_input.get("year", "")
    if not table_number or not year:
        return json.dumps({"error": "table_number and year are both required."})
    results = list_columns_for_concept(table_number, year=year)
    if not results:
        return json.dumps({"error": f"No columns found for table_number={table_number!r} year={year!r}."})
    return json.dumps(
        [{"column": r.table_id, "physical_table": r.physical_table, "description": r.text} for r in results]
    )


def _execute_tool(conn, name: str, tool_input: dict) -> str:
    if name == "query_census_data":
        return _execute_query_tool(conn, tool_input)
    if name == "search_census_concepts":
        return _execute_search_concepts_tool(tool_input)
    if name == "list_concept_columns":
        return _execute_list_concept_columns_tool(tool_input)
    return json.dumps({"error": f"Unknown tool: {name}"})


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
            tools=TOOL_DEFINITIONS,
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
                output = _execute_tool(conn, block.name, block.input)
            except AgentError as exc:
                return str(exc), messages
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
        messages.append({"role": "user", "content": tool_results})

    return (
        "I wasn't able to find a reliable answer after a few attempts - could you rephrase or "
        "narrow your question?",
        messages,
    )
