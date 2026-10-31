"""audio_features_step 测试：librosa 数值特征提取。

用合成的音频验证 BPM/能量/人声判断，并验证降级安全。
合成方式：soundfile 已随 librosa 安装，生成已知 BPM 的脉冲正弦波。
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------- #
# pipeline 结构
# ---------------------------------------------------------------------- #

def test_audio_pipeline_has_features_step():
    """audio pipeline 包含 audio_features step。"""
    from apps.topos_vault.db.repository import VaultRepository
    from apps.topos_vault.pipeline.orchestrator import Orchestrator

    orch = Orchestrator(VaultRepository())
    assert "audio_features" in orch.list_pipeline_steps("audio")
    assert "audio_features" in orch.list_pipeline_steps("video")


def test_video_pipeline_has_features_after_transcript():
    """video pipeline：audio_features 在 transcript_embed 之后。"""
    from apps.topos_vault.db.repository import VaultRepository
    from apps.topos_vault.pipeline.orchestrator import Orchestrator

    orch = Orchestrator(VaultRepository())
    steps = orch.list_pipeline_steps("video")
    assert steps.index("transcript_embed") < steps.index("audio_features")


# ---------------------------------------------------------------------- #
# 合成音频辅助
# ---------------------------------------------------------------------- #

@pytest.fixture
def synthesized_beep(tmp_path):
    """合成一段有节奏脉冲的 wav（约 120 BPM 的"嘟嘟"声）。

    用 soundfile 生成，每 0.5 秒一个短脉冲（= 120 BPM），
    便于验证 BPM 检测在合理范围。
    """
    import numpy as np
    try:
        import soundfile as sf
    except ImportError:
        pytest.skip("soundfile 未安装")

    sr = 22050
    duration_sec = 8.0
    t = np.linspace(0, duration_sec, int(sr * duration_sec), endpoint=False)

    # 每 0.5 秒一个 200ms 的 440Hz 脉冲 → 约 120 BPM
    beep_period = 0.5
    beep_len = 0.2
    env = np.zeros_like(t)
    for start in np.arange(0, duration_sec, beep_period):
        mask = (t >= start) & (t < start + beep_len)
        # 用衰减包络让脉冲有起有落（beat 检测更稳）
        local_t = t[mask] - start
        env[mask] = np.exp(-local_t * 8)

    wave = 0.6 * env * np.sin(2 * np.pi * 440 * t)

    path = tmp_path / "beat.wav"
    sf.write(str(path), wave.astype(np.float32), sr)
    return path


# ---------------------------------------------------------------------- #
# BPM 检测（核心）
# ---------------------------------------------------------------------- #

def test_bpm_detected_for_synthesized_beat(repo, synthesized_beep):
    """合成的 120BPM 脉冲 → 检测出的 BPM 在合理范围（60-200）。"""
    from apps.topos_vault.db.faiss_store import clear_store_cache
    from apps.topos_vault.pipeline.orchestrator import Orchestrator
    from apps.topos_vault.pipeline.steps import StepStatus

    clear_store_cache()
    orch = Orchestrator(repo)
    results = orch.index_file(str(synthesized_beep), media_type="audio")

    feat = next(r for r in results if r.step_name == "audio_features")
    assert feat.status == StepStatus.DONE

    # 库里查回
    from apps.topos_vault.db.repository import VaultRepository
    src = VaultRepository().get_media_source_by_path(str(synthesized_beep))
    meta = VaultRepository().get_audio_meta(src.fingerprint)
    assert meta is not None
    assert meta.bpm is not None
    # 120 BPM 的合成节拍，检测值应在 60-200 之间（librosa 偶有倍频/分频误差）
    assert 60 <= meta.bpm <= 200, f"BPM={meta.bpm} 超出合理范围"


def test_energy_and_vocal_written(repo, synthesized_beep):
    """能量和人声判断写入 audio_meta。"""
    from apps.topos_vault.db.faiss_store import clear_store_cache
    from apps.topos_vault.pipeline.orchestrator import Orchestrator

    clear_store_cache()
    orch = Orchestrator(repo)
    orch.index_file(str(synthesized_beep), media_type="audio")

    from apps.topos_vault.db.repository import VaultRepository
    src = VaultRepository().get_media_source_by_path(str(synthesized_beep))
    meta = VaultRepository().get_audio_meta(src.fingerprint)

    assert meta.energy is not None
    assert 0.0 <= meta.energy <= 1.0
    assert meta.vocal_type in ("instrumental", "vocal", "mixed", None)


# ---------------------------------------------------------------------- #
# 幂等
# ---------------------------------------------------------------------- #

def test_audio_features_idempotent(repo, synthesized_beep):
    """重复跑 audio_features，第二次跳过（已有数值特征）。"""
    from apps.topos_vault.db.faiss_store import clear_store_cache
    from apps.topos_vault.pipeline.orchestrator import Orchestrator
    from apps.topos_vault.pipeline.steps import StepStatus

    clear_store_cache()
    orch = Orchestrator(repo)
    orch.index_file(str(synthesized_beep), media_type="audio")

    # 第二次
    results2 = orch.index_file(str(synthesized_beep), media_type="audio")
    feat2 = next(r for r in results2 if r.step_name == "audio_features")
    assert feat2.status == StepStatus.SKIPPED  # 已完成


# ---------------------------------------------------------------------- #
# 降级安全
# ---------------------------------------------------------------------- #

def test_audio_features_skipped_when_file_missing(repo, tmp_path):
    """文件不存在 → ingest 失败，pipeline 停止（audio_features 不会执行）。"""
    from apps.topos_vault.db.faiss_store import clear_store_cache
    from apps.topos_vault.pipeline.orchestrator import Orchestrator
    from apps.topos_vault.pipeline.steps import StepStatus

    clear_store_cache()
    orch = Orchestrator(repo)
    fake = tmp_path / "nope.wav"
    results = orch.index_file(str(fake), media_type="audio")

    # ingest 失败（不可恢复）→ pipeline 中断，audio_features 不出现
    assert results[0].step_name == "ingest"
    assert results[0].status == StepStatus.FAILED
    # 只有 ingest 的结果（后续步骤因不可恢复失败而中断）
    assert all(r.step_name == "ingest" for r in results)


def test_audio_features_step_directly_handles_missing_file(repo, tmp_path):
    """直接调 audio_features step，文件不存在 → StepSkipped（不抛未捕获异常）。"""
    from apps.topos_vault.db.repository import MediaSource
    from apps.topos_vault.pipeline.audio_features_step import AudioFeaturesStep

    step = AudioFeaturesStep(repo)
    src = MediaSource(fingerprint="fp_test_missing", media_type="audio",
                      path_original=str(tmp_path / "nope.wav"))
    result = step.execute(src)
    from apps.topos_vault.pipeline.steps import StepStatus
    assert result.status == StepStatus.SKIPPED


def test_audio_features_skipped_when_not_audio(repo, sample_files):
    """图片文件 → audio_features 跳过（applies_to 不含 image）。"""
    from apps.topos_vault.db.faiss_store import clear_store_cache
    from apps.topos_vault.pipeline.orchestrator import Orchestrator
    from apps.topos_vault.pipeline.steps import StepStatus

    clear_store_cache()
    orch = Orchestrator(repo)
    results = orch.index_file(str(sample_files["image"]))

    # 图片 pipeline 没有 audio_features step
    assert all(r.step_name != "audio_features" for r in results)


# ---------------------------------------------------------------------- #
# 调性估计（合成单音 → 应能算出某调性，不强断言具体值）
# ---------------------------------------------------------------------- #

def test_key_estimate_runs_without_crash(repo, synthesized_beep):
    """调性估计能跑（合成的单频未必准，但不该崩；结果要么是 None 要么合法格式）。"""
    from apps.topos_vault.db.faiss_store import clear_store_cache
    from apps.topos_vault.pipeline.orchestrator import Orchestrator

    clear_store_cache()
    orch = Orchestrator(repo)
    orch.index_file(str(synthesized_beep), media_type="audio")

    from apps.topos_vault.db.repository import VaultRepository
    src = VaultRepository().get_media_source_by_path(str(synthesized_beep))
    meta = VaultRepository().get_audio_meta(src.fingerprint)

    # key 要么 None（合成音频太特殊测不出），要么 "X major"/"X minor" 格式
    if meta.key is not None:
        assert " " in meta.key
        tonic, mode = meta.key.rsplit(" ", 1)
        assert mode in ("major", "minor")
        assert len(tonic) <= 2
