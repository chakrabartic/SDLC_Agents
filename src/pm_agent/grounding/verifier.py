"""The Verifier — evidence & policy gate (docs/06 §3).

* ``check_claims`` turns LLM ``ClaimDraft``s into resolved ``Claim``s (V1–V5); anything unverifiable is **blocked**,
  never repaired. The model supplies only ``evidence_id`` + an exact quote; spans, hashes and permalinks are filled
  in here by code.
* ``verify_subject`` re-checks every claim of a subject from scratch (stored ``verification`` statuses are never
  trusted), adds V0 and V6–V10, and returns an ES256-signed ``VerificationReport``. The HITL ledger only proposes
  actions / mints approval tokens against a PASS report whose hashes match the subject and the exact target payload.

V0–V4 and V6–V10 are deterministic code. V5 is the bounded entailment judge and fails closed.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Collection, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from pm_agent.crypto import Signer, b64url_decode, b64url_encode, verify_es256
from pm_agent.grounding.atoms import ATOMS_VERSION, AtomExtractor
from pm_agent.grounding.evidence_store import EvidenceStore
from pm_agent.grounding.judge import EntailmentJudge, JudgeUnavailable, JudgeVerdict
from pm_agent.grounding.policy import ACTION_SUBJECT_TYPES, POLICY_VERSION, PolicyEngine
from pm_agent.grounding.redaction import Redactor
from pm_agent.schemas import (
    CITATION_REQUIRED,
    CheckResult,
    CheckStatus,
    Citation,
    Claim,
    ClaimDraft,
    ClaimKind,
    ClaimVerification,
    DuplicateCandidate,
    DuplicateDecision,
    EvidenceChunk,
    FieldStatus,
    InvestigationPack,
    KnowledgeArticleDraft,
    ProductProblem,
    PrototypeSpec,
    RequirementDraft,
    Signal,
    SignalStatus,
    Span,
    SubjectRef,
    TraceLink,
    VerificationOutcome,
    VerificationReport,
    VerificationStatus,
    canonical_json,
    compute_provenance_hash,
    content_sha256,
    new_id,
    sha256_hex,
    string_leaves,
)
from pm_agent.settings import Settings

SUBJECT_TYPES: dict[str, type[BaseModel]] = {
    "requirement_draft": RequirementDraft,
    "knowledge_article_draft": KnowledgeArticleDraft,
    "prototype_spec": PrototypeSpec,
    "product_problem": ProductProblem,
    "trace_link": TraceLink,
    "investigation_pack": InvestigationPack,
}
# Workflow bookkeeping that changes *after* verification; excluded so a report keeps matching its subject.
VOLATILE_FIELDS = frozenset({"verification_report_id", "approval_state"})
CLAIM_CHECKS = ("V1", "V2", "V3", "V4", "V5")
# V5 judges statements read from evidence; aggregates are recomputed by code (V6) instead.
JUDGED_KINDS = frozenset({ClaimKind.FACT, ClaimKind.CLASSIFICATION, ClaimKind.PM_JUDGMENT})
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_ID_FIELDS = ("spec_id", "article_id", "requirement_id", "pack_id", "link_id", "problem_id")  # most specific first
_INFERRED_PATH = re.compile(r"^fields\.(?P<name>[^.\[]+)\.claims\[\d+\]$")
_JUDGE_CACHE_MAX = 4096

AggregateChecker = Callable[[Claim], bool]
"""V6: recompute an aggregate claim's value from linked evidence; ``True`` only on an exact match."""


class NearDuplicateChecker(Protocol):
    """V10: existing ADO work items / KB articles / problems similar to *text* (docs/06 §6.3)."""

    def find(self, text: str) -> list[DuplicateCandidate]: ...


@dataclass(frozen=True)
class BlockedClaim:
    """A claim that did not survive verification — the first failing check stops the pipeline."""

    claim_id: str
    text: str
    failed_checks: list[str]
    reason: str


@dataclass
class ClaimCheckResult:
    passed: list[Claim] = field(default_factory=list)
    blocked: list[BlockedClaim] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return not self.blocked


@dataclass(frozen=True)
class _Cite:
    """A citation to check: LLM-supplied (``resolved is None``) or previously resolved (re-verification)."""

    evidence_id: str
    quote: str
    resolved: Citation | None = None


class _CheckFailed(Exception):
    def __init__(self, check_id: str, reason: str) -> None:
        super().__init__(f"{check_id}: {reason}")
        self.check_id = check_id
        self.reason = reason


def subject_digest(subject: BaseModel) -> str:
    """The hash reports and approvals bind to: canonical JSON of the subject minus bookkeeping fields."""
    data = subject.model_dump(mode="json")
    for name in VOLATILE_FIELDS:
        data.pop(name, None)
    return content_sha256(data)


def subject_id(subject: BaseModel) -> str:
    for name in _ID_FIELDS:
        value = getattr(subject, name, None)
        if isinstance(value, str):
            return value
    return "unknown"


def iter_claims(obj: Any, path: str = "") -> Iterator[tuple[str, Claim]]:
    """Every ``Claim`` inside a (nested) subject, with its dotted path (e.g. ``fields.summary.claims[0]``)."""
    if isinstance(obj, Claim):
        yield path, obj
    elif isinstance(obj, BaseModel):
        for name in type(obj).model_fields:
            yield from iter_claims(getattr(obj, name), f"{path}.{name}" if path else name)
    elif isinstance(obj, dict):
        for key, value in obj.items():
            yield from iter_claims(value, f"{path}.{key}" if path else str(key))
    elif isinstance(obj, (list, tuple)):
        for index, value in enumerate(obj):
            yield from iter_claims(value, f"{path}[{index}]")


def report_signing_input(report: VerificationReport) -> bytes:
    return canonical_json(report.model_dump(mode="json", exclude={"signature"}))


def verify_report_signature(report: VerificationReport, public_key_pem: bytes) -> bool:
    """True iff the report carries a valid ES256 signature by the holder of *public_key_pem*."""
    if not report.signature:
        return False
    try:
        signature = b64url_decode(report.signature)
    except ValueError:
        return False
    return verify_es256(public_key_pem, report_signing_input(report), signature)


class Verifier:  # pylint: disable=too-many-instance-attributes
    """Mechanical evidence & policy gate. Holds no write credentials; its only output is a signed report."""

    def __init__(self, *, store: EvidenceStore, judge: EntailmentJudge, settings: Settings, signer: Signer,
                 second_judge: EntailmentJudge | None = None, redactor: Redactor | None = None,
                 policy: PolicyEngine | None = None, aggregate_checker: AggregateChecker | None = None,
                 near_duplicate_checker: NearDuplicateChecker | None = None,
                 dls_component_keys: Collection[str] | None = None, atoms: AtomExtractor | None = None) -> None:
        self._store = store
        self._judges: dict[str, EntailmentJudge | None] = {"primary": judge, "secondary": second_judge}
        self._settings = settings
        self._signer = signer
        self._redactor = redactor
        self._policy = policy or PolicyEngine(settings)
        self._aggregate_checker = aggregate_checker
        self._near_dups = near_duplicate_checker
        self._dls_keys = frozenset(dls_component_keys) if dls_component_keys is not None else None
        self._atoms = atoms or AtomExtractor.from_settings(settings)
        self._judge_cache: dict[tuple[str, str, tuple[str, ...]], JudgeVerdict] = {}

    @property
    def signer(self) -> Signer:
        return self._signer

    @property
    def atoms(self) -> AtomExtractor:
        return self._atoms

    # ------------------------------------------------------------------------------------------------ claims
    def check_claims(self, drafts: Sequence[ClaimDraft]) -> ClaimCheckResult:
        """V1–V5 for LLM output. Passed claims carry resolved citations; the rest are blocked (never fixed)."""
        result = ClaimCheckResult()
        for draft in drafts:
            cites = [_Cite(c.evidence_id, c.quote) for c in draft.citations]
            try:
                citations = self._verify_claim(draft.text, draft.kind, cites, dual_judge=False)
                claim = Claim(text=draft.text, kind=draft.kind, citations=citations,
                              atoms=self._atoms.extract(draft.text),
                              verification=ClaimVerification(status=VerificationStatus.PASS))
                if claim.kind is ClaimKind.AGGREGATE:
                    self._recompute(claim)
            except _CheckFailed as failure:
                result.blocked.append(BlockedClaim(claim_id=new_id("clm"), text=draft.text,
                                                   failed_checks=[failure.check_id], reason=failure.reason))
                continue
            result.passed.append(claim)
        return result

    def _recompute(self, claim: Claim) -> None:
        """V6 for one aggregate claim (also run subject-wide in ``verify_subject``)."""
        if self._aggregate_checker is None:
            raise _CheckFailed("V6", "aggregate claims require a recomputation checker")
        if not self._aggregate_checker(claim):
            raise _CheckFailed("V6", "aggregate value does not match recomputation from the cited evidence")

    def _verify_claim(self, text: str, kind: ClaimKind, cites: Sequence[_Cite], *, dual_judge: bool) -> list[Citation]:
        """V1 → V5 in order; the first failure raises ``_CheckFailed``. Returns the resolved citations."""
        atoms = self._atoms.extract(text)
        if kind is ClaimKind.PROCEDURAL:
            if atoms:
                raise _CheckFailed("V1", f"procedural text may not carry checkable atoms {atoms}")
            return []
        if not cites:
            if kind in CITATION_REQUIRED:
                raise _CheckFailed("V1", f"{kind.value} claim has no citation")
            if atoms:
                raise _CheckFailed("V1", f"uncited {kind.value} claim carries atoms {atoms}")
            return []
        located = [self._resolvable(cite) for cite in cites]  # V2 for every citation first
        citations = [self._span(cite, chunk, signal) for cite, (chunk, signal) in zip(cites, located)]  # V3
        quotes = [c.quote for c in citations]
        missing = self._atoms.unsupported(text, quotes)
        if kind is ClaimKind.AGGREGATE:  # computed counts are not quotable — V6 recomputes them exactly
            missing = [atom for atom in missing if not _NUMBER.fullmatch(atom)]
        if missing:
            raise _CheckFailed("V4", f"atoms not found in any cited quote: {missing}")
        if kind in JUDGED_KINDS:
            self._entailment(text, quotes, dual_judge=dual_judge)
        return citations

    def _resolvable(self, cite: _Cite) -> tuple[EvidenceChunk, Signal]:
        """V2: evidence exists, is active, untampered, and (on re-verification) still matches its provenance."""
        chunk = self._store.get_chunk(cite.evidence_id)
        if chunk is None:
            raise _CheckFailed("V2", f"unknown evidence id {cite.evidence_id!r}")
        signal = self._store.get_signal(chunk.signal_id)
        if signal is None:
            raise _CheckFailed("V2", f"evidence {cite.evidence_id!r} has no signal")
        if signal.status is not SignalStatus.ACTIVE:
            raise _CheckFailed("V2", f"signal {signal.signal_id} is {signal.status.value}")
        expected = compute_provenance_hash(signal.source_system.value, signal.source_record_id,
                                           signal.source_version, signal.raw_text)
        if signal.provenance_hash != expected:
            raise _CheckFailed("V2", f"signal {signal.signal_id} provenance hash does not recompute")
        if signal.raw_text[chunk.char_start:chunk.char_end] != chunk.text or chunk.text_sha256 != sha256_hex(
                chunk.text):
            raise _CheckFailed("V2", f"evidence {cite.evidence_id!r} does not match its signal text")
        resolved = cite.resolved
        if resolved is not None and (resolved.signal_id != signal.signal_id or resolved.permalink != signal.permalink
                                     or resolved.source_system is not signal.source_system
                                     or resolved.source_record_id != signal.source_record_id):
            raise _CheckFailed("V2", f"citation provenance for {cite.evidence_id!r} does not match the signal")
        return chunk, signal

    def _span(self, cite: _Cite, chunk: EvidenceChunk, signal: Signal) -> Citation:
        """V3: the quote is an exact substring of the chunk; the span is computed here, never by a model."""
        max_chars = self._settings.verifier.max_quote_chars
        if cite.resolved is not None:
            span, quote = cite.resolved.span, cite.resolved.quote
            if chunk.text[span.start:span.end] != quote or cite.resolved.quote_sha256 != sha256_hex(quote):
                raise _CheckFailed("V3", f"cited span no longer matches evidence {cite.evidence_id!r}")
        else:
            located = _locate(unicodedata.normalize("NFC", cite.quote), chunk.text)
            if located is None:
                raise _CheckFailed("V3", f"quote not found verbatim in evidence {cite.evidence_id!r}")
            span, quote = located
        if len(quote) > max_chars:
            raise _CheckFailed("V3", f"quote longer than {max_chars} characters")
        return Citation(evidence_id=chunk.evidence_id, signal_id=signal.signal_id,
                        source_system=signal.source_system, source_record_id=signal.source_record_id,
                        permalink=signal.permalink, span=span, quote=quote)

    def _entailment(self, text: str, quotes: list[str], *, dual_judge: bool) -> None:
        """V5: bounded judge(s) must return ``entailed`` with a valid quote index; any error fails closed."""
        if not self._settings.verifier.judge_required:
            return
        slots = ("primary", "secondary") if dual_judge else ("primary",)
        for slot in slots:
            judge = self._judges[slot]
            if judge is None:
                raise _CheckFailed("V5", "dual entailment judging is required for this action but no second judge "
                                         "is configured")
            key = (slot, text, tuple(quotes))
            verdict = self._judge_cache.get(key)
            if verdict is None:
                try:
                    verdict = judge.judge(text, quotes)
                except JudgeUnavailable as exc:
                    raise _CheckFailed("V5", f"verification unavailable ({exc}) — not shown") from exc
                if len(self._judge_cache) >= _JUDGE_CACHE_MAX:
                    self._judge_cache.pop(next(iter(self._judge_cache)))
                self._judge_cache[key] = verdict
            index = verdict.supporting_quote_index
            if not verdict.entailed or index is None or not 0 <= index < len(quotes):
                raise _CheckFailed("V5", f"{slot} judge verdict: {verdict.verdict}")

    # ----------------------------------------------------------------------------------------------- subject
    def verify_subject(self, subject: BaseModel, *, subject_type: str, commit_bound: bool = False,
                       target_action: str | None = None,
                       target_payload: dict[str, Any] | None = None) -> VerificationReport:
        """Full V0–V10 over a draft/spec/problem. Returns a signed report (``overall`` PASS only if all pass)."""
        claims = list(iter_claims(subject))
        dual = commit_bound and target_action in self._settings.verifier.dual_judge_action_types
        checks = [self._v0_schema(subject, subject_type)]
        claim_checks, blocked_ids = self._claim_checks(claims, dual)
        checks += claim_checks
        checks += [
            self._v6_aggregates(claims),
            self._v7_inference(subject, claims, commit_bound),
            self._v8_redaction(claims, target_payload, commit_bound),
            self._v9_policy(subject, subject_type, commit_bound, target_action, target_payload),
            self._v10_near_duplicates(subject, claims),
        ]
        if any(c.status is CheckStatus.FAIL for c in checks):
            overall = VerificationOutcome.FAIL
        elif any(c.status is CheckStatus.NEEDS_PM_DECISION for c in checks):
            overall = VerificationOutcome.NEEDS_PM_DECISION
        else:
            overall = VerificationOutcome.PASS
        digest = subject_digest(subject)
        version = getattr(subject, "version", None)
        report = VerificationReport(
            subject_ref=SubjectRef(type=subject_type, id=subject_id(subject),
                                   version=version if isinstance(version, int) else None, sha256=digest),
            subject_sha256=digest,
            verifier_version=self._settings.verifier.version,
            policy_versions={"config": self._settings.config_version, "atoms": ATOMS_VERSION,
                             "policy": POLICY_VERSION},
            checks=checks, overall=overall, blocked_claim_ids=blocked_ids, commit_bound=commit_bound,
            target_action=target_action,
            target_payload_sha256=content_sha256(target_payload) if target_payload is not None else None,
            signing_key_id=self._signer.key_id)
        return self.sign(report)

    def sign(self, report: VerificationReport) -> VerificationReport:
        unsigned = report.model_copy(update={"signature": None, "signing_key_id": self._signer.key_id})
        signature = b64url_encode(self._signer.sign(report_signing_input(unsigned)))
        return unsigned.model_copy(update={"signature": signature})

    def _claim_checks(self, claims: list[tuple[str, Claim]], dual: bool) -> tuple[list[CheckResult], list[str]]:
        failures: dict[str, list[tuple[str, str]]] = {check: [] for check in CLAIM_CHECKS}
        for _, claim in claims:
            cites = [_Cite(c.evidence_id, c.quote, resolved=c) for c in claim.citations]
            try:
                self._verify_claim(claim.text, claim.kind, cites, dual_judge=dual)
            except _CheckFailed as failure:
                failures[failure.check_id].append((claim.claim_id, failure.reason))
        results = []
        for check_id in CLAIM_CHECKS:
            failed = failures[check_id]
            results.append(CheckResult(
                check_id=check_id, status=CheckStatus.FAIL if failed else CheckStatus.PASS,
                details="; ".join(f"{cid}: {reason}" for cid, reason in failed) or f"{len(claims)} claims checked",
                claim_ids=[cid for cid, _ in failed]))
        blocked = [cid for check_id in CLAIM_CHECKS for cid, _ in failures[check_id]]
        return results, blocked

    @staticmethod
    def _v0_schema(subject: BaseModel, subject_type: str) -> CheckResult:
        expected = SUBJECT_TYPES.get(subject_type)
        if expected is None:
            return CheckResult(check_id="V0", status=CheckStatus.FAIL, details=f"unknown subject type {subject_type!r}")
        if not isinstance(subject, expected):
            return CheckResult(check_id="V0", status=CheckStatus.FAIL,
                               details=f"subject is {type(subject).__name__}, expected {expected.__name__}")
        try:
            expected.model_validate_json(subject.model_dump_json())
        except ValidationError as exc:
            return CheckResult(check_id="V0", status=CheckStatus.FAIL,
                               details=f"schema validation failed: {exc.error_count()} error(s)")
        return CheckResult(check_id="V0", status=CheckStatus.PASS, details=f"{expected.__name__} valid")

    def _v6_aggregates(self, claims: list[tuple[str, Claim]]) -> CheckResult:
        aggregates = [c for _, c in claims if c.kind is ClaimKind.AGGREGATE]
        if not aggregates:
            return CheckResult(check_id="V6", status=CheckStatus.SKIPPED, details="no aggregate claims")
        if self._aggregate_checker is None:
            return CheckResult(check_id="V6", status=CheckStatus.FAIL, claim_ids=[c.claim_id for c in aggregates],
                               details="aggregate claims present but no recomputation checker is configured")
        failed = [c.claim_id for c in aggregates if not self._aggregate_checker(c)]
        return CheckResult(check_id="V6", status=CheckStatus.FAIL if failed else CheckStatus.PASS, claim_ids=failed,
                           details=f"{len(aggregates) - len(failed)}/{len(aggregates)} aggregates recomputed exactly")

    @staticmethod
    def _v7_inference(subject: BaseModel, claims: list[tuple[str, Claim]], commit_bound: bool) -> CheckResult:
        fields = subject.fields if isinstance(subject, RequirementDraft) else {}
        inferences = [(path, c) for path, c in claims if c.kind is ClaimKind.INFERENCE]
        misplaced = []
        for path, claim in inferences:
            match = _INFERRED_PATH.match(path)
            name = match.group("name") if match else None
            if name is None or name not in fields or fields[name].status is not FieldStatus.INFERRED:
                misplaced.append(claim.claim_id)
        if misplaced:
            return CheckResult(check_id="V7", status=CheckStatus.FAIL, claim_ids=misplaced,
                               details="inference presented outside an inferred field")
        if commit_bound and inferences:
            return CheckResult(check_id="V7", status=CheckStatus.FAIL, claim_ids=[c.claim_id for _, c in inferences],
                               details="commit-bound subject still contains unresolved inference (PM must confirm)")
        return CheckResult(check_id="V7", status=CheckStatus.PASS,
                           details=f"{len(inferences)} inference claim(s), all in inferred fields")

    def _v8_redaction(self, claims: list[tuple[str, Claim]], target_payload: dict[str, Any] | None,
                      commit_bound: bool) -> CheckResult:
        if self._redactor is None:
            status = CheckStatus.FAIL if commit_bound else CheckStatus.SKIPPED
            return CheckResult(check_id="V8", status=status, details="no redaction inspector configured")
        if target_payload is not None:
            text = "\n".join(string_leaves(target_payload))
        else:
            text = "\n".join([c.text for _, c in claims] + [q.quote for _, c in claims for q in c.citations])
        findings = self._redactor.inspect(text)
        if findings:
            summary = ", ".join(f"{name}×{count}" for name, count in sorted(findings.items()))
            return CheckResult(check_id="V8", status=CheckStatus.FAIL, details=f"sensitive data found: {summary}")
        return CheckResult(check_id="V8", status=CheckStatus.PASS, details="no secrets or raw PII")

    def _v9_policy(self, subject: BaseModel, subject_type: str, commit_bound: bool, target_action: str | None,
                   target_payload: dict[str, Any] | None) -> CheckResult:
        reasons: list[str] = []
        if isinstance(subject, PrototypeSpec):
            reasons += self._prototype_rules(subject, commit_bound)
        if target_action is None:
            if commit_bound:
                reasons.append("commit-bound verification requires a target action")
        else:
            allowed_subjects = ACTION_SUBJECT_TYPES.get(target_action, frozenset())
            if subject_type not in allowed_subjects:
                reasons.append(f"{target_action} cannot be backed by a {subject_type}")
            decision = self._policy.evaluate(target_action, target_payload or {}, {})
            reasons += [f"{rule}: {reason}" for rule, reason in zip(decision.rule_ids, decision.reasons)]
        if reasons:
            return CheckResult(check_id="V9", status=CheckStatus.FAIL, details="; ".join(reasons))
        if target_action is None and not isinstance(subject, PrototypeSpec):
            return CheckResult(check_id="V9", status=CheckStatus.SKIPPED, details="no target action")
        return CheckResult(check_id="V9", status=CheckStatus.PASS, details="policies satisfied")

    def _prototype_rules(self, spec: PrototypeSpec, commit_bound: bool) -> list[str]:
        reasons = []
        minimum = self._settings.policies.min_dls_reuse_ratio
        if spec.reuse_ratio < minimum:
            reasons.append(f"P-07: DLS reuse ratio {spec.reuse_ratio:.2f} below {minimum}")
        reasons += [f"P-07: new pattern {p.name!r} has no justification (FR-B3)" for p in spec.new_patterns
                    if not p.justification]
        if self._dls_keys is None:
            if commit_bound:
                reasons.append("P-07: no pinned DLS catalog snapshot to validate component keys (Q-03)")
        else:
            unknown = sorted(set(spec.component_keys()) - self._dls_keys)
            if unknown:
                reasons.append(f"P-07: component keys not in the pinned DLS catalog: {unknown}")
        return reasons

    def _v10_near_duplicates(self, subject: BaseModel, claims: list[tuple[str, Claim]]) -> CheckResult:
        known: list[DuplicateCandidate] = list(getattr(subject, "near_duplicates", []) or [])
        resolved = {d.ref for d in known if d.decision is not DuplicateDecision.PENDING}
        candidates = list(known)
        if self._near_dups is not None:
            candidates += self._near_dups.find(" ".join(c.text for _, c in claims))
        elif not known:
            return CheckResult(check_id="V10", status=CheckStatus.SKIPPED, details="no near-duplicate source")
        threshold = self._settings.verifier.near_duplicate_threshold
        pending = sorted({c.ref for c in candidates if c.similarity >= threshold and c.ref not in resolved
                          and c.decision is DuplicateDecision.PENDING})
        if pending:
            return CheckResult(check_id="V10", status=CheckStatus.NEEDS_PM_DECISION,
                               details=f"possible duplicates need a PM decision (link/merge/proceed): {pending}")
        return CheckResult(check_id="V10", status=CheckStatus.PASS, details="no unresolved near-duplicates")


def _locate(quote: str, text: str) -> tuple[Span, str] | None:
    """Exact substring first; then whitespace-insensitive (the stored quote is always the source's own text)."""
    start = text.find(quote)
    if start >= 0:
        return Span(start=start, end=start + len(quote)), quote
    tokens = quote.split()
    if not tokens:
        return None
    match = re.search(r"\s+".join(re.escape(t) for t in tokens), text)
    if match is None:
        return None
    return Span(start=match.start(), end=match.end()), match.group(0)
