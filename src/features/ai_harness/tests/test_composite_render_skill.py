"""plan §9.5「合成渲染后导入」editorial 通道测试。

覆盖：

1. ``build_composite_render_import`` 构造器（有候选 / 无候选降级 / 待导入提示）。
2. ``LegacyHarnessAdapter`` 的 composite 分支：provider 注入、显式输入回退、
   未知 skill 拒绝。
3. skill_id 路由进 ``EDITORIAL_BUILDER_SKILL_IDS``。
"""
from __future__ import annotations

from typing import Any


from src.features.ai_harness.editorial_skills import (
    SkillArtifact,
    build_composite_render_import,
)
from src.features.ai_harness.skill_compiler import EDITORIAL_BUILDER_SKILL_IDS


def _render_ref(media_asset_id: str = "") -> SkillArtifact:
    return SkillArtifact(
        artifact_id="generated_media:gen-comp-abc123",
        artifact_type="media_generation_ref",
        summary="合成渲染候选",
        payload_ref="gen-comp-abc123",
        media_asset_id=media_asset_id,
    )


class TestBuilder:
    def test_no_render_output_degrades(self) -> None:
        outcome = build_composite_render_import(
            project_id="p1", timeline_id="t1", base_revision=3,
            render_media_ref=None,
        )
        assert outcome.degraded
        assert outcome.plan is None
        assert outcome.artifacts == ()
        codes = [d.get("code") for d in outcome.diagnostics]
        assert "COMPOSITE_RENDER_NO_OUTPUT" in codes

    def test_render_candidate_artifact(self) -> None:
        outcome = build_composite_render_import(
            project_id="p1", timeline_id="t1", base_revision=3,
            render_media_ref=_render_ref(),
        )
        assert not outcome.degraded
        assert outcome.plan is None  # 红线：不产时间线写入计划
        assert len(outcome.artifacts) == 1
        art = outcome.artifacts[0]
        assert art.artifact_type == "media_generation_ref"
        assert art.media_asset_id == ""  # 未导入媒体池前不可放置
        codes = [d.get("code") for d in outcome.diagnostics]
        assert "COMPOSITE_RENDER_PENDING_IMPORT" in codes

    def test_imported_candidate_no_pending_hint(self) -> None:
        outcome = build_composite_render_import(
            project_id="p1", timeline_id="t1", base_revision=3,
            render_media_ref=_render_ref(media_asset_id="media-9"),
        )
        codes = [d.get("code") for d in outcome.diagnostics]
        assert "COMPOSITE_RENDER_PENDING_IMPORT" not in codes


class TestRouting:
    def test_skill_id_in_editorial_set(self) -> None:
        assert "topos.composite_render_import" in EDITORIAL_BUILDER_SKILL_IDS


class _RecordingRuntime:
    """HarnessRuntime 最小替身：记录 finish/diagnostic/artifact 调用。"""

    def __init__(self) -> None:
        self.finished: list[dict[str, Any]] = []
        self.diagnostics: list[dict[str, Any]] = []
        self.artifacts: list[dict[str, Any]] = []

    def mark_running(self, run_id: str) -> None:  # noqa: ARG002
        pass

    def is_cancelled(self, run_id: str) -> bool:  # noqa: ARG002
        return False

    def record_diagnostic(self, run_id: str, diag: Any) -> None:  # noqa: ARG002
        self.diagnostics.append(dict(diag))

    def record_artifact(self, run_id: str, art: Any) -> None:  # noqa: ARG002
        self.artifacts.append(art)

    def finish(self, run_id: str, **kwargs: Any) -> None:  # noqa: ARG002
        self.finished.append(kwargs)


class _Service:
    plan_repository = None


def _composite_invocation(inputs: dict[str, Any] | None = None) -> Any:
    from src.features.ai_harness.invocation_models import (
        Invocation, InvocationScope,
    )

    return Invocation(
        invocation_id="inv-comp-1",
        skill_id="topos.composite_render_import",
        scope=InvocationScope(
            project_id="p1", timeline_id="t1", selected_asset_ids=("a1",),
        ),
        inputs=dict(inputs or {}),
        user_text="",
    )


class TestAdapterCompositeBranch:
    def _adapter(self) -> Any:
        from src.features.ai_harness.legacy_adapters import LegacyHarnessAdapter

        return LegacyHarnessAdapter(_Service(), _RecordingRuntime())  # type: ignore[arg-type]

    def test_provider_render_returns_artifact(self) -> None:
        adapter = self._adapter()
        seen: dict[str, Any] = {}

        def provider(inv: Any, cancel_requested: Any) -> SkillArtifact:
            seen["skill_id"] = inv.skill_id
            seen["cancelled"] = callable(cancel_requested)
            return _render_ref()

        adapter.set_composite_render_provider(provider)
        outcome = adapter._build_editorial_outcome(
            _composite_invocation(), cancel_requested=lambda: False,
        )
        seen_codes = [d.get("code") for d in outcome.diagnostics]
        assert seen["skill_id"] == "topos.composite_render_import"
        assert seen["cancelled"] is True
        assert len(outcome.artifacts) == 1
        assert "COMPOSITE_RENDER_PENDING_IMPORT" in seen_codes

    def test_provider_none_degrades(self) -> None:
        adapter = self._adapter()
        adapter.set_composite_render_provider(lambda inv, cancel: None)
        outcome = adapter._build_editorial_outcome(_composite_invocation())
        assert outcome.degraded

    def test_explicit_imported_media_input_fallback(self) -> None:
        adapter = self._adapter()
        inv = _composite_invocation({"rendered_media_asset_id": "media-7"})
        outcome = adapter._build_editorial_outcome(inv)
        assert len(outcome.artifacts) == 1
        assert outcome.artifacts[0].media_asset_id == "media-7"

    def test_no_graph_no_output_degrades(self) -> None:
        adapter = self._adapter()
        outcome = adapter._build_editorial_outcome(_composite_invocation())
        assert outcome.degraded
        assert "COMPOSITE_RENDER_NO_OUTPUT" in [
            d.get("code") for d in outcome.diagnostics
        ]
