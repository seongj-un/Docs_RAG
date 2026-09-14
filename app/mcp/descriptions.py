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


# ==========================================================================
# M7 W4 — 툴 설계 A/B 용 변형 문구
#
# 위의 두 상수는 **프로덕션이 읽는 것**이고, 아래는 실험에서만 읽는다. 같은
# 파일에 두는 이유는 이 모듈의 docstring 이 말한 그대로다 — 변형이 문자열
# 하나의 차이여야 실험이 변수 하나를 재게 된다. 어떤 변형이 어떤 실험의 어느
# 조건인지는 ``app/mcp/variants.py`` 가 한자리에 묶는다.
#
# 문구를 쓸 때 지킨 규칙 두 가지:
#
#   1. **사실 집합이 같아야 한다.** 아래 두 실험의 A/B 는 같은 것을 말하되
#      말하는 방식만 다르다. 한쪽에만 있는 사실이 하나라도 있으면 우리가 잰
#      것은 "문구 스타일"이 아니라 "정보량"이 된다.
#   2. **길이를 맞춘다.** 긴 쪽이 이기면 그게 프레이밍 때문인지 분량 때문인지
#      가를 수 없다. 재는 단위는 **단어 수**다 — 모델이 무는 것은 글자가 아니라
#      토큰이고, 영어 산문에서 단어 수가 토큰 수의 더 나은 대리값이다. 실험 2
#      의 두 문구는 135 대 141 단어이고, tests/test_mcp_variants.py 가 ±10% 를
#      넘지 않는지 붙잡는다.
# ==========================================================================


# --- 실험 2: description 문구 · 조건 A (기능 서술형) ----------------------
#
# "이 툴은 무엇을 하는가"만 적는다. 호출 시점에 대한 단서를 **의도적으로**
# 넣지 않았다 — 그것이 조건 B 가 더하는 유일한 것이다.
SEARCH_FUNCTIONAL_DESCRIPTION = """\
Performs hybrid retrieval (dense embeddings and lexical matching, then \
cross-encoder reranking) over the documents this user has uploaded to this \
service, and returns the matching passages ranked best first.

The operation stops at retrieval and produces no written answer. Each result \
carries the passage text, its `chunk_id`, the `document_id` it belongs to, its \
page range, and a cross-encoder `score` between 0 and 1 that is comparable \
only within one response. An empty result set means the indexed documents \
contain nothing that matches.

The `question` argument is the text the retriever matches on; it is built for \
natural-language questions rather than keyword lists, and it handles Korean, \
English, and mixtures of the two. The optional `document_id` argument narrows \
retrieval to a single document. The optional `max_results` argument sets how \
many passages come back, up to 20.\
"""

# --- 실험 2: description 문구 · 조건 B (사용 시점 명시형) -----------------
#
# 같은 사실을 "언제 부르는가"로 다시 쓴다. ⚠️ 부르지 **말아야** 할 때는 일부러
# 적지 않았다. Notion 의 가설이 "후자가 호출률이 높다"이므로, 금지 단서를 넣는
# 순간 우리는 그 가설이 아니라 "금지 문구가 호출을 줄이는가"를 재게 된다.
SEARCH_USE_WHEN_DESCRIPTION = """\
Use this when the user asks about something that would be written down in a \
document they uploaded to this service, and you want the passage rather than \
your own recollection.

Use it to gather material, not to get an answer: what comes back is the \
passage text, its `chunk_id`, the `document_id` it belongs to, its page range, \
and a `score` between 0 and 1 comparable within that one response. When \
nothing comes back, take that as "it is not in these documents".

When you call it, pass the user's question in `question` as they phrased it — \
the retriever is built for natural questions, not keyword lists, and works in \
Korean, English, and across both. Use `document_id` when you already know \
which document to look in, and `max_results` when you want more or fewer than \
the default, up to 20.\
"""


# --- 실험 1: 툴 분해 · 조건 B (search · fetch · list_collections) ---------
#
# 분해 조건의 문구는 프로덕션 문구와 **같은 문체**로 썼다. 분해하면서 문체까지
# 바꾸면 그 조건은 변수 둘을 동시에 바꾼 것이 된다. 각 툴의 문구는 프로덕션
# description 에서 그 툴에 해당하는 문장들을 가져오고, "이 툴이 무엇이 아닌지"
# 를 더한다 — 셋이 있을 때 가장 비싼 실패는 옆 툴을 부르는 것이라서다.

SPLIT_SEARCH_DESCRIPTION = """\
Search the user's uploaded documents and return the passages that best match a \
question, so you can answer from them and cite where the answer came from.

Returns ranked passages, not an answer, and not the documents themselves. Each \
result carries the passage text, its `chunk_id`, the `document_id` it belongs \
to, its page range, and a relevance `score` between 0 and 1 that is comparable \
only within one call. A top result below roughly 0.005 means nothing in the \
corpus is really about the question: treat that as "not in these documents".

Retrieval is hybrid (dense embeddings + lexical matching, then cross-encoder \
reranking) and works in Korean and English, including across the two. Pass the \
user's question as they asked it — do not translate it, strip it to keywords, \
or expand it into synonyms, all of which make the ranking worse.

This is the tool for finding text you have not seen yet. It is not the tool \
for re-reading a passage you already have an id for (`fetch`), nor for finding \
out which documents exist (`list_collections`).

Scope with `document_id` only when the user pointed at a specific document. \
Call this once per question and read the results before deciding to call it \
again: a call takes several seconds and counts against the user's daily query \
quota, the same budget their own questions spend.\
"""

SPLIT_FETCH_DESCRIPTION = """\
Fetch one passage in full by its `chunk_id`, exactly as it is stored.

Use it when you already hold a `chunk_id` — from an earlier `search` result or \
because the user quoted one — and you need the passage text itself rather than \
another ranked list. Returns the passage, the `document_id` it belongs to, and \
its page range.

This does no retrieval and spends no search quota: it is a direct read of one \
known passage, and it is cheap. An id that does not belong to this user is \
reported as not found rather than as a permission error.

If you do not have a `chunk_id`, this is the wrong tool — `search` is how you \
get one.\
"""

SPLIT_LIST_COLLECTIONS_DESCRIPTION = """\
List the documents this user has uploaded to this service, with the filename, \
the `document_id`, the page count, and how many passages each one was indexed \
into.

Use it when the user asks what is available, what you can see, or which \
documents you are working from — and when you need a `document_id` to narrow a \
later `search` but only know the document by name.

It reads nothing from inside the documents: it answers "which documents exist", \
never "what do they say". For the latter, use `search`.

Takes no arguments and spends no search quota.\
"""


# --- 실험 3: 파라미터 스키마 · 코드만 (예산이 생기면 실행) ---------------
#
# ⚠️ 아래 둘은 **한 번도 측정하지 않았다.** 사용자의 예산 결정으로 W4 는 실험
# 둘(툴 분해·문구)만 실제로 돌렸다. 문구를 미리 써 두는 이유는 예산이 생겼을 때
# 코드를 다시 설계하지 않고 조건 이름만 넘기면 되게 하기 위해서다.
#
# 조건 A 는 좁힐 방법이 **아예 없는** 스키마다. 프로덕션의 `document_id` 를
# 남겨 두면 "enum 대 UUID"를 재게 되는데, Notion 의 가설은 "좁힐 손잡이가
# 열거형일 때 범위 좁히기가 성공하는가"이므로 대조군은 손잡이가 없는 쪽이다.
SEARCH_QUERY_ONLY_DESCRIPTION = """\
Search the user's uploaded documents and return the passages that best match a \
question, so you can answer from them and cite where the answer came from.

Returns ranked passages, not an answer. Each result carries the passage text, \
its `chunk_id`, the `document_id` it belongs to, its page range, and a \
relevance `score` between 0 and 1, comparable only within one call.

The search always covers every document this user has uploaded. Pass the \
user's question as they asked it — do not translate it or strip it to \
keywords.\
"""

SEARCH_COLLECTION_ENUM_DESCRIPTION = """\
Search the user's uploaded documents and return the passages that best match a \
question, so you can answer from them and cite where the answer came from.

Returns ranked passages, not an answer. Each result carries the passage text, \
its `chunk_id`, the `document_id` it belongs to, its page range, and a \
relevance `score` between 0 and 1, comparable only within one call.

Set `collection` to one of the listed document names to search only that \
document; omit it to search all of them. Pass the user's question as they \
asked it — do not translate it or strip it to keywords.\
"""


# --- 분해 조건이 마운트됐을 때의 서버 안내문 -----------------------------
#
# 하네스는 이 문자열을 읽지 않는다(``server/discover`` 를 부르지 않는다 — 근거는
# eval/l3.py 의 시스템 프롬프트 주석). 그럼에도 채워 두는 이유는, 분해 변형을
# 실제로 띄웠을 때 안내문이 "There is exactly one tool" 이라고 거짓말을 하면 안
# 되기 때문이다. 변형은 실험용이어도 서버로서 앞뒤가 맞아야 한다.
SPLIT_SERVER_INSTRUCTIONS = """\
Retrieval over one user's own uploaded documents (PDFs indexed by this Docs \
Q&A service). Authentication is a session id from this service carried as \
`Authorization: Bearer <session_id>`; every result is scoped to the account \
that owns that session, and there is no way to reach another account's \
documents through this server.

There are three tools. `list_collections` says which documents exist, `search` \
finds passages that match a question, and `fetch` re-reads one passage by id. \
All three stop at retrieval: none of them writes an answer, and composing the \
answer and the citation is the calling agent's job.

This server cannot upload or delete documents, and cannot answer questions \
about documents the user has not uploaded here. For those, use the web \
application.\
"""


# ==========================================================================
# M7 W6 — 생성 위치 비교 (모드 A: 서버 생성)
#
# Notion W6 의 표에서 모드 A 는 "서버 내 Gemini 가 답을 쓰고, 인용 형식과 거부
# 가드레일을 서버가 통제한다". 그 통제가 실제로 존재한다는 것을 에이전트에게
# **말해 주는 것**이 아래 문구의 일이다 — 에이전트가 이 툴의 답을 자기 말로
# 다시 쓰면(relay) 서버가 건 가드레일은 그 지점에서 끝나기 때문이다. 그래서
# 문구는 "받은 답을 그대로 전하라"를 명시적으로 요구한다. 그렇게 요구해도
# 실제로 그러는지는 측정 대상이고, W6 비교 러너가 그것을 잰다.
#
# 길이를 search_documents 와 맞추려 하지 않았다. 이것은 문구 A/B(실험 2)가
# 아니라 **툴이 하는 일 자체가 다른** 조건이고, 두 문구를 같은 길이로 깎으면
# 각자 말해야 할 사실 중 하나가 빠진다.
# ==========================================================================

ANSWER_QUESTION_DESCRIPTION = """\
Answer a question from the user's own uploaded documents. This tool does the \
retrieval **and** writes the answer, and returns that answer together with the \
passages it was written from.

Returns an `answer` string, a `refused` flag, and a list of `citations` (each \
with the `document_id`, the page range, and the snippet that was actually used \
to write the answer). The answer is written only from those passages; the \
service refuses rather than filling a gap from general knowledge, and when it \
refuses, `refused` is true and `citations` is empty.

Relay the `answer` to the user as it is written, including its `[p.N]` page \
markers, and present the `citations` as the sources. Do not rewrite it from \
your own knowledge and do not drop the page markers: the grounding and the \
refusal guarantee belong to the text as returned, and a rewrite carries \
neither. If `refused` is true, tell the user the documents do not contain the \
answer instead of answering it yourself.

Retrieval is hybrid (dense embeddings + lexical matching, then cross-encoder \
reranking) and works in Korean and English, including across the two. Pass the \
user's question as they asked it — do not translate it, strip it to keywords, \
or expand it into synonyms, all of which make the ranking worse.

Scope with `document_id` only when the user pointed at a specific document. \
Leaving it unset covers everything they have uploaded, which is usually what \
you want.

Call this once per question. It is the expensive tool on this server: it pays \
for reranking *and* for a generation call, and it counts against the user's \
daily query quota, the same budget their own questions spend. Read the answer \
before deciding to call it again, and ask again only when the first answer \
shows the question itself was wrong.\
"""

# 모드 A 변형이 마운트됐을 때의 서버 안내문. SPLIT_SERVER_INSTRUCTIONS 와 같은
# 이유로 채운다 — 변형이 실험용이어도 안내문이 툴 목록에 대해 거짓말을 하면
# 안 된다. 여기서 바뀌는 문단은 "무엇을 돌려주는가" 하나뿐이다.
ANSWER_SERVER_INSTRUCTIONS = """\
Question answering over one user's own uploaded documents (PDFs indexed by \
this Docs Q&A service). Authentication is a session id from this service \
carried as `Authorization: Bearer <session_id>`; every result is scoped to the \
account that owns that session, and there is no way to reach another account's \
documents through this server.

There is exactly one tool, `answer_question`, and it goes all the way to an \
answer: it returns the written answer, whether the service refused for lack of \
grounding, and the passages the answer was written from. Relaying that answer \
and its citations is the calling agent's job; rewriting it is not.

This server cannot list, upload, or delete documents, and cannot answer \
questions about documents the user has not uploaded here. For those, use the \
web application.\
"""
