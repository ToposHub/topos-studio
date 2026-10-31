"""指纹算法测试：三类指纹 + 退化路径 + 分派。"""

from __future__ import annotations

import os
import shutil

import pytest


# ---------------------------------------------------------------------- #
# compute / verify 基础行为
# ---------------------------------------------------------------------- #

def test_compute_returns_prefixed_string(sample_files):
    """所有指纹都带类型前缀（或 size: 降级）。"""
    from apps.topos_vault.fingerprint import compute_fingerprint, parse_fingerprint_type

    for mtype, key in [("video", "video"), ("audio", "audio"), ("image", "image")]:
        fp = compute_fingerprint(str(sample_files[key]), mtype)
        assert ":" in fp, f"指纹无冒号分隔：{fp}"
        prefix = parse_fingerprint_type(fp)
        # 要么是真实算法前缀，要么是 size: 降级
        assert prefix in ("xxh3_3pt", "chroma", "phash", "size"), f"未知前缀：{prefix}"


def test_verify_same_file_true(sample_files):
    """同一文件 verify 自己的指纹 → True。"""
    from apps.topos_vault.fingerprint import compute_fingerprint, verify_fingerprint

    for mtype, key in [("video", "video"), ("audio", "audio"), ("image", "image")]:
        fp = compute_fingerprint(str(sample_files[key]), mtype)
        assert verify_fingerprint(str(sample_files[key]), fp), f"{mtype} 自身验证失败"


def test_verify_different_file_false(sample_files):
    """不同文件用对方的指纹 verify → False。"""
    from apps.topos_vault.fingerprint import compute_fingerprint, verify_fingerprint

    fp_video = compute_fingerprint(str(sample_files["video"]), "video")
    assert not verify_fingerprint(str(sample_files["audio"]), fp_video)


def test_unknown_media_type_falls_back_to_size(sample_files):
    """未知类型 → size: 降级。"""
    from apps.topos_vault.fingerprint import compute_fingerprint, parse_fingerprint_type

    fp = compute_fingerprint(str(sample_files["video"]), "unknown_type")
    assert parse_fingerprint_type(fp) == "size"


# ---------------------------------------------------------------------- #
# 内容相同的文件 → 相同指纹（核心：这是 relink 能工作的基础）
# ---------------------------------------------------------------------- #

def test_identical_content_same_fingerprint(sample_files, tmp_path):
    """拷贝一份内容相同的文件，指纹应相同。"""
    from apps.topos_vault.fingerprint import compute_fingerprint

    original = sample_files["image"]
    copy = tmp_path / "copy.jpg"
    shutil.copy2(original, copy)

    fp1 = compute_fingerprint(str(original), "image")
    fp2 = compute_fingerprint(str(copy), "image")
    assert fp1 == fp2, "内容相同的文件指纹必须相同（relink 的基础）"


def test_different_content_different_fingerprint(sample_files):
    """不同内容的文件指纹应不同。"""
    from apps.topos_vault.fingerprint import compute_fingerprint

    fp1 = compute_fingerprint(str(sample_files["image"]), "image")
    fp2 = compute_fingerprint(str(sample_files["image2"]), "image")
    assert fp1 != fp2, "不同内容应有不同指纹"


# ---------------------------------------------------------------------- #
# 降级模式（依赖缺失时）
# ---------------------------------------------------------------------- #

def test_degraded_mode_still_works(sample_files):
    """降级模式下（size:）仍能算指纹和验证（不崩溃）。"""
    from apps.topos_vault.fingerprint import compute_fingerprint, verify_fingerprint
    # 不管 xxhash/imagehash 是否装了，compute 都不抛异常
    for mtype, key in [("video", "video"), ("audio", "audio"), ("image", "image")]:
        fp = compute_fingerprint(str(sample_files[key]), mtype)
        assert fp  # 非空
        assert verify_fingerprint(str(sample_files[key]), fp)


# ---------------------------------------------------------------------- #
# 分派器
# ---------------------------------------------------------------------- #

def test_dispatch_by_media_type(sample_files):
    """compute_fingerprint 按 media_type 分派到对应算法。"""
    from apps.topos_vault.fingerprint import compute_fingerprint, parse_fingerprint_type

    # video → xxh3_3pt 或 size
    fp = compute_fingerprint(str(sample_files["video"]), "video")
    assert parse_fingerprint_type(fp) in ("xxh3_3pt", "size")

    # image → phash 或 size
    fp = compute_fingerprint(str(sample_files["image"]), "image")
    assert parse_fingerprint_type(fp) in ("phash", "size")

    # image_sequence 也走 phash
    fp = compute_fingerprint(str(sample_files["image"]), "image_sequence")
    assert parse_fingerprint_type(fp) in ("phash", "size")


def test_verify_dispatches_by_prefix(sample_files):
    """verify_fingerprint 按指纹前缀分派（不依赖 media_type）。"""
    from apps.topos_vault.fingerprint import compute_fingerprint, verify_fingerprint

    fp = compute_fingerprint(str(sample_files["video"]), "video")
    # verify 不需要知道 media_type，靠前缀分派
    assert verify_fingerprint(str(sample_files["video"]), fp)
