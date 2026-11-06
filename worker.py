"""Job worker: claims jobs from Postgres (SKIP LOCKED) and runs summarize / embed.

Run several in parallel: docker compose up --scale worker=3
"""
import hashlib
import os
import socket
import sys
import time
import traceback

from psycopg.types.json import Jsonb

import store

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
MAX_ATTEMPTS = 5
STALE_AFTER = "10 minutes"  # a 'running' job older than this is presumed orphaned
PROMPT_VERSION = store.PROMPT_VERSION
CHUNK_CHARS, CHUNK_OVERLAP = 1500, 200
MAP_CHARS = 60_000  # transcripts longer than this are summarized map-reduce style

MODELS = {"micro": "claude-haiku-5-5", "short": "claude-sonnet-5-5", "medium": "claude-sonnet-5-5"}

CLAIM_SQL = """
UPDATE jobs SET status='running', locked_by=%s, locked_at=now(), attempts=attempts+1
WHERE id = (
  SELECT id FROM jobs
  WHERE (status='pending' AND run_after <= now())
     OR (status='running' AND locked_at < now() - %s::interval)
  ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING id, type, payload, attempts
"""


# ---------- summarizer / embedder backends ----------

def _claude(model, system, user, max_tokens):
    import anthropic

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    # stream: medium summaries of very long podcasts need max_tokens too large for a plain call
    with client.messages.stream(
        model=model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}],
    ) as stream:
        msg = stream.get_final_message()
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def _stub_llm(model, system, user, max_tokens):
    return "STUB SUMMARY: " + " ".join(user.split()[:40])


def llm(model, system, user, max_tokens):
    fn = _stub_llm if os.environ.get("SUMMARIZER") == "stub" else _claude
    return fn(model, system, user, max_tokens)


_embedder = None


def embed(texts):
    """Return a list of 384-dim vectors."""
    if os.environ.get("EMBEDDER") == "stub":
        out = []
        for t in texts:  # deterministic hash-bag-of-words, enough to test plumbing
            v = [0.0] * 384
            for w in t.lower().split():
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 384] += 1.0
            n = sum(x * x for x in v) ** 0.5 or 1.0
            out.append([x / n for x in v])
        return out
    global _embedder
    if _embedder is None:
        from fastembed import TextEmbedding

        _embedder = TextEmbedding("BAAI/bge-small-en-v1.5")
    return [list(map(float, v)) for v in _embedder.embed(texts)]


def vec_literal(v):
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


# ---------- helpers ----------

def chunk_text(text):
    chunks, i = [], 0
    while i < len(text):
        end = min(i + CHUNK_CHARS, len(text))
        if end < len(text):  # prefer to break on whitespace
            ws = text.rfind(" ", i + CHUNK_CHARS // 2, end)
            end = ws if ws != -1 else end
        chunks.append(text[i:end].strip())
        if end >= len(text):
            break
        i = max(end - CHUNK_OVERLAP, i + 1)
    return [c for c in chunks if c]


def _max_tokens(words):
    return int(words * 2) + 256  # ~1.3 tokens/word plus headroom


def summarize_text(text, kind, title):
    """Summarize to SUMMARY_KINDS[kind] of the source length. Returns (model, summary, target_words)."""
    words = store.target_words(text, kind)
    model = MODELS[kind]
    system = ("You summarize transcripts faithfully. Output only the summary text, "
              f"about {words} words (a target; stay within 20% of it), no preamble.")
    if len(text) > MAP_CHARS:
        # map: summarize each section at this kind's ratio so the reduce step keeps enough
        # detail; reduce: summarize the concatenated section summaries to the final target.
        ratio = store.SUMMARY_KINDS[kind]
        pieces = [text[i:i + MAP_CHARS] for i in range(0, len(text), MAP_CHARS)]
        partials = []
        for p in pieces:
            pw = max(10, round(len(p.split()) * ratio))
            partials.append(llm(MODELS["medium"],
                                f"Summarize this transcript section in about {pw} words. "
                                "Output only the summary.", p, _max_tokens(pw)))
        text_for_llm = "\n\n".join(partials)
    else:
        text_for_llm = text
    return model, llm(model, system, f"Title: {title}\n\nTranscript:\n{text_for_llm}",
                      _max_tokens(words)), words


# ---------- job handlers ----------

def do_summarize(conn, payload):
    doc_id, kind = payload["document_id"], payload["kind"]
    title, text = conn.execute("SELECT title, text FROM documents WHERE id=%s", (doc_id,)).fetchone()
    model, summary, target = summarize_text(text, kind, title)
    row = conn.execute(
        """INSERT INTO summaries (document_id, kind, target_words, model, prompt_version, text, metadata)
           VALUES (%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (document_id, kind, model, prompt_version) DO NOTHING RETURNING id""",
        (doc_id, kind, target, model, PROMPT_VERSION, summary,
         Jsonb({"source_document_id": doc_id, "source_chars": len(text),
               "source_words": len(text.split()), "ratio": store.SUMMARY_KINDS[kind]})),
    ).fetchone()
    if row:
        store.enqueue_job(conn, "embed", {"summary_id": row[0]}, f"embed:summary:{row[0]}")


def do_embed(conn, payload):
    if "summary_id" in payload:
        sid = payload["summary_id"]
        doc_id, text = conn.execute(
            "SELECT document_id, text FROM summaries WHERE id=%s", (sid,)).fetchone()
        conn.execute("DELETE FROM chunks WHERE summary_id=%s", (sid,))
    else:
        sid, doc_id = None, payload["document_id"]
        text = conn.execute("SELECT text FROM documents WHERE id=%s", (doc_id,)).fetchone()[0]
        conn.execute("DELETE FROM chunks WHERE document_id=%s AND summary_id IS NULL", (doc_id,))
    pieces = chunk_text(text)
    vecs = embed(pieces)
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO chunks (document_id, summary_id, ordinal, text, embedding) "
            "VALUES (%s,%s,%s,%s,%s::vector)",
            [(doc_id, sid, i, t, vec_literal(v)) for i, (t, v) in enumerate(zip(pieces, vecs))],
        )


HANDLERS = {"summarize": do_summarize, "embed": do_embed}


# ---------- loop ----------

def run_one(conn):
    """Claim and process one job. Returns False if the queue is empty."""
    row = conn.execute(CLAIM_SQL, (WORKER_ID, STALE_AFTER)).fetchone()
    conn.commit()  # claim is visible/durable before we start work
    if not row:
        return False
    job_id, type_, payload, attempts = row
    print(f"[{WORKER_ID}] job {job_id} {type_} {payload} (attempt {attempts})", flush=True)
    try:
        HANDLERS[type_](conn, payload)
        conn.execute("UPDATE jobs SET status='done', error=NULL WHERE id=%s", (job_id,))
    except Exception:
        conn.rollback()
        err = traceback.format_exc()
        print(err, file=sys.stderr, flush=True)
        if attempts >= MAX_ATTEMPTS:
            conn.execute("UPDATE jobs SET status='failed', error=%s WHERE id=%s", (err, job_id))
        else:
            backoff = f"{30 * 2 ** (attempts - 1)} seconds"
            conn.execute("UPDATE jobs SET status='pending', error=%s, "
                         "run_after=now()+%s::interval WHERE id=%s", (err, backoff, job_id))
    conn.commit()
    return True


def main():
    once = "--once" in sys.argv  # drain the queue then exit (handy for tests)
    while True:
        try:
            conn = store.connect()
            break
        except Exception as e:
            print(f"waiting for db: {e}", flush=True)
            time.sleep(2)
    with conn:
        while True:
            if not run_one(conn):
                if once:
                    return
                time.sleep(2)


if __name__ == "__main__":
    main()
