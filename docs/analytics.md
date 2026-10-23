# Analytics: audit trail, semantic memory and Parquet SQL (DuckDB)

Each agent can keep an in-process [DuckDB](https://duckdb.org) 2.0 database next to it:

| Table | One row per | What it holds |
| --- | --- | --- |
| `agent_episodes` | agent run | who/what started it (pseudonym, never an e-mail), start time, duration, outcome (`ok`, `blocked` + reason, `awaiting_approval`, `error`), model, input/output tokens, every tool call with its duration and success. With capture `redacted`: the latest input, the messages sent to the model, the output and each tool call's arguments, **after PII redaction**. |
| `semantic_memory` | thing the agent remembers | active lessons, subject events, each person's own notes, with an embedding (`FLOAT[n]`) for hybrid SQL + vector search. |

Redis or Cosmos DB stays the live store for sessions and memory; the DuckDB file is a per-agent copy built for
questions like "which tool is slowest this week", "how many runs did the guardrail refuse", "what do we already
know about invoice INV-0042". It is **off by default**.

```bash
AGENTKIT_ANALYTICS_ENABLED=true
pip install "agentkit-memory[duckdb]"          # duckdb 2.0 + s3fs
pip install "agentkit-memory[fastembed]"       # only for in-process embeddings
```

It needs long-term memory (`AGENTKIT_MEMORY=user` or `full`, the default). `build_agent` wires it with no code in
the agent; the memory API gains `/v1/analytics` for memory admins.

## What the agent gets

* **`search_memory(query, kind?, subject?, limit?)`**: searches its active lessons, subject events and the current
  person's own notes. Filters run in SQL (agent, tenant, person, kind, subject, not expired), then each row is
  scored `0.7 × cosine similarity + 0.3 × word overlap`. With no embedder, or when the embedder is slow or down,
  it scores by words alone. Pending lessons are not searchable (they haven't been reviewed); one person's notes
  are never returned to another.
* **`query_analytics(sql)`** (only when datasets are configured): one read-only SELECT over Parquet datasets.

Results come back inside a fence that tells the model the text is data, not instructions.

## How it handles many agents at once

DuckDB allows one writer per file, so:

* **One writer thread** owns the read-write connection. Every write (an episode, a memory change, an embedding)
  is put on a bounded queue without waiting; the writer applies them in batches, one transaction per batch. If
  one write in a batch fails, the rest are applied one by one. When the queue is full the write is **dropped and
  counted** (`dropped` on the status page and `agentkit.analytics.operations{outcome=dropped}`); a reply is never
  slowed down or failed by analytics.
* **Readers** use a small pool of DuckDB cursors on worker threads and read under MVCC beside the writer.
  Ad-hoc SQL is interrupted at its timeout.
* **One process per file.** On the VPS each agent has its own volume. With several replicas, each keeps its own
  file and exports under its own `replica` name; dashboards read the Parquet exports, never the live file.

Embeddings are computed by a background worker after a row is stored, never on the write path. Rows whose text
didn't change keep their vectors. Changing the embedding model re-embeds everything automatically.

## Capture and privacy

| `AGENTKIT_ANALYTICS_CAPTURE` | Stored |
| --- | --- |
| `off` (default) | Metadata only: timing, outcome, tokens, model, tool names and durations. |
| `redacted` | Also the input, prompt messages, output (each up to 4,000 characters) and tool arguments (500), after e-mail, SSN, card and phone numbers are replaced with `[REDACTED_…]`. |

Raw text is never stored. Actors are the same salted pseudonyms as the run journal. Memory text was screened
(no PII, no prompt injection) before it was stored in the first place. Every row has an expiry
(`AGENTKIT_ANALYTICS_RETENTION_DAYS`, default 30, for episodes; memory rows keep their own expiry) and expired
rows are deleted hourly. Only memory admins can read anything through the API.

## Parquet exports

```bash
AGENTKIT_ANALYTICS_EXPORT_URI=s3://acme-lake/agents       # or an absolute folder
AGENTKIT_ANALYTICS_EXPORT_INTERVAL_MINUTES=60
AGENTKIT_ANALYTICS_S3_ENDPOINT_URL=                        # MinIO, R2...; empty for AWS
# credentials: the usual AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_REGION, or an instance role
```

Layout (Hive partitions, readable by DuckDB, Spark, Athena, Fabric):

```
<uri>/agent_episodes/agent=<agent>/date=<YYYY-MM-DD>/<replica>-<ms>.parquet    new rows since the last export
<uri>/semantic_memory/agent=<agent>/<replica>-latest.parquet                    a full snapshot
```

Episode exports are incremental (a watermark in the database), so no row is exported twice. S3 goes through
`s3fs` rather than DuckDB's `httpfs` extension, so nothing is downloaded at run time.

## Dataset SQL tool

```bash
AGENTKIT_ANALYTICS_DATASETS=sales=s3://acme-lake/sales/,episodes=/data/analytics/export/agent_episodes/
AGENTKIT_ANALYTICS_QUERY_MAX_ROWS=200
AGENTKIT_ANALYTICS_QUERY_TIMEOUT_SECONDS=10
AGENTKIT_ANALYTICS_QUERY_MEMORY_LIMIT=512MB
```

Each `name=uri` pair becomes a view (a folder ending in `/` means every Parquet file under it, with Hive
partition columns). Two walls stand around every query:

1. **The SQL check** (`sqlguard.py`) uses DuckDB's own parser (`json_serialize_sql`): exactly one SELECT; only the
   listed views (or CTEs it defines); no table functions (`read_csv`, `glob`, `range`...), no file paths or
   `s3://` URLs as tables, no other schema or catalog, no functions that read files, settings or the environment
   (`getenv`, `current_setting`, `read_text`...). INSERT, COPY, ATTACH, INSTALL, SET and PRAGMA never parse as a
   SELECT.
2. **The connection itself**: after the views are created it may read only the datasets' own folders
   (`allowed_directories`), all other file and network access is off (`enable_external_access = false`) and the
   configuration is locked (`lock_configuration = true`), so even SQL that got past the check can't read
   `/etc/passwd` or turn access back on. It has its own memory and thread limits.

Then: a row cap, a timeout (the query is interrupted), at most two queries at once, and every string in the
result is PII-redacted before the model sees it. A dataset that can't be read is left out and shown as
unavailable, rather than failing every query; it is retried after a minute.

## Embeddings

| `AGENTKIT_ANALYTICS_EMBEDDINGS` | |
| --- | --- |
| `none` (default) | Word matching only. |
| `ollama` | A local [Ollama](https://ollama.com) server: `AGENTKIT_ANALYTICS_OLLAMA_URL`, `AGENTKIT_ANALYTICS_OLLAMA_MODEL` (default `nomic-embed-text`, 768 dimensions). Run `ollama pull nomic-embed-text` first. |
| `fastembed` | In-process ONNX (`agentkit-memory[fastembed]`), default `BAAI/bge-small-en-v1.5` (384 dimensions, ~130 MB downloaded once; set `AGENTKIT_ANALYTICS_FASTEMBED_CACHE_DIR` to a volume). |

No per-call cost either way. A circuit breaker stops calling a failing embedder for a minute; a search waits at
most 2 seconds for its query vector before falling back to words.

Search is brute force (`array_cosine_similarity` over the filtered rows), which is fast for the tens of
thousands of rows an agent's memory holds. DuckDB's `vss` extension (HNSW index) isn't used: it is downloaded at
run time and its index persistence is experimental.

## The API (memory admins)

```
GET  /v1/analytics                 status: counts, queue depth, dropped writes, embedder, exports, datasets
GET  /v1/analytics/episodes        ?hours=24&outcome=blocked&limit=100
POST /v1/analytics/search          {"query": "decline code 51", "kind": "lesson", "subject": null, "k": 10}
POST /v1/analytics/query           {"sql": "select ...", "target": "store" | "datasets", "max_rows": 200}
POST /v1/analytics/export          202: write the Parquet exports now
POST /v1/analytics/reindex         202: copy the stored lessons and subject events into semantic memory again
```

`target: store` queries this agent's `agent_episodes` and `semantic_memory`; `datasets` the configured Parquet
datasets. Errors: `422 sql_rejected` (with the reason), `504 query_timeout`, `429 query_busy`, `404` when there
are no datasets or no export location.

Lessons and subject events stored before analytics was turned on are copied in automatically the first time
each tenant's agent runs. People's own notes are indexed the next time each is written (their keys can't be
listed; they are also given to the model directly on every run).

Example questions:

```sql
-- slowest tools this week
select t.name, count(*) calls, avg(t.duration_ms) avg_ms, max(t.duration_ms) max_ms
from agent_episodes, unnest(from_json(tool_calls, '[{"name":"VARCHAR","duration_ms":"INTEGER"}]')) as u(t)
where started_at > now() - interval 7 day group by 1 order by avg_ms desc;

-- refusals by reason per day
select date_trunc('day', started_at) day, blocked_reason, count(*) from agent_episodes
where outcome = 'blocked' group by all order by day;
```

## Settings

| Setting | Default | |
| --- | --- | --- |
| `AGENTKIT_ANALYTICS_ENABLED` | `false` | |
| `AGENTKIT_ANALYTICS_DIR` | `data/analytics` | the file is `<dir>/<service name>.duckdb` |
| `AGENTKIT_ANALYTICS_CAPTURE` | `off` | `off` or `redacted` |
| `AGENTKIT_ANALYTICS_RETENTION_DAYS` | `30` | episodes |
| `AGENTKIT_ANALYTICS_MEMORY_LIMIT` / `_THREADS` | `256MB` / `2` | the store's DuckDB limits |
| `AGENTKIT_ANALYTICS_QUEUE_SIZE` | `10000` | writes waiting before new ones are dropped |
| `AGENTKIT_ANALYTICS_EMBEDDINGS` | `none` | `none`, `ollama`, `fastembed` |
| `AGENTKIT_ANALYTICS_EXPORT_URI` | | absolute folder or `s3://bucket/prefix` |
| `AGENTKIT_ANALYTICS_DATASETS` | | `name=uri,...` |

## On the VPS

The image installs `agentkit-memory[redis,duckdb]`, and `agentctl.py` gives every agent its own volume at
`/data/analytics`. Turn it on for every agent in `.env` (`ANALYTICS=true`, plus `ANALYTICS_CAPTURE`,
`ANALYTICS_EMBEDDINGS`, `OLLAMA_URL`, `ANALYTICS_EXPORT_URI`), or for one agent in its `agents.yaml` env
(`AGENTKIT_ANALYTICS_ENABLED: "true"`), then `python3 agentctl.py render` and rebuild that agent. `fastembed`
isn't in the VPS image (it adds ~300 MB); use Ollama there.

## Tested, and not yet

Tested in CI (`packages/agentkit-memory/tests/test_analytics.py`): eight threads writing while thirty reads run,
queue overflow, a bad write in a batch, retention across a restart, model changes, hybrid ranking with tenant and
person isolation, incremental exports to a folder and to S3 (moto), the SQL check against bypass attempts, the
locked connection refusing files directly, query timeouts, the Ollama client and its breaker (mocked HTTP), and
the whole path through `build_agent` and the API with a scripted model.

Not exercised here: a real Ollama server, real fastembed models, AWS S3 itself (moto stands in), and
multi-gigabyte datasets.
