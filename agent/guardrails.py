"""Fast pre-check that classifies a user message before the main agent runs.

This is the "fast-fail path": clearly off-topic or unsafe messages are rejected
with a canned reply before spending a full tool-use round-trip. The main
agent's system prompt (agent/prompts.py) carries the same instructions as a
second, independent layer in case this classifier is wrong or unavailable.
"""

from agent.prompts import GUARDRAIL_PROMPT

GUARDRAIL_MODEL = "claude-haiku-4-5-20251001"


def classify_message(client, message: str) -> str:
    """Returns "ON_TOPIC", "OFF_TOPIC", or "UNSAFE".

    Fails open to ON_TOPIC on any classifier error (timeout, API outage, unexpected
    output) so a guardrail-layer outage degrades to "let the main agent's own
    instructions handle it" rather than blocking the whole app.
    """
    try:
        response = client.messages.create(
            model=GUARDRAIL_MODEL,
            max_tokens=10,
            messages=[{"role": "user", "content": GUARDRAIL_PROMPT.format(message=message)}],
        )
        text = "".join(b.text for b in response.content if b.type == "text").strip().upper()
        for label in ("UNSAFE", "OFF_TOPIC", "ON_TOPIC"):
            if label in text:
                return label
        return "ON_TOPIC"
    except Exception:
        return "ON_TOPIC"


OFF_TOPIC_REPLY = (
    "I can only help with questions about US population and demographics from the Census "
    "dataset. Try asking something like \"What was the population of Travis County, TX in 2020?\""
)

UNSAFE_REPLY = (
    "I can't help with that. I'm scoped to answering questions about US population and "
    "demographics from the Census dataset - happy to help with something in that space."
)
