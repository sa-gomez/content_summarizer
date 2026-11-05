"""Ingest two fixture documents (twice each, to prove idempotency)."""
import store

DOCS = [
    ("vidA", "Claude Code tips",
     "We talk about using subagents and worktrees to parallelize coding work. "
     "Worktrees isolate changes so several agents can edit code at once without conflicts. " * 20),
    ("vidB", "Sourdough baking",
     "Feed your starter daily, fold the dough gently, and bake in a hot dutch oven "
     "for a crisp crust. Hydration and fermentation time matter a lot. " * 20),
]

with store.connect() as c:
    for ext, title, text in DOCS:
        for _ in range(2):
            print(ext, store.upsert_document(
                c, source_type="youtube", external_id=ext, text=text, title=title,
                segments=[(0.0, text[:50]), (5.0, text[50:100])],
                source_url="https://youtu.be/" + ext, author="fixture"))
