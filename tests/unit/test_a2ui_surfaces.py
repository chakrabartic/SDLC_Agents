"""Deterministic A2UI surface builders (docs/11 §3, §5, §7) — the PM sees exactly what was verified.

Decision surfaces are pure functions over verified objects: no LLM lays out, omits or re-words decision content.
Assertions resolve bindings against the surface's own data model, i.e. they check what a renderer would display.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from pm_agent.a2ui import BASIC_CATALOG_ID, SLB_CATALOG_ID, sanitize_id
from pm_agent.a2ui.actions import DECISION_ACTIONS, SURFACE_EVENTS, UiAction, is_decision, parse_action
from pm_agent.a2ui.surfaces import (
    ACTION_LABELS,
    approval_surface,
    audit_surface,
    delete_surface,
    duplicate_decision_surface,
    investigation_surface,
    knowledge_review_surface,
    problem_detail_surface,
    problem_inbox_surface,
    prototype_surface,
    readiness_surface,
    requirement_review_surface,
    status_surface,
)
from pm_agent.schemas import ActionStatus, ActionType, Decision, FieldStatus, VerificationOutcome
from tests.fixtures.a2ui import a2ui_testkit as kit

MISSING_TEXT = "missing — see clarifying question"


@pytest.fixture(name="ado", scope="module")
def _ado() -> kit.Proposal:
    return kit.ado_proposal()


def _ui(messages: list[dict[str, Any]], component: dict[str, Any], prop: str) -> Any:
    """What the renderer hands to *component* for *prop* (bindings resolved against the data model)."""
    return kit.resolve(component[prop], kit.data_model(messages))


# ------------------------------------------------------------------------------------------- ids
@pytest.mark.parametrize(("raw", "expected"), [
    ("act_01J9ABC", "act_01J9ABC"),
    ("approve-act-01j9", "approve_act_01j9"),
    ("draft-req_01JD3", "draft_req_01JD3"),
    ("1st item", "_1st_item"),
    ("status-run#42", "status_run_42"),
    ("a.b/c:d", "a_b_c_d"),
    ("über", "_ber"),
    ("", "_"),
])
def test_sanitize_id_yields_ascii_uax31_identifiers(raw: str, expected: str) -> None:
    clean = sanitize_id(raw)
    assert clean == expected
    assert clean.isidentifier() and sanitize_id(clean) == clean  # valid and idempotent


# ------------------------------------------------------------------------------------------ approval
def test_approval_surface_shows_exactly_what_was_verified(ado: kit.Proposal) -> None:
    messages = approval_surface(ado.action, ado.subject, ado.report)
    surface = kit.create_surface(messages)
    assert surface["surfaceId"] == sanitize_id(f"approve_{ado.action.action_id}")
    assert surface["catalogId"] == BASIC_CATALOG_ID and surface["sendDataModel"] is False
    model = kit.data_model(messages)
    assert model["action"] == ado.action.model_dump(mode="json")  # verbatim, nothing re-worded or dropped
    assert model["report"] == ado.report.model_dump(mode="json")
    [card] = kit.slb_components(messages, "ApprovalCard")
    assert (card["action"], card["report"], card["diff"]) == ({"@path": "/action"}, {"@path": "/report"},
                                                              {"@path": "/diff"})
    [badge] = kit.slb_components(messages, "VerifierBadge")
    assert badge["report"] == {"@path": "/report"}
    text = kit.all_text(messages)
    for fact in (ado.action.action_id, ado.action.action_type.value, ado.action.payload_sha256,
                 ado.report.report_id, json.dumps(ado.action.target, sort_keys=True), "Blocked claims: 0",
                 f"{ado.report.overall.value}"):
        assert str(fact) in text, fact
    for check in ado.report.checks:
        assert f"{check.check_id} {check.status.value}" in text
    [payload_table] = [t for t in kit.slb_components(messages, "DataTable")
                       if "payload" in str(t["@accessibility"]["label"]).lower()]
    rows = _ui(messages, payload_table, "rows")
    assert {"field": "fields/System.Title", "value": ado.action.payload["fields"]["System.Title"]} in rows
    assert {"field": "work_item_type", "value": "Requirement"} in rows


def test_decision_buttons_carry_the_literal_action_identity(ado: kit.Proposal) -> None:
    messages = approval_surface(ado.action, ado.subject, ado.report)
    assert sorted(kit.events(messages)) == sorted(DECISION_ACTIONS)
    assert kit.data_model(messages)["review"] == {"comment": ""}
    for button in kit.buttons(messages):
        event = button["action"]["event"]
        assert event["context"] == {"actionId": ado.action.action_id, "actionVersion": ado.action.version,
                                    "payloadSha256": ado.action.payload_sha256, "reportId": ado.report.report_id,
                                    "comment": {"@path": "/review/comment"}}
        assert button["@accessibility"]["label"]
        needs_comment = event["name"] in ("request_changes", "reject_action")
        assert bool(button.get("@checks")) is needs_comment
        if needs_comment:
            [check] = button["@checks"]
            assert check["condition"] == {"@call": "required", "args": {"value": {"@path": "/review/comment"}}}
    [primary] = [b for b in kit.buttons(messages) if b.get("variant") == "primary"]
    assert primary["action"]["event"]["name"] == "approve_action"
    assert "Create ADO work item" in kit.button_label(messages, primary)
    [comment] = kit.of_type(messages, "TextField")
    assert comment["value"] == {"@path": "/review/comment"} and comment["variant"] == "longText"


def test_approval_surface_renders_every_subject_claim_with_its_citations(ado: kit.Proposal) -> None:
    messages = approval_surface(ado.action, ado.subject, ado.report)
    shown = [claim for cl in kit.slb_components(messages, "ClaimList") for claim in _ui(messages, cl, "claims")]
    expected = [c.model_dump(mode="json") for c in ado.subject.all_claims()]  # type: ignore[attr-defined]
    assert shown == expected
    assert all(c["citations"] and c["citations"][0]["evidence_id"] == "ev_pd_1" for c in shown)


def test_approval_surface_diff_is_normalised_and_bound(ado: kit.Proposal) -> None:
    after = ado.action.payload["fields"]
    plain = approval_surface(ado.action, ado.subject, ado.report, diff={"before": {"System.Title": "Old"},
                                                                       "after": after})
    per_field = approval_surface(ado.action, ado.subject, ado.report,
                                 diff={"System.Title": {"before": "Old", "after": after["System.Title"]}})
    for messages in (plain, per_field):
        [field_diff] = kit.slb_components(messages, "FieldDiff")
        assert (field_diff["before"], field_diff["after"]) == ({"@path": "/diff/before"}, {"@path": "/diff/after"})
        assert kit.data_model(messages)["diff"] == {"before": {"System.Title": "Old"}, "after": after}
    no_diff = approval_surface(ado.action, ado.subject, ado.report)
    assert not kit.slb_components(no_diff, "FieldDiff") and kit.data_model(no_diff)["diff"] == {}
    with pytest.raises(ValueError):
        approval_surface(ado.action, ado.subject, ado.report, diff={"System.Title": "just a value"})


@pytest.mark.parametrize("tamper", [
    "report_for_other_action", "report_not_pass", "report_not_commit_bound", "report_for_other_action_type",
    "payload_not_verified", "subject_edited", "action_not_pending"])
def test_approval_surface_refuses_anything_but_the_verified_pending_action(ado: kit.Proposal, tamper: str) -> None:
    action, subject, report = ado.action, ado.subject, ado.report
    if tamper == "report_for_other_action":
        report = report.model_copy(update={"report_id": "vr_other"})
    elif tamper == "report_not_pass":
        report = report.model_copy(update={"overall": VerificationOutcome.NEEDS_PM_DECISION})
    elif tamper == "report_not_commit_bound":
        report = report.model_copy(update={"commit_bound": False})
    elif tamper == "report_for_other_action_type":
        report = report.model_copy(update={"target_action": "KB_PUBLISH_ARTICLE"})
    elif tamper == "payload_not_verified":
        report = report.model_copy(update={"target_payload_sha256": "sha256:" + "0" * 64})
    elif tamper == "subject_edited":
        subject = subject.model_copy(update={"work_item_type": "Initiative"})
    else:
        action = action.model_copy(update={"status": ActionStatus.APPROVED})
    with pytest.raises(ValueError):
        approval_surface(action, subject, report)


# ---------------------------------------------------------------------------------- requirement review
def test_requirement_review_flags_inferred_and_missing_fields() -> None:
    draft = kit.requirement_draft()
    report = kit.report_for(draft, "requirement_draft", overall=VerificationOutcome.FAIL, blocked=("clm_removed_1",))
    messages = requirement_review_surface(draft, report, template_fields=kit.TEMPLATE_FIELDS)
    model = kit.data_model(messages)
    assert [s["key"] for s in model["sections"]] == kit.TEMPLATE_FIELDS
    statuses = {s["key"]: s["status"] for s in model["sections"]}
    assert statuses["summary"] == "populated" and statuses["acceptance_criteria"] == "inferred"
    missing = [k for k, v in statuses.items() if v == FieldStatus.MISSING.value]
    assert len(missing) == 6 and kit.texts(messages).count(MISSING_TEXT) == len(missing)
    lists = {cl["label"]: cl for cl in kit.slb_components(messages, "ClaimList")}
    summary = lists["Summary"]
    assert _ui(messages, summary, "fieldStatus") == "populated"
    cited = [c["evidence_id"] for c in _ui(messages, summary, "claims")[0]["citations"]]
    assert cited == ["ev_pd_1", "ev_snow_1"]
    text = kit.all_text(messages)
    assert "Inferred — needs your confirmation" in text and "Acceptance criteria" in text
    assert "removed — no supporting evidence" in text and "clm_removed_1" in text
    [badge] = kit.slb_components(messages, "VerifierBadge")
    assert _ui(messages, badge, "report")["overall"] == "FAIL"
    edits = kit.buttons(messages, "edit_field")
    assert [b["action"]["event"]["context"] for b in edits] == [
        {"requirementId": draft.requirement_id, "version": draft.version, "field": key} for key in kit.TEMPLATE_FIELDS]
    assert not set(kit.events(messages)) & DECISION_ACTIONS  # no action proposed ⇒ nothing to approve


def test_requirement_review_without_report_says_unverified_and_keeps_dict_order() -> None:
    draft = kit.requirement_draft()
    messages = requirement_review_surface(draft, None)
    assert [s["key"] for s in kit.data_model(messages)["sections"]] == list(draft.fields)
    assert not kit.slb_components(messages, "VerifierBadge")
    assert "Not verified yet" in kit.all_text(messages)
    assert kit.create_surface(messages)["surfaceId"] == "req_review_draft_req_01JD3XAMPLE000000000001_v2"


def test_requirement_review_refuses_a_report_for_another_version() -> None:
    draft = kit.requirement_draft()
    stale = kit.report_for(draft.model_copy(update={"version": 1}), "requirement_draft")
    with pytest.raises(ValueError):
        requirement_review_surface(draft, stale)


def test_requirement_review_can_carry_the_decision_block(ado: kit.Proposal) -> None:
    messages = requirement_review_surface(ado.subject, ado.report, action=ado.action)  # type: ignore[arg-type]
    assert sorted(set(kit.events(messages)) & DECISION_ACTIONS) == sorted(DECISION_ACTIONS)
    assert kit.slb_components(messages, "ApprovalCard")
    with pytest.raises(ValueError):
        requirement_review_surface(ado.subject, None, action=ado.action)  # type: ignore[arg-type]


# ------------------------------------------------------------------------------------------- readiness
def test_readiness_surface_collects_answers_for_open_questions() -> None:
    readiness, questions = kit.readiness(), kit.questions()
    messages = readiness_surface(kit.PROBLEM_ID, readiness, questions)
    model = kit.data_model(messages)
    [checklist] = kit.slb_components(messages, "ReadinessChecklist")
    assert _ui(messages, checklist, "items") == readiness.model_dump(mode="json")["items"]
    fields = {f["value"]["@path"]: f for f in kit.of_type(messages, "TextField")}
    assert set(fields) == {"/answers/q_ac", "/answers/q_edge"}  # the answered question gets no input
    assert model["answers"] == {"q_ac": "", "q_edge": ""}
    [picker] = kit.of_type(messages, "ChoicePicker")
    assert picker["value"] == {"@path": "/outreach/q_ac"} and picker["variant"] == "multipleSelection"
    assert picker["options"] == [{"label": "PERSON_9F2C1A (RTDS SRE)", "value": "PERSON_9F2C1A"}]
    [answer] = kit.buttons(messages, "answer_questions")
    assert answer["action"]["event"]["context"] == {"problemId": kit.PROBLEM_ID, "answers": {"@path": "/answers"}}
    [outreach] = kit.buttons(messages, "draft_outreach")
    assert outreach["action"]["event"]["context"] == {"problemId": kit.PROBLEM_ID,
                                                      "respondents": {"@path": "/outreach"}}
    raw = [c["text"] for c in kit.of_type(messages, "Text") if kit.unescape_md(c["text"]).endswith(
        kit.QUESTION_WITH_MARKDOWN)]
    assert raw and kit.QUESTION_WITH_MARKDOWN not in raw[0]  # markdown in evidence-derived text is escaped
    assert "answered" in kit.all_text(messages)
    why = [cl for cl in kit.slb_components(messages, "ClaimList") if _ui(messages, cl, "claims")]
    assert any(_ui(messages, cl, "claims")[0]["claim_id"] == "clm_q_ac" for cl in why)


# ------------------------------------------------------------------------------------------ duplicates
def test_duplicate_decision_surface_offers_choices_only_for_pending_candidates() -> None:
    candidates = kit.duplicate_candidates()
    messages = duplicate_decision_surface(kit.PROBLEM_ID, candidates)
    [table] = kit.slb_components(messages, "DataTable")
    assert [r["ref"] for r in _ui(messages, table, "rows")] == [c.ref for c in candidates]
    for event in ("confirm_merge", "link_duplicate", "mark_distinct"):
        contexts = [b["action"]["event"]["context"] for b in kit.buttons(messages, event)]
        assert contexts == [{"subjectId": kit.PROBLEM_ID, "candidateRef": c.ref, "candidateSystem": c.system}
                            for c in candidates[:2]]


# -------------------------------------------------------------------------------------- problem detail
def test_problem_detail_surface_shows_claims_signals_and_evidence() -> None:
    problem, signals = kit.product_problem(), kit.signals()
    messages = problem_detail_surface(problem, signals)
    [tabs] = kit.of_type(messages, "Tabs")
    assert [t["title"] for t in tabs["tabs"]] == ["Overview", "Signals", "Evidence"]
    tables = kit.slb_components(messages, "DataTable")
    rows = next(_ui(messages, t, "rows") for t in tables if "signal" in str(t["@accessibility"]["label"]).lower())
    assert [(r["signal_id"], r["permalink"]) for r in rows] == [(s.signal_id, s.permalink) for s in signals]
    panels = kit.slb_components(messages, "EvidencePanel")
    chips = kit.slb_components(messages, "CitationChip")
    assert len(panels) == len(chips) == 4  # unique (evidence, span) pairs cited by the problem's claims
    assert {_ui(messages, c, "evidenceId") for c in chips} == {"ev_pd_1", "ev_snow_1"}
    assert all(_ui(messages, p, "revealAllowed") is False for p in panels)
    assert _ui(messages, panels[0], "signal")["author"]["token"] == "PERSON_9F2C1A"
    revealed = problem_detail_surface(problem, signals, reveal_allowed=True)
    assert all(_ui(revealed, p, "revealAllowed") is True for p in kit.slb_components(revealed, "EvidencePanel"))
    text = kit.all_text(messages)
    assert "Partial data" in text and "servicenow" in text and "Low-confidence classification" in text
    [health] = kit.slb_components(messages, "SourceHealth")
    assert _ui(messages, health, "sources") == [{"source": "servicenow", "status": "unavailable", "healthy": False}]
    for event in ("start_requirement", "investigate"):
        [button] = kit.buttons(messages, event)
        assert button["action"]["event"]["context"] == {"problemId": problem.problem_id}
    [merge] = kit.buttons(messages, "confirm_merge")
    assert merge["action"]["event"]["context"] == {"subjectId": problem.problem_id, "candidateRef": "2830205",
                                                   "candidateSystem": "ado"}
    [split] = kit.buttons(messages, "split_signal")
    assert split["action"]["event"]["context"] == {"problemId": problem.problem_id,
                                                   "signalIds": {"@path": "/split/signal_ids"}}
    titles = {cl["label"] for cl in kit.slb_components(messages, "ClaimList")}
    assert {"Title", "Classification", "Affected customers", "Business impact"} <= titles


# --------------------------------------------------------------------------------------- problem inbox
def test_problem_inbox_is_loud_about_partial_data() -> None:
    problems = [kit.product_problem(), kit.second_problem()]
    messages = problem_inbox_surface(problems, kit.SOURCE_HEALTH)
    [health] = kit.slb_components(messages, "SourceHealth")
    assert _ui(messages, health, "sources") == [
        {"source": "ado", "status": "ok", "healthy": True},
        {"source": "pagerduty", "status": "ok", "healthy": True},
        {"source": "servicenow", "status": "unavailable", "healthy": False}]
    assert "Partial data" in kit.all_text(messages) and "servicenow" in kit.all_text(messages)
    [table] = kit.slb_components(messages, "DataTable")
    rows = _ui(messages, table, "rows")
    assert [r["problem_id"] for r in rows] == [p.problem_id for p in problems]
    assert rows[0]["title"] == problems[0].title.text and rows[0]["partial_sources"] == "servicenow"
    [opener] = kit.buttons(messages, "open_problem")
    assert opener["action"]["event"]["context"] == {"problemIds": {"@path": "/selection/problem_ids"}}
    pickers = {p["value"]["@path"]: p for p in kit.of_type(messages, "ChoicePicker")}
    assert [o["value"] for o in pickers["/selection/problem_ids"]["options"]] == [p.problem_id for p in problems]
    [filt] = kit.buttons(messages, "filter")
    assert filt["action"]["event"]["context"] == {"status": {"@path": "/filter/status"},
                                                  "classification": {"@path": "/filter/classification"}}
    healthy = problem_inbox_surface(problems, {"ado": "ok"})
    assert "Partial data" not in kit.all_text(healthy)
    empty = problem_inbox_surface([], {})
    assert not kit.buttons(empty, "open_problem") and "No problems" in kit.all_text(empty)


# ----------------------------------------------------------------------------------------- prototype
def test_prototype_surface_shows_preview_reuse_and_component_mapping() -> None:
    spec = kit.display_prototype_spec()
    messages = prototype_surface(spec, None, kit.IMAGES)
    [preview] = kit.slb_components(messages, "PrototypePreview")
    assert _ui(messages, preview, "images") == kit.IMAGES
    reuse = _ui(messages, preview, "reuse")
    assert reuse["reuse_ratio"] == 0.5 and reuse["min_reuse_ratio"] == 0.9 and reuse["new_patterns"] == 1
    assert "below the 90% DLS reuse target" in kit.all_text(messages)
    [mapping] = kit.slb_components(messages, "DataTable")
    keys = [r["component_key"] for r in _ui(messages, mapping, "rows")]
    assert "dls:button/primary" in keys
    justification = [c["claim_id"] for cl in kit.slb_components(messages, "ClaimList")
                     for c in _ui(messages, cl, "claims")]
    assert justification == ["clm_np_1"]
    assert not kit.slb_components(messages, "VerifierBadge") and not kit.buttons(messages)


def test_prototype_surface_with_a_pending_render_action() -> None:
    figma = kit.figma_proposal()
    messages = prototype_surface(figma.subject, figma.report, kit.IMAGES, action=figma.action)  # type: ignore[arg-type]
    assert sorted(kit.events(messages)) == sorted(DECISION_ACTIONS)
    assert "below the" not in kit.all_text(messages)


# ----------------------------------------------------------------------------------------- knowledge
def test_knowledge_review_surface_sections() -> None:
    article = kit.knowledge_article()
    messages = knowledge_review_surface(article, kit.report_for(article, "knowledge_article_draft"))
    sections = {s["key"]: s for s in kit.data_model(messages)["sections"]}
    assert list(sections) == ["title", "symptom", "root_cause", "resolution", "workaround"]
    assert sections["workaround"]["status"] == "missing" and sections["resolution"]["status"] == "inferred"
    text = kit.all_text(messages)
    assert "DrillOps" in text and "internal" in text and MISSING_TEXT in text
    assert "Inferred — needs your confirmation" in text
    other = kit.report_for(article.model_copy(update={"applies_to_product": "OptiSite"}), "knowledge_article_draft")
    with pytest.raises(ValueError):
        knowledge_review_surface(article, other)
    kb = kit.kb_proposal()
    decision = knowledge_review_surface(kb.subject, kb.report, action=kb.action)  # type: ignore[arg-type]
    assert sorted(kit.events(decision)) == sorted(DECISION_ACTIONS)


# ------------------------------------------------------------------------------------- investigation
def test_investigation_surface_timeline_and_next_steps() -> None:
    pack = kit.investigation_pack()
    messages = investigation_surface(pack)
    [timeline] = kit.slb_components(messages, "Timeline")
    assert [e["label"] for e in _ui(messages, timeline, "events")] == [c.text for c in pack.timeline]
    pickers = kit.of_type(messages, "ChoicePicker")
    [outcome] = [p for p in pickers if p["value"] == {"@path": "/outcome/selected"}]
    assert kit.data_model(messages)["outcome"]["selected"] == ["workaround"]
    assert {o["value"] for o in outcome["options"]} == {"workaround", "workflow_clarification", "defect",
                                                         "non_defect"}
    [confirm] = kit.buttons(messages, "confirm_outcome")
    assert confirm["action"]["event"]["context"] == {"packId": pack.pack_id, "outcome": {"@path": "/outcome/selected"}}
    for event in ("draft_kb", "draft_bug"):
        [button] = kit.buttons(messages, event)
        assert button["action"]["event"]["context"] == {"packId": pack.pack_id}
    [related] = kit.slb_components(messages, "DataTable")
    assert _ui(messages, related, "rows")[0]["signal_id"] == kit.SIG_SNOW


# --------------------------------------------------------------------------------------------- audit
def test_audit_surface_orders_entries_and_exports_the_range() -> None:
    messages = audit_surface(kit.ledger_entries())
    [table] = kit.slb_components(messages, "DataTable")
    assert [r["seq"] for r in _ui(messages, table, "rows")] == [1, 2, 3]
    [timeline] = kit.slb_components(messages, "Timeline")
    assert [e["kind"] for e in _ui(messages, timeline, "events")] == ["ACTION_PROPOSED", "DECISION_RECORDED",
                                                                     "TOKEN_ISSUED"]
    [export] = kit.buttons(messages, "export")
    assert export["action"]["event"]["context"] == {"fromSeq": 1, "toSeq": 3}
    [filt] = kit.buttons(messages, "filter")
    assert filt["action"]["event"]["context"] == {"entryTypes": {"@path": "/filter/entry_types"}}
    empty = audit_surface([])
    assert "No ledger entries" in kit.all_text(empty) and not kit.buttons(empty, "export")


# ------------------------------------------------------------------------------------ status & delete
def test_status_and_delete_surface() -> None:
    messages = status_surface("status-run#42", "Drafting *requirement*", ["Collecting evidence", "Verifying 3 claims"])
    surface = kit.create_surface(messages)
    assert surface["surfaceId"] == "status_run_42"
    raw = [c["text"] for c in kit.of_type(messages, "Text")]
    assert raw[0].startswith("## ") and kit.unescape_md(raw[0]) == "## Drafting *requirement*"
    assert kit.texts(messages)[1:] == ["Collecting evidence", "Verifying 3 claims"]
    assert delete_surface("status-run#42") == {"version": "1.0", "deleteSurface": {"surfaceId": "status_run_42"}}


# ------------------------------------------------------------------------------------- cross-cutting
def test_builders_are_deterministic_and_json_safe() -> None:
    first, second = kit.build_all_surfaces(), kit.build_all_surfaces()
    stable = {"readiness", "duplicates", "problem_detail", "problem_inbox", "problem_inbox_empty", "prototype",
              "knowledge_review", "investigation", "audit", "audit_empty", "status", "requirement_review",
              "requirement_review_unverified"}
    for name in stable:  # same verified input ⇒ byte-identical surface
        assert kit.canonical(first[name]) == kit.canonical(second[name]), name
    for messages in first.values():
        assert json.loads(json.dumps(messages)) == messages


def test_interactive_and_domain_components_are_labelled_for_assistive_tech() -> None:
    for name, messages in kit.build_all_surfaces().items():
        for component in kit.components(messages):
            interactive = component["component"] in {"Button", "TextField", "ChoicePicker", "Tabs"}
            if interactive or component.get("@catalogId") == SLB_CATALOG_ID:
                label = component.get("@accessibility", {}).get("label")
                assert isinstance(label, str) and label.strip(), (name, component["id"])


def test_every_emitted_event_is_a_documented_surface_event() -> None:
    for name, messages in kit.build_all_surfaces().items():
        assert set(kit.events(messages)) <= SURFACE_EVENTS, name
    assert set(ACTION_LABELS) == set(ActionType)  # every catalogued action gets a human label


def test_ui_action_helpers() -> None:
    message = {"version": "1.0", "action": {"name": "request_changes", "surfaceId": "approve_act_1",
                                            "sourceComponentId": "changes_btn", "timestamp": "2026-11-24T10:00:00Z",
                                            "context": {"actionId": "act_1", "comment": "Split the scope"}}}
    action = parse_action(message)
    assert isinstance(action, UiAction)
    assert (action.decision, action.action_id, action.comment) == (Decision.REQUEST_CHANGES, "act_1",
                                                                   "Split the scope")
    assert is_decision(action) and is_decision("approve_action") and not is_decision("open_problem")
    assert DECISION_ACTIONS == frozenset({"approve_action", "reject_action", "request_changes"})
    other = parse_action({**message, "action": {**message["action"], "name": "open_problem", "context": {}}})
    assert other.decision is None and other.action_id is None and other.comment == ""
