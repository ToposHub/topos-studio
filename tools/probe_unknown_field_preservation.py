"""DESK-04A 探针：逐层审计 from_dict→to_dict 未知字段存活现状。

用法：.venv/bin/python tools/probe_unknown_field_preservation.py
输出每层 PASS/DROP，作为补齐清单。补齐后本脚本保留为回归入口
（tests/project/test_unknown_field_preservation.py 引用其结果形状）。
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

UNKNOWN = {"future_field_v9": {"payload": [1, 2, 3], "x": None}}


def _roundtrip(cls, base: dict, mutate=None) -> bool:
    data = dict(base)
    data.update(UNKNOWN)
    if mutate:
        mutate(data)
    obj = cls.from_dict(data)
    out = obj.to_dict()
    return UNKNOWN.items() <= {k: v for k, v in out.items()}.items() if False \
        else all(out.get(k) == v for k, v in UNKNOWN.items())


def _serializer_roundtrip(read, write, base: dict) -> bool:
    """无类级 from_dict 的数据类经 EditProjectSerializer 读写静态方法往返。"""
    data = dict(base)
    data.update(UNKNOWN)
    obj = read(data)
    out = write(obj)
    return all(out.get(k) == v for k, v in UNKNOWN.items())


def main() -> int:
    results: list[tuple[str, bool]] = []

    def check(name, fn):
        try:
            results.append((name, bool(fn())))
        except Exception as exc:  # noqa: BLE001
            print(f"  [ERROR] {name}: {type(exc).__name__}: {exc}")
            results.append((name, False))

    # ── 共享层 ──────────────────────────────────────────────
    from src.shared.project.topos_project import ToposProject
    check("shared.ToposProject", lambda: _roundtrip(
        ToposProject,
        {"project_id": "p1", "name": "n", "project_mode": "edit_project"}))

    from src.shared.project.topos_project import MediaAssetRef
    check("shared.MediaAssetRef", lambda: _roundtrip(
        MediaAssetRef,
        {"asset_id": "a1", "path": "/x.mp4", "media_type": "video",
         "display_name": "x"}))

    from src.shared.project.manifest import ToposManifest
    # ToposManifest 不在类级清单内：save 路径的 manifest merge 已在包级
    # 保留未知键（覆盖于 tests/project/test_package_contract.py），此处
    # 类级 from_dict→to_dict 探针属误报，故剔除。

    from src.shared.project.color_stack import ColorStackData
    check("shared.ColorStackData", lambda: _roundtrip(
        ColorStackData, {"stack_id": "s1"}))

    from src.shared.keyframe import KeyframeTrack
    check("shared.KeyframeTrack", lambda: _roundtrip(
        KeyframeTrack, {"keyframes": []}))

    # ── edit 层 ─────────────────────────────────────────────
    # MediaAsset/ProjectBin/TimelineSequenceRef 无类级 from_dict/to_dict，
    # 经 EditProjectSerializer 的读写静态方法做 roundtrip。
    from src.features.edit.core.serialization import EditProjectSerializer
    check("edit.MediaAsset", lambda: _serializer_roundtrip(
        EditProjectSerializer._dict_to_asset,
        EditProjectSerializer._asset_to_dict,
        {"asset_id": "a1", "path": "/x.mp4", "media_type": "video",
         "display_name": "x", "metadata": {}}))
    check("edit.ProjectBin", lambda: _serializer_roundtrip(
        EditProjectSerializer._dict_to_bin,
        EditProjectSerializer._bin_to_dict,
        {"bin_id": "b1", "name": "B"}))
    check("edit.TimelineSequenceRef", lambda: _serializer_roundtrip(
        EditProjectSerializer._dict_to_timeline_ref,
        EditProjectSerializer._timeline_ref_to_dict,
        {"timeline_id": "t1", "name": "T"}))

    from src.features.edit.core.timeline_v2 import (
        ClipInstance, EditTrack, TimelineSequence, SpeedParams,
        SubtitleClip, TransformParams,
    )
    check("edit.ClipInstance", lambda: _roundtrip(
        ClipInstance,
        {"clip_id": "c1", "asset_id": "a1", "track_id": "v1",
         "timeline_start": 0, "timeline_duration": 10,
         "source_start": 0, "source_duration": 10}))
    check("edit.EditTrack", lambda: _roundtrip(
        EditTrack, {"track_id": "t1", "name": "V1", "track_type": "video"}))
    check("edit.TimelineSequence", lambda: _roundtrip(
        TimelineSequence, {"timeline_id": "t1", "name": "T"}))
    check("edit.SpeedParams", lambda: _roundtrip(
        SpeedParams, {"speed": 1.0}))
    check("edit.SubtitleClip", lambda: _roundtrip(
        SubtitleClip,
        {"clip_id": "sc1", "track_id": "s1",
         "timeline_start": 0, "timeline_duration": 10}))
    check("edit.TransformParams", lambda: _roundtrip(
        TransformParams, {}))

    from src.features.edit.core.effect_instance import EffectInstance
    check("edit.EffectInstance", lambda: _roundtrip(
        EffectInstance, {"effect_id": "e1", "effect_type": "blur"}))

    # ── 汇总 ────────────────────────────────────────────────
    dropped = [name for name, ok in results if not ok]
    for name, ok in results:
        print(f"  {'PASS' if ok else 'DROP'}  {name}")
    print(f"\n{len(results) - len(dropped)}/{len(results)} 层保留未知字段")
    if dropped:
        print("缺失层：", ", ".join(dropped))
    return 1 if dropped else 0


if __name__ == "__main__":
    raise SystemExit(main())
