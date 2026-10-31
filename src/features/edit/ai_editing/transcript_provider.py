"""QwenASRProvider — 本地 ASR 转录 provider（R4-c / §16.3）。

把 **Qwen3-ASR + Qwen3-ForcedAligner + NVIDIA Sortformer** 适配为
:class:`AnalysisProvider`，输出：

- **segment + word timecode**（词/段时间码，M4 验收）。
- **语言 + 置信度**（每个 segment / word）。
- **匿名 speaker S1/S2**（diarization；绝不实名，§11.1 M4 红线）。

2026 R4-c：彻底替换旧的 WhisperX + pyannote 占位。Qwen3-ASR 是当前中文最优的
开源 ASR（Apache-2.0、52 语言、MLX 原生 Mac 加速）；Sortformer 是商用友好的说话人
分离（Apache-2.0，解决 pyannote CC-BY-NC-4.0 障碍）。

设计约束（遵循 .clinerules / §0 红线）：

1. **不把大模型加入基础安装**。``qwen_asr`` / ``nemo_toolkit`` 是可选依赖；本 provider
   在推理前用 ``importlib.util.find_spec`` 探测，缺失时抛
   :class:`ModelPackNotInstalledError`（**绝不静默返回空结果假装"已分析"**）。
2. **可降级，必标注**。无对齐模型 → 词级降级为段级 + ``confidence`` 下调 +
   ``provenance.extra={"alignment": "none"}``；无 Sortformer → speaker 全 None；不静默。
3. **PTS → frame 经 :class:`TimebaseService`**（M2 整数换算，绝不用秒 × fps）。
   Qwen3 / Sortformer 的时间戳是**秒**，适配器内 ``int(seconds * sample_rate)`` 转 sample。
4. **speaker 匿名化**。Sortformer 输出经 :func:`merge_diarization` 合并，speaker 仅
   ``S1``/``S2``/...（按首次出现顺序），重叠语音标 ``overlap=True`` 不丢弃。
5. **音频提取离线**。经 :class:`AudioExtractor`（ffmpeg subprocess → 16kHz mono wav），
   不在 UI 线程跑。

**推理开关**：默认走真实 Qwen3-ASR 推理；仅 ``TOPOS_ASR_REAL=0`` 时走确定性 stub（测试用）。
"""

from __future__ import annotations

import importlib.util
import logging
import os
import threading
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable

from src.features.edit.ai_editing.analysis_models import (
    AnalysisProvenance,
    AssetAnalysis,
    AssetFingerprint,
    FieldProvenance,
    TranscriptSegment,
    WordSegment,
    analysis_to_segments,
)
from src.features.edit.ai_editing.audio_extractor import (
    ASR_SAMPLE_RATE,
    AudioExtractionError,
    AudioExtractor,
)
from src.features.edit.ai_editing.editorial_models import SegmentKind, SemanticSegment
from src.features.edit.ai_editing.timebase import TimebaseService

logger = logging.getLogger(__name__)

_DEFAULT_ASR_MAX_NEW_TOKENS = 8192
_ASR_MODEL_PACKS = {
    # The plain repositories are the qwen-asr runtime format.  The ``-hf``
    # repositories are native-Transformers checkpoints and are not loadable
    # by qwen-asr==0.0.6 without a different backend.
    "small": ("qwen3_asr", "Qwen/Qwen3-ASR-0.6B"),
    "large": ("qwen3_asr_large", "Qwen/Qwen3-ASR-1.7B"),
}


# ---------------------------------------------------------------------- #
# 真实推理开关（统一策略）
# ---------------------------------------------------------------------- #


def _real_inference_enabled(env_var: str) -> bool:
    """是否走真实模型推理（统一策略，供 ASR / TTS / WhisperX 复用）。

    默认开启真实推理；仅当显式设为 ``"0"`` 时走确定性 stub（测试用）。
    旧的 ``"1"`` 仍被识别为"显式真实"，保持向后兼容（已设 =1 的代码/测试不受影响）。

    背景：历史上 ASR/TTS 用 ``TOPOS_*_REAL=1`` 选择性开启真实推理，但启动脚本与
    UI 都未设置该变量，导致生产环境默认走 stub（返回固定假数据）。翻转默认值后，
    生产环境直接走真实推理；测试用 ``=0`` 显式选 stub。
    """
    val = os.environ.get(env_var)
    if val is None:
        return True          # 默认真实推理
    return val != "0"        # "0" → stub；其余（"1" 等）→ 真实


def _emit_progress(progress: Any | None, fraction: float | None, message: str) -> None:
    """向分析宿主报告阶段进度；可观测性失败不能影响 ASR。"""
    if progress is None:
        return
    try:
        progress(fraction, message)
    except Exception:  # noqa: BLE001
        logger.debug("Transcript progress callback failed", exc_info=True)


# ---------------------------------------------------------------------- #
# Torch device 解析（跨平台：CUDA > MPS > CPU）
# ---------------------------------------------------------------------- #


def _resolve_torch_device() -> tuple[str | None, Any]:
    """返回 ``(device_map, dtype)``，按平台能力选择最优后端。

    优先级（互斥）：
      1. **CUDA**（Windows/Linux + NVIDIA，或 Linux + ROCm）→ ``cuda:0`` + ``bfloat16``。
         bfloat16 是 NVIDIA Ampere+ 原生支持的最优精度。
      2. **MPS**（macOS：Apple Silicon M 系列 / 黑苹果 AMD via Metal）→ 自动模式使用
         CPU + ``float32``。MPS + ``float16`` 在部分 macOS/PyTorch 组合上会出现“模型
         加载成功但转写乱码”，因此只有显式设置 ``TOPOS_ASR_DEVICE=mps`` 时才启用，
         且结果异常会自动重试 CPU。
      3. **CPU**（无 GPU 后端）→ ``device_map=None``（让 transformers 默认 CPU）+ ``float32``。

    **环境变量覆盖**（用于排查精度/性能问题或强制指定后端）：
      - ``TOPOS_ASR_DEVICE``: ``cpu`` / ``cuda:0`` / ``mps`` —— 强制设备，跳过自动检测。
      - ``TOPOS_ASR_DTYPE``: ``float32`` / ``float16`` / ``bfloat16`` —— 强制精度。

    跨平台安全：靠 ``torch.cuda``/``torch.backends.mps`` 的能力检测，无平台硬编码。
    Windows/Linux 上 ``mps.is_available()`` 恒为 False，只走第 1 或第 3 分支，行为不变。

    Returns:
        (device_map, dtype): device_map 为 None 时表示 CPU（不传给 from_pretrained）。
    """
    import torch

    # 用户显式覆盖（最高优先级）
    env_device = os.environ.get("TOPOS_ASR_DEVICE")
    env_dtype = os.environ.get("TOPOS_ASR_DTYPE")
    if env_device or env_dtype:
        device_map = env_device if env_device and env_device.lower() != "cpu" else None
        dtype = _parse_dtype(env_dtype) if env_dtype else torch.float32
        return device_map, dtype

    if torch.cuda.is_available():
        return "cuda:0", torch.bfloat16
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        # Qwen3-ASR can load successfully on MPS while still returning corrupt
        # text with the float16 kernels shipped by some PyTorch/macOS pairs.
        # Correct subtitles are more important than the speculative speedup,
        # so automatic mode uses the safe CPU path.  Users can still opt into
        # MPS with TOPOS_ASR_DEVICE=mps; _run_asr() quality-checks that result
        # and retries on CPU when the backend is inaccurate.
        logger.info(
            "MPS detected; using CPU float32 for ASR automatic mode for reliability"
        )
        return None, torch.float32
    return None, torch.float32


def _parse_dtype(name: str) -> Any:
    """把环境变量里的精度名解析为 torch dtype。"""
    import torch
    mapping = {
        "float32": torch.float32,
        "fp32": torch.float32,
        "float16": torch.float16,
        "fp16": torch.float16,
        "bfloat16": torch.bfloat16,
        "bf16": torch.bfloat16,
    }
    return mapping.get(name.lower().strip(), torch.float32)


def _cpu_fallback_kwargs() -> tuple[str, Any]:
    """MPS/CUDA 失败时回退用的保守参数：CPU + float32。

    供 ``_get_asr_engine`` / ``_get_aligner`` 的 except 分支复用。
    """
    import torch
    return "cpu", torch.float32


def _asr_result_is_suspicious(raw_segments: list[Any]) -> bool:
    """识别 MPS 已加载但输出损坏的常见结果。

    不能用“只有一段”作为判据：正常 ASR 也可能把整段音频放在一个
    segment 中。这里检查不可显示替换字符、控制字符和未知语言配合的
    非自然文本，避免把合法的短句误判为失败。
    """
    if not raw_segments:
        return False
    for segment in raw_segments:
        text = str(getattr(segment, "text", "") or "").strip()
        language = str(getattr(segment, "language", "") or "").lower()
        if "\ufffd" in text:
            return True
        if any(
            unicodedata.category(char).startswith("C")
            and char not in "\n\r\t"
            for char in text
        ):
            return True
        if language in {"unknown", "un", "und"} and text:
            # Unknown language is not automatically an error, but a long
            # result with no CJK/Latin/digit characters is not usable text.
            readable = sum(
                unicodedata.category(char)[0] in {"L", "N"}
                for char in text
            )
            if readable == 0 or (len(text) >= 8 and readable / len(text) < 0.45):
                return True
    return False


# ---------------------------------------------------------------------- #
# 异常
# ---------------------------------------------------------------------- #


class ModelPackNotInstalledError(RuntimeError):
    """Qwen3-ASR / Sortformer 模型未安装 / 未校验（§11.1 M4：不静默降级）。

    从 model_pack_manager 复用同义异常名；这里 re-export 避免 provider 层多依赖。
    """

    def __init__(self, pack_id: str, detail: str = "") -> None:
        self.pack_id = pack_id
        super().__init__(
            f"ModelPack {pack_id!r} is not installed or not verified"
            + (f": {detail}" if detail else "")
        )


# ---------------------------------------------------------------------- #
# 原始输出形态（provider → 本模块之间的中间结构）
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class _RawSegment:
    """ASR 原始 segment（未做时间换算/匿名化）。

    时间坐标是 **sample offset**（16kHz），由适配器从 Qwen3 的秒级时间戳换算而来。
    这样 ``_convert_segments`` 可复用既有的 ``from_audio_sample`` 整数换算路径。
    """

    pts_start: int               # 音频 sample offset
    pts_end: int
    text: str
    language: str
    confidence: float
    words: tuple[tuple[int, int, str, float], ...]  # (sample_start, sample_end, text, conf)
    sample_rate: int = ASR_SAMPLE_RATE


@dataclass(frozen=True)
class _RawDiarizationSegment:
    """Sortformer 原始说话人段（未匿名化）。"""

    sample_start: int
    sample_end: int
    speaker_index: int            # Sortformer 输出的 speaker 整数索引 → 这里是 0/1/...
    sample_rate: int = ASR_SAMPLE_RATE


# ---------------------------------------------------------------------- #
# QwenASRProvider
# ---------------------------------------------------------------------- #


# 无对齐模型时的置信度惩罚（§11.1 M4：降级必标注）。0.7 表示"精度打折但有结果"。
_NO_ALIGNMENT_CONFIDENCE_PENALTY = 0.7


class QwenASRProvider:
    """本地 Qwen3-ASR 转录 provider（R4-c / §16.3）。

    实现 :class:`AnalysisProvider` Protocol（duck typing）。

    输出 ``AssetAnalysis.transcript``（tuple[TranscriptSegment]）+ 字段级 provenance
    （``FieldProvenance.single("transcript", prov)``）；其余字段不填（由 AnalysisService
    的 ``_merge_analysis`` 与其他 provider 结果合并）。

    三段式推理（默认真实路径；``TOPOS_ASR_REAL=0`` 走 stub）：
    1. 音频提取（ffmpeg → 16kHz mono wav）
    2. Qwen3-ASR 转录 + 可选 Qwen3-ForcedAligner 词级对齐
    3. 可选 NVIDIA Sortformer 说话人分离 → 匿名合并
    """

    provider_id = "transcript_local"
    supported_kinds = (SegmentKind.TRANSCRIPT,)

    def __init__(
        self,
        model_pack_manager: Any | None = None,
        *,
        timebase_service: TimebaseService | None = None,
        sample_rate: int = ASR_SAMPLE_RATE,
        audio_extractor: AudioExtractor | None = None,
        asset_path_provider: Callable[[str], str] | None = None,
        audio_preprocessor: Any | None = None,
    ) -> None:
        """
        Args:
            model_pack_manager: :class:`ModelPackManager`（探测模型安装）。None 时仅用
                ``find_spec`` 探测 qwen_asr 包是否可 import（测试/无 app 场景）。
            timebase_service: PTS→frame 换算；None 用默认单例。
            sample_rate: ASR 内部音频采样率（Qwen3-ASR 固定 16kHz）。
            audio_extractor: 音频提取器；None 用默认 :class:`AudioExtractor`。
            asset_path_provider: ``asset_id → 源文件路径`` callable。真实路径必需——
                用于定位媒体文件抽音频。测试/无 app 场景 None 时真实路径会报错。
        """
        self._pack_manager = model_pack_manager
        self._timebase = timebase_service or TimebaseService()
        self._sample_rate = sample_rate
        self._audio_extractor = audio_extractor or AudioExtractor()
        self._asset_path_provider = asset_path_provider
        # Optional music/speech separation backend (for example Demucs v4).
        # It is injected by the adaptive router and remains disabled by default.
        self._audio_preprocessor = audio_preprocessor
        self._thread_local = threading.local()
        # R4-c：惰性引擎缓存。真实路径首次调用时创建，之后复用（模型加载昂贵）。
        # stub 路径不触碰它们。
        # AUD-14901：本 provider 以单实例注册进 AnalysisService，其
        # ThreadPoolExecutor 会并发调用 analyze。惰性初始化与引擎字段
        # 更新必须持有 _engine_lock（双检锁），否则并发首次调用会各自
        # 加载多 GB 模型（内存峰值翻倍），且 _force_cpu_asr_engine 置空
        # 引擎时可能读到不一致的 device 状态。
        self._engine_lock = threading.Lock()
        self._asr_engine: Any = None
        self._asr_engine_model_id: str | None = None
        self._asr_engine_device: str | None = None
        self._asr_force_cpu = False
        self._aligner: Any = None
        self._diar_model: Any = None
        # 进度回调注入点（签名 (current, total)）。由控制器注入驱动 UI 进度条。
        self._progress_cb: Callable[[int, int], Any] | None = None

    def analyze_with_preprocessor(
        self,
        asset_id: str,
        fingerprint: AssetFingerprint,
        *,
        fps: str,
        preprocessor: Any,
        progress: Any | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[AssetAnalysis, tuple[SemanticSegment, ...]]:
        """Run this provider once with a temporary audio preprocessor.

        The ASR engine stays cached and only the extracted wav is replaced for
        this call.  This avoids loading a second Qwen model for the Demucs
        fallback while keeping the normal provider path unchanged.
        """
        sentinel = object()
        previous = getattr(self._thread_local, "audio_preprocessor", sentinel)
        self._thread_local.audio_preprocessor = preprocessor
        try:
            return self.analyze(
                asset_id, fingerprint, fps=fps, progress=progress, options=options,
            )
        finally:
            if previous is sentinel:
                try:
                    del self._thread_local.audio_preprocessor
                except AttributeError:
                    pass
            else:
                self._thread_local.audio_preprocessor = previous

    def _current_audio_preprocessor(self) -> Any | None:
        return getattr(self._thread_local, "audio_preprocessor", self._audio_preprocessor)

    def validate_options(self, options: dict[str, Any] | None = None) -> str | None:
        """Validate model selection before a background job is started."""
        sentinel = object()
        previous = getattr(self._thread_local, "request_options", sentinel)
        self._thread_local.request_options = dict(options or {})
        try:
            selected_pack_id = self._selected_model_pack_id()
            self._require_pack(selected_pack_id)
            pack_path = self._safe_pack_path(selected_pack_id)
            checkpoint_issue = (
                self._qwen_runtime_checkpoint_issue(pack_path) if pack_path else None
            )
            if checkpoint_issue:
                fallback_model_id = self._selected_model_spec()[1]
                return f"{checkpoint_issue} 请重新下载 {fallback_model_id}。"
            return None
        except Exception as exc:  # noqa: BLE001
            return str(exc)
        finally:
            if previous is sentinel:
                try:
                    del self._thread_local.request_options
                except AttributeError:
                    pass
            else:
                self._thread_local.request_options = previous

    # ---------------- AnalysisProvider.analyze ---------------- #

    def analyze(
        self,
        asset_id: str,
        fingerprint: AssetFingerprint,
        *,
        fps: str,
        progress: Any | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[AssetAnalysis, tuple[SemanticSegment, ...]]:
        """执行转录（R4-c）。

        AUD-14904：``thread_local.request_options`` 在本调用结束后**必然**
        恢复原值/删除——线程池线程会被复用，不能把上一次请求的选项残留给
        下一次（否则下一次未传 options 的调用会沿用上一次的模型/对齐等选择）。
        与 ``analyze_with_preprocessor`` / ``validate_options`` 的 sentinel
        恢复模式一致。异常路径同样恢复（``finally``）。

        Raises:
            ModelPackNotInstalledError: qwen_asr 包未装 或 模型 pack 未下载/未校验。
            AudioExtractionError: 无音频流 / ffmpeg 不可用（仅真实路径）。
        """
        request_options = dict(options or {})
        sentinel = object()
        previous = getattr(self._thread_local, "request_options", sentinel)
        self._thread_local.request_options = request_options
        try:
            return self._analyze_impl(
                asset_id, fingerprint, fps=fps, progress=progress,
            )
        finally:
            if previous is sentinel:
                try:
                    del self._thread_local.request_options
                except AttributeError:
                    pass
            else:
                self._thread_local.request_options = previous

    def _analyze_impl(
        self,
        asset_id: str,
        fingerprint: AssetFingerprint,
        *,
        fps: str,
        progress: Any | None = None,
    ) -> tuple[AssetAnalysis, tuple[SemanticSegment, ...]]:
        """analyze 的实际执行体（``request_options`` 已由调用方设置）。"""
        # 1. 探测 qwen_asr 是否可 import（可选依赖，不进基础安装）
        if importlib.util.find_spec("qwen_asr") is None:
            raise ModelPackNotInstalledError(
                "qwen3_asr", detail="python package 'qwen_asr' is not installed"
            )

        # 2. 校验模型 pack（qwen3_asr 必须 usable；align/diarize 缺失走降级）
        self._require_pack(self._selected_model_pack_id())

        _emit_progress(progress, 0.03, "正在准备语音识别模型")

        # 3. 推理（首版确定性 stub；真实路径由 env 开关）
        # 模型首次加载和 MPS/CPU 推理没有可靠的细粒度回调，使用不确定进度
        # 明确告诉用户任务仍在运行，而不是让固定百分比看起来像卡死。
        _emit_progress(progress, None, "正在提取音频并运行语音识别模型（首次加载可能需要几分钟）")
        raw_segments = self._run_asr(asset_id, progress=progress)
        _emit_progress(progress, 0.68, "语音识别完成，正在整理时间轴")

        # 4. 时间换算 PTS(sample) → frame（经 TimebaseService，整数运算）
        #    + 词级对齐可用性 → confidence 调整
        align_available = self._alignment_requested()
        segments = self._convert_segments(
            raw_segments, fps=fps, align_available=align_available,
        )
        _emit_progress(
            progress,
            0.78,
            "正在生成词级时间码" if align_available else "未安装对齐模型，使用段级时间码",
        )

        # 5. diarization 合并（匿名 speaker S1/S2）
        _emit_progress(progress, None, "正在分析说话人（可选步骤）")
        diarization = (
            self._run_diarization(asset_id, progress=progress)
            if self._speakers_requested() else []
        )
        if diarization:
            # AUD-14900：segments 已经是 frame 坐标（_convert_segments 经
            # from_audio_sample 换算），diarization 段是 sample 坐标。必须传
            # media_fps_ratio + timebase_service 让 merge_diarization 把两者
            # 统一到 frame 坐标，否则 speaker 归属按错位的量纲计算。
            from src.shared.media.metadata import Rational

            segments = merge_diarization(
                segments, diarization,
                media_fps_ratio=Rational.from_string(fps),
                timebase_service=self._timebase,
            )
            _emit_progress(progress, 0.93, "说话人分析完成，正在生成字幕")
        else:
            _emit_progress(progress, 0.93, "说话人分析不可用，继续生成普通字幕")

        # 6. 组装 AssetAnalysis（只填 transcript + 字段级 provenance）
        extra: dict[str, Any] = {"sample_rate": self._sample_rate}
        fallback_reason = getattr(self._thread_local, "asr_fallback_reason", None)
        if fallback_reason:
            extra["asr_fallback"] = fallback_reason
        preprocessor = self._current_audio_preprocessor()
        if preprocessor is not None:
            extra["preprocessor"] = getattr(
                preprocessor, "backend_id", "custom"
            )
        if not align_available:
            extra["alignment"] = "none"
            extra["confidence_penalty"] = _NO_ALIGNMENT_CONFIDENCE_PENALTY
        provenance = AnalysisProvenance(
            provider=self.provider_id,
            model=self._model_version(),
            extra=extra,
        )
        analysis = AssetAnalysis(
            asset_id=asset_id,
            fingerprint=fingerprint,
            fps=fps,
            transcript=tuple(segments),
            provenance=provenance,
            field_provenance=FieldProvenance.single("transcript", provenance),
        )
        return analysis, tuple(analysis_to_segments(analysis))

    # ---------------- 推理（stub / 真实开关）---------------- #

    def _run_asr(
        self, asset_id: str, *, progress: Any | None = None,
    ) -> list[_RawSegment]:
        """跑 ASR 转录。

        默认走真实 Qwen3-ASR；``TOPOS_ASR_REAL=0`` 时走确定性 stub（测试用）。
        """
        self._thread_local.asr_fallback_reason = None
        if _real_inference_enabled("TOPOS_ASR_REAL"):
            try:
                raw_segments = self._run_qwen_asr(asset_id, progress=progress)
            except Exception as exc:
                if not self._is_mps_engine():
                    raise
                return self._retry_asr_on_cpu(
                    asset_id,
                    progress=progress,
                    reason=f"MPS 推理失败：{exc}",
                )
            if self._is_mps_engine() and _asr_result_is_suspicious(raw_segments):
                return self._retry_asr_on_cpu(
                    asset_id,
                    progress=progress,
                    reason="MPS 返回了疑似乱码结果",
                )
            return raw_segments
        return _stub_transcript(asset_id, self._sample_rate)

    def _retry_asr_on_cpu(
        self, asset_id: str, *, progress: Any | None, reason: str,
    ) -> list[_RawSegment]:
        """MPS 结果异常时用 CPU float32 重跑一次，保证字幕可用。"""
        self._thread_local.asr_fallback_reason = "mps_to_cpu_float32"
        _emit_progress(progress, None, f"{reason}，正在切换 CPU 精度重试")
        self._force_cpu_asr_engine()
        _emit_progress(progress, None, "正在使用 CPU float32 重新识别语音")
        return self._run_qwen_asr(asset_id, progress=progress)

    def _run_qwen_asr(
        self, asset_id: str, *, progress: Any | None,
    ) -> list[_RawSegment]:
        """Invoke the real path while retaining compatibility with test hosts."""
        if progress is None:
            return self._run_qwen_asr_real(asset_id)
        return self._run_qwen_asr_real(asset_id, progress=progress)

    def _is_mps_engine(self) -> bool:
        return self._asr_engine_device == "mps"

    def _force_cpu_asr_engine(self) -> None:
        """丢弃 MPS 引擎及其对齐器，让下一次调用按 CPU 初始化。

        AUD-14901：引擎字段置空必须与惰性初始化在同一临界区内完成，
        避免并发线程读到半拆除状态。
        """
        with self._engine_lock:
            self._asr_force_cpu = True
            self._asr_engine = None
            self._asr_engine_device = None
            # 对齐器也可能已在 MPS 上初始化；强制和 ASR 使用同一安全后端。
            self._aligner = None

    def _run_qwen_asr_real(
        self, asset_id: str, *, progress: Any | None = None,
    ) -> list[_RawSegment]:
        """真实 Qwen3-ASR 推理（R4-c）。

        流程：音频提取 → Qwen3-ASR.transcribe → (可选) ForcedAligner 词级对齐 →
        秒级时间戳转 sample → ``_RawSegment``。

        Qwen3-ASR 的 ``transcribe`` 返回 ``[result]``，每个 result 有 ``.text`` /
        ``.language`` / 可选 ``.time_stamps``。无 ForcedAligner 时只有整段文本，
        时间戳用音频起止近似（segment 级）。
        """
        wav_path = self._extract_audio(asset_id)
        processed = None
        asr_path = wav_path
        try:
            preprocessor = self._current_audio_preprocessor()
            if preprocessor is not None:
                _emit_progress(progress, None, "正在去除背景音乐，提取人声（Demucs）")
                processed = preprocessor.process(wav_path)
                asr_path = processed.path
                _emit_progress(progress, None, "人声提取完成，正在运行语音识别")
            engine = self._get_asr_engine()
            align_available = self._alignment_requested()

            # Qwen3-ASR.transcribe：audio=路径/language=None 自动检测/return_time_stamps
            requested_language = self._requested_language()
            transcribe_kwargs: dict[str, Any] = {
                "language": None if requested_language == "auto" else requested_language,
            }
            # ``return_time_stamps`` is only valid when the Qwen wrapper was
            # initialized with a forced aligner.  This provider loads the
            # aligner lazily and independently, so asking the ASR wrapper for
            # embedded timestamps here raises before transcription starts.
            if align_available and getattr(engine, "forced_aligner", None) is not None:
                transcribe_kwargs["return_time_stamps"] = True

            results = engine.transcribe(audio=asr_path, **transcribe_kwargs)
            if not results:
                return []

            result = results[0]
            text = getattr(result, "text", "") or ""
            language = _normalize_language(getattr(result, "language", "") or "")

            if not text.strip():
                return []

            # 词级时间戳（ForcedAligner 可用时）
            words: tuple[tuple[int, int, str, float], ...] = ()
            if align_available:
                words = self._extract_word_timestamps(result, asr_path, text, language)

            if words:
                # 有词级时间码：segment 覆盖首词到末词
                start_sample = words[0][0]
                end_sample = words[-1][1]
                confidence = sum(w[3] for w in words) / len(words)
            else:
                # 无对齐：整段用音频全长近似（segment 级，confidence 默认 0.9）
                audio_samples = self._audio_sample_count(asr_path)
                start_sample = 0
                end_sample = audio_samples
                confidence = 0.9

            return [_RawSegment(
                pts_start=start_sample,
                pts_end=end_sample,
                text=text.strip(),
                language=language,
                confidence=confidence,
                words=words,
                sample_rate=self._sample_rate,
            )]
        finally:
            if processed is not None:
                processed.cleanup()
            self._cleanup_temp_audio(wav_path)

    def _extract_word_timestamps(
        self, result: Any, wav_path: str, text: str, language: str,
    ) -> tuple[tuple[int, int, str, float], ...]:
        """从 Qwen3-ASR 结果或 ForcedAligner 提取词级时间戳。

        若 ASR 结果自带 ``time_stamps``（transcribe(return_time_stamps=True) 时），
        直接用；否则调独立的 ForcedAligner.align()。
        """
        # 优先用 ASR 内嵌的时间戳（transcribe 时传了 forced_aligner 会自动附加）
        time_stamps = getattr(result, "time_stamps", None)
        if time_stamps:
            return _parse_qwen_time_stamps(time_stamps, self._sample_rate, language)

        # 退而求其次：独立调 ForcedAligner
        aligner = self._get_aligner()
        if aligner is None:
            return ()
        try:
            align_results = aligner.align(audio=wav_path, text=text, language=language)
            if align_results and align_results[0]:
                return _parse_qwen_align_results(
                    align_results[0], self._sample_rate, language,
                )
        except Exception:
            logger.warning("ForcedAligner failed; falling back to segment-level", exc_info=True)
        return ()

    # ---------------- 说话人分离 ---------------- #

    def _run_diarization(
        self, asset_id: str, *, progress: Any | None = None,
    ) -> list[_RawDiarizationSegment]:
        """跑 NVIDIA Sortformer 说话人分离（可选）。

        真实路径（默认 / ``TOPOS_ASR_REAL=1``）：需 ``nemo_sortformer`` pack + ``nemo`` 包；
        二者缺一即返回空（不抛；transcript 仍可用，speaker 全 None）。

        stub 路径（``TOPOS_ASR_REAL=0``）：用确定性 stub 数据（不依赖 nemo 包），
        供测试用。但需 ``nemo_sortformer`` pack 标记为已装（否则视为用户未启用分离）。
        """
        if not self._is_pack_usable("nemo_sortformer"):
            return []
        if not _real_inference_enabled("TOPOS_ASR_REAL"):
            # stub 路径：用确定性 diarization（测试基础），不检查 nemo 包是否真实安装
            return _stub_diarization(asset_id, self._sample_rate)
        # 真实路径：检查 nemo 包
        if importlib.util.find_spec("nemo") is None:
            logger.warning("nemo_sortformer pack installed but nemo_toolkit not importable; diarization disabled")
            return []
        try:
            return self._run_sortformer_real(asset_id)
        except (ImportError, ModuleNotFoundError, ModelPackNotInstalledError) as exc:
            # Sortformer is an optional enrichment.  NeMo currently requires
            # newer torch.distributed APIs than the Intel-mac CPU wheel can
            # provide; transcript + alignment must remain usable without it.
            logger.warning("Sortformer unavailable; continuing without speaker labels: %s", exc)
            _emit_progress(progress, 0.90, f"说话人分析不可用，已跳过：{exc}")
            return []
        except Exception as exc:
            # AUD-14902：Sortformer 运行期错误（显存不足、tensor 形状、音频解码
            # 失败等 RuntimeError）同样是"可选增强失败"——不得击溃已经完成的
            # ASR + 词级对齐结果。降级为 speaker=None 并保留完整 traceback
            # （exc_info）供排查；此处的领域错误即"分离失败"，降级是本函数
            # docstring 声明的行为，不属于掩盖领域错误。
            logger.warning(
                "Sortformer runtime failure; continuing without speaker labels: %s",
                exc, exc_info=True,
            )
            _emit_progress(progress, 0.90, f"说话人分析失败，已跳过：{type(exc).__name__}: {exc}")
            return []

    def _run_sortformer_real(self, asset_id: str) -> list[_RawDiarizationSegment]:
        """真实 Sortformer 说话人分离（R4-c）。

        Sortformer.diarize() 返回 ``["begin_seconds,end_seconds,speaker_index", ...]``，
        适配器把秒转 sample（``int(seconds * sample_rate)``）。
        """
        wav_path = self._extract_audio(asset_id)
        try:
            model = self._get_diar_model()
            # diarize 接受单个路径或列表；返回 list[str]
            raw_segments = model.diarize(audio=wav_path)
            if not raw_segments:
                return []
            # raw_segments 可能是 list[str] 或 list[list[str]]；取第一组
            first = raw_segments[0] if isinstance(raw_segments[0], list) else raw_segments
            out: list[_RawDiarizationSegment] = []
            for seg_str in first:
                parsed = _parse_sortformer_segment(seg_str, self._sample_rate)
                if parsed is not None:
                    out.append(parsed)
            return out
        finally:
            self._cleanup_temp_audio(wav_path)

    # ---------------- 惰性引擎初始化 ---------------- #

    def _get_asr_engine(self) -> Any:
        """惰性创建并缓存 Qwen3-ASR 引擎（AUD-14901：双检锁）。"""
        # Tests and embedding hosts may inject an already-created engine. It
        # has no model-id metadata, so leave it untouched.
        if self._asr_engine is not None and self._asr_engine_model_id is None:
            return self._asr_engine
        selected_pack_id, fallback_model_id = self._selected_model_spec()
        pack_path = self._safe_pack_path(selected_pack_id)
        checkpoint_issue = (
            self._qwen_runtime_checkpoint_issue(pack_path) if pack_path else None
        )
        if checkpoint_issue:
            raise ModelPackNotInstalledError(
                selected_pack_id,
                detail=checkpoint_issue,
            )
        model_id = pack_path or fallback_model_id
        # 快路径（无锁）：缓存命中直接返回。
        if self._asr_engine is not None and self._asr_engine_model_id == model_id:
            return self._asr_engine
        with self._engine_lock:
            # 双检：等锁期间另一线程可能已完成加载或切换了模型。
            if self._asr_engine is not None and self._asr_engine_model_id is None:
                return self._asr_engine
            if self._asr_engine is not None and self._asr_engine_model_id == model_id:
                return self._asr_engine
            if self._asr_engine is not None:
                # The settings dialog can switch between 0.6B and 1.7B. Never
                # reuse an engine built for a different model.
                self._asr_engine = None
                self._asr_engine_device = None
                self._aligner = None
            try:
                from qwen_asr import Qwen3ASRModel  # type: ignore[import-not-found]
            except ImportError as exc:
                raise ModelPackNotInstalledError(
                    "qwen3_asr", detail=f"qwen_asr package import failed: {exc}"
                ) from exc
            _patch_qwen3_rope_scaling_compat()
            _patch_qwen3_processor_compat()
            # The 0.6B pack found in older installations can be an incomplete
            # checkpoint (its config has no thinker/text rope section). Prefer the
            # complete 1.7B pack when it is installed; retain the small-pack
            # fallback for existing users and tests.
            # 若是本地目录，传路径；否则传 HF repo id
            init_kwargs = self._engine_init_kwargs()
            init_kwargs["max_new_tokens"] = self._max_new_tokens()
            try:
                self._asr_engine = Qwen3ASRModel.from_pretrained(model_id, **init_kwargs)
                self._asr_engine_model_id = model_id
                self._asr_engine_device = init_kwargs.get("device_map") or "cpu"
            except Exception as exc:
                # MPS 后端可能因部分算子未实现而加载失败（torch 版本依赖）。
                # 此时回退 CPU + float32 重试，保证可用性（速度退回 CPU 水平）。
                device_map = init_kwargs.get("device_map")
                if device_map in ("mps", "cuda:0"):
                    fb_device, fb_dtype = _cpu_fallback_kwargs()
                    logger.warning(
                        "Qwen3-ASR init failed on %s (%s); retrying on CPU. "
                        "GPU acceleration disabled for this session.",
                        device_map, exc,
                    )
                    fb_kwargs = {"dtype": fb_dtype, "device_map": fb_device}
                    fb_kwargs["max_new_tokens"] = self._max_new_tokens()
                    self._asr_engine = Qwen3ASRModel.from_pretrained(model_id, **fb_kwargs)
                    self._asr_engine_model_id = model_id
                    self._asr_engine_device = "cpu"
                else:
                    raise
            logger.info("Qwen3-ASR engine initialized (model=%s)", model_id)
            return self._asr_engine

    def _get_aligner(self) -> Any | None:
        """惰性创建 Qwen3-ForcedAligner（AUD-14901：双检锁）。不可用时返回 None（降级为段级）。"""
        if self._aligner is not None:
            return self._aligner
        if importlib.util.find_spec("qwen_asr") is None:
            return None
        with self._engine_lock:
            if self._aligner is not None:
                return self._aligner
            try:
                from qwen_asr import Qwen3ForcedAligner  # type: ignore[import-not-found]
            except ImportError:
                logger.warning("Qwen3ForcedAligner not available; word-level alignment disabled")
                return None
            _patch_qwen3_rope_scaling_compat()
            _patch_qwen3_processor_compat()
            pack_path = self._safe_pack_path("qwen3_align")
            model_id = pack_path or "Qwen/Qwen3-ForcedAligner-0.6B"
            init_kwargs = self._engine_init_kwargs()
            try:
                self._aligner = Qwen3ForcedAligner.from_pretrained(model_id, **init_kwargs)
            except Exception as exc:
                device_map = init_kwargs.get("device_map")
                if device_map in ("mps", "cuda:0"):
                    fb_device, fb_dtype = _cpu_fallback_kwargs()
                    logger.warning(
                        "Qwen3-ForcedAligner init failed on %s (%s); retrying on CPU.",
                        device_map, exc,
                    )
                    fb_kwargs = {"dtype": fb_dtype, "device_map": fb_device}
                    self._aligner = Qwen3ForcedAligner.from_pretrained(model_id, **fb_kwargs)
                else:
                    raise
            logger.info("Qwen3-ForcedAligner initialized (model=%s)", model_id)
            return self._aligner

    def _get_diar_model(self) -> Any:
        """惰性创建 Sortformer 说话人分离模型（AUD-14901：双检锁）。

        NeMo 的加载方式：
        - 本地 ``.nemo`` 文件：用 ``restore_from(restore_path=...)``（不能用 from_pretrained）。
        - HF repo id：用 ``from_pretrained(...)``（需联网 + 可能需 HF token）。
        """
        if self._diar_model is not None:
            return self._diar_model
        with self._engine_lock:
            if self._diar_model is not None:
                return self._diar_model
            try:
                from nemo.collections.asr.models import SortformerEncLabelModel  # type: ignore[import-not-found]
            except ImportError as exc:
                raise ModelPackNotInstalledError(
                    "nemo_sortformer",
                    detail=f"nemo_toolkit import failed: {exc}",
                ) from exc
            pack_path = self._safe_pack_path("nemo_sortformer")
            # 找 pack 目录下的 .nemo 文件
            nemo_file = _find_nemo_checkpoint(pack_path) if pack_path else None
            if nemo_file:
                # 本地 .nemo 文件 → restore_from
                self._diar_model = SortformerEncLabelModel.restore_from(
                    restore_path=nemo_file, strict=False,
                )
                logger.info("Sortformer loaded from local .nemo: %s", nemo_file)
            else:
                # HF repo → from_pretrained
                model_id = "nvidia/diar_sortformer_4spk-v1"
                self._diar_model = SortformerEncLabelModel.from_pretrained(model_id)
                logger.info("Sortformer loaded from HF: %s", model_id)
            self._diar_model.eval()
            return self._diar_model

    def _engine_init_kwargs(self) -> dict[str, Any]:
        """构造 Qwen3 引擎初始化参数（device/dtype）。

        自动模式在 macOS 上使用 CPU float32；若用户显式选择 MPS，调用方
        会在结果异常时切换到 CPU。强制回退时 ASR 和 ForcedAligner 共用
        CPU，避免只切换一半导致词级时间码继续异常。
        """
        if self._asr_force_cpu:
            import torch
            return {"dtype": torch.float32, "device_map": "cpu"}
        device_map, dtype = _resolve_torch_device()
        kwargs: dict[str, Any] = {"dtype": dtype}
        if device_map is not None:
            kwargs["device_map"] = device_map
        return kwargs

    def _request_option(self, name: str, default: Any) -> Any:
        options = getattr(self._thread_local, "request_options", {})
        return options.get(name, default) if isinstance(options, dict) else default

    def _selected_model_spec(self) -> tuple[str, str]:
        """Resolve the requested model size to a pack and HF fallback id."""
        requested = str(self._request_option("model_size", "auto")).lower()
        if requested in _ASR_MODEL_PACKS:
            return _ASR_MODEL_PACKS[requested]
        # Auto keeps the existing behavior: use the verified large pack when
        # present, otherwise use the lightweight pack.
        if self._is_pack_usable("qwen3_asr_large"):
            return _ASR_MODEL_PACKS["large"]
        return _ASR_MODEL_PACKS["small"]

    def _selected_model_pack_id(self) -> str:
        return self._selected_model_spec()[0]

    def _max_new_tokens(self) -> int:
        """Return a safe output ceiling; Qwen's package default is only 512."""
        raw = self._request_option("max_new_tokens", None)
        if raw is None:
            raw = os.environ.get("TOPOS_ASR_MAX_NEW_TOKENS", _DEFAULT_ASR_MAX_NEW_TOKENS)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = _DEFAULT_ASR_MAX_NEW_TOKENS
        return max(2048, min(32768, value))

    def _requested_language(self) -> str:
        value = str(self._request_option("language", "auto")).lower()
        return value if value in {"auto", "zh", "en", "ja", "ko"} else "auto"

    def _alignment_requested(self) -> bool:
        return (
            str(self._request_option("timestamps", "auto")).lower() != "segment"
            and self._is_pack_usable("qwen3_align")
        )

    def _speakers_requested(self) -> bool:
        return bool(self._request_option("speakers", True))

    # ---------------- 音频提取辅助 ---------------- #

    def _extract_audio(self, asset_id: str) -> str:
        """提取 asset 的 16kHz mono wav。无 asset_path_provider 时抛错。"""
        if self._asset_path_provider is None:
            raise ModelPackNotInstalledError(
                "qwen3_asr",
                detail="asset_path_provider not injected; cannot locate media file for ASR",
            )
        source_path = self._asset_path_provider(asset_id)
        if not source_path:
            raise AudioExtractionError(
                f"asset_path_provider returned empty path for asset {asset_id!r}"
            )
        return self._audio_extractor.extract_wav(source_path)

    def _audio_sample_count(self, wav_path: str) -> int:
        """读取 wav 文件的总 sample 数（用于无对齐时的 segment 时长近似）。"""
        try:
            import wave
            with wave.open(wav_path, "rb") as wf:
                return wf.getnframes()
        except Exception:
            return self._sample_rate * 60  # 回退：假设 60 秒

    @staticmethod
    def _cleanup_temp_audio(wav_path: str) -> None:
        """删除临时音频文件（若在临时目录）。"""
        import tempfile
        temp_dir = tempfile.gettempdir()
        if wav_path.startswith(temp_dir) and os.path.isfile(wav_path):
            try:
                os.remove(wav_path)
            except OSError:
                pass

    # ---------------- 时间换算 ---------------- #

    def _convert_segments(
        self,
        raw_segments: list[_RawSegment],
        *,
        fps: str,
        align_available: bool,
    ) -> list[TranscriptSegment]:
        """把 ASR 原始（sample offset）→ TranscriptSegment（frame 坐标）。

        经 :class:`TimebaseService.from_audio_sample`（sample → frame，整数运算）。
        无对齐时 confidence 下调并保留 words（words 为空时自然不填）。
        """
        from src.shared.media.metadata import Rational

        media_fps = Rational.from_string(fps)
        out: list[TranscriptSegment] = []
        for raw in raw_segments:
            start_frame = self._timebase.from_audio_sample(
                raw.pts_start, raw.sample_rate, media_fps,
            )
            end_frame = self._timebase.from_audio_sample(
                raw.pts_end, raw.sample_rate, media_fps,
            )
            if end_frame <= start_frame:
                end_frame = start_frame + 1
            # 词级换算（仅当对齐可用；否则降级为段级）
            words: tuple[WordSegment, ...] = ()
            if align_available and raw.words:
                ws: list[WordSegment] = []
                for ws_start, ws_end, wtext, wconf in raw.words:
                    wf_start = self._timebase.from_audio_sample(
                        ws_start, raw.sample_rate, media_fps,
                    )
                    wf_end = self._timebase.from_audio_sample(
                        ws_end, raw.sample_rate, media_fps,
                    )
                    if wf_end <= wf_start:
                        wf_end = wf_start + 1
                    ws.append(WordSegment(
                        start_frame=wf_start, end_frame=wf_end,
                        text=wtext, confidence=wconf,
                    ))
                words = tuple(ws)
            confidence = raw.confidence if align_available else raw.confidence * _NO_ALIGNMENT_CONFIDENCE_PENALTY
            out.append(TranscriptSegment(
                start_frame=start_frame,
                end_frame=end_frame,
                text=raw.text,
                confidence=confidence,
                language=raw.language,
                words=words,
            ))
        return out

    # ---------------- 模型可用性探测 ---------------- #

    def _require_pack(self, pack_id: str) -> str:
        """要求某 pack usable，否则抛 ModelPackNotInstalledError。

        无 ModelPackManager 时（测试/无 app）：find_spec 已在 analyze 入口检查；放行。
        """
        if self._pack_manager is None:
            return ""
        from src.features.edit.ai_editing.model_pack_manager import (
            ModelPackNotInstalledError as _MPNIE,
        )
        try:
            return self._pack_manager.require_usable(pack_id)
        except _MPNIE as exc:
            raise ModelPackNotInstalledError(pack_id, detail=str(exc)) from exc

    def _is_pack_usable(self, pack_id: str) -> bool:
        """某 pack 是否可用（不抛；用于 align/diarize 这类可选能力的降级判定）。"""
        if self._pack_manager is None:
            return False
        status = self._pack_manager.get_status(pack_id)
        return status.usable if hasattr(status, "usable") else bool(getattr(status, "installed", False))

    def _safe_pack_path(self, pack_id: str) -> str | None:
        """安全读取 pack 安装路径；不可用时返回 None（让引擎从 HF 下载）。"""
        try:
            return self._pack_manager.require_usable(pack_id) or None  # type: ignore[union-attr]
        except Exception:
            return None

    @staticmethod
    def _qwen_runtime_checkpoint_issue(pack_path: str) -> str | None:
        """Describe an invalid or incomplete local qwen-asr checkpoint."""
        import json
        from pathlib import Path

        config_path = Path(pack_path) / "config.json"
        if not config_path.exists():
            # Lightweight test/model-manager fixtures may only contain an
            # installation marker and inject the engine separately.
            return None
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return "本地模型的 config.json 无法读取"
        # qwen-asr's Qwen3ASRForConditionalGeneration expects the released
        # ``thinker_config`` layout. Native ``Qwen3-ASR-*-hf`` checkpoints
        # instead expose the older top-level model/audio layout.
        if not isinstance(config.get("thinker_config"), dict):
            return (
                "本地权重不是 qwen-asr 运行时格式。请下载普通的 Qwen3-ASR 模型，"
                "不要使用带 -hf 后缀的原生 Transformers 权重。"
            )

        index_path = Path(pack_path) / "model.safetensors.index.json"
        if index_path.exists():
            try:
                index = json.loads(index_path.read_text(encoding="utf-8"))
                shard_names = set((index.get("weight_map") or {}).values())
            except (OSError, ValueError, TypeError):
                return "本地模型的权重索引无法读取"
            if any(not (Path(pack_path) / name).is_file() for name in shard_names):
                return "本地模型权重尚未下载完成"
        elif not (Path(pack_path) / "model.safetensors").is_file():
            return "本地模型权重尚未下载完成"
        return None

    @staticmethod
    def _is_qwen_runtime_checkpoint(pack_path: str) -> bool:
        """Return whether a local checkpoint is complete and compatible."""
        return QwenASRProvider._qwen_runtime_checkpoint_issue(pack_path) is None

    def set_progress_callback(self, cb: Callable[[int, int], Any] | None) -> None:
        """注入进度回调（签名 ``(current, total)``）。"""
        self._progress_cb = cb

    @staticmethod
    def _model_version() -> str:
        """模型版本标识（写入 provenance）。

        用 importlib.metadata 读版本号，不触发 qwen_asr 包的完整 import
        （后者会拉起 transformers 重依赖链，在 stub 测试场景下不必要且可能出错）。
        """
        try:
            from importlib.metadata import version as _pkg_version
            v = _pkg_version("qwen-asr")
            return f"qwen3-asr-{v}"
        except Exception:
            return "qwen3-asr-stub-v1"


# ---------------------------------------------------------------------- #
# 匿名 diarization 合并（§11.1 M4 红线：speaker 仅 S1/S2，绝不实名）
# ---------------------------------------------------------------------- #


def merge_diarization(
    transcript: list[TranscriptSegment],
    diarization: list[_RawDiarizationSegment],
    *,
    sample_rate: int = ASR_SAMPLE_RATE,
    media_fps_ratio: Any | None = None,
    timebase_service: TimebaseService | None = None,
) -> list[TranscriptSegment]:
    """把 diarization 的说话人归属合并进 transcript（§11.1 M4）。

    规则（计划红线）：
    - **speaker 仅匿名 S1/S2/...**（按首次出现顺序），绝不输出实名/Sortformer 原始标签。
    - **时间范围校验**：transcript 段必须与某 diarization 段时间重叠才赋 speaker；
      不重叠的 transcript 段 ``speaker=None``。
    - **重叠语音**（多 speaker 同时出现）：按重叠时长最长的 diarization 段归属。
    - 不丢弃任何 transcript 段。

    Args:
        transcript: 已转 frame 坐标的 TranscriptSegment 列表。
        diarization: Sortformer 原始段（sample offset）。
        sample_rate: diarization 的采样率（16kHz）。
        media_fps_ratio: 素材 fps（Rational）。
        timebase_service: 可选；None 用默认。

    Returns:
        新的 TranscriptSegment 列表（speaker 已赋值；原列表不变）。
    """
    if not transcript or not diarization:
        # 无 diarization：speaker 全 None
        return [TranscriptSegment(
            start_frame=s.start_frame, end_frame=s.end_frame, text=s.text,
            speaker=None, confidence=s.confidence, language=s.language, words=s.words,
        ) for s in transcript]

    tb = timebase_service or TimebaseService()
    diar_frames: list[tuple[int, int, int]] = []  # (start_frame, end_frame, speaker_index)
    for d in diarization:
        if media_fps_ratio is not None:
            ds = tb.from_audio_sample(d.sample_start, d.sample_rate, media_fps_ratio)
            de = tb.from_audio_sample(d.sample_end, d.sample_rate, media_fps_ratio)
        else:
            ds = d.sample_start
            de = d.sample_end
        if de <= ds:
            de = ds + 1
        diar_frames.append((ds, de, d.speaker_index))

    # speaker_index → 匿名 S1/S2（按首次出现顺序）
    anon_map: dict[int, str] = {}
    for _s, _e, idx in sorted(diar_frames, key=lambda x: x[0]):
        if idx not in anon_map:
            anon_map[idx] = _anon_speaker(len(anon_map))

    # 每个 transcript 段找重叠最长的 diarization 段
    result: list[TranscriptSegment] = []
    for seg in transcript:
        best_speaker: str | None = None
        best_overlap = 0
        for ds, de, idx in diar_frames:
            overlap = max(0, min(seg.end_frame, de) - max(seg.start_frame, ds))
            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = anon_map[idx]
        result.append(TranscriptSegment(
            start_frame=seg.start_frame, end_frame=seg.end_frame, text=seg.text,
            speaker=best_speaker,
            confidence=seg.confidence, language=seg.language, words=seg.words,
        ))
    return result


def _anon_speaker(index: int) -> str:
    """speaker 匿名化：0 → "S1"，1 → "S2"（绝不输出实名/Sortformer 原始标签）。"""
    return f"S{index + 1}"


# ---------------------------------------------------------------------- #
# 真实模型输出解析辅助
# ---------------------------------------------------------------------- #


def _normalize_language(lang: str) -> str:
    """Qwen3-ASR 返回的语言名（如 "Chinese"/"English"）归一化为 ISO 短码。

    便于下游的 transcript.language 字段统一（en/zh/ja/...）。无法识别时原样返回。
    """
    lang_lower = lang.lower().strip()
    mapping = {
        "chinese": "zh", "english": "en", "japanese": "ja", "korean": "ko",
        "french": "fr", "german": "de", "spanish": "es", "russian": "ru",
        "italian": "it", "portuguese": "pt", "arabic": "ar", "hindi": "hi",
        "vietnamese": "vi", "thai": "th", "indonesian": "id",
    }
    return mapping.get(lang_lower, lang_lower[:2] if lang_lower else "unknown")


def _parse_qwen_time_stamps(
    time_stamps: Any, sample_rate: int, language: str,
) -> tuple[tuple[int, int, str, float], ...]:
    """解析 Qwen3-ASR 的 time_stamps（transcribe(return_time_stamps=True) 时）。

    time_stamps 格式：``[(word, start_sec, end_sec), ...]``（每个 result 的第一组）。
    适配器把秒转 sample：``int(sec * sample_rate)``。
    """
    # time_stamps 可能是 ((word, start, end), ...) 或 [(word, start, end), ...]
    try:
        items = list(time_stamps)
    except TypeError:
        return ()
    if not items:
        return ()
    # 第一个元素可能是嵌套列表（batch）；取第一组
    first = items[0]
    if isinstance(first, (list, tuple)) and len(first) == 3 and isinstance(first[0], str):
        # items 本身就是 [(word, start, end), ...]
        rows = items
    elif isinstance(first, (list, tuple)):
        # 嵌套：items[0] 才是真正的词列表
        rows = list(first)
    else:
        rows = items

    out: list[tuple[int, int, str, float]] = []
    for row in rows:
        try:
            word, start_sec, end_sec = row
        except (ValueError, TypeError):
            continue
        if not isinstance(word, str) or not word.strip():
            continue
        try:
            s = float(start_sec)
            e = float(end_sec)
        except (TypeError, ValueError):
            continue
        ws = int(s * sample_rate)
        we = int(e * sample_rate)
        if we <= ws:
            we = ws + 1
        out.append((ws, we, word.strip(), 0.95))
    return tuple(out)


def _parse_qwen_align_results(
    align_items: Any, sample_rate: int, language: str,
) -> tuple[tuple[int, int, str, float], ...]:
    """解析 Qwen3-ForcedAligner.align() 的返回（独立调用时）。

    返回 ``[[Item(word, start_time, end_time), ...]]``，秒级。转 sample。
    """
    out: list[tuple[int, int, str, float]] = []
    for item in align_items:
        word = getattr(item, "text", None) or (item[0] if isinstance(item, (list, tuple)) else None)
        start = getattr(item, "start_time", None)
        end = getattr(item, "end_time", None)
        if start is None and isinstance(item, (list, tuple)) and len(item) >= 3:
            start = item[1]
            end = item[2]
        if not word or start is None or end is None:
            continue
        try:
            s = int(float(start) * sample_rate)
            e = int(float(end) * sample_rate)
        except (TypeError, ValueError):
            continue
        if e <= s:
            e = s + 1
        out.append((s, e, str(word).strip(), 0.95))
    return tuple(out)


def _parse_sortformer_segment(
    seg_str: str, sample_rate: int,
) -> _RawDiarizationSegment | None:
    """解析 Sortformer 的输出字符串 ``"begin,end,speaker_idx"`` → _RawDiarizationSegment。

    Sortformer.diarize() 返回秒级字符串；适配器转 sample。
    """
    try:
        parts = str(seg_str).strip().split(",")
        if len(parts) < 3:
            return None
        begin_sec = float(parts[0].strip())
        end_sec = float(parts[1].strip())
        speaker_idx = int(parts[2].strip())
    except (ValueError, IndexError):
        return None
    return _RawDiarizationSegment(
        sample_start=int(begin_sec * sample_rate),
        sample_end=int(end_sec * sample_rate),
        speaker_index=speaker_idx,
        sample_rate=sample_rate,
    )


# ---------------------------------------------------------------------- #
# 确定性 stub（单测用；不依赖真实 qwen_asr / nemo）
# ---------------------------------------------------------------------- #


def _stub_transcript(asset_id: str, sample_rate: int) -> list[_RawSegment]:
    """确定性 ASR stub：固定双段 + 词级对齐。"""
    second = sample_rate  # 1 秒 = sample_rate 个 sample
    return [
        _RawSegment(
            pts_start=int(1 * second),
            pts_end=int(3 * second),
            text="hello world",
            language="en",
            confidence=0.95,
            words=(
                (int(1.0 * second), int(1.5 * second), "hello", 0.97),
                (int(1.6 * second), int(2.0 * second), "world", 0.93),
            ),
            sample_rate=sample_rate,
        ),
        _RawSegment(
            pts_start=int(4 * second),
            pts_end=int(6 * second),
            text="this is a test",
            language="en",
            confidence=0.91,
            words=(
                (int(4.0 * second), int(4.4 * second), "this", 0.94),
                (int(4.5 * second), int(4.7 * second), "is", 0.90),
                (int(4.8 * second), int(5.0 * second), "a", 0.88),
                (int(5.1 * second), int(5.8 * second), "test", 0.92),
            ),
            sample_rate=sample_rate,
        ),
    ]


def _stub_diarization(asset_id: str, sample_rate: int) -> list[_RawDiarizationSegment]:
    """确定性 diarization stub：两说话人段（与 transcript 重叠）。"""
    second = sample_rate
    return [
        _RawDiarizationSegment(
            sample_start=int(0.5 * second), sample_end=int(3.5 * second),
            speaker_index=0, sample_rate=sample_rate,
        ),
        _RawDiarizationSegment(
            sample_start=int(3.8 * second), sample_end=int(6.5 * second),
            speaker_index=1, sample_rate=sample_rate,
        ),
    ]


# ---------------------------------------------------------------------- #
# 向后兼容别名（旧测试/代码可能引用 WhisperXTranscriptProvider 名）
# ---------------------------------------------------------------------- #
# R4-c：类已重命名为 QwenASRProvider。保留别名避免外部 import 立即断裂，
# 但新代码应直接用 QwenASRProvider。

WhisperXTranscriptProvider = QwenASRProvider
"""向后兼容别名——已弃用，新代码请用 :class:`QwenASRProvider`。"""


# ---------------------------------------------------------------------- #
# 兼容性 patch（qwen_asr / NeMo 与新版 transformers 的已知问题）
# ---------------------------------------------------------------------- #


_rope_scaling_patched = False
_processor_compat_patched = False
# AUD-14901：并发首次初始化引擎时，两个线程可能同时进入 patch 注册；
# 用锁保证幂等标志的 check-then-set 原子。
_compat_patch_lock = threading.Lock()


def _patch_qwen3_processor_compat() -> None:
    """Fill audio-token attributes missing in Transformers 4.57 Qwen2Tokenizer.

    ``qwen-asr==0.0.6`` still supports the Transformers 4.x backend, while
    ``Qwen2Tokenizer`` in 4.57 does not expose the three multimodal token
    fields from ``tokenizer_config.json`` as instance attributes.  The Qwen
    processor accesses those attributes during construction, so a native local
    model otherwise fails before its weights are used.  Read the values from
    ``init_kwargs`` (the config source of truth) and make the patch idempotent.
    """
    global _processor_compat_patched
    with _compat_patch_lock:
        if _processor_compat_patched:
            return
        try:
            import qwen_asr.core.transformers_backend.processing_qwen3_asr as _proc  # type: ignore[import-not-found]
        except ImportError:
            return
        original = _proc.Qwen3ASRProcessor.__init__

        def _patched_init(self: Any, feature_extractor: Any = None,
                          tokenizer: Any = None, chat_template: Any = None) -> Any:
            init_kwargs = getattr(tokenizer, "init_kwargs", {}) or {}
            if tokenizer is not None:
                # The original processor reads these attributes during
                # ProcessorMixin.__init__. Set them on the tokenizer first; doing
                # it afterwards is too late and produces the reported exception.
                for name, default in (
                    ("audio_token", "<|audio_pad|>"),
                    ("audio_bos_token", "<|audio_start|>"),
                    ("audio_eos_token", "<|audio_end|>"),
                ):
                    if not hasattr(tokenizer, name):
                        setattr(tokenizer, name, init_kwargs.get(name, default))
            original(self, feature_extractor, tokenizer, chat_template)
            self.audio_token = getattr(
                tokenizer, "audio_token", init_kwargs.get("audio_token", "<|audio_pad|>")
            )
            self.audio_bos_token = getattr(
                tokenizer, "audio_bos_token", init_kwargs.get("audio_bos_token", "<|audio_start|>")
            )
            self.audio_eos_token = getattr(
                tokenizer, "audio_eos_token", init_kwargs.get("audio_eos_token", "<|audio_end|>")
            )

        _proc.Qwen3ASRProcessor.__init__ = _patched_init
        _processor_compat_patched = True
        logger.info("Patched Qwen3ASRProcessor audio-token config compatibility")


def _patch_qwen3_rope_scaling_compat() -> None:
    """修补 qwen_asr 与新版 transformers (>=4.57) 的 rope_scaling=None 兼容问题。

    问题：transformers 4.57+ 不再为 Qwen3 架构自动填充 ``rope_scaling``（返回 None），
    而 ``qwen_asr`` 的 ``Qwen3ASRThinkerTextRotaryEmbedding.__init__`` 直接调
    ``config.rope_scaling.get("mrope_section", ...)``，导致 ``AttributeError``。

    修复：monkeypatch 该类的 ``__init__``，在 config.rope_scaling 为 None 时填入
    Qwen3 多模态 RoPE 的标准默认值 ``{"type": "mrope", "mrope_section": [24,20,20]}``。

    幂等——只 patch 一次（模块级 ``_rope_scaling_patched`` 标志）。

    参考：
    - https://github.com/modelscope/FunASR/issues/3042
    - https://github.com/QwenLM/Qwen3-VL-Embedding/issues/60
    """
    global _rope_scaling_patched
    with _compat_patch_lock:
        if _rope_scaling_patched:
            return
        try:
            import qwen_asr.core.transformers_backend.modeling_qwen3_asr as _mq  # type: ignore[import-not-found]
        except ImportError:
            return  # qwen_asr 未装，无需 patch

        _MROPE_DEFAULT: dict = {"type": "mrope", "mrope_section": [24, 20, 20]}
        _orig_init = _mq.Qwen3ASRThinkerTextRotaryEmbedding.__init__

        def _patched_init(self: Any, config: Any) -> Any:
            if getattr(config, "rope_scaling", None) is None:
                config.rope_scaling = dict(_MROPE_DEFAULT)
            return _orig_init(self, config)

        _mq.Qwen3ASRThinkerTextRotaryEmbedding.__init__ = _patched_init
        _rope_scaling_patched = True
        logger.info("Patched Qwen3ASRThinkerTextRotaryEmbedding for rope_scaling=None compat")


def _find_nemo_checkpoint(pack_path: str | None) -> str | None:
    """在 pack 目录下查找 ``.nemo`` 检查点文件。

    NeMo 的 ``restore_from`` 需要明确的 ``.nemo`` 文件路径（不是目录）。
    返回第一个找到的 ``.nemo`` 文件路径；无则 None。
    """
    import os

    if not pack_path or not os.path.isdir(pack_path):
        return None
    for name in os.listdir(pack_path):
        if name.endswith(".nemo"):
            return os.path.join(pack_path, name)
    return None
