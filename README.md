# YouTube transcripts

    uv run transcribe.py URL [URL ...]            # captions first, Whisper fallback
    uv run transcribe.py URL --force-whisper --model small
    uv run transcribe.py URL --timestamps --lang en --out transcripts

Output: one Markdown file per video in `transcripts/`.
Models: tiny/base/small/medium/large-v3/turbo (bigger = slower, more accurate).

Dependencies are declared at the top of `transcribe.py` (PEP 723); `uv run`
builds a cached environment from them on first use, so there is no venv to
create or activate. Install uv: https://docs.astral.sh/uv/

## Storing, summarizing and searching (Postgres)

Transcripts can be stored in Postgres (pgvector) with LLM summaries and hybrid
keyword + semantic search. Raw text is immutable; summaries/chunks/embeddings are derived.

    docker compose up -d db worker          # Postgres + job worker (scale: --scale worker=3)
    export ANTHROPIC_API_KEY=...            # workers use Claude for summaries
    uv run transcribe.py URL --store        # also writes .md; add --no-md to skip it
    docker compose run --rm app python search.py "query" [--kind summary|transcript|short|medium|long] [--mode hybrid|keyword|semantic]

Schema: `db/migrations/001_init.sql` (documents, segments, summaries -> document_id,
chunks, jobs). `--store` reads `DATABASE_URL` (default: the compose db on localhost:5432);
point it at any managed Postgres with the pgvector extension to host it.
Set `SUMMARIZER=stub` / `EMBEDDER=stub` for offline plumbing tests.
Fixture test: `docker compose run --rm app python tests/ingest_fixture.py`, then `python worker.py --once`.
