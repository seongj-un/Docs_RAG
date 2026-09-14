"""Retrieval evaluation harness.

Measures retrieval quality (not generation) for a labeled query set against a
known corpus, so retrieval changes can be compared with numbers instead of
anecdotes. Used for the M2 D18 A/B experiment; intended to carry into the M4
evaluation loop.

M7 W3 adds a second layer on top of those one-off experiment runners:
``eval.harness`` takes a jsonl golden set, scores chunk-level L1 metrics,
stores the run in Postgres and diffs it against the previous one. The older
modules answer "which configuration is better"; the harness answers "did this
commit make it worse".
"""
