"""EF-16 regression tests — teaching_animation/ai Suggestion-level fixes.

Covers (see docs/plans/2026-09-28-multi-machine-program/REMEDIATION_PLAN.md,
batch EF-16; all fixes live under src/features/teaching_animation/ai):

- AUD-26700 — MotionGraphicBrief.from_dict / apply_patch normalize numeric
  fields (``"30"`` → ``30.0``) and raise a readable ValueError naming the
  field instead of a TypeError from a comparison inside ``__post_init__``.
- AUD-26708 — set_background requires ``color``, add_audio_ref requires
  ``audio_id``; set_brand_theme raises on a present-but-invalid palette/
  type_scale/spacing/mapping instead of silently degrading to ``{}``.
- AUD-26714 — the capability catalog cache exposes an explicit invalidate
  entry point so a runtime registry change can reach the prompt.
- AUD-26802 — DeliveryProfileRegistry implements the documented
  ``version_of`` (displaced profiles are kept, oldest first).
- AUD-26809 — the O(n²) text-overlap check is skipped for scenes beyond the
  complexity hard limit (which already blocks the preview) and still runs
  below it.
- AUD-26812 — MotionGraphicAuditEvent.from_dict survives ``timestamp: None``
  and converges ``brief_revision`` to the declared ``int | None``.
- AUD-26816 — export publish is atomic no-clobber: a target file that
  appears in the check→publish race window is refused (EXPORT_OVERWRITE_
  REQUIRED) instead of being silently overwritten without approval.
- AUD-26819 — export_job no longer imports Callable/Iterable and
  ``is_terminal`` reads the status under the job lock.
- AUD-27001 — SceneDiffSummary docstrings no longer claim "hashable" (a
  frozen dataclass with a Mapping field still raises TypeError on hash()).
- AUD-27012 — MotionGraphicRevisionLog mutations run under an RLock; the
  add_feedback latest-check + store is one atomic section, so concurrent
  feedback cannot fork a lineage.
- AUD-27114 — TestRequirement renames ``required_layers`` to ``any_layer_of``
  so the field name matches compute_coverage's any-of semantics.

The ai_harness/tests half of EF-16 (AUD-2250x/2260x/2270x) is verified by
running that directory's own suite; no duplicate coverage here.

Run: .venv/bin/python -m pytest tests/teaching_animation/test_ef16_aud.py -q
"""

from __future__ import annotations

import dataclasses
import importlib
import threading
import uuid
from pathlib import Path
from typing import Any

import pytest

from src.features.teaching_animation.ai.brief import MotionGraphicBrief
from src.features.teaching_animation.ai.command_catalog import (
    ExtendedCommandRegistry,
)
from src.features.teaching_animation.ai.commands import TeachingAICommand
from src.features.teaching_animation.ai.context_provider import (
    _ai_capability_catalog,
    invalidate_ai_capability_catalog,
)
from src.features.teaching_animation.ai.delivery_profile import (
    DeliveryProfileRegistry,
)
from src.features.teaching_animation.ai.design_validator import (
    MOTION_GRAPHIC_COMPLEXITY_LIMIT,
    DesignValidationOptions,
    validate_scene_design,
)
from src.features.teaching_animation.ai.error_codes import (
    MOTION_GRAPHIC_REVISION_CONFLICT,
    MotionGraphicAuditEvent,
    MotionGraphicFailure,
)
from src.features.teaching_animation.ai.export_job import (
    EXPORT_FAILED,
    EXPORT_OVERWRITE_REQUIRED,
    EXPORT_SUCCEEDED,
    ExportRequest,
    new_export_job,
    postflight_from_stat,
)
from src.features.teaching_animation.ai.preview import SceneDiffSummary
from src.features.teaching_animation.ai.proposal import (
    CreativeProposal,
    NarrativeBeat,
    StyleTokens,
)
from src.features.teaching_animation.ai.revision import (
    FEEDBACK_COPY,
    FeedbackPatch,
    MotionGraphicRevisionLog,
)
from src.features.teaching_animation.ai import test_matrix as _mg_test_matrix
from src.features.teaching_animation.core.model import TeachingSceneDocument

# 短别名（经模块属性访问；名字避开 Test* 前缀，避免 pytest 误收集为测试类）。
DEFAULT_MATRIX = _mg_test_matrix.DEFAULT_MATRIX
LAYER_INTEGRATION = _mg_test_matrix.LAYER_INTEGRATION
LAYER_UNIT = _mg_test_matrix.LAYER_UNIT
MGTestRequirement = _mg_test_matrix.TestRequirement
compute_coverage = _mg_test_matrix.compute_coverage


# --------------------------------------------------------------------------- #
# Shared factories
# --------------------------------------------------------------------------- #


def _scene() -> TeachingSceneDocument:
    return TeachingSceneDocument.create_default("EF16 Test Scene")


def _complete_brief(**overrides: Any) -> MotionGraphicBrief:
    base: dict[str, Any] = dict(
        brief_id="mg-brief-ef16",
        topic="介绍 AI Agent",
        duration_seconds=15.0,
        canvas_aspect="16:9",
    )
    base.update(overrides)
    return MotionGraphicBrief(**base)


def _proposal(**overrides: Any) -> CreativeProposal:
    brief = _complete_brief(brief_id="mg-brief-1")
    base: dict[str, Any] = dict(
        proposal_id=f"mg-prop-{uuid.uuid4().hex[:8]}",
        brief_id=brief.brief_id,
        brief_revision=brief.revision,
        brief_hash=brief.brief_hash(),
        title="方案 A · 科技冷色",
        summary="15 秒介绍 AI Agent 的四个步骤。",
        beats=(
            NarrativeBeat("b1", "开场", 0.0, 3.0, on_screen_copy="AI Agent"),
            NarrativeBeat("b2", "理解", 3.0, 3.0),
            NarrativeBeat("b3", "规划", 6.0, 3.0),
            NarrativeBeat("b4", "执行 + 验证", 9.0, 6.0),
        ),
        style=StyleTokens(palette={"background": "#0f172a", "accent": "#38bdf8"}),
    )
    base.update(overrides)
    return CreativeProposal(**base)


def _text_element(
    element_id: str,
    *,
    x: float = 100,
    y: float = 100,
    width: float = 600,
    height: float = 80,
) -> dict[str, Any]:
    return {
        "element_id": element_id,
        "type": "text",
        "z_index": 1,
        "transform": {"x": x, "y": y, "width": width, "height": height},
        "style": {"font_size": 48, "color": "#ffffff"},
        "content": "标题",
    }


def _mg_scene_dict(elements: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "scene_id": "scene-ef16",
        "schema_version": 1,
        "name": "EF16 scene",
        "width": 1920,
        "height": 1080,
        "fps_num": 24,
        "fps_den": 1,
        "duration_frames": 360,
        "background": {"color": "#0f172a"},
        "elements": elements,
        "animations": [],
        "markers": [],
        "asset_refs": [],
    }


def _teaching_ai_proposal_error() -> type[Exception]:
    from src.features.teaching_animation.ai.commands import TeachingAIProposalError

    return TeachingAIProposalError


# --------------------------------------------------------------------------- #
# AUD-26700 — brief numeric normalization
# --------------------------------------------------------------------------- #


def test_aud26700_from_dict_normalizes_numeric_strings() -> None:
    brief = MotionGraphicBrief.from_dict({
        "brief_id": "b1",
        "duration_seconds": "30",
        "width": "1920",
        "height": "1080",
        "fps": "30",
        "budget_units": "2.5",
        "call_to_action": 123,
        "source_user_text_hash": 456,
    })
    assert brief.duration_seconds == 30.0
    assert brief.width == 1920
    assert brief.height == 1080
    assert brief.fps == 30.0
    assert brief.budget_units == 2.5
    # 标量字符串字段收敛为 str（文档/注解承诺）。
    assert brief.call_to_action == "123"
    assert brief.source_user_text_hash == "456"


def test_aud26700_from_dict_defaults_survive_missing_fields() -> None:
    brief = MotionGraphicBrief.from_dict({"brief_id": "b1"})
    assert brief.duration_seconds is None
    assert brief.width is None
    assert brief.height is None
    assert brief.fps == 24.0
    assert brief.budget_units is None


def test_aud26700_from_dict_rejects_unparseable_number_as_value_error() -> None:
    # 反例：畸形数据必须以可读的校验错误失败，而不是 '<' not supported 的
    # TypeError（修复前的行为）。
    with pytest.raises(ValueError) as exc_info:
        MotionGraphicBrief.from_dict({
            "brief_id": "b1", "duration_seconds": "abc",
        })
    assert "duration_seconds" in str(exc_info.value)
    with pytest.raises(ValueError) as exc_info2:
        MotionGraphicBrief.from_dict({"brief_id": "b1", "width": [1, 2]})
    assert "width" in str(exc_info2.value)


def test_aud26700_apply_patch_normalizes_and_rejects_bad_scalars() -> None:
    brief = _complete_brief()
    patched, _patch = brief.apply_patch({"fps": "30", "width": "800"})
    assert patched.fps == 30.0
    assert patched.width == 800
    assert patched.revision == brief.revision + 1
    with pytest.raises(ValueError) as exc_info:
        brief.apply_patch({"duration_seconds": "abc"})
    assert "duration_seconds" in str(exc_info.value)
    with pytest.raises(ValueError):
        brief.apply_patch({"budget_units": {"not": "a number"}})


# --------------------------------------------------------------------------- #
# AUD-26708 — command input validation
# --------------------------------------------------------------------------- #


def test_aud26708_set_background_requires_color() -> None:
    registry = ExtendedCommandRegistry()
    scene = _scene()
    err = _teaching_ai_proposal_error()
    # 反例：缺少 color 的背景字典不再静默持久化。
    with pytest.raises(err):
        registry.compile(
            TeachingAICommand("set_background", {"background": {"blur": 0.5}}),
            scene,
        )
    # 正例：带 color 的合法背景通过。
    command = registry.compile(
        TeachingAICommand("set_background", {"background": {"color": "#0f172a"}}),
        scene,
    )
    assert dict(command.background)["color"] == "#0f172a"


def test_aud26708_add_audio_ref_requires_audio_id() -> None:
    registry = ExtendedCommandRegistry()
    scene = _scene()
    err = _teaching_ai_proposal_error()
    with pytest.raises(err):
        registry.compile(
            TeachingAICommand("add_audio_ref", {"audio": {"path": "/x.mp3"}}),
            scene,
        )
    command = registry.compile(
        TeachingAICommand("add_audio_ref", {"audio": {"audio_id": "aud-1"}}),
        scene,
    )
    assert dict(command.audio)["audio_id"] == "aud-1"


def test_aud26708_set_brand_theme_invalid_palette_raises() -> None:
    registry = ExtendedCommandRegistry()
    scene = _scene()
    err = _teaching_ai_proposal_error()
    # 反例：palette 非法（非 Mapping）→ 显式报错，而不是静默降级为 {}。
    with pytest.raises(err) as exc_info:
        registry.compile(
            TeachingAICommand("set_brand_theme", {"theme": {
                "theme_id": "t1", "palette": "not-a-map",
            }}),
            scene,
        )
    assert "palette" in str(exc_info.value)
    # 正例：省略可选字段仍允许（默认 {}），合法 Mapping 通过。
    command = registry.compile(
        TeachingAICommand("set_brand_theme", {"theme": {"theme_id": "t1"}}),
        scene,
    )
    assert dict(command.theme.palette) == {}
    command2 = registry.compile(
        TeachingAICommand("set_brand_theme", {"theme": {
            "theme_id": "t1", "palette": {"primary": "#38bdf8"},
        }}),
        scene,
    )
    assert dict(command2.theme.palette)["primary"] == "#38bdf8"


# --------------------------------------------------------------------------- #
# AUD-26714 — capability catalog invalidation
# --------------------------------------------------------------------------- #


@pytest.fixture
def _clean_capability_cache():
    invalidate_ai_capability_catalog()
    yield
    invalidate_ai_capability_catalog()


def test_aud26714_catalog_is_cached_until_invalidated(
    _clean_capability_cache: None,
) -> None:
    first = _ai_capability_catalog()
    # 正例：同一进程内缓存命中（同一对象），prompt 构建不重复建注册表。
    assert _ai_capability_catalog() is first
    assert "add_element" in first["commands"]


def test_aud26714_invalidate_lets_registry_changes_reach_the_prompt(
    _clean_capability_cache: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.features.teaching_animation.ai import command_catalog

    base = _ai_capability_catalog()
    assert "zzz_ef16_test_command" not in base["commands"]
    # 模拟运行期注册表扩展（新命令进入词汇表）。
    monkeypatch.setattr(
        command_catalog, "EXTENDED_COMMAND_NAMES",
        frozenset({"zzz_ef16_test_command"}),
    )
    # 反例（修复前行为）：不失效时缓存冻结，新命令永远进不了 prompt。
    assert _ai_capability_catalog() is base
    assert "zzz_ef16_test_command" not in _ai_capability_catalog()["commands"]
    # 正例（修复后）：显式失效入口后重建，注册表变化直达 prompt。
    invalidate_ai_capability_catalog()
    refreshed = _ai_capability_catalog()
    assert "zzz_ef16_test_command" in refreshed["commands"]


# --------------------------------------------------------------------------- #
# AUD-26802 — registry version_of
# --------------------------------------------------------------------------- #


def test_aud26802_version_of_preserves_displaced_profiles() -> None:
    registry = DeliveryProfileRegistry()
    original = registry.require("web_1080p")
    upgraded = dataclasses.replace(original, fps=60.0)
    registry.register(upgraded)
    # 新版本生效，旧版本保留且可经 version_of 取回（文档承诺）。
    assert registry.get("web_1080p") is upgraded
    assert registry.version_of("web_1080p") == (original,)

    third = dataclasses.replace(original, fps=90.0)
    registry.register(third)
    assert registry.version_of("web_1080p") == (original, upgraded)
    assert registry.get("web_1080p") is third


def test_aud26802_init_extras_also_keep_displaced_builtin() -> None:
    builtin = DeliveryProfileRegistry().require("web_1080p")
    upgraded = dataclasses.replace(builtin, fps=60.0)
    registry = DeliveryProfileRegistry(extras=(upgraded,))
    assert registry.get("web_1080p") is upgraded
    assert registry.version_of("web_1080p") == (builtin,)


def test_aud26802_version_of_empty_for_unknown_or_fresh_ids() -> None:
    registry = DeliveryProfileRegistry()
    assert registry.version_of("web_1080p") == ()  # 从未被替换
    assert registry.version_of("no_such_profile") == ()


# --------------------------------------------------------------------------- #
# AUD-26809 — overlap check skip beyond the complexity hard limit
# --------------------------------------------------------------------------- #


def test_aud26809_overlap_check_skipped_beyond_hard_limit() -> None:
    elements = [_text_element(f"t{i}", y=100 + i * 200) for i in range(3)]
    elements.append(_text_element("t-overlap-a", y=100))
    elements.append(_text_element("t-overlap-b", y=100))  # 与 a 完全重叠
    scene = _mg_scene_dict(elements)
    options = DesignValidationOptions(max_elements_warning=2, max_elements_error=4)
    report = validate_scene_design(scene, options=options)
    codes = [finding.code for finding in report.findings]
    # 超硬上限 → 复杂度 error 仍在（阻断语义不丢）。
    assert MOTION_GRAPHIC_COMPLEXITY_LIMIT in codes
    overlap = [
        finding for finding in report.findings
        if finding.code == "MOTION_GRAPHIC_TEXT_OVERFLOW" and "重叠" in finding.message
    ]
    # 反例不再发生：超限时不再跑 O(n²) 重叠检查（已有 error 阻断）。
    assert overlap == []


def test_aud26809_overlap_check_still_runs_below_hard_limit() -> None:
    elements = [
        _text_element("a", y=100),
        _text_element("b", y=100),  # 与 a 完全重叠
        _text_element("c", y=900),
    ]
    scene = _mg_scene_dict(elements)
    options = DesignValidationOptions(max_elements_warning=2, max_elements_error=4)
    report = validate_scene_design(scene, options=options)
    overlap = [
        finding for finding in report.findings
        if finding.code == "MOTION_GRAPHIC_TEXT_OVERFLOW" and "重叠" in finding.message
    ]
    # 正例：未超限时重叠告警照常产出（跳过逻辑不能误杀常规检查）。
    assert overlap, "限内场景必须仍然报告文本重叠"


# --------------------------------------------------------------------------- #
# AUD-26812 — audit event from_dict defense
# --------------------------------------------------------------------------- #


def test_aud26812_from_dict_survives_none_timestamp_and_converges_revision() -> None:
    event = MotionGraphicAuditEvent.from_dict({
        "code": "MG_TEST", "stage": "unit",
        "timestamp": None,          # 修复前：float(None) → TypeError
        "brief_revision": "7",      # 修复前：str 原样通过，破坏 int | None 注解
        "cost_units": None,
        "duration_seconds": None,
    })
    assert event.timestamp == 0.0
    assert event.brief_revision == 7
    assert isinstance(event.brief_revision, int)
    assert event.cost_units == 0.0
    assert event.duration_seconds == 0.0


def test_aud26812_from_dict_keeps_none_revision_as_none() -> None:
    event = MotionGraphicAuditEvent.from_dict({
        "code": "MG_TEST", "stage": "unit", "brief_revision": None,
    })
    assert event.brief_revision is None


# --------------------------------------------------------------------------- #
# AUD-26816 — publish is atomic no-clobber (TOCTOU)
# --------------------------------------------------------------------------- #


def _export_job(tmp_path: Path, *, overwrite: bool) -> tuple[Any, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    final_path = tmp_path / "out.mp4"
    temp_path = tmp_path / "out.mp4.tmp"
    temp_path.write_bytes(b"rendered-bytes")
    request = ExportRequest(
        scene_id="s1", output_path=str(final_path), profile_id="web_1080p",
        overwrite_existing=overwrite,
    )
    job = new_export_job(request)
    job.start()
    job.output_temp_path = str(temp_path)
    return job, final_path, temp_path


def test_aud26816_refuses_existing_target_without_approval(tmp_path: Path) -> None:
    job, final_path, temp_path = _export_job(tmp_path, overwrite=False)
    final_path.write_bytes(b"victim-content")
    report = postflight_from_stat(str(temp_path))
    job.finish(postflight=report, output_temp_path=str(temp_path))
    assert job.status == EXPORT_FAILED
    assert job.diagnostic_code == EXPORT_OVERWRITE_REQUIRED
    # 已存在文件未被覆盖，临时文件已清理。
    assert final_path.read_bytes() == b"victim-content"
    assert not temp_path.exists()


def test_aud26816_race_window_target_is_not_clobbered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """目标文件在“检查之后、发布之前”出现 → 拒绝覆盖（TOCTOU 关闭）。"""
    import os as os_module

    job, final_path, temp_path = _export_job(tmp_path, overwrite=False)
    real_link = os_module.link

    def racing_link(src: str, dst: str, *, _real=real_link) -> None:
        # 模拟另一进程/任务在竞态窗口内创建了目标文件。
        Path(dst).write_bytes(b"victim-content")
        _real(src, dst)

    monkeypatch.setattr(os_module, "link", racing_link)
    report = postflight_from_stat(str(temp_path))
    job.finish(postflight=report, output_temp_path=str(temp_path))
    assert job.status == EXPORT_FAILED
    assert job.diagnostic_code == EXPORT_OVERWRITE_REQUIRED
    # 竞态窗口内出现的文件内容完好无损（修复前会被 os.replace 静默覆盖）。
    assert final_path.read_bytes() == b"victim-content"
    assert not temp_path.exists()


def test_aud26816_publish_succeeds_with_approval_and_when_absent(tmp_path: Path) -> None:
    # 用例 1：目标不存在 → 正常发布。
    job, final_path, temp_path = _export_job(tmp_path / "case1", overwrite=False)
    report = postflight_from_stat(str(temp_path))
    job.finish(postflight=report, output_temp_path=str(temp_path))
    assert job.status == EXPORT_SUCCEEDED
    assert final_path.read_bytes() == b"rendered-bytes"
    assert not temp_path.exists()
    assert job.is_terminal

    # 用例 2：目标已存在但已批准覆盖 → 覆盖成功。
    job2, final_path2, temp_path2 = _export_job(tmp_path / "case2", overwrite=True)
    final_path2.write_bytes(b"old-content")
    report2 = postflight_from_stat(str(temp_path2))
    job2.finish(postflight=report2, output_temp_path=str(temp_path2))
    assert job2.status == EXPORT_SUCCEEDED
    assert final_path2.read_bytes() == b"rendered-bytes"
    assert not temp_path2.exists()


# --------------------------------------------------------------------------- #
# AUD-26819 — dead imports gone; is_terminal takes the lock
# --------------------------------------------------------------------------- #


def test_aud26819_unused_typing_imports_removed() -> None:
    module: Any = importlib.import_module(
        "src.features.teaching_animation.ai.export_job",
    )
    # Callable / Iterable 从未被使用 → 不应再出现在模块命名空间。
    assert not hasattr(module, "Callable")
    assert not hasattr(module, "Iterable")
    assert hasattr(module, "Mapping")  # 仍在使用


class _LockProbe:
    """Replacement for the job lock that counts ``with`` entries."""

    def __init__(self) -> None:
        self._inner = threading.RLock()
        self.enter_count = 0

    def __enter__(self) -> None:
        self.enter_count += 1
        self._inner.__enter__()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._inner.__exit__(exc_type, exc, tb)


def test_aud26819_is_terminal_reads_status_under_lock() -> None:
    job = new_export_job(ExportRequest(
        scene_id="s1", output_path="/tmp/ef16.mp4", profile_id="web_1080p",
    ))
    assert job.is_terminal is False
    probe = _LockProbe()
    job._lock = probe  # type: ignore[assignment]
    _ = job.is_terminal
    # 正例：状态读取在锁内进行（与其它状态访问的锁协议一致）。
    assert probe.enter_count == 1


# --------------------------------------------------------------------------- #
# AUD-27001 — SceneDiffSummary hashability docs
# --------------------------------------------------------------------------- #


def test_aud27001_scediffsummary_hash_raises_type_error() -> None:
    summary = SceneDiffSummary(timeline_insertion={"track_id": "v1", "insert_frame": 0})
    # frozen dataclass 含 Mapping 字段 → hash() 必然 TypeError（行为锁定）。
    with pytest.raises(TypeError):
        hash(summary)


def test_aud27001_docs_no_longer_claim_hashable() -> None:
    import src.features.teaching_animation.ai.preview as preview_module

    source = Path(preview_module.__file__).read_text(encoding="utf-8")
    assert "compact, hashable description" not in source
    assert "Not hashable" in source


# --------------------------------------------------------------------------- #
# AUD-27012 — revision log locking
# --------------------------------------------------------------------------- #


def test_aud27012_add_feedback_runs_under_lock() -> None:
    log = MotionGraphicRevisionLog()
    first = log.add_initial(_proposal())
    patch = FeedbackPatch(patch_id="fp-1", categories=(FEEDBACK_COPY,))
    log._lock = probe = _LockProbe()  # type: ignore[assignment]
    log.add_feedback(first.revision_id, _proposal(title="v2"), patch)
    # 正例：变更路径持锁（latest 检查 + 写入在同一原子段）。
    assert probe.enter_count >= 1

    log._lock = probe2 = _LockProbe()  # type: ignore[assignment]
    with pytest.raises(MotionGraphicFailure) as exc_info:
        # 反例路径（基于旧版本修改）同样持锁并以稳定码拒绝。
        log.add_feedback(first.revision_id, _proposal(title="v3"), patch)
    assert probe2.enter_count >= 1
    assert exc_info.value.code == MOTION_GRAPHIC_REVISION_CONFLICT


def test_aud27012_concurrent_feedback_cannot_fork_lineage() -> None:
    log = MotionGraphicRevisionLog()
    first = log.add_initial(_proposal())
    patch = FeedbackPatch(patch_id="fp-race", categories=(FEEDBACK_COPY,))
    winner: list[Any] = []
    losers: list[MotionGraphicFailure] = []
    barrier = threading.Barrier(8)

    def worker(index: int) -> None:
        barrier.wait()
        try:
            winner.append(log.add_feedback(
                first.revision_id, _proposal(title=f"v-race-{index}"), patch,
            ))
        except MotionGraphicFailure as exc:
            losers.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    # 恰好一个成功，其余全部以稳定码拒绝；血缘没有分叉。
    assert len(winner) == 1
    assert len(losers) == 7
    assert all(exc.code == MOTION_GRAPHIC_REVISION_CONFLICT for exc in losers)
    assert len(log.all_revisions()) == 2  # initial + 唯一 child
    assert log.latest_for(first.lineage_id) is winner[0]


# --------------------------------------------------------------------------- #
# AUD-27114 — any_layer_of naming matches coverage semantics
# --------------------------------------------------------------------------- #


def test_aud27114_coverage_uses_any_of_semantics() -> None:
    requirement = MGTestRequirement(
        "MG-EF16", "any-of semantics", any_layer_of=(LAYER_UNIT, LAYER_INTEGRATION),
    )
    # 只覆盖其中一层（integration）→ 已算 covered（any-of 语义）。
    report = compute_coverage(
        (requirement,), collected_by_capability={"MG-EF16": (LAYER_INTEGRATION,)},
    )
    assert report.covered == ("MG-EF16",)
    # 一层都没有 → missing。
    report2 = compute_coverage((requirement,), collected_by_capability={})
    assert report2.missing == ("MG-EF16",)


def test_aud27114_default_matrix_coverage_still_works() -> None:
    collected = {"MG-01": (LAYER_UNIT,)}
    report = compute_coverage(DEFAULT_MATRIX, collected_by_capability=collected)
    assert "MG-01" in report.covered
    assert "MG-02" in report.missing


def test_aud27114_field_renamed_to_any_layer_of() -> None:
    requirement = MGTestRequirement("MG-Z", "renamed", any_layer_of=(LAYER_UNIT,))
    assert requirement.any_layer_of == (LAYER_UNIT,)
    payload = requirement.to_dict()
    assert payload["any_layer_of"] == ["unit"]
    assert "required_layers" not in payload
    # 反例：旧字段名不再被接受（消除名实矛盾）。
    with pytest.raises(TypeError):
        MGTestRequirement("MG-Z", "old name", required_layers=(LAYER_UNIT,))  # type: ignore[call-arg]
