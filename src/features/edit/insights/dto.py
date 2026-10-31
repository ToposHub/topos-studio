"""InsightSnapshot v1 —— 只读洞察快照 DTO（T01）。

设计约束（计划 §2.3，逐条对应）：

1. **深冻结**：所有字段为 str/int/float/bool/None 或由它们组成的
   tuple；不含 dict（除只读 property 派生）、不含宿主模型对象/QObject/
   GPU 句柄。capture 阶段即深复制白名单字段，此后宿主继续编辑不影响
   已冻结 DTO。
2. **半开区间**：所有时间线/源侧帧区间都是 ``[start, end)``；``end =
   start + duration``。
3. **精确时间基**：fps 以分子/分母有理数传递，附 DF 标志；不引入
   float 时间码公式（显示用 ``src.shared.media.timecode.Timecode``）。
4. **身份**：稳定 ID 一律用宿主真实 ID（project/timeline/track/clip/
   cue/marker/asset 的 uuid），不把机器路径当 ID；路径仅以 basename
   脱敏形式出现（沿用 ``snapshot_service._redact_path`` 纪律）。
5. **加载范围显式化**：ref 存在但 sequence 未物化 → ``load_state =
   "unloaded"``，其 ``duration_frames`` 为 None（未收集，不是 0）。
6. **hash 双轨**：``payload_hash`` 校验完整规范载荷（除 hash 字段本身，
   含 capture_id/时间/revision）；``content_hash`` 只取逻辑内容（不含
   捕获时间/ID、revision 计数、导航状态），供 T15 对照"编辑内容是否
   变化"。二者由 ``codec`` 计算与校验。
"""

from __future__ import annotations

from dataclasses import dataclass

# ── schema / 常量 ────────────────────────────────────────────────

SCHEMA_VERSION = "1"

# sequence 加载状态：loaded = ref+完整序列均已捕获；unloaded = ref 存在
# 但序列未物化（controller 懒加载），内容字段一律空、duration 为 None。
LOAD_STATE_LOADED = "loaded"
LOAD_STATE_UNLOADED = "unloaded"

# 源帧映射状态（T11/T13 消费口径）：
#   identity        —— 恒速 1.0 正放、无冻结、无速度点/曲线域，源区间直读；
#   constant        —— 恒定非 1.0 速度（线性映射，host 口径已明确）；
#   unknown         —— 速度点/曲线域/反向/冻结等复杂变速；对应映射尚未经
#                      M 线验收，工具不得自行推导逐帧映射（计划 T11 条件依赖）。
SOURCE_MAPPING_IDENTITY = "identity"
SOURCE_MAPPING_CONSTANT = "constant"
SOURCE_MAPPING_UNKNOWN = "unknown"

# 轨道/素材类型沿用宿主字符串值，不另造枚举
TRACK_TYPES = ("video", "audio", "subtitle")


@dataclass(frozen=True)
class FpsSpec:
    """有理帧率 + DF 标志（DF 判定复用 Timecode.auto_detect_type）。"""

    num: int
    den: int
    drop_frame: bool

    @property
    def to_float(self) -> float:
        return self.num / self.den


@dataclass(frozen=True)
class ProjectEntry:
    project_id: str
    name: str
    revision: int


@dataclass(frozen=True)
class AssetEntry:
    """素材池白名单条目（搜索/统计用日志字段齐备；路径只留 basename）。"""

    asset_id: str
    display_name: str
    media_type: str
    is_offline: bool
    file_basename: str
    reel: str = ""
    scene: str = ""
    shot: str = ""
    take: str = ""
    keywords: tuple[str, ...] = ()
    rating: int = 0
    flag: str = "none"
    color_label: str = "none"
    comment: str = ""


@dataclass(frozen=True)
class ClipEntry:
    """视频/音频轨上一个 clip 的只读条目（时间线与源侧均半开区间）。"""

    clip_id: str
    sequence_id: str
    track_id: str
    track_type: str  # video/audio
    asset_id: str
    timeline_start: int
    timeline_end: int  # [start, end)
    source_start: int
    source_end: int  # [start, end)
    is_enabled: bool
    source_mapping: str  # SOURCE_MAPPING_*
    reverse: bool = False
    constant_speed: float | None = None  # source_mapping == "constant" 时给出倍率
    linked_clip_id: str | None = None
    label_color: str | None = None

    @property
    def timeline_duration(self) -> int:
        return self.timeline_end - self.timeline_start

    @property
    def source_duration(self) -> int:
        return self.source_end - self.source_start


@dataclass(frozen=True)
class CueEntry:
    """字幕/对白条目（来自 subtitle 轨 SubtitleClip；文本为白名单字段）。"""

    clip_id: str
    sequence_id: str
    track_id: str
    text: str
    timeline_start: int
    timeline_end: int  # [start, end)
    is_enabled: bool = True

    @property
    def timeline_duration(self) -> int:
        return self.timeline_end - self.timeline_start


@dataclass(frozen=True)
class MarkerEntry:
    marker_id: str
    sequence_id: str
    frame: int
    name: str
    color: str
    comment: str
    kind: str
    clip_id: str | None = None
    track_id: str | None = None


@dataclass(frozen=True)
class TrackEntry:
    track_id: str
    sequence_id: str
    name: str
    track_type: str  # video/audio/subtitle
    index: int
    is_enabled: bool
    track_role: str = "standard"
    clips: tuple[ClipEntry, ...] = ()
    cues: tuple[CueEntry, ...] = ()


@dataclass(frozen=True)
class SequenceEntry:
    sequence_id: str
    name: str
    fps: FpsSpec
    width: int
    height: int
    sample_rate: int
    revision: int
    load_state: str  # LOAD_STATE_*
    duration_frames: int | None  # unloaded → None（未收集，不冒充 0）
    tracks: tuple[TrackEntry, ...] = ()
    markers: tuple[MarkerEntry, ...] = ()


@dataclass(frozen=True)
class InsightSnapshot:
    """一次只读捕获的完整结果（project 与全部序列同次捕获）。"""

    schema_version: str
    capture_id: str
    captured_at: float
    project: ProjectEntry
    sequences: tuple[SequenceEntry, ...]
    assets: tuple[AssetEntry, ...]
    payload_hash: str | None = None
    content_hash: str | None = None

    @property
    def sequence_revision_map(self) -> dict[str, int]:
        """计划 §2.3 要求的 sequence_revision_map（由条目派生，只读）。"""
        return {s.sequence_id: s.revision for s in self.sequences}

    def loaded_sequence_ids(self) -> tuple[str, ...]:
        return tuple(
            s.sequence_id for s in self.sequences if s.load_state == LOAD_STATE_LOADED
        )
