# GKE data plane manifests (Kustomize)

| Workload | Kind | KSA → GSA (Workload Identity Federation for GKE) | Ingress |
|---|---|---|---|
| `signal-event-gateway` | Deployment + Service + HPA (2–10) + PDB + Ingress/BackendConfig (Cloud Armor) + ManagedCertificate; webhook secrets via ExternalSecret | `signal-event-gateway` → `sa-gke-signal-event-gateway` | public HTTPS LB |
| `correlation-engine` (`mcp-correlation`) | Deployment (2) + Service + PDB + Gateway `gke-l7-rilb` + HTTPRoute + HealthCheckPolicy | `correlation-engine` → `sa-gke-correlation-engine` | internal L7 LB (TLS), reached via Agent Gateway |
| `correlation-recluster` | CronJob nightly 02:00 UTC | `correlation-engine` | none |
| `dls-catalog-sync` | CronJob hourly | `dls-catalog-sync` → `sa-gke-dls-catalog-sync` | none |
| `ledger-anchor` | CronJob every 5 min | `ledger-anchor` → `sa-mcp-ledger` (only the ledger identity signs) | none |

```bash
kustomize build deploy/k8s/overlays/dev | kubectl apply -f -      # or Config Sync (docs/12 §3)
```

* Images: one per component from `deploy/Dockerfile` (`--build-arg COMPONENT=<name>`); CI pins tags with
  `kustomize edit set image pm-agent/<component>=<repo>/<component>:$SHORT_SHA`.
* Overlay values come from `terraform output k8s_overlay_values`; stage/prod overlays keep `PLACEHOLDER:` tokens
  until Q-10 is answered.

## Webhook secrets (signal-event-gateway)

`base/external-secrets.yaml` projects seven Secret Manager secrets into the Kubernetes Secret
`signal-event-gateway-webhooks`, consumed with `envFrom`:

| Env var | Secret Manager secret | Bootstrap placeholder |
|---|---|---|
| `PM_AGENT_WEBHOOK_ADO_BASIC_USERNAME` | `webhook-ado-basic-username` | `PLACEHOLDER:Q-04:ado_webhook_basic_username` |
| `PM_AGENT_WEBHOOK_ADO_BASIC_PASSWORD` | `webhook-ado-basic-password` | `PLACEHOLDER:Q-04:ado_webhook_basic_password` |
| `PM_AGENT_WEBHOOK_ADO_SHARED_SECRET` | `webhook-ado-shared-secret` | `PLACEHOLDER:Q-04:ado_webhook_shared_secret` |
| `PM_AGENT_WEBHOOK_SERVICENOW_HMAC_SECRET` | `webhook-servicenow-hmac-secret` | `PLACEHOLDER:Q-05:servicenow_webhook_hmac_secret` |
| `PM_AGENT_WEBHOOK_PAGERDUTY_SIGNING_SECRETS` | `webhook-pagerduty-signing-secrets` (comma-separated) | `PLACEHOLDER:Q-07:pagerduty_webhook_signing_secret` |
| `PM_AGENT_WEBHOOK_MSGRAPH_CLIENT_STATE` | `webhook-msgraph-client-state` | `PLACEHOLDER:Q-13:msgraph_subscription_client_state` |
| `PM_AGENT_WEBHOOK_FIGMA_PASSCODE` | `webhook-figma-passcode` | `PLACEHOLDER:Q-02:figma_webhook_passcode` |

* **Prerequisite:** External Secrets Operator (`external-secrets.io/v1` CRDs), e.g.
  `helm install external-secrets external-secrets/external-secrets -n external-secrets --create-namespace`
  (or via Config Sync). ESO was chosen over the Secret Manager CSI add-on because the code reads **env vars**, while
  CSI mounts files.
* **Least privilege:** the `SecretStore` authenticates as the `signal-event-gateway` KSA (`serviceAccountRef`), so
  only `sa-gke-signal-event-gateway` holds `secretAccessor` on these secrets (Terraform `local.webhook_secrets`); the
  ESO controller itself needs no Google IAM. The synced Secret lives in etcd, encrypted with the `cmek-gke` key.
* **Bootstrap / rotation:** Terraform writes the placeholder tokens as version 1 (public values; the app fails closed
  on them). Add real values out-of-band (`gcloud secrets versions add <secret> --data-file=-`), then **disable** (do
  not destroy) version 1. ESO refreshes within `refreshInterval` (1h). Env vars are read at start-up, so finish with
  `kubectl rollout restart deploy/signal-event-gateway`. PagerDuty accepts several comma-separated secrets, so old and
  new can overlap.

## Optional: synthetic golden fixtures in dev images (NOT in the default image)

To let a dev image serve the synthetic connector fixtures, build a separate dev-only variant that additionally copies
`tests/fixtures/connectors` (e.g. to `/app/fixtures/connectors`) and sets `PM_AGENT_FIXTURE_ROOT` to that path; the
allow-list in `deploy/Dockerfile.dockerignore` must re-include `tests/fixtures/connectors/**` for that build only.
Never build or promote such an image for stage/prod.

## Notes

* `mcp-correlation` TLS: create a Certificate Manager certificate `mcp-correlation-internal` (private CA) for
  `mcp-correlation.pm-agent.internal`, and give the Agent Gateway connectivity template a trust config for that CA
  (`egress_trust_config` in `modules/agent_gateway`). Not provisioned yet.
* `correlation-recluster` sets `PM_AGENT_JOB=recluster`; the `pm_agent.serve` dispatcher must support that job
  mode (or a dedicated component name).
* Pods run non-root (uid 10001), read-only root FS, all capabilities dropped, `RuntimeDefault` seccomp; the
  namespace enforces Pod Security `restricted`.
