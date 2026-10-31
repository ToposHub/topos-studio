"""真实素材端到端验证驱动：批次 D/E/F（多机位 / 嵌套音频 / 内容迁移）。

用 tests/test_videos 的真实视频 + ffmpeg 真实副本跑完整生产链路：

  D. 多机位波形同步 —— 原片为参考机位，ffmpeg 重编码出错开 0.5s / 1.25s
     的真实副本模拟 B/C 机位；真实 MulticamSyncWorker 解码互相关 → 校验
     偏移/置信度 → 组写回 → 切换命令语义 → 角度网格真实取帧。
  E. 嵌套音频真实导出 —— 子序列（含真实音频轨）嵌套进无音频轨的父序列，
     TimelineExporter + FFmpegVideoEncoder 写出真实 MP4；输出里存在非静音
     音频且与源素材对应区段互相关峰值≈0 ⇒ 音频确实来自嵌套子序列。
  F. 内容迁移真实工程 —— 用仓库里真实保存的 2K_h264.topos：降级 manifest
     为 v1 + 注入 legacy 内容 → EditProjectIO.load_project 触发迁移 →
     save_project 回写 v3 → 重载无迁移；另验证未来版本前向兼容路径。

运行：QT_QPA_PLATFORM=offscreen .venv/bin/python tests/benchmark/drive_real_material_batches.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
VIDEO = ROOT / "tests" / "test_videos" / "2K_h264.mp4"

PASS = "  ✅"
FAIL = "  ❌"
_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, ok, detail))
    print(f"{PASS if ok else FAIL} {name}" + (f"  ({detail})" if detail else ""))
    return ok


def section(title: str) -> None:
    print(f"\n{'=' * 66}\n{title}\n{'=' * 66}")


# ======================================================================
# 素材准备：真实副本机位
# ======================================================================

def make_camera_copies(workdir: Path) -> dict[str, Path]:
    """ffmpeg 重编码出延迟 0.5s / 1.25s 的真实机位副本（帧精确）。"""
    out = {}
    for label, shift in (("camB", 0.5), ("camC", 1.25)):
        dst = workdir / f"{label}.mp4"
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-ss", f"{shift}", "-i", str(VIDEO),
            "-t", "20",
            "-vf", "scale=960:540",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-c:a", "aac", "-b:a", "128k",
            str(dst),
        ]
        subprocess.run(cmd, check=True, timeout=180)
        out[label] = dst
        print(f"  prepared {label}: shift={shift}s size={dst.stat().st_size // 1024}KB")
    return out


def probe(path: Path) -> dict:
    import av
    c = av.open(str(path))
    v = next((s for s in c.streams if s.type == "video"), None)
    a = next((s for s in c.streams if s.type == "audio"), None)
    info = {
        "fps": float(v.average_rate) if v else 0.0,
        "frames": int(v.frames or 0) if v else 0,
        "has_audio": a is not None,
        "sample_rate": int(a.codec_context.sample_rate) if a else 0,
    }
    c.close()
    return info


# ======================================================================
# 批次 D：多机位波形同步
# ======================================================================

def run_batch_d(workdir: Path) -> None:
    section("批次 D：多机位波形同步 + 角度网格（真实素材）")

    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])

    from src.features.edit.core.multicam import (
        angle_by_asset_id,
        build_multicam_group_from_assets,
        group_with_sync_offsets,
        multicam_group_from_asset,
        resolve_angle_source_frame,
        stamp_multicam_group_on_assets,
    )
    from src.features.edit.core.multicam_sync import (
        MulticamSyncJob,
        MulticamSyncWorker,
    )
    from src.features.edit.core.project import EditProject, MediaAsset

    cameras = make_camera_copies(workdir)
    paths = {"camA": VIDEO, **cameras}
    infos = {label: probe(p) for label, p in paths.items()}
    fps = infos["camA"]["fps"]
    print(f"  reference fps={fps} frames={infos['camA']['frames']} "
          f"audio={infos['camA']['has_audio']}@{infos['camA']['sample_rate']}Hz")
    check("副本素材有效（视频+音频）",
          all(i["has_audio"] and i["fps"] > 0 for i in infos.values()))

    # 真实素材 → MediaAsset / 组 / 盖戳
    assets = {}
    for label, p in paths.items():
        a = MediaAsset(
            asset_id=f"asset_{label}", path=str(p), media_type="video",
            display_name=label,
            metadata={"fps": fps, "total_frames": infos[label]["frames"]},
        )
        assets[label] = a
    group = build_multicam_group_from_assets(
        [assets[l].asset_id for l in ("camA", "camB", "camC")],
        display_names={a.asset_id: l for l, a in assets.items()},
    )
    stamped = stamp_multicam_group_on_assets(
        [assets[l] for l in ("camA", "camB", "camC")], group)
    project = EditProject.create(name="real-mc")
    for a in stamped:
        project = project.with_media_asset_added(a)
    check("组创建 + 盖戳（读回一致）",
          multicam_group_from_asset(project.assets["asset_camB"]) is not None
          and multicam_group_from_asset(project.assets["asset_camB"]).group_id
          == group.group_id)

    # 真实 worker（后台线程体直接执行；解码/相关全部走真实文件）
    jobs = [
        MulticamSyncJob(a.asset_id, a.path, fps)
        for a in stamped
    ]
    worker = MulticamSyncWorker(jobs, reference_asset_id="asset_camA")
    payload: dict = {}
    failed: list = []
    worker.sync_finished.connect(payload.update)
    worker.sync_failed.connect(failed.append)
    worker.run()

    check("worker 完成（无致命失败）", not failed, str(failed))
    check("全部角度无错误", all(
        not v.get("error") for v in payload.values()),
        str({k: v.get('error') for k, v in payload.items() if v.get('error')}))

    ref = payload.get("asset_camA", {})
    check("参考角度偏移=0 置信=1", ref.get("offset_frames") == 0
          and ref.get("confidence") == 1.0, str(ref))
    for label, shift in (("camB", 0.5), ("camC", 1.25)):
        item = payload.get(f"asset_{label}", {})
        expected = round(-shift * fps)
        got = item.get("offset_frames", 9999)
        check(f"{label} 同步偏移 ≈ {-shift}s（{expected}±1 帧）",
              abs(got - expected) <= 1 and item.get("confidence", 0) > 0.3,
              f"got={got} conf={item.get('confidence')}")

    # 偏移写回组 → 素材 metadata 持久
    offsets = {aid: v["offset_frames"] for aid, v in payload.items()
               if not v.get("error")}
    new_group = group_with_sync_offsets(group, offsets)
    restamped = stamp_multicam_group_on_assets(
        list(project.assets.values()), new_group)
    p2 = EditProject.create(name="real-mc2")
    for a in restamped:
        p2 = p2.with_media_asset_added(a)
    back = multicam_group_from_asset(p2.assets["asset_camC"])
    check("偏移写回组并持久", angle_by_asset_id(back, "asset_camC").sync_offset
          == offsets["asset_camC"])

    # 切换命令语义（真实偏移）：切到 camC 后 source_start = base + offset
    from src.features.edit.core.commands import CommandStack, SwitchMulticamAngleCommand
    from src.features.edit.core.timeline_v2 import (
        ClipInstance,
        EditTrack,
        Rational,
        TimelineSequence,
    )
    tl = TimelineSequence(
        timeline_id="tl", name="T", fps=Rational(int(round(fps)), 1),
        video_tracks=(EditTrack(
            track_id="v1", name="V1", track_type="video", index=0,
            clips=(ClipInstance(
                clip_id="mc", asset_id="asset_camA", track_id="v1",
                timeline_start=0, timeline_duration=100,
                source_start=0, source_duration=300,
            ),),
        ),),
    )
    cmd = SwitchMulticamAngleCommand.create(
        "v1", "mc", 40, "asset_camC",
        angle_sync_offset=offsets["asset_camC"], angle_label="camC")
    new_tl = CommandStack().execute(cmd, tl)
    right = next(c for c in new_tl.get_track_by_id("v1").clips
                 if c.timeline_start == 40)
    # split 后右半段先继承源位置（switch_frame=40 映射源 40），再叠加角度偏移
    expect_src = max(0, 40 + offsets["asset_camC"])
    check("切换命令读取 switch 点源位置+sync_offset",
          right.asset_id == "asset_camC" and right.source_start == expect_src,
          f"source_start={right.source_start} expect={expect_src}")

    # 角度网格：playhead=60 时 camC 应显示的源帧（含偏移）真实取帧成功
    grid_frame = resolve_angle_source_frame(
        clip_source_start=0, clip_timeline_start=0,
        active_sync_offset=0, target_sync_offset=offsets["asset_camC"],
        playhead_frame=60, total_frames=infos["camC"]["frames"])
    check("网格帧号 = 60 + 偏移（夹取有效）",
          grid_frame == max(0, min(60 + offsets["asset_camC"],
                                   infos["camC"]["frames"] - 1)),
          f"frame={grid_frame}")

    from src.shared.media.thumbnail_provider import ThumbnailProvider
    got = {}
    provider = ThumbnailProvider.instance()
    provider.frame_thumbnail_ready.connect(
        lambda path, frame, pix: got.setdefault("pixmap", pix))
    provider.request_frame_pixmap_async(
        "asset_camC", str(paths["camC"]), grid_frame,
        infos["camC"]["frames"], target_height=96)
    for _ in range(300):
        app.processEvents()
        if "pixmap" in got:
            break
        import time
        time.sleep(0.05)
    check("角度网格真实取帧（cv2 解码真实副本）",
          got.get("pixmap") is not None and not got["pixmap"].isNull(),
          f"size={None if not got else got['pixmap'].width()}x"
          f"{None if not got else got['pixmap'].height()}")


# ======================================================================
# 批次 E：嵌套音频真实导出
# ======================================================================

def run_batch_e(workdir: Path) -> None:
    section("批次 E：嵌套序列音频真实导出（TimelineExporter + FFmpeg）")

    from src.features.edit.core.nested_sequence import (
        create_nested_sequence_asset,
        flatten_nested_audio_tracks,
    )
    from src.features.edit.core.project import EditProject, MediaAsset
    from src.features.edit.core.timeline_export import (
        ExportCodec,
        ExportSource,
        TimelineExportConfig,
        create_timeline_exporter,
        expand_sequence_audio_for_render,
    )
    from src.features.edit.core.timeline_v2 import (
        ClipInstance,
        EditTrack,
        Rational,
        TimelineSequence,
    )

    fps = probe(VIDEO)["fps"]
    asset = MediaAsset(
        asset_id="a_real", path=str(VIDEO), media_type="video",
        display_name="2K_h264",
        metadata={"fps": fps, "total_frames": probe(VIDEO)["frames"]},
    )

    # 子序列：视频轨 + 音频轨（真实素材源 [100,148)）
    child = TimelineSequence(
        timeline_id="child", name="Child",
        fps=Rational(int(round(fps)), 1), width=960, height=540,
        video_tracks=(EditTrack(
            track_id="cv", name="CV", track_type="video", index=0,
            clips=(ClipInstance(
                clip_id="cv1", asset_id="a_real", track_id="cv",
                timeline_start=0, timeline_duration=48,
                source_start=0, source_duration=48,
            ),),
        ),),
        audio_tracks=(EditTrack(
            track_id="ca", name="CA", track_type="audio", index=0,
            clips=(ClipInstance(
                clip_id="ca1", asset_id="a_real", track_id="ca",
                timeline_start=0, timeline_duration=48,
                source_start=100, source_duration=48,
            ),),
        ),),
    )
    nested_asset = create_nested_sequence_asset(child, display_name="Nest")

    # 父序列：只有视频轨（嵌套片段）；没有任何自身音频 →
    # 导出文件里的音频只能来自嵌套子序列
    parent = TimelineSequence(
        timeline_id="parent", name="Parent",
        fps=Rational(int(round(fps)), 1), width=960, height=540,
        video_tracks=(EditTrack(
            track_id="v1", name="V1", track_type="video", index=0,
            clips=(ClipInstance(
                clip_id="nest1", asset_id=nested_asset.asset_id, track_id="v1",
                timeline_start=0, timeline_duration=48,
                source_start=0, source_duration=48,
            ),),
        ),),
        audio_tracks=(),
    )

    project = EditProject.create(name="real-nest")
    for a in (asset, nested_asset):
        project = project.with_media_asset_added(a)

    # 展开纯函数：父序列零音频轨但展开出虚拟轨
    expanded = flatten_nested_audio_tracks(
        parent, {"child": child}.get, project.assets.get)
    check("展开出嵌套虚拟音频轨",
          any(t.track_id.startswith("nest:") for t in expanded),
          f"tracks={[t.track_id for t in expanded]}")

    out_path = workdir / "nested_export.mp4"
    config = TimelineExportConfig(
        source=ExportSource.TIMELINE,
        timeline_id="parent",
        output_path=str(out_path),
        codec=ExportCodec.H264,
        resolution=(640, 360),
        include_audio=True,
    )
    exporter = create_timeline_exporter(
        project, parent, timeline_sequences={"child": child})
    ok = exporter.export_timeline(config)
    check("真实导出完成", ok and out_path.exists(),
          f"size={out_path.stat().st_size // 1024 if out_path.exists() else 0}KB")

    # 探测输出：音频流存在 + 非静音 + 与源素材对应区段互相关峰值≈0
    import av
    c = av.open(str(out_path))
    a_stream = next((s for s in c.streams if s.type == "audio"), None)
    v_stream = next((s for s in c.streams if s.type == "video"), None)
    check("输出含音频流", a_stream is not None)
    check("输出含视频流", v_stream is not None,
          f"frames={v_stream.frames if v_stream else '?'}")
    if a_stream is None:
        c.close()
        return

    chunks = []
    for frame in c.decode(audio=0):
        arr = frame.to_ndarray()
        if arr.ndim == 1:
            arr = arr[None, :]
        chunks.append(arr)
    c.close()
    out_pcm = np.concatenate(chunks, axis=1).astype(np.float32)
    if out_pcm.shape[0] > 1:
        out_mono = out_pcm.mean(axis=0)
    else:
        out_mono = out_pcm[0]
    rms = float(np.sqrt(np.mean(out_mono ** 2)))
    peak = float(np.abs(out_mono).max())
    check("输出音频非静音（能量来自嵌套）", rms > 1e-4,
          f"rms={rms:.5f} peak={peak:.4f}")

    # 位置正确性：输出音频 ≈ 源素材 [100,148) 帧 @25fps = 源 4.0s..5.92s
    from src.features.edit.core.multicam_sync import (
        best_lag_samples,
        decimate_mean,
        downmix_to_mono,
        _decode_mono_segment,
    )
    src_mono, err = _decode_mono_segment(str(VIDEO), seconds=8.0)
    check("源素材解码成功", src_mono is not None, err)
    if src_mono is None:
        return
    src_rate = 48000
    seg = src_mono[int(4.0 * src_rate):int(5.92 * src_rate)]
    factor = 6
    a = decimate_mean(out_mono, factor)
    b = decimate_mean(downmix_to_mono(seg[None, :]), factor)
    n = min(a.size, b.size)
    lag, conf = best_lag_samples(b[:n], a[:n], len(a) // 2)
    rate_low = src_rate // factor
    check("输出音频与源区段互相关峰值≈0（位置正确）",
          abs(lag) <= rate_low // 10 and conf > 0.2,
          f"lag={lag} ({lag / rate_low:+.3f}s) conf={conf:.3f}")


# ======================================================================
# 批次 F：真实工程文件迁移
# ======================================================================

def _rewrite_zip(src: Path, dst: Path, mutate) -> None:
    with zipfile.ZipFile(src) as zf:
        entries = {n: zf.read(n) for n in zf.namelist()}
    mutate(entries)
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, blob in entries.items():
            zf.writestr(name, blob)


def run_batch_f(workdir: Path) -> None:
    section("批次 F：内容 schema 迁移（真实 .topos 工程）")

    from src.features.edit.core.edit_project_io import EditProjectIO
    from src.features.edit.core.project_migration import (
        EDIT_CONTENT_SCHEMA_VERSION,
        EDIT_CONTENT_VERSION_KEY,
    )

    real_topos = ROOT / "tests" / "test_videos" / "2K_h264.topos"

    def inject_legacy(entries: dict) -> None:
        manifest = json.loads(entries["manifest.json"])
        manifest.pop(EDIT_CONTENT_VERSION_KEY, None)  # 历史工程：无版本标记
        entries["manifest.json"] = json.dumps(manifest).encode()
        # 素材 metadata 置空（v1→v2 迁移目标）
        mp = json.loads(entries["edit/media_pool.json"])
        for asset in mp.values():
            if isinstance(asset, dict):
                asset["metadata"] = None
        entries["edit/media_pool.json"] = json.dumps(mp).encode()
        # 注入 legacy blur（v2→v3 迁移目标）：任意深度 directional → motion
        seqs = json.loads(entries["edit/timeline_sequences.json"])
        injected = 0

        def walk(node):
            nonlocal injected
            if isinstance(node, dict):
                if node.get("effect_type") == "blur":
                    params = node.get("params")
                    if isinstance(params, dict):
                        params["blur_type"] = "directional"
                        injected += 1
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(seqs)
        if injected == 0 and seqs:
            # 工程无效果时补一个最小可解析的 legacy 效果到首个 clip
            for seq in seqs.values():
                for track in seq.get("video_tracks", []):
                    for clip in track.get("clips", []):
                        clip.setdefault("effects", []).append({
                            "effect_type": "blur",
                            "params": {"blur_type": "directional"},
                        })
                        injected += 1
                        break
                    if injected:
                        break
                if injected:
                    break
        entries["edit/timeline_sequences.json"] = json.dumps(seqs).encode()
        print(f"  injected legacy blur ×{injected}, metadata nulled, "
              f"manifest version removed")

    legacy = workdir / "legacy.topos"
    _rewrite_zip(real_topos, legacy, inject_legacy)

    io = EditProjectIO()
    result = io.load_project(legacy)
    check("旧工程加载成功", result is not None)
    if result is None:
        return
    log = io.last_content_migrations
    print("  migration log:")
    for entry in log:
        print(f"    - {entry}")
    check("触发完整迁移链（v1→v3）",
          any("assets_metadata_dict" in e for e in log)
          and any("blur_directional_to_motion" in e for e in log),
          f"{len(log)} steps")

    project, _ui, seqs = result
    first_asset = next(iter(project.assets.values()))
    check("素材 metadata 修复为 dict",
          isinstance(first_asset.metadata, dict))

    # 保存 → manifest 回写当前版本
    saved = workdir / "resaved.topos"
    ok = io.save_project(str(saved), project,
                         timeline_sequences=seqs or {},
                         create_backup=False)
    check("迁移后保存成功", ok and saved.exists())
    with zipfile.ZipFile(saved) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    check("保存回写当前内容版本",
          manifest.get(EDIT_CONTENT_VERSION_KEY) == EDIT_CONTENT_SCHEMA_VERSION,
          f"v{manifest.get(EDIT_CONTENT_VERSION_KEY)}")

    io2 = EditProjectIO()
    result2 = io2.load_project(saved)
    check("重载已迁移工程：无迁移、数据一致",
          result2 is not None and io2.last_content_migrations == (),
          f"log={io2.last_content_migrations}")

    # 前向兼容：未来版本工程 → 不盲迁、可打开、有告警
    future = workdir / "future.topos"

    def bump_future(entries: dict) -> None:
        manifest = json.loads(entries["manifest.json"])
        manifest[EDIT_CONTENT_VERSION_KEY] = EDIT_CONTENT_SCHEMA_VERSION + 9
        entries["manifest.json"] = json.dumps(manifest).encode()

    _rewrite_zip(real_topos, future, bump_future)
    io3 = EditProjectIO()
    result3 = io3.load_project(future)
    check("未来版本工程按尽力兼容打开",
          result3 is not None
          and any("较新版本" in e for e in io3.last_content_migrations),
          f"log={list(io3.last_content_migrations)[:1]}")


# ======================================================================

def main() -> int:
    if not VIDEO.exists():
        print(f"real video missing: {VIDEO}")
        return 2
    with tempfile.TemporaryDirectory(prefix="topos_real_") as td:
        workdir = Path(td)
        run_batch_d(workdir)
        run_batch_e(workdir)
        run_batch_f(workdir)

    section("总结")
    passed = sum(1 for _, ok, _ in _results if ok)
    for name, ok, detail in _results:
        if not ok:
            print(f"{FAIL} {name}  {detail}")
    print(f"\n{passed}/{len(_results)} checks passed")
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    sys.exit(main())
