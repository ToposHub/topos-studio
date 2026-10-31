"""ModelPackManager — 分析模型包生命周期（M4 / §16.4 / 计划 §11.1 M4-1）。

WhisperX/pyannote 等大模型**不加入基础安装**：它们体积大、许可证各异、需要 GPU/VRAM。
本模块描述"一个分析能力需要哪些模型包"，并管理其在本地 cache 的安装状态、完整性
（sha256）、许可证与硬件需求，使 UI 能在下载/分析前向用户**明示**这些信息
（§11.1 M4 红线：模型许可证、受限权重、GPU/CPU 依赖必须在安装和结果中明确，而不是
静默降级）。

设计约束（遵循 .clinerules）：

- **无 Qt 依赖**：纯 dataclass + 逻辑。下载 worker 由 host（AIModelManager 模式）注入，
  避免在核心层 import Qt，也避免把"网络/线程"耦合进可单测的数据层。
- **不静默降级**：模型未装 / sha256 不匹配 → 状态明确为 ``installed=False/verified=False``，
  provider 据此抛 ``ModelPackNotInstalledError``，绝不返回空结果假装"已分析"。
- **许可证是数据**：``commercial_use=False`` 的 pack（如 pyannote CC-BY-NC）在 UI 上必须
  警告；本模块只提供事实，由 UI 决定是否阻断。
- **工程不保存模型**：cache 在 ``~/.topos_cache/model_packs/``，可重建（§0：可重建的大型
  缓存不进入工程文件）。

M4 首版：注册内置 spec + 探测安装 + sha256 校验 + 解析 recipe 所需 pack。**真实下载
worker 留 M5**（接 AIModelManager 的下载管线）；本版的 ``mark_installed`` 由下载完成后的
host 回调调用。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from dataclasses import replace
from typing import Any, Mapping

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------- #
# 默认 cache 目录（项目内 models/ai_editing/，与降噪/抠像模型同源）
# ---------------------------------------------------------------------- #

def default_cache_dir() -> Path:
    """AI 剪辑模型包的默认 cache 目录：项目根/models/ai_editing/。

    与 ``src/shared/ai_models/`` 的 ``models/mask``、``models/denoise`` 同源（项目内），
    便于把所有离线模型集中放在 ``models/`` 下统一管理。``models/`` 已被
    ``.gitignore`` 忽略，模型权重文件不入库；每个 pack 落地为子目录，例如：
    ``models/ai_editing/rapidocr_ocr/``、``models/ai_editing/whisperx_base/``。

    项目根由本文件位置向上推导（AUD-31618）：本文件在
    ``<project>/src/shared/ai_models/generation/model_pack_manager.py``，
    先向上定位 ``src`` 目录、再取其父目录作为项目根——不再依赖
    ``parents[4]`` 这类硬编码深度（模块再移动一层就会静默写错位置）。
    """
    _here = Path(__file__).resolve()
    _project_root: Path | None = None
    for _parent in _here.parents:
        if _parent.name == "src" and _parent.is_dir():
            _project_root = _parent.parent
            break
    if _project_root is None:
        # 后备：无法定位 src 布局时退回旧的深度假设（src/shared/ai_models/
        # generation/model_pack_manager.py → parents[4] == 项目根）。
        _project_root = _here.parents[4]
        logger.warning(
            "default_cache_dir: 'src' ancestor not found under %s; "
            "falling back to fixed depth %s",
            _here, _project_root,
        )
    return _project_root / "models" / "ai_editing"


DEFAULT_CACHE_DIR = default_cache_dir()
"""模型包 cache 根目录（项目内 models/ai_editing/，可重建，不入工程文件）。"""

_MANIFEST_FILENAME = "manifest.json"
_RELEASE_MANIFEST_REQUIRED = "sha256:release-manifest-required"
_SHA256_RE = re.compile(r"^(?:sha256:)?[0-9a-fA-F]{64}$")


# ---------------------------------------------------------------------- #
# 异常
# ---------------------------------------------------------------------- #


class ModelPackNotInstalledError(RuntimeError):
    """请求的模型包未安装 / 未通过完整性校验（§11.1 M4：不静默降级）。

    provider 据此把 job 标 ``MODELPACK_MISSING``，UI 提示用户下载。
    """

    def __init__(self, pack_id: str, detail: str = "") -> None:
        self.pack_id = pack_id
        super().__init__(
            f"ModelPack {pack_id!r} is not installed or not verified"
            + (f": {detail}" if detail else "")
        )


# ---------------------------------------------------------------------- #
# 数据模型
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ModelPackLicense:
    """模型包许可证与限制（§16.4）。

    ``commercial_use=False``（如 pyannote CC-BY-NC）的 pack 在商用场景需 UI 警告；
    本字段只陈述事实，是否阻断由产品策略决定。
    """

    name: str                       # "WhisperX (MIT)" / "pyannote 3.x (CC-BY-NC-4.0)"
    commercial_use: bool            # 商用授权
    redistribution: bool            # 是否允许再分发
    attribution_required: bool      # 是否要求署名
    notes: str = ""                 # 额外限制说明（如"仅研究用途"）


@dataclass(frozen=True)
class ModelPackSpec:
    """一个分析能力所需的模型包描述（M4 / §16.4）。

    一个 recipe（如 EDITORIAL_LOCAL）可能依赖多个 pack（transcript + align + diarize）。
    ``resolve_for_recipe`` 据此返回所需 pack 列表，供下载/分析前向用户展示体积、硬件、
    许可证。
    """

    pack_id: str                    # "whisperx_base" / "pyannote_diarize_3" / "whisperx_align"
    capability: str                 # "transcript" / "align" / "diarization"
    version: str                    # 模型版本（用于失效与复现）
    size_bytes: int                 # 下载体积（UI 展示）
    sha256: str                     # 完整性校验（下载/安装后校验）
    languages: tuple[str, ...]      # 支持语言代码（空=语言无关，如 diarize）
    requires_gpu: bool              # 是否强制 GPU
    min_vram_mb: int                # 最小显存（requires_gpu=False 时为 0）
    license: ModelPackLicense
    download_url: str = ""          # 官方下载地址（下载 worker 用；首版可不填）
    notes: str = ""                 # 人类可读备注（如"对齐模型缺失则降级为段级"）

    @property
    def has_release_integrity(self) -> bool:
        """Whether this spec carries a real, release-supplied SHA-256."""
        return bool(_SHA256_RE.fullmatch(self.sha256 or ""))

    @property
    def needs_release_manifest(self) -> bool:
        """Whether a release manifest must replace the development metadata."""
        # Vault-provided specs historically used several placeholder spellings
        # as well as the main application's sentinel.  In a strict host none
        # of them is release integrity metadata, so treat every non-digest as
        # requiring the controlled manifest instead of surfacing a misleading
        # ordinary "not installed" state.
        return not self.has_release_integrity

    def to_dict(self) -> dict[str, Any]:
        return {
            "pack_id": self.pack_id,
            "capability": self.capability,
            "version": self.version,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "languages": list(self.languages),
            "requires_gpu": self.requires_gpu,
            "min_vram_mb": self.min_vram_mb,
            "license": {
                "name": self.license.name,
                "commercial_use": self.license.commercial_use,
                "redistribution": self.license.redistribution,
                "attribution_required": self.license.attribution_required,
                "notes": self.license.notes,
            },
            "download_url": self.download_url,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class ModelPackStatus:
    """某 pack 在本机的安装状态（探测结果）。"""

    pack_id: str
    installed: bool                 # 目录存在且 manifest 记录已装
    install_path: str | None        # 安装目录绝对路径（未装为 None）
    verified: bool                  # sha256 校验通过（installed=True 但 verified=False 视为损坏）
    languages_available: tuple[str, ...] = ()  # 实际可用语言（=spec.languages 的子集）
    reason: str = ""
    verification_mode: str = "strict"  # strict / demo / unverified

    @property
    def usable(self) -> bool:
        """installed 且 verified → 可用。"""
        return self.installed and self.verified

    @property
    def is_demo(self) -> bool:
        """Development-only directory-exists verification, never production."""
        return self.verification_mode == "demo"


# ---------------------------------------------------------------------- #
# 内置 spec 常量（Qwen3 默认 + WhisperX/Demucs 可选备用；真实 URL/体积按官方填）
# ---------------------------------------------------------------------- #

# M8: RapidOCR（本地 OCR，CPU 可跑，Apache-2.0）。模型权重来自 PaddleOCR/ONNX。
# 许可证宽松（商用、可再分发、需署名）。
_RAPIDOCR_LICENSE = ModelPackLicense(
    name="RapidOCR (Apache-2.0)",
    commercial_use=True,
    redistribution=True,
    attribution_required=True,
    notes="RapidOCR is Apache-2.0; bundled detection/recognition models are derived "
          "from PaddleOCR (ONNX). Verify model-card terms before redistribution.",
)

# ---------------------------------------------------------------------- #
# 2026 现代模型许可证（Qwen3-ASR / Qwen3-ForcedAligner / NVIDIA Sortformer）
# ----------------------------------------------------------------------
# R4-c：彻底替换旧的 WhisperX + pyannote。这些模型在中文准确率、商用许可、
# Mac 原生加速三个维度有显著优势。详见 models/ai_editing/README.md 的
# 「2026 推荐替代模型」章节。

_QWEN3_LICENSE = ModelPackLicense(
    name="Qwen3-ASR (Apache-2.0)",
    commercial_use=True,
    redistribution=True,
    attribution_required=True,
    notes="Qwen3-ASR / Qwen3-ForcedAligner are Apache-2.0 (Alibaba Cloud). "
          "Best Chinese ASR accuracy in 2026; native Apple Silicon support via MLX.",
)

_NEMO_LICENSE = ModelPackLicense(
    name="NVIDIA NeMo Sortformer (Apache-2.0)",
    commercial_use=True,
    redistribution=True,
    attribution_required=True,
    notes="NVIDIA NeMo is Apache-2.0. Sortformer diarization is the commercial-friendly "
          "alternative to pyannote (CC-BY-NC-4.0). Comparable or better DER.",
)

_WHISPERX_LICENSE = ModelPackLicense(
    name="WhisperX / Whisper (MIT; verify model terms)",
    commercial_use=True,
    redistribution=True,
    attribution_required=True,
    notes="Optional multilingual fallback. The selected Whisper checkpoint and "
          "language alignment model may carry additional terms; show them before download.",
)

_DEMUCS_LICENSE = ModelPackLicense(
    name="Demucs v4 (MIT)",
    commercial_use=True,
    redistribution=True,
    attribution_required=True,
    notes="Optional music source separation backend; use only for music-heavy speech.",
)

_QWEN3_TTS_LICENSE = ModelPackLicense(
    name="Qwen3-TTS (Apache-2.0; verify checkpoint terms)",
    commercial_use=True,
    redistribution=True,
    attribution_required=True,
    notes="Qwen3-TTS runtime and checkpoints are multilingual speech-generation models; "
          "show the selected checkpoint terms before redistribution.",
)

# 语言代码约定：ISO 639-1（en/zh/ja/...）。空 tuple = 语言无关（diarize 只看声纹）。
# M8: 本地 OCR pack（RapidOCR + PaddleOCR ONNX 模型，中文优先，CPU 可跑）。
# 与 ASR pack 分开：体积小、不需 GPU，便于低端机也能跑视觉文字检索。
BUILTIN_RAPIDOCR = ModelPackSpec(
    pack_id="rapidocr_ocr",
    capability="ocr",
    version="rapidocr-multilingual-v1",
    size_bytes=15_000_000,           # ~15MB（det + rec ONNX）
    sha256=_RELEASE_MANIFEST_REQUIRED,
    languages=("zh", "en", "ja", "ko"),  # 中文优先
    requires_gpu=False,             # CPU 可跑（GPU 可选加速）
    min_vram_mb=0,
    license=_RAPIDOCR_LICENSE,
    notes="Local OCR (RapidOCR + PaddleOCR ONNX). Text output is untrusted data "
          "(§9): never enters subtitles without explicit user confirmation.",
)

# ====================================================================== #
# 2026 现代模型（R4-c：provider 已接真实推理）
# ====================================================================== #
# 彻底替换旧的 WhisperX + pyannote。Qwen3-ASR 是当前中文最优开源 ASR；
# Sortformer 是商用友好的说话人分离。详见 models/ai_editing/README.md 的
# 「2026 推荐替代模型」章节。

BUILTIN_QWEN3_ASR_SMALL = ModelPackSpec(
    pack_id="qwen3_asr",
    capability="transcript",
    version="qwen3-asr-0.6b",
    size_bytes=1_200_000_000,        # ~1.2GB
    sha256=_RELEASE_MANIFEST_REQUIRED,
    languages=("en", "zh", "ja", "de", "fr", "es", "ko", "ru", "it", "pt",
               "ar", "hi", "vi", "th", "id"),  # Qwen3-ASR 支持 52 语言，列主要的
    requires_gpu=True,              # 训练态需 GPU；MLX 在 Mac 上可 CPU/MPS 加速
    min_vram_mb=2000,
    license=_QWEN3_LICENSE,
    # qwen-asr runtime uses the plain checkpoint. The ``-hf`` variant is the
    # native Transformers layout and is not interchangeable with qwen-asr.
    download_url="https://huggingface.co/Qwen/Qwen3-ASR-0.6B",
    notes="Qwen3-ASR 0.6B (Alibaba, Apache-2.0). Best Chinese ASR in 2026. "
          "Native Apple Silicon via mlx-qwen3-asr. 52 languages. Recommended default.",
)

BUILTIN_QWEN3_ASR_BASE = ModelPackSpec(
    pack_id="qwen3_asr_large",
    capability="transcript",
    version="qwen3-asr-1.7b",
    size_bytes=3_400_000_000,        # ~3.4GB
    sha256=_RELEASE_MANIFEST_REQUIRED,
    languages=("en", "zh", "ja", "de", "fr", "es", "ko", "ru", "it", "pt",
               "ar", "hi", "vi", "th", "id"),
    requires_gpu=True,
    min_vram_mb=4000,
    license=_QWEN3_LICENSE,
    download_url="https://huggingface.co/Qwen/Qwen3-ASR-1.7B",
    notes="Qwen3-ASR 1.7B (Alibaba, Apache-2.0). Higher accuracy than 0.6B. "
          "~5.9% WER. 52 languages. For CUDA GPU or vLLM serving.",
)

BUILTIN_QWEN3_ALIGN = ModelPackSpec(
    pack_id="qwen3_align",
    capability="align",
    version="qwen3-forced-aligner",
    size_bytes=360_000_000,          # ~360MB
    sha256=_RELEASE_MANIFEST_REQUIRED,
    languages=("en", "zh", "ja", "de", "fr", "es", "ko", "ru", "it", "pt",
               "ar", "hi", "vi", "th", "id"),
    requires_gpu=True,
    min_vram_mb=2000,
    license=_QWEN3_LICENSE,
    download_url="https://huggingface.co/Qwen/Qwen3-ForcedAligner",
    notes="Qwen3-ForcedAligner (Apache-2.0). Word-level alignment for 52 languages; "
          "replaces wav2vec2 (English-only). Same team as Qwen3-ASR.",
)

BUILTIN_NEMO_SORTFORMER = ModelPackSpec(
    pack_id="nemo_sortformer",
    capability="diarization",
    version="sortformer-3.1",
    size_bytes=90_000_000,           # ~90MB
    sha256=_RELEASE_MANIFEST_REQUIRED,
    languages=(),                   # 语言无关（只看声纹）
    requires_gpu=True,
    min_vram_mb=2000,
    license=_NEMO_LICENSE,
    download_url="https://huggingface.co/nvidia/sortformer-3.1",
    notes="NVIDIA Sortformer (Apache-2.0). Commercial-friendly alternative to "
          "pyannote (CC-BY-NC-4.0). ~10-15% DER. Recommended for commercial use.",
)

BUILTIN_WHISPERX = ModelPackSpec(
    pack_id="whisperx_base",
    capability="transcript_fallback",
    version="whisperx-large-v3",
    size_bytes=3_000_000_000,
    sha256=_RELEASE_MANIFEST_REQUIRED,
    languages=("en", "zh", "ja", "de", "fr", "es", "ko", "ru", "it", "pt"),
    requires_gpu=True,
    min_vram_mb=4000,
    license=_WHISPERX_LICENSE,
    download_url="https://github.com/m-bain/whisperX",
    notes="Optional fallback for language coverage, word alignment, and diarization.",
)

BUILTIN_DEMUCS_V4 = ModelPackSpec(
    pack_id="demucs_v4",
    capability="speech_preprocess",
    version="htdemucs-v4",
    size_bytes=1_500_000_000,
    sha256=_RELEASE_MANIFEST_REQUIRED,
    languages=(),
    requires_gpu=True,
    min_vram_mb=3000,
    license=_DEMUCS_LICENSE,
    download_url="https://github.com/facebookresearch/demucs",
    notes="Optional vocals stem extraction before ASR; not enabled for every asset.",
)

BUILTIN_QWEN3_TTS_CUSTOMVOICE = ModelPackSpec(
    pack_id="qwen3_tts_customvoice",
    capability="tts",
    version="qwen3-tts-12hz-1.7b-customvoice",
    size_bytes=3_400_000_000,
    sha256=_RELEASE_MANIFEST_REQUIRED,
    languages=("zh", "en", "ja", "ko", "de", "fr", "ru", "pt", "es", "it"),
    requires_gpu=True,
    min_vram_mb=5000,
    license=_QWEN3_TTS_LICENSE,
    download_url="https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
    notes="Default subtitle-to-speech model. Requires qwen-tts; tokenizer assets are "
          "resolved by the Qwen3-TTS package/model directory.",
)

BUILTIN_QWEN3_TTS_TOKENIZER = ModelPackSpec(
    pack_id="qwen3_tts_tokenizer_12hz",
    capability="tts_tokenizer",
    version="qwen3-tts-tokenizer-12hz",
    size_bytes=400_000_000,
    sha256=_RELEASE_MANIFEST_REQUIRED,
    languages=(),
    requires_gpu=False,
    min_vram_mb=0,
    license=_QWEN3_TTS_LICENSE,
    download_url="https://huggingface.co/Qwen/Qwen3-TTS-Tokenizer-12Hz",
    notes="Required codec/tokenizer assets for offline Qwen3-TTS inference.",
)

BUILTIN_QWEN3_TTS_BASE = ModelPackSpec(
    pack_id="qwen3_tts_base",
    capability="tts_voice_clone",
    version="qwen3-tts-12hz-1.7b-base",
    size_bytes=3_400_000_000,
    sha256=_RELEASE_MANIFEST_REQUIRED,
    languages=("zh", "en", "ja", "ko", "de", "fr", "ru", "pt", "es", "it"),
    requires_gpu=True,
    min_vram_mb=5000,
    license=_QWEN3_TTS_LICENSE,
    download_url="https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base",
    notes="Optional 3-second voice-clone model; only use with user-authorized reference audio.",
)

BUILTIN_SPECS: tuple[ModelPackSpec, ...] = (
    # ASR + 对齐 + 分离（R4-c：Qwen3-ASR / ForcedAligner / Sortformer 已接真实推理）
    BUILTIN_QWEN3_ASR_SMALL,
    BUILTIN_QWEN3_ASR_BASE,
    BUILTIN_QWEN3_ALIGN,
    BUILTIN_NEMO_SORTFORMER,
    BUILTIN_WHISPERX,
    BUILTIN_DEMUCS_V4,
    BUILTIN_QWEN3_TTS_CUSTOMVOICE,
    BUILTIN_QWEN3_TTS_TOKENIZER,
    BUILTIN_QWEN3_TTS_BASE,
    # OCR（R4-a：RapidOCR 已接真实推理）
    BUILTIN_RAPIDOCR,
)
"""内置模型包清单（Qwen3 默认，WhisperX/Demucs 按需备用）。

host 据此在 UI 展示体积、许可证和硬件需求。Qwen3-ASR + Sortformer 是默认链路；
WhisperX 仅用于语言/对齐/说话人质量回退，Demucs 仅用于音乐覆盖严重的音频。
详见 models/ai_editing/README.md。
"""


# ---------------------------------------------------------------------- #
# recipe → 所需 capability 映射（与 analysis_service.resolve_providers 对齐）
# ---------------------------------------------------------------------- #

# 注意：这里映射的是 *capability*（transcript/align/diarization），不是 pack_id。
# 因为一个 capability 可能有多个可选 pack（whisperx_base vs whisperx_medium），
# 具体选哪个 pack 由用户/硬件检测在 resolve_for_recipe 里决定。
_RECIPE_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "fast_local": (),                                                       # FAST_LOCAL 不需 ASR
    "editorial_local": ("transcript", "align", "diarization"),              # 口播/访谈/粗剪
    "visual_semantic_local": ("transcript", "align", "diarization", "ocr"),  # M8: +本地 OCR
    "cloud_enhanced": ("transcript", "align", "diarization"),               # 云 ASR（M10）
}


# ---------------------------------------------------------------------- #
# ModelPackManager
# ---------------------------------------------------------------------- #


@dataclass
class _InstalledRecord:
    """manifest 里的一条安装记录。"""

    pack_id: str
    install_path: str
    sha256: str                      # 安装时记录的 sha（用于和 spec.sha256 比对）
    languages: tuple[str, ...] = ()
    verification_mode: str = "strict"
    last_error: str = ""


class ModelPackManager:
    """模型包生命周期（§16.4 / M4-1）。

    职责：
    - 注册内置 pack spec（Qwen3 默认，WhisperX/Demucs 可选备用）。
    - 探测本地 cache 安装状态（目录 + manifest + sha256）。
    - sha256 完整性校验（损坏标记 verified=False，provider 据此报错不静默）。
    - 解析某 recipe 所需的 pack 列表（供下载前向用户展示体积/许可证/硬件）。
    - mark_installed / remove（下载 worker 完成 / 用户删除时调）。

    不负责：真实下载（host 注入 worker）、GPU/VRAM 探测（host 据此过滤）、
    模型推理（provider 负责）。
    """

    def __init__(
        self,
        cache_dir: Path | str | None = None,
        *,
        specs: tuple[ModelPackSpec, ...] = BUILTIN_SPECS,
        strict_verification: bool = True,
    ) -> None:
        """
        Args:
            cache_dir: cache 根目录；None 用 :data:`DEFAULT_CACHE_DIR`。
            specs: 启动即注册的 spec 清单；默认 :data:`BUILTIN_SPECS`。
        """
        self._cache_dir = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE_DIR
        # ``False`` is retained solely for explicitly configured deterministic/offline
        # demo hosts and unit fixtures. Production defaults to strict verification;
        # a directory with a placeholder release hash is never reported as usable.
        self._strict_verification = bool(strict_verification)
        self._specs: dict[str, ModelPackSpec] = {}
        for spec in specs:
            self.register_spec(spec)
        # manifest：pack_id → _InstalledRecord（磁盘上的 cache 真相）
        self._manifest_path = self._cache_dir / _MANIFEST_FILENAME
        # AUD-31619：_installed 的变更（mark_installed / remove / verify）
        # 可能来自下载 worker 回调线程，而 get_status / require_usable 来自
        # UI / 推理线程——统一由可重入锁保护（_save_manifest 内部再取同一
        # 锁做快照，RLock 允许嵌套）。
        self._lock = threading.RLock()
        self._installed: dict[str, _InstalledRecord] = {}
        # 目录校验结果缓存（AUD-31614）：install_path → (目录指纹, 期望
        # sha256, allow_demo, verified)。指纹只收集 relpath/size/mtime_ns
        #（不读文件内容），目录未变化时跳过 GB 级全量重哈希。
        self._verify_cache: dict[
            str, tuple[tuple[tuple[str, int, int], ...], str, bool, bool]
        ] = {}
        self._load_manifest()

    # ---------------- spec 注册 ---------------- #

    def register_spec(self, spec: ModelPackSpec) -> None:
        """注册/覆盖一个 pack spec。"""
        self._specs[spec.pack_id] = spec

    def list_specs(self) -> tuple[ModelPackSpec, ...]:
        """所有已注册 spec（按 pack_id 排序，便于 UI 稳定展示）。"""
        return tuple(self._specs[k] for k in sorted(self._specs.keys()))

    def get_spec(self, pack_id: str) -> ModelPackSpec | None:
        return self._specs.get(pack_id)

    def needs_release_manifest(self, pack_id: str | None = None) -> bool:
        """Return whether strict verification still lacks release metadata.

        Built-in source metadata intentionally has no fabricated weight digest.
        Install UI must therefore repair the controlled release-manifest gate
        before it suggests that a model pack can be downloaded and verified.
        """
        specs = (
            (self._specs.get(pack_id),) if pack_id is not None
            else tuple(self._specs.values())
        )
        return any(
            spec is not None and spec.needs_release_manifest
            for spec in specs
        )

    # ---------------- 安装状态探测 ---------------- #

    def get_status(self, pack_id: str) -> ModelPackStatus:
        """探测某 pack 的安装 + 完整性状态。

        installed = manifest 有记录且目录存在；
        verified = installed 且记录的 sha256 与 spec.sha256 一致（防止 spec 升级后旧 cache 被误用）。
        """
        spec = self._specs.get(pack_id)
        if spec is None:
            return ModelPackStatus(pack_id=pack_id, installed=False, install_path=None, verified=False,
                                   reason="未知模型包")

        record = self._installed.get(pack_id)
        if record is None or not os.path.isdir(record.install_path):
            return ModelPackStatus(pack_id=pack_id, installed=False, install_path=None, verified=False,
                                   reason="模型包未安装")

        # spec 升级后 sha 变化 → 旧 cache 视为未校验（需重新下载/校验）
        demo = not spec.has_release_integrity and not self._strict_verification
        verified = record.sha256 == spec.sha256 and self._verify_dir_cached(
            record.install_path, spec.sha256, allow_demo=demo,
        )
        reason = ""
        if not spec.has_release_integrity and self._strict_verification:
            reason = "等待发布清单提供真实 SHA-256"
        elif not verified:
            reason = record.last_error or "模型包完整性校验失败"
        return ModelPackStatus(
            pack_id=pack_id,
            installed=True,
            install_path=record.install_path,
            verified=verified,
            languages_available=record.languages,
            reason=reason,
            verification_mode="demo" if demo else record.verification_mode,
        )

    def all_status(self) -> dict[str, ModelPackStatus]:
        """所有 spec 的状态（UI 一次性展示用）。"""
        return {pid: self.get_status(pid) for pid in self._specs}

    def require_usable(self, pack_id: str) -> str:
        """要求某 pack 可用，否则抛 :class:`ModelPackNotInstalledError`。

        返回安装路径。provider 在推理前调本方法，把"模型缺失"显式化（不静默）。
        """
        status = self.get_status(pack_id)
        if not status.usable:
            detail = "not installed" if not status.installed else status.reason or "sha256 verification failed (cache corrupt or spec upgraded)"
            raise ModelPackNotInstalledError(pack_id, detail=detail)
        assert status.install_path is not None  # usable 蕴含
        return status.install_path

    # ---------------- 安装 / 删除（由 host 下载 worker 驱动）---------------- #

    def mark_installed(
        self,
        pack_id: str,
        install_path: str | Path,
        *,
        languages: tuple[str, ...] | None = None,
        verify: bool = True,
    ) -> bool:
        """标记某 pack 已安装（下载 worker 完成回调用）。

        Args:
            install_path: 解压后的模型目录。
            languages: 实际可用语言（默认 spec.languages）。
            verify: 是否在标记前校验 sha256；校验失败不标记并返回 False。
        Returns:
            是否标记成功（sha256 校验通过）。
        """
        spec = self._specs.get(pack_id)
        if spec is None:
            logger.warning("mark_installed: unknown pack_id %r", pack_id)
            return False
        path = str(install_path)
        if not os.path.isdir(path):
            logger.warning("mark_installed: install path is not a directory for %r", pack_id)
            return False
        demo = not spec.has_release_integrity and not self._strict_verification
        # 显式 mark_installed 走全量校验并作废该目录的缓存校验结果。
        self._verify_cache.pop(path, None)
        if verify and not self._verify_dir(path, spec.sha256, allow_demo=demo):
            logger.warning("mark_installed: sha256 mismatch for %r, not marking", pack_id)
            with self._lock:  # AUD-31619：与读取线程互斥
                self._installed[pack_id] = _InstalledRecord(
                    pack_id=pack_id, install_path=path, sha256="",
                    languages=languages if languages is not None else spec.languages,
                    verification_mode="unverified", last_error="sha256 verification failed",
                )
                self._save_manifest()
            return False
        with self._lock:  # AUD-31619：与读取线程互斥
            self._installed[pack_id] = _InstalledRecord(
                pack_id=pack_id,
                install_path=path,
                sha256=spec.sha256,
                languages=languages if languages is not None else spec.languages,
                verification_mode="demo" if demo else "strict",
            )
            self._save_manifest()
        logger.info("ModelPack %s marked installed at %s", pack_id, path)
        return True

    def remove(self, pack_id: str, *, delete_files: bool = True) -> bool:
        """删除某 pack 的安装记录（可选删除磁盘文件）。

        Returns:
            是否删除了一条记录（未装则 False）。
        """
        with self._lock:  # AUD-31619：pop + manifest 保存原子化
            record = self._installed.pop(pack_id, None)
            if record is None:
                return False
            self._verify_cache.pop(record.install_path, None)
            self._save_manifest()
        if delete_files:
            try:
                # 只删 cache_dir 之内的目录，防止误删（§0 安全：不写绝对路径到工程）
                abs_install = os.path.abspath(record.install_path)
                abs_cache = os.path.abspath(str(self._cache_dir))
                if os.path.commonpath([abs_cache, abs_install]) == abs_cache:
                    import shutil
                    shutil.rmtree(abs_install, ignore_errors=True)
                else:
                    logger.warning(
                        "remove: install_path %s outside cache_dir %s, skipping file delete",
                        abs_install, abs_cache,
                    )
            except Exception:
                logger.debug("remove: failed to delete files for %s", pack_id, exc_info=True)
        self._save_manifest()
        logger.info("ModelPack %s removed", pack_id)
        return True

    # ---------------- recipe 解析 ---------------- #

    def resolve_for_recipe(
        self,
        recipe_id: str,
        *,
        available_languages: tuple[str, ...] | None = None,
    ) -> tuple[ModelPackSpec, ...]:
        """解析某 recipe 所需的 pack 列表（M4-1 / §16.4）。

        按 capability（transcript/align/diarization）选 pack；若某 capability 有多个候选
        pack（如 whisperx_base vs medium），首版取 list_specs 顺序的第一个（host 后续可按
        硬件/用户偏好覆盖）。

        Args:
            recipe_id: AnalysisRecipe.value（"fast_local"/"editorial_local"/...）。
            available_languages: 用户素材涉及的语言；用于过滤 align pack（如 align 只覆盖 en）。
                None 时不按语言过滤。
        Returns:
            所需 pack spec 列表（FAST_LOCAL 为空）。
        """
        capabilities = _RECIPE_CAPABILITIES.get(recipe_id, ())
        if not capabilities:
            return ()
        all_specs = self.list_specs()
        result: list[ModelPackSpec] = []
        for cap in capabilities:
            for spec in all_specs:
                if spec.capability != cap:
                    continue
                # 语言过滤：align 这类语言相关 pack，若用户语言不在覆盖范围仍返回
                # （provider 据此降级），但优先返回覆盖了该语言的 pack。
                if (
                    available_languages
                    and spec.languages
                    and not any(lang in spec.languages for lang in available_languages)
                ):
                    # 该 pack 不覆盖用户任何语言 → 跳过，provider 会走降级
                    continue
                result.append(spec)
                break  # 每个 capability 取第一个匹配
        return tuple(result)

    def missing_for_recipe(
        self,
        recipe_id: str,
        *,
        available_languages: tuple[str, ...] | None = None,
    ) -> tuple[ModelPackSpec, ...]:
        """recipe 所需但尚未 usable 的 pack（UI 据此提示下载）。"""
        return tuple(
            s for s in self.resolve_for_recipe(
                recipe_id, available_languages=available_languages
            )
            if not self.get_status(s.pack_id).usable
        )

    # ---------------- sha256 校验 ---------------- #

    def verify(self, pack_id: str) -> bool:
        """显式校验某 pack 目录的 sha256（更新 cache 真相）。"""
        spec = self._specs.get(pack_id)
        record = self._installed.get(pack_id)
        if spec is None or record is None:
            return False
        demo = not spec.has_release_integrity and not self._strict_verification
        # 显式校验必须重哈希：绕过 _verify_dir_cached 的指纹缓存，成功后
        # 以新指纹回填，使紧随其后的 get_status 免于再次全量哈希。
        fingerprint = self._dir_fingerprint(record.install_path)
        self._verify_cache.pop(record.install_path, None)
        ok = self._verify_dir(record.install_path, spec.sha256, allow_demo=demo)
        if fingerprint is not None:
            self._verify_cache[record.install_path] = (
                fingerprint, spec.sha256, demo, ok,
            )
        if not ok:
            # 标记损坏：保留记录但 sha 记为不匹配（get_status.verified=False）
            with self._lock:  # AUD-31619
                record.sha256 = ""
                record.verification_mode = "unverified"
                record.last_error = "sha256 verification failed"
                self._save_manifest()
        else:
            # 成功也必须回写（AUD-31616）：否则 record.sha256 仍是旧值，
            # 发布清单更新摘要（或文件被外部替换为匹配新 spec 的内容）时
            # verify() 返回 True 而 get_status 仍算 verified=False，
            # require_usable 继续抛错——显式校验通过的结论无法持久化。
            with self._lock:  # AUD-31619
                record.sha256 = spec.sha256
                record.verification_mode = "demo" if demo else "strict"
                record.last_error = ""
                self._save_manifest()
        return ok

    @staticmethod
    def _dir_fingerprint(
        install_path: str,
    ) -> tuple[tuple[str, int, int], ...] | None:
        """目录指纹：每个文件的 (相对路径, 大小, mtime_ns)，不读内容。

        供校验结果缓存判断目录是否变化（AUD-31614）；stat 失败（目录
        被删/权限问题）返回 None，调用方回退为直接校验。
        """
        entries: list[tuple[str, int, int]] = []
        for root, dirs, files in os.walk(install_path):
            dirs.sort()
            for name in sorted(files):
                fp = os.path.join(root, name)
                try:
                    st = os.stat(fp)
                except OSError:
                    return None
                rel = os.path.relpath(fp, install_path).replace(os.sep, "/")
                entries.append((rel, st.st_size, st.st_mtime_ns))
        return tuple(entries)

    def _verify_dir_cached(
        self,
        install_path: str,
        expected_sha256: str,
        *,
        allow_demo: bool = False,
    ) -> bool:
        """:meth:`_verify_dir` + 目录指纹缓存（AUD-31614）。

        get_status / require_usable 在每次转写、TTS、OCR 请求前都会被调
        用；无缓存时每次都对 0.36–3.4GB 的权重目录全量重读重哈希（SSD 上
        秒级到几十秒）。指纹采集只 stat 每个文件（不读内容），目录未变
        化且期望摘要相同时直接复用上次校验结果。
        """
        fingerprint = self._dir_fingerprint(install_path)
        if fingerprint is None:
            return self._verify_dir(
                install_path, expected_sha256, allow_demo=allow_demo
            )
        cached = self._verify_cache.get(install_path)
        if cached is not None:
            cached_fp, cached_sha, cached_demo, cached_result = cached
            if (
                cached_fp == fingerprint
                and cached_sha == expected_sha256
                and cached_demo == allow_demo
            ):
                return cached_result
        result = self._verify_dir(
            install_path, expected_sha256, allow_demo=allow_demo
        )
        self._verify_cache[install_path] = (
            fingerprint, expected_sha256, allow_demo, result,
        )
        return result

    @staticmethod
    def _verify_dir(
        install_path: str,
        expected_sha256: str,
        *,
        allow_demo: bool = False,
    ) -> bool:
        """校验目录内所有文件的 sha256 串联是否匹配 expected。

        ``release-manifest-required`` 只允许被显式 demo host 使用；严格应用 host
        必须先加载发布清单提供的 64 位十六进制 hash。这样不会把开发目录存在误报
        为真实模型已验收。
        """
        if not os.path.isdir(install_path):
            return False
        if expected_sha256 == _RELEASE_MANIFEST_REQUIRED:
            return allow_demo
        if not _SHA256_RE.fullmatch(expected_sha256 or ""):
            return allow_demo
        # 严格校验：拼接目录树所有文件 sha256，再 hash 一次得到目录摘要。
        # 子目录必须排序（AUD-31615）：os.walk 的目录枚举顺序取决于文件系
        # 统，未排序时同一目录树在不同机器/拷贝后可算出不同 digest，已正
        # 确安装的 pack 会被误判"校验失败"。发布清单生成端须用同一算法。
        h = hashlib.sha256()
        for root, dirs, files in os.walk(install_path):
            dirs.sort()
            for name in sorted(files):
                fp = os.path.join(root, name)
                try:
                    rel = os.path.relpath(fp, install_path).replace(os.sep, "/")
                    h.update(rel.encode("utf-8"))
                    h.update(b"\0")
                    with open(fp, "rb") as f:
                        for chunk in iter(lambda: f.read(65536), b""):
                            h.update(chunk)
                except OSError:
                    return False
        digest = h.hexdigest()
        expected = expected_sha256.split(":", 1)[-1] if ":" in expected_sha256 else expected_sha256
        return digest == expected

    # ---------------- manifest 持久化 ---------------- #

    def _load_manifest(self) -> None:
        try:
            if not self._manifest_path.is_file():
                return
            with open(self._manifest_path, encoding="utf-8") as f:
                data = json.load(f)
            with self._lock:  # AUD-31619：__init__ 单线程调用，防御性持锁
                for entry in data.get("installed", []):
                    pid = entry.get("pack_id", "")
                    if not pid:
                        continue
                    self._installed[pid] = _InstalledRecord(
                        pack_id=pid,
                        install_path=str(entry.get("install_path", "")),
                        sha256=str(entry.get("sha256", "")),
                        languages=tuple(entry.get("languages", ()) or ()),
                        verification_mode=str(entry.get("verification_mode", "strict")),
                        last_error=str(entry.get("last_error", "")),
                    )
        except (OSError, json.JSONDecodeError, ValueError):
            # AUD-31617：损坏的 manifest 意味着全部安装记录清零（模型将被
            # 判为未安装），必须 warning 留痕，不能 debug 静默吞掉。
            logger.warning(
                "model_pack manifest load failed (all install records "
                "reset to not-installed): %s",
                self._manifest_path,
                exc_info=True,
            )

    def _save_manifest(self) -> None:
        # AUD-31619：锁内快照（values() 迭代期间并发 mark_installed/remove
        # 会抛 RuntimeError: dictionary changed size during iteration）。
        with self._lock:
            records = list(self._installed.values())
            data = {
                "installed": [
                    {
                        "pack_id": r.pack_id,
                        "install_path": r.install_path,
                        "sha256": r.sha256,
                        "languages": list(r.languages),
                        "verification_mode": r.verification_mode,
                        "last_error": r.last_error,
                    }
                    for r in records
                ],
            }
        # AUD-31617：原子写。truncate 式 open("w") 在写盘中途崩溃/断电会让
        # manifest 截断损坏 → 下次启动全部安装记录静默清零。先写临时文件
        # 再 os.replace（同目录、同文件系统，POSIX 原子）。
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{_MANIFEST_FILENAME}.", dir=self._cache_dir
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_name, self._manifest_path)
            finally:
                try:
                    os.unlink(tmp_name)
                except FileNotFoundError:
                    pass
        except OSError:
            logger.warning("model_pack manifest save failed", exc_info=True)

    # ---------------- 工具 ---------------- #

    @property
    def cache_dir(self) -> Path:
        return self._cache_dir

    # ---------------- release manifest ---------------- #

    def apply_release_manifest(self, manifest: Mapping[str, Any]) -> tuple[str, ...]:
        """Apply verified release metadata without accepting fake hashes.

        The checked-in catalog intentionally contains download/licence metadata
        but no fabricated model-weight digests.  A signed/controlled release
        manifest supplies the real digest at install time.  Invalid entries are
        ignored and returned to the caller for a visible diagnostic.
        """
        entries = manifest.get("packs", manifest.get("models", ()))
        if not isinstance(entries, (list, tuple)):
            raise ValueError("release manifest packs must be a list")
        rejected: list[str] = []
        for raw in entries:
            if not isinstance(raw, Mapping):
                rejected.append("<invalid>")
                continue
            pack_id = str(raw.get("pack_id", ""))
            digest = str(raw.get("sha256", ""))
            if not pack_id or not _SHA256_RE.fullmatch(digest):
                rejected.append(pack_id or "<missing-pack-id>")
                continue
            current = self._specs.get(pack_id)
            if current is None:
                rejected.append(pack_id)
                continue
            updates: dict[str, Any] = {"sha256": digest}
            if raw.get("version"):
                updates["version"] = str(raw["version"])
            if raw.get("download_url"):
                updates["download_url"] = str(raw["download_url"])
            self._specs[pack_id] = replace(current, **updates)
        return tuple(rejected)

    def load_release_manifest(self, path: str | Path) -> tuple[str, ...]:
        """Load a local release manifest; no network access is performed."""
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, Mapping):
            raise ValueError("release manifest root must be an object")
        rejected = self.apply_release_manifest(data)
        self._save_manifest()
        return rejected
