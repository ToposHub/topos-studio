"""F2.10 · 摄影机视频与独立录音的源级同步绑定。

语义（对齐 DaVinci Resolve 的"外录 WAV 绑定到视频素材"）：

- ``AudioSyncBinding``：视频素材 → 独立 WAV 素材的绑定（offset/方法/
  保留机内声/置信度/声道映射）。挂 ``EditProject.audio_sync_bindings``
  （project 级 dict，与 source_patch/interpretation 同模式），经
  ``UpdateAudioSyncBindingCommand`` 单次领域事务提交，随工程持久化。
- **域区分**（计划原文）：不是 ``MulticamAngle.sync_offset``（多机位
  角度域）、不是 ``linked_clip_id``（clip 链接域）、不影响播放时钟。
- offset_seconds：WAV 内容相对视频素材 0 点提前量（WAV 提前开录 → 正，
  视频第 t 秒的内容出现在 WAV 第 t+offset 秒处）。
- ``keep_camera_audio``：True = 保留机内参考声（绑定信息留存，音频消费
  暂不切换——双源混音属混音图后续片）；False = 音频消费切换到 WAV。
- 非破坏：全程只写项目数据，原视频/WAV 文件零改写。
- ``resolve_audio_source(project, asset)``：五个音频消费点（播放/成片/
  Stem/源监视器预听/波形）统一取源入口。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Tuple


@dataclass(frozen=True)
class AudioSyncBinding:
    """视频素材的外录音频绑定（不可变，命令栈替换）。"""

    wav_asset_id: str
    #: WAV 相对视频 0 点的提前量（秒，提前开录为正）
    offset_seconds: float = 0.0
    #: "waveform" | "timecode" | "manual"（身份/审计）
    method: str = "manual"
    #: False = 音频消费切换到 WAV；True = 保留机内参考声（仅留存绑定）
    keep_camera_audio: bool = False
    #: 波形法置信度留存（TC/manual = 1.0）
    confidence: float = 1.0
    #: 多声道 WAV 的源声道选取（复用 SourcePatchSpec.audio_channel_map
    #: 形状 ((src, dst), ...)，None=直通）
    audio_channel_map: Optional[Tuple[Tuple[int, int], ...]] = None
    #: M03.3 · 双源混音显式声明（"" = 按 keep_camera_audio 推导：
    #: False→"replace"、True→"camera_only"；显式值 "replace"/
    #: "camera_only"/"dual"。**"dual"（机内+WAV 叠加）当前未声明
    #: 支持**——validate 拒绝，消费方不得隐式叠加两路）
    mix_mode: str = ""

    def to_dict(self) -> dict:
        return {
            "wav_asset_id": self.wav_asset_id,
            "offset_seconds": self.offset_seconds,
            "method": self.method,
            "keep_camera_audio": self.keep_camera_audio,
            "confidence": self.confidence,
            "audio_channel_map": (
                [list(pair) for pair in self.audio_channel_map]
                if self.audio_channel_map is not None else None),
            "mix_mode": self.mix_mode,
        }

    @staticmethod
    def from_dict(data: dict) -> "AudioSyncBinding":
        raw_map = data.get("audio_channel_map")
        method = str(data.get("method", "manual"))
        if method not in ("waveform", "timecode", "manual"):
            method = "manual"
        mix_mode = str(data.get("mix_mode", "") or "")
        if mix_mode not in ("", "replace", "camera_only"):
            mix_mode = ""  # 未知载荷保留策略：回落推导值（不丢绑定）
        return AudioSyncBinding(
            wav_asset_id=str(data.get("wav_asset_id", "")),
            offset_seconds=float(data.get("offset_seconds", 0.0) or 0.0),
            method=method,
            keep_camera_audio=bool(data.get("keep_camera_audio", False)),
            confidence=float(data.get("confidence", 1.0) or 1.0),
            audio_channel_map=(
                tuple((int(s), int(d)) for s, d in raw_map)
                if raw_map is not None else None),
            mix_mode=mix_mode,
        )

    def validate(self, video_duration: Optional[float] = None) -> None:
        """绑定自洽性：非空 WAV id、offset 有限且在合理范围、方法合法。"""
        if not self.wav_asset_id:
            raise ValueError("audio sync binding requires wav_asset_id")
        if self.method not in ("waveform", "timecode", "manual"):
            raise ValueError(f"unknown sync method: {self.method}")
        if self.offset_seconds != self.offset_seconds or \
                abs(self.offset_seconds) > 24 * 3600.0:
            raise ValueError(
                f"offset_seconds out of range: {self.offset_seconds}")
        # M03.3：双源混音未声明支持——显式拒绝（消费方不得隐式叠加）
        if self.mix_mode == "dual":
            raise ValueError(
                'mix_mode="dual"（机内声+WAV 叠加）未声明支持——'
                "双源音量关系未定义，请显式选择 replace 或 camera_only")


def dual_source_mix_declaration(
    binding: Optional[AudioSyncBinding],
) -> dict:
    """M03.3 · 双源混音显式声明（消费侧统一契约）。

    五个音频消费点（播放/成片/Stem/源监视器/波形）按本声明取源——
    混音关系不再是隐式行为：

    - 无绑定 → ``{"sources": ["camera"], "mix": "single"}``；
    - replace（keep_camera_audio=False）→ 机内声被 WAV 替换；
    - camera_only（keep_camera_audio=True）→ 绑定留存但消费机内声；
    - "dual" → **未声明支持**：返回 ``{"unsupported": "dual"}`` 供
      消费方显式拒绝/诊断（不是回退某一路冒充成功）。
    """
    if binding is None:
        return {"sources": ["camera"], "camera_audio": True,
                "mix": "single"}
    mode = binding.mix_mode or (
        "camera_only" if binding.keep_camera_audio else "replace")
    if mode == "dual":
        return {"unsupported": "dual",
                "detail": "机内声+WAV 叠加未声明支持（音量关系未定义）"}
    if mode == "camera_only":
        return {"sources": ["camera"], "camera_audio": True,
                "mix": "camera_only", "binding_retained": True}
    return {"sources": ["external_wav"], "camera_audio": False,
            "mix": "replace", "wav_asset_id": binding.wav_asset_id}


#: 绑定的音频消费统一入口 ────────────────────────────────────────────────


def binding_for_asset(overrides: dict, asset_id: str) -> Optional[AudioSyncBinding]:
    """取视频素材的外录绑定（无/未知载荷/保留机内声 = None=不切换源）。"""
    value = overrides.get(asset_id) if overrides else None
    if isinstance(value, AudioSyncBinding) and not value.keep_camera_audio:
        return value
    return None


def resolve_audio_source(
    project: Any, asset: Any,
) -> Tuple[str, float]:
    """素材的音频消费源：``(path, offset_seconds)``。

    有生效绑定时返回 WAV 路径与提前量（WAV 提前开录 → 消费 WAV 时加
    offset 秒定位）；否则 ``(asset.path, 0.0)``。五个消费点（播放/成片/
    Stem/源监视器/波形）共用，保证"预听、插入/修剪、保存重开、成片
    保持同一关系"。
    """
    binding = binding_for_asset(
        getattr(project, "audio_sync_bindings", None) or {},
        getattr(asset, "asset_id", ""),
    )
    if binding is not None and project is not None:
        wav = project.assets.get(binding.wav_asset_id)
        wav_path = getattr(wav, "path", None) if wav is not None else None
        if wav_path:
            return wav_path, binding.offset_seconds
    return getattr(asset, "path", ""), 0.0


def bound_audio_frame(
    source_frame: int, offset_seconds: float, source_fps: float,
) -> int:
    """视频源帧 → WAV 等效帧（同 fps 时钟下的偏移折算）。

    解码器按 frame/fps 定位秒；WAV 内该内容位于 source_frame/fps +
    offset 秒 → 等效帧 = source_frame + offset*fps。
    """
    if not offset_seconds or source_fps <= 0:
        return int(source_frame)
    return int(round(source_frame + offset_seconds * source_fps))


def serialize_audio_sync_bindings(bindings: Any) -> dict:
    """绑定 → 可 JSON 化 dict；未知载荷原样保留（重存不丢）。"""
    if not bindings:
        return {}
    result: dict = {}
    for asset_id, value in bindings.items():
        result[asset_id] = (
            value.to_dict() if isinstance(value, AudioSyncBinding) else value)
    return result


def deserialize_audio_sync_bindings(raw: Any) -> dict:
    """落盘 dict → 绑定；仅识别已知 schema（含 wav_asset_id 键）。"""
    if not isinstance(raw, dict):
        return {}
    result: dict = {}
    for asset_id, payload in raw.items():
        if isinstance(payload, dict) and "wav_asset_id" in payload:
            try:
                result[asset_id] = AudioSyncBinding.from_dict(payload)
                continue
            except Exception:
                pass
        result[asset_id] = payload
    return result


def timecode_seconds_from_meta(meta: dict, default_fps: float = 25.0) -> Optional[float]:
    """M03（S06）：metadata["timecode"] → 秒（帧率取 metadata["fps"]，
    缺省 default_fps）。视频/WAV 两侧共用同一折算（此前两处各自硬编码
    /25.0）。"""
    tc = (meta or {}).get("timecode")
    if not tc:
        return None
    from src.features.edit.core.interchange import parse_timecode
    try:
        hh, mm, ss, ff = parse_timecode(str(tc))
    except ValueError:
        return None
    fps = float((meta or {}).get("fps") or 0) or float(default_fps) or 25.0
    if fps <= 0:
        fps = 25.0
    return hh * 3600.0 + mm * 60.0 + ss + ff / fps


#: 半日阈值：TC 是 24h 环，朴素偏移绝对值超过该值时按环上最短表达
#: 解释（跨日录制）；更长偏移属真实跨日隔断，声明交人工不静默改。
_TC_WRAP_THRESHOLD_S = 12 * 3600.0


def timecode_offset_with_wrap(video_tc: str, wav_tc: str,
                              ) -> "tuple[float, dict]":
    """M03.5 · 跨日 TC 偏移声明（午夜翻转的显式假设，不静默）。

    朴素 offset = wav − video 在跨日录制（23:59:xx → 次日 00:01:xx）
    下给出 −86xxx 秒的荒谬值；TC 域是 24 小时环，|朴素偏移| 超过
    **半日阈值（显式假设）** 时按环上最短表达解释（±24h 归一）。

    Returns:
        (offset_seconds, declaration)——declaration：
        ``{"wrapped": bool, "raw_offset": float,
           "threshold_s": 12*3600,
           "caveat": "" | "跨日翻转假设（半日阈值归一）——更长偏移"
                     "属跨日隔断，请人工确认"}``。
    非法时码抛 ValueError（与 parse_timecode 同域）。
    """
    from src.features.edit.core.interchange import parse_timecode
    vh, vm, vs, vf = parse_timecode(str(video_tc))
    wh, wm, ws, wf = parse_timecode(str(wav_tc))
    video_s = vh * 3600.0 + vm * 60.0 + vs + vf / 25.0
    wav_s = wh * 3600.0 + wm * 60.0 + ws + wf / 25.0
    raw = wav_s - video_s
    offset = raw
    wrapped = False
    if raw < -_TC_WRAP_THRESHOLD_S:
        offset = raw + 24 * 3600.0
        wrapped = True
    elif raw > _TC_WRAP_THRESHOLD_S:
        offset = raw - 24 * 3600.0
        wrapped = True
    return offset, {
        "wrapped": wrapped,
        "raw_offset": raw,
        "threshold_s": _TC_WRAP_THRESHOLD_S,
        "caveat": ("跨日翻转假设（半日阈值归一）——更长偏移属跨日隔断，"
                   "请人工确认" if wrapped else ""),
    }


def wav_timecode_seconds(asset: Any, path: str) -> Optional[float]:
    """F2.10：WAV 的 TC 秒——metadata["timecode"] 优先，缺则读 BWF bext。

    bext TimeReference 是"第一个音频样本的采样索引"（外录提前开录的
    权威锚点）；折算秒需要采样率（metadata audio_info.sample_rate，
    缺省 48000）。

    M03（S06）：TC 帧部分按素材声明的 TC 帧率折算（metadata["fps"]），
    缺省 25——此前硬编码 /25.0，24fps 的 "00:00:01:12" 得 1.48s 而非
    1.5s（12/24=0.5）。
    """
    meta = getattr(asset, "metadata", None) or {}
    tc_seconds = timecode_seconds_from_meta(meta)
    if tc_seconds is not None:
        return tc_seconds
    try:
        from src.features.edit.core.bwf import read_bext_chunk
        bext = read_bext_chunk(path)
    except Exception:
        return None
    if not bext:
        return None
    ref = getattr(bext, "time_reference_samples", None)
    if ref is None:
        return None
    audio_info = meta.get("audio_info") or {}
    rate = float(audio_info.get("sample_rate") or 48000) or 48000.0
    return float(ref) / rate
