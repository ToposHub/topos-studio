"""Capability Registry — 机器可生成的产品能力注册表（P1-17）。

背景（计划表 P1-17）：互操作能力声明与实际实现存在分裂——
``interchange.py`` 的能力表是静态常量（生产代码零消费），
多个模块带 DORMANT 头注释休眠，BWF 的 available=True 与 writer
实际能力之间只靠单测绑定。用户可见的"支持"没有统一来源。

本模块建立单一事实来源：

- 每个能力条目有明确状态（available / partial / dormant / unverified /
  unavailable）与**证据链**（实现文件 + 测试文件）；
- 状态由**运行时探测**得出（BWF writer 探测、效果注册健康检查、
  可选依赖探测），而不是手写字面量；
- ``find_status_drift()`` 把注册表与 ``interchange.py`` 静态表的分歧
  作为机器可查报告输出——分裂从"测试碰巧覆盖"变成"每次可检测"；
- UI / 文档 / 导出菜单应只读取本注册表，不得各自硬编码能力声明。

约束：
- 探测必须廉价（惰性导入、无 Qt、无 GPU），可在任何线程调用；
- 注册表是纯数据（frozen dataclass），构建失败降级为条目级错误，
  绝不抛出中断调用方。
"""

from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

CAPABILITY_REGISTRY_SCHEMA = "topos.capability_registry.v1"


class CapabilityStatus(str, Enum):
    """能力状态（由探测与证据决定，不手写）。"""

    AVAILABLE = "available"      # 已实现、有测试证据、有生产入口
    PARTIAL = "partial"          # 已实现但仅覆盖子集 / 仅离线 / 明确降级
    DORMANT = "dormant"          # 代码与测试存在，但无生产入口（有意休眠）
    UNVERIFIED = "unverified"    # 代码存在，缺少验收证据
    UNAVAILABLE = "unavailable"  # 未实现


@dataclass(frozen=True)
class Capability:
    """单个产品能力条目。

    evidence 是"为什么敢声明这个状态"的指针列表：实现文件、生产入口
    （若有）、测试文件。用户可见的任何"支持"都必须能落到这里。
    """

    capability_id: str
    title: str
    status: CapabilityStatus
    evidence: Tuple[str, ...] = ()
    notes: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "capability_id": self.capability_id,
            "title": self.title,
            "status": self.status.value,
            "evidence": list(self.evidence),
            "notes": self.notes,
        }


@dataclass(frozen=True)
class CapabilityStatusDrift:
    """注册表与 interchange 静态能力表之间的分歧记录。"""

    capability_id: str
    registry_status: CapabilityStatus
    interchange_available: bool
    detail: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "capability_id": self.capability_id,
            "registry_status": self.registry_status.value,
            "interchange_available": self.interchange_available,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class CapabilityRegistry:
    """一次探测生成的注册表快照。"""

    capabilities: Tuple[Capability, ...] = field(default_factory=tuple)
    generated_at: str = ""
    probe_errors: Tuple[str, ...] = field(default_factory=tuple)

    def get(self, capability_id: str) -> Optional[Capability]:
        for cap in self.capabilities:
            if cap.capability_id == capability_id:
                return cap
        return None

    def by_status(self, status: CapabilityStatus) -> Tuple[Capability, ...]:
        return tuple(c for c in self.capabilities if c.status == status)

    @property
    def is_healthy(self) -> bool:
        """无探测错误，且所有条目都带证据。"""
        return not self.probe_errors and all(c.evidence for c in self.capabilities)

    def to_dict(self) -> Dict[str, object]:
        return {
            "schema": CAPABILITY_REGISTRY_SCHEMA,
            "generated_at": self.generated_at,
            "capabilities": [c.to_dict() for c in self.capabilities],
            "probe_errors": list(self.probe_errors),
        }


# ─────────────────────────────────────────────────────────────────────────────
# 探测函数（每个可单独 monkeypatch / 单测）
# ─────────────────────────────────────────────────────────────────────────────

def probe_bwf_supported(errors: Optional[List[str]] = None) -> bool:
    """BWF writer 实际能力探测（P0-06 语义：声明跟随 writer，不手写）。

    通过 ``bwf.is_true_bwf_supported()`` 惰性探测；失败视为不支持，
    状态诚实降级为"WAV with RIFF INFO"。

    Args:
        errors: 可选的错误收集列表（AUD-15700）。探测**异常**（依赖缺失、
            writer 抛错）时向其追加一行机器可读描述，供注册表
            ``probe_errors`` 持久化——旧实现异常只落到 logger.warning，
            ``is_healthy`` 的"无探测错误"分支成为死代码。
            "writer 存在但不支持"不是探测错误，不写入。
    """
    try:
        from src.features.edit.core.bwf import is_true_bwf_supported
        return bool(is_true_bwf_supported())
    except Exception as exc:  # noqa: BLE001 - 探测失败=不可用，不中断调用方
        logger.warning("BWF capability probe failed; reporting as unsupported", exc_info=True)
        if errors is not None:
            errors.append(f"bwf: probe failed: {exc}")
        return False


def probe_audio_effect_health() -> Tuple[bool, int, str]:
    """音频内置效果注册健康（复用 P1-07 的注册健康检查）。

    Returns:
        (healthy, missing_count, detail) —— healthy=False 时 detail 说明
        缺失原因（注册表缺项 / DSP 后端缺失）。
    """
    try:
        from src.features.edit.core.effect_registry import get_effect_registry_health
        health = get_effect_registry_health()
        missing = health.missing
        if health.is_healthy:
            return True, 0, ""
        reasons = "; ".join(
            f"{s.effect_type}: {s.reason}" for s in missing
        )[:400]
        return False, len(missing), health.registry_error or reasons
    except Exception as exc:  # noqa: BLE001
        logger.warning("Audio effect health probe failed", exc_info=True)
        return False, -1, f"probe error: {exc}"


def probe_optional_dependency(module_name: str) -> bool:
    """可选依赖是否安装（不导入模块本体，仅 find_spec）。"""
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError):
        return False


# ─────────────────────────────────────────────────────────────────────────────
# 注册表构建
# ─────────────────────────────────────────────────────────────────────────────

def _interchange_capabilities() -> Dict[str, bool]:
    """读取 interchange 静态能力表（name 前缀 → available）。"""
    from src.features.edit.core.interchange import INTERCHANGE_CAPABILITIES
    return {
        f.name.split("(")[0].strip().lower(): bool(f.available)
        for f in INTERCHANGE_CAPABILITIES
    }


def build_capability_registry() -> CapabilityRegistry:
    """执行全部探测并生成注册表快照。

    每次调用重新探测（不缓存），保证 UI / 导出菜单 / manifest
    拿到的状态与当下环境一致。
    """
    caps: List[Capability] = []
    probe_errors: List[str] = []

    # 1) 时间码 / drop-frame（P0-05 已修复的 SMPTE 参考实现）
    caps.append(Capability(
        capability_id="edit.timecode.drop_frame",
        title="SMPTE drop-frame 时间码往返",
        status=CapabilityStatus.AVAILABLE,
        evidence=(
            "src/features/edit/core/interchange.py (frames_to_timecode/timecode_to_frames)",
            "tests/edit/test_interchange.py (TestDropFrameTimecode)",
        ),
        notes="29.97/59.94 有理数 timebase，边界分钟/十分钟/小时 round-trip 全覆盖。",
    ))

    # 2) BWF（声明跟随 writer 实际能力）
    bwf_ok = probe_bwf_supported(probe_errors)
    caps.append(Capability(
        capability_id="edit.interchange.bwf",
        title="BWF (Broadcast Wave) 录音/交付",
        status=CapabilityStatus.AVAILABLE if bwf_ok else CapabilityStatus.PARTIAL,
        evidence=(
            "src/features/edit/core/bwf.py",
            "src/features/edit/core/recording_manager.py",
            "tests/edit/test_bwf.py",
        ),
        notes=(
            "EBU Tech 3285 bext（TimeReference/coding history），writer 默认注入。"
            if bwf_ok else
            "bext writer 不可用：实际交付物为 WAV with RIFF INFO metadata。"
        ),
    ))

    # 3) CSV Cue Sheet
    caps.append(Capability(
        capability_id="edit.interchange.csv_cue_sheet",
        title="CSV Cue Sheet",
        status=CapabilityStatus.DORMANT,
        evidence=(
            "src/features/edit/core/interchange.py (build_cue_sheet_from_sequence)",
            "tests/edit/test_interchange.py (TestCueSheet)",
        ),
        notes=(
            "F1.3/R4：实现与测试存在但无生产菜单/命令入口（全库仅测试引用），"
            "按注册表自身 available=有生产入口 的定义改列 DORMANT；"
            "interchange 静态表同步改 False。接入入口后升级 AVAILABLE。"
        ),
    ))

    # 4) Stem 导出（FE-B14）：File 菜单 export_stem_tracks 入口 +
    #    StemExportDialog 已接生产链路（file_controller.py），旧的
    #    DORMANT 声明过期——帮助/AI manifest 与菜单从此一致。
    caps.append(Capability(
        capability_id="edit.interchange.audio_stems",
        title="音频分轨 Stem 导出",
        status=CapabilityStatus.AVAILABLE,
        evidence=(
            "src/app/controllers/file_controller.py (export_stem_tracks 菜单入口)",
            "src/features/edit/ui/stem_export_dialog.py (StemExportDialog)",
            "src/features/edit/core/stem_export.py",
            "tests/edit/test_stem_export.py",
        ),
        notes="File 菜单入口 + 共享 AudioGraphRenderer 渲染链已接线。",
    ))

    # 5) FCPXML / OTIO 适配器（P1-16 已接线 File 菜单导入/导出入口）
    caps.append(Capability(
        capability_id="edit.interchange.fcpxml",
        title="FCPXML 导入/导出",
        status=CapabilityStatus.PARTIAL,
        evidence=(
            "src/features/edit/adapters/fcpxml_adapter.py",
            "src/features/edit/adapters/import_builder.py",
            "src/app/controllers/file_controller.py (import_interchange_timeline/export_timeline_fcpxml)",
            "tests/edit/ai_editing/test_fcpxml_adapter.py",
            "tests/edit/ai_editing/test_import_builder.py",
        ),
        notes=(
            "受控子集已接入生产入口（P1-16）：导入带预览确认/映射报告/"
            "缺失媒体转离线素材；导出显式列出丢弃项。multicam/audition/"
            "第三方 effect/精细音频 routing 等明确降级。"
        ),
    ))
    otio_dep = probe_optional_dependency("opentimelineio")
    caps.append(Capability(
        capability_id="edit.interchange.otio",
        title="OpenTimelineIO 导入/导出",
        status=CapabilityStatus.PARTIAL,
        evidence=(
            "src/features/edit/adapters/otio_adapter.py",
            "src/app/controllers/file_controller.py (import_interchange_timeline/export_timeline_otio)",
            "tests/edit/ai_editing/test_otio_adapter.py",
            "tests/edit/ai_editing/test_import_builder.py",
        ),
        notes=(
            "可选依赖 opentimelineio 已安装；P1-16 已接线 File 菜单入口。"
            if otio_dep else
            "可选依赖 opentimelineio 未安装：入口预检禁用并提示安装"
            "（graceful degrade）。"
        ),
    ))

    # 6) AAF / OMF（评估未实现）
    caps.append(Capability(
        capability_id="edit.interchange.aaf",
        title="AAF 交换",
        status=CapabilityStatus.UNAVAILABLE,
        evidence=("src/features/edit/core/interchange.py (INTERCHANGE_CAPABILITIES 评估)",),
        notes="需 aaf2（BSD-3），评估优先级低于 FCPXML。",
    ))
    caps.append(Capability(
        capability_id="edit.interchange.omf",
        title="OMF 交换",
        status=CapabilityStatus.UNAVAILABLE,
        evidence=("src/features/edit/core/interchange.py (INTERCHANGE_CAPABILITIES 评估)",),
        notes="评估不实现，建议导出 AAF 或 CSV。",
    ))

    # 7) 音频内置效果（探测注册健康，复用 P1-07）
    healthy, missing, detail = probe_audio_effect_health()
    if missing < 0:
        # AUD-15700: 探测异常（missing == -1）写入 probe_errors；
        # 注册表缺项（missing >= 1）是条目级降级，记录在条目 notes。
        probe_errors.append(f"audio_effects: {detail}")
    caps.append(Capability(
        capability_id="edit.audio_effects",
        title="内置音频混音效果（EQ/压缩/限制/混响等）",
        status=CapabilityStatus.AVAILABLE if healthy else CapabilityStatus.PARTIAL,
        evidence=(
            "src/features/edit/core/effect_registry.py (get_effect_registry_health)",
            "src/features/edit/core/audio_dsp.py (_ensure_effect_registry)",
            "tests/edit/test_audio_effect_registry_health.py",
        ),
        notes="" if healthy else f"缺失 {missing} 项：{detail}",
    ))

    # 8) 保调变速（P1-08 离线 WSOLA + F3.2 实时流式 WSOLA）
    caps.append(Capability(
        capability_id="edit.audio.preserve_pitch",
        title="保持音调变速（preserve-pitch）",
        status=CapabilityStatus.AVAILABLE,
        evidence=(
            "src/features/edit/core/audio_timestretch.py (wsola_time_stretch / StreamingWsola)",
            "src/features/edit/core/timeline_export.py (_build_preserved_pitch_audio)",
            "src/features/edit/core/audio_playback.py (_decode_preserve_pitch)",
            "tests/edit/test_audio_timestretch.py",
            "tests/edit/test_streaming_wsola.py",
        ),
        notes=(
            "预览（流式 WSOLA）与导出（整段 WSOLA）同 25ms 帧/7.5ms 搜索参数，"
            "互相关 ≥ 0.95；实时算法延迟 ≈ 32ms 由首块读前补偿；速度 0.1–8.0；"
            "多声道共用对齐；倒放/速度曲线回退变调并留痕（预览/导出同策略）。"
            "自研 numpy 实现，无第三方许可约束。"
        ),
    ))
    # F2.8：时间线变速插值质量声明（此前注册表零速度/插值条目，唯一可见
    # 声明是属性面板下拉——帮助/AI/预检不可见）。
    caps.append(Capability(
        capability_id="edit.video.retime_interpolation",
        title="时间线变速插值（nearest / frame blend）",
        status=CapabilityStatus.AVAILABLE,
        evidence=(
            "src/features/edit/core/timeline_frame_mapper.py (map_cfr_frame / _apply_frame_blend_layer)",
            "src/features/edit/core/timeline_preview_renderer.py (_retime_interp_requires_cpu GPU 门控)",
            "src/features/edit/core/timeline_export.py (CPU 导出帧混合 + GPU 显式让位)",
            "tests/edit/test_frame_blend.py",
            "tests/edit/test_retime_interpolation_quality.py",
        ),
        notes=(
            "nearest 全链；frame_blend 预览 GPU 帧整帧回退 CPU、导出 CPU 混合"
            "（预览/成片一致）；optical_flow 未实现——UI 隐藏、渲染端显式告警"
            "后按 nearest，不静默降级。受控光流 provider 登记 F7.2。"
        ),
    ))
    # F1.3/R5/R6：补齐音频域真实能力条目（此前不在注册表，帮助/AI 不可见）。
    caps.append(Capability(
        capability_id="edit.audio.loudness",
        title="响度分析（BS.1770-4 / R128）与真峰值",
        status=CapabilityStatus.AVAILABLE,
        evidence=(
            "src/features/edit/core/loudness_meter.py (LoudnessMeter 实时表)",
            "src/features/edit/core/loudness_analyzer.py (analyze_loudness + 4x 过采样真峰值 + 取消)",
            "src/app/controllers/edit_controller.py (素材归一化 worker)",
            "src/features/edit/core/timeline_export.py (导出响度归一)",
            "tests/edit/test_loudness_meter.py",
            "tests/edit/test_loudness_analyzer.py",
        ),
        notes="预览实时表 + 素材归一化 + 导出归一三入口齐备；取消三处齐备。",
    ))
    caps.append(Capability(
        capability_id="edit.audio.recording_adr",
        title="录音与 ADR Take",
        status=CapabilityStatus.AVAILABLE,
        evidence=(
            "src/features/edit/core/recording_manager.py (RecordingManager，BWF bext)",
            "src/features/edit/ui/adr_take_sheet_panel.py (ADR 面板)",
            "src/app/controllers/edit_controller.py (arm/start/take 导入单步 Undo)",
            "tests/edit/test_recording_manager.py",
            "tests/edit/test_adr_take_chain.py",
        ),
        notes="pre/post-roll、loop、延迟校准、BWF、take 单步撤销。",
    ))
    caps.append(Capability(
        capability_id="edit.audio.graph_validation",
        title="音频图校验门（编辑/导出路由）",
        status=CapabilityStatus.AVAILABLE,
        evidence=(
            "src/features/edit/core/audio_graph_snapshot.py (build + revision + hash)",
            "src/features/edit/ui/edit_page.py (P0-02 路由激活门)",
            "src/features/edit/core/timeline_export.py (导出前路由门禁)",
            "tests/edit/test_audio_graph_snapshot.py",
        ),
        notes="非法音频图在编辑激活与导出前被显式拒绝（不静默丢路由）。",
    ))
    caps.append(Capability(
        capability_id="edit.audio.health_report",
        title="音频健康报告",
        status=CapabilityStatus.DORMANT,
        evidence=(
            "src/features/edit/core/audio_health.py (build_audio_graph_snapshot 调用无生产入口)",
            "tests/edit/test_audio_health_report.py",
        ),
        notes="F1.3/R5：实现与测试存在、无生产消费者，如实列 DORMANT。",
    ))

    # 9) 多机位 / 嵌套序列（阶段 3 计划项，dormant）
    caps.append(Capability(
        capability_id="edit.multicam",
        title="多机位（Multicam）",
        status=CapabilityStatus.PARTIAL,
        evidence=(
            "src/features/edit/core/multicam.py (build_multicam_group_from_assets / stamp_multicam_group_on_assets)",
            "src/features/edit/core/multicam_sync.py (compute_sync_alignments / MulticamSyncWorker)",
            "src/features/edit/core/commands.py (SwitchMulticamAngleCommand / UpdateClipSourceCommand)",
            "src/features/edit/ui/multicam_panel.py (MulticamPanel 角度网格 + 波形同步)",
            "src/app/controllers/edit_controller.py (_on_create_multicam_group / _switch_multicam_angle / _on_multicam_sync_requested)",
            "src/features/edit/ui/timeline_view.py (数字键 1-9 切换)",
            "tests/edit/test_multicam.py",
            "tests/edit/test_multicam_production.py",
            "tests/edit/test_multicam_sync.py",
        ),
        notes=(
            "P1-01 已接线：媒体池多选创建组（定义挂成员素材 metadata，时间线片段"
            "始终指向真实角度素材——渲染零改动、split/trim 天然保持组归属）、"
            "角度面板 + 键盘 1-9 回放中实时切换、切换点经 SwitchMulticamAngleCommand"
            "（split+换源）可编辑/可撤销/保存重开保持；波形自动同步（互相关各角度"
            "音频起始段，后台 worker，偏移写回组并作用于后续切换）；角度网格监看"
            "（各角度在 playhead 的静帧，映射与切换命令一致，250ms 去抖）。"
            "限制：网格为静帧而非多路实时解码、无时间码/标记同步、音频不跟随切换、"
            "重同步不追溯平移已放置片段。"
            "创建组和片段共享时间线历史的一步撤销；波形同步偏移走工程历史，"
            "可独立撤销，源身份变化的后台结果会被拒绝。"
        ),
    ))
    caps.append(Capability(
        capability_id="edit.nested_sequence",
        title="嵌套序列 / Compound Clip",
        status=CapabilityStatus.PARTIAL,
        evidence=(
            "src/features/edit/core/nested_sequence.py (build_nested_from_selection / flatten_nested_audio_tracks)",
            "src/features/edit/core/timeline_preview_renderer.py (_render_nested_sequence_frame + set_sequence_resolver)",
            "src/features/edit/core/timeline_export.py (_render_nested_sequence_layer / expand_sequence_audio_for_render)",
            "src/features/edit/ui/edit_page.py (set_audio_sequence_resolver + 嵌套音频展开)",
            "src/app/controllers/edit_controller.py (_on_create_nested_from_selection/_on_enter_nested_sequence)",
            "src/app/controllers/file_controller.py (音频 resolver 注入)",
            "tests/edit/test_nested_sequence.py",
            "tests/edit/test_nested_sequence_production.py",
            "tests/edit/test_nested_audio_flatten.py",
        ),
        notes=(
            "P1-02 已接线：右键'从选择创建嵌套序列'/双击进入/退出（时间线下拉）、"
            "循环嵌套阻断、预览 resolver 实时更新、导出递归渲染、保存重开 round-trip；"
            "嵌套音频参与父时间线混音（实时/导出/Stem 共用 flatten 纯函数：子序列"
            "音频轨平移进父坐标、源窗口钳制、轨参数/自动化/Bus 拓扑保留）。"
            "创建的父片段、子序列引用与嵌套素材共享时间线历史的一步撤销；"
            "后续剪辑操作按顺序撤销，子序列已被独立修改时拒绝覆盖。"
            "限制：父嵌套片段变速/倒放/速度曲线时音频跳过、子轨转场不映射。"
        ),
    ))

    # P2-1：Cloud / Power Bins / 跨工程共享——协议已设计、实现为零。
    # UNAVAILABLE 是诚实声明：服务具备前 UI 不得出现任何入口。
    caps.append(Capability(
        capability_id="cloud_power_bins",
        title="Cloud / Power Bins（跨工程共享）",
        status=CapabilityStatus.UNAVAILABLE,
        evidence=(
            "docs/architecture/media-cloud-sharing-protocol.md (协议设计稿)",
        ),
        notes=(
            "未实现。身份/同步/冲突/权限/离线缓存/配额协议见设计文档；"
            "具备云服务后按协议实施并把本条目升级（补实现/测试/生产"
            "入口 evidence）。在此之前任何 UI 不得声明可用。"
        ),
    ))

    return CapabilityRegistry(
        capabilities=tuple(caps),
        generated_at=datetime.now(timezone.utc).isoformat(),
        probe_errors=tuple(probe_errors),
    )


# interchange 静态表 name 前缀 → 注册表 id 映射（drift 检测用）
_INTERCHANGE_ID_MAP = {
    "bwf": "edit.interchange.bwf",
    "csv cue sheet": "edit.interchange.csv_cue_sheet",
    "audio stems": "edit.interchange.audio_stems",
    "final cut pro xml": "edit.interchange.fcpxml",
    "aaf": "edit.interchange.aaf",
    "omf": "edit.interchange.omf",
}


def find_status_drift(registry: Optional[CapabilityRegistry] = None) -> Tuple[CapabilityStatusDrift, ...]:
    """报告注册表与 interchange 静态能力表的分歧。

    静态表只有 available 布尔；注册表状态更细。AUD-15701：分歧采用
    分档语义，两个方向都可检测——

    - **高估**（静态表 available=True，注册表非 AVAILABLE）：静态表
      过度承诺（dormant/partial/unavailable）；
    - **低估**（静态表 available=False，注册表 AVAILABLE 或 PARTIAL）：
      注册表已实现/已接线（如 FCPXML P1-16 已接 File 菜单生产入口），
      静态表仍声明不可用——旧实现把 PARTIAL 折叠进"不可用"，该方向的
      分歧永不报告。

    出现分歧即说明两处声明之一已过期，应以注册表为准并更新静态表
    （或接线生产入口后同步两边）。
    """
    if registry is None:
        registry = build_capability_registry()
    try:
        table = _interchange_capabilities()
    except Exception as exc:  # noqa: BLE001
        logger.warning("interchange table unavailable for drift check", exc_info=True)
        return (
            CapabilityStatusDrift(
                capability_id="<interchange-table>",
                registry_status=CapabilityStatus.UNVERIFIED,
                interchange_available=False,
                detail=f"静态表读取失败：{exc}",
            ),
        )

    drifts: List[CapabilityStatusDrift] = []
    for prefix, cap_id in _INTERCHANGE_ID_MAP.items():
        table_available = table.get(prefix)
        if table_available is None:
            continue
        cap = registry.get(cap_id)
        if cap is None:
            continue
        # F1.3/R1：PARTIAL=受控子集已交付生产入口，与静态表布尔
        # available=True 语义一致，不再计为漂移（FCPXML 永久漂移修复）。
        if table_available and cap.status not in (
            CapabilityStatus.AVAILABLE,
            CapabilityStatus.PARTIAL,
        ):
            drifts.append(CapabilityStatusDrift(
                capability_id=cap_id,
                registry_status=cap.status,
                interchange_available=table_available,
                detail=(
                    f"interchange 静态表 available=True，"
                    f"注册表探测状态={cap.status.value}（静态表疑似高估，以注册表为准）"
                ),
            ))
        elif not table_available and cap.status in (
            CapabilityStatus.AVAILABLE,
            CapabilityStatus.PARTIAL,
        ):
            drifts.append(CapabilityStatusDrift(
                capability_id=cap_id,
                registry_status=cap.status,
                interchange_available=table_available,
                detail=(
                    f"interchange 静态表 available=False，"
                    f"注册表探测状态={cap.status.value}"
                    f"{'（已接生产入口）' if '生产入口' in cap.notes or '已接线' in cap.notes else ''}"
                    f"（静态表疑似低估，以注册表为准）"
                ),
            ))
    return tuple(drifts)


def get_capability_registry() -> CapabilityRegistry:
    """对外主入口：执行探测并返回注册表快照。"""
    return build_capability_registry()
