"""Regression tests for remediation blocker batch wd13-blk.

Covers the 8 items of the re-dispatched blocker/registry batch:

- AUD-17108  DuplicateAssetCommand records and rolls back the shared scene
             store registration created by ``EditProject.duplicate_asset``
             (apply 注册 / undo 反注册对称)
- AUD-16906  mixer preset library: per-entry parsing + atomic writes; a
             corrupted library is never overwritten (no permanent wipe)
- AUD-16911  build_nested_from_selection: layered host-track placement; a
             dirty host no longer crashes with an internal-UUID error
- AUD-26910  strip_markdown_fence only strips a fence at the very end —
             teaching pages with body code fences are not truncated
- AUD-20203  DiskProxyService public cache-write API (mark_ready) and the
             media-pool panel consuming it (no private lock/cache access)
- AUD-19500  host-side wiring: teaching_animation module subscribes to
             ``motion_graphic_template_requested`` and sets use_template
- AUD-16900  CPU iris circle/ellipse radius matches the GPU adaptive formula
             (sqrt(aspect^2+1) / 0.62 compensation) — no endpoint jump
- AUD-R2-003 INVALID guard: the port-count reconnect predictor was removed
             by AUD-10602; measured diff covers second-image-input nodes
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PyQt6.QtCore import QObject, pyqtSignal

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.commands import DuplicateAssetCommand
from src.features.edit.core.mixer_preset import (
    ChannelStripPreset,
    add_preset,
    load_presets,
    remove_preset,
    save_presets,
)
from src.features.edit.core.nested_sequence import build_nested_from_selection
from src.features.edit.core.project import EditProject
from src.features.edit.core.transition_mask import compute_transition_mask
from src.features.teaching_animation.ai.html_llm_generator import strip_markdown_fence
from src.features.teaching_animation.core.model import TeachingSceneDocument
from src.features.teaching_animation.scene_store import get_shared_scene_store


# ═══════════════════════════ AUD-17108 ═══════════════════════════


def _motion_graphic_project() -> tuple[EditProject, str, TeachingSceneDocument]:
    """Create a project with one registered Motion Graphic asset."""
    scene = TeachingSceneDocument.create_default("Motion Graphic Clip")
    store = get_shared_scene_store()
    store.register(scene)
    project, asset_id = EditProject.create("MG").create_motion_graphic_asset(scene)
    return project, asset_id, scene


class TestDuplicateAssetCommandSceneRollback:

    def test_apply_records_registered_scene_and_undo_unregisters(self, qapp):
        project, asset_id, scene = _motion_graphic_project()
        store = get_shared_scene_store()
        try:
            cmd = DuplicateAssetCommand.create(asset_id, project.root_bin_id)
            project = cmd.apply(project)

            new_id = cmd.created_asset_id
            assert new_id is not None
            copied_scene_id = project.assets[new_id].metadata["scene_id"]
            # apply 期间注册的 scene 被命令记录（注册半边）。
            assert cmd.created_scene_id == copied_scene_id
            assert store.get(copied_scene_id) is not None

            project = cmd.undo(project)
            # undo 反注册半边：store 中不再有副本场景，素材亦被移除。
            assert new_id not in project.assets
            assert store.get(copied_scene_id) is None
            # 原场景不受影响。
            assert store.get(scene.scene_id) is not None
        finally:
            store.remove(scene.scene_id)

    def test_undo_unregisters_scene_even_if_asset_already_gone(self, qapp):
        project, asset_id, scene = _motion_graphic_project()
        store = get_shared_scene_store()
        try:
            cmd = DuplicateAssetCommand.create(asset_id, project.root_bin_id)
            project = cmd.apply(project)
            copied_scene_id = cmd.created_scene_id
            assert copied_scene_id is not None

            # 素材被其它路径先移除：undo 仍须回收 apply 的场景注册。
            project = project.remove_asset(cmd.created_asset_id)
            project = cmd.undo(project)
            assert store.get(copied_scene_id) is None
        finally:
            store.remove(scene.scene_id)

    def test_plain_asset_records_no_scene(self, qapp):
        project = EditProject.create("P")
        project = project.import_asset(
            path="/tmp/plain.mov", media_type="video", metadata={},
            display_name="plain.mov",
        )
        asset_id = next(iter(project.assets))
        cmd = DuplicateAssetCommand.create(asset_id, project.root_bin_id)
        project = cmd.apply(project)
        assert cmd.created_scene_id is None
        project = cmd.undo(project)
        assert cmd.created_asset_id not in project.assets

    def test_fallback_branch_without_scene_registration(self, qapp):
        # store 中没有源场景（如重开后未恢复）→ duplicate 走空场景回退
        # 分支，不得记录 scene（undo 也不应误删 store 中无关条目）。
        project, asset_id, scene = _motion_graphic_project()
        store = get_shared_scene_store()
        store.remove(scene.scene_id)  # 模拟场景未随工程恢复
        try:
            cmd = DuplicateAssetCommand.create(asset_id, project.root_bin_id)
            project = cmd.apply(project)
            assert cmd.created_scene_id is None
            project = cmd.undo(project)
            assert cmd.created_asset_id not in project.assets
        finally:
            store.remove(scene.scene_id)

    def test_dict_roundtrip_carries_scene_id(self):
        cmd = DuplicateAssetCommand(
            command_id="cid", label="L", source_asset_id="src",
            target_bin_id="bin", created_asset_id="aid",
            created_scene_id="scene_abc",
        )
        data = cmd.to_dict()
        assert data["created_scene_id"] == "scene_abc"
        restored = DuplicateAssetCommand.from_dict(data)
        assert restored.created_scene_id == "scene_abc"


# ═══════════════════════════ AUD-16906 ═══════════════════════════


@pytest.fixture()
def preset_file(tmp_path, monkeypatch):
    """Point the mixer preset library at a temp file; return its path."""
    from src.features.edit.core import mixer_preset

    path = tmp_path / "mixer_presets.json"
    monkeypatch.setattr(mixer_preset, "_presets_path", lambda: str(path))
    return path


class TestMixerPresetLibraryResilience:

    def test_load_skips_single_corrupted_entry(self, preset_file):
        good_1 = ChannelStripPreset(preset_id="p1", name="One", volume=0.8)
        good_2 = ChannelStripPreset(preset_id="p2", name="Two", pan=-0.3)
        bad_volume = {"preset_id": "bad", "name": "Bad", "volume": {"o": 1}}
        preset_file.write_text(json.dumps({
            "presets": [good_1.to_dict(), bad_volume, "not-a-dict", good_2.to_dict()],
        }), encoding="utf-8")

        loaded = load_presets()

        # 单条损坏只被跳过，不再拖垮整个预设库。
        assert [p.preset_id for p in loaded] == ["p1", "p2"]

    def test_add_preset_does_not_wipe_unreadable_library(self, preset_file):
        preset_file.write_text("{ this is not json", encoding="utf-8")
        before = preset_file.read_bytes()

        result = add_preset(ChannelStripPreset(preset_id="new", name="New"))

        # 返回内存视图（与旧行为一致），但盘上损坏的库不被覆写清空。
        assert [p.preset_id for p in result] == ["new"]
        assert preset_file.read_bytes() == before

    def test_remove_preset_does_not_wipe_unreadable_library(self, preset_file):
        preset_file.write_text('{"presets": "not-a-list"}', encoding="utf-8")
        before = preset_file.read_bytes()

        result = remove_preset("whatever")

        assert result == []
        assert preset_file.read_bytes() == before

    def test_save_presets_atomic_on_serialization_failure(self, preset_file):
        preset_file.write_text('{"presets": []}', encoding="utf-8")
        before = preset_file.read_bytes()
        # sends 里的裸对象没有 to_dict，json 序列化必失败。
        poisoned = ChannelStripPreset(preset_id="p", name="P", sends=(object(),))

        assert save_presets([poisoned]) is False

        # 原文件保持不变，且不残留 .tmp 半成品。
        assert preset_file.read_bytes() == before
        leftovers = [
            p.name for p in preset_file.parent.iterdir()
            if p.name.startswith("mixer_presets.json.tmp")
        ]
        assert leftovers == []

    def test_add_then_load_roundtrip(self, preset_file):
        add_preset(ChannelStripPreset(preset_id="r1", name="Round", volume=1.2))
        loaded = load_presets()
        assert [p.preset_id for p in loaded] == ["r1"]
        assert loaded[0].volume == 1.2


# ═══════════════════════════ AUD-16911 ═══════════════════════════


def _nclip(clip_id: str, track_id: str, start: int, duration: int = 50):
    from src.features.edit.core.timeline_v2 import ClipInstance

    return ClipInstance(
        clip_id=clip_id, asset_id=f"a_{clip_id}", track_id=track_id,
        timeline_start=start, timeline_duration=duration,
        source_start=0, source_duration=600,
    )


def _timeline_2v(v1_clips, v2_clips):
    from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence
    from src.shared.media.metadata import Rational

    v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                   clips=tuple(v1_clips))
    v2 = EditTrack(track_id="v2", name="V2", track_type="video", index=1,
                   clips=tuple(v2_clips))
    a1 = EditTrack(track_id="a1", name="A1", track_type="audio", index=0, clips=())
    return TimelineSequence(
        timeline_id="t2v", name="Main", fps=Rational(24, 1),
        video_tracks=(v1, v2), audio_tracks=(a1,),
    )


class TestNestedSelectionHostTrackPlacement:

    def test_dirty_first_video_track_falls_through_to_clean_track(self):
        # v1 非连续选择（gap 内有未选片段）→ v1 脏；v2 干净。
        # 旧代码在 v1 上 with_clip_added 必抛内部 UUID 重叠错误。
        tl = _timeline_2v(
            v1_clips=[
                _nclip("a1", "v1", 0, 20),    # selected
                _nclip("gap", "v1", 20, 20),  # unselected → v1 dirty
                _nclip("a2", "v1", 40, 20),   # selected
            ],
            v2_clips=[
                _nclip("b1", "v2", 0, 30),    # selected → v2 clean
            ],
        )

        txn = build_nested_from_selection(tl, ("a1", "a2", "b1"), name="Child")

        # 分层放置：宿主顺延到干净的 v2，而不是抛 ValueError。
        assert txn.host_track_id == "v2"
        v2_clips = txn.new_timeline.get_track_by_id("v2").clips
        assert [c.clip_id for c in v2_clips] == [txn.nested_clip_id]
        nested = v2_clips[0]
        assert nested.timeline_start == 0
        assert nested.timeline_duration == 60  # span = max_end - min_start
        # v1：选择片段移除、未选片段保留。
        assert [c.clip_id for c in txn.new_timeline.get_track_by_id("v1").clips] == ["gap"]

    def test_all_candidates_dirty_raises_user_semantic_error(self):
        tl = _timeline_2v(
            v1_clips=[
                _nclip("c1", "v1", 0, 10),
                _nclip("mid", "v1", 10, 40),  # unselected, inside span
                _nclip("c3", "v1", 50, 10),
            ],
            v2_clips=[],
        )

        with pytest.raises(ValueError) as excinfo:
            build_nested_from_selection(tl, ("c1", "c3"))

        message = str(excinfo.value)
        # 带用户语义、带帧号；不再泄漏内部 UUID / "Clip overlaps with"。
        assert "Cannot nest selection" in message
        assert "frames 0 and 60" in message
        assert "mid" not in message
        assert "Clip overlaps" not in message

    def test_clean_single_track_selection_unchanged(self):
        tl = _timeline_2v(
            v1_clips=[_nclip("x1", "v1", 0), _nclip("x2", "v1", 50)],
            v2_clips=[],
        )
        txn = build_nested_from_selection(tl, ("x1", "x2"))
        assert txn.host_track_id == "v1"
        parent = txn.new_timeline.get_track_by_id("v1").clips
        assert [c.clip_id for c in parent] == [txn.nested_clip_id]
        assert parent[0].timeline_duration == 100


# ═══════════════════════════ AUD-26910 ═══════════════════════════


class TestStripMarkdownFenceBodyFences:

    def test_body_code_fence_is_not_truncated(self):
        doc = (
            "<!doctype html>\n<html>\n<body>\n"
            "<pre>```python\nprint(1)\n```</pre>\n"
            "<p>tail content</p>\n"
            "</body>\n</html>"
        )
        # 旧 rfind("```") 会从 <pre> 内的闭合围栏处把文档静默腰斩。
        assert strip_markdown_fence(doc) == doc

    def test_preamble_and_trailing_outer_fence_stripped(self):
        raw = (
            "Here is your page:\n```html\n"
            "<!doctype html>\n<html></html>\n"
            "```\n"
        )
        assert strip_markdown_fence(raw) == "<!doctype html>\n<html></html>"

    def test_body_fence_survives_outer_fence_removal(self):
        inner = (
            "<!doctype html>\n<html>\n<body>\n"
            "<pre>```js\ncode()\n```</pre>\n"
            "</body>\n</html>"
        )
        raw = f"```html\n{inner}\n```\n"
        assert strip_markdown_fence(raw) == inner

    def test_plain_document_untouched_and_empty_safe(self):
        doc = "<!doctype html>\n<html><body>教学页面正文</body></html>"
        assert strip_markdown_fence(doc) == doc
        assert strip_markdown_fence("") == ""
        assert strip_markdown_fence(None) == ""


# ═══════════════════════════ AUD-20203 ═══════════════════════════


class TestDiskProxyMarkReadyApi:

    def test_mark_ready_publishes_cache_and_dispatches_callbacks(self):
        from src.shared.media.disk_proxy_service import DiskProxyService
        from src.shared.media.proxy import DiskProxyInfo, ProxyStatus

        service = DiskProxyService()
        dispatched: list = []
        service.register_status_callback(lambda sp, info: dispatched.append((sp, info)))

        info = DiskProxyInfo(
            status=ProxyStatus.READY,
            proxy_path="/cache/proxy.mov",
            proxy_resolution=(960, 540),
            source_mtime=1.5,
            source_size=42,
        )
        service.mark_ready("/src/master.mov", info)

        assert service.get_proxy_status("/src/master.mov") is ProxyStatus.READY
        assert service.get_disk_proxy_info("/src/master.mov").proxy_path == "/cache/proxy.mov"
        assert dispatched == [("/src/master.mov", info)]

    def test_media_pool_panel_uses_public_disk_api_only(self, qapp, monkeypatch):
        # fake disk 服务只暴露 mark_ready（无 _lock/_info_cache 属性）：
        # 旧的私有锁直写路径若仍存在会 AttributeError 被吞、mark_ready
        # 不会被调用。
        from src.features.edit.ui import media_pool_panel as mpp
        from src.shared.media.proxy import ProxyStatus

        marked: list = []
        frame_updated: list = []

        class _DiskService:
            def mark_ready(self, source_path, info):
                marked.append((source_path, info))

        class _FrameService:
            def get_shared_clips_info(self):
                return {"/src.mov": ["clip-1"]}

            def get_clip_session(self, clip_id):
                assert clip_id == "clip-1"
                return SimpleNamespace(is_open=True)

            def update_disk_proxy_info(self, clip_id, info):
                frame_updated.append((clip_id, info))

        monkeypatch.setattr(mpp, "get_disk_proxy_service", lambda: _DiskService())
        monkeypatch.setattr(mpp, "get_frame_provider_service", lambda: _FrameService())

        # AUD-20210：探测移入 QThreadPool worker，服务回填经 probed 信号
        # 在 UI 线程回调完成——桩以同步 worker 复现整条链路
        shell = SimpleNamespace(
            _proxy_resolution_signals=mpp.ProxyResolutionSignals(),
            _pending_proxy_meta={},
        )
        shell._on_proxy_resolution_probed = (
            mpp.MediaPoolPanel._on_proxy_resolution_probed.__get__(shell)
        )
        shell._apply_proxy_ready = (
            mpp.MediaPoolPanel._apply_proxy_ready.__get__(shell)
        )
        def _on_probed(proxy_path, source_path, resolution, asset_id, generation, request_id):
            # SimpleNamespace 不可弱引用，经普通函数闭包转发给绑定方法
            # N21：转发完整身份载荷（asset_id/generation）
            shell._on_proxy_resolution_probed(
                proxy_path, source_path, resolution, asset_id, generation, request_id)
        shell._proxy_resolution_signals.probed.connect(_on_probed)

        class _FakeWorker:
            def __init__(self, proxy_path, source_path, asset, signals=None,
                 asset_id="", session_generation=-1, request_id=""):
                self._proxy_path = proxy_path
                self._source_path = source_path
                self._signals = signals
                self._asset_id = asset_id
                self._gen = session_generation
                self._request_id = request_id

            def run(self) -> None:
                # N21：信号载荷含 asset_id/generation（替身传空/当前代次）
                self._signals.probed.emit(
                    self._proxy_path, self._source_path, (1280, 720),
                    self._asset_id, self._gen, self._request_id)

        monkeypatch.setattr(mpp, "ProxyResolutionWorker", _FakeWorker)
        started: list = []
        monkeypatch.setattr(
            mpp, "QThreadPool",
            SimpleNamespace(globalInstance=lambda: SimpleNamespace(
                start=started.append)))

        shell._update_disk_proxy_services = (
            mpp.MediaPoolPanel._update_disk_proxy_services.__get__(shell)
        )

        shell._update_disk_proxy_services(SimpleNamespace(path="/src.mov"), "/proxy.mov")
        # 真实 worker 在池线程异步执行，此时 _pending_proxy_meta 已暂存；
        # 调用返回后再驱动 worker 即可复现该时序
        for worker in started:
            worker.run()

        assert len(marked) == 1
        source_path, info = marked[0]
        assert source_path == "/src.mov"
        assert info.proxy_path == "/proxy.mov"
        assert info.status == ProxyStatus.READY
        # FrameProviderService 半侧（wd12-g66 已修）不回归。
        assert len(frame_updated) == 1
        assert frame_updated[0][0] == "clip-1"


# ═══════════════════════════ AUD-19500 ═══════════════════════════


class _FakeAssistantPanel(QObject):
    motion_graphic_confirm_requested = pyqtSignal(str)
    motion_graphic_cancel_requested = pyqtSignal(str)
    motion_graphic_proposal_selected = pyqtSignal(str, str)
    motion_graphic_template_requested = pyqtSignal(str)


class TestMotionGraphicTemplateWiring:

    def test_attach_subscribes_template_signal(self, qapp):
        from src.features.teaching_animation.module import TeachingAnimationModule

        module = TeachingAnimationModule()
        panel = _FakeAssistantPanel()
        window = SimpleNamespace(ai_edit_assistant_panel=panel)

        module.attach_main_window(window)

        # AUD-19500：宿主接线后，面板的 receivers 检测放行专用信号路径。
        assert panel.receivers(panel.motion_graphic_template_requested) == 1

    def test_template_request_sets_use_template_and_confirms(self, qapp, monkeypatch):
        from src.features.teaching_animation.module import TeachingAnimationModule

        module = TeachingAnimationModule()
        panel = _FakeAssistantPanel()
        window = SimpleNamespace(ai_edit_assistant_panel=panel)
        module.attach_main_window(window)

        # 失败卡之后的 plan：无 scene（MG-05 守卫平时会拒绝裸确认）。
        plan = {"phase": "build_failed", "seconds": 6.0, "fps": 25.0}
        module._pending_motion_graphic_requests["req-1"] = plan

        confirmed: list = []
        monkeypatch.setattr(
            module, "_confirm_motion_graphic_plan",
            lambda mw, rid: confirmed.append((mw, rid)) or True,
        )

        panel.motion_graphic_template_requested.emit("req-1")

        assert plan.get("use_template") is True
        assert confirmed == [(window, "req-1")]

    def test_template_request_unknown_id_still_delegates(self, monkeypatch):
        from src.features.teaching_animation.module import TeachingAnimationModule

        module = TeachingAnimationModule()
        confirmed: list = []
        monkeypatch.setattr(
            module, "_confirm_motion_graphic_plan",
            lambda mw, rid: confirmed.append(rid) or False,
        )

        assert module._request_motion_graphic_template(object(), "missing-req") is False
        assert confirmed == ["missing-req"]


# ═══════════════════════════ AUD-16900 ═══════════════════════════


def _gpu_iris_mirror(width: int, height: int, progress: float, kind: str) -> np.ndarray:
    """GPU（gpu_transition_shaders.procedural_mask）iris 公式的 numpy 镜像。"""
    yy, xx = np.mgrid[0:height, 0:width]
    x = (xx.astype(np.float32) + 0.5) / max(1.0, float(width)) * 2.0 - 1.0
    y = (yy.astype(np.float32) + 0.5) / max(1.0, float(height)) * 2.0 - 1.0
    aspect = float(width) / max(1.0, float(height))
    x = x * aspect
    if kind == "circle":
        radius = progress * math.sqrt(aspect * aspect + 1.0)
        inside = (x * x + y * y) <= radius * radius
    else:
        scale = progress * math.sqrt(aspect * aspect + 1.0 / (0.62 * 0.62))
        inside = (
            (x / max(scale, 1e-6)) ** 2 + (y / max(scale * 0.62, 1e-6)) ** 2
        ) <= 1.0
    return inside.astype(np.float32)


class TestIrisCpuGpuAspectAlignment:

    @pytest.mark.parametrize("width,height", [(40, 40), (64, 36), (160, 90), (239, 100)])
    @pytest.mark.parametrize("progress", [0.2, 0.55, 0.9])
    def test_iris_circle_matches_gpu_formula(self, width, height, progress):
        cpu = compute_transition_mask(width, height, progress, "iris_circle")
        mirror = _gpu_iris_mirror(width, height, progress, "circle")
        np.testing.assert_array_equal(cpu, mirror)

    @pytest.mark.parametrize("width,height", [(40, 40), (64, 36), (160, 90), (239, 100)])
    @pytest.mark.parametrize("progress", [0.2, 0.55, 0.9])
    def test_iris_ellipse_matches_gpu_formula(self, width, height, progress):
        cpu = compute_transition_mask(width, height, progress, "iris_ellipse")
        mirror = _gpu_iris_mirror(width, height, progress, "ellipse")
        np.testing.assert_array_equal(cpu, mirror)

    def test_widescreen_edge_midpoints_covered_before_endpoint(self):
        # 16:9 @ progress=0.9：旧常数 1.55*0.9=1.395 盖不住 |x|=1.778 的
        # 左右边缘中点（终点处全帧跳变的根因）；自适应公式 0.9*sqrt(a^2+1)
        # ≈1.839 > 1.778 已覆盖。
        width, height = 160, 90
        mask = compute_transition_mask(width, height, 0.9, "iris_circle")
        assert mask[height // 2, 0] == 1.0
        assert mask[height // 2, width - 1] == 1.0

    def test_widescreen_ellipse_top_bottom_covered_before_endpoint(self):
        # 椭圆 0.62 补偿：16:9 @ progress=0.75 时 scale*0.62>1，上下边缘
        # 中点已覆盖（旧公式 1.55*0.75*0.62≈0.72 < 1 未覆盖）。
        width, height = 160, 90
        mask = compute_transition_mask(width, height, 0.75, "iris_ellipse")
        assert mask[0, width // 2] == 1.0
        assert mask[height - 1, width // 2] == 1.0

    def test_endpoints_still_clamped(self):
        zeros = compute_transition_mask(64, 36, 0.0, "iris_circle")
        ones = compute_transition_mask(64, 36, 1.0, "iris_circle")
        assert zeros.max() == 0.0
        assert ones.min() == 1.0


# ═══════════════════════════ AUD-R2-003 ═══════════════════════════


class TestReconnectHeuristicSupersededGuard:
    """AUD-R2-003 INVALID 守护：端口数启发式已被 AUD-10602 的测量式
    快照/差分取代；含第二 image 输入口的节点（Blur velocity 口）的
    重连链路现在被记录并在 undo 中显式拆除。"""

    def test_port_count_predictor_is_gone(self):
        from src.features.composite.ui.node_graph_commands import DeleteNodeCommand

        assert not hasattr(DeleteNodeCommand, "_calculate_reconnection_links")

    def test_blur_second_image_input_reconnect_recorded_and_undone(self, qapp):
        from src.features.composite.core.node_engine import NodeGraph
        from src.features.composite.nodes.blur_node import BlurNode
        from src.features.composite.nodes.constant_node import ConstantNode
        from src.features.composite.nodes.output_node import OutputNode
        from src.features.composite.ui.node_graph_commands import DeleteNodeCommand

        graph = NodeGraph()
        graph.add_node(ConstantNode(node_id="source"))
        # Blur 有第二个 image 输入口（velocity），只连接主 image 口。
        graph.add_node(BlurNode(node_id="blur"))
        graph.add_node(OutputNode(node_id="output"))
        assert graph.connect("source", "result", "blur", "image")
        assert graph.connect("blur", "result", "output", "image")

        def _quads() -> set:
            return {
                (c.src_node_id, c.src_port_name, c.dst_node_id, c.dst_port_name)
                for c in graph.connections
            }

        original = _quads()

        cmd = DeleteNodeCommand(graph=graph, node_ids=["blur"])
        cmd.redo()

        # 引擎桥接了链路，且被测量式记录（不再依赖 connect 单输入替换
        # 的巧合）。
        assert graph.get_node("blur") is None
        assert ("source", "result", "output", "image") in _quads()
        assert cmd._reconnected_links == [{
            "from_node": "source", "from_port": "result",
            "to_node": "output", "to_port": "image",
        }]

        cmd.undo()
        assert _quads() == original
        assert graph.get_node("blur") is not None
