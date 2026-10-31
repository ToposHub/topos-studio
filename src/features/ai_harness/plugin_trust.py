"""Trust policies and detached signature verification for plugin manifests.

Content hashes detect accidental or unauthenticated changes.  They do not
prove who authored a third-party plugin.  Discovery therefore accepts an
injected verifier for verified third-party sources.  The HMAC implementation
is useful for controlled deployments and tests; production marketplaces can
inject an Ed25519/PKI-backed verifier without changing discovery.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import base64
import hashlib
import hmac
from typing import Protocol

from src.features.ai_harness.plugin_runtime import PluginManifest, PluginSource


class PluginSignatureVerifier(Protocol):
    def verify(self, manifest: PluginManifest) -> bool:
        """Verify the detached signature over ``manifest.content_hash``."""


@dataclass(frozen=True)
class PluginTrustPolicy:
    """Host policy controlling whether verified third parties need signatures."""

    require_signature_for_verified_third_party: bool = True
    verifier: PluginSignatureVerifier | None = None

    def verify(self, manifest: PluginManifest) -> tuple[bool, str]:
        if manifest.source is not PluginSource.VERIFIED_THIRD_PARTY:
            return True, ""
        if not self.require_signature_for_verified_third_party:
            return True, ""
        if not manifest.signature or not manifest.signature_key_id:
            return False, "PLUGIN_SIGNATURE_MISSING"
        if self.verifier is None:
            return False, "PLUGIN_SIGNATURE_VERIFIER_UNAVAILABLE"
        try:
            valid = bool(self.verifier.verify(manifest))
        except Exception:  # noqa: BLE001 - trust failures are fail-closed
            valid = False
        return (True, "") if valid else (False, "PLUGIN_SIGNATURE_INVALID")


class HmacPluginSignatureVerifier:
    """Detached HMAC-SHA256 verifier for controlled hosts and test fixtures.

    This is not a public-key marketplace signature.  It deliberately makes
    the keyring an injected host concern; keys never live in ``PluginManifest``
    or project files.
    """

    def __init__(self, keys: Mapping[str, bytes | str]) -> None:
        self._keys = {
            str(key_id): value.encode("utf-8") if isinstance(value, str) else bytes(value)
            for key_id, value in keys.items()
        }

    @staticmethod
    def sign(content_hash: str, key: bytes | str) -> str:
        secret = key.encode("utf-8") if isinstance(key, str) else bytes(key)
        digest = hmac.new(secret, str(content_hash).encode("ascii"), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def verify(self, manifest: PluginManifest) -> bool:
        key = self._keys.get(manifest.signature_key_id)
        if not key or not manifest.signature:
            return False
        expected = self.sign(manifest.content_hash, key)
        return hmac.compare_digest(expected, manifest.signature)


__all__ = [
    "HmacPluginSignatureVerifier",
    "PluginSignatureVerifier",
    "PluginTrustPolicy",
]
