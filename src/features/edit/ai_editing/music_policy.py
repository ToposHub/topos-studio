"""TAEP Music Policy（Phase 9，补齐 §11.7）。

音乐是 AI 剪辑里最容易做出“看似自动、实际不可交付”的部分。本模块实现**保守**
的首版策略（TAEP §11.7）：

- 音乐只能从媒体池内 ``music`` bin 或已授权 catalog 的 ``MediaAsset`` 中选择。
- 必须显示授权/来源、时长、BPM（若有）和将要使用的范围。
- 未授权/未知来源音乐**没有“应用”按钮**。
- AI 只能提出 ``MusicDecision``、audio gain/ducking proposal；未实现正式 audio
  command 的能力必须显示“不支持”，不能伪造已添加音乐。
- BPM 缺失只降级显示，不编造数据。

TAEP §11.7 表格首版可做：从已导入且已授权的本地音乐素材中提议 1–3 首，并创建
音频 clip proposal。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from src.features.edit.ai_editing.editorial_models import FrameRange


class LicenseStatus(str, Enum):
    """音乐授权状态（TAEP §11.7：必须显示授权/来源）。"""

    LICENSED = "licensed"  # 已授权，可应用
    CLEARED = "cleared"  # 已清权（用户确认拥有版权）
    UNKNOWN = "unknown"  # 未知来源，不可应用
    PENDING = "pending"  # 待确认


@dataclass(frozen=True)
class MusicAsset:
    """媒体池里一个候选音乐素材（脱敏摘要）。

    BPM/情绪 metadata 缺失时为 None —— 只降级显示，不编造（TAEP §11.7）。
    """

    asset_id: str
    name: str
    duration_frames: int
    license_status: LicenseStatus = LicenseStatus.UNKNOWN
    bpm: float | None = None
    mood: str | None = None
    source_label: str = ""  # 来源/授权标识（如 "Epidemic Sound / 授权号"）

    @property
    def is_applicable(self) -> bool:
        """是否可应用（必须已授权/已清权，TAEP §11.7）。"""
        return self.license_status in (LicenseStatus.LICENSED, LicenseStatus.CLEARED)


@dataclass(frozen=True)
class MusicDecision:
    """AI 对音乐的提议（proposal only，不直接添加音频 clip）。

    TAEP §11.7：AI 只能提出 MusicDecision；未实现正式 audio command 前只输出建议，
    不能伪造已添加音乐。
    """

    decision_id: str
    music_asset: MusicAsset
    proposed_range: FrameRange
    reason: str = ""
    target_lufs: float | None = None
    ducking_rule: str | None = None  # 如 "duck voiceover by -6dB"
    state: str = "proposed"  # proposed/accepted/rejected

    @property
    def is_applicable(self) -> bool:
        return self.music_asset.is_applicable


class MusicPolicy:
    """音乐授权筛选与 AI 可用音乐清单（TAEP §11.7）。

    从媒体池筛选已授权音乐，供 variant_planner / decision inspector 使用。
    未授权音乐不出现在可用清单里（没有“应用”按钮）。
    """

    def __init__(self) -> None:
        self._catalog: dict[str, MusicAsset] = {}

    def register(self, music: MusicAsset) -> None:
        self._catalog[music.asset_id] = music

    def register_from_media_asset(self, asset: Any) -> MusicAsset:
        """从一个 MediaAsset 构造并注册 MusicAsset（读 metadata 里的授权/BPM）。

        AUD-14007："cleared"（已清权，用户自证版权）必须落 ``LicenseStatus.CLEARED``，
        不能并入 LICENSED（渠道授权）分支——TAEP §11.7 要求诚实展示授权来源，
        语义混同会让"已清权"素材被 UI 显示成"已授权"。
        """
        meta = getattr(asset, "metadata", {}) or {}
        license_str = str(meta.get("music_license", meta.get("license", "unknown"))).lower()
        if license_str in ("licensed", "授权", "已授权"):
            license_status = LicenseStatus.LICENSED
        elif license_str == "cleared":
            license_status = LicenseStatus.CLEARED
        elif license_str == "pending":
            license_status = LicenseStatus.PENDING
        else:
            license_status = LicenseStatus.UNKNOWN
        bpm = meta.get("bpm")
        music = MusicAsset(
            asset_id=getattr(asset, "asset_id", ""),
            name=getattr(asset, "display_name", ""),
            duration_frames=int(meta.get("duration_frames", 0) or 0),
            license_status=license_status,
            bpm=float(bpm) if bpm is not None else None,
            mood=meta.get("mood"),
            source_label=str(meta.get("music_source", "")),
        )
        self.register(music)
        return music

    def applicable_music(self) -> list[MusicAsset]:
        """返回所有可应用（已授权）的音乐，按 BPM/名称排序。"""
        return sorted(
            [m for m in self._catalog.values() if m.is_applicable],
            key=lambda m: (m.bpm is None, m.bpm or 0, m.name),
        )

    def all_music(self) -> list[MusicAsset]:
        """返回全部音乐（含未授权，UI 须标记不可应用）。"""
        return list(self._catalog.values())

    def propose(
        self,
        music: MusicAsset,
        proposed_range: FrameRange,
        *,
        reason: str = "",
        target_lufs: float | None = None,
        ducking_rule: str | None = None,
    ) -> MusicDecision:
        """生成一个音乐提议。

        若音乐未授权，提议仍生成但 ``is_applicable=False``，UI 据此隐藏“应用”。
        """
        import uuid as _uuid

        return MusicDecision(
            decision_id=f"music-{_uuid.uuid4().hex[:8]}",
            music_asset=music,
            proposed_range=proposed_range,
            reason=reason,
            target_lufs=target_lufs,
            ducking_rule=ducking_rule,
            state="proposed",
        )

    def best_matches(
        self, target_duration: int, target_mood: str | None = None, limit: int = 3
    ) -> list[MusicAsset]:
        """为目标时长/情绪匹配最佳音乐（仅已授权，TAEP §11.7：提议 1–3 首）。

        匹配优先级：mood 命中 > 时长足够 > BPM 接近 120。
        BPM/情绪缺失只降级排序，不编造。
        """
        candidates = self.applicable_music()
        # 时长足够覆盖目标
        candidates = [m for m in candidates if m.duration_frames >= target_duration]
        # 排序：mood 命中优先
        def _score(m: MusicAsset) -> tuple[int, int, float]:
            mood_hit = 0 if (target_mood and m.mood and target_mood.lower() in (m.mood or "").lower()) else 1
            bpm_dist = abs((m.bpm or 120) - 120) if m.bpm else 999
            return (mood_hit, 0 if m.bpm else 1, bpm_dist)
        candidates.sort(key=_score)
        return candidates[:limit]
