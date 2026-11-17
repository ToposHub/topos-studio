"""G1：format_factory 接管——G 节专门退出验收。

行验收：**输入快照、输出计划/冲突、进度/取消/失败恢复；不走旧 /launch**。
新入口/服务/关闭路径逐项锁定；单输入与多选政策明确。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from src.features.files.format_factory_service import (
    FormatFactoryService,
    InputSnapshot,
    NativeToolCommandService,
    PRESETS,
)
from src.shared.plugins.dispatch import DispatchAuthority
from src.shared.plugins.services import CommandRejected

FFMPEG = shutil.which("ffmpeg")


def _media(root: Path, name="clip.mov", size_bytes=4096) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_bytes(os.urandom(size_bytes))
    return path


def _real_clip(root: Path, name="clip.mov") -> Path:
    subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc2=size=128x72:duration=0.4:rate=10",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(root / name)],
        check=True, cwd=root)
    return root / name


# ---------------------------------------------------------------------------
# 输入快照
# ---------------------------------------------------------------------------

def test_input_snapshot_detects_drift(tmp_path):
    media = _media(tmp_path)
    snapshot = InputSnapshot.capture(str(media))
    assert snapshot.matches_disk()
    os.utime(media, ns=(snapshot.mtime_ns + 1_000_000,) * 2)
    assert not snapshot.matches_disk()          # mtime 漂移即失配
    snapshot2 = InputSnapshot.capture(str(media))
    assert snapshot2.matches_disk()
    media.unlink()
    assert not snapshot2.matches_disk()         # 消失即失配


# ---------------------------------------------------------------------------
# 输出计划 / 冲突（政策明确：rename/skip/overwrite）
# ---------------------------------------------------------------------------

def test_build_plan_conflict_rename_autodisambiguates(tmp_path):
    media = _media(tmp_path, "clip.mov")
    existing = tmp_path / "clip.mp4"
    existing.write_bytes(b"old")
    service = FormatFactoryService(ffmpeg=FFMPEG or "ffmpeg")
    plan = service.build_plan([str(media)], "copy-mp4", conflict_policy="rename")
    item = plan[0]
    assert item.output_path.endswith("clip-1.mp4")   # 冲突→改名（可见）
    assert "resolved" in item.conflict
    # 二次占位再冲突：-2
    (tmp_path / "clip-1.mp4").write_bytes(b"x")
    plan2 = service.build_plan([str(media)], "copy-mp4", conflict_policy="rename")
    assert plan2[0].output_path.endswith("clip-2.mp4")


def test_build_plan_conflict_skip_and_overwrite_keep_target(tmp_path):
    media = _media(tmp_path, "clip.mov")
    (tmp_path / "clip.mp4").write_bytes(b"old")
    service = FormatFactoryService(ffmpeg=FFMPEG or "ffmpeg")
    skip = service.build_plan([str(media)], "copy-mp4", conflict_policy="skip")
    assert skip[0].conflict == "exists" and skip[0].output_path.endswith("clip.mp4")
    over = service.build_plan([str(media)], "copy-mp4", conflict_policy="overwrite")
    assert over[0].conflict == "exists"          # 政策显式覆盖（不静默）


def test_build_plan_multi_input_and_policies_validated(tmp_path):
    inputs = [str(_media(tmp_path, f"c{index}.mov")) for index in range(3)]
    service = FormatFactoryService(ffmpeg=FFMPEG or "ffmpeg")
    plan = service.build_plan(inputs, "h264-mp4")
    assert len(plan) == 3
    assert {Path(item.output_path).suffix for item in plan} == {".mp4"}
    with pytest.raises(ValueError):
        service.build_plan(inputs, "bogus-preset")
    with pytest.raises(ValueError):
        service.build_plan(inputs, "h264-mp4", conflict_policy="silent")


# ---------------------------------------------------------------------------
# 执行：进度/取消/失败恢复/原子交付（真 ffmpeg）
# ---------------------------------------------------------------------------

@pytest.mark.skipif(FFMPEG is None, reason="本机无 ffmpeg CLI")
def test_execute_delivers_atomically_and_reports(tmp_path):
    clip = _real_clip(tmp_path)
    service = FormatFactoryService(ffmpeg=FFMPEG)
    plan = service.build_plan([str(clip)], "copy-mp4")
    results: list[tuple[bool, str]] = []
    job_id = service.execute_item(plan[0], "copy-mp4", on_done=lambda ok, msg: results.append((ok, msg)))
    deadline = __import__("time").monotonic() + 15
    while not results and __import__("time").monotonic() < deadline:
        __import__("time").sleep(0.05)
    assert results and results[0][0] is True
    assert results[0][1] == plan[0].output_path
    out = Path(plan[0].output_path)
    assert out.is_file() and not Path(str(out) + ".part").exists()
    # 内容确为可用封装（ffprobe 头可读）
    probe = subprocess.run(
        [FFMPEG, "-loglevel", "error", "-i", str(out), "-f", "null", "-"],
        capture_output=True)
    assert probe.returncode == 0
    assert job_id not in service.jobs             # 完成即出账


@pytest.mark.skipif(FFMPEG is None, reason="本机无 ffmpeg CLI")
def test_cancel_terminates_and_cleans(tmp_path):
    subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc2=size=128x72:duration=6:rate=25",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         str(tmp_path / "long.mov")], check=True, cwd=tmp_path)
    clip = tmp_path / "long.mov"
    service = FormatFactoryService(ffmpeg=FFMPEG, item_deadline_sec=120)
    plan = service.build_plan([str(clip)], "h264-mp4")
    # 拉长转码时间：超慢预设（libx264 placebo + 大放大）
    from src.features.files import format_factory_service as ffs
    ffs.PRESETS["h264-mp4"] = dict(
        ffs.PRESETS["h264-mp4"],
        args=["-c:v", "libx264", "-preset", "placebo", "-crf", "18",
              "-vf", "scale=iw*8:ih*8", "-c:a", "aac"])
    try:
        results: list[tuple[bool, str]] = []
        service.execute_item(plan[0], "h264-mp4",
                             on_done=lambda ok, msg: results.append((ok, msg)))
        import time as _t
        _t.sleep(0.5)
        assert not results                          # 在途
        service.cancel_all()
        deadline = _t.monotonic() + 10
        while not results and _t.monotonic() < deadline:
            _t.sleep(0.05)
        assert results and results[0][0] is False
        assert "取消" in results[0][1]
        assert not Path(plan[0].output_path).exists()
        assert not Path(plan[0].output_path + ".part").exists()
    finally:
        ffs.PRESETS["h264-mp4"] = dict(  # 还原共享预设表
            args=["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                  "-c:a", "aac", "-b:a", "160k"],
            title=ffs.PRESETS["h264-mp4"]["title"], suffix=".mp4")


@pytest.mark.skipif(FFMPEG is None, reason="本机无 ffmpeg CLI")
def test_retry_failed_item_after_recovery(tmp_path):
    """失败恢复：skip 政策撞已存在输出 → 失败项；重试换 rename 政策成功。"""
    clip = _real_clip(tmp_path, "flaky.mov")
    service = FormatFactoryService(ffmpeg=FFMPEG)
    (tmp_path / "flaky.mp4").write_bytes(b"occupied")   # 预占输出
    plan = service.build_plan([str(clip)], "copy-mp4",
                              conflict_policy="skip")
    assert plan[0].conflict == "exists"
    with pytest.raises(FileExistsError):                 # skip 政策：跳过即失败
        service.execute_item(plan[0], "copy-mp4", conflict_policy="skip")
    # 失败恢复：同一计划项换 rename 政策重试（快照不变，目标改名）
    results: list[tuple[bool, str]] = []
    service.retry_item(plan[0], "copy-mp4", conflict_policy="rename",
                       on_done=lambda ok, msg: results.append((ok, msg)))
    import time as _t
    deadline = _t.monotonic() + 15
    while not results and _t.monotonic() < deadline:
        _t.sleep(0.05)
    assert results and results[0][0] is True
    delivered = Path(results[0][1])
    assert delivered.name == "flaky-1.mp4"               # rename 政策生效
    assert delivered.is_file()
    assert (tmp_path / "flaky.mp4").read_bytes() == b"occupied"  # 预占未被覆盖


# ---------------------------------------------------------------------------
# 原生命令服务：三段 + 原子边界（不走旧 /launch）
# ---------------------------------------------------------------------------

def test_native_command_prepare_revalidate_dispatch():
    revisions = {"value": 7}
    service = NativeToolCommandService(
        command_id="files.format_factory.open",
        plugin_id="files.format_factory",
        revision_provider=lambda: revisions["value"])
    prepared = service.prepare(["/m/a.mov", "/m/b.mov"])
    assert dict(prepared.payload) == {"paths": ("/m/a.mov", "/m/b.mov")}
    opened: list[tuple] = []
    outcome = service.dispatch(service.revalidate(prepared),
                               lambda paths: opened.append(paths))
    assert outcome.accepted and opened == [("/m/a.mov", "/m/b.mov")]


def test_native_command_rejects_revision_drift():
    revisions = {"value": 7}
    service = NativeToolCommandService(
        command_id="files.format_factory.open",
        plugin_id="files.format_factory",
        revision_provider=lambda: revisions["value"])
    prepared = service.prepare(["/m/a.mov"])
    revisions["value"] = 9
    with pytest.raises(CommandRejected) as exc:
        service.revalidate(prepared)
    assert exc.value.error_code == "LIBRARY_REVISED"


def test_native_command_epoch_mutation_blocks_dispatch():
    service = NativeToolCommandService(
        command_id="files.format_factory.open",
        plugin_id="files.format_factory")
    prepared = service.prepare(["/m/a.mov"])
    service.authority.invalidate("library-switch")   # 停用/切库经边界
    opened: list[tuple] = []
    outcome = service.dispatch(prepared, lambda paths: opened.append(paths))
    assert not outcome.accepted
    assert outcome.error_code == "AUTHORITY_EPOCH_MOVED"
    assert opened == []                              # 过期调用零打开


def test_native_command_identity_mismatch_rejected():
    service = NativeToolCommandService(
        command_id="files.format_factory.open",
        plugin_id="files.format_factory")
    other = NativeToolCommandService(
        command_id="files.other.open", plugin_id="files.other")
    with pytest.raises(CommandRejected):
        service.revalidate(other.prepare(["/m/a.mov"]))
