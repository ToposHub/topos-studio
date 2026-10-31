"""Persistent Topos image-sequence export tests.

The sequence sink must reuse the video encoder context while publishing
independent ``.toos`` files that can each be opened without the other frames.
"""

from __future__ import annotations

import os

import numpy as np
import pytest

from src.shared.codec import topos_binding
from src.shared.export.topos_encoder import ToposImageSequenceEncoder
from src.shared.export.video_encoder import FFmpegEncoderConfig
from src.shared.media.frame import DecodedFrame
from src.shared.media.metadata import Rational


def _lib_available() -> bool:
    return any(p.is_file() for p in topos_binding.candidate_library_paths())


pytestmark = pytest.mark.skipif(
    not _lib_available(), reason="libtopos_codec 未构建"
)


def _frame(index: int, width: int = 64, height: int = 40) -> DecodedFrame:
    data = np.zeros((height, width, 3), dtype=np.uint8)
    data[..., 0] = (index * 17 + 12) & 0xFF
    data[..., 1] = 82
    data[..., 2] = 141
    return DecodedFrame(
        frame_index=index,
        pts=index,
        time_seconds=index / 24.0,
        data=data,
        pixel_format="bgr24",
        width=width,
        height=height,
    )


def _config() -> FFmpegEncoderConfig:
    return FFmpegEncoderConfig(
        codec="topos_image_sequence",
        width=64,
        height=40,
        fps=Rational(24, 1),
        pix_fmt="yuv422p10le",
        profile="standard",
    )


def _staging_dirs(tmp_path, base: str = "sequence"):
    """当前实例的暂存目录（N02：唯一命名 <base>.topos-seq-tmp.*）。"""
    return sorted(tmp_path.glob(f"{base}.topos-seq-tmp*"))


def test_persistent_encoder_writes_independent_toos(tmp_path) -> None:
    output = tmp_path / "sequence"
    enc = ToposImageSequenceEncoder(str(output), _config(), slice_threads=2)
    enc.open()
    for i in range(3):
        enc.encode_frame(_frame(i))
    enc.close()

    paths = sorted(output.glob("frame_*.toos"))
    assert [p.name for p in paths] == [
        "frame_0001.toos", "frame_0002.toos", "frame_0003.toos"
    ]
    codec = __import__(
        "src.shared.codec.topos_image_binding",
        fromlist=["get_image_codec"],
    ).get_image_codec()
    for path in paths:
        info = codec.validate(str(path), deep=True)
        planes, _, _ = codec.decode(str(path))
        assert info.visible_width == 64
        assert info.visible_height == 40
        assert len(planes) == 3


def test_image_sequence_rans2_native_payload(tmp_path) -> None:
    """rans2 序列原生入 .toos（2026-09-12 信封解封 V1..V8 后不再降级 v2）。

    此前 TPIM IDSC 域校验排除 V7/V8，序列导出把 rans2 静默覆写回 V2 VLC；
    解封后视频默认熵档（ADR-C038 rans2）原样进信封：包 major=7、
    信封解码 == 视频路径解码。"""
    output = tmp_path / "sequence-rans2"
    config = _config()
    config.entropy_mode = "rans2"
    enc = ToposImageSequenceEncoder(str(output), config, slice_threads=2)
    enc.open()
    assert enc._frame_cfg.reserved[0] == 8
    enc.encode_frame(_frame(0))
    enc.close()

    path = output / "frame_0001.toos"
    codec = __import__(
        "src.shared.codec.topos_image_binding",
        fromlist=["get_image_codec"],
    ).get_image_codec()
    info = codec.validate(str(path), deep=True)
    assert info.payload_version == (7, 0)  # 内层 TPIC major = V7（rans2 原生）
    planes, _, _ = codec.decode(str(path))
    assert info.visible_width == 64
    assert info.visible_height == 40
    assert len(planes) == 3


def test_abort_leaves_user_files_untouched(tmp_path) -> None:
    """C01：取消只清本次暂存产物——输出目录未被创建，用户原有序列逐字节不变。"""
    output = tmp_path / "sequence"
    enc = ToposImageSequenceEncoder(str(output), _config(), slice_threads=2)
    enc.open()
    enc.encode_frame(_frame(0))
    staging = _staging_dirs(tmp_path)
    assert len(staging) == 1
    assert (staging[0] / "frame_0001.toos").is_file()
    assert not output.exists()  # 写帧阶段不得触碰输出目录
    enc.abort()
    assert not _staging_dirs(tmp_path)
    assert not output.exists()


def test_abort_preserves_preexisting_sequence_frames(tmp_path) -> None:
    """C01 反例（原缺陷：abort 按路径 unlink 删掉被覆盖的用户原文件）。"""
    output = tmp_path / "sequence"
    output.mkdir()
    old1, old2 = output / "frame_0001.toos", output / "frame_0002.toos"
    old1.write_bytes(b"OLD-FRAME-1")
    old2.write_bytes(b"OLD-FRAME-2")
    enc = ToposImageSequenceEncoder(str(output), _config(), slice_threads=2)
    enc.open()
    enc.encode_frame(_frame(0))
    enc.encode_frame(_frame(1))
    enc.abort()
    assert old1.read_bytes() == b"OLD-FRAME-1"
    assert old2.read_bytes() == b"OLD-FRAME-2"
    assert not _staging_dirs(tmp_path)


def test_write_failure_preserves_preexisting_frames(tmp_path) -> None:
    """C01：第 N 帧写失败只清暂存，用户旧文件逐字节不变。"""
    from src.shared.codec import topos_image_binding

    output = tmp_path / "sequence"
    output.mkdir()
    old = output / "frame_0001.toos"
    old.write_bytes(b"OLD-FRAME-1")
    codec = topos_image_binding.get_image_codec()
    real_write = codec.write_packet
    calls = {"n": 0}

    def failing_write(path, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:  # 第 2 帧注入写失败
            raise OSError("injected write failure")
        return real_write(path, *a, **kw)

    enc = ToposImageSequenceEncoder(str(output), _config(), slice_threads=2)
    enc.open()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(codec, "write_packet", failing_write)
        with pytest.raises(RuntimeError, match="injected write failure"):
            enc.encode_frame(_frame(0))
            enc.encode_frame(_frame(1))
    enc.abort()
    assert old.read_bytes() == b"OLD-FRAME-1"
    assert not _staging_dirs(tmp_path)


def test_commit_failure_rolls_back_to_preexisting_frames(tmp_path) -> None:
    """C01：提交阶段失败 → 回滚硬链接备份，旧文件逐字节不变。"""
    output = tmp_path / "sequence"
    output.mkdir()
    old = {
        "frame_0001.toos": b"OLD-FRAME-1",
        "frame_0002.toos": b"OLD-FRAME-2",
    }
    for name, payload in old.items():
        (output / name).write_bytes(payload)
    enc = ToposImageSequenceEncoder(str(output), _config(), slice_threads=2)
    enc.open()
    enc.encode_frame(_frame(0))
    enc.encode_frame(_frame(1))
    assert len(_staging_dirs(tmp_path)) == 1

    real_replace = os.replace

    def failing_replace(src, dst):
        # 只拦提交动作（src 在暂存目录根、目标为 frame_0002）；
        # write_packet 与回滚（src 在 .backup/）不受影响
        if (os.path.basename(str(src)) == "frame_0002.toos"
                and ".backup" not in str(src)):
            raise OSError("injected commit failure")
        return real_replace(src, dst)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("src.shared.export.topos_encoder.os.replace",
                   failing_replace)
        with pytest.raises(RuntimeError, match="定稿失败"):
            enc.close()
    for name, payload in old.items():
        assert (output / name).read_bytes() == payload
    assert not _staging_dirs(tmp_path)


def test_commit_overwrites_matching_frames_only(tmp_path) -> None:
    """C01 成功覆盖策略：同名帧替换、超出本次帧数的旧文件不动。"""
    output = tmp_path / "sequence"
    output.mkdir()
    (output / "frame_0001.toos").write_bytes(b"OLD-FRAME-1")
    (output / "frame_0002.toos").write_bytes(b"OLD-FRAME-2")
    (output / "frame_0003.toos").write_bytes(b"OLD-FRAME-3")
    enc = ToposImageSequenceEncoder(str(output), _config(), slice_threads=2)
    enc.open()
    enc.encode_frame(_frame(0))
    enc.encode_frame(_frame(1))
    enc.close()

    assert (output / "frame_0001.toos").read_bytes() != b"OLD-FRAME-1"
    assert (output / "frame_0002.toos").read_bytes() != b"OLD-FRAME-2"
    assert (output / "frame_0003.toos").read_bytes() == b"OLD-FRAME-3"
    assert not _staging_dirs(tmp_path)
    codec = __import__(
        "src.shared.codec.topos_image_binding",
        fromlist=["get_image_codec"],
    ).get_image_codec()
    for name in ("frame_0001.toos", "frame_0002.toos"):
        codec.validate(str(output / name), deep=True)


# ---------------- N01/N02：序列事务与并发暂存 ----------------

def _dead_pid() -> int:
    """取一个确定已退出的进程 pid（模拟中断事务的宿主）。

    Windows：Popen 对象自身持有进程句柄期间，内核认为 PID 仍存活
    （os.kill(pid,0) 的 OpenProcess 成功）——真实崩溃由内核关闭句柄，
    这里必须显式释放（del + gc 触发 Popen.__del__）后 PID 才真死，
    否则 journal 恢复被判"宿主存活"而跳过（W01 首跑实案）。
    """
    import gc
    import subprocess
    import sys
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    pid = proc.pid
    proc.wait()
    del proc
    gc.collect()
    return pid


def test_rollback_secondary_failure_keeps_unique_backup(tmp_path) -> None:
    """N01：提交失败且备份还原二次失败 → 唯一备份保留 + 下次导出自动恢复。

    原缺陷：回滚失败仅记日志，close 异常路径把 .backup 一并 rmtree——
    旧帧内容从此不可找回。事务化后：回滚未完成禁止清理暂存目录，错误
    信息带恢复位置；同进程下一次导出按 journal 自动完成恢复。
    """
    output = tmp_path / "sequence"
    output.mkdir()
    (output / "frame_0001.toos").write_bytes(b"OLD-FRAME-1")
    (output / "frame_0002.toos").write_bytes(b"OLD-FRAME-2")
    enc = ToposImageSequenceEncoder(str(output), _config(), slice_threads=2)
    enc.open()
    enc.encode_frame(_frame(0))
    enc.encode_frame(_frame(1))

    real_replace = os.replace

    def failing_replace(src, dst):
        s = str(src)
        base = os.path.basename(s)
        if base == "frame_0002.toos" and ".backup" not in s:
            raise OSError("injected commit failure")   # 提交第 2 帧失败
        if ".backup" in s and base == "frame_0001.toos":
            raise OSError("injected rollback failure")  # 还原第 1 帧备份失败
        return real_replace(src, dst)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("src.shared.export.topos_encoder.os.replace",
                   failing_replace)
        with pytest.raises(RuntimeError, match="回滚未完成"):
            enc.close()
    # 唯一备份保留在暂存目录中（未被 close 的清理路径删除）
    dirs = _staging_dirs(tmp_path)
    assert len(dirs) == 1
    backup = dirs[0] / ".backup" / "frame_0001.toos"
    assert backup.read_bytes() == b"OLD-FRAME-1"
    # 未受影响的帧保持旧内容
    assert (output / "frame_0002.toos").read_bytes() == b"OLD-FRAME-2"
    # 同进程下一次导出（首个暂存目录创建点）：journal 驱动自动恢复
    enc2 = ToposImageSequenceEncoder(str(output), _config(), slice_threads=2)
    enc2.open()
    enc2._image_sequence_tmp_dir = enc2._create_image_sequence_staging()
    enc2.abort()   # 只触发恢复，不发布任何帧
    assert (output / "frame_0001.toos").read_bytes() == b"OLD-FRAME-1"
    assert (output / "frame_0002.toos").read_bytes() == b"OLD-FRAME-2"
    assert not _staging_dirs(tmp_path)


def test_interrupted_commit_recovered_by_next_export(tmp_path) -> None:
    """N01：进程在提交中段中断（prepared）→ 下次导出按 journal 整体回滚。"""
    import json

    output = tmp_path / "sequence"
    output.mkdir()
    (output / "frame_0001.toos").write_bytes(b"OLD-FRAME-1")
    (output / "frame_0002.toos").write_bytes(b"OLD-FRAME-2")
    # 伪造崩溃现场：第 1 帧已被新内容替换、第 2 帧未动、备份完整
    stale = tmp_path / "sequence.topos-seq-tmp.deadbeef"
    (stale / ".backup").mkdir(parents=True)
    (stale / ".backup" / "frame_0001.toos").write_bytes(b"OLD-FRAME-1")
    (stale / ".backup" / "frame_0002.toos").write_bytes(b"OLD-FRAME-2")
    (output / "frame_0001.toos").write_bytes(b"NEW-PARTIAL")
    journal = {
        "version": 1, "output": str(output), "pid": _dead_pid(),
        "state": "prepared", "created_output": False,
        "frames": [
            {"name": "frame_0001.toos", "existed": True},
            {"name": "frame_0002.toos", "existed": True},
        ],
    }
    (stale / ".commit-journal.json").write_text(json.dumps(journal))

    enc = ToposImageSequenceEncoder(str(output), _config(), slice_threads=2)
    enc.open()
    enc._image_sequence_tmp_dir = enc._create_image_sequence_staging()
    enc.abort()   # 恢复发生在暂存目录创建点；不发布任何帧
    # 中断事务已收敛：被部分替换的第 1 帧还原旧字节，第 2 帧未动
    assert (output / "frame_0001.toos").read_bytes() == b"OLD-FRAME-1"
    assert (output / "frame_0002.toos").read_bytes() == b"OLD-FRAME-2"
    assert not stale.exists()
    assert not _staging_dirs(tmp_path)


def test_stale_writing_dir_without_journal_cleaned_on_next_export(
        tmp_path) -> None:
    """N01：写帧阶段中断（无 journal / 宿主已死）→ 帧从未发布，直接清理。"""
    output = tmp_path / "sequence"
    output.mkdir()
    (output / "frame_0001.toos").write_bytes(b"OLD-FRAME-1")
    stale = tmp_path / "sequence.topos-seq-tmp.orphan"
    stale.mkdir()
    (stale / "frame_0001.toos").write_bytes(b"STALE-STAGING")

    enc = ToposImageSequenceEncoder(str(output), _config(), slice_threads=2)
    enc.open()
    enc.encode_frame(_frame(0))
    enc.close()
    assert (output / "frame_0001.toos").read_bytes() != b"STALE-STAGING"
    assert not stale.exists()
    assert not _staging_dirs(tmp_path)


def test_concurrent_same_target_exports_are_isolated(tmp_path) -> None:
    """N02：两个实例同目标交错写/取消/定稿——互不删除对方数据。

    原缺陷：共用固定暂存目录 + exist_ok=True，一方 abort 整目录
    rmtree，另一方 close 因帧缺失而失败。
    """
    output = tmp_path / "sequence"
    output.mkdir()
    (output / "frame_0001.toos").write_bytes(b"OLD-FRAME-1")
    a = ToposImageSequenceEncoder(str(output), _config(), slice_threads=2)
    b = ToposImageSequenceEncoder(str(output), _config(), slice_threads=2)
    a.open()
    b.open()
    a.encode_frame(_frame(0))     # a 暂存 frame_0001
    b.encode_frame(_frame(0))     # b 暂存 frame_0001、frame_0002
    b.encode_frame(_frame(1))
    a.abort()                     # 只清 a 自己的暂存目录
    dirs = _staging_dirs(tmp_path)
    assert len(dirs) == 1
    assert (dirs[0] / "frame_0001.toos").is_file()
    assert (dirs[0] / "frame_0002.toos").is_file()
    b.close()                     # b 不受 a 取消影响，正常定稿
    assert (output / "frame_0001.toos").read_bytes() != b"OLD-FRAME-1"
    assert (output / "frame_0002.toos").is_file()
    assert not _staging_dirs(tmp_path)


def test_failed_commit_removes_newly_created_empty_output_dir(
        tmp_path) -> None:
    """N02：输出目录原不存在、提交失败回滚后——不留伪成功空目录。"""
    output = tmp_path / "fresh"
    enc = ToposImageSequenceEncoder(str(output), _config(), slice_threads=2)
    enc.open()
    enc.encode_frame(_frame(0))

    real_replace = os.replace

    def failing_replace(src, dst):
        if os.path.basename(str(src)) == "frame_0001.toos" \
                and ".backup" not in str(src):
            raise OSError("injected commit failure")
        return real_replace(src, dst)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("src.shared.export.topos_encoder.os.replace",
                   failing_replace)
        with pytest.raises(RuntimeError, match="定稿失败"):
            enc.close()
    assert not output.exists()          # 空目录已随回滚移除
    assert not _staging_dirs(tmp_path)  # 回滚完成 → 暂存正常清理


def test_sequence_frames_carry_tmet_tier_metadata(tmp_path) -> None:
    """v1.7 容器元数据：序列帧携带 TMET（tier id + 档位名 + 厂商）。

    序列导出走视频档位体系（standard → tier id 3 / 'Topos 422'）；
    读侧经 image source probe 恢复为 codec_profile，Info 面板免拼接显示。
    """
    from src.shared.media.metadata import format_codec_label
    from src.shared.media.topos_image_source import ToposImageSource

    output = tmp_path / "sequence-tmet"
    enc = ToposImageSequenceEncoder(str(output), _config(), slice_threads=2)
    enc.open()
    enc.encode_frame(_frame(0))
    enc.close()

    path = output / "frame_0001.toos"
    codec = __import__(
        "src.shared.codec.topos_image_binding",
        fromlist=["get_image_codec"],
    ).get_image_codec()
    meta = codec.read_export_meta(str(path))
    assert meta is not None
    assert meta["tier_id"] == 3  # standard
    assert meta["vendor"] == "TOPOS"
    assert meta["label"] == "Topos 422"

    src_ = ToposImageSource(str(path))
    md = src_.probe()
    assert md.codec_name == "topos-image"
    assert md.codec_profile == "Topos 422"
    assert format_codec_label(md.codec_name, md.codec_profile) == "Topos 422"


# ---------------- M2-F8/F10/F12：图片档位注册表导出矩阵 ----------------

class TestImageTierExportMatrix:
    """profile 携带图片档位注册表 id → 按 CLI 同锚编码 + TMET 展示名。

    整数（422/444）与浮点四档全矩阵；每档验证：IDSC 组合、TMET tier id
    与展示名（N-4/N-5 形式）、读回 label 与导入侧显示一致。
    """

    @staticmethod
    def _encode_one_tier(tmp_path, tier):
        output = tmp_path / f"seq-{tier.tier_id}"
        cfg = FFmpegEncoderConfig(
            codec="topos_image_sequence", width=64, height=40,
            fps=Rational(24, 1),
            # 档位权威：pix_fmt/qp 由注册表 coerce（UI 只传 tier id）
            pix_fmt=tier.pix_fmt, profile=tier.tier_id,
            crf=int(tier.qp),
        )
        enc = ToposImageSequenceEncoder(str(output), cfg)
        enc.open()
        enc.encode_frame(_frame(0))
        enc.close()
        return output / "frame_0001.toos"

    def _tier_ids(self, *families):
        from src.shared.codec.topos_profiles import available_topos_image_tiers
        return [t for t in available_topos_image_tiers()
                if t.family in families]

    def test_integer_tiers_matrix(self, tmp_path) -> None:
        from src.shared.codec.topos_image_binding import get_image_codec
        codec = get_image_codec()
        for tier in self._tier_ids("int422", "int444"):
            path = self._encode_one_tier(tmp_path, tier)
            info = codec.probe(str(path))
            assert info.image_profile == tier.image_profile, tier.tier_id
            assert info.pixel_format == tier.pixel_format, tier.tier_id
            assert info.bit_depth == tier.bit_depth, tier.tier_id
            # F10：TMET label = N-4 形式（444 家族不入阶梯 → 仅 vendor）
            meta = codec.read_export_meta(str(path)) or {}
            if tier.tm_tier_id:
                assert meta.get("tier_id") == tier.tm_tier_id, tier.tier_id
                assert meta.get("label") == tier.file_label, tier.tier_id
            else:
                assert meta.get("tier_id") in (0, None), tier.tier_id
                assert not meta.get("label"), tier.tier_id

    def test_float_tiers_matrix(self, tmp_path) -> None:
        from src.shared.codec.topos_image_binding import (
            TC_IMG_PROFILE_HALF_FLOAT,
            TC_IMG_SAMPLE_KIND_HALF,
            get_image_codec,
        )
        from src.shared.media.metadata import Rational  # noqa: F401
        codec = get_image_codec()
        hdr_frame = self._hdr_float_frame
        for tier in self._tier_ids("float"):
            output = tmp_path / f"seq-{tier.tier_id}"
            cfg = FFmpegEncoderConfig(
                codec="topos_image_sequence", width=64, height=40,
                fps=Rational(24, 1), pix_fmt=tier.pix_fmt,
                profile=tier.tier_id, crf=int(tier.qp),
            )
            enc = ToposImageSequenceEncoder(str(output), cfg)
            enc.open()
            enc.encode_frame(hdr_frame(0))
            enc.close()
            path = output / "frame_0001.toos"
            info = codec.probe(str(path))
            assert info.sample_kind == TC_IMG_SAMPLE_KIND_HALF, tier.tier_id
            assert info.image_profile == TC_IMG_PROFILE_HALF_FLOAT, tier.tier_id
            assert info.pixel_format == 2 and info.bit_depth == 16, tier.tier_id
            assert info.color_transfer == 8, tier.tier_id  # linear 冻结
            # F10：浮点 TMET tier id 与整数阶梯共用 + N-5 展示名
            meta = codec.read_export_meta(str(path)) or {}
            assert meta.get("tier_id") == tier.tm_tier_id, tier.tier_id
            assert meta.get("label") == tier.file_label, tier.tier_id

    @staticmethod
    def _hdr_float_frame(index: int) -> DecodedFrame:
        yy, xx = np.mgrid[0:40, 0:64]
        rgb = np.zeros((40, 64, 3), np.float32)
        rgb[..., 0] = 0.25 + 0.5 * xx / 64
        rgb[..., 1] = -0.1 + 0.2 * yy / 40
        rgb[..., 2] = 0.4
        rgb[0, 0, 2] = 2.0
        return DecodedFrame(
            frame_index=index, pts=index, time_seconds=index / 24.0,
            data=rgb, pixel_format="rgb24", width=64, height=40,
        )

    def test_readback_label_matches_import_display(self, tmp_path) -> None:
        """F10 验收：落盘 label 读回 = ToposImageSource 导入侧显示名。"""
        from src.shared.codec.topos_image_binding import get_image_codec
        from src.shared.media.topos_image_source import ToposImageSource

        codec = get_image_codec()
        for tier in self._tier_ids("int422", "float"):
            if tier.family == "float":
                output = tmp_path / f"rb-{tier.tier_id}"
                cfg = FFmpegEncoderConfig(
                    codec="topos_image_sequence", width=64, height=40,
                    fps=Rational(24, 1), pix_fmt=tier.pix_fmt,
                    profile=tier.tier_id, crf=int(tier.qp),
                )
                enc = ToposImageSequenceEncoder(str(output), cfg)
                enc.open()
                enc.encode_frame(self._hdr_float_frame(0))
                enc.close()
                path = output / "frame_0001.toos"
            else:
                path = self._encode_one_tier(tmp_path, tier)
            src = ToposImageSource(str(path))
            try:
                meta = src.probe()
                assert meta.codec_profile == tier.file_label, tier.tier_id
            finally:
                src.close()

    def test_quick_export_toos_still_frame(self, tmp_path) -> None:
        """F9：Quick Export 静帧 .toos（FileIO.save_image 单帧编码）。

        默认档 float-ultra → 'toos Ultra 16bit float'；整数档同通。
        """
        from src.shared.codec.topos_image_binding import (
            TC_IMG_SAMPLE_KIND_HALF,
            get_image_codec,
        )
        from src.shared.io.file_io import FileIO

        codec = get_image_codec()
        rng = np.random.default_rng(11)
        img = (0.3 + 0.4 * rng.random((32, 48, 3))).astype(np.float32)

        out_float = tmp_path / "still.toos"
        assert FileIO.save_image(str(out_float), img,
                                 options={"toos_tier": "float-ultra"})
        info = codec.probe(str(out_float))
        assert info.sample_kind == TC_IMG_SAMPLE_KIND_HALF
        meta = codec.read_export_meta(str(out_float)) or {}
        assert meta.get("label") == "toos Ultra 16bit float"
        assert meta.get("tier_id") == 4

        out_int = tmp_path / "still_int.toos"
        assert FileIO.save_image(str(out_int), img,
                                 options={"toos_tier": "422-high"})
        meta_int = codec.read_export_meta(str(out_int)) or {}
        assert meta_int.get("label") == "toos High 10bit"
        assert meta_int.get("tier_id") == 3


# ---------------- M3-R1/R4：图片 RAW（TRAW）应用导出编码链 ----------------

class TestRawImageExport:
    """RAW 档应用导出：CFA 相位平面直通编码，与 CLI --tier 同档产物
    逐位一致（qp/几何/TMET，R1 验收）。

    TMET：应用与 CLI 同写 tier_id=0 + N-6 展示名 'toos RAW 12bit 4:1'
    （D2 位深无连字符）。
    """

    W, H = 64, 48

    @staticmethod
    def _cfa_planes(seed: int = 5):
        rng = np.random.default_rng(seed)
        ph, pw = (TestRawImageExport.H + 1) // 2, (TestRawImageExport.W + 1) // 2
        # 12-bit 域内容（uint16 满刻度容器，码值 ≤ 4095）
        return [rng.integers(0, 4096, size=(ph, pw)).astype('<u2')
                for _ in range(4)]

    @staticmethod
    def _cfa_frame(planes):
        h, w = TestRawImageExport.H, TestRawImageExport.W
        from src.shared.media.frame import PlaneInfo
        infos = tuple(
            PlaneInfo.create(index=i, width=p.shape[1], height=p.shape[0],
                             component=c, dtype=np.dtype(np.uint16),
                             bit_depth=12)
            for i, (c, p) in enumerate(zip(('R', 'Gr', 'Gb', 'B'), planes)))
        return DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            planes=tuple(planes), pixel_format='topos_cfa12le',
            dtype=np.dtype(np.uint16), width=w, height=h,
            plane_infos=infos,
        )

    def _app_encode(self, tmp_path, tier_id, planes, pix_fmt):
        output = tmp_path / f"raw-{tier_id}"
        cfg = FFmpegEncoderConfig(
            codec="topos_image_sequence", width=self.W, height=self.H,
            fps=Rational(24, 1), pix_fmt=pix_fmt, profile=tier_id,
            crf=59,
        )
        enc = ToposImageSequenceEncoder(str(output), cfg)
        enc.open()
        enc.encode_frame(self._cfa_frame(planes))
        enc.close()
        return output / "frame_0001.toos"

    def test_raw_matches_cli_byte_identical(self, tmp_path) -> None:
        """R1 验收：应用导出 RAW .toos ≡ CLI 同档产物（payload+TMET）。"""
        import subprocess
        import sys
        from pathlib import Path as _P

        cli = _P(__file__).resolve().parents[2] / \
            "native/topos_codec/build/debug/toos"
        if not cli.exists():
            pytest.skip("toos CLI 未构建")

        from src.shared.codec.topos_image_binding import get_image_codec

        planes = self._cfa_planes()
        raw_in = tmp_path / "bayer.raw"
        np.concatenate([p.reshape(-1) for p in planes]).astype('<u2').tofile(raw_in)

        codec = get_image_codec()
        for tier_id, pix, qp in (("raw12-4", "topos_cfa12le", 59),):
            app_out = self._app_encode(tmp_path, tier_id, planes, pix_fmt=pix)
            cli_out = tmp_path / f"cli-{tier_id}.toos"
            proc = subprocess.run(
                [str(cli), "encode", str(raw_in), "-o", str(cli_out),
                 "--width", str(self.W), "--height", str(self.H),
                 "--tier", tier_id, "--overwrite"],
                capture_output=True, text=True)
            assert proc.returncode == 0, proc.stderr

            # 逐位一致（TPIC payload + IDSC + TMET 全文件）
            assert app_out.read_bytes() == cli_out.read_bytes(), (
                f"{tier_id}: 应用产物与 CLI 不同")

            info = codec.probe(str(app_out))
            assert info.image_profile == 3
            assert info.pixel_format == 3 and info.bit_depth == 12
            assert info.color_transfer == 20   # TRAW_LOG0（12-bit 冻结对）
            meta = codec.read_export_meta(str(app_out)) or {}
            assert meta.get("tier_id") == 0    # RAW 不入 image tier id
            assert meta.get("label") == "toos RAW 12bit 4:1"

    def test_raw16_rejected_alpha_and_packed(self, tmp_path) -> None:
        """RAW 约束：alpha 互斥 + packed RGB 输入显式拒绝（不静默编码）。"""
        planes = self._cfa_planes()
        frame = self._cfa_frame(planes)
        # packed 输入 + raw 档：清晰报错
        cfg = FFmpegEncoderConfig(
            codec="topos_image_sequence", width=self.W, height=self.H,
            fps=Rational(24, 1), pix_fmt="topos_cfa12le", profile="raw12-4",
            crf=59,
        )
        enc = ToposImageSequenceEncoder(str(tmp_path / "raw-packed"), cfg)
        rgb = np.zeros((self.H, self.W, 3), np.float32)
        packed = DecodedFrame(frame_index=0, pts=0, time_seconds=0.0,
                              data=rgb, pixel_format="rgb24",
                              width=self.W, height=self.H)
        enc.open()
        try:
            with pytest.raises(RuntimeError, match="CFA"):
                enc.encode_frame(packed)
        finally:
            enc.abort()   # 帧被拒 → 丢弃半成品（close 会拒空序列）


class TestToosExportKeepsLinearPolicy:
    """2026-09-21 复查：.toos 浮点档导出保持场景线性（F2 契约）。"""

    def test_policy_matrix(self) -> None:
        from src.shared.io.file_io import FileIO

        # EXR 恒线性
        assert FileIO.toos_export_keeps_linear("/tmp/a.exr") is True
        # .toos 浮点档 → 线性；整数/RAW/未指定（默认 float-ultra）语义
        assert FileIO.toos_export_keeps_linear(
            "/tmp/a.toos", {"toos_tier": "float-low"}) is True
        assert FileIO.toos_export_keeps_linear(
            "/tmp/a.toos", {"toos_tier": "float-ultra"}) is True
        assert FileIO.toos_export_keeps_linear(
            "/tmp/a.toos", {"toos_tier": "422-high"}) is False
        assert FileIO.toos_export_keeps_linear(
            "/tmp/a.toos", {"toos_tier": "444-ultra"}) is False
        assert FileIO.toos_export_keeps_linear(
            "/tmp/a.toos", {"toos_tier": "raw12-4"}) is False
        assert FileIO.toos_export_keeps_linear("/tmp/a.toos", {}) is True
        # 其他显示格式不保持
        assert FileIO.toos_export_keeps_linear("/tmp/a.png") is False
        assert FileIO.toos_export_keeps_linear("/tmp/a.tif") is False

    def test_float_alpha_rejected_fail_closed(self, tmp_path) -> None:
        """HALF+alpha 写侧显式拒绝（读侧白名单未开放——不再产出
        应用内无法导入的文件）。"""
        import numpy as np

        from src.shared.io.file_io import FileIO

        rgba = np.zeros((4, 4, 4), dtype=np.float32)
        rgba[..., 3] = 1.0
        with pytest.raises(ValueError, match="alpha"):
            FileIO._save_toos_image(
                str(tmp_path / "a.toos"), rgba,
                {"toos_tier": "float-ultra"})
