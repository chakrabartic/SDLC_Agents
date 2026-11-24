# GKE data plane manifests (Kustomize)

| Workload | Kind | KSA → GSA (Workload Identity Federation for GKE) | Ingress |
|---|---|---|---|
| `signal-event-gateway` | Deployment + Service + HPA (2–10) + PDB + Ingress/BackendConfig (Cloud Armor) + ManagedCertificate | `signal-event-gateway` → `sa-gke-signal-event-gateway` | public HTTPS LB |
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
* `mcp-correlation` TLS: create a Certificate Manager certificate `mcp-correlation-internal` (private CA) for
  `mcp-correlation.pm-agent.internal`, and give the Agent Gateway connectivity template a trust config for that CA
  (`egress_trust_config` in `modules/agent_gateway`). Not provisioned yet.
* `correlation-recluster` sets `PM_AGENT_JOB=recluster`; the `pm_agent.serve` dispatcher must support that job
  mode (or a dedicated component name).
* Pods run non-root (uid 10001), read-only root FS, all capabilities dropped, `RuntimeDefault` seccomp; the
  namespace enforces Pod Security `restricted`.
