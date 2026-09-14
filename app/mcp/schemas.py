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
