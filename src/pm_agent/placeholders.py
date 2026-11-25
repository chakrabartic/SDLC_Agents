"""Placeholders for SLB-specific details that are not yet known.

This is a *reference architecture*: anything SLB has not supplied yet (org ID, ADO process
fields, KB backend, Figma library keys, …) is represented by a tracked token rather than a
guess. Every token names the open question in ``docs/15-risks-assumptions-open-questions.md``
(``Q-xx``) so ``make placeholders`` can list exactly what remains to be filled in.

Token format (identical in Python, YAML and Terraform so one grep finds them all)::

    PLACEHOLDER:<question-id>:<snake_case_name>      e.g.  PLACEHOLDER:Q-10:gcp_org_id

Placeholders are tolerated in ``local``/``dev`` and rejected in ``stage``/``prod`` (strict mode). Strict checks
search for the token *anywhere* — inside a longer value (``https://PLACEHOLDER:Q-05:host/api``) or in a mapping key —
with the same grammar as ``tools/list_placeholders.py``, so the registry and the stage/prod refusal cannot disagree.
``parse_placeholder`` / ``is_placeholder`` stay exact: they answer "is this value *only* a token?".
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import NoReturn, TypeVar

PREFIX = "PLACEHOLDER:"
_QUESTION_RE = re.compile(r"^Q-\d{2}$")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_TOKEN_RE = re.compile(r"^PLACEHOLDER:(Q-\d{2}):([a-z][a-z0-9_]*)$")
_TOKEN_SEARCH = re.compile(r"PLACEHOLDER:(Q-\d{2}):([a-z][a-z0-9_]*)")  # == tools/list_placeholders.py TOKEN

T = TypeVar("T")


class PlaceholderError(RuntimeError):
    """Base class for placeholder-related failures."""

    def __init__(self, question_id: str, name: str, message: str) -> None:
        super().__init__(f"[{question_id}] {name}: {message}")
        self.question_id = question_id
        self.name = name


class PlaceholderNotResolved(PlaceholderError):
    """A configuration value is still a placeholder in an environment that requires real values."""


class PlaceholderNotImplemented(PlaceholderError, NotImplementedError):
    """A code path depends on SLB-specific integration details that have not been supplied yet."""


def placeholder(question_id: str, name: str) -> str:
    """Return the canonical placeholder token for an open question."""
    if not _QUESTION_RE.match(question_id):
        raise ValueError(f"question_id must look like 'Q-07', got {question_id!r}")
    if not _NAME_RE.match(name):
        raise ValueError(f"name must be snake_case, got {name!r}")
    return f"{PREFIX}{question_id}:{name}"


def parse_placeholder(value: object) -> tuple[str, str] | None:
    """Return ``(question_id, name)`` if *value* is a placeholder token, else ``None``."""
    if not isinstance(value, str):
        return None
    match = _TOKEN_RE.match(value)
    return (match.group(1), match.group(2)) if match else None


def is_placeholder(value: object) -> bool:
    """True when *value* is a well-formed placeholder token."""
    return parse_placeholder(value) is not None


def require(value: T, *, strict: bool) -> T:
    """Return *value*; in strict mode refuse one that contains an unresolved placeholder anywhere inside it."""
    found = find_placeholders(value) if strict else []
    if found:
        path, question_id, name = found[0]
        where = f" (at {path})" if path else ""
        raise PlaceholderNotResolved(question_id, name, f"value must be supplied before use in this environment{where}")
    return value


def find_placeholders(obj: object, path: str = "") -> list[tuple[str, str, str]]:
    """Recursively list ``(dotted_path, question_id, name)`` for every placeholder token inside *obj*.

    Tokens are found as substrings of string values *and* mapping keys (a key's path is the key itself).
    """
    found: list[tuple[str, str, str]] = []
    if isinstance(obj, str):
        found.extend((path, match.group(1), match.group(2)) for match in _TOKEN_SEARCH.finditer(obj))
    elif isinstance(obj, Mapping):
        for key, child in obj.items():
            child_path = f"{path}.{key}" if path else str(key)
            found.extend(find_placeholders(key, child_path) if isinstance(key, str) else [])
            found.extend(find_placeholders(child, child_path))
    elif isinstance(obj, Sequence) and not isinstance(obj, bytes):
        for index, child in enumerate(obj):
            found.extend(find_placeholders(child, f"{path}[{index}]"))
    return found


def not_implemented(question_id: str, what: str) -> NoReturn:
    """Raise for a code path that needs an SLB-specific integration detail (keeps the gap explicit)."""
    raise PlaceholderNotImplemented(question_id, what, "awaiting SLB input — see PLACEHOLDERS.md")
