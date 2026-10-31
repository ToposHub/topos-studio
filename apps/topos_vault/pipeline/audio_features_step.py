"""音频数值特征步骤（音频/视频共用）。

用 librosa 算 BPM/调性/能量/人声判断等**数值**特征，写入 audio_meta。

为什么单独成 step、不走 provider：
    BPM/调性这类是**确定性数值**特征，大模型（Qwen2.5-Omni）做不了精确值
    （contracts.py 的注释已声明），而 librosa 的 beat/tempo/key 检测是成熟数值算法。
    这类特征不涉及"可切换模型"，放 provider 层（Protocol + 注册 + 配置切换）反而
    过度设计。直接作为一个 pipeline step 实现，依赖缺失时优雅降级。

    语义理解（情绪/乐器/描述）仍由 AUDIO_UNDERSTAND provider 负责（audio_understand_step），
    与本 step 各写各的字段，互不干扰。

产出（写入 audio_meta）：
    bpm            - tempo（拍/分钟）
    key            - 调性，如 "C major"
    energy         - RMS 能量归一化 0-1
    vocal_type     - instrumental / vocal / mixed（基于高频谐波/噪声估计）

降级安全：librosa 未装 → 跳过（audio_meta 其他字段由后续 audio_understand step 填）。
"""

from __future__ import annotations

from typing import Any

import logging
import os

from apps.topos_vault.db.repository import AudioMeta, MediaSource, VaultRepository
from apps.topos_vault.pipeline.steps import PipelineStep, StepSkipped

logger = logging.getLogger(__name__)

try:
    import librosa  # type: ignore
    import numpy as np
    _HAS_LIBROSA = True
except ImportError:
    _HAS_LIBROSA = False

# 音频 I/O：优先 soundfile（无 GStreamer 依赖，最稳），
# 回退 librosa.load（走 audioread，部分环境 GStreamer 坏了会崩）
try:
    import soundfile as sf  # type: ignore
    _HAS_SOUNDFILE = True
except ImportError:
    _HAS_SOUNDFILE = False


# ---------------------------------------------------------------------- #
# 调性推断用的 24 个 key（大调 + 小调），对齐 Krumhansl-Schmuckler 算法
# ---------------------------------------------------------------------- #

_KEY_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


class AudioFeaturesStep(PipelineStep):
    """音频数值特征步骤。

    对音频/视频文件提取 BPM/调性/能量/人声判断，写入 audio_meta。
    视频走 ASR 步骤抽出的音轨不在此处理——本 step 直接读原文件（librosa 自己解码）。

    幂等：已有 BPM 等数值时跳过（除非 force）。
    """

    step_name = "audio_features"
    # 音频整曲 + 视频也跑（视频本身也有音频轨，BPM/能量是剪辑选曲的常用特征）
    applies_to = ("audio", "video")

    # 采样时长上限（秒）：长音频只取前 N 秒算特征，加速 + 避免内存爆
    # （BPM/调性在前几分钟就稳定，整曲没必要）
    MAX_ANALYSIS_SECONDS = 120.0

    def __init__(self, repo: VaultRepository):
        super().__init__(repo)

    def _run(self, source: MediaSource) -> str:
        if not _HAS_LIBROSA:
            raise StepSkipped(self.step_name, source.fingerprint,
                              "librosa 未安装，无法计算数值特征")

        path = source.path_current or source.path_original
        if not os.path.exists(path):
            raise StepSkipped(self.step_name, source.fingerprint, "文件不存在")

        # 音频解码可能失败（损坏/格式不支持/编码不可读）
        try:
            y, sr = self._load_audio(path)
        except Exception as e:
            logger.debug("音频加载失败（%s），跳过数值特征", e)
            raise StepSkipped(self.step_name, source.fingerprint,
                              f"音频解码失败：{type(e).__name__}")

        if y is None or len(y) == 0:
            raise StepSkipped(self.step_name, source.fingerprint, "空音频")

        # 算各特征（每步独立 try，单个失败不影响其他）
        bpm = self._safe_compute(lambda: self._estimate_bpm(y, sr))
        key = self._safe_compute(lambda: self._estimate_key(y, sr))
        energy = self._safe_compute(lambda: self._estimate_energy(y))
        vocal_type = self._safe_compute(lambda: self._estimate_vocal_type(y, sr))

        # 写入 audio_meta（COALESCE 保留已有字段）
        self.repo.upsert_audio_meta(AudioMeta(
            fingerprint=source.fingerprint,
            bpm=bpm,
            key=key,
            energy=energy,
            vocal_type=vocal_type,
        ))

        parts = []
        if bpm is not None:
            parts.append(f"BPM={bpm:.0f}")
        if key:
            parts.append(f"key={key}")
        if energy is not None:
            parts.append(f"energy={energy:.2f}")
        if vocal_type:
            parts.append(f"vocal={vocal_type}")
        return " ".join(parts) if parts else "（未提取到数值特征）"

    def is_done(self, source: MediaSource) -> bool:
        """已有任一数值特征 → 视为完成（避免重复算，librosa 开销大）。"""
        meta = self.repo.get_audio_meta(source.fingerprint)
        if meta is None:
            return False
        return any(v is not None for v in (meta.bpm, meta.key, meta.energy))

    # ------------------------------------------------------------------ #
    # 音频加载（限制时长 + 转单声道）
    # ------------------------------------------------------------------ #

    def _load_audio(self, path: str) -> tuple["np.ndarray", int]:
        """加载音频，转单声道，限制到前 MAX_ANALYSIS_SECONDS 秒。

        I/O 优先级：
            1. soundfile（纯 libsndfile，无 GStreamer 依赖，最稳；支持 wav/flac/ogg/m4a）
            2. librosa.load（回退，走 audioread——部分 Linux 环境 GStreamer 坏了会崩）

        重采样到 22050Hz（librosa 默认，数值特征够用）。
        """
        target_sr = 22050
        max_samples = int(self.MAX_ANALYSIS_SECONDS * target_sr)

        if _HAS_SOUNDFILE:
            try:
                data, orig_sr = sf.read(path, dtype="float32",
                                        frames=max_samples if max_samples else None,
                                        always_2d=False)
                # 转单声道
                if data.ndim > 1:
                    data = data.mean(axis=1)
                # 重采样（orig_sr != target_sr 时）
                if orig_sr != target_sr:
                    data = librosa.resample(data, orig_sr=orig_sr,
                                            target_sr=target_sr)
                # 截断到上限
                if len(data) > max_samples:
                    data = data[:max_samples]
                return data, target_sr
            except Exception as e:
                logger.debug("soundfile 读取失败，回退 librosa.load：%s", e)

        # 回退：librosa.load（可能触发 audioread/GStreamer）
        y, sr = librosa.load(path, sr=target_sr, mono=True,
                             duration=self.MAX_ANALYSIS_SECONDS)
        return y, sr

    # ------------------------------------------------------------------ #
    # 各特征提取
    # ------------------------------------------------------------------ #

    def _estimate_bpm(self, y: "np.ndarray", sr: int) -> float | None:
        """BPM 估计（librosa 节拍检测）。

        librosa 0.11+：librosa.feature.rhythm.tempo（librosa.beat.tempo 已废弃 alias）；
        兼容老版本：librosa.beat.tempo。
        """
        try:
            # onset 包络是 beat 检测的基础
            onset_env = librosa.onset.onset_strength(y=y, sr=sr)
            # 新版 API 在 feature.rhythm 下；优先用，回退旧 beat.tempo
            tempo_fn = getattr(getattr(librosa.feature, "rhythm", None), "tempo", None)
            if tempo_fn is None:
                tempo_fn = librosa.beat.tempo
            tempo = tempo_fn(onset_envelope=onset_env, sr=sr)
            # tempo 可能是数组（多估计）或标量，取第一个
            if hasattr(tempo, "__len__"):
                tempo = float(np.asarray(tempo).flat[0])
            else:
                tempo = float(tempo)
        except Exception as e:
            logger.debug("BPM 估计失败：%s", e)
            return None

        # 合理性校验：音乐 BPM 通常 30-300，超出范围可能是误估
        if not (30.0 <= tempo <= 300.0):
            logger.debug("BPM=%.1f 超出合理范围，丢弃", tempo)
            return None
        return tempo

    def _estimate_key(self, y: "np.ndarray", sr: int) -> str | None:
        """调性估计（Krumhansl-Schmuckler key profile 匹配）。

        librosa 0.10+ 内置 librosa.feature.chroma + 自实现 K-S 匹配。
        返回 "C major" / "A minor" 形式。
        """
        try:
            chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
            # 12 维 chroma 均值（音高类别能量分布）
            chroma_mean = chroma.mean(axis=1)

            major_profile = np.array(
                [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
            )
            minor_profile = np.array(
                [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
            )

            # 对每个调（12 个起始音 × 大/小调）算相关度，取最大
            best_score = -np.inf
            best_key: tuple[int, str] | None = None
            for i in range(12):
                rolled = np.roll(chroma_mean, -i)
                # 归一化后算相关（K-S 用 Pearson 相关）
                maj = _pearson(rolled, major_profile)
                if maj > best_score:
                    best_score = maj
                    best_key = (i, "major")
                m = _pearson(rolled, minor_profile)
                if m > best_score:
                    best_score = m
                    best_key = (i, "minor")

            if best_key is None:
                return None
            tonic, mode = best_key
            return f"{_KEY_NAMES[tonic]} {mode}"
        except Exception as e:
            logger.debug("调性估计失败：%s", e)
            return None

    def _estimate_energy(self, y: "np.ndarray") -> float | None:
        """RMS 能量，归一化到 0-1（经验值上界 0.2 → 映射到 1.0）。"""
        try:
            rms = float(np.sqrt(np.mean(y ** 2)))
            # 短音频 RMS 一般 0.001-0.2，用经验上界归一化
            normalized = min(1.0, rms / 0.2)
            return round(normalized, 3)
        except Exception as e:
            logger.debug("能量估计失败：%s", e)
            return None

    def _estimate_vocal_type(self, y: "np.ndarray", sr: int) -> str | None:
        """人声/纯音乐判断（基于频谱平坦度 spectral_flatness）。

        纯音乐频谱较"满"（flatness 低），人声/语音频谱起伏大（flatness 高）。
        粗判，不替代 dedicated vocal separation，但够用于检索过滤。
        """
        try:
            flatness = float(np.mean(librosa.feature.spectral_flatness(y=y)))
            # 经验阈值：< 0.1 偏音乐，> 0.3 偏语音，之间 mixed
            if flatness < 0.1:
                return "instrumental"
            if flatness > 0.3:
                return "vocal"
            return "mixed"
        except Exception as e:
            logger.debug("人声判断失败：%s", e)
            return None

    def _safe_compute(self, fn) -> Any:
        """跑一个特征计算，失败返回 None（单个失败不影响其他特征写入）。"""
        try:
            return fn()
        except Exception as e:
            logger.debug("特征计算失败：%s", e)
            return None


# ---------------------------------------------------------------------- #
# 辅助
# ---------------------------------------------------------------------- #

def _pearson(a: "np.ndarray", b: "np.ndarray") -> float:
    """Pearson 相关系数（用于 K-S 调性匹配）。"""
    a_mean = a - a.mean()
    b_mean = b - b.mean()
    denom = np.sqrt(np.sum(a_mean ** 2) * np.sum(b_mean ** 2))
    if denom < 1e-12:
        return 0.0
    return float(np.sum(a_mean * b_mean) / denom)
