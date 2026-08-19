# Reflection

## Development process and key architectural decisions

I built this with Claude Code from a plan we agreed on before writing any code. The core decision was how to ground answers in the data: a hand-built Claude tool-use agent that writes SQL against Snowflake, not RAG and not Cortex Analyst.

RAG doesn't really fit here — Census questions need exact aggregation across rows, and similarity search over text chunks can't compute a SUM. Cortex Analyst was tempting since it's Snowflake's own answer to this exact problem, but it's region-gated on trial accounts (a real risk of losing hours to a region migration), and leaning on a managed black box would show less of the actual engineering the assignment is scoring. So Snowflake is just the warehouse; the reasoning, guardrails, and conversation logic are all mine.

A few other decisions worth calling out:

- **A curated schema description, not a full data dictionary.** `agent/schema_context.py` hand-documents about a dozen commonly-asked concepts directly. The dataset has ~365 distinct concepts total (some physical tables bundle 400+ columns), so hardcoding everything wasn't realistic.
- **Streamlit Community Cloud for hosting** — free, deploys straight from GitHub, nothing to manage.
- **Key-pair auth for the Snowflake connection, not a password.** New trial accounts enforce MFA on password logins, which silently breaks any non-interactive client — including the app itself. Key-pair auth sidesteps that and is Snowflake's own recommended pattern for service accounts anyway.
- **Two layers of guardrails** — a fast, cheap classifier that rejects obviously off-topic or unsafe messages before the real agent runs, plus the main agent's own instructions as a backup if the classifier misses something.

One thing changed shape mid-build and is worth calling out specifically: the "long-tail" lookup, for concepts outside the curated dozen, started as the model writing raw SQL against the metadata table itself. Live testing showed that was genuinely unreliable — a search for "travel time" got crowded out of the results by an unrelated table purely because of row-limit ordering, and the model had no real way to guess which internal column number held a specific detail like "male, under 5 years old." I rebuilt it as a proper two-stage retrieval system (`retrieval/`): hybrid keyword+semantic search to find the right *concept* first, then a plain, deterministic listing of that concept's columns once it's chosen. It's its own small package now, with its own tests, and it fixed both failures reliably.

## What I'd improve or do differently with more time

- **Real token streaming.** The answer renders in one shot after a spinner right now; interleaving streaming with the tool-use round-trips would feel more responsive on longer answers.
- **Run the eval suite multiple times, not once.** I found two intermittent bugs this session purely because I happened to re-run something — a guardrail inconsistency and a round-budget issue that only showed up on a second pass. There are probably others I haven't caught yet for the same reason.
- **Broader coverage of the long-tail retrieval system.** I've only directly verified it on a handful of concepts (veterans, commute time, sex-by-age breakdowns) out of ~365 possible ones. That's a spot check, not real coverage.
- **Adversarial/red-team testing.** One prompt-injection phrasing was tried and blocked. Someone trying several bypass strategies deliberately hasn't been simulated.
- **Connection pooling.** The app caches one Snowflake connection per session, fine for the traffic this demo will see, but it wouldn't hold up under real concurrent load.
- **Conversation length management.** History just grows with a hard 40-message cap. A real deployment would want to summarize or trim older turns.
- **Basic observability.** No logging of questions, generated SQL, or latency per turn right now — for an actual handoff I'd want that so a bad answer could be debugged after the fact.

## Edge cases and failure modes identified but not fully addressed

- **Prompt injection through tool results, not just user messages.** If the dataset itself ever contained adversarial text, it would reach the model as a tool result unfiltered. Low risk since Marketplace data isn't user-writable, but the guardrails only look at what the user typed, not at what comes back from Snowflake.
- **Mixing data vintages in one answer.** The dataset has 2019 and 2020 ACS estimates plus a separate 2020 Redistricting table with a different methodology entirely. The model is told to state which year it used, but nothing actually stops it from silently combining a 2019 figure with a 2020 one across two calls in the same answer.
- **No stemming in the retrieval system.** A plain plural/singular mismatch (searching "males" against text that says "Male") can weaken the keyword half of the search. It held up fine in testing because concept-level search doesn't usually hinge on getting that one word exactly right, but it's a real gap, not a solved one.
- **Non-English or malformed input** was never explicitly tested — empty strings, very long input, non-UTF8 text.
- **No load testing at all**, on an app that's now genuinely public.

## How I approached testing, and what I'd add

Four layers, each catching something the others structurally can't:

1. **Unit tests** for the pure logic — SQL safety validation, guardrail fail-open behavior. No AI, no database, just checking our own code does what it should.
2. **Control-flow tests** that simulate the model with a scripted fake, so the tool-use loop's handling of retries, errors, and edge cases can be checked deterministically and for free.
3. **A manual smoke test** against the real Claude + Snowflake stack — this is how the first real bugs turned up (case-sensitive column names, an empty-answer bug, an MFA login wall), none of which the mocked tests could have found.
4. **An automated eval system** that replays the same kind of real questions but scores them automatically instead of me reading transcripts — mostly plain deterministic checks (does the answer contain the right number, was the database actually queried, did the guardrail classify it correctly), plus a small advisory LLM-judge layer for the couple of things a keyword check can't capture.

That eval system earned its keep. Across the session it — plus some direct live tracing — found six real bugs: a guardrail that inconsistently rejected valid borderline questions, an off-by-one in the tool-call round budget that made certain questions literally unable to finish, a batch of tool calls getting thrown away because one of them got cut off mid-generation, silent truncation on broad queries, and the two retrieval failures that led to the two-stage rebuild described above. Every one of them has a regression test now.

What I'd add: multiple trials per eval case to get a real pass rate instead of a single snapshot, since I already know some of these bugs are intermittent; a wider sample of long-tail concepts instead of the handful I've checked; deliberate adversarial cases; and running the eval directly against the deployed app instead of a local connection, since the two environments aren't guaranteed to behave identically.
