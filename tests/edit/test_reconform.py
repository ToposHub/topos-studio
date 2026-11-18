"""P2 · Reconform 核心（2026-11-19 计划第二阶段）。

验收（核心算法子集）：内容指纹对小幅噪点稳定、对内容变化敏感；
删场/换镜/重排三类改版输出正确的 keep/insert/delete/move 候选表；
音频计划保留 handles。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.reconform import (
    ShotRef,
    match_shots,
    plan_audio_reconform,
    shot_fingerprint,
)


def _clip(seed: int, frames: int = 24, size: int = 32):
    """按 seed 亮度分层的结构化内容（指纹可区分）。"""
    rng = np.random.default_rng(seed)
    base = rng.integers(0, 120, (frames, size, size, 3), dtype=np.uint8)
    return np.clip(base.astype(np.int16) + (seed * 23) % 200,
                   0, 255).astype(np.uint8)


class TestFingerprint:
    def test_stable_to_noise_sensitive_to_content(self) -> None:
        base = _clip(1)
        noisy = base.astype(np.int16)
        noisy += np.random.default_rng(9).integers(
            -6, 6, noisy.shape)          # 小幅噪点
        # 真实内容差异：整体亮度抬升 60。
        different = np.clip(base.astype(np.int16) + 60, 0,
                            255).astype(np.uint8)
        assert shot_fingerprint(base) == shot_fingerprint(
            noisy.astype(np.uint8))
        assert shot_fingerprint(base) != shot_fingerprint(different)


class TestMatchShots:
    def _shots(self, seeds):
        return [ShotRef(shot_id=f"s{seed}", source_tc_frames=i * 24,
                        duration_frames=24,
                        fingerprint=shot_fingerprint(_clip(seed)))
                for i, seed in enumerate(seeds)]

    def test_delete_scene(self) -> None:
        old = self._shots([1, 2, 3, 4])
        new = self._shots([1, 3, 4])     # 删掉镜 2
        changes = match_shots(old, new)
        kinds = [change.kind for change in changes]
        assert kinds.count("delete") == 1
        assert kinds.count("keep") == 3
        deleted = next(c for c in changes if c.kind == "delete")
        assert deleted.old_shot.shot_id == "s2"

    def test_insert_shot(self) -> None:
        old = self._shots([1, 2])
        new = self._shots([1, 9, 2])     # 新镜 9 插入
        changes = match_shots(old, new)
        kinds = [change.kind for change in changes]
        assert kinds.count("insert") == 1
        assert kinds.count("keep") == 2

    def test_reorder_becomes_move(self) -> None:
        old = self._shots([1, 2, 3])
        new = self._shots([3, 1, 2])     # 镜 3 移到最前
        changes = match_shots(old, new)
        kinds = [change.kind for change in changes]
        assert "move" in kinds
        moved = next(c for c in changes if c.kind == "move")
        assert moved.old_shot.shot_id == "s3"
        assert moved.new_start == 0

    def test_identity_by_id_or_fingerprint(self) -> None:
        old = [ShotRef("a", 0, 24, fingerprint="ff")]
        new = [ShotRef("a", 0, 24, fingerprint="")]   # id 命中
        assert match_shots(old, new)[0].kind == "keep"
        new2 = [ShotRef("z", 0, 24, fingerprint="ff")]  # 指纹命中
        assert match_shots(old, new2)[0].kind == "keep"


class TestAudioPlan:
    def test_moves_and_handles(self) -> None:
        changes = [
            type(match_shots([ShotRef("a", 0, 24)],
                              [ShotRef("a", 0, 24)])[0])(
                kind="keep",
                old_shot=ShotRef("a", 0, 24),
                new_shot=ShotRef("a", 0, 24),
                old_start=0, new_start=0),
        ]
        plan = plan_audio_reconform(changes, handle_frames=12)
        assert plan.keeps == ((0, 0, 24),)
        assert plan.trims[0][2] == 12     # handles 保留
        assert plan.moves == ()
