"""Deterministic atom extraction for V1 (procedural text) and V4 (atom consistency) — docs/06 §3.1.

An *atom* is a checkable token whose presence in the cited quote can be verified mechanically:
numbers/versions/record ids (anything containing a digit), ALL-CAPS identifiers (``TVDHD``, ``TIME_SURFACE``), and
gazetteer terms (organizations and products, with aliases such as ``CNE`` ↔ ``Contoso Energy``).
Ordinary words are not atoms; that is what the bounded entailment judge (V5) is for.

Text is NFKC-normalised *before* tokenising (fullwidth ``１２０`` is the number ``120``, not invisible), and digits of
any script start a token, so no spelling of a number escapes V1/V4. A digit-bearing atom is supported only by the
identical atom in a quote: ``12`` is not ``12.5``, ``234`` is not ``1,234``, ``37.8`` is not ``37.8.87``.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from pm_agent.settings import Settings

ATOMS_VERSION = "atoms-2"
# A token starts at a letter/digit of any script, or at a minus sign directly before a digit that does not continue
# another token (``-5`` is negative five; ``WELL-0042`` and ``5-10`` stay single tokens).
_TOKEN = re.compile(r"(?:(?<![\w\-.,])-(?=\d))?[^\W_][\w\-.,]*(?:\([a-z0-9]+\))?")
_ALL_CAPS = re.compile(r"^[A-Z][A-Z0-9_\-]{2,}(?:\([a-z0-9]+\))?$")
# Identifier characters: a gazetteer term inside ``TIME_RT_WITSML`` is part of that identifier, not a mention.
_BEFORE, _AFTER = r"(?<![A-Za-z0-9_])", r"(?![A-Za-z0-9_])"
# NFKC folds fullwidth/compatibility forms; U+2212 MINUS SIGN has no decomposition, so it is mapped explicitly.
_SIGNS = str.maketrans({"\u2212": "-"})
DEFAULT_STOPLIST = frozenset({
    "ado", "api", "pm", "pms", "ui", "ux", "kb", "prd", "sme", "sla", "sow", "and", "the", "for", "not", "with",
    "from", "url", "id", "ids", "ok", "tbd", "eta", "faq", "rca", "note", "fyi", "asap", "gcp", "llm",
})


def normalize_text(text: str) -> str:
    """The form every atom is read from: NFKC with typographic minus signs folded to ``-``."""
    return unicodedata.normalize("NFKC", text).translate(_SIGNS)


def normalize(token: str) -> str:
    """NFKC, lower-case, strip trailing punctuation, drop thousands separators in numbers."""
    text = normalize_text(token).strip().lower().rstrip(".,;:")
    if re.fullmatch(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?", text):
        text = text.replace(",", "")
    return text


def has_digit(atom: str) -> bool:
    """Numbers, versions and record ids — atoms that only an identical quote atom can support."""
    return any(ch.isdigit() for ch in atom)


@dataclass
class AtomExtractor:
    """Extracts atoms; aliases map every surface form to one canonical (lower-case) name."""

    aliases: dict[str, str] = field(default_factory=dict)
    stoplist: frozenset[str] = DEFAULT_STOPLIST

    @classmethod
    def from_settings(cls, settings: Settings) -> "AtomExtractor":
        """Build the alias table from the configured gazetteers and product aliases (data, not code)."""
        extractor = cls()
        gaz = settings.grounding.gazetteers
        for canonical, forms in (gaz.get("organizations") or {}).items():
            extractor.extend((form, canonical) for form in [canonical, *forms])
        extractor.extend((str(product), str(product)) for product in gaz.get("products") or [])
        for canonical, forms in settings.product.product_aliases.items():
            extractor.extend((form, canonical) for form in [canonical, *forms])
        return extractor

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
        return self._phrase_atoms(normalize_text(text))[1]

    def extract(self, text: str) -> list[str]:
        """Ordered, de-duplicated atoms of *text* (read from its NFKC form)."""
        atoms, rest = self._phrase_atoms(normalize_text(text))
        for raw in _TOKEN.findall(rest):
            token = raw.rstrip(".,;:")
            norm = normalize(token)
            if not norm or norm in self.stoplist:
                continue
            if has_digit(norm) or _ALL_CAPS.match(token):
                atoms.append(norm)
        return list(dict.fromkeys(atoms))

    def unsupported(self, claim_text: str, quotes: Sequence[str]) -> list[str]:
        """Atoms of the claim that appear in none of the quotes (empty list ⇒ V4 passes).

        Digit-bearing atoms must be *exact* atoms of a quote — a boundary regex would let ``12`` match inside
        ``12.5`` (``.``/``,`` are not identifier characters). Identifiers and gazetteer terms without digits may also
        appear in a quote as a case-insensitive whole word (``TVDHD`` ↔ ``tvdhd``).
        """
        quote_atoms = {atom for quote in quotes for atom in self.extract(quote)}
        joined = " ".join(normalize(q) for q in quotes)
        missing = []
        for atom in self.extract(claim_text):
            if atom in quote_atoms:
                continue
            if not has_digit(atom) and re.search(rf"{_BEFORE}{re.escape(atom)}{_AFTER}", joined):
                continue
            missing.append(atom)
        return missing

    def has_atoms(self, text: str) -> bool:
        return bool(self.extract(text))

    def extend(self, aliases: Iterable[tuple[str, str]]) -> None:
        """Register ``(surface form, canonical name)`` pairs, keyed by the normalised (NFKC, lower-case) form."""
        for form, canonical in aliases:
            self.aliases[normalize_text(form).lower()] = normalize_text(canonical).lower()
