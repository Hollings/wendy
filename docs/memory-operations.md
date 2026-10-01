# Agentic memory pilot

Implemented stack: **Wendy MCP → isolated research agent → Hindsight + original
source index**. The bot remains the authority for conversation scope and inbox
visibility. The service has no Discord send tool or access to Wendy's filesystem.

## What is implemented

- `research_memory(question, context?, depth?, followup_to?)` returns one compact
  text answer with citations. `open_memory_evidence(research_id, refs, cursor?)`
  expands originals on demand.
- Gemini researcher (default), DeepSeek, and Claude CLI researchers. All use
  the same read-only gateway, source validation, budgets and result contract.
- Hindsight 0.10.2: extracted facts, semantic/graph/temporal recall, per-domain
  banks, durable asynchronous ingestion, idempotent operation IDs, revision
  filtering, bounded ten-minute chat episodes and document replacement/deletion.
- SQLite FTS5 original-source search, reply/neighbor expansion, source hashes,
  speaker roles, timestamps, stable Discord links and journal/profile locators.
- Transactional message-change outbox, snapshot/replay, file reconciliation,
  restored-index detection, scoped follow-ups and seven-day result retention.
- Optional resumable Discord backfill, active/accessible archived thread
  discovery, and a full audit to reconcile edits/deletions missed while offline.
- Private evaluation modes: source search, Hindsight recall, or combined. Original
  evidence lookup stays available in all modes.

## Configuration

The pilot is disabled until `WENDY_MEMORY_ENABLED=true` in the bot. Existing
conversation behavior is unchanged when disabled. Restart bot clients when
enabling it so Claude discovers the new MCP tools.

[`config/memory.json`](../config/memory.json) controls corpus policy:

| Setting | Meaning |
| --- | --- |
| `channels: []` | All configured Wendy conversations, plus registered child threads. Guild logger data outside this set is excluded. Supply channel IDs to narrow the pilot. |
| `cross_channel: true` | Every included conversation may research every included domain. Set false to restrict each conversation to its own history and journal. |
| `include_profiles: true` | Explicitly share current global people profiles among included conversations. Set false if those notes should not be shared. |
| `backfill: false` | Only cached history is imported. Set true to fetch older Discord history on startup. |
| `backfill_audit: false` | Set true with backfill for a complete rescan, refreshing edits and removing messages missing from a successfully scanned channel. Partial/failed scans never purge that channel. |

Each thread's journal uses its own registered folder. File scans read **runtime**
Markdown under channel journals and `claude_fragments/people`, not repository
seeds. Renames remove the old source ID. Files over 500 KB, unreadable files and
symlinks are excluded. Source files are never modified by memory research.

The origin conversation's cutoff is its existing delivery watermark. Search never
advances that watermark. Other authorized conversations are bounded by their
cached history at the start of the request. A Hindsight episode containing any
source outside these limits is withheld in full.

[`config/memory_limits.json`](../config/memory_limits.json) has standard/deep
deadlines (25/60 seconds), retrieval calls (8/16), source byte caps (48/96 KB),
answer character caps (2,800/4,800), and citation caps (3/6). These are hard bounds,
not exact token counts. Citations and limitations add small bounded overhead.
Requests also have fixed transport limits. Restart the memory service after
changing limits. Research concurrency and pending ingestion default to two each,
configurable with `WENDY_MEMORY_CONCURRENCY` and `WENDY_MEMORY_INGEST_CONCURRENCY`.

## Production setup

The compose override is [`deploy/docker-compose.memory.yml`](../deploy/docker-compose.memory.yml).
Create three restricted secret files on the deployment host. Do not put values
in Git or the JSON config.

`/srv/secrets/wendy/memory-bot.env`:

```dotenv
WENDY_MEMORY_SERVICE_TOKEN=<random shared secret, at least 32 characters>
```

`/srv/secrets/wendy/memory.env`:

```dotenv
WENDY_MEMORY_SERVICE_TOKEN=<same shared secret>
HINDSIGHT_API_KEY=<separate random backend secret>
WENDY_MEMORY_RESEARCHER=gemini
GEMINI_API_KEY=<existing suitable Gemini key>
WENDY_MEMORY_GEMINI_MODEL=gemini-3.5-flash
```

To use Claude, select `WENDY_MEMORY_RESEARCHER=claude`, supply a valid
`CLAUDE_CODE_OAUTH_TOKEN` (or `ANTHROPIC_API_KEY`), and optionally
`WENDY_MEMORY_MODEL=sonnet`. The image pins Claude CLI 2.1.280. Its clean child
environment receives only inference credentials and its own retrieval capability.
The local token used during development was revoked; Claude live inference has
**not** passed the smoke test. Gemini has.

To test DeepSeek V4.1 Flash, use the following in `memory.env`:

```dotenv
WENDY_MEMORY_RESEARCHER=deepseek
DEEPSEEK_API_KEY=<DeepSeek credential>
WENDY_MEMORY_DEEPSEEK_MODEL=deepseek-flash
WENDY_MEMORY_DEEPSEEK_EFFORT=low
```

`low` explicitly enables thinking with low effort; `none` disables it. `high` and
`max` are also supported but must still finish within the same research deadline.
The provider's required reasoning context is passed back only within the isolated
request. It is never returned to Wendy or stored in research results. Token counters
include thinking in `output_tokens`, report `thinking_tokens` separately (a subset,
not an extra charge), and track cache hits/misses. Gemini counters now also include
thinking. Usage covers the researcher; Hindsight's internal inference and ingestion
are separate and are not included in these counters.

`/srv/secrets/wendy/hindsight.env`:

```dotenv
HINDSIGHT_API_TENANT_API_KEY=<same separate backend secret>
HINDSIGHT_API_LLM_PROVIDER=gemini
HINDSIGHT_API_LLM_API_KEY=<existing suitable Gemini key>
HINDSIGHT_API_LLM_MODEL=gemini-3.5-flash
```

Hindsight can independently use DeepSeek with `HINDSIGHT_API_LLM_PROVIDER=deepseek`,
`HINDSIGHT_API_LLM_MODEL=deepseek-flash`, and the DeepSeek credential in
`HINDSIGHT_API_LLM_API_KEY`. Embeddings and reranking stay local.

From the repository root on the host:

```sh
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.memory.yml build memory wendy
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.memory.yml up -d hindsight memory wendy
```

Only the memory gateway is published, on host loopback port 8950. Hindsight has no
published host port and requires its separate API credential. The service gets no
bot secret file, Docker socket or source-data volume. The full Hindsight image
includes local embeddings/reranking and is about 9 GB on AMD64. Its embedded
PostgreSQL volume is suitable for the pilot; plan external PostgreSQL and backups
before a larger deployment. Backfill can incur substantial extraction cost: start
with selected channels and inspect ingestion progress before broadening scope.

Keep **one bot exporter and one memory service instance per source database**.
The bot synchronizes sources before and after a query and revalidates the result.
If evidence changes, the query is rejected so Wendy can retry. Warm clients need
a restart after changing the enabled tool set; policy changes apply per request.

Rollback: set `WENDY_MEMORY_ENABLED=false`, restart Wendy, and stop the two memory
services. Keep volumes for recovery. Rebuilding the source volume causes a fresh
source scan. Do not restore a Hindsight database from a different point in time
and assume it matches source checkpoints; use a fresh bank prefix and rebuild
the memory source volume together, allowing the bot to export originals again.

## Verification and evaluation

Verified on 2026-09-29:

- Full bot suite in a disposable Linux container: **351 passed, 2 skipped**.
- Memory suite: **15 passed**, including the real stdio MCP-to-bot-to-service
  path, origin cutoff and domain separation, revision/citation checks, replay,
  cancellation, output limits, file reconciliation and offline-history audit.
- Built `wendy-memory:pilot` and ran Hindsight 0.10.2 with actual Gemini inference.
  The Linux-image research run answered the synthetic corrected-decision question
  in **8.98 seconds**, using five retrieval calls and three verified references.
  Immediate source suppression and backend fact deletion both passed.
- Native Windows full-suite run encountered six existing POSIX/filesystem
  assumptions; the Linux run above is the deployment-target result.

These checks validate the implementation and one synthetic scenario. They do not
measure quality on Wendy's real history. Production services have not been changed.

### DeepSeek live test (2026-09-29)

Added a native DeepSeek tool-loop adapter and tested `deepseek-flash` (V4.1 Flash)
with low thinking effort. Hindsight 0.10.2 also used DeepSeek for this run, with
its existing local embeddings/reranker. The rebuilt Linux image passed five
searches over three synthetic messages: three repetitions of a corrected decision,
one missing launch-date question, and one history-cutoff question. Exact source
quotations/revisions were verified. The cutoff returned only the initial MySQL
proposal; the full history returned the final Postgres decision and both reasons.
Missing evidence returned `no_evidence`. Ingestion, grounded recall, immediate
suppression, and backend deletion all passed.

The first run exposed a coverage-tool bug: its `sources` count mapping was treated
as a list of source records. Fixed it and added a regression test. The combined
memory/provider suite now passes **23 tests**; lint and the image build also pass.

| Case | Seconds | Uncached input | Cached input | Output incl. thinking | Thinking subset | Researcher USD off-peak | Researcher USD peak |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Decision 1 | 5.57 | 1,669 | 6,784 | 782 | 94 | 0.00073990 | 0.00147980 |
| Decision 2 | 6.08 | 1,765 | 6,656 | 836 | 56 | 0.00078632 | 0.00157264 |
| Decision 3 | 4.02 | 1,576 | 4,096 | 578 | 17 | 0.00059549 | 0.00119098 |
| Missing evidence | 7.60 | 3,412 | 11,392 | 1,111 | 193 | 0.00121258 | 0.00242515 |
| History cutoff | 5.04 | 1,311 | 5,376 | 622 | 80 | 0.00058598 | 0.00117196 |

Median latency: **5.57 seconds**. Average researcher cost: **$0.000784 off-peak /
$0.001568 peak**, equivalent to **$0.78 / $1.57 per 1,000 similar searches**.
Calculated from API-reported tokens using [DeepSeek's current direct API rates](https://api-docs.deepseek.com/quick_start/pricing/):
$0.15/$0.003/$0.60 per million uncached-input/cached-input/output tokens off-peak,
double those rates at peak. These are priced token estimates, not account-invoice
measurements. Hindsight internal LLM calls, ingestion, hosting, and Wendy's own
response are excluded. Repeated prompts benefit from caching, and a three-message
corpus is not representative of production search quality or cost. Raw synthetic
results are in the ignored local `.venv/deepseek-memory-linux.jsonl` file.

DeepSeek is selectable; the production default is still Gemini. No production
deployment or real-history import was performed as part of this test.

```sh
python -m pytest tests/test_memory.py tests/test_memory_providers.py -q
docker build -f services/memory/Dockerfile -t wendy-memory:pilot .
docker pull ghcr.io/vectorize-io/hindsight:0.10.2
python -m scripts.memory_smoke --env-file .env --docker --researcher gemini
python -m scripts.memory_smoke --env-file .env --docker --researcher deepseek --hindsight-provider deepseek --repeats 3 --extended
```

The live test uses synthetic messages, creates a uniquely named disposable
Hindsight container, and removes it afterwards. It checks real extraction,
grounded recall, researcher citations, immediate suppression of deleted evidence,
and remote fact deletion. Add `--research-image wendy-memory:pilot` to run the
research loop in the Linux image. No Discord messages or production data are used.
`--extended` adds missing-evidence and restricted-cutoff cases. `--out <path>` appends
synthetic results with citations and usage as JSONL. Repeated cases also expose API
cache behavior; they are not independent quality examples.

For private quality evaluation, create a JSONL suite with one object per query:

```json
{"id":"decision-1","question":"Which database did we finally choose?","scope":{"origin":"7","domains":["channel:7"],"cutoffs":{"7":"102"},"policy":"evaluation"},"expected_source_ids":["discord:102"],"forbidden_terms":[],"should_abstain":false}
```

The IDs must refer to actual indexed sources. Use the service credential in the
operator environment, then run:

```sh
python -m scripts.memory_eval --suite private-cases.jsonl --out private-results.jsonl
```

This compares all three retrieval modes and reports latency, evidence recall,
abstention and forbidden-term checks. Review grounding manually; quotation
matching does not prove that an answer follows from its citations. Keep questions
and outputs containing real history outside Git. The planned ~60-question Wendy
quality set is not populated yet; a synthetic smoke pass is not a quality benchmark.

## Current limits

- Original-source retrieval uses FTS5; semantic embeddings and graph retrieval
  come from Hindsight. An independent source-vector baseline is still future work.
- Hindsight observations/reflect are disabled. Only world/experience facts with
  current document provenance enter the researcher. This avoids mixed derived
  summaries during access changes and deletion. Other backends remain future work.
- A failed or timed-out researcher returns `unavailable`; no unfinished draft is
  exposed. Research IDs expire after seven days or any indexed source mutation.
  Invalidation is deliberately conservative and can require repeating a follow-up.
- Scope is administrator-defined, not a live Discord membership/ACL mirror.
  Inaccessible/undiscovered threads and failed imports remain coverage gaps.
  Backfill is not a guarantee that every historical message still exists.
- Images/attachments are URL references only. Claude session logs, old file
  revisions, a Brain trace UI, and automatically seeded production evals are not
  part of this pilot. Source evidence remains authoritative.
