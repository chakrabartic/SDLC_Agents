"""Deterministic business-entity resolution for connectors: customers and products (docs/10 §2.1).

Customers are business entities (not PII) resolved against the configured gazetteer
(``grounding.gazetteers.organizations``); products against ``product.product_aliases``. Matching is case-insensitive
on word boundaries; e-mail addresses and URLs are ignored (a domain is not a customer statement). When a text names
more than one customer the result is ``None`` — connectors never guess (``Signal.customer`` holds one entity).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from pm_agent.schemas import Customer
from pm_agent.settings import Settings

_ADDRESSES = re.compile(r"https?://\S+|[^\s@<>()\"']+@[^\s@<>()\"']+", re.IGNORECASE)


def _compile(mapping: Mapping[str, Sequence[str]]) -> list[tuple[str, re.Pattern[str]]]:
    compiled = []
    for canonical, forms in mapping.items():
        names = sorted({canonical, *forms}, key=len, reverse=True)
        alternation = "|".join(re.escape(name) for name in names if name.strip())
        compiled.append((canonical, re.compile(rf"(?<![A-Za-z0-9])(?:{alternation})(?![A-Za-z0-9])", re.IGNORECASE)))
    return compiled


class EntityResolver:
    def __init__(self, organizations: Mapping[str, Sequence[str]], products: Mapping[str, Sequence[str]]) -> None:
        self._organizations = _compile(organizations)
        self._products = _compile(products)
        self._organization_names = {name.strip().lower(): canonical for canonical, forms in organizations.items()
                                    for name in (canonical, *forms)}

    @classmethod
    def from_settings(cls, settings: Settings) -> "EntityResolver":
        raw = settings.grounding.gazetteers.get("organizations") or {}
        organizations = {str(name): [str(alias) for alias in (aliases or [])] for name, aliases in raw.items()}
        return cls(organizations, settings.product.product_aliases)

    @staticmethod
    def _found(compiled: list[tuple[str, re.Pattern[str]]], texts: Sequence[str | None]) -> list[str]:
        text = "\n".join(_ADDRESSES.sub(" ", t) for t in texts if t)
        hits = []
        for canonical, pattern in compiled:
            match = pattern.search(text)
            if match:
                hits.append((match.start(), canonical))
        return [canonical for _, canonical in sorted(hits)]

    def customers_in(self, *texts: str | None) -> list[str]:
        """Canonical customer names mentioned in *texts*, in order of first mention."""
        return self._found(self._organizations, texts)

    def customer(self, *texts: str | None) -> Customer | None:
        found = self.customers_in(*texts)
        return Customer(name=found[0]) if len(found) == 1 else None

    def canonical_customer(self, name: str | None) -> str | None:
        """Exact (case-insensitive) gazetteer lookup for structured vendor fields such as a CSM account."""
        return self._organization_names.get(name.strip().lower()) if name else None

    def products_in(self, *texts: str | None) -> list[str]:
        return self._found(self._products, texts)

    def product(self, *texts: str | None) -> str | None:
        found = self.products_in(*texts)
        return found[0] if len(found) == 1 else None


def resolve_customer(entities: EntityResolver | None, structured: str | None, *texts: str | None) -> Customer | None:
    """Structured vendor field first (canonicalized when known), else a single unambiguous mention in *texts*."""
    if structured and structured.strip():
        canonical = entities.canonical_customer(structured) if entities else None
        return Customer(name=canonical or structured.strip())
    return entities.customer(*texts) if entities else None


def resolve_product(entities: EntityResolver | None, *texts: str | None) -> str | None:
    return entities.product(*texts) if entities else None
