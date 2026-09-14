"""Text an agent reads: the server instructions and the tool description.

Kept apart from the code that registers them because **the tool description is
a prompt**, and W4 is going to A/B it. A variant should be a change to a string
in this module and nothing else — if a variant ever requires touching
``tools.py``, the experiment is measuring two changes at once.

Everything asserted here is either a measured number from ``app/config.py`` or
a property of the code in ``tools.py``. An agent cannot check these claims, so
a stale one is a lie we told it — when the pipeline changes, this text is part
of the change.
"""

# server/discover 가 그대로 돌려준다. 에이전트가 이 서버를 처음 만났을 때
# 읽는 유일한 안내문이므로, "무엇이 있는지"보다 "무엇이 없는지"를 먼저
# 적는다 — 없는 것을 찾다 포기하는 것이 가장 비싼 실패다.
SERVER_INSTRUCTIONS = """\
Retrieval over one user's own uploaded documents (PDFs indexed by this Docs \
Q&A service). Authentication is a session id from this service carried as \
`Authorization: Bearer <session_id>`; every result is scoped to the account \
that owns that session, and there is no way to reach another account's \
documents through this server.

There is exactly one tool, `search_documents`, and it stops at retrieval: it \
returns the matching passages and their page numbers, never a written answer. \
Composing the answer and the citation is the calling agent's job.

This server cannot list, upload, or delete documents, and cannot answer \
questions about documents the user has not uploaded here. For those, use the \
web application.\
"""

# 툴 description. MCP SDK 는 description= 인자를 함수 docstring 보다 우선
# 하므로, 여기 적힌 것이 곧 모델이 보는 전부다.
#
# 길이는 의도적이다. 이 툴은 8.8초가 걸리고 하루 쿼터를 깎는데(근거:
# app/config.py 의 cand_k 주석, tools.py 의 쿼터 주석), 그걸 모르는 에이전트는
# 질문 하나를 다섯 번 쪼개 던진다. "언제 부르지 말아야 하는가"를 적지 않으면
# 그 비용은 전부 사용자가 낸다.
SEARCH_DOCUMENTS_DESCRIPTION = """\
Search the user's own uploaded documents and return the passages that best \
match a question, so you can answer from them and cite where the answer came \
from.

Returns ranked passages, not an answer. Each result carries the passage text, \
its `chunk_id`, the `document_id` it belongs to, its page range, and a \
relevance `score`. Write the answer yourself from the passages, and cite it \
with the document and page range you used. If the passages do not contain the \
answer, say so instead of filling the gap from your own knowledge — the whole \
point of this tool is that the answer is grounded in the user's documents.

`score` is a cross-encoder relevance value between 0 and 1. Scores are only \
comparable within one call. A top result below roughly 0.005 means nothing in \
the corpus is really about the question: treat that as "not in these \
documents" rather than as a weak but usable match.

Retrieval is hybrid (dense embeddings + lexical matching, then cross-encoder \
reranking) and works in Korean and English, including across the two. Pass the \
user's question as they asked it — do not translate it, strip it to keywords, \
or expand it into a list of synonyms, all of which make the ranking worse.

Scope with `document_id` only when the user pointed at a specific document. \
Leaving it unset searches everything they have uploaded, which is usually what \
you want; a wrong `document_id` is reported as not found rather than silently \
widening.

Call this once per question and read the results before deciding to call it \
again. A call takes several seconds (reranking dominates) and counts against \
the user's daily query quota, the same budget their own questions spend, so \
fanning one question out into several searches is directly expensive to them. \
Ask again only when the first results show you were looking for the wrong \
thing.\
"""
