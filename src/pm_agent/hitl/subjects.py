"""Draft/subject storage used by the ledger flow and the commit executor (docs/10 §4–5).

Subjects are immutable per version: ``put`` refuses to overwrite a reference with different content (an edit is a
new version → new verification → new approval, H-4). Production: AlloyDB ``drafts.*`` tables behind ``mcp-store``.
"""

from __future__ import annotations

import threading
from typing import Protocol

from pydantic import BaseModel

from pm_agent.schemas.subjects import SUBJECT_TYPES, subject_digest, subject_id


class SubjectStore(Protocol):
    def put(self, subject_type: str, subject: BaseModel) -> str: ...

    def get(self, ref: str) -> tuple[str, BaseModel]: ...

    def versions(self, subject_type: str, identifier: str) -> list[BaseModel]: ...


def format_subject_ref(subject_type: str, identifier: str, version: int = 1) -> str:
    return f"{subject_type}:{identifier}:v{version}"


def subject_ref(subject_type: str, subject: BaseModel) -> str:
    """``<type>:<id>:v<version>`` — stable reference recorded on the ``ProposedAction``."""
    version = getattr(subject, "version", None)
    return format_subject_ref(subject_type, subject_id(subject), version if isinstance(version, int) else 1)


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

    def versions(self, subject_type: str, identifier: str) -> list[BaseModel]:
        """Every stored version of one subject, oldest first."""
        prefix = format_subject_ref(subject_type, identifier, 0)[:-1]
        with self._lock:
            found = [(ref, subject) for ref, (_, subject) in self.items.items() if ref.startswith(prefix)
                     and ref[len(prefix):].isdecimal()]
        return [subject for _, subject in sorted(found, key=lambda item: int(item[0][len(prefix):]))]
