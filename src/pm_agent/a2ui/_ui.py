"""Internal builders for A2UI v1.0 component dicts and the ``createSurface`` envelope.

Plain dicts on purpose (no SDK builder models): the wire format is pinned by contract tests against the vendored
v1.0 schemas, so SDK churn cannot silently change what the PM sees. Basic-catalog components carry no
``@catalogId`` (the surface default is the basic catalog); SLB components always carry ``@catalogId``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, Final, Literal

from pm_agent.a2ui._core import A2UI_VERSION, BASIC_CATALOG_ID, SLB_CATALOG_ID, sanitize_id

Component = dict[str, Any]
Live = Literal["off", "polite", "assertive"]

_MD_SPECIAL: Final = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~&])")


def md(text: str) -> str:
    """Literal text for a basic ``Text`` (which renders Markdown): one line, every Markdown metacharacter escaped.

    Evidence-derived strings (titles, questions, ids) must never turn into headings, links or emphasis.
    """
    return _MD_SPECIAL.sub(r"\\\1", " ".join(text.split()))


def heading(text: str, level: int = 2) -> str:
    return f"{'#' * level} {md(text)}"


def bold(text: str) -> str:
    return f"**{md(text)}**"


def pointer(*tokens: str | int) -> str:
    """RFC 6901 JSON Pointer from raw tokens (``~`` → ``~0``, ``/`` → ``~1``)."""
    return "".join("/" + str(t).replace("~", "~0").replace("/", "~1") for t in tokens) or "/"


def bind(*tokens: str | int) -> dict[str, str]:
    """``{"@path": <pointer>}`` data binding into the surface's data model."""
    return {"@path": pointer(*tokens)}


def label(name: str) -> str:
    """``acceptance_criteria`` → ``Acceptance criteria``."""
    words = name.replace("_", " ").strip()
    return words[:1].upper() + words[1:]


class SurfaceBuilder:
    """Accumulates components (unique UAX #31 ids) and the data model of one surface."""

    def __init__(self, surface_id: str) -> None:
        self.surface_id = sanitize_id(surface_id)
        self.data: dict[str, Any] = {}
        self._components: list[Component] = []
        self._ids: set[str] = {"root"}

    # ------------------------------------------------------------------------------------------ plumbing
    def add(self, component: Component) -> str:
        """Register a component; ids are sanitized and must be unique within the surface."""
        cid = sanitize_id(str(component["id"]))
        if cid in self._ids:
            raise ValueError(f"duplicate component id {cid!r} on surface {self.surface_id!r}")
        self._ids.add(cid)
        self._components.append({**component, "id": cid})
        return cid

    def messages(self, children: Sequence[str]) -> list[dict[str, Any]]:
        """One self-contained ``createSurface`` (components + data model inline, v1.0 single-message UI)."""
        root: Component = {"id": "root", "component": "Column", "children": list(children)}
        return [{"version": A2UI_VERSION, "createSurface": {
            "surfaceId": self.surface_id, "catalogId": BASIC_CATALOG_ID, "sendDataModel": False,
            "components": [root, *self._components], "dataModel": self.data}}]

    # ------------------------------------------------------------------------------------ basic catalog
    def text(self, cid: str, text: str, *, caption: bool = False, live: Live | None = None) -> str:
        component: Component = {"id": cid, "component": "Text", "text": text}
        if caption:
            component["variant"] = "caption"
        if live:
            component["@accessibility"] = {"live": live}
        return self.add(component)

    def column(self, cid: str, children: Sequence[str]) -> str:
        return self.add({"id": cid, "component": "Column", "children": list(children)})

    def row(self, cid: str, children: Sequence[str], *, justify: str = "start") -> str:
        return self.add({"id": cid, "component": "Row", "children": list(children), "justify": justify,
                         "align": "center"})

    def card(self, cid: str, child: str) -> str:
        return self.add({"id": cid, "component": "Card", "child": child})

    def tabs(self, cid: str, tabs: Sequence[tuple[str, str]], a11y: str) -> str:
        return self.add({"id": cid, "component": "Tabs", "tabs": [{"title": t, "child": c} for t, c in tabs],
                         "@accessibility": {"label": a11y}})

    def button(self, cid: str, caption: str, event: str, context: dict[str, Any], *, a11y: str,
               primary: bool = False, borderless: bool = False, user_message: str | None = None,
               checks: list[dict[str, Any]] | None = None) -> str:
        """A button whose click sends the agent event *event* with *context* (literals or ``@path`` bindings)."""
        child = self.text(f"{cid}_label", md(caption))
        payload: dict[str, Any] = {"name": event, "context": context}
        if user_message:
            payload["userMessage"] = user_message
        component: Component = {"id": cid, "component": "Button", "child": child,
                                "variant": "primary" if primary else "borderless" if borderless else "default",
                                "action": {"event": payload}, "@accessibility": {"label": a11y}}
        if checks:
            component["@checks"] = checks
        return self.add(component)

    def text_field(self, cid: str, caption: str, value: dict[str, str], *, a11y: str, long: bool = False,
                   placeholder: str | None = None) -> str:
        component: Component = {"id": cid, "component": "TextField", "label": caption, "value": value,
                                "variant": "longText" if long else "shortText", "@accessibility": {"label": a11y}}
        if placeholder:
            component["placeholder"] = placeholder
        return self.add(component)

    def choice_picker(self, cid: str, caption: str, options: Sequence[tuple[str, str]], value: dict[str, str], *,
                      a11y: str, multiple: bool, filterable: bool = False) -> str:
        return self.add({"id": cid, "component": "ChoicePicker", "label": caption,
                         "options": [{"label": text, "value": val} for text, val in options], "value": value,
                         "variant": "multipleSelection" if multiple else "mutuallyExclusive",
                         "filterable": filterable, "@accessibility": {"label": a11y}})

    # -------------------------------------------------------------------------------------- SLB catalog
    def slb(self, cid: str, component: str, *, a11y: str, live: Live | None = None, **props: Any) -> str:
        """An SLB domain component (``@catalogId`` = ``slb.com:pm_workbench_v1``) with an accessible label."""
        accessibility: dict[str, Any] = {"label": a11y}
        if live:
            accessibility["live"] = live
        return self.add({"id": cid, "component": component, "@catalogId": SLB_CATALOG_ID, **props,
                         "@accessibility": accessibility})
