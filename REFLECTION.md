# Reflection

## Development process and key architectural decisions


The first big decision was how to ground answers in the data at all. Census questions aren't "find the relevant document" questions. "What's the population of Travis County" needs an exact sum across a few hundred rows, not a similarity match against a chunk of text. That ruled out RAG pretty much immediately — RAG is the right tool when the answer is somewhere in a passage, and the wrong one when the answer is a computation. So it came down to either letting the model write real SQL and execute it against Snowflake, or using Cortex Analyst, Snowflake's own managed text-to-SQL feature. I looked hard at Cortex Analyst since it's literally what Snowflake built for this. Two things pushed me away from it: it's region-gated on trial accounts, which felt like a real risk of losing hours to an account migration with a 24-hour clock running, and it's a managed black box, so using it would hide most of the actual engineering work — prompt design, the tool-use loop, guardrails, error handling — that the assignment says it's grading. So I built the loop by hand and used Snowflake purely as the warehouse.

Once that was decided, the next question was how much of the schema to actually hand the model. The dataset has around 365 distinct concepts (a concept being one specific measurement — "Total Population," "Sex By Age," that kind of thing; basically TABLE_NUMBER in the field descriptions meta data table), and some physical tables bundle 400+ columns. Trying to hardcode all of that would've been enormous and slow to reason over, and honestly the model would still have had to search through it anyway. I ended up thinking about it the way you'd onboard a new analyst — you don't hand someone the entire data dictionary on day one, you tell them the dozen tables they'll actually use and make sure they know how to look up the rest themselves. So `agent/schema_context.py` documents about a dozen common concepts by hand, and everything else goes through a lookup mechanism, which — more on this below — I ended up rebuilding twice because my first two attempts didn't hold up.

For Snowflake authorization, The obvious move, a service account with a password, broke the first time I tried it programmatically: new trial accounts require MFA on password logins, and MFA has no way to complete itself for a script. That's not just annoying for local testing either — it would've broken the deployed app too, since Streamlit Cloud is exactly the kind of non-interactive client that can't click through an MFA prompt. So whatever I did had to work for an automated account, not just for me at a terminal, which is what pointed at key-pair auth — Snowflake's own recommended pattern for service accounts, and not subject to the MFA policy at all.

Guardrails are two separate layers on purpose. If one thing catches everything, one mistake breaks everything. So there's a fast, cheap classifier screening messages before the real agent runs at all (cheap rejection for obviously off-topic stuff, and it also proves the expensive Snowflake/Claude path never even fires for those messages), and separately the main agent's own system prompt repeats the same refusal instructions as a backup in case the classifier gets something wrong.

## The retrieval system: two attempts that didn't work before one that did

Worth walking through this one in detail, because it's the part of the build where I actually learned something instead of just executing a plan.

First pass: let the model write its own SQL against the metadata table, a plain `WHERE TABLE_TITLE ILIKE '%...%'` search. Simple, no extra infrastructure, and it worked in early testing — a "how many veterans" question came back correct. That felt like enough evidence at the time. It wasn't. It worked because "veterans" happened to be a distinctive enough word that a crude substring match couldn't really miss it.

Two harder questions broke it. Asked about "travel time," the search actually matched the right table, but a row limit on the unranked results let a different, bigger table crowd it out completely — I re-ran the same search with the limit removed and confirmed the right answer had been sitting there the whole time, just never surfaced. Asked "how many males under 5," the model had no way to know which of ten internal column-numbering levels held that detail, guessed wrong twice, and burned its entire round budget without ever producing an answer. Neither of those is some contrived edge case — they're exactly the kind of thing a normal person would ask.

So I rebuilt it properly: TF-IDF for keyword matching plus sentence-transformer embeddings for semantic matching, combined with Reciprocal Rank Fusion (merged by rank position, not raw score, since the two methods' scores aren't on comparable scales). That's the standard way to do this kind of search well. I built it, tested it instead of assuming it worked, and it still failed the travel-time case. Took a bit of digging to figure out why — I was still ranking individual columns, and a concept with 98 columns (Sex By Age, for instance) gets 98 separate shots at a fixed-size results list while a concept with 2 columns only gets 2. Nothing to do with actual relevance, just a numbers game the bigger concept was always going to win.

Once I saw that, the fix was obvious in hindsight: stop conflating "which concept is relevant" with "which specific column within it." Split it into two stages. Stage one ranks concepts, not columns — one document per concept, about 365 of them, so every concept gets exactly one shot regardless of size. Stage two doesn't rank anything — once a concept's picked, it's just a complete list of every column that concept has, and the model reads it and picks the right one. That kills the column-count bias at the root instead of trying to tune around it, and it happens to solve the field-level-guessing problem too, for free, since there's no number left to guess. Both questions that used to fail now answer correctly and consistently — checked each one three times against the real stack before I trusted it.

The thing I'd actually take away from this: my first fix felt right. Hybrid search is the textbook answer. It still failed, because I fixed the algorithm without questioning the level it was operating at. Testing it rather than just building it is the only reason I caught that.

## What I'd improve or do differently with more time

Expanding the eval suite to cover more testcases and running the eval suite multiple times instead of once would probably be the highest-value thing to add. Two of the bugs I found this session — a guardrail inconsistency and a round-budget off-by-one — were intermittent, and I only caught them because I happened to re-run something a second time. A single pass/fail per case is almost certainly hiding others.

Beyond that:

- The long-tail retrieval system is only directly verified on maybe five concepts out of 365. Reasonable spot check, not real coverage.
- Real token streaming instead of a spinner-then-everything-at-once would feel noticeably better on longer answers.
- I tried one prompt injection phrasing and it got blocked, but nobody's actually tried to break this deliberately with a few different strategies.
- The app caches one Snowflake connection per session with no pooling — fine for a demo, wouldn't survive real concurrent traffic. Need a caching system to cache already searched queries with a TTL to preserve latency budget.
- Conversation history just grows, capped at 40 messages. A real deployment would want to trim or summarize older turns before that becomes an issue.
- There's no logging of questions, generated SQL, or latency anywhere right now. For an actual handoff that's the first thing I'd add, so a bad answer is debuggable after the fact instead of only reproducible by luck.
- Would make the UI better.

## Edge cases and failure modes identified but not fully addressed

Data returned from Snowflake gets fed back to the model as a tool result, and the guardrails only ever look at what the user typed — not what comes back from the database. If the dataset itself ever contained adversarial text, it'd reach the model unfiltered. Low real risk since Marketplace data isn't user-writable, but worth naming since it's a real gap in the guardrail design, not just a hypothetical one.

The dataset spans 2019 and 2020 ACS estimates plus a separate 2020 Redistricting table built on an entirely different methodology — actual counts, not survey estimates. The model's told to say which year it used, but nothing stops it from silently mixing a 2019 figure with a 2020 one across two calls in the same answer.

The retrieval system has no stemming, so a plain plural mismatch — searching "males" against corpus text that says "Male" — can weaken the keyword half of the search. Didn't actually cause a failure in testing, since concept-level search rarely hinges on getting one word form exactly right, but it's a real gap I know about rather than one I've closed.

Non-English or malformed input was never tested — empty strings, very long input, non-UTF8 text. And there's been zero load testing on an app that's now genuinely public and has no login.

## How I approached testing, and what I'd add

Four layers, and the reason there are four instead of one is that each one catches something the others structurally can't.

Unit tests check pure logic in isolation — does the SQL validator actually reject `DROP TABLE`, does the guardrail classifier fail open instead of locking people out if the classification call itself errors. No AI, no database, nothing non-deterministic. If one of these breaks it's unambiguously my own code's fault.

Control-flow tests simulate the model with a scripted fake so I can check the tool-use loop's retries and error handling without needing a real API call for every scenario.

Neither of those tells you whether the real model, asked a real question, actually writes correct SQL and gives a good answer. That's what the manual smoke test is for — real questions against the real Claude and real Snowflake. This is genuinely how the first bugs in this whole project turned up: case-sensitive column names that fail silently unless quoted, an empty-answer bug caused by the model eating its whole token budget on internal reasoning before writing anything, the MFA wall mentioned above. None of that shows up in the first two layers, because it only exists in the gap between what I assumed the system would do and what it actually did.

The problem with the smoke test is a human has to read the transcript and decide if it's good — not repeatable, not scored, not something I can realistically rerun after every small change. That's what pushed me to build the eval system: same kind of real questions, but each one carries automated checks now — does the answer contain a number close to something I've independently verified, was the database actually queried (or correctly not queried, for something that should've been rejected outright), did the guardrail classify it as expected, did it respond in time. There's a small advisory layer where a second, cheap model call judges the couple of things a keyword check can't — but that layer doesn't count toward the real score. An AI grading an AI isn't ground truth, so it's reported separately.

It paid off. Between that system and some direct live tracing when something looked off, I found six real bugs across the session: the guardrail inconsistency and round-budget issue above, a batch of tool calls getting thrown out entirely because one of them got cut off mid-generation, silent truncation on a broad query that could've produced a confident but wrong total, and the two retrieval failures that led to the rebuild described earlier. Every one of them has a permanent regression test now.

What I'd add next: automated multiple trials per eval case to get an actual pass rate instead of trusting one green run, since I already know some of these failures come and go; a much wider sample of long-tail concepts than the handful I've checked by hand and some deliberate adversarial cases.
