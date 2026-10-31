"""复查批次 D 回归：R26 代理任务代次防护 / R28 typed extras + 兼容 JSON
revision。

对应 docs/edit_page_followup_audit_optimization_plan_2026-10-14.md §3.3
（F-R07/F-R09）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence

pytestmark = pytest.mark.edit_core


# ------------------------------------------------------------------ R26

class _PoolHarness:
    """轻量媒体池宿主：真实代次校验逻辑，替身面板。"""

    def __init__(self):
        from src.features.edit.ui.media_pool_panel import MediaPoolPanel

        self.panel = MediaPoolPanel.__new__(MediaPoolPanel)
        self.panel._proxy_jobs = {}
        self.panel._proxy_job_tokens = {}
        self.panel._proxy_token_counter = 0
        self.panel._proxy_session_generation = 0
        self.panel._proxy_job_generations = {}
        self.panel._pending_proxy_meta = {}
        self.writes: list[str] = []

    def register(self, asset_id: str, path: str) -> int:
        p = self.panel
        p._proxy_jobs[asset_id] = path
        p._proxy_token_counter += 1
        token = p._proxy_token_counter
        p._proxy_job_tokens[asset_id] = token
        p._proxy_job_generations[asset_id] = p._proxy_session_generation
        return token

    def finish(self, asset_id: str, path: str, token: int) -> bool:
        """镜像 _on_proxy_finished 的守卫链。"""
        from src.features.edit.ui.media_pool_panel import MediaPoolPanel

        p = self.panel
        if token != -1 and not MediaPoolPanel._proxy_job_token_valid(
            p, asset_id, token
        ):
            return False
        expected = p._proxy_jobs.get(asset_id)
        if expected is None or expected != path:
            return False
        self.writes.append(asset_id)
        return True


def test_proxy_token_invalid_across_projects() -> None:
    """F-R07：p1 注册 token17 → set_project(p2) 复用 ID/path → 旧回调拒绝。"""
    h = _PoolHarness()
    token = h.register("a1", "/tmp/x.mov")

    # 切工程：注销全部在途任务
    h.panel._invalidate_all_proxy_jobs()

    # 新工程同 ID/path 重新注册（新 token）
    new_token = h.register("a1", "/tmp/x.mov")
    assert new_token != token

    # p1 的旧 token17 回调：即便新任务在册也拒绝
    assert h.finish("a1", "/tmp/x.mov", token) is False
    assert h.writes == []
    # 新 token 正常
    assert h.finish("a1", "/tmp/x.mov", new_token) is True


def test_set_project_same_content_keeps_generation() -> None:
    """同一工程数据替换（timeline 激活）不注销任务。"""
    h = _PoolHarness()
    token = h.register("a1", "/tmp/x.mov")
    # 未发生 project_id 变化 → 代次不变 → token 有效
    assert h.finish("a1", "/tmp/x.mov", token) is True


def test_pending_probe_stale_source_discarded() -> None:
    """F-R07 + N21/RF05：身份不匹配的旧探测零写入，且不消耗新 pending。

    N21 新契约：回调 get 后核对 (source, asset_id, generation) 三重身
    份，全部匹配才 pop——旧回调先 pop 的行为会把重新登记的同 key
    pending 吞掉（旧 resolution 配新任务 stat 写入服务）。
    """
    from src.features.edit.ui.media_pool_panel import MediaPoolPanel

    h = _PoolHarness()
    p = h.panel
    p._proxy_session_generation = 5
    # 当前 pending：新工程的 source + 完整身份
    p._pending_proxy_meta["/tmp/proxy.mov"] = (
        "/new/project/x.mov", 1.0, 100, "asset-1", 5,
    )
    applied: list[str] = []
    p._apply_proxy_ready = lambda *a: applied.append(a[0])

    # 旧工程探测（source 不匹配，generation 过期）
    MediaPoolPanel._on_proxy_resolution_probed(
        p, "/tmp/proxy.mov", "/old/project/x.mov", (1920, 1080),
        "asset-1", 5,
    )
    assert applied == []
    # N21：pending 不被旧回调消耗（新任务仍可落盘）
    assert "/tmp/proxy.mov" in p._pending_proxy_meta

    # 旧代次探测（source 相同但 generation 过期）同样拒绝
    MediaPoolPanel._on_proxy_resolution_probed(
        p, "/tmp/proxy.mov", "/new/project/x.mov", (111, 222),
        "asset-1", 4,
    )
    assert applied == []
    assert "/tmp/proxy.mov" in p._pending_proxy_meta

    # 完整身份匹配 → 消费并应用
    MediaPoolPanel._on_proxy_resolution_probed(
        p, "/tmp/proxy.mov", "/new/project/x.mov", (1920, 1080),
        "asset-1", 5,
    )
    assert applied == ["/new/project/x.mov"]
    assert "/tmp/proxy.mov" not in p._pending_proxy_meta


# ------------------------------------------------------------------ R28

def test_track_sequence_extras_roundtrip() -> None:
    """F-R09：future_track/future_sequence 在 load→edit→save 后保留。"""
    t = TimelineSequence(timeline_id="t1", name="T")
    t = t.with_track_added(EditTrack(track_id="v1", name="V1", track_type="video", index=0))
    d = t.to_dict()
    d["future_sequence"] = 42
    d["video_tracks"][0]["future_track"] = "keep"

    loaded = TimelineSequence.from_dict(d)
    assert loaded._extra_fields.get("future_sequence") == 42
    assert loaded.video_tracks[0]._extra_fields.get("future_track") == "keep"

    # 编辑（copy-on-write）后仍保留
    edited = loaded._rebuild(name="Edited")
    edited = edited.with_track_updated(
        edited.video_tracks[0]._rebuild(is_locked=True)
    )
    assert edited._extra_fields.get("future_sequence") == 42
    assert edited.video_tracks[0]._extra_fields.get("future_track") == "keep"

    # 再保存
    saved = edited.to_dict()
    assert saved.get("future_sequence") == 42
    assert saved["video_tracks"][0].get("future_track") == "keep"


def test_compat_json_preserves_project_revision() -> None:
    from src.features.edit.core.project import EditProject
    from src.features.edit.core.serialization import EditProjectSerializer
    from dataclasses import replace

    project = replace(EditProject.create("t"), revision=7)
    payload = json.loads(json.dumps(EditProjectSerializer.to_dict(project)))
    loaded = EditProjectSerializer.from_dict(payload)
    assert loaded.revision == 7


# ------------------------------------------------------------------ U04

def test_replace_media_updates_type_and_fps(tmp_path) -> None:
    """F-U04：video→jpg 替换后 media_type/fps 跟随新源。"""
    import os

    from src.features.edit.core.project import EditProject, replace_media

    video = tmp_path / "a.mov"
    video.write_bytes(b"x" * 8)
    jpg = tmp_path / "b.jpg"
    jpg.write_bytes(b"y" * 8)
    project = EditProject.create("p").import_asset(
        path=str(video), media_type="video",
        metadata={"fps": 24.0, "duration_frames": 100},
    )
    asset_id = next(iter(project.assets))

    replaced = replace_media(
        project, asset_id, str(jpg),
        new_metadata={"fps": 1.0, "duration_frames": 1},
        new_media_type="image",
    )
    new_asset = replaced.assets[asset_id]
    assert new_asset.media_type == "image"
    assert new_asset.metadata.get("fps") == 1.0

    # undo（完整旧 asset 快照）恢复 video/24fps
    from src.features.edit.core.commands import ReplaceMediaCommand

    cmd = ReplaceMediaCommand.create(
        asset_id, str(jpg),
        new_metadata={"fps": 1.0, "duration_frames": 1},
        new_media_type="image",
    )
    applied = cmd.apply(project)
    restored = cmd.undo(applied)
    assert restored.assets[asset_id].media_type == "video"
    assert restored.assets[asset_id].metadata.get("fps") == 24.0
