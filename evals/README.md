# Evaluations (docs/13 §2–§3)

Evals measure model-dependent behaviour with numeric gates. Two of them are **hard CI gates** today:

| Gate | Where it runs | Rule |
|---|---|---|
| E3 Groundedness | `pytest tests/component -k groundedness` (CI gate 5) and `run_offline.py` | uncited claims in proposed/approved outputs = **0** |
| E6 Safety / HITL | `pytest tests -k 'safety or token'` (CI gate 6) | unauthorized writes and accepted forged/replayed/mismatched tokens = **0** |

## Layout

```text
evals/
  suites.yaml       E1–E8 definitions and thresholds (data, read by the runners)
  goldens/          S1–S7 scenario stubs (expected outcomes); sanitized corpus fixtures await Q-11
  fixtures/         placeholder — sanitized, pseudonymized corpus derivatives (never raw SLB data)
  adk/              placeholder — ADK *.evalset.json / test_config.json for trajectory (E8) and response quality (E4)
  run_offline.py    offline baseline over the synthetic golden connectors (no credentials, no network)
```

## Running

```bash
make evals            # = .venv/bin/python evals/run_offline.py
```

The offline baseline ingests the synthetic golden fixture set (`tests/fixtures/connectors`, built by
`pm_agent.connectors.fixtures.golden_connectors`) through the real W1/W2 workflows with the deterministic extractive
model, then scores E1 (problem formation), E3 (groundedness), E7 (redaction) and prints a JSON report. It exits
non-zero when a hard gate fails.

Live evals (real Gemini on Agent Platform, nightly in `dev`) reuse the same suites with
`SettingsModelProvider`; they need `gcloud auth application-default login` and the dev project (Q-10).

> [!IMPORTANT]
> The golden scenarios S1–S7 in `docs/13` are derived from the SLB corpus. Their sanitized fixtures are **not** in
> this repository until SLB approves deriving them (open question Q-11). The stubs in `goldens/` describe the
> expected outcomes generically and are marked `status: placeholder`.
