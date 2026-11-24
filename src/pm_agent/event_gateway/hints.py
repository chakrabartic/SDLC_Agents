"""Change hints — the only thing the signal-event-gateway emits (docs/09 §9, docs/08 §8 T8).

A hint says "record *X* in system *Y* changed, re-read it" and nothing more: identifiers only, never body text
(no message text, descriptions, notes or ``resourceData`` beyond ids). The ingestion workflow always re-reads the
record through the read MCP server, so a spoofed hint can at worst cause a redundant read.

* ``ChangeHint`` — frozen, ``extra="forbid"``; every string is identifier-like (1–512 chars, no whitespace or control
  characters, none of ``< > " { }``), so free text and JSON fragments cannot ride along.
* ``HintPublisher`` — where hints go: ``InMemoryHintPublisher`` (tests, local) or ``PubSubHintPublisher`` (Pub/Sub
  topic ``signal-change-hints``, drained by ``signal-ingestion``; the GKE workload identity holds publish only).
"""

from __future__ import annotations

import importlib
import re
import threading
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pm_agent.placeholders import PREFIX, not_implemented
from pm_agent.schemas.canonical import canonical_json
from pm_agent.settings import Settings

HintSystem = Literal["ado", "servicenow", "pagerduty", "msgraph", "figma"]

DEFAULT_HINTS_TOPIC = "signal-change-hints"
MAX_IDENTIFIER_CHARS = 512
_IDENTIFIER_RE = re.compile(r'[^\s<>"{}\x00-\x1f\x7f]{1,512}')  # 512 == MAX_IDENTIFIER_CHARS
_IDENTIFIER_RULE = "hints carry identifiers only: 1-512 chars, no whitespace/control characters and none of < > \" { }"


def is_identifier(value: str) -> bool:
    """True when *value* is identifier-like (safe to carry in a hint)."""
    return _IDENTIFIER_RE.fullmatch(value) is not None


class ChangeHint(BaseModel):
    """A change notice ("record X in system Y changed"): identifiers only; consumers re-read the record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    system: HintSystem
    event_type: str
    received_utc: datetime
    record_id: str | None = None
    resource: str | None = None
    attributes: dict[str, str] = Field(default_factory=dict)

    @field_validator("event_type", "record_id", "resource")
    @classmethod
    def _identifier_only(cls, value: str | None) -> str | None:
        if value is not None and not is_identifier(value):
            raise ValueError(_IDENTIFIER_RULE)
        return value

    @field_validator("attributes")
    @classmethod
    def _identifier_attributes(cls, value: dict[str, str]) -> dict[str, str]:
        if not all(is_identifier(key) and is_identifier(item) for key, item in value.items()):
            raise ValueError(_IDENTIFIER_RULE)
        return value

    @field_validator("received_utc")
    @classmethod
    def _aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("received_utc must be timezone-aware")
        return value.astimezone(UTC)


def encode_hint(hint: ChangeHint) -> bytes:
    """Pub/Sub message data: RFC 8785 canonical JSON, so equal hints are byte-identical (cheap de-duplication)."""
    return canonical_json(hint)


class HintPublisher(Protocol):
    def publish(self, hint: ChangeHint) -> None:
        """Deliver one hint; at-least-once is fine (consumers re-read the record, duplicates are harmless)."""


class InMemoryHintPublisher:
    """Collects hints in ``.hints`` (tests / local runs); safe to call from the server's worker threads."""

    def __init__(self) -> None:
        self.hints: list[ChangeHint] = []
        self._lock = threading.Lock()

    def publish(self, hint: ChangeHint) -> None:
        with self._lock:
            self.hints.append(hint)


class PubSubHintPublisher:
    """Publishes hints to a Pub/Sub topic (``projects/<project>/topics/signal-change-hints``).

    ``client`` is injectable (tests); otherwise ``google.cloud.pubsub_v1.PublisherClient`` is created lazily on first
    publish, so importing this module never requires google-cloud-pubsub. Message attributes ``system`` and
    ``event_type`` allow subscription filters without parsing the data.
    """

    def __init__(self, topic_path: str, client: Any | None = None, timeout_seconds: float = 10.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._topic_path = topic_path
        self._client = client
        self._timeout_seconds = timeout_seconds
        self._lock = threading.Lock()

    @property
    def topic_path(self) -> str:
        """Fully-qualified topic, e.g. ``projects/chayan-genai-bb/topics/signal-change-hints``."""
        return self._topic_path

    @classmethod
    def from_settings(cls, settings: Settings, topic: str = DEFAULT_HINTS_TOPIC) -> PubSubHintPublisher:
        """Topic in the component's own project (k8s sets ``PM_AGENT_HINTS_TOPIC``; pass it as *topic*)."""
        return cls(f"projects/{settings.gcp.project_id}/topics/{topic}")

    def publish(self, hint: ChangeHint) -> None:
        """Publish one hint and wait (bounded by ``timeout_seconds``) for the server acknowledgement.

        Raises ``PlaceholderNotImplemented`` (Q-10) while the topic path still contains a placeholder or when the
        Pub/Sub client library is not installed in this image; publish errors propagate to the caller.
        """
        if PREFIX in self._topic_path:
            not_implemented("Q-10", "signal_change_hints_topic")
        future = self._publisher_client().publish(
            self._topic_path, data=encode_hint(hint), system=hint.system, event_type=hint.event_type)
        future.result(timeout=self._timeout_seconds)

    def _publisher_client(self) -> Any:
        with self._lock:
            if self._client is None:
                self._client = _load_pubsub_v1().PublisherClient()
            return self._client


def _load_pubsub_v1() -> Any:
    try:
        return importlib.import_module("google.cloud.pubsub_v1")
    except ImportError:
        not_implemented("Q-10", "google-cloud-pubsub publisher not installed in this image")
