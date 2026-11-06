# AGENTS.md

Guidance for AI coding agents (and humans) working in this repo.

## Where the work is tracked

**Linear is the source of truth for tasks:**
https://linear.app/sagomez-projects/project/content-summarizer-b3d7dbf29c0e/overview

Check the project for open issues before starting work, and put new work there rather than
in TODO comments or this file. Reference the Linear issue ID in branch names and PR
descriptions.

## What this project is

Ingests transcripts (YouTube today; podcasts and others planned), stores the raw text in
Postgres with metadata, generates LLM summaries of several lengths via background workers,
and supports hybrid keyword + semantic search. GitHub: https://github.com/sa-gomez/content_summarizer

Design and rationale: [docs/storage-and-search.md](docs/storage-and-search.md). Read it
before changing the schema, worker or search.

## Layout

| Path | Purpose |
|---|---|
| `transcribe.py` | CLI: YouTube URL -> transcript (captions, Whisper fallback). `--store` saves to Postgres. Runs on the host via `uv` (PEP 723 deps). |
| `store.py` | Postgres helpers: `connect`, `upsert_document`, `enqueue_job` |
| `worker.py` | Job worker (`summarize`, `embed`) using `FOR UPDATE SKIP LOCKED` |
| `search.py` | Hybrid search CLI (keyword + pgvector, reciprocal rank fusion) |
| `db/migrations/001_init.sql` | Schema; mounted into the Postgres container's init dir |
| `docker-compose.yml`, `Dockerfile` | `db` (pgvector), `worker`, and `app` (one-off CLI, profile `tools`) |
| `tests/ingest_fixture.py` | Fixture ingest used for manual end-to-end checks |

## Running things

```
docker compose up -d db worker                      # Postgres + worker
uv run transcribe.py URL --store                    # ingest (needs uv on the host)
docker compose run --rm app python search.py "q"    # search
docker compose run --rm app python worker.py --once # drain the queue and exit
docker compose down -v                              # tear down AND delete data
```

Env vars: `DATABASE_URL`, `ANTHROPIC_API_KEY`, `SUMMARIZER=stub|claude`,
`EMBEDDER=stub|fastembed`. Use the stubs for offline plumbing tests; the stub embedder is
not semantic.

## Invariants (do not break)

- **`documents.text` is immutable.** Raw transcripts are the irreplaceable data. Anything
  else (summaries, chunks, embeddings) must stay regenerable from it.
- **Every summary points to its source** via `summaries.document_id` (and
  `metadata.source_document_id`).
- **Jobs must be idempotent.** Enqueue with a `dedupe_key`; handlers may run more than once
  (stale-lock reclaim, retries).
- **The embedding dimension (384) is fixed in the schema.** Changing the embedding model means
  a new column/table and re-embedding all chunks.
- Schema changes go in a new numbered file under `db/migrations/`. The init directory only
  runs on a fresh database, so existing databases need the file applied by hand until a
  migration runner exists.

## Conventions

- Keep `transcribe.py` importing `store` lazily so it works without a database.
- Commit messages: short imperative summary, then a body explaining why.
- Verify changes end to end where you can (ingest -> worker -> search) and state plainly what
  was not tested.
