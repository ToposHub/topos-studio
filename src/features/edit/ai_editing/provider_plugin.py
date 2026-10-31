"""Provider/analysis adapters backed by the AI Harness plugin kernel.

This module is the first P3 bridge.  It gives existing provider-neutral
adapters a plugin identity, lifecycle, source, capability and health snapshot
without moving provider SDKs into the harness or allowing them to edit the
timeline.  A provider becomes visible to :class:`AnalysisService` only after
its plugin is activated; deactivation removes it from future analysis runs
while preserving historical analysis records.

The bridge intentionally does not claim to be a process sandbox.  Provider
execution brokering, credentials and network/path grants remain host-owned
follow-up work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import threading
from typing import Any, Mapping

from src.features.ai_harness.plugin_runtime import (
    PLUGIN_API_VERSION,
    PluginCapability,
    PluginContext,
    PluginExecutionPolicy,
    PluginHealth,
    PluginManifest,
    PluginRegistry,
    PluginSource,
)
from src.features.edit.ai_editing.analysis_models import AnalysisProvider
from src.features.edit.ai_editing.analysis_service import AnalysisService


_PROVIDER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_PROVIDER_KINDS = frozenset({"llm", "analysis", "mcp"})
_DATA_POLICIES = frozenset({"local", "cloud", "hybrid"})


@dataclass(frozen=True)
class ProviderDescriptor:
    """Non-secret identity and capability declaration for one provider."""

    provider_id: str
    display_name: str
    provider_kind: str = "analysis"
    capabilities: tuple[str, ...] = ()
    model_id: str = ""
    version: str = "1.0.0"
    source: PluginSource = PluginSource.BUILTIN
    data_policy: str = "local"
    requires_consent: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        provider_id = str(self.provider_id or "").strip()
        if not _PROVIDER_ID_RE.fullmatch(provider_id):
            raise ValueError("provider_id must use lowercase letters, digits, '.', '_' or '-'")
        if not str(self.display_name or "").strip():
            raise ValueError("provider display_name is required")
        provider_kind = str(self.provider_kind or "").strip().lower()
        if provider_kind not in _PROVIDER_KINDS:
            raise ValueError(f"unsupported provider kind: {provider_kind!r}")
        data_policy = str(self.data_policy or "").strip().lower()
        if data_policy not in _DATA_POLICIES:
            raise ValueError(f"unsupported provider data policy: {data_policy!r}")
        source = self.source if isinstance(self.source, PluginSource) else PluginSource(str(self.source))
        capabilities = tuple(dict.fromkeys(
            str(capability).strip() for capability in self.capabilities if str(capability).strip()
        ))
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "provider_kind", provider_kind)
        object.__setattr__(self, "data_policy", data_policy)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(
            self,
            "requires_consent",
            bool(self.requires_consent or data_policy in {"cloud", "hybrid"}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "display_name": self.display_name[:200],
            "provider_kind": self.provider_kind,
            "capabilities": list(self.capabilities),
            "model_id": self.model_id[:200],
            "version": self.version,
            "source": self.source.value,
            "data_policy": self.data_policy,
            "requires_consent": self.requires_consent,
            "metadata": dict(self.metadata),
        }


class AnalysisProviderPluginAdapter:
    """Lifecycle adapter for one existing ``AnalysisProvider``."""

    def __init__(self, provider: AnalysisProvider) -> None:
        self.provider = provider
        self._active = False

    def activate(self, context: PluginContext) -> None:
        self._active = True

    def deactivate(self) -> None:
        self._active = False

    def health(self) -> PluginHealth:
        if not self._active:
            return PluginHealth(status="inactive", message="provider plugin is inactive")
        checker = getattr(self.provider, "health", None)
        if callable(checker):
            health = checker()
            if isinstance(health, PluginHealth):
                return health
        return PluginHealth(status="healthy", message="analysis provider ready")


class AnalysisPluginHost:
    """Expose analysis providers through ``PluginRegistry`` lifecycle gates.

    AUD-14201：宿主自身的 ``_bindings`` 注册表与其委托的 ``AnalysisService``
    一把 ``threading.Lock`` 保护——与线程安全的 ``PluginRegistry`` 保持同一
    保护级别；并发 register/activate/deactivate/list 不再有 dict 竞态窗口。
    """

    def __init__(
        self,
        analysis_service: AnalysisService,
        *,
        plugin_registry: PluginRegistry | None = None,
    ) -> None:
        self.analysis_service = analysis_service
        self.plugin_registry = plugin_registry or PluginRegistry()
        self._bindings: dict[str, tuple[ProviderDescriptor, AnalysisProvider]] = {}
        self._bindings_lock = threading.Lock()

    def register(
        self,
        provider: AnalysisProvider,
        descriptor: ProviderDescriptor | None = None,
        *,
        policy: PluginExecutionPolicy | None = None,
        enabled: bool = True,
    ) -> bool:
        """Register a provider plugin without making it runnable yet."""

        provider_id = str(getattr(provider, "provider_id", "") or "")
        if descriptor is None:
            kinds = tuple(
                getattr(kind, "value", str(kind))
                for kind in (getattr(provider, "supported_kinds", ()) or ())
            )
            descriptor = ProviderDescriptor(
                provider_id=provider_id,
                display_name=provider_id,
                capabilities=kinds,
            )
        if descriptor.provider_id != provider_id:
            raise ValueError("provider descriptor ID must match provider ID")
        with self._bindings_lock:
            already_bound = provider_id in self._bindings
        if (
            provider_id in self.analysis_service.available_providers()
            and not already_bound
        ):
            # Do not let an untrusted plugin silently replace a built-in
            # provider.  A deliberate migration can unregister the built-in
            # at the composition root before registering its plugin adapter.
            return False
        plugin_id = self.plugin_id_for(provider_id)
        capability = PluginCapability(
            capability_id=f"provider.analysis.{provider_id}",
            description=descriptor.display_name,
            permission="expensive" if descriptor.provider_kind == "analysis" else "read",
            metadata={
                "provider": descriptor.to_dict(),
                "requires_consent": descriptor.requires_consent,
            },
        )
        manifest = PluginManifest(
            plugin_id=plugin_id,
            display_name=descriptor.display_name,
            version=descriptor.version,
            api_version=PLUGIN_API_VERSION,
            source=descriptor.source,
            description=f"{descriptor.provider_kind} provider adapter",
            capabilities=(capability,),
            metadata={"provider": descriptor.to_dict()},
        )
        accepted = self.plugin_registry.register(
            manifest,
            AnalysisProviderPluginAdapter(provider),
            enabled=enabled,
            policy=policy,
        )
        if accepted:
            with self._bindings_lock:
                self._bindings[provider_id] = (descriptor, provider)
        return accepted

    def activate(self, provider_id: str) -> bool:
        provider_id = str(provider_id)
        with self._bindings_lock:
            binding = self._bindings.get(provider_id)
        if binding is None or not self.plugin_registry.activate(self.plugin_id_for(provider_id)):
            return False
        self.analysis_service.register(binding[1])
        return True

    def deactivate(self, provider_id: str) -> bool:
        provider_id = str(provider_id)
        if not self.plugin_registry.deactivate(self.plugin_id_for(provider_id)):
            return False
        self.analysis_service.unregister(provider_id)
        return True

    def descriptor(self, provider_id: str) -> ProviderDescriptor | None:
        with self._bindings_lock:
            binding = self._bindings.get(str(provider_id))
        return binding[0] if binding is not None else None

    def list_descriptors(self, *, active_only: bool = False) -> tuple[ProviderDescriptor, ...]:
        descriptors: list[ProviderDescriptor] = []
        with self._bindings_lock:
            bindings = sorted(self._bindings.items())
        for provider_id, (descriptor, _) in bindings:
            if active_only:
                record = self.plugin_registry.get(self.plugin_id_for(provider_id))
                if record is None or record.state.value != "active":
                    continue
            descriptors.append(descriptor)
        return tuple(descriptors)

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": "topos-analysis-plugin-host/1.0",
            "providers": [descriptor.to_dict() for descriptor in self.list_descriptors()],
            "plugin_registry": self.plugin_registry.snapshot(),
        }

    @staticmethod
    def plugin_id_for(provider_id: str) -> str:
        return f"provider.analysis.{provider_id}"


__all__ = [
    "AnalysisPluginHost",
    "AnalysisProviderPluginAdapter",
    "ProviderDescriptor",
]
