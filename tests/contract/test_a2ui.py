"""A2UI v1.0 wire-format contract (docs/11 §2, §4, §6, §8) — CI pins the protocol, not the SDK.

* Every message every builder emits validates against the vendored v1.0 ``agent_to_renderer.json`` (+
  ``common_types.json``), with the ``catalog.json`` placeholder mapped to basic ∪ SLB catalogs.
* Every SLB component instance validates against ``slb.com:pm_workbench_v1`` — its wire form *and* the data its
  bindings resolve to.
* Ids are UAX #31 identifiers; trees are complete (one root, no dangling or orphan ids); the A2UI core SDK's own
  renderer-side processor accepts every surface.
* Renderer→agent ``action`` messages are parsed strictly; A2A packaging uses the A2UI MIME type.
"""
from __future__ import annotations

import importlib.resources
import json
import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import urljoin

import jsonschema
import pytest
import referencing
from a2a.types import DataPart, Message, Part, Role, TextPart
from a2ui.a2a import A2UI_MIME_TYPE, get_a2ui_extension_uri, get_a2ui_messages, is_a2ui_part
from a2ui.a2a import renderer_capabilities_from_message
from a2ui.core import (
    STRICT_VALIDATION,
    Catalog,
    MessageProcessor,
    MessageProcessorOptions,
    get_agent_to_renderer_schema_json,
    get_common_types_schema_json,
    is_valid_uax31_identifier,
)
from a2ui.core.basic_catalog import BasicCatalog
from a2ui.core.schema import ProtocolVersion
from a2ui.utils import read_renderer_capabilities
from referencing.jsonschema import DRAFT202012

from pm_agent.a2ui import (
    A2UI_VERSION,
    BASIC_CATALOG_ID,
    EXTENSION_URI,
    MIME_TYPE,
    RENDERER_CAPABILITIES_KEY,
    SLB_CATALOG_ID,
    agent_extension,
    load_slb_catalog,
    renderer_capabilities,
    supports_a2ui,
    to_a2a_parts,
)
from pm_agent.a2ui.actions import DECISION_ACTIONS, InvalidUiAction, is_decision, parse_action
from pm_agent.a2ui.surfaces import delete_surface
from pm_agent.schemas import (
    ActionType,
    CheckStatus,
    ClaimKind,
    FieldStatus,
    ReadinessItemName,
    ReadinessStatus,
    VerificationOutcome,
)
from tests.fixtures.a2ui import a2ui_testkit as kit

pytestmark = pytest.mark.contract


# ----------------------------------------------------------------------------------- schema registry
def _fixture(name: str) -> dict[str, Any]:
    return json.loads((kit.FIXTURES / name).read_text(encoding="utf-8"))  # type: ignore[no-any-return]


A2R = _fixture("agent_to_renderer.json")
COMMON = _fixture("common_types.json")
R2A = _fixture("renderer_to_agent.json")
BASIC = _fixture("basic_catalog.json")
SLB = load_slb_catalog()
CATALOG_PLACEHOLDER = urljoin(A2R["$id"], "catalog.json")  # what `catalog.json#/$defs/anyComponent` resolves to


def _catalog_union(*catalogs: dict[str, Any]) -> dict[str, Any]:
    """The document mapped onto ``catalog.json``: mixable catalogs (v1.0 §Mixable catalogs) as one union."""
    functions = [{"$ref": f"{c['$id']}#/functions/{name}"} for c in catalogs for name in c.get("functions", {})]
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": CATALOG_PLACEHOLDER,
            "$defs": {"anyComponent": {"oneOf": [{"$ref": f"{c['$id']}#/components/{name}"}
                                                 for c in catalogs for name in c["components"]]},
                      "anyFunction": {"oneOf": functions} if functions else {"not": {}}}}


def _registry(placeholder: dict[str, Any]) -> referencing.Registry:  # type: ignore[type-arg]
    docs = {A2R["$id"]: A2R, COMMON["$id"]: COMMON, R2A["$id"]: R2A, CATALOG_PLACEHOLDER: placeholder,
            BASIC["$id"]: BASIC, SLB["$id"]: SLB}
    for catalog in (BASIC, SLB):  # catalogs reference `common_types.json#/$defs/…` relative to their own $id
        docs.setdefault(urljoin(catalog["$id"], "common_types.json"), COMMON)
    return referencing.Registry().with_resources(  # type: ignore[no-any-return]
        (uri, DRAFT202012.create_resource(doc)) for uri, doc in docs.items())


ENVELOPE = jsonschema.Draft202012Validator(A2R, registry=_registry(_catalog_union(BASIC, SLB)))
SLB_COMPONENT = jsonschema.Draft202012Validator({"$ref": f"{A2R['$id']}#/$defs/Component"},
                                                registry=_registry(_catalog_union(SLB)))
RENDERER_TO_AGENT = jsonschema.Draft202012Validator(R2A, registry=_registry(_catalog_union(BASIC, SLB)))


def _errors(validator: jsonschema.Draft202012Validator, instance: Any) -> list[str]:
    """Readable errors: for ``oneOf``/``anyOf`` failures, report the most relevant sub-error (what actually broke)."""
    found = []
    for error in validator.iter_errors(instance):
        best = jsonschema.exceptions.best_match([error]) or error
        found.append(f"{'/'.join(map(str, best.absolute_path))}: {best.message[:300]}")
    return found


# ------------------------------------------------------------------------------------------- corpus
SURFACES = kit.build_all_surfaces()
NAMES = sorted(SURFACES)
UAX31_ASCII = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
CHILD_PROPS = ("child", "trigger", "content")


def _children(component: dict[str, Any]) -> Iterator[str]:
    for prop in CHILD_PROPS:
        if isinstance(component.get(prop), str):
            yield component[prop]
    children = component.get("children")
    if isinstance(children, list):
        yield from children
    elif isinstance(children, dict):
        yield children["componentId"]
    for tab in component.get("tabs", []):
        yield tab["child"]


# ---------------------------------------------------------------------------------- pins & constants
def _as_json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def test_vendored_schemas_are_the_wire_format_the_installed_sdk_speaks() -> None:
    assert _as_json(get_agent_to_renderer_schema_json(ProtocolVersion.V1_0)) == A2R
    assert _as_json(get_common_types_schema_json(ProtocolVersion.V1_0)) == COMMON
    assert BasicCatalog(A2UI_VERSION).to_json() == BASIC
    assert BASIC_CATALOG_ID == BASIC["catalogId"] == BASIC["$id"] == BasicCatalog("1.0").catalog_id


def test_protocol_constants_match_spec_and_sdk() -> None:
    assert A2UI_VERSION == "1.0"
    assert SLB_CATALOG_ID == "slb.com:pm_workbench_v1"
    assert BASIC_CATALOG_ID == "https://a2ui.org/specification/v1_0/catalogs/basic/catalog.json"
    assert EXTENSION_URI == get_a2ui_extension_uri("1.0") == "https://a2ui.org/a2a-extension/a2ui/v1.0"
    assert MIME_TYPE == A2UI_MIME_TYPE == "application/a2ui+json"
    assert RENDERER_CAPABILITIES_KEY == "a2uiRendererCapabilities"


# ------------------------------------------------------------------------------- agent → renderer
@pytest.mark.parametrize("name", NAMES)
def test_every_message_validates_against_the_v1_envelope(name: str) -> None:
    for message in SURFACES[name]:
        assert message["version"] == "1.0"
        assert len(set(message) - {"version"}) == 1
        assert not _errors(ENVELOPE, message), _errors(ENVELOPE, message)
    surface = kit.create_surface(SURFACES[name])
    assert surface["catalogId"] == BASIC_CATALOG_ID and surface["sendDataModel"] is False


def test_delete_surface_message_validates() -> None:
    message = delete_surface("approve_act_01")
    assert message == {"version": "1.0", "deleteSurface": {"surfaceId": "approve_act_01"}}
    assert not _errors(ENVELOPE, message)


def test_the_validators_really_reject_bad_payloads() -> None:
    """Guard against vacuous oracles: unknown props, bad enums, v0.9 leftovers and catalog-less SLB components."""
    good = SURFACES["approval_ado"][0]
    bad_prop = json.loads(json.dumps(good))
    bad_prop["createSurface"]["components"][0]["bogus"] = True
    bad_enum = json.loads(json.dumps(good))
    next(c for c in bad_enum["createSurface"]["components"] if c["component"] == "Button")["variant"] = "danger"
    theme = json.loads(json.dumps(good))
    theme["createSurface"]["theme"] = {"primaryColor": "#00f"}  # removed in v1.0
    bad_a11y = json.loads(json.dumps(good))
    bad_a11y["createSurface"]["components"][0]["@accessibility"] = {"label": "x", "role": "region"}
    for message in (bad_prop, bad_enum, theme, bad_a11y):
        assert _errors(ENVELOPE, message)
    no_catalog = json.loads(json.dumps(good))
    for component in no_catalog["createSurface"]["components"]:
        component.pop("@catalogId", None)  # SLB components would then resolve against the basic catalog
    assert _processor().process_messages_with_errors([no_catalog])
    assert not _processor().process_messages_with_errors([good])


@pytest.mark.parametrize("name", NAMES)
def test_every_component_resolves_to_exactly_one_catalog(name: str) -> None:
    for component in kit.components(SURFACES[name]):
        catalog = component.get("@catalogId", BASIC_CATALOG_ID)
        assert catalog in (BASIC_CATALOG_ID, SLB_CATALOG_ID)
        source = SLB if catalog == SLB_CATALOG_ID else BASIC
        assert component["component"] in source["components"], component


@pytest.mark.parametrize("name", NAMES)
def test_every_slb_component_instance_validates_against_the_slb_catalog(name: str) -> None:
    model = kit.data_model(SURFACES[name])
    for component in kit.slb_components(SURFACES[name]):
        assert not _errors(SLB_COMPONENT, component), (component, _errors(SLB_COMPONENT, component))
        resolved = kit.resolve(component, model)  # the data the renderer will hand to the SLB component
        assert not _errors(SLB_COMPONENT, resolved), (component["id"], _errors(SLB_COMPONENT, resolved))


@pytest.mark.parametrize("name", NAMES)
def test_ids_are_uax31_identifiers_and_trees_are_complete(name: str) -> None:
    surface = kit.create_surface(SURFACES[name])
    ids = [c["id"] for c in surface["components"]]
    for identifier in [surface["surfaceId"], *ids]:
        assert UAX31_ASCII.match(identifier), identifier
        assert identifier.isidentifier() and is_valid_uax31_identifier(identifier), identifier
    assert len(ids) == len(set(ids)), "duplicate component ids"
    assert ids.count("root") == 1
    by_id = kit.component_map(SURFACES[name])
    seen, stack = set(), ["root"]
    while stack:
        current = stack.pop()
        assert current in by_id, f"dangling reference {current!r}"
        if current not in seen:
            seen.add(current)
            stack.extend(_children(by_id[current]))
    assert seen == set(ids), f"orphans: {set(ids) - seen}"


@pytest.mark.parametrize("name", NAMES)
def test_every_data_binding_resolves_in_the_initial_data_model(name: str) -> None:
    model = kit.data_model(SURFACES[name])
    for component in kit.components(SURFACES[name]):
        for path in kit.bound_paths(component):
            kit.resolve_pointer(model, path)  # KeyError / IndexError ⇒ dangling binding


def test_data_models_carry_no_reserved_at_keys() -> None:
    def keys(value: Any) -> Iterator[str]:
        if isinstance(value, dict):
            for key, item in value.items():
                yield key
                yield from keys(item)
        elif isinstance(value, list):
            for item in value:
                yield from keys(item)

    for name in NAMES:
        assert not [k for k in keys(kit.data_model(SURFACES[name])) if k.startswith("@")], name


def _processor() -> MessageProcessor:
    slb = Catalog.from_json(load_slb_catalog(), protocol_version=A2UI_VERSION)
    return MessageProcessor(catalogs=[BasicCatalog(A2UI_VERSION), slb],
                            options=MessageProcessorOptions(validation_config=STRICT_VALIDATION))


@pytest.mark.parametrize("name", NAMES)
def test_the_a2ui_core_renderer_processor_accepts_every_surface(name: str) -> None:
    """Independent oracle: the SDK's strict renderer-side processor (integrity, catalogs, ids, bindings)."""
    surface_id = kit.create_surface(SURFACES[name])["surfaceId"]
    errors = _processor().process_messages_with_errors([*SURFACES[name], delete_surface(surface_id)])
    assert not errors, errors


# ------------------------------------------------------------------------------------- SLB catalog
DOC11_COMPONENTS = {  # docs/11 §4: component → key props
    "CitationChip": {"evidenceId", "sourceSystem", "recordId", "permalink"},
    "EvidencePanel": {"quote", "span", "signal", "revealAllowed"},
    "ClaimList": {"claims", "fieldStatus"},
    "VerifierBadge": {"report"},
    "ApprovalCard": {"action", "report", "diff"},
    "FieldDiff": {"before", "after"},
    "ReadinessChecklist": {"items"},
    "DataTable": {"columns", "rows"},
    "Timeline": {"events"},
    "TraceGraph": {"nodes", "links"},
    "PrototypePreview": {"images", "reuse"},
    "SourceHealth": {"sources"},
}
ALLOWED_COMMON_REFS = {"ComponentId", "Child", "ChildList", "DynamicString", "DynamicNumber", "DynamicBoolean",
                       "DynamicStringList", "DynamicValue", "AccessibilityAttributes", "CheckRule", "Checkable",
                       "Action", "DataBinding", "FunctionCall"}


def _refs(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        if isinstance(value.get("$ref"), str):
            yield value["$ref"]
        for item in value.values():
            yield from _refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from _refs(item)


def test_slb_catalog_is_a_valid_v1_catalog() -> None:
    catalog = load_slb_catalog()
    assert catalog["$id"] == catalog["catalogId"] == SLB_CATALOG_ID
    assert catalog["protocolVersion"] == "1.0"
    assert set(catalog) <= {"$schema", "$id", "protocolVersion", "title", "description", "catalogId", "instructions",
                            "metadata", "components", "functions", "$defs"}
    jsonschema.Draft202012Validator.check_schema(catalog)
    assert Catalog.from_json(catalog, protocol_version="1.0").catalog_id == SLB_CATALOG_ID
    assert set(catalog["components"]) == set(DOC11_COMPONENTS)
    assert "Surface" not in catalog["components"]
    for name, schema in catalog["components"].items():
        assert is_valid_uax31_identifier(name)
        assert schema["type"] == "object" and schema["properties"]["component"] == {"const": name}
        assert "component" in schema["required"]
        assert DOC11_COMPONENTS[name] <= set(schema["properties"]), name
        assert all(is_valid_uax31_identifier(prop) for prop in schema["properties"]), name
        assert not {"oneOf", "anyOf", "allOf"} & set(schema), f"{name}: no combinators at the component root"
    listed = {ref["$ref"] for ref in catalog["$defs"]["anyComponent"]["oneOf"]}
    assert listed == {f"#/components/{name}" for name in catalog["components"]}
    for ref in _refs(catalog):
        if ref.startswith("common_types.json#/$defs/"):
            assert ref.rsplit("/", 1)[1] in ALLOWED_COMMON_REFS, ref
        else:
            assert ref.startswith(("#/components/", "#/$defs/")), ref
            assert ref not in ("#/$defs/anyComponent", "#/$defs/anyFunction") or ref in listed, ref


def test_slb_catalog_ships_as_package_data() -> None:
    resource = importlib.resources.files("pm_agent.a2ui").joinpath("catalogs/slb_pm_workbench_v1.json")
    assert resource.is_file()
    assert json.loads(resource.read_text(encoding="utf-8")) == load_slb_catalog()
    first = load_slb_catalog()
    first["components"].clear()
    assert load_slb_catalog()["components"], "load_slb_catalog must return a fresh copy"


def test_slb_catalog_enums_track_the_pm_agent_schemas() -> None:
    """The data contracts enumerate docs/10 enums; a new value in pm_agent.schemas must extend the catalog."""
    defs = load_slb_catalog()["$defs"]
    for name, enum in (("ActionType", ActionType), ("ClaimKind", ClaimKind), ("FieldStatus", FieldStatus),
                       ("ReadinessItemName", ReadinessItemName), ("ReadinessStatus", ReadinessStatus),
                       ("CheckStatus", CheckStatus), ("VerificationOutcome", VerificationOutcome)):
        assert set(defs[name]["enum"]) == {member.value for member in enum}, name


def test_agent_card_extension_advertises_v1_and_our_catalogs() -> None:
    extension = agent_extension()
    assert extension.uri == EXTENSION_URI
    assert extension.params == {"supportedCatalogIds": [SLB_CATALOG_ID, BASIC_CATALOG_ID]}


# ------------------------------------------------------------------------------- renderer → agent
SPEC_EXAMPLE_ACTION = {"version": "1.0", "action": {
    "name": "submitForm", "surfaceId": "contact_form_1", "sourceComponentId": "submit_button",
    "timestamp": "2026-06-02T08:57:23Z", "userMessage": "Submitted contact form", "context": {"isSubscribed": True}}}


def _decision(**context: Any) -> dict[str, Any]:
    return {"version": "1.0", "action": {"name": "approve_action", "surfaceId": "approve_act_1",
                                         "sourceComponentId": "approve_btn", "timestamp": "2026-11-24T10:00:00+00:00",
                                         "context": context}}


def test_parse_action_accepts_spec_valid_actions() -> None:
    assert not _errors(RENDERER_TO_AGENT, SPEC_EXAMPLE_ACTION)
    action = parse_action(SPEC_EXAMPLE_ACTION)
    assert (action.name, action.surface_id, action.source_component_id) == ("submitForm", "contact_form_1",
                                                                            "submit_button")
    assert action.context == {"isSubscribed": True} and action.user_message == "Submitted contact form"
    assert action.timestamp.tzinfo is not None and not is_decision(action)
    decision = _decision(actionId="act_1", comment="ok")
    assert not _errors(RENDERER_TO_AGENT, decision)
    assert parse_action(decision).action_id == "act_1"


def _without(key: str) -> dict[str, Any]:
    message = json.loads(json.dumps(SPEC_EXAMPLE_ACTION))
    del message["action"][key]
    return message


SCHEMA_INVALID = {
    "missing name": _without("name"),
    "missing surfaceId": _without("surfaceId"),
    "missing sourceComponentId": _without("sourceComponentId"),
    "missing timestamp": _without("timestamp"),
    "missing context": _without("context"),
    "wrong version": {**SPEC_EXAMPLE_ACTION, "version": "0.9"},
    "missing version": {"action": SPEC_EXAMPLE_ACTION["action"]},
    "extra top-level key": {**SPEC_EXAMPLE_ACTION, "error": {"code": "X", "message": "m", "surfaceId": "s"}},
    "context not an object": {"version": "1.0", "action": {**SPEC_EXAMPLE_ACTION["action"], "context": ["x"]}},
    "name not a string": {"version": "1.0", "action": {**SPEC_EXAMPLE_ACTION["action"], "name": 7}},
}
SEMANTICALLY_INVALID = {  # schema-valid (format is an annotation) but unsafe to act on
    "naive timestamp": {"version": "1.0", "action": {**SPEC_EXAMPLE_ACTION["action"],
                                                     "timestamp": "2026-06-02T08:57:23"}},
    "not a timestamp": {"version": "1.0", "action": {**SPEC_EXAMPLE_ACTION["action"], "timestamp": "yesterday"}},
    "empty name": {"version": "1.0", "action": {**SPEC_EXAMPLE_ACTION["action"], "name": ""}},
    "decision without actionId": _decision(comment="looks good"),
    "decision with blank actionId": _decision(actionId=" "),
    "decision with non-string comment": _decision(actionId="act_1", comment=5),
    "decision with malformed payload hash": _decision(actionId="act_1", payloadSha256="md5:abc"),
    "decision with non-integer version": _decision(actionId="act_1", actionVersion="2"),
}
NOT_AN_ACTION = {
    "callAgentFunction": {"version": "1.0", "callAgentFunction": {"surfaceId": "s", "functionCallId": "f1",
                                                                  "callFunction": {"@call": "required",
                                                                                   "args": {"value": "x"}}}},
    "error": {"version": "1.0", "error": {"code": "VALIDATION_FAILED", "surfaceId": "s", "path": "/c",
                                          "message": "bad"}},
}


@pytest.mark.parametrize("label", sorted(SCHEMA_INVALID))
def test_parse_action_rejects_schema_invalid_messages(label: str) -> None:
    assert _errors(RENDERER_TO_AGENT, SCHEMA_INVALID[label]), "fixture must be invalid per renderer_to_agent.json"
    with pytest.raises(InvalidUiAction):
        parse_action(SCHEMA_INVALID[label])


@pytest.mark.parametrize("label", sorted(SEMANTICALLY_INVALID))
def test_parse_action_rejects_unsafe_actions(label: str) -> None:
    with pytest.raises(InvalidUiAction):
        parse_action(SEMANTICALLY_INVALID[label])


@pytest.mark.parametrize("label", sorted(NOT_AN_ACTION))
def test_parse_action_only_accepts_action_messages(label: str) -> None:
    assert not _errors(RENDERER_TO_AGENT, NOT_AN_ACTION[label])
    with pytest.raises(InvalidUiAction):
        parse_action(NOT_AN_ACTION[label])


def test_parse_action_rejects_non_objects() -> None:
    for junk in (None, [], "approve", 42):
        with pytest.raises(InvalidUiAction):
            parse_action(junk)  # type: ignore[arg-type]
    assert issubclass(InvalidUiAction, ValueError)


def test_decision_buttons_round_trip_through_a_simulated_renderer() -> None:
    proposal = kit.ado_proposal()
    from pm_agent.a2ui.surfaces import approval_surface  # pylint: disable=import-outside-toplevel

    messages = approval_surface(proposal.action, proposal.subject, proposal.report)
    seen = set()
    for button in kit.buttons(messages):
        name = button["action"]["event"]["name"]
        if name not in DECISION_ACTIONS:
            continue
        message = kit.click(messages, button)
        assert not _errors(RENDERER_TO_AGENT, message), _errors(RENDERER_TO_AGENT, message)
        action = parse_action(message)
        assert is_decision(action) and action.action_id == proposal.action.action_id
        assert action.context["payloadSha256"] == proposal.action.payload_sha256
        assert action.context["actionVersion"] == proposal.action.version and action.comment == ""
        seen.add(name)
    assert seen == set(DECISION_ACTIONS)


# ------------------------------------------------------------------------------------ A2A transport
def test_to_a2a_parts_wraps_each_message_in_an_a2ui_datapart() -> None:
    messages = [*SURFACES["status"], delete_surface("status-run#42")]
    parts = to_a2a_parts(messages)
    assert len(parts) == len(messages)
    for part, message in zip(parts, messages):
        assert isinstance(part, Part) and isinstance(part.root, DataPart)
        assert part.root.metadata == {"mimeType": MIME_TYPE} and part.root.kind == "data"
        assert part.root.data == message and is_a2ui_part(part)
        json.dumps(part.model_dump(mode="json", by_alias=True))
    assert get_a2ui_messages(parts) == messages


def test_to_a2a_parts_refuses_non_v1_messages() -> None:
    for message in ({"deleteSurface": {"surfaceId": "x"}}, {"version": "0.9", "deleteSurface": {"surfaceId": "x"}}):
        with pytest.raises(ValueError):
            to_a2a_parts([message])


def test_renderer_capabilities_are_what_the_sdk_reads_back() -> None:
    capabilities = renderer_capabilities()
    assert capabilities == {"1.0": {"supportedCatalogIds": [SLB_CATALOG_ID, BASIC_CATALOG_ID]}}
    metadata = {RENDERER_CAPABILITIES_KEY: capabilities}
    assert read_renderer_capabilities(metadata, "1.0") == capabilities
    message = Message(message_id="m1", role=Role.user, parts=[Part(root=TextPart(text="hi"))], metadata=metadata)
    assert renderer_capabilities_from_message(message) == capabilities
    assert supports_a2ui(metadata) and supports_a2ui(message.metadata)
    capabilities["1.0"]["supportedCatalogIds"].clear()
    assert renderer_capabilities()["1.0"]["supportedCatalogIds"], "must return a fresh dict"


@pytest.mark.parametrize("metadata", [
    None, {}, {RENDERER_CAPABILITIES_KEY: None}, {RENDERER_CAPABILITIES_KEY: []},
    {RENDERER_CAPABILITIES_KEY: {"0.9": {"supportedCatalogIds": [SLB_CATALOG_ID, BASIC_CATALOG_ID]}}},
    {RENDERER_CAPABILITIES_KEY: {"supportedCatalogIds": [SLB_CATALOG_ID, BASIC_CATALOG_ID]}},  # flat shape
    {RENDERER_CAPABILITIES_KEY: {"1.0": {"supportedCatalogIds": [BASIC_CATALOG_ID]}}},
    {RENDERER_CAPABILITIES_KEY: {"1.0": {"supportedCatalogIds": [SLB_CATALOG_ID]}}},
    {RENDERER_CAPABILITIES_KEY: {"1.0": {"supportedCatalogIds": "everything"}}},
    {RENDERER_CAPABILITIES_KEY: {"1.0": {}}},
    {"a2uiClientCapabilities": {"v0.9": {"supportedCatalogIds": [SLB_CATALOG_ID, BASIC_CATALOG_ID]}}},
])
def test_supports_a2ui_fails_closed(metadata: Any) -> None:
    """Spec (A2A extension §Capability scoping): no v1.0 capabilities on the turn ⇒ no A2UI on the turn."""
    assert supports_a2ui(metadata) is False


def test_supports_a2ui_can_require_fewer_catalogs() -> None:
    basic_only = {RENDERER_CAPABILITIES_KEY: {"1.0": {"supportedCatalogIds": [BASIC_CATALOG_ID]}}}
    assert supports_a2ui(basic_only, catalogs=[BASIC_CATALOG_ID])
    assert not supports_a2ui(basic_only)
