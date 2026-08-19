# US Census Population Chat

A chat agent that answers natural-language questions about US population and
demographics, grounded in the **US Open Census Data (Neighborhood Insights
Free Dataset)** — SafeGraph's free Census dataset — available on the
Snowflake Marketplace.

**Live demo:** `<TODO: fill in Streamlit Community Cloud URL after deployment>`
**Access:** public, no login required.

## Architecture

```
User (browser)
   |
   v
Streamlit app (app.py) -- session state holds chat history
   |
   |--1. Fast guardrail check (agent/guardrails.py)-------------+
   |     cheap classifier call: ON_TOPIC / OFF_TOPIC / UNSAFE   |
   |     off-topic/unsafe -> canned reply, no further calls-----+
   |
   |--2. Agent tool-use loop (agent/llm_client.py)
   |     Claude (Sonnet) with three tools:
   |       query_census_data(sql, reasoning)          - real data query
   |       search_census_concepts(query)               - stage 1 concept lookup
   |       list_concept_columns(table_number, year)      - stage 2 column listing
   |     system prompt = curated schema description (agent/schema_context.py)
   |     + rules for ambiguity, out-of-scope questions, and refusing to guess
   |
   v
Snowflake (agent/tools.py)          Local retrieval index (retrieval/)
   - validates SQL is a single         - hybrid (TF-IDF + semantic) search
     SELECT with a LIMIT                 over ~365 pre-built concept docs
   - executes via key-pair auth        - deterministic column listing once
     against a read-only role            a concept is chosen
   - US Open Census Data (Marketplace share)
```

**Why this shape, not RAG or Cortex Analyst:**
- Census data is tabular/numeric (aggregates, filters, joins across
  geography/time), which needs exact computation, not semantic-similarity
  retrieval over text chunks - so grounding is done via **text-to-SQL tool
  use** against the real warehouse, not RAG.
- Cortex Analyst (Snowflake's managed NL→SQL) was considered but not used:
  it's only available in specific regions on trial accounts (a real risk of
  losing hours to a region migration), and using it would show less of the
  agent-engineering the assignment is scoring (prompt design, tool-use loop,
  guardrails, error handling) than building the loop by hand.

## The real dataset

The Marketplace listing (`US_OPEN_CENSUS_DATA_NEIGHBORHOOD_INSIGHTS_FREE_DATASET`)
is ACS 5-year survey estimates at **Census Block Group** granularity - the
finest Census geography, ~150 ACS table families (age/sex, race, income,
housing, education, employment, etc.), one physical table per family per
year (e.g. `2020_CBG_B19` = every income-related ACS table's columns), plus
metadata tables mapping cryptic column codes to descriptions and FIPS codes
to place names. Full detail and the exact grounding text given to the model
is in `agent/schema_context.py`; the highlights that shaped the design:

- **No city/place/ZIP table.** Only state and county (via FIPS) plus raw
  lat/long per block group. "Population of Austin, TX" isn't a clean join
  (Austin isn't a county) - the agent is instructed to say so and offer the
  containing county as a labeled approximation rather than silently
  substituting one for the other.
- **Medians aren't summable.** Fields like median household income are a
  per-block-group statistic; summing or averaging them across block groups
  to fake a county/state median is statistically invalid. The agent is
  instructed to refuse that and offer a clearly-labeled population-weighted
  approximation instead.
- **Column/table identifiers need double-quoting.** ACS columns are
  case-sensitive mixed-case identifiers (`"B01003e1"`, not `B01003E1`) and
  table names start with a digit (`"2020_CBG_B01"`) - both fail to resolve
  unquoted in Snowflake. This was found the hard way (see REFLECTION.md) and
  is now spelled out explicitly in the system prompt with a worked example.

## Repo layout

```
app.py                     Streamlit entrypoint: chat UI, session state, error handling
agent/
  llm_client.py             Claude tool-use loop (multi-turn, 3 tools)
  tools.py                  SQL validation + Snowflake execution (key-pair auth)
  guardrails.py              Fast on-topic/off-topic/unsafe pre-check
  prompts.py                  System prompt + guardrail prompt templates
  schema_context.py            Curated data dictionary fed into the system prompt
retrieval/                    Two-stage hybrid search for concepts outside the curated list
  build_corpus.py               Offline, one-time: builds the corpus + TF-IDF/embedding indexes
  search.py                      Query-time: search_concepts(), list_columns_for_concept()
  data/                           Committed artifacts (parquet/pickle/npy, tens of MB)
snowflake/
  setup.sql                    Read-only service role/user/warehouse (key-pair auth) for the app
  explore_schema.sql           Discovery queries used to map the real dataset schema
tests/                          pytest suite (see Testing below)
scripts/
  discover_schema.py            One-off: lists databases/schemas/tables via the app's own connection
  smoke_test.py                  Manual golden-question script against the real stack
```

## Grounding approach

`agent/schema_context.py` holds a curated, human-written description of the
real Census tables (naming convention, headline metric-to-column mappings,
geography/aggregation/SQL-syntax rules) that's rendered into the system
prompt. This covers ~12 of the most commonly-asked-about concepts directly -
a deliberate middle ground between hardcoding every one of the ~365 distinct
ACS concepts (infeasible - some single physical tables bundle 400+ columns
across dozens of concepts) and giving the model raw `information_schema`
access to introspect everything itself (slower, easy to get lost in).

**For anything not in that curated list**, a dedicated two-stage retrieval
system (`retrieval/`) takes over, via two extra tools:

1. **`search_census_concepts(query)`** - hybrid (keyword + semantic) search
   over ~365 pre-built *concept*-level documents (one per `TABLE_NUMBER`,
   built offline by `retrieval/build_corpus.py`), combined via Reciprocal
   Rank Fusion. Returns a handful of ranked candidate concepts for the model
   to judge against the actual question.
2. **`list_concept_columns(table_number, year)`** - once a concept is
   chosen, no search is needed: a deterministic filter returns *every*
   column that concept has, so the model picks the exact one matching the
   question's specific detail (e.g. "Male" + "Under 5 years") from a
   complete, guaranteed-correct list rather than guessing a column number.

This replaced an earlier, simpler approach (the model writing raw
`WHERE TABLE_TITLE ILIKE '%...%'` SQL against the metadata table directly)
after live testing found it genuinely unreliable - see REFLECTION.md for
the two specific bugs that motivated the rebuild and the failed first
attempt at fixing it (flat column-level ranking, which had its own bias:
a concept with 98 columns got 98 chances to appear in a ranked list, a
2-column concept got 2 chances, before the fix separated concept-ranking
from column-listing into two stages).

## Guardrails

Two independent layers, per the "operational guardrails" requirement:
1. **Fast pre-check** (`agent/guardrails.py`) - a cheap classification call
   run before the main agent, so off-topic/unsafe input fails fast instead of
   paying for a full tool-use round trip. Fails open to `ON_TOPIC` on
   classifier error, so a guardrail-layer outage doesn't take down the whole
   app. Verified live against on-topic, off-topic, and several
   prompt-injection phrasings (including a "pretend you are DAN" jailbreak
   attempt) - all classified correctly.
2. **System-prompt instructions** (`agent/prompts.py`) - the main agent is
   also told to refuse off-topic requests and prompt-injection attempts, as
   defense in depth if the classifier misses something.

A third, structural guardrail: `agent/tools.py` independently validates that
any SQL the model generates is a single `SELECT` with a `LIMIT` before it's
ever sent to Snowflake - on top of the service role only having read access.

## Graceful degradation

- **Snowflake unreachable** -> caught in `agent/tools.py`/`agent/llm_client.py`,
  surfaced as "I'm having trouble connecting to the Census data right now."
- **Bad/invalid generated SQL** -> caught and returned to the model as a tool
  result so it can retry with a corrected query (bounded to a few rounds);
  if it still can't produce a working query, the user gets an honest
  "couldn't find a reliable answer" message, not an error page.
- **Empty final answer** (e.g. the model got cut off mid-"thinking" before
  producing any text - a real failure mode found during live testing, see
  REFLECTION.md) -> caught and turned into an honest "I ran into trouble"
  message rather than a blank chat bubble.
- **Empty result set** -> reported to the model as "no matching rows," which
  it's instructed to relay plainly rather than guess.
- **Ambiguous questions** (e.g. "Washington" state vs. county) -> the model
  asks a clarifying question instead of picking one - verified live.
- **City/place questions** (not a county) -> the model states the limitation
  and offers the containing county as a labeled approximation - verified live
  with "median household income in Austin, TX".
- **Median-aggregation requests** above block-group level -> the model
  refuses to naively sum/average and offers a labeled weighted-average
  approximation instead - verified live with "average median household
  income across Texas".
- **Genuinely out-of-dataset questions** (crime rate, population in 2100)
  -> the model says so plainly - verified live and by the eval suite (see
  below). The guardrail classifier initially had a real inconsistency on
  borderline cases like "crime rate in Travis County" (sometimes rejected
  outright as OFF_TOPIC instead of passing through to the main agent's
  better, specific explanation) - found by the eval system, fixed by
  clarifying that the classifier's job is topic-scoping only, not guessing
  data availability; see REFLECTION.md for the full story.
- **Any other unexpected exception** -> caught at the top level in `app.py`
  and turned into a generic, honest failure message rather than a stack
  trace or blank response.

## Multi-turn conversation

Full message history (including tool-use turns) is kept in
`st.session_state.messages` and replayed to Claude each turn, so follow-up
questions ("how does that compare to Williamson County?") resolve against
the prior exchange - verified live. A per-session message cap (40) is
enforced as basic cost/abuse protection on the unauthenticated public
deployment.

## Latency

The 60-second budget is met by: showing a spinner during the SQL tool-call
phase (`st.status("Querying Census data...")`), capping the tool-use loop at
4 rounds, and a 30-second Snowflake statement timeout. Live-tested questions
(including ones requiring two SQL round-trips) completed in a few seconds
each. **Simplification made under time pressure:** the final answer is
rendered in one shot rather than token-streamed - true streaming interleaved
with tool-use calls is more plumbing than the time budget allowed; the
spinner covers the same UX goal (the user sees progress, not a frozen
screen). Noted in REFLECTION.md as a next step.

## Authentication note: Snowflake MFA

New Snowflake trial accounts enforce mandatory MFA on password logins,
which blocks any non-interactive client (this app, a CI job, a local
script) from connecting with a plain password. The app's service user
(`CENSUS_APP_USER`, created by `snowflake/setup.sql`) instead uses
**key-pair authentication**, which isn't subject to that policy and is
Snowflake's own recommended approach for service/programmatic connections
anyway. `agent/tools.py` prefers `SNOWFLAKE_PRIVATE_KEY` when present in
secrets, falling back to `SNOWFLAKE_PASSWORD` only if you deliberately want
password auth (which will fail on an MFA-enforced account).

## Running locally

```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # fill in real values
streamlit run app.py
```

## Testing

```bash
pytest tests/ -v
```

- `tests/test_tools.py` - SQL validator (rejects DDL/DML/multi-statement,
  auto-adds LIMIT) and the Snowflake query wrapper against a mocked connector.
- `tests/test_guardrails.py` - on-topic/off-topic/unsafe classification,
  including fail-open behavior when the classifier call itself errors.
- `tests/test_agent_eval.py` - the tool-use control loop (direct answer, one
  tool round, multi-turn history retention, Snowflake-unavailable path,
  max-rounds giveup, invalid-SQL-reported-to-model, empty-final-text
  fallback) against a **fake** Anthropic client, so these are deterministic
  and run in CI (`.github/workflows/tests.yml`) with no API/warehouse cost.
- `tests/test_retrieval.py` - the two-stage concept/column search
  (`retrieval/search.py`) against the real, committed corpus artifacts
  (static offline-built data, no Snowflake/API calls needed) - regression
  tests for both real bugs this system fixes (concept crowding, field-level
  guessing), plus shape/filtering checks on `list_columns_for_concept`.
- `scripts/smoke_test.py` - **not** part of the automated suite. Golden
  natural-language scenarios (straightforward lookup, multi-turn follow-up,
  ambiguous geography, city-not-a-county, median-aggregation trap,
  out-of-scope topic, off-topic, prompt injection) run against the real
  Claude + Snowflake stack. This is how the bugs described in REFLECTION.md
  were actually found - the mocked control-flow tests alone would not have
  caught them. Run it locally with real secrets in `.streamlit/secrets.toml`
  before submitting/deploying.

See REFLECTION.md for what's deliberately out of scope in the test suite.

## Eval

`scripts/smoke_test.py` above answers "does it run without crashing on real
questions" - it doesn't *score* anything, a human has to read the transcript.
`eval/` closes that gap: the same kind of golden questions, but each one now
has automated pass/fail checks attached, replayed through the exact same
guardrail-then-agent flow `app.py` uses (not a shortcut around it).

```bash
python -m eval.run_eval
```

- `eval/checks.py` - deterministic, free, authoritative checks: does the
  answer contain a number close to a hand-verified true value
  (`contains_number_near`), does/doesn't it mention certain phrases
  (`mentions_any` / `not_mentions_any`), was a real Snowflake query actually
  made (`tool_was_called`) or correctly *not* made for a guardrail-rejected
  message (`tool_was_not_called` - this doubles as a cost-control check, not
  just a quality one), did the guardrail classify it as expected
  (`guardrail_classified_as`), did it respond within the 60s budget
  (`within_latency`).
- `eval/judge.py` - a small, explicitly **advisory** LLM-as-judge layer for
  the couple of things a keyword can't capture (e.g. "did it explain *why*
  medians can't be averaged, not just assert that they can't"). Reported
  separately in the scorecard and never counted into the authoritative
  pass/fail total - an LLM judging an LLM is not ground truth.
- `eval/cases.py` - the golden set: the same scenarios validated via
  `smoke_test.py`, now with checks attached, plus three cases exercising the
  long-tail retrieval fallback for concepts outside the curated headline
  metrics list ("veterans," "commute time," "males under 5 years old" - the
  latter two are permanent regression guards for real bugs found live, see
  REFLECTION.md).
- `eval/run_eval.py` - runs every case, prints a console scorecard, and
  writes a timestamped report to `eval_results/`.

**Not wired into CI** - like `smoke_test.py`, this costs real Anthropic/Snowflake
usage and needs live secrets, so it's run manually, not on every push. Each
case runs once (not multiple trials), which trades statistical confidence for
speed and cost given the project timeline - a documented limitation, not an
oversight (see REFLECTION.md).

Real runs of this eval - and sharp questions during review - found six
genuine bugs, not hypothetical ones: a guardrail classification
inconsistency, an off-by-one in the tool-use round budget, a partial
tool-call batch getting discarded entirely on a mid-generation cutoff,
row-cap truncation that could silently produce a confidently wrong answer
for a broad query (e.g. "every county in Texas" - 254 counties), and two
found while live-tracing the long-tail concept lookup: a keyword search
that could silently miss the right table (crowded out by a larger, wrong
one behind a row limit), and a model forced to guess an internal column
number with no principled way to know it, which burned its whole round
budget without ever answering. The last two motivated replacing that
lookup entirely with the two-stage retrieval system described above. All
six are fixed and permanently guarded by eval cases. The latest full run
confirmed 51/51 deterministic checks passing consistently. See
REFLECTION.md for the full story.

## Deployment

Deployed on Streamlit Community Cloud:
1. Push this repo to GitHub.
2. Create a new Streamlit Community Cloud app pointing at `app.py`.
3. In the app's Secrets settings, paste the contents of a filled-in
   `.streamlit/secrets.toml` (see `.streamlit/secrets.toml.example`) -
   including the PEM private key for key-pair auth.
4. Verify against the **deployed** URL, not just localhost, before submitting.

## Known limitations / not done

See REFLECTION.md for the full list; the headline items are: streaming is
spinner-based rather than token-by-token, there's no persistent conversation
storage (history resets on page refresh, by design, for a public
unauthenticated demo), the automated test suite mocks the LLM rather than
replaying real recorded API responses, and `sentence-transformers` (used by
the retrieval system's semantic search half) pulls in `torch` - a genuinely
heavy dependency (~1-2GB) that is a real risk for Streamlit Community
Cloud's free-tier build limits and has **not yet been verified against an
actual deployment**. If the free tier can't handle it, the documented
fallback is dropping the semantic half and keeping TF-IDF-only search,
which still fixes both concept-lookup bugs on its own (verified separately
during development).
