"""Labeled eval corpora.

Each module exposes ``CLAUSES`` (one clause per page, page = index + 1) and
``QUERIES`` as ``(question, gold_page, query_type)``.
"""

import importlib
from types import ModuleType

_AVAILABLE = ("simple", "hard", "longchunk", "wide")


def load(name: str) -> ModuleType:
    if name not in _AVAILABLE:
        raise ValueError(f"unknown corpus {name!r}; choose from {_AVAILABLE}")
    return importlib.import_module(f"eval.corpora.{name}")
