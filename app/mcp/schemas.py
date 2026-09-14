"""Structured input/output for the MCP tools.

Separate from ``app/schemas.py`` because these models are read by a language
model, not by our own frontend. Every ``description=`` here ships in the tool's
JSON Schema and is part of the prompt: field names an agent has to guess at are
what turn into wrong ``document_id`` arguments and invented citations.

The shape deliberately matches ``schemas.Citation`` (chunk id, document id,
page range) so that an agent's citation and the web UI's citation point at the
same thing — with ``content`` carrying the whole passage rather than
``Citation.snippet``'s 240-character cut, since the agent has to answer from it
and not merely display it.
"""

import uuid

from pydantic import BaseModel, Field


class SearchHit(BaseModel):
    """One retrieved passage, with everything needed to cite it."""

    chunk_id: uuid.UUID = Field(
        description="Stable id of this passage. Use it to refer to the exact "
        "passage you used; it is not a page number."
    )
    document_id: uuid.UUID = Field(
        description="The document this passage came from. Pass it back as "
        "`document_id` to restrict a later search to this document."
    )
    page_from: int | None = Field(
        default=None,
        description="First page of the passage in the original PDF, 1-based. "
        "Null when the document has no page information.",
    )
    page_to: int | None = Field(
        default=None,
        description="Last page of the passage. Equal to `page_from` when the "
        "passage sits on a single page.",
    )
    score: float = Field(
        description="Cross-encoder relevance, 0 to 1, higher is better. "
        "Comparable only against the other hits in the same response."
    )
    content: str = Field(
        description="The full text of the passage. Answer from this text only."
    )


class SearchDocumentsResult(BaseModel):
    """What ``search_documents`` returns.

    A wrapper object rather than a bare list: the SDK would otherwise wrap a
    list as ``{"result": [...]}`` in the structured output, which names nothing
    and leaves no room to say anything *about* the results. ``hits`` being
    empty is a real, common outcome that an agent must be able to tell apart
    from a failure, so it gets said out loud rather than inferred from an empty
    array.
    """

    hits: list[SearchHit] = Field(
        description="Matching passages, best first. Empty when the user's "
        "documents contain nothing relevant to the question — that is an "
        "answer ('not in these documents'), not an error to retry."
    )
    searched_document_id: uuid.UUID | None = Field(
        default=None,
        description="The document the search was restricted to, or null if it "
        "covered every document the user has uploaded.",
    )


# --- M7 W4: 분해 변형(search · fetch · list_collections)이 쓰는 모양 -------
#
# 프로덕션은 이 둘을 쓰지 않는다. 그래도 같은 파일에 있는 이유는 위의 두 모델과
# 같은 제약을 받기 때문이다 — 여기 적는 ``description=`` 은 전부 모델이 읽는
# 프롬프트다. ``Passage`` 가 ``SearchHit`` 에서 ``score`` 만 뺀 모양인 것도
# 의도다: 같은 청크를 검색으로 만나든 id 로 다시 읽든 필드 이름이 같아야
# 에이전트가 둘을 같은 것으로 알아본다.


class Passage(BaseModel):
    """One passage read back by id, with no ranking attached."""

    chunk_id: uuid.UUID = Field(
        description="Stable id of this passage — the same id you passed in."
    )
    document_id: uuid.UUID = Field(
        description="The document this passage came from."
    )
    page_from: int | None = Field(
        default=None,
        description="First page of the passage in the original PDF, 1-based.",
    )
    page_to: int | None = Field(
        default=None, description="Last page of the passage."
    )
    content: str = Field(
        description="The full text of the passage. Answer from this text only."
    )


class DocumentSummary(BaseModel):
    """One document the user has uploaded."""

    document_id: uuid.UUID = Field(
        description="Pass this back as `document_id` to restrict a search to "
        "this document."
    )
    filename: str = Field(
        description="The name the user uploaded the file under. This is the "
        "only name they will recognise it by."
    )
    pages: int | None = Field(
        default=None,
        description="Number of pages in the original PDF, or null if unknown.",
    )
    passages: int = Field(
        description="How many passages this document was indexed into. A "
        "rough measure of how much text it holds, not of its relevance."
    )


class ListCollectionsResult(BaseModel):
    """What ``list_collections`` returns."""

    documents: list[DocumentSummary] = Field(
        description="Every document this user has uploaded and indexed, "
        "newest first. Empty means they have uploaded nothing yet — say so "
        "rather than searching."
    )


# --- M7 W6: 서버 생성 변형(answer_question)이 쓰는 모양 -------------------
#
# 프로덕션은 이 둘을 쓰지 않는다. ``AnswerCitation`` 이 ``schemas.Citation``
# (웹 UI 가 받는 것)과 필드가 같은 것은 이 파일 docstring 의 규칙 그대로다 —
# 에이전트가 보여줄 인용과 웹 UI 가 보여줄 인용이 같은 것을 가리켜야 한다.
# ``SearchHit`` 과 달리 ``score`` 가 없는 이유는 이 응답에서 순위가 이미
# 소비됐기 때문이다: 답이 그 청크들로부터 쓰였다는 사실이 남을 뿐, 에이전트가
# 다시 고를 것이 없다.


class AnswerCitation(BaseModel):
    """One passage the answer was actually written from."""

    chunk_id: uuid.UUID = Field(
        description="Stable id of the passage this citation points at."
    )
    document_id: uuid.UUID = Field(
        description="The document the passage came from."
    )
    page_from: int | None = Field(
        default=None,
        description="First page of the passage in the original PDF, 1-based. "
        "This is the number the answer's `[p.N]` markers refer to.",
    )
    page_to: int | None = Field(
        default=None, description="Last page of the passage."
    )
    snippet: str = Field(
        description="The opening of the passage, for showing the user where "
        "the answer came from. It is an excerpt, not the whole passage."
    )


class AnswerQuestionResult(BaseModel):
    """What ``answer_question`` returns: the answer, and what backs it."""

    answer: str = Field(
        description="The answer, written from the user's documents only, with "
        "`[p.N]` markers naming the pages it came from. Relay it as written."
    )
    refused: bool = Field(
        description="True when the documents did not contain the answer and "
        "the service declined to invent one. `answer` then says so and "
        "`citations` is empty — tell the user that, rather than answering "
        "from your own knowledge."
    )
    citations: list[AnswerCitation] = Field(
        description="The passages the answer was written from, best first. "
        "Empty when `refused` is true."
    )
    searched_document_id: uuid.UUID | None = Field(
        default=None,
        description="The document the search was restricted to, or null if it "
        "covered every document the user has uploaded.",
    )
