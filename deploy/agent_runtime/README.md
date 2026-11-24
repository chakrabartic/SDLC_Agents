# Agent Runtime deployment (pm-agent-core · signal-ingestion · commit-executor)

The three ADK 2.x agents run on **Agent Runtime** (Gemini Enterprise Agent Platform; resource type still
`reasoningEngines`). They are deployed by CI with the Agent Platform SDK, not from `deploy/Dockerfile`
(docs/12 §2, ADR-002). Infrastructure they depend on comes from `infra/terraform` (`terraform output
agent_runtime_deploy_config`).

| Deployment | ADK app object | Identity | Gateways | Scaling (docs/12 §2) |
|---|---|---|---|---|
| `pm-agent-core` | `pm_agent.core_app:app` | `AID-CORE` | egress **and** ingress (Workbench BFF via A2A/SSE) | min 1 · max 20 · `{"cpu": "4", "memory": "8Gi"}` |
| `signal-ingestion` | `pm_agent.ingestion_app:app` | `AID-INGEST` | egress **and** ingress (Cloud Scheduler) | min 0–1 · max 5 |
| `commit-executor` | `pm_agent.executor_app:app` | `AID-COMMIT` | egress (A2A caller access is engine IAM: only `AID-CORE`) | min 1 · max 3 |

## 1. Immutable-at-first-deploy settings

> [!IMPORTANT]
> `identity_type=AGENT_IDENTITY` and `agent_gateway_config` must be set when the engine is **created**. Patching
> `agentGatewayConfig` later does not change `identity_type`, and Semantic Governance needs both from creation.
> Deployments that set `agent_gateway_config` without `AGENT_IDENTITY` fail validation; `service_account` must
> **not** be set with `AGENT_IDENTITY`.

## 2. `client.runtimes.create` config keys

| Key | Value | Source |
|---|---|---|
| `display_name` | `pm-agent-core` / `signal-ingestion` / `commit-executor` | docs/12 §2 |
| `agent_framework` | `"google-adk"` | docs/12 §2 |
| `identity_type` | `AGENT_IDENTITY` (`types.IdentityType.AGENT_IDENTITY`) — **immutable** | docs/08 §3.1 |
| `agent_gateway_config` | `{"agent_to_anywhere_config": {"agent_gateway": <egress_gateway_id>}, "client_to_agent_config": {"agent_gateway": <ingress_gateway_id>}}` — **immutable**; executor: egress only | `terraform output agent_runtime_deploy_config` |
| `psc_interface_config` | `{"network_attachment": <agent_runtime_network_attachment_id>}` (max_instances ≤ 100 with PSC-I / VPC-SC) | Terraform `network` |
| `encryption_spec` | `{"kms_key_name": <cmek-agent-runtime key>}` | Terraform `kms` |
| `min_instances` / `max_instances` / `resource_limits` / `container_concurrency` | per table above (`container_concurrency` from load test) | docs/12 §2 |
| `env_vars` | `PM_AGENT_ENV`, `GOOGLE_GENAI_USE_VERTEXAI=true`, `GOOGLE_GENAI_USE_ENTERPRISE=true`, `PM_AGENT_VERIFIER_KEY`, `PM_AGENT_APPROVAL_KEY`, `PM_AGENT_CONFIG` — **never** `GOOGLE_CLOUD_PROJECT` / `GOOGLE_CLOUD_LOCATION` (platform-provided) | lead alignment, docs/02 P2 |

```python
# Design sketch — CI deploy step. Client construction in google-cloud-agentplatform 2.x is UNCONFIRMED (docs/12 §2,
# docs/02 P3); the config keys below are verified.
import json, subprocess
from vertexai import types  # module path per installed SDK

tf = json.loads(subprocess.check_output(
    ["terraform", "-chdir=infra/terraform/envs/dev", "output", "-json", "agent_runtime_deploy_config"]))

from pm_agent.core_app import app  # ADK App

remote = client.runtimes.create(
    agent=app,
    config={
        "display_name": "pm-agent-core",
        "agent_framework": "google-adk",
        "identity_type": types.IdentityType.AGENT_IDENTITY,      # immutable
        "agent_gateway_config": tf["agent_gateway_config"],      # immutable (executor: egress block only)
        "psc_interface_config": tf["psc_interface_config"],
        "encryption_spec": tf["encryption_spec"],
        "min_instances": 1, "max_instances": 20,
        "resource_limits": {"cpu": "4", "memory": "8Gi"},
        "env_vars": {"PM_AGENT_ENV": "dev", "GOOGLE_GENAI_USE_VERTEXAI": "true",
                     "GOOGLE_GENAI_USE_ENTERPRISE": "true"},
    },
)
print(remote.api_resource.name)   # .../reasoningEngines/<ENGINE_ID>  -> feed back to Terraform (step 4)
```

**CLI alternative** (command keeps the legacy name):

```bash
adk deploy agent_engine --project chayan-genai-bb --region us-central1 \
  --display_name pm-agent-core --adk_app <app module> --otel_to_cloud <agent dir>
```

Whether the CLI exposes `identity_type` / `agent_gateway_config` flags is **UNCONFIRMED** — use the SDK path for
gateway-bound engines (both settings are immutable, so a CLI-created engine without them must be recreated).

**Terraform alternative (confirmed):** `google_vertex_ai_reasoning_engine` supports `spec.identity_type =
"AGENT_IDENTITY"`, `spec.deployment_spec.agent_gateway_config.{agent_to_anywhere_config,client_to_agent_config}.agent_gateway`
(hashicorp/google ≥ 8.1.0), `psc_interface_config`, `encryption_spec`, min/max instances and `container_spec.image_uri`.
Not wired here because docs/12 keeps Agent Runtime in the CI SDK step; adopt it if image-based deploys are preferred.

## 3. Before binding to an ENFORCING gateway

* Binding routes **all** agent egress (incl. Sessions / Memory Bank / Gemini calls) through the gateway. Agent
  invocations fail with **HTTP 498** unless the essential Google API hosts are registered and allowed — Terraform
  registers them as Agent Registry endpoints (`essential_hosts` in `modules/platform`) and grants all three agent
  identities. Keep `access_policy_dry_run = true` until the soak is clean.
* **BYOC images:** container-image deployments must bake the egress gateway root CA into the image trust store
  (`terraform output egress_gateway_root_certificates`); source-based deployments get it injected automatically.
* Engines created before 2026-04-29 cannot bind to an Agent Gateway (recreate).

## 4. Feed engine ids back to Terraform (phase 3 of infra/README.md)

Agent Runtime may assign numeric resource ids. After the three deploys:

```hcl
# infra/terraform/envs/<env>/terraform.tfvars
agent_runtime_deployed = true
reasoning_engine_ids = { pm_agent_core = "<id>", signal_ingestion = "<id>", commit_executor = "<id>" }
```

`terraform apply` then registers `commit-executor` as an Agent Registry endpoint, binds the IAP egress policies
(still DRY_RUN), grants `AID-CORE` sole invoke rights on commit-executor, and grants the reasoning-engine service agent
its Model Armor / CMEK roles.
