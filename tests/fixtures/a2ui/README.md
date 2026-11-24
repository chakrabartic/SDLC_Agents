# A2UI v1.0 schema fixtures (vendored)

Pinned copies of the A2UI **v1.0** wire-format schemas used by `tests/contract/test_a2ui.py`
(`.research_cache/` is git-ignored, so the contract tests must not depend on it).
Files are kept **verbatim**: their original `$id` / `$ref` values are unchanged so `$ref`
resolution works exactly as upstream intends (`catalog.json` is the catalog placeholder the
envelope schema references; the tests map it to the basic + SLB catalogs).

| File | Source | Retrieved | SHA-256 |
|---|---|---|---|
| `agent_to_renderer.json` | `github.com/a2ui-project/a2ui` · `specification/v1_0/json/agent_to_renderer.json` (via `.research_cache/docs-2026-11/a2ui/v1_0-agent_to_renderer.json`) | 2026-11-24 | `2f575f783fdda0e4d175fe5d331661655e4a4901613262322792dae7e823761d` |
| `common_types.json` | `specification/v1_0/json/common_types.json` (via `.research_cache/.../v1_0-common_types.json`) | 2026-11-24 | `c4e78305be1de73eecb37f8179ba169730250611703683af291b18135a6430dc` |
| `renderer_to_agent.json` | `specification/v1_0/json/renderer_to_agent.json` (via `.research_cache/.../v1_0-renderer_to_agent.json`) | 2026-11-24 | `58ecde02101f0f37566b5e238dc6ef6af628c3ff312fce0e17ab7670b7215465` |
| `basic_catalog.json` | **Not in the research cache.** Generated from the installed SDK: `a2ui-agent-sdk==0.12.1` → `a2ui.core.basic_catalog.BasicCatalog("1.0").to_json()` (`json.dumps(indent=2)`) | 2026-11-24 | `b8a252769849f2aad15cd81f8202ee93e64afbf8839263459cd9821df5d1da09` |

Notes

* The three specification files are byte-identical to the research-cache downloads, and the contract tests
  additionally assert they equal what the installed SDK bundles
  (`a2ui.core.get_agent_to_renderer_schema_json` / `get_common_types_schema_json`), so an SDK upgrade that
  changes the wire format fails CI loudly.
* `basic_catalog.json`: upstream also publishes `catalogs/basic/v1/catalog.json` (git blob
  `2b1bd647f95c67ee1052c76197177834cbb03ac9`, 41 967 bytes, per the cached repository tree). Content equality
  with the SDK-generated copy is **UNCONFIRMED** (the upstream file was not downloaded; formatting differs).
* `tests/fixtures/a2ui/a2ui_testkit.py` holds the **synthetic** entities (no SLB data) and message-inspection
  helpers shared by the A2UI unit and contract tests.
