"""Draft/subject storage used by the ledger flow and the commit executor (docs/10 §4–5).

Subjects are immutable per version: ``put`` refuses to overwrite a reference with different content (an edit is a
new version → new verification → new approval, H-4). Production: AlloyDB ``drafts.*`` tables behind ``mcp-store``.
"""

from __future__ import annotations

import threading
from typing import Protocol

from pydantic import BaseModel

from pm_agent.grounding.verifier import SUBJECT_TYPES, subject_digest, subject_id


class SubjectStore(Protocol):
    def put(self, subject_type: str, subject: BaseModel) -> str: ...

    def get(self, ref: str) -> tuple[str, BaseModel]: ...


def subject_ref(subject_type: str, subject: BaseModel) -> str:
    """``<type>:<id>:v<version>`` — stable reference recorded on the ``ProposedAction``."""
    version = getattr(subject, "version", None)
    return f"{subject_type}:{subject_id(subject)}:v{version if isinstance(version, int) else 1}"


class InMemorySubjectStore:
    def __init__(self) -> None:
        self.items: dict[str, tuple[str, BaseModel]] = {}
        self._lock = threading.Lock()

    def put(self, subject_type: str, subject: BaseModel) -> str:
        """Store a subject version; re-putting identical content is idempotent, different content is refused."""
        if subject_type not in SUBJECT_TYPES:
            raise ValueError(f"unknown subject type {subject_type!r}")
        ref = subject_ref(subject_type, subject)
        with self._lock:
            existing = self.items.get(ref)
            if existing is not None and subject_digest(existing[1]) != subject_digest(subject):
                raise ValueError(f"{ref} already exists with different content — save a new version instead")
            self.items[ref] = (subject_type, subject)
        return ref

    def get(self, ref: str) -> tuple[str, BaseModel]:
        try:
            return self.items[ref]
        except KeyError:
            raise LookupError(f"unknown subject {ref}") from None
