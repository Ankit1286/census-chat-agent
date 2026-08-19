# Reflection

## Development process and key architectural decisions

I used Claude Code throughout: it drafted the architecture options, I picked
between them (custom tool-use agent vs. Cortex Analyst; Streamlit Community
Cloud vs. a separate frontend/backend), and it wrote the implementation
against an explicit plan we agreed on before any code was written.

The central decision was **grounding via text-to-SQL tool-use against
Snowflake, not RAG and not Cortex Analyst**:
- RAG is built for retrieving relevant *text*; Census questions need exact
  aggregation/filtering/joins over numeric data, which only a real query
  engine can compute correctly. RAG would risk returning plausible-looking
  but wrong numbers.
- Cortex Analyst was tempting (it's the "intended" Snowflake-native answer),
  but it's region-gated on trial accounts, and using a managed black box
  would demonstrate less of the actual agent engineering (prompt design for
  grounding, a tool-use loop, layered guardrails, graceful degradation) that
  the rubric says it's scoring. So Snowflake is used purely as the warehouse;
  the reasoning/guardrail/conversation logic is hand-built.

Secondary decisions: a curated, hand-written schema description
(`agent/schema_context.py`) rather than either hardcoding every column or
giving the model raw `information_schema` access - a middle ground the
assignment's own tips pointed at ("context awareness" without requiring
every column hardcoded). Two independent guardrail layers (a fast
classifier pre-check, plus system-prompt-level refusal instructions) so a
failure in one doesn't remove all protection. SQL validation as a structural
guardrail independent of both the LLM's judgment and the database role's
permissions - defense in depth against a single point of failure.

## Bugs found via live testing (and why they matter)

Once the Snowflake trial account and Marketplace dataset were connected, I
ran `scripts/smoke_test.py` against the real Claude + Snowflake stack. Three
real bugs surfaced that no amount of mocked unit testing would have caught,
which is exactly why the plan called for a manual live smoke test as a
separate layer from the automated suite:

1. **Case-sensitive, digit-leading SQL identifiers.** The dataset's ACS
   columns are mixed-case (`B01003e1`, not `B01003E1`) and table names start
   with a digit (`2020_CBG_B01`). Unquoted, Snowflake either folds the
   column name to uppercase (→ "invalid identifier") or fails to parse the
   table reference entirely. The very first golden question - "total
   population of Travis County" - failed silently (agent gave up after
   exhausting its tool-call budget) until I traced it with a standalone
   script and found the connector error. Fix: added an explicit "SQL syntax"
   section with a worked example query to `agent/schema_context.py`, so the
   model is told to double-quote these identifiers rather than discovering
   it by trial and error.
2. **Empty final answer on a harder question.** "Median household income in
   Austin, TX" (which requires recognizing Austin isn't a county, running two
   queries, and computing a labeled approximation) came back as a blank
   response. Tracing it showed the model's response included a `thinking`
   content block that consumed the entire `max_tokens=1024` budget before
   any answer text was generated, so `stop_reason` was `max_tokens` with no
   text at all - a genuine "queries that are reasonable but the system fails
   silently" case squarely in the failure modes the assignment asks about.
   Fixed two ways: raised `max_tokens` to 4096, and added a defensive check
   in `run_agent_turn` so an empty final answer *always* surfaces as an
   honest "I ran into trouble" message instead of a blank chat bubble,
   regardless of why it happened.
3. **Snowflake's mandatory MFA policy blocks programmatic password auth.**
   New trial accounts require MFA on password logins, which fails outright
   for any non-interactive client - the discovery script, and critically,
   the deployed Streamlit app itself would have hit the same wall in
   production. Fixed by switching the app's Snowflake connection to
   key-pair authentication for the dedicated service user (`CENSUS_APP_USER`),
   which isn't subject to that policy and is Snowflake's own recommended
   pattern for service accounts regardless.

After these three fixes, the full golden-question set (state/county lookup,
multi-turn follow-up, ambiguous geography, city-not-a-county, the
median-aggregation trap, out-of-scope questions, off-topic redirection, and
a prompt-injection attempt) all produced correct, well-caveated answers -
see the transcript captured during development. This was, honestly, a good
reminder that a system prompt built from `DESCRIBE TABLE` output and
documentation review looks complete right up until it meets a real
identifier-quoting quirk or a real token-budget edge case - live testing
against the actual stack found problems the mocked control-flow tests
structurally could not.

## What I'd improve or do differently with more time

- **True token streaming.** The current UX shows a spinner during the SQL
  tool-call phase and renders the final answer in one shot. Interleaving
  real token streaming with tool-use round-trips (stream text, pause to run
  a tool, resume streaming) is more plumbing than I invested in; it would
  meaningfully improve perceived latency on longer answers.
- **Conversation length management.** Message history grows unbounded within
  a session (capped only by a hard 40-message limit). A real deployment
  would want to summarize or truncate older turns so very long sessions
  don't blow the context window or the latency budget.
- **Geography disambiguation.** Right now, disambiguation (e.g. "Washington"
  state vs. county) is handled entirely by system-prompt instruction. With
  more time I'd add a lightweight fuzzy-match lookup against the geography
  dimension table so the agent can proactively list the candidates
  ("Did you mean Washington State, or Washington County in one of these 30
  states?") instead of relying on the model to know that unaided.
- **Observability.** No logging/tracing of tool calls, guardrail decisions,
  or latency per turn. For a real handoff I'd add structured logging (at
  minimum: question, classification, generated SQL, latency, error/success)
  so a customer's team could debug a bad answer after the fact.
- **Cost/rate limiting.** The per-session message cap is a token gesture at
  abuse protection on a public, unauthenticated URL. A real deployment needs
  IP- or session-based rate limiting and a budget alarm on both the
  Anthropic and Snowflake sides.

## Fourth fix: truncated row-listing queries could silently produce wrong answers

Raised directly by a user question during review, not found by the eval or
smoke tests: `agent/tools.py`'s `MAX_ROWS` cap protects against unbounded
queries (cost, latency, and even blowing the model's own context window if a
huge result got fed back into the conversation), but a query that legitimately
needed more rows than the cap - e.g. "population of every county in Texas"
(254 counties) - would previously get silently truncated, with nothing
telling the model the data was incomplete. If the model then computed a total
or presented the partial list as if it were everything, that's a confident,
wrong answer - arguably worse than an honest refusal. `QueryResult` already
carried a `truncated: true/false` flag back to the model, but the system
prompt never told it what that meant.

Fixed two ways: (1) a new system-prompt rule (`agent/prompts.py`, rule 3)
telling the model to prefer server-side aggregation (`SUM`/`COUNT`, which
always returns one complete row no matter how many rows it scanned) over
listing individual rows, and to treat `truncated: true` as a hard signal
that it is not looking at complete data - never compute a total from it,
either rewrite to aggregate or tell the user to narrow the request; (2)
raised `MAX_ROWS` from 200 to 300, comfortably covering a full per-county
listing for any single US state (Texas has the most, at 254) while still
bounding genuinely unbounded queries (nationwide counties or block groups).
Verified live: "What is the population of every county in Texas?" now
correctly declines to dump a silently-partial list, explains the 254-county
limitation, and offers a total, a top-N, or batched retrieval instead. Added
a permanent eval case (`row_cap_truncation_awareness`) guarding this.

## Edge cases and failure modes identified but not fully addressed

- **Prompt injection via tool results.** Data returned from Snowflake (e.g. a
  geography name) is passed back into the model as a tool result; if a
  malicious actor could get injected text into the dataset itself, that text
  reaches the model's context. Low risk here (Marketplace data isn't
  user-writable), but worth naming - the guardrail layers only inspect the
  user's own message, not tool output.
- ~~Very large result sets silently truncated~~ - **fixed**, see "Fourth fix"
  above.
- **Vintage/methodology mixing.** The dataset has 2019 and 2020 ACS 5-year
  estimates plus a separate 2020 Redistricting table with actual decennial
  counts (no sampling error, different table shape entirely). The system
  prompt tells the model to mention which vintage it used, but there's no
  hard guard against it silently mixing, say, a 2019 income figure with a
  2020 population figure across two calls in one answer - this would need
  either explicit year-consistency checking in the tool layer or a stronger
  prompt instruction to always state and stick to one vintage per answer.
- **Only "headline metrics" are curated; the long tail relies on the model's
  own metadata lookup.** `schema_context.py` covers ~12 common concepts
  (population, income, race, education, etc.) explicitly; anything else
  (e.g. veteran status, internet access, commute time) depends on the model
  correctly querying `METADATA_CBG_FIELD_DESCRIPTIONS` itself. This worked
  in every case I tried, but I did not exhaustively test the ~140 ACS table
  families outside the headline list.
- **Concurrent sessions sharing a Snowflake connection.** `app.py` caches one
  connection per Streamlit session via `st.session_state`, not a pool. Under
  concurrent public traffic this could exhaust connections or hold idle
  connections open; a real deployment should use a connection pool with a
  sensible max size and idle timeout.
- **Non-English or malformed input.** Not explicitly tested. The guardrail
  classifier and main agent should handle it reasonably (Claude is
  multilingual), but there's no test coverage confirming graceful behavior
  on, e.g., empty strings, extremely long input, or non-UTF8 content.

## Testing approach and tradeoffs

Four layers now, each chosen for a different kind of confidence:

1. **Unit tests** (`test_tools.py`, `test_guardrails.py`) - deterministic,
   no external dependencies (mocked Snowflake connector, mocked Anthropic
   client). These validate the parts of the system that are pure logic and
   should never depend on model behavior: SQL safety validation and
   guardrail fail-open behavior.
2. **Control-flow tests** (`test_agent_eval.py`) - a fake Anthropic client
   simulates specific model behaviors (direct answer, one tool round,
   Snowflake outage mid-turn, runaway tool-calling, invalid generated SQL)
   to verify the *loop* handles each correctly. These are not a substitute
   for evaluating real answer quality - they can't tell you whether the
   model actually writes correct SQL for a given question - but they run in
   CI for free and catch regressions in the orchestration code itself.
3. **Manual smoke test** (`scripts/smoke_test.py`) - a fixed list of golden
   natural-language questions run against the real Claude + Snowflake stack.
   Exercises real NL understanding and real generated SQL, but a human has
   to read the transcript and judge whether each answer is good - not
   automated, not scored, not repeatable as a pass/fail gate.
4. **Eval system** (`eval/`) - built specifically to close the gap in #3:
   the same style of golden questions, but each one now carries automated,
   deterministic checks (`eval/checks.py` - does the answer contain a number
   close to a hand-verified true value, was a real query actually made or
   correctly *not* made, did the guardrail classify it as expected, did it
   respond within budget), replayed through the *actual* guardrail-then-agent
   flow `app.py` uses, not a shortcut around it. A small, explicitly
   **advisory** LLM-as-judge layer (`eval/judge.py`) covers the couple of
   things a keyword check can't (e.g. "did it explain *why* medians can't be
   averaged, not just assert that they can't") - reported separately, never
   counted toward the authoritative total, because an LLM judging an LLM
   isn't ground truth. Deliberately **not** wired into CI (real API/warehouse
   cost) and runs each case once rather than multiple trials (trades
   statistical confidence for speed given the timeline) - documented
   limitations, not oversights.

**The eval earned its keep on the first real run, and the fix was verified
with a second run.** 32/33 deterministic checks passed on the first run,
and `crime_rate_out_of_scope` failed - not because the main agent handles it
badly (it doesn't; earlier manual smoke testing showed a good answer), but
because **the fast guardrail classifier was inconsistent on this specific
borderline case.** "What was the crime rate in Travis County in 2020?"
names a real, in-scope place, so on some runs the classifier correctly
passed it through to the main agent (which explains the limitation well),
and on other runs it pre-emptively rejected the whole message as OFF_TOPIC,
giving a more generic redirect instead of the better, topic-specific
explanation. This only showed up because the eval runs through the *real*
guardrail-first path - `smoke_test.py` calls the main agent directly and
skips the guardrail entirely, so it structurally could not have caught
this. Root cause: `GUARDRAIL_PROMPT` (`agent/prompts.py`) blurred two
different jobs together - "is this about US population/places at all"
(the classifier's actual job) versus "does this dataset have that specific
metric" (the main agent's job, which it already does well). A fast
classifier without explicit guidance reasonably struggled on a question
that sounds like it's asking about data availability. Fix: rewrote the
prompt to state explicitly that topic-scoping and data-availability are
different questions, and added the failing case itself as a worked
few-shot example. Re-running the full eval afterward confirmed **33/33
deterministic checks passing**, with no regressions on the off-topic or
injection cases. This is a clean example of eval-driven debugging: found
by a scored, repeatable check (not a vibe), root-caused precisely, fixed
with a targeted prompt change, and the fix was *proven* rather than assumed
by re-running the same eval.

Also worth noting honestly: the one advisory judge check that failed (on
the Texas median-income case) wanted a deeper statistical explanation than
the answer gave, even after the fix - left as-is deliberately, since it's a
good illustration of why judge results are advisory rather than
authoritative; that bar may be stricter than what's actually needed for a
good user-facing answer, and "chasing" an AI judge's pickiness isn't
obviously worth trading away a natural-sounding response.

**A second run, immediately after fixing the first bug, found a second, more
interesting one** - directly validating the value of re-running rather than
trusting one green pass. `long_tail_veteran_lookup` failed: 4 tool calls
were made, then the agent gave up with "I wasn't able to find a reliable
answer." Root cause was an off-by-one in `agent/llm_client.py`'s
`run_agent_turn`: each loop iteration is one model call, so a task needing
N tool calls needs N+1 rounds total (N to gather data, one more for the
model to read the last tool's results and actually write the answer). The
round budget (`MAX_TOOL_ROUNDS`) was set to exactly 4, so any question
whose lookup genuinely needed all 4 tool calls could *never* finish - the
final answering round was simply never budgeted for. This is a real design
bug that could have shipped invisibly: it only manifests on questions
needing the full multi-step lookup path (a glossary query, a geography
lookup, a data query, sometimes a retry), which is exactly the kind of
"long-tail concept not in the curated cheat-sheet" case this eval was
specifically built to exercise. Fixed by raising the budget to 6 (headroom
for the same worst-case lookup plus one true answering round), re-verified
with two more full eval runs afterward (33/33 both times).

**A third bug turned up from real usage, not from the automated test suites at
all.** While testing the deployed app, a broad follow-up question ("what else
can you tell me about Santa Clara County?") occasionally returned a generic
"I ran into trouble" instead of an answer. Tracing it with a standalone script
showed the model reasonably trying to fire off several independent lookups in
parallel in one turn (race, Hispanic origin, employment, education - none
depend on each other, so batching them is a legitimate efficiency choice, not
a mistake) - but writing out that many SQL queries plus explanations
sometimes exceeded the `max_tokens` budget for that single response, cutting
off mid-generation of the last tool call. The bug: `run_agent_turn` in
`agent/llm_client.py` decided whether to execute tool calls based on
`response.stop_reason == "tool_use"` alone - and a truncated response comes
back with `stop_reason == "max_tokens"` instead, even though most of its tool
calls were perfectly well-formed. The old code discarded the *entire* batch
(several good queries along with the one bad one) rather than running what it
could. Fixed by changing the condition to check for the actual presence of
tool_use blocks in the response, independent of the exact stop_reason -
`_execute_tool` already turned a missing/empty SQL into a graceful rejection
message rather than crashing, so a truncated block now degrades safely
without losing its well-formed siblings. Added a regression test
(`test_partial_tool_calls_still_execute_on_max_tokens_cutoff`) simulating
exactly this mixed-batch scenario, and re-verified against the real stack -
the same question that used to intermittently fail now reliably returns a
complete, multi-fact answer. This is the one bug in the whole project that
neither the mocked tests nor the eval/smoke-test golden questions were
designed to catch, because none of them happened to ask a broad enough
follow-up to trigger multi-call batching - a reminder that a curated golden
set, however good, is still a sample, not a guarantee.

**What I'd add with more time:** run each eval case multiple times
automatically (not just manually re-running a few times, as done above) to
get a pass *rate* instead of a single pass/fail per case, since LLM outputs
aren't fully deterministic - both bugs above were only visible because they
were intermittent, and a single lucky run of either could have hidden them;
add a small set of adversarial/red-team prompts specifically targeting the
guardrail classifier (more paraphrases of "ignore your instructions,"
indirect injection via a question that quotes suspicious text); and add an
eval case that deliberately forces a 5-6 round lookup to directly guard the
round-budget fix above, rather than relying on the veteran-lookup case to
incidentally exercise it. Load/performance testing (concurrent sessions,
sustained traffic) remains explicitly out of scope for this submission.

## Honest state of this submission

Done and verified against the real stack: Snowflake trial account created,
US Open Census Data Marketplace dataset added, real schema discovered and
integrated into `agent/schema_context.py`, a least-privilege service user
created with key-pair auth (working around the account's mandatory-MFA
policy), and the full golden-question smoke test run successfully end to
end - including the harder edge cases (ambiguous geography, city-not-a-county,
the median-aggregation trap, out-of-scope questions, off-topic redirection,
and a prompt-injection attempt). Three real bugs were found and fixed this
way (see "Bugs found via live testing" above) that the mocked automated
tests could not have caught.

**Not yet done:** deployment to Streamlit Community Cloud itself, and a
smoke test run against that deployed URL rather than a local connection
(the assignment's own "Deployment Truth" tip - local doesn't count).
**`<TODO>`: once deployed, fill in the live-demo URL at the top of README.md
and record here that the smoke test was re-run successfully against the
deployed environment before final submission.**
