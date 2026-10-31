"""F2.1 · Source Patching 模型——源视频/音频开关与源声道→目标轨映射。

语义（对齐 DaVinci Resolve 的 Source Patching，本实现为首片：模型/命令/
持久化；渲染消费与 UI 接线见任务后续片）：

- ``video_enabled`` / ``audio_enabled``：插入/覆盖时是否携带对应流
  （video-only / audio-only patch）。关闭音频的有声源不再强制成对落轨。
- ``audio_channel_map``：源声道 → 目标轨内通道的显式映射（0 基）。空/
  None = 按源布局直通（现状行为）。例如 5.1 源只把源声道 0/1（L/R）送
  目标轨，或单选源声道 2（C）做 ADR 对轨。
- 映射是**用户选择并持久化**的素材级覆盖（EditProject.source_patch_
  overrides，经 UpdateSourcePatchCommand 提交），不是 UI 临时变量。
- 校验拒绝越界/重复目标通道（映射必须无歧义），未知布局退回直通。

声道顺序沿用 channel_layout（ITUR/ATSC A/52）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class SourcePatchSpec:
    """单个素材的源 patch 设置（不可变，命令栈替换）。"""

    video_enabled: bool = True
    audio_enabled: bool = True
    #: ((source_channel_index, target_channel_index), ...) 0 基；None=直通
    audio_channel_map: Optional[Tuple[Tuple[int, int], ...]] = None

    def to_dict(self) -> dict:
        return {
            "video_enabled": self.video_enabled,
            "audio_enabled": self.audio_enabled,
            "audio_channel_map": (
                [list(pair) for pair in self.audio_channel_map]
                if self.audio_channel_map is not None else None
            ),
        }

    @staticmethod
    def from_dict(data: dict) -> "SourcePatchSpec":
        raw_map = data.get("audio_channel_map")
        channel_map = (
            tuple((int(src), int(dst)) for src, dst in raw_map)
            if raw_map is not None else None
        )
        return SourcePatchSpec(
            video_enabled=bool(data.get("video_enabled", True)),
            audio_enabled=bool(data.get("audio_enabled", True)),
            audio_channel_map=channel_map,
        )

    def validate(self, n_source_channels: Optional[int]) -> None:
        """校验映射无歧义且（已知源通道数时）不越界。"""
        if self.audio_channel_map is None:
            return
        targets: set[int] = set()
        for src, dst in self.audio_channel_map:
            if src < 0 or dst < 0:
                raise ValueError(f"channel map indices must be >= 0: {(src, dst)}")
            if n_source_channels is not None and src >= n_source_channels:
                raise ValueError(
                    f"source channel {src} out of range (source has "
                    f"{n_source_channels} channels)")
            if dst in targets:
                raise ValueError(f"duplicate target channel {dst} in map")
            targets.add(dst)

    def resolve_source_channels(self, n_source_channels: int) -> Tuple[int, ...]:
        """按映射返回目标轨各通道对应的源声道索引（有序，供解码选道）。

        None/空映射 = 直通（目标通道数 = 源通道数，恒等）。
        """
        if not self.audio_channel_map:
            return tuple(range(n_source_channels))
        self.validate(n_source_channels)
        ordered = sorted(self.audio_channel_map, key=lambda pair: pair[1])
        return tuple(src for src, _dst in ordered)

    def target_channel_count(self, n_source_channels: int) -> int:
        if not self.audio_channel_map:
            return n_source_channels
        return len(self.audio_channel_map)

    def select_channels(self, samples):
        """按 ``audio_channel_map`` 对 ``(channels, n)`` 采样数组选道/重排。

        返回**新数组**（按目标通道序重排后的源声道行）；无映射、或实际
        声道数不足以覆盖映射基线（解码布局与设置映射时不符）时**原样
        返回同一对象**——调用方据此退回直通，且不得再向下游声明容器
        布局（选道后的布局是自定义的）。预览/导出/Stem 三条消费路径
        共用本方法。
        """
        if not self.audio_channel_map:
            return samples
        n_channels = samples.shape[0]
        for src, _dst in self.audio_channel_map:
            if src >= n_channels:
                return samples
        ordered = sorted(self.audio_channel_map, key=lambda pair: pair[1])
        return samples[[src for src, _dst in ordered], :]


#: 素材级默认（无覆盖时的行为 = 现状：双流全开、声道直通）
DEFAULT_SOURCE_PATCH = SourcePatchSpec()


def patch_for_asset(overrides: dict, asset_id: str) -> SourcePatchSpec:
    """取素材生效的 patch（无覆盖/未知载荷 = 默认直通）。

    旧包/未来 schema 的未知载荷（非 ``SourcePatchSpec``）会被 IO 层原样
    保留在 overrides 里以便重存，但运行时一律按默认直通处理。
    """
    spec = overrides.get(asset_id) if overrides else None
    if isinstance(spec, SourcePatchSpec):
        return spec
    return DEFAULT_SOURCE_PATCH


# ── M12.2b：目录迁移计划（代理/缓存目录变更的安全预检） ──────────────

def plan_cache_migration(
    entries: "list[tuple[str, str]]",
    *,
    project_active_paths: "set[str]",
) -> "dict[str, list[str]]":
    """代理/缓存目录迁移预检：哪些条目会被移动、哪些因活动引用冻结。

    Args:
        entries: (proxy_path, new_path) 候选迁移对。
        project_active_paths: 当前工程正在使用的源/代理路径集合
            （活动引用保护——这些条目**不迁移**，防播放中移动文件）。

    Returns:
        {"migrate": [(old, new), ...], "frozen_active": [...],
         "invalid": [(path, reason), ...]}
    """
    migrate: list[tuple[str, str]] = []
    frozen: list[str] = []
    invalid: list[tuple[str, str]] = []
    seen_new: set[str] = set()
    import os
    for old, new in entries:
        if not old or not new:
            invalid.append((old or "", "空路径"))
            continue
        if new in seen_new:
            invalid.append((old, f"目标路径重复：{new}"))
            continue
        seen_new.add(new)
        if old in project_active_paths:
            frozen.append(old)
            continue
        if not os.path.isfile(old):
            invalid.append((old, "源文件不存在"))
            continue
        migrate.append((old, new))
    return {"migrate": migrate, "frozen_active": frozen,
            "invalid": invalid}


__all__ = [
    "SourcePatchSpec",
    "DEFAULT_SOURCE_PATCH",
    "patch_for_asset",
    "plan_cache_migration",
]
