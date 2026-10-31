"""Optional speech pre-processing backends for transcript providers.

The preprocessor is deliberately separate from ASR.  A project may run Qwen3-
ASR directly, use Demucs v4 for music-heavy clips, or add another enhancer
without changing subtitle placement or the AnalysisService contract.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreprocessedAudio:
    """Audio path plus provenance and cleanup information."""

    path: str
    backend: str
    metadata: dict[str, Any]
    cleanup_dir: str | None = None

    def cleanup(self) -> None:
        if self.cleanup_dir:
            shutil.rmtree(self.cleanup_dir, ignore_errors=True)


class SpeechPreprocessor(Protocol):
    backend_id: str

    def process(self, audio_path: str) -> PreprocessedAudio:
        ...


class DemucsV4Preprocessor:
    """Separate a vocals stem with Demucs v4 for music-heavy speech.

    Demucs is optional and never imported during normal Qwen startup.  The
    subprocess boundary keeps PyTorch/Demucs memory isolated from the Qt UI
    process and makes cancellation/error reporting explicit.
    """

    backend_id = "demucs_v4"

    def __init__(
        self,
        *,
        model: str = "htdemucs",
        python_executable: str | None = None,
        timeout_seconds: float = 900.0,
        model_pack_manager: Any | None = None,
    ) -> None:
        self.model = model
        self.python_executable = python_executable or sys.executable
        self.timeout_seconds = timeout_seconds
        self._model_pack_manager = model_pack_manager

    def is_available(self) -> bool:
        return not self.unavailable_reason()

    def unavailable_reason(self) -> str:
        """Explain why the optional separator cannot run."""
        if importlib.util.find_spec("demucs") is None:
            return "Python 环境未安装 demucs"
        manager = self._model_pack_manager
        if manager is not None and getattr(manager, "get_spec", None):
            spec = manager.get_spec("demucs_v4")
            if spec is not None:
                status = manager.get_status("demucs_v4")
                if not bool(getattr(status, "usable", False)):
                    return "Demucs v4 模型包未安装或未通过校验"
        return ""

    def process(self, audio_path: str) -> PreprocessedAudio:
        if not audio_path or not os.path.isfile(audio_path):
            raise FileNotFoundError(audio_path)
        if not self.is_available():
            raise RuntimeError(
                f"Demucs v4 不可用：{self.unavailable_reason()}"
            )

        output_dir = tempfile.mkdtemp(prefix="topos-demucs-")
        cmd = [
            self.python_executable,
            "-m",
            "demucs",
            "--two-stems=vocals",
            "-n",
            self.model,
            "-o",
            output_dir,
            audio_path,
        ]
        try:
            completed = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            shutil.rmtree(output_dir, ignore_errors=True)
            raise RuntimeError(f"Demucs 执行失败：{exc}") from exc

        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            shutil.rmtree(output_dir, ignore_errors=True)
            raise RuntimeError(f"Demucs 执行失败：{stderr[-800:]}")

        stem_candidates = list(Path(output_dir).glob(f"{self.model}/**/vocals.wav"))
        if not stem_candidates:
            shutil.rmtree(output_dir, ignore_errors=True)
            raise RuntimeError("Demucs 未生成 vocals.wav")
        stem_path = str(stem_candidates[0])
        logger.info("Demucs v4 produced speech stem: %s", stem_path)
        return PreprocessedAudio(
            path=stem_path,
            backend=self.backend_id,
            metadata={"model": self.model, "stem": "vocals"},
            cleanup_dir=output_dir,
        )


__all__ = ["DemucsV4Preprocessor", "PreprocessedAudio", "SpeechPreprocessor"]
