"""P1-14 — 项目级代理策略核心测试。

覆盖：
- ProxyPolicySettings 校验/序列化
- evaluate_proxy_action 纯函数决策与原因（诊断基础）
- EditProject.proxy_policy 访问器/with_proxy_policy/不可变更新保持
- UpdateProxyPolicyCommand apply/undo/redo
- export_source_path 导出取源策略（默认强制 master）
- 持久化 roundtrip（serializer + .topos 包）
"""

from __future__ import annotations

import sys
import os
from dataclasses import replace
from pathlib import Path

import pytest

pytestmark = pytest.mark.edit_core

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from src.features.edit.core.commands import UpdateProxyPolicyCommand
from src.features.edit.core.project import EditProject, MediaAsset, update_asset_proxy
from src.features.edit.core.proxy_policy import (
    PROXY_POLICY_MODES,
    ProxyActionDecision,
    ProxyPolicySettings,
    evaluate_proxy_action,
    export_source_path,
)


# ================================================================
# 设置类型
# ================================================================


def test_defaults_and_modes():
    policy = ProxyPolicySettings()
    assert policy.mode == "auto"
    assert policy.max_native_width == 1920
    assert policy.max_native_height == 1080
    assert policy.target_height == 1080
    assert policy.auto_generate is True
    assert policy.export_use_master is True
    assert set(PROXY_POLICY_MODES) == {"off", "auto", "all"}


def test_validation_rejects_bad_values():
    with pytest.raises(ValueError):
        ProxyPolicySettings(mode="sometimes")
    with pytest.raises(ValueError):
        ProxyPolicySettings(max_native_width=0)
    with pytest.raises(ValueError):
        ProxyPolicySettings(target_height=-2)


def test_dict_roundtrip():
    policy = ProxyPolicySettings(
        mode="all", max_native_width=3840, max_native_height=2160,
        target_height=1080, auto_generate=False, export_use_master=False,
    )
    restored = ProxyPolicySettings.from_dict(policy.to_dict())
    assert restored == policy
    assert ProxyPolicySettings.from_dict(None) == ProxyPolicySettings()
    assert ProxyPolicySettings.from_dict({}) == ProxyPolicySettings()


# ================================================================
# 决策评估
# ================================================================


def test_evaluate_auto_above_threshold_generates():
    policy = ProxyPolicySettings()
    decision = evaluate_proxy_action(
        policy, media_type="video", width=3840, height=2160,
    )
    assert decision.should_generate
    assert decision.reason == "above_native_threshold"
    # 宽度超阈值也触发
    wide = evaluate_proxy_action(policy, media_type="video", width=2560, height=1080)
    assert wide.should_generate


def test_evaluate_skip_reasons():
    policy = ProxyPolicySettings()
    cases = [
        (dict(media_type="video", width=1920, height=1080), "below_threshold"),
        (dict(media_type="audio", width=0, height=0), "not_video"),
        (dict(media_type="image", width=8000, height=6000), "not_video"),
        (dict(media_type="video", width=3840, height=2160, is_offline=True), "offline"),
    ]
    for kwargs, reason in cases:
        decision = evaluate_proxy_action(policy, **kwargs)
        assert decision.action == "skip"
        assert decision.reason == reason


def test_evaluate_mode_off_and_all():
    off = ProxyPolicySettings(mode="off")
    assert evaluate_proxy_action(
        off, media_type="video", width=7680, height=4320
    ).reason == "policy_off"
    all_mode = ProxyPolicySettings(mode="all")
    decision = evaluate_proxy_action(all_mode, media_type="video", width=640, height=360)
    assert decision.should_generate and decision.reason == "policy_all"
    # 总开关关闭优先于 all
    disabled = ProxyPolicySettings(mode="all", auto_generate=False)
    assert evaluate_proxy_action(
        disabled, media_type="video", width=640, height=360
    ).reason == "auto_generate_disabled"


def test_decision_is_plain_data():
    d = ProxyActionDecision(action="skip", reason="below_threshold")
    assert not d.should_generate


# ================================================================
# EditProject 集成
# ================================================================


def test_project_accessor_defaults_and_dict():
    project = EditProject.create(name="P")
    assert project.proxy_policy is None
    assert project.get_proxy_policy() == ProxyPolicySettings()

    from src.features.edit.core.serialization import EditProjectSerializer

    as_dict = {"mode": "all", "target_height": 720}
    project = EditProjectSerializer._dict_to_project(
        dict(EditProjectSerializer._project_to_dict(project), proxy_policy=as_dict)
    )
    parsed = project.get_proxy_policy()
    assert parsed.mode == "all"
    assert parsed.target_height == 720


def test_immutable_updates_preserve_policy():
    project = EditProject.create(name="P")
    project = project.with_proxy_policy(ProxyPolicySettings(mode="all"))
    asset = MediaAsset(
        asset_id="a1", path="/tmp/x.mov", media_type="video",
        display_name="x", metadata={},
    )
    project = project.import_asset(
        "/tmp/x.mov", "video", {}, display_name="x"
    )
    asset_id = project.get_asset_by_path("/tmp/x.mov").asset_id

    # 任一不可变更新不应把策略重置为默认
    project = update_asset_proxy(project, asset_id, "/tmp/x_proxy.mov", "ready")
    assert project.get_proxy_policy().mode == "all"
    project = project.rename_bin(project.root_bin_id, "Renamed")
    assert project.get_proxy_policy().mode == "all"


def test_command_apply_undo_redo():
    stack_project = EditProject.create(name="P")
    cmd = UpdateProxyPolicyCommand.create(
        ProxyPolicySettings(mode="off", target_height=720)
    )
    p1 = cmd.apply(stack_project)
    assert p1.get_proxy_policy().mode == "off"
    assert p1.get_proxy_policy().target_height == 720

    p2 = cmd.undo(p1)
    # 默认策略（未设置过）→ undo 回 None → accessor 给默认
    assert p2.proxy_policy is None
    assert p2.get_proxy_policy().mode == "auto"

    # 有旧值时 undo 精确恢复
    p3 = cmd.apply(p2) if False else p1  # p1 已是 applied 状态
    cmd2 = UpdateProxyPolicyCommand.create(ProxyPolicySettings(mode="all"))
    p4 = cmd2.apply(p1)  # old = off/720
    assert p4.get_proxy_policy().mode == "all"
    p5 = cmd2.undo(p4)
    assert p5.get_proxy_policy().mode == "off"
    assert p5.get_proxy_policy().target_height == 720
    p6 = cmd2.redo(p5)
    assert p6.get_proxy_policy().mode == "all"


# ================================================================
# 导出取源
# ================================================================


def _asset_with_proxy() -> MediaAsset:
    return MediaAsset(
        asset_id="a1", path="/tmp/master.mov", media_type="video",
        display_name="m", metadata={},
        proxy_path="/tmp/proxy.mov", proxy_status="ready",
    )


def test_export_source_path_defaults_to_master():
    project = EditProject.create(name="P")
    asset = _asset_with_proxy()
    # 默认策略：导出强制 master（避免 720p 预览代理进成片）
    assert export_source_path(project, asset) == "/tmp/master.mov"

    project = project.with_proxy_policy(
        ProxyPolicySettings(export_use_master=False)
    )
    assert export_source_path(project, asset) == "/tmp/proxy.mov"

    # 无代理素材始终回退原片
    no_proxy = MediaAsset(
        asset_id="a2", path="/tmp/plain.mov", media_type="video",
        display_name="p", metadata={},
    )
    assert export_source_path(project, no_proxy) == "/tmp/plain.mov"


# ================================================================
# .topos 包持久化
# ================================================================


def test_topos_package_roundtrip(tmp_path):
    from src.features.edit.core.edit_project_io import EditProjectIO

    project = EditProject.create(name="Policy Roundtrip").with_proxy_policy(
        ProxyPolicySettings(
            mode="all", max_native_width=3840, max_native_height=2160,
            target_height=720, export_use_master=False,
        )
    )
    io = EditProjectIO()
    pkg = tmp_path / "policy.topos"
    assert io.save_project(str(pkg), project)
    loaded, _ui, _seqs = io.load_project(str(pkg))
    restored = loaded.get_proxy_policy()
    assert restored.mode == "all"
    assert restored.max_native_width == 3840
    assert restored.target_height == 720
    assert restored.export_use_master is False


def test_all_common_updates_preserve_policy():
    """所有常见不可变更新方法都必须携带 proxy_policy（threading 不变量）。"""
    from src.features.edit.core.project import SmartBin

    project = EditProject.create(name="P").with_proxy_policy(
        ProxyPolicySettings(mode="all", target_height=720)
    )
    project = project.import_asset("/tmp/p.mov", "video", {}, display_name="p")
    asset_id = project.get_asset_by_path("/tmp/p.mov").asset_id
    project = project.create_timeline("T1")
    timeline_id = next(iter(project.timelines))
    project = project.set_active_timeline(timeline_id)
    project = project.add_smart_bin(SmartBin(
        bin_id="sb-x", name="S", filter_type="custom", filter_params={},
    ))
    project = project.rename_bin(project.root_bin_id, "Renamed Root")
    project = update_asset_proxy(project, asset_id, "/tmp/p_proxy.mov", "ready")

    policy = project.get_proxy_policy()
    assert policy.mode == "all"
    assert policy.target_height == 720
    assert project.timelines[timeline_id].name == "T1"


# ================================================================
# P2-02/T-02: codec 感知策略（证据：topos_422_family_benchmark_2026-09-16）
# ================================================================


def test_topos_family_never_proxied_any_resolution():
    """Topos native 是编辑优化编码（4K 75.6fps）：任何分辨率都不套代理。"""
    policy = ProxyPolicySettings()
    for w, h in ((3840, 2160), (7680, 4320)):
        d = evaluate_proxy_action(policy, media_type="video", width=w, height=h,
                                  codec="topos 422 hq")
        assert not d.should_generate
        assert d.reason == "topos_edit_codec_no_proxy_needed"
    # "all" 模式同样豁免（原生即优化中间片）
    d = evaluate_proxy_action(
        replace(policy, mode="all"), media_type="video", width=3840, height=2160,
        codec="topos_proxy",
    )
    assert not d.should_generate
    assert d.reason == "topos_edit_codec_no_proxy_needed"


def test_long_gop_and_heavy_edit_reason_codes():
    """超阈值生成的原因码区分 long_gop / heavy_edit（T-02 排序依据）。"""
    policy = ProxyPolicySettings()
    d = evaluate_proxy_action(policy, media_type="video", width=3840, height=2160,
                              codec="h265 10bit")
    assert d.should_generate and d.reason == "long_gop_above_threshold"
    d = evaluate_proxy_action(policy, media_type="video", width=3840, height=2160,
                              codec="ProRes 422 HQ")
    assert d.should_generate and d.reason == "heavy_edit_above_threshold"
    d = evaluate_proxy_action(policy, media_type="video", width=3840, height=2160,
                              codec="DNxHR HQX")
    assert d.should_generate and d.reason == "heavy_edit_above_threshold"


def test_below_threshold_stays_skipped_regardless_of_codec():
    """1080p（默认画布内）任何 codec 都不生成；T-02 建议：2K 阈值放宽
    属产品调优决策（2K 实测 ≥142fps），见 default-tier 文档。"""
    policy = ProxyPolicySettings()
    for codec in ("h264", "prores", "topos", "hevc"):
        d = evaluate_proxy_action(policy, media_type="video", width=1920, height=1080,
                                  codec=codec)
        assert not d.should_generate, (codec, d.reason)


def test_unknown_dimensions_explicit_reason():
    """未探测尺寸显式 dimensions_unknown（不能误判 below_threshold）。"""
    policy = ProxyPolicySettings()
    d = evaluate_proxy_action(policy, media_type="video", width=0, height=0)
    assert not d.should_generate
    assert d.reason == "dimensions_unknown"
