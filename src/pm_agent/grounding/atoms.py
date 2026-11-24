"""Deterministic atom extraction for V1 (procedural text) and V4 (atom consistency) — docs/06 §3.1.

An *atom* is a checkable token whose presence in the cited quote can be verified mechanically:
numbers/versions/record ids (anything containing a digit), ALL-CAPS identifiers (``TVDHD``, ``TIME_SURFACE``), and
gazetteer terms (organizations and products, with aliases such as ``CNE`` ↔ ``Contoso Energy``).
Ordinary words are not atoms; that is what the bounded entailment judge (V5) is for.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from pm_agent.settings import Settings

ATOMS_VERSION = "atoms-1"
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-.,]*(?:\([a-z0-9]+\))?")
_ALL_CAPS = re.compile(r"^[A-Z][A-Z0-9_\-]{2,}(?:\([a-z0-9]+\))?$")
# Identifier characters: a gazetteer term inside ``TIME_RT_WITSML`` is part of that identifier, not a mention.
_BEFORE, _AFTER = r"(?<![A-Za-z0-9_])", r"(?![A-Za-z0-9_])"
DEFAULT_STOPLIST = frozenset({
    "ado", "api", "pm", "pms", "ui", "ux", "kb", "prd", "sme", "sla", "sow", "and", "the", "for", "not", "with",
    "from", "url", "id", "ids", "ok", "tbd", "eta", "faq", "rca", "note", "fyi", "asap", "gcp", "llm",
})


def normalize(token: str) -> str:
    """NFKC, lower-case, strip trailing punctuation, drop thousands separators in numbers."""
    text = unicodedata.normalize("NFKC", token).strip().lower().rstrip(".,;:")
    if re.fullmatch(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?", text):
        text = text.replace(",", "")
    return text


@dataclass
class AtomExtractor:
    """Extracts atoms; aliases map every surface form to one canonical (lower-case) name."""

    aliases: dict[str, str] = field(default_factory=dict)
    stoplist: frozenset[str] = DEFAULT_STOPLIST

    @classmethod
    def from_settings(cls, settings: Settings) -> "AtomExtractor":
        """Build the alias table from the configured gazetteers and product aliases (data, not code)."""
        aliases: dict[str, str] = {}
        gaz = settings.grounding.gazetteers
        for canonical, forms in (gaz.get("organizations") or {}).items():
            for form in [canonical, *forms]:
                aliases[form.lower()] = canonical.lower()
        for product in gaz.get("products") or []:
            aliases[str(product).lower()] = str(product).lower()
        for canonical, forms in settings.product.product_aliases.items():
            for form in [canonical, *forms]:
                aliases[form.lower()] = canonical.lower()
        return cls(aliases=aliases)

    def _phrase_atoms(self, text: str) -> tuple[list[str], str]:
        """Find (multi-word) gazetteer phrases first; return atoms and the text with them masked."""
        found: list[str] = []
        masked = text
        for alias in sorted(self.aliases, key=len, reverse=True):
            pattern = re.compile(rf"{_BEFORE}{re.escape(alias)}{_AFTER}", re.IGNORECASE)
            if pattern.search(masked):
                found.append(self.aliases[alias])
                masked = pattern.sub(" ", masked)
        return found, masked

    def mask_gazetteer(self, text: str) -> str:
        """*text* with organization/product mentions blanked (context, not content, for similarity)."""
        return self._phrase_atoms(text)[1]

    def extract(self, text: str) -> list[str]:
        """Ordered, de-duplicated atoms of *text*."""
        atoms, rest = self._phrase_atoms(text)
        for raw in _TOKEN.findall(rest):
            token = raw.rstrip(".,;:")
            norm = normalize(token)
            if not norm or norm in self.stoplist:
                continue
            if any(ch.isdigit() for ch in norm) or _ALL_CAPS.match(token):
                atoms.append(norm)
        return list(dict.fromkeys(atoms))

    def unsupported(self, claim_text: str, quotes: Sequence[str]) -> list[str]:
        """Atoms of the claim that appear in none of the quotes (empty list ⇒ V4 passes)."""
        quote_atoms: set[str] = set()
        joined = " ".join(normalize(q) for q in quotes)
        for quote in quotes:
            quote_atoms.update(self.extract(quote))
        missing = []
        for atom in self.extract(claim_text):
            if atom in quote_atoms:
                continue
            if re.search(rf"{_BEFORE}{re.escape(atom)}{_AFTER}", joined):
                continue
            missing.append(atom)
        return missing

    def has_atoms(self, text: str) -> bool:
        return bool(self.extract(text))

    def extend(self, aliases: Iterable[tuple[str, str]]) -> None:
        for form, canonical in aliases:
            self.aliases[form.lower()] = canonical.lower()
