"""HITL Approval & Audit Ledger (docs/07): catalog, tokens, ledger, identity, approvals, executor, resume.

Import from the sub-modules (``pm_agent.hitl.ledger``, ``pm_agent.hitl.approvals``, …). This package deliberately
re-exports nothing: an eager ``__init__`` would load token minting, the write guard and the commit executor into every
process that merely needs ``IdentityError`` or the ``Ledger`` — including LLM-side workflows (ADR-003, ADR-011;
enforced by ``tests/architecture``).
"""
