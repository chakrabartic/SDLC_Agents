"""Signal similarity scorers for correlation (docs/06 §6.2). Thresholds belong to the scorer that produced the score.

* ``EmbeddingScorer`` — production: ``gemini-embedding`` via the Gen AI SDK on Agent Platform (enterprise client),
  cosine over ``SEMANTIC_SIMILARITY`` embeddings, thresholds ``correlation.assign_threshold`` / ``adjudicate_floor``.
* ``LexicalScorer`` — offline/reference: the better of (a) cosine over stemmed content words and (b) shared *domain
  identifiers* (channel/log names, well ids …). Customers, products, people (pseudonyms) and bare numbers never count
  as identity on their own. Thresholds ``correlation.lexical_*`` (calibrated by E1, like the embedding ones).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Protocol

from pm_agent.grounding.atoms import AtomExtractor
from pm_agent.schemas import Signal
from pm_agent.settings import Settings

_WORD = re.compile(r"[a-z0-9][a-z0-9_\-]{2,}")
_NUMBER = re.compile(r"-?\d+(?:[.,]\d+)*")
_PSEUDONYM = re.compile(r"^(email|phone|person)_[0-9a-f]{6}$")
_STOP = frozenset("""the and for with that this from are was were has have had not but can will would should could
into onto over under after before when then than there their them they you your our its his her she him who what
which while also only been being does did doing done each other some such very just more most less least please
hello thanks regards team since""".split())
_SUFFIXES = ("ing", "ed", "es", "s")


def stem(word: str) -> str:
    """Tiny suffix stripper: exporting/exports/exported → export; logs → log (no dictionary, deterministic)."""
    for suffix in _SUFFIXES:
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def term_vector(text: str) -> Counter[str]:
    return Counter(stem(w) for w in _WORD.findall(text.lower()) if w not in _STOP)


def cosine(a: Counter[str], b: Counter[str]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(a[w] * b[w] for w in a.keys() & b.keys())
    return dot / (math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values())))


class SimilarityScorer(Protocol):
    assign_threshold: float
    adjudicate_floor: float

    def score(self, a: Signal, b: Signal) -> tuple[float, str]: ...


def _boost(a: Signal, b: Signal) -> float:
    boost = 0.05 if a.product and a.product == b.product else 0.0
    return boost + (0.03 if a.customer.name and a.customer.name == b.customer.name else 0.0)


class LexicalScorer:
    def __init__(self, settings: Settings, atoms: AtomExtractor) -> None:
        self.assign_threshold = settings.correlation.lexical_assign_threshold
        self.adjudicate_floor = settings.correlation.lexical_adjudicate_floor
        self._atoms = atoms
        gazetteers = settings.grounding.gazetteers
        self._orgs = {str(o).lower() for o in (gazetteers.get("organizations") or {})}
        self._products = {str(p).lower() for p in gazetteers.get("products") or []} | {
            p.lower() for p in settings.product.product_aliases}

    def identifiers(self, text: str) -> set[str]:
        """Atoms that can identify a problem (not customers, people, bare numbers or 1–2 character tokens)."""
        return {a for a in self._atoms.extract(text) if len(a) >= 3 and a not in self._orgs
                and not _NUMBER.fullmatch(a) and not _PSEUDONYM.match(a)}

    def score(self, a: Signal, b: Signal) -> tuple[float, str]:
        """``max(content cosine, identifier score)`` + small same-product/customer boosts, capped at 0.99."""
        lexical = cosine(term_vector(self._atoms.mask_gazetteer(a.raw_text)),
                         term_vector(self._atoms.mask_gazetteer(b.raw_text)))
        ids_a, ids_b = self.identifiers(a.raw_text), self.identifiers(b.raw_text)
        shared = ids_a & ids_b
        identity = 0.0
        if len(shared) >= 2 and shared - self._products:
            identity = 0.5 + 0.5 * len(shared) / min(len(ids_a), len(ids_b))
        best = max(lexical, identity)
        reason = f"shared identifiers: {sorted(shared)}" if identity >= lexical else "content similarity"
        return round(min(best + _boost(a, b), 0.99), 4), reason


class EmbeddingScorer:
    """Production scorer over ``gemini-embedding`` vectors (cached per signal id)."""

    def __init__(self, settings: Settings, client: Any, dimensions: int = 768) -> None:
        self.assign_threshold = settings.correlation.assign_threshold
        self.adjudicate_floor = settings.correlation.adjudicate_floor
        self._client = client
        self._model = settings.models.embedding
        self._dimensions = dimensions
        self._cache: dict[str, list[float]] = {}

    def _vector(self, signal: Signal) -> list[float]:
        if signal.signal_id not in self._cache:
            from google.genai import types  # pylint: disable=import-outside-toplevel

            response = self._client.models.embed_content(
                model=self._model, contents=[signal.raw_text[:8000]],
                config=types.EmbedContentConfig(task_type="SEMANTIC_SIMILARITY",
                                                output_dimensionality=self._dimensions))
            self._cache[signal.signal_id] = list(response.embeddings[0].values or [])
        return self._cache[signal.signal_id]

    def score(self, a: Signal, b: Signal) -> tuple[float, str]:
        va, vb = self._vector(a), self._vector(b)
        dot = sum(x * y for x, y in zip(va, vb))
        norm = math.sqrt(sum(x * x for x in va)) * math.sqrt(sum(y * y for y in vb))
        return round(min((dot / norm if norm else 0.0) + _boost(a, b), 0.99), 4), "embedding similarity"


def make_scorer(settings: Settings, atoms: AtomExtractor) -> SimilarityScorer:
    """``correlation.scoring``: ``embedding`` (gemini-embedding, needs ADC) or ``lexical`` (offline reference)."""
    if settings.correlation.scoring == "embedding":
        from pm_agent.settings import make_genai_client  # pylint: disable=import-outside-toplevel

        return EmbeddingScorer(settings, make_genai_client(settings))
    return LexicalScorer(settings, atoms)
