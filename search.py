"""Hybrid (keyword + semantic) search over transcripts and summaries.

  python search.py "query" [--kind summary|transcript|short|medium|long] [--source youtube]
                   [--limit 10] [--mode hybrid|keyword|semantic]

Keyword rank (ts_rank) and vector rank (pgvector cosine) are merged with
reciprocal rank fusion. Every hit carries the pointer back to its document.
"""
import argparse

import store
import worker

RRF_K = 60

SQL = """
WITH base AS (
  SELECT c.id, c.document_id, c.summary_id, c.text, c.tsv, c.embedding,
         s.kind AS summary_kind
  FROM chunks c
  JOIN documents d ON d.id = c.document_id
  LEFT JOIN summaries s ON s.id = c.summary_id
  WHERE (%(source)s::text IS NULL OR d.source_type = %(source)s)
    AND (%(kind)s::text IS NULL
         OR (%(kind)s = 'summary' AND c.summary_id IS NOT NULL)
         OR (%(kind)s = 'transcript' AND c.summary_id IS NULL)
         OR s.kind = %(kind)s)
),
kw AS (
  SELECT id, row_number() OVER (ORDER BY ts_rank(tsv, q) DESC) AS r
  FROM base, websearch_to_tsquery('english', %(q)s) q
  WHERE %(use_kw)s AND tsv @@ q
  ORDER BY ts_rank(tsv, q) DESC LIMIT 50
),
sem AS (
  SELECT id, row_number() OVER (ORDER BY embedding <=> %(vec)s::vector) AS r
  FROM base WHERE %(use_sem)s
  ORDER BY embedding <=> %(vec)s::vector LIMIT 50
),
fused AS (
  SELECT id, sum(1.0 / (%(k)s + r)) AS score
  FROM (SELECT * FROM kw UNION ALL SELECT * FROM sem) u
  GROUP BY id
)
SELECT f.score, b.document_id, b.summary_id, b.summary_kind, d.title, d.source_url, b.text
FROM fused f
JOIN base b ON b.id = f.id
JOIN documents d ON d.id = b.document_id
ORDER BY f.score DESC LIMIT %(limit)s
"""


def search(conn, q, *, kind=None, source=None, limit=10, mode="hybrid"):
    vec = worker.vec_literal(worker.embed([q])[0]) if mode != "keyword" else None
    cur = conn.execute(SQL, {
        "q": q, "vec": vec, "kind": kind, "source": source, "limit": limit, "k": RRF_K,
        "use_kw": mode in ("hybrid", "keyword"), "use_sem": mode in ("hybrid", "semantic"),
    })
    return cur.fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--kind")
    ap.add_argument("--source")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--mode", default="hybrid", choices=["hybrid", "keyword", "semantic"])
    a = ap.parse_args()
    with store.connect() as conn:
        for score, doc_id, sum_id, skind, title, url, text in search(
                conn, a.query, kind=a.kind, source=a.source, limit=a.limit, mode=a.mode):
            what = f"summary#{sum_id} ({skind})" if sum_id else "transcript"
            print(f"{score:.4f}  doc#{doc_id} {what}  {title}\n    {url}\n    {text[:200].strip()!r}\n")


if __name__ == "__main__":
    main()
