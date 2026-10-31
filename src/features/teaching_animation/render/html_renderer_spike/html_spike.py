"""W0-A spike: high-level experiments that the ADR and gate tests drive.

``HtmlRendererSpike`` orchestrates the candidate backend through the exact
experiments the plan requires for S-00:

* seek determinism — same frame repeated N times is byte-identical;
* out-of-order seek — requesting 0/mid/last/keyframes in shuffled order yields
  the same pixels as in-order;
* no-network / no-file security — the attack corpus is denied by the runtime
  policy (this is checkable without a real browser via the URL policy + CSP);
* cancel/crash/release — a cancel token aborts before capture; teardown leaves
  no page/process residue.

The orchestrator is backend-agnostic: pass any ``FrameCaptureBackend``.  In
this environment the Qt backend self-reports unavailable, so the real-browser
experiments report a structured BLANK/UNSUPPORTED gate failure rather than
pretending to pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import random
from typing import Mapping

from .capture_backend import (
    CancelToken,
    CaptureFailure,
    CaptureFailureCode,
    CaptureSurface,
    CapturedFrame,
    FrameCaptureBackend,
)
from .determinism import (
    DeterminismReport,
    summarize_out_of_order,
    summarize_repetition,
)
from .fixtures import BENIGN_FIXTURES, HtmlFixture, ATTACK_FIXTURES, AttackFixture
from .security import (
    CspPolicy,
    DISABLED_FEATURES,
    ResourceBudget,
    ToposAssetRegistry,
    UrlPolicy,
    UrlRequest,
)


@dataclass(frozen=True)
class SpikeConfig:
    """Hermetic inputs for one spike run."""

    surface: CaptureSurface
    fps_num: int = 24
    fps_den: int = 1
    repetition_count: int = 5
    seek_frames: tuple[int, ...] = (0, 1, 12, 23)
    timeout_ms: int = 10000
    budget: ResourceBudget = field(default_factory=ResourceBudget)


@dataclass(frozen=True)
class SecurityVerdict:
    """Per-attack verdict: was the abuse vector denied?"""

    fixture_id: str
    vector: str
    denied: bool
    reason: str


@dataclass(frozen=True)
class GateReport:
    """Aggregate result of the W0-A gate for the ADR."""

    backend_id: str
    backend_available: bool
    determinism: DeterminismReport | None
    out_of_order: DeterminismReport | None
    security: tuple[SecurityVerdict, ...]
    cancel_honoured: bool | None
    notes: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        """The gate passes only when every experiment succeeds on a live backend."""
        if not self.backend_available:
            return False
        det_ok = self.determinism.same_frame_repeats_identical if self.determinism else False
        ooo_ok = self.out_of_order.out_of_order_identical if self.out_of_order else False
        sec_ok = all(v.denied for v in self.security)
        cancel_ok = self.cancel_honoured is True
        return det_ok and ooo_ok and sec_ok and cancel_ok


class HtmlRendererSpike:
    """Drives a capture backend through the W0-A experiments."""

    def __init__(self, backend: FrameCaptureBackend) -> None:
        self.backend = backend

    # -- determinism -------------------------------------------------------

    def probe_determinism(self, fixture: HtmlFixture, config: SpikeConfig) -> tuple[DeterminismReport, CapturedFrame | CaptureFailure]:
        captures: list[CapturedFrame] = []
        last: CapturedFrame | CaptureFailure = CaptureFailure(
            CaptureFailureCode.UNSUPPORTED, "no capture attempted", self.backend.backend_id
        )
        for _ in range(config.repetition_count):
            result = self._capture(fixture, frame=config.seek_frames[0], config=config)
            last = result
            if isinstance(result, CapturedFrame):
                captures.append(result)
            else:
                break
        report = summarize_repetition(tuple(captures)) if captures else DeterminismReport(False, False, (), last if isinstance(last, CaptureFailure) else None)
        return report, last

    def probe_out_of_order(self, fixture: HtmlFixture, config: SpikeConfig) -> tuple[DeterminismReport, dict[int, CapturedFrame] | CaptureFailure]:
        ordered: dict[int, CapturedFrame] = {}
        for frame in config.seek_frames:
            result = self._capture(fixture, frame=frame, config=config)
            if isinstance(result, CapturedFrame):
                ordered[frame] = result
            else:
                return DeterminismReport(False, False, (), None), result
        shuffled_frames = list(config.seek_frames)
        random.Random(0xC0FFEE).shuffle(shuffled_frames)
        shuffled: dict[int, CapturedFrame] = {}
        for frame in shuffled_frames:
            result = self._capture(fixture, frame=frame, config=config)
            if isinstance(result, CapturedFrame):
                shuffled[frame] = result
            else:
                return DeterminismReport(False, False, (), None), result
        return summarize_out_of_order(ordered, shuffled), ordered

    # -- security ----------------------------------------------------------

    def evaluate_security_corpus(self, registry: ToposAssetRegistry | None = None) -> tuple[SecurityVerdict, ...]:
        """Evaluate the URL policy + CSP against the attack corpus.

        This is checkable without a live backend: the runtime policy is pure
        data, so every attack fixture is mapped to the requests it would issue
        and each request is denied by the policy.  The corpus is evaluated
        against the benign fixtures' bound assets so that an unbound
        ``topos-asset://`` reference is correctly rejected.

        AUD-30203 fix in two halves:

        * a fixture that documents a **URL-policy** denial reason must be
          denied with exactly that reason — an incidental denial with a
          different reason is a regression signal, not coverage;
        * a fixture that documents a **capability** denial (script execution,
          popups, storage, ...) issues no interceptable URL at all; it is
          verified against the strict sandbox profile (CSP directives +
          disabled features) instead of scoring a fabricated placeholder
          request as "denied".
        """
        if registry is None:
            bound: set[str] = set()
            for fixture in BENIGN_FIXTURES:
                bound.update(fixture.bound_asset_ids)
            allowed_ids = frozenset(bound)
        else:
            allowed_ids = registry.allowed_ids()
        policy = UrlPolicy(allowed_ids)
        strict_csp = CspPolicy(allowed_asset_ids=allowed_ids).to_header()
        verdicts: list[SecurityVerdict] = []
        for fixture in ATTACK_FIXTURES:
            requests = _requests_for_attack(fixture)
            denied = True
            reason = "no_requests"
            for request in requests:
                decision = policy.evaluate(request)
                if decision.decision.value != "deny":
                    denied = False
                    reason = decision.reason
                    break
                reason = decision.reason
            expected = fixture.expected_denial_reason
            if denied and expected:
                if requests and expected in _URL_POLICY_DENIAL_REASONS:
                    if reason != expected:
                        denied = False
                        reason = f"expected_denial_reason={expected}, observed={reason}"
                else:
                    # Either the fixture embeds no interceptable URL at all
                    # (script/API class — including fixtures that document a
                    # policy reason their vector cannot reach, like an XHR
                    # with a scheme-relative URL), or the expectation names a
                    # capability.  Both are verified against the sandbox
                    # profile that must supply the denial.
                    covered, capability_reason = _sandbox_denial_for_vector(
                        fixture.vector, strict_csp,
                    )
                    reason = capability_reason
                    if not covered:
                        denied = False
            verdicts.append(SecurityVerdict(fixture.fixture_id, fixture.vector, denied, reason))
        return tuple(verdicts)

    # -- cancel ------------------------------------------------------------

    def probe_cancel(self, fixture: HtmlFixture, config: SpikeConfig) -> bool | None:
        """Cancel immediately and confirm the backend reports CANCELLED."""
        token = CancelToken()
        token.cancel()
        result = self._capture(fixture, frame=config.seek_frames[0], config=config, cancel_token=token)
        if isinstance(result, CaptureFailure) and result.code == CaptureFailureCode.CANCELLED:
            return True
        if isinstance(result, CaptureFailure) and result.code in (CaptureFailureCode.UNSUPPORTED, CaptureFailureCode.BLANK):
            # Backend cannot run here; cancel cannot be demonstrated either way.
            return None
        return False

    # -- aggregate ---------------------------------------------------------

    def run_gate(self, config: SpikeConfig, fixture: HtmlFixture = BENIGN_FIXTURES[1]) -> GateReport:
        det_report, _ = self.probe_determinism(fixture, config)
        ooo_report, _ = self.probe_out_of_order(fixture, config)
        security = self.evaluate_security_corpus()
        cancel = self.probe_cancel(fixture, config)
        notes: list[str] = []
        if not self.backend.is_available():
            notes.append("backend self-test reports unavailable; real-browser experiments skipped-with-reason")
        return GateReport(
            backend_id=self.backend.backend_id,
            backend_available=self.backend.is_available(),
            determinism=det_report,
            out_of_order=ooo_report,
            security=security,
            cancel_honoured=cancel,
            notes=tuple(notes),
        )

    # -- internals ---------------------------------------------------------

    def _capture(
        self,
        fixture: HtmlFixture,
        *,
        frame: int,
        config: SpikeConfig,
        cancel_token: CancelToken | None = None,
    ) -> CapturedFrame | CaptureFailure:
        # Build the CSP from the fixture's bound assets (benign fixtures only).
        csp = CspPolicy(allowed_asset_ids=frozenset(fixture.bound_asset_ids)).to_header()
        policy = UrlPolicy(frozenset(fixture.bound_asset_ids))
        return self.backend.capture(
            fixture.html,
            config.surface,
            frame=frame,
            fps_num=config.fps_num,
            fps_den=config.fps_den,
            csp=csp,
            url_policy=policy,
            timeout_ms=config.timeout_ms,
            cancel_token=cancel_token,
        )


# Every literal denial reason UrlPolicy.evaluate can produce (kept in sync
# with security.py).  A fixture whose ``expected_denial_reason`` appears here
# is URL-policy-verifiable; anything else describes a capability the *sandbox
# profile* must switch off (see _sandbox_denial_for_vector).
_URL_POLICY_DENIAL_REASONS: frozenset[str] = frozenset({
    "url_contains_whitespace",
    "url_contains_backslash_or_control",
    "relative_or_missing_scheme",
    "asset_id_empty",
    "asset_id_not_bound",
    "network_scheme_blocked",
    "network_host_not_allowed",
    "network_host_missing",
    "file_scheme_blocked",
    "data_uri_blocked",
    "blob_uri_blocked",
    "javascript_uri_blocked",
    "mailto_blocked",
    "unrecognized_scheme",
})

# Capability-class vectors → the DISABLED_FEATURES entry that must be switched
# off in the strict W0-A profile.  Legacy CSS ``expression()`` and inline event
# handlers are script-execution surfaces, so the script capability covers them.
_VECTOR_DISABLED_FEATURES: Mapping[str, str] = {
    "popup": "popups",
    "download": "downloads",
    "storage": "storage",
    "permission": "permissions",
    "gpu_api": "gpu-apis",
    "network": "network",
}


def _sandbox_denial_for_vector(vector: str, csp_header: str) -> tuple[bool, str]:
    """Verify the sandbox layer that must deny a capability-class vector.

    Script/storage/permission/... attacks issue no interceptable URL, so the
    URL policy cannot see them.  What the W0-A profile *can* prove without a
    browser is that the capability is switched off: the strict CSP denies the
    corresponding fetch directives and/or ``DISABLED_FEATURES`` lists the
    capability.  Returns ``(covered, reason)``.
    """
    if vector in {"script", "css_expression"}:
        covered = "script-src 'none'" in csp_header
        return covered, "csp_script_src_none" if covered else "csp_allows_scripts"
    if vector == "frame":
        covered = "frame-src 'none'" in csp_header and "object-src 'none'" in csp_header
        return covered, "csp_frame_object_none" if covered else "csp_allows_frames"
    if vector == "form":
        covered = "form-action 'none'" in csp_header
        return covered, "csp_form_action_none" if covered else "csp_allows_forms"
    feature = _VECTOR_DISABLED_FEATURES.get(vector)
    if feature is not None:
        covered = feature in DISABLED_FEATURES
        return covered, f"disabled_feature:{feature}" if covered else f"feature_enabled:{feature}"
    return False, "no_sandbox_mapping_for_vector"


def _requests_for_attack(fixture: AttackFixture) -> tuple[UrlRequest, ...]:
    """Map an attack fixture to the concrete requests the policy must deny.

    Each attack fixture embeds a representative URL the runtime would intercept
    (resource, navigation, or fetch).  We extract it so the policy verdict is
    deterministic and does not depend on a browser actually firing the request.

    Script/API-class fixtures embed no URL at all; they return an empty tuple
    and are verified against the sandbox profile by the caller.  They must NOT
    be mapped to a fabricated ``about:blank`` placeholder: the policy denies
    that for an unrelated reason (``unrecognized_scheme``), which made the
    corpus look covered while testing nothing (AUD-30203).
    """
    text = fixture.html
    requests: list[UrlRequest] = []
    # Common schemes that appear verbatim in the corpus.
    for marker in ("https://", "http://", "file:///", "file://", "data:", "blob:", "javascript:", "mailto:", "topos-asset://"):
        idx = text.find(marker)
        if idx == -1:
            continue
        # Extract up to the next quote/space/angle bracket.
        end_chars = ("'", '"', " ", ">", "<", ")")
        end = len(text)
        for ch in end_chars:
            pos = text.find(ch, idx)
            if pos != -1:
                end = min(end, pos)
        url = text[idx:end].strip()
        if url:
            kind = "navigation" if marker in ("javascript:",) else "resource"
            requests.append(UrlRequest(url=url, kind=kind))
    # Absolute path without scheme.
    if 'src="/' in text:
        idx = text.find('src="/') + 5
        end = text.find('"', idx)
        requests.append(UrlRequest(url=text[idx:end], kind="resource"))
    return tuple(requests)


__all__ = [
    "GateReport",
    "HtmlRendererSpike",
    "SecurityVerdict",
    "SpikeConfig",
]
