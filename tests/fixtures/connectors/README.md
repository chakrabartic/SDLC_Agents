# Connector fixtures (synthetic)

Everything here is **synthetic** and vendor-shaped (no SLB data, no real people). Hosts are `example.*` /
`example.invalid`; customers are the config gazetteer's synthetic organisations (Contoso Energy/CNE, Fabrikam
Petroleum/FPC, Northwind Offshore/NWO).

| Directory | Vendor shape | Loaded by |
|---|---|---|
| `ado/workitems.json` | `workitemsbatch` (`$expand=Relations`) + `workItems/{id}/comments` (7.1-preview.4) | `FixtureAdoConnector` |
| `servicenow/tables.json` | Table API rows with `sysparm_display_value=all` + `sys_journal_field` | `FixtureServiceNowConnector` |
| `pagerduty/incidents.json` | REST v2 `/incidents`, `/incidents/{id}/notes`, `/incidents/{id}/log_entries` | `FixturePagerDutyConnector` |
| `msgraph/mail.json` | `messages/delta` (final page) | `FixtureGraphMailConnector` |
| `msgraph/drive.json` | `root/delta`, extracted item text, workbook `usedRange` | `FixtureGraphDriveConnector` |
| `figma/library.json` | team components / component sets, file nodes, file comments | `FixtureFigmaConnector` |
| `kb/articles.json` | backend-agnostic KB articles (backend = Q-01) | `FixtureKbAdapter` |

`golden_manifest.json` documents the golden scenario consumed by W1 (`pm_agent.connectors.fixtures.golden_connectors`):

* **P1** "standardized log channel not populated after a late mapping-file upload" — PagerDuty incident #90000123
  (+ note + log entries), ServiceNow case CS9000123 whose work note references `PD #90000123` and the ADO bug URL,
  a ServiceNow incident INC9000456 that mirrors the PagerDuty text (**near-duplicate**), ADO bug 9100123 + comment,
  a two-message email thread and a OneDrive tracker row. No acceptance criteria anywhere (**readiness gap**).
* **P2** "bulk export of depth logs to the rig WITSML store" — ADO feature 9100456, two emails from different
  customers (FPC, NWO), discovery notes and a tracker row.
* The PagerDuty incident is delivered twice at the same version (**exact duplicate**).
* Secrets (`Password=hunter2`, a bearer token) and PII (emails, phone numbers) appear in free text to prove that
  connectors redact before anything leaves them.
