"""分时复用调度器（24GB 显存友好）。

问题：Qwen3-VL（~6GB）+ Qwen2.5-Omni（~8GB）同时驻留会爆显存。
解决：把打标步骤分阶段，每个阶段结束后 unload 该阶段的大模型，腾出显存。

阶段划分（按显存占用 + 能否共存）：
    Phase 1 "ingest"：轻量（指纹/探测/scene_cut）— 无大模型
    Phase 2 "audio_features"：librosa（CPU）+ face（InsightFace CPU/小）— 无大模型
    Phase 3 "asr"：ASR（主项目 qwen3_asr 或 vault）— 中型模型
    Phase 4 "visual_caption"：Qwen3-VL（画面理解，~6GB）— 大模型，独占
    Phase 5 "audio_understand"：Qwen2.5-Omni（音频理解，~8GB）— 大模型，独占
    （visual_embed/transcript_embed 用小模型 SigLIP2/Qwen3-Embedding，常驻）

用法：
    from apps.topos_vault.pipeline.phased_orchestrator import PhasedOrchestrator
    orch = PhasedOrchestrator(repo)
    report = orch.index_directory("/path", phased=True)

或 CLI：
    topos-vault index /path --phased

设计：
    - 复用 Orchestrator 的 step 列表，但按 phase 重组执行顺序
    - 每个 phase 跑完，调 unload 释放该 phase 的 provider（粗粒度：卸载所有大模型 provider）
    - 进度回调带 phase 名（UI 显示当前阶段）
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable

from apps.topos_vault.db.repository import VaultRepository
from apps.topos_vault.pipeline.orchestrator import (
    BatchReport, classify_media_type,
)
from apps.topos_vault.pipeline.steps import PipelineStep, StepResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------- #
# 阶段定义：阶段名 → 该阶段的 step_name 集合
# ---------------------------------------------------------------------- #

# 步骤 → 阶段映射（决定某 step 在哪个 phase 跑）
_STEP_PHASE: dict[str, str] = {
    "ingest": "ingest",
    "scene_cut": "ingest",
    # 画面/台词向量（小模型，常驻，归到 ingest 阶段一起跑）
    "visual_embed": "ingest",
    "transcript_embed": "ingest",
    "visual_embed_video": "visual_embed",  # SigLIP2 小模型
    # CPU/小模型任务
    "audio_features": "cpu_light",
    "face": "cpu_light",
    "video_caption": "cpu_light",  # 复用关键帧，caption 由 visual_caption phase 负责
    # 大模型阶段（独占显存）
    "asr": "asr",
    "image_caption": "visual_caption",   # Qwen3-VL
    "visual_caption": "visual_caption",  # Qwen3-VL
    "audio_understand": "audio_understand",  # Qwen2.5-Omni
}

# 阶段顺序（按显存策略排：轻 → 中 → 大模型逐个独占）
PHASE_ORDER: tuple[str, ...] = (
    "ingest",          # 指纹/探测/scene_cut/小向量
    "cpu_light",       # librosa/face/video_caption（无大模型）
    "asr",             # ASR（中模型）
    "visual_caption",  # Qwen3-VL（大模型独占）
    "audio_understand",  # Qwen2.5-Omni（大模型独占）
)

# 每个阶段结束后要 unload 的 provider capability（腾显存）
# None = 不卸载（轻量阶段）；具体 capability = 跑完卸载
_PHASE_UNLOAD_CAPS: dict[str, list[str]] = {
    "ingest": [],            # 小模型常驻
    "cpu_light": [],         # 无大模型
    "asr": ["asr"],          # ASR 模型 unload
    "visual_caption": ["visual_caption"],  # Qwen3-VL unload
    "audio_understand": ["audio_understand"],  # Qwen2.5-Omni unload
}


@dataclass
class PhaseInfo:
    """阶段执行信息（给进度回调）。"""
    phase: str
    current: int
    total: int
    current_path: str
    current_step: str

    @property
    def percent(self) -> float:
        return (self.current / self.total * 100) if self.total else 0.0


PhaseProgressCallback = Callable[[PhaseInfo], None]


# ---------------------------------------------------------------------- #
# PhasedOrchestrator
# ---------------------------------------------------------------------- #

class PhasedOrchestrator:
    """分阶段打标调度器。

    与 Orchestrator 区别：
        - Orchestrator：逐文件跑完所有 step（文件粒度，大模型轮流加载）
        - PhasedOrchestrator：逐阶段跑完所有文件（阶段粒度，大模型每阶段加载一次）

    显存优势：处理 N 个文件时，Qwen3-VL 只在 visual_caption 阶段加载 1 次
    （而非 N 次），且跑完立即 unload，不和 Qwen2.5-Omni 抢显存。
    """

    def __init__(self, repo: VaultRepository):
        self.repo = repo
        # 复用 Orchestrator 的 step 构造（保持 step 列表一致）
        from apps.topos_vault.pipeline.orchestrator import Orchestrator
        self._inner = Orchestrator(repo)

    def index_file(
        self, path: str, media_type: str = "auto",
        progress: PhaseProgressCallback | None = None,
    ) -> dict[str, list[StepResult]]:
        """给单个文件分阶段打标。

        Returns:
            {phase: [StepResult, ...]} 每阶段的结果。
        """
        path = os.path.abspath(path)
        if not os.path.exists(path):
            return {"ingest": [StepResult.failed(path, "ingest", "文件不存在",
                                                 recoverable=False)]}

        if media_type == "auto":
            media_type = classify_media_type(path) or ""
        if not media_type:
            return {"ingest": [StepResult.skipped(path, "ingest", "非素材文件")]}

        steps = self._inner._steps.get(media_type, [])
        return self._run_phases(path, media_type, steps, progress)

    def index_directory(
        self, root: str, media_type: str = "auto", recursive: bool = True,
        progress: PhaseProgressCallback | None = None,
    ) -> BatchReport:
        """给目录下所有素材分阶段打标。

        每个阶段遍历所有文件，跑该阶段的 step；阶段间 unload 大模型。
        """
        from apps.topos_vault.pipeline.orchestrator import _EXT_MAP

        root = os.path.abspath(root)
        if not os.path.exists(root):
            raise FileNotFoundError(root)

        # 收集文件
        files: list[str] = []
        if os.path.isfile(root):
            files = [root]
        elif recursive:
            for dirpath, _dirs, fnames in os.walk(root):
                for name in fnames:
                    if name.startswith("."):
                        continue
                    full = os.path.join(dirpath, name)
                    if media_type == "auto":
                        if classify_media_type(full) is not None:
                            files.append(full)
                    else:
                        ext = os.path.splitext(name)[1].lower()
                        if ext in _EXT_MAP.get(media_type, set()):
                            files.append(full)
        else:
            files = [
                os.path.join(root, f) for f in os.listdir(root)
                if not f.startswith(".") and os.path.isfile(os.path.join(root, f))
                and classify_media_type(os.path.join(root, f)) is not None
            ]

        report = BatchReport()
        # 收集每个文件的步骤（按 media_type）
        file_steps: list[tuple[str, str, list[PipelineStep]]] = []
        for f in files:
            mt = classify_media_type(f) or media_type
            steps = self._inner._steps.get(mt, [])
            file_steps.append((f, mt, steps))

        # 按阶段分组步骤，每阶段跑完所有文件
        # 先确定要跑哪些阶段（取所有文件 step 的并集）
        all_phases = set()
        for _f, _mt, steps in file_steps:
            for step in steps:
                phase = _STEP_PHASE.get(step.step_name, "ingest")
                all_phases.add(phase)

        ordered_phases = [p for p in PHASE_ORDER if p in all_phases]

        # 每个文件的全量结果（跨阶段累积）
        file_results: dict[str, list[StepResult]] = {f: [] for f, _, _ in file_steps}

        total_files = len(file_steps)
        for pi, phase in enumerate(ordered_phases):
            logger.info("=== Phase %d/%d: %s ===", pi + 1, len(ordered_phases), phase)
            for fi, (fpath, mt, steps) in enumerate(file_steps):
                phase_steps = [s for s in steps
                               if _STEP_PHASE.get(s.step_name, "ingest") == phase]
                if not phase_steps:
                    continue
                if progress:
                    progress(PhaseInfo(
                        phase=phase, current=fi + 1, total=total_files,
                        current_path=fpath,
                        current_step=phase_steps[0].step_name if phase_steps else "",
                    ))
                results = self._run_file_phase(fpath, mt, phase_steps)
                file_results[fpath].extend(results)

            # 阶段结束：unload 该阶段的大模型 provider
            self._unload_phase_models(phase)

        # 汇总报告
        for fpath, _mt, _steps in file_steps:
            report.add(fpath, file_results[fpath])

        return report

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _run_phases(
        self, path: str, media_type: str, steps: list[PipelineStep],
        progress: PhaseProgressCallback | None,
    ) -> dict[str, list[StepResult]]:
        """单文件分阶段跑（index_file 用）。"""
        from apps.topos_vault.db.repository import MediaSource

        # 按 phase 分组该文件的 step
        phases: dict[str, list[PipelineStep]] = {}
        for step in steps:
            phase = _STEP_PHASE.get(step.step_name, "ingest")
            phases.setdefault(phase, []).append(step)

        ordered = [p for p in PHASE_ORDER if p in phases]
        results_by_phase: dict[str, list[StepResult]] = {}

        # 拿/建 MediaSource（ingest 阶段会填真实 fingerprint）
        existing = self.repo.get_media_source_by_path(path)
        source = MediaSource(
            fingerprint=existing.fingerprint if existing else "",
            media_type=media_type,
            path_original=path, path_current=path,
            title=os.path.basename(path),
        )

        for pi, phase in enumerate(ordered):
            if progress:
                progress(PhaseInfo(
                    phase=phase, current=pi + 1, total=len(ordered),
                    current_path=path,
                    current_step=phases[phase][0].step_name,
                ))
            phase_results: list[StepResult] = []
            for step in phases[phase]:
                result = step.execute(source)
                phase_results.append(result)
                if result.is_failed and not result.recoverable:
                    break
            results_by_phase[phase] = phase_results
            # 从 ingest 阶段拿回真实 fingerprint（ingest step 会写 source.fingerprint）
            if not source.fingerprint and existing:
                source.fingerprint = existing.fingerprint
            self._unload_phase_models(phase)

        return results_by_phase

    def _run_file_phase(
        self, path: str, media_type: str, phase_steps: list[PipelineStep],
    ) -> list[StepResult]:
        """单文件单阶段跑（index_directory 用）。"""
        from apps.topos_vault.db.repository import MediaSource

        existing = self.repo.get_media_source_by_path(path)
        source = MediaSource(
            fingerprint=existing.fingerprint if existing else "",
            media_type=media_type,
            path_original=path, path_current=path,
            title=os.path.basename(path),
        )
        results: list[StepResult] = []
        for step in phase_steps:
            result = step.execute(source)
            results.append(result)
            if result.is_failed and not result.recoverable:
                break
        return results

    def _unload_phase_models(self, phase: str) -> None:
        """unload 某阶段的大模型 provider（腾显存）。

        延迟 import provider 注册表，按 capability 找已加载的 provider unload。
        stub provider 没有 unload 方法（无模型可卸），忽略。
        """
        caps_to_unload = _PHASE_UNLOAD_CAPS.get(phase, [])
        if not caps_to_unload:
            return

        try:
            from apps.topos_vault.providers.base import Capability, get_registry
        except ImportError:
            return

        reg = get_registry()
        for cap_name in caps_to_unload:
            try:
                cap = Capability(cap_name)
            except ValueError:
                continue
            # 找该 capability 下所有 provider，unload 已加载的
            for desc in reg.list_by_capability(cap):
                provider = reg.get(desc.provider_id)
                if provider is None:
                    continue
                unload_fn = getattr(provider, "unload", None)
                if unload_fn is not None:
                    try:
                        unload_fn()
                    except Exception as e:
                        logger.debug("unload %s 失败：%s", desc.provider_id, e)


def unload_all_providers() -> None:
    """unload 所有 provider 的模型（命令结束时清理用）。

    遍历 registry 所有 provider，调 unload（有的话）。
    """
    try:
        from apps.topos_vault.providers import list_providers
    except ImportError:
        return
    from apps.topos_vault.providers.base import get_registry
    reg = get_registry()
    for desc in list_providers():  # 全部 provider 描述
        provider = reg.get(desc.provider_id)
        if provider is None:
            continue
        unload_fn = getattr(provider, "unload", None)
        if unload_fn is not None:
            try:
                unload_fn()
            except Exception:
                pass
