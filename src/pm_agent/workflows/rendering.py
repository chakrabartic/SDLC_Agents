"""Render verified drafts into the exact payloads the PM approves (docs/07 §2, docs/10 §4.2).

Rendering is deterministic and uses only verified claims; every bullet carries its citation link, missing fields
are shown as missing (never filled), and the payload hash binds the approval (H-4).
"""

from __future__ import annotations

from html import escape
from typing import Any

from pm_agent.schemas import Claim, FieldStatus, RequirementDraft
from pm_agent.settings import TemplateSettings

MISSING_NOTE = "<p><em>Missing: a clarifying question is open for this field.</em></p>"


def _citation_links(claim: Claim) -> str:
    links = [f'<a href="{escape(c.permalink)}">{escape(c.source_system.value)}:{escape(c.source_record_id)}</a>'
             for c in claim.citations]
    return f" [{', '.join(links)}]" if links else ""


def claims_html(claims: list[Claim]) -> str:
    return "<ul>" + "".join(f"<li>{escape(c.text)}{_citation_links(c)}</li>" for c in claims) + "</ul>"


def _title(text: str) -> str:
    return text.replace("_", " ").capitalize()


def render_ado_payload(draft: RequirementDraft, template: TemplateSettings) -> dict[str, Any]:
    """``{work_item_type, fields{System.*}, relations[]}`` for ``mcp-ado-write.create_work_item``."""
    field_map = template.ado_field_map
    summary = draft.fields.get("summary")
    title = summary.claims[0].text[:255] if summary and summary.claims else "Requirement draft (summary missing)"
    fields: dict[str, str] = {field_map.get("summary", "System.Title"): title}
    sections = []
    for name in template.fields:
        field = draft.fields.get(name)
        if name == "summary":
            continue
        body = MISSING_NOTE if field is None or field.status is FieldStatus.MISSING else claims_html(field.claims)
        if name in field_map:
            fields[field_map[name]] = body
        else:
            sections.append(f"<h3>{escape(_title(name))}</h3>{body}")
    if draft.feasibility is not None:
        sections.append(f"<h3>Feasibility pre-check (advisory: {escape(draft.feasibility.verdict.value)}, "
                        f"Engineering decides)</h3>{claims_html(draft.feasibility.claims)}")
    fields[field_map.get("_description", "System.Description")] = "".join(sections)
    fields[field_map.get("_tags", "System.Tags")] = f"pm-agent; pm-agent-draft:{draft.requirement_id}"
    permalinks = sorted({c.permalink for claim in draft.all_claims() for c in claim.citations
                         if c.permalink.startswith("https://")})
    return {"work_item_type": draft.work_item_type, "fields": fields,
            "relations": [{"rel": "Hyperlink", "url": url} for url in permalinks]}
