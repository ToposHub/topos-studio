"""P8 discovery, integrity and explicit-enable boundary tests."""

from __future__ import annotations

from dataclasses import replace

from src.features.ai_harness.plugin_discovery import (
    PluginDiscovery,
    PluginDiscoveryState,
)
from src.features.ai_harness.plugin_runtime import (
    PluginCapability,
    PluginManifest,
    PluginRegistry,
    PluginSource,
    PluginState,
)
from src.features.ai_harness.plugin_trust import (
    HmacPluginSignatureVerifier,
    PluginTrustPolicy,
)


def _manifest(
    plugin_id: str,
    *,
    source: PluginSource = PluginSource.BUILTIN,
    permission: str = "read",
    declared_hash: str = "",
) -> PluginManifest:
    return PluginManifest(
        plugin_id=plugin_id,
        display_name=plugin_id,
        source=source,
        declared_hash=declared_hash,
        capabilities=(PluginCapability(
            capability_id=f"{plugin_id}.capability",
            tool_names=(f"{plugin_id}.inspect",),
            permission=permission,
        ),),
    )


def test_discovery_marks_local_dev_unverified_and_install_stays_disabled() -> None:
    manifest = _manifest(
        "topos.local",
        source=PluginSource.LOCAL_DEV,
        permission="write",
    )
    candidate = PluginDiscovery.inspect(manifest)

    assert candidate.state == PluginDiscoveryState.UNVERIFIED
    assert candidate.installable is True
    assert candidate.requires_explicit_enable is True
    assert candidate.high_risk is True

    registry = PluginRegistry()
    assert PluginDiscovery.install(registry, candidate)
    assert registry.get(manifest.plugin_id).state == PluginState.DISABLED


def test_verified_third_party_requires_integrity_and_is_pending_enable() -> None:
    missing = PluginDiscovery.inspect(_manifest(
        "topos.missing-hash",
        source=PluginSource.VERIFIED_THIRD_PARTY,
    ))
    assert missing.state == PluginDiscoveryState.REJECTED
    assert missing.installable is False
    assert missing.diagnostics[0].code == "PLUGIN_INTEGRITY_HASH_MISSING"

    unsigned = _manifest("topos.verified", source=PluginSource.VERIFIED_THIRD_PARTY)
    hash_only = replace(unsigned, declared_hash=unsigned.content_hash)
    missing_signature = PluginDiscovery.inspect(hash_only)
    assert missing_signature.state == PluginDiscoveryState.REJECTED
    assert missing_signature.diagnostics[0].code == "PLUGIN_SIGNATURE_MISSING"

    signature = HmacPluginSignatureVerifier.sign(unsigned.content_hash, "test-key")
    verified = replace(
        hash_only,
        signature=signature,
        signature_key_id="marketplace-test",
    )
    candidate = PluginDiscovery.inspect(
        verified,
        trust_policy=PluginTrustPolicy(
            verifier=HmacPluginSignatureVerifier({"marketplace-test": "test-key"}),
        ),
    )
    assert candidate.state == PluginDiscoveryState.PENDING_ENABLE
    assert candidate.installable is True
    assert candidate.requires_explicit_enable is True

    mismatch = PluginDiscovery.inspect(replace(hash_only, declared_hash="0" * 64))
    assert mismatch.state == PluginDiscoveryState.REJECTED
    assert mismatch.diagnostics[0].code == "PLUGIN_INTEGRITY_HASH_MISMATCH"


def test_install_with_report_stages_without_activation() -> None:
    manifest = _manifest("topos.install-report", source=PluginSource.LOCAL_DEV)
    candidate = PluginDiscovery.inspect(manifest)
    registry = PluginRegistry()

    result = PluginDiscovery.install_with_report(registry, candidate)

    assert result.accepted is True
    assert result.state == PluginState.DISABLED.value
    assert result.diagnostics[0].code == "PLUGIN_INSTALLED_DISABLED"
    assert registry.get(manifest.plugin_id).state == PluginState.DISABLED


def test_discovery_rejects_duplicates_and_invalid_inputs_without_side_effects() -> None:
    first = _manifest("topos.duplicate")
    report = PluginDiscovery.discover((first, first, {"plugin_id": "unsafe"}))

    assert report.candidates[0].state == PluginDiscoveryState.READY
    assert report.candidates[1].state == PluginDiscoveryState.REJECTED
    assert report.candidates[1].diagnostics[-1].code == "PLUGIN_DUPLICATE_ID"
    assert report.candidates[2].state == PluginDiscoveryState.REJECTED
    assert report.installable == (report.candidates[0],)
