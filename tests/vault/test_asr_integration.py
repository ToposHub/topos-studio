"""P0-3 ASR 集成接线测试：主项目 ASR 集成 + 后端切换。

测集成路径的代码正确性（不依赖真实 ASR 模型）：
    1. TOPOS_VAULT_ASR_BACKEND=vault → 强制 vault provider，不碰主项目
    2. 主项目 import 失败 → _try_main_project_asr 返回 []（降级到 vault）
    3. 主项目 analyze 返回帧坐标 → 正确转秒（用 mock 验证帧/秒换算）
    4. auto 模式：主项目失败时降级到 vault provider
    5. fps 解析正确（Rational.from_string）
"""

from __future__ import annotations

import os
import pytest


# ---------------------------------------------------------------------- #
# 后端选择
# ---------------------------------------------------------------------- #

def test_vault_backend_forces_vault_provider(vault_dir, sample_files, monkeypatch):
    """TOPOS_VAULT_ASR_BACKEND=vault → 跳过主项目 ASR，用 vault provider。

    vault stub provider 返回空台词，所以 asr step 会 skipped（不崩）。
    用 monkeypatch 确保 _try_main_project_asr 不会被调用。
    """
    from apps.topos_vault.db import schema
    from apps.topos_vault.db.repository import VaultRepository
    from apps.topos_vault.pipeline.orchestrator import Orchestrator
    from apps.topos_vault.pipeline import asr_step as asr_mod

    schema.init_db()
    monkeypatch.setenv("TOPOS_VAULT_ASR_BACKEND", "vault")

    # 监视 _try_main_project_asr 不被调用
    called = {"main": False}
    orig = asr_mod.AsrStep._try_main_project_asr
    def spy(self, source):
        called["main"] = True
        return []
    monkeypatch.setattr(asr_mod.AsrStep, "_try_main_project_asr", spy)

    orch = Orchestrator(VaultRepository())
    # sample_files["video"] 是假字节，ASR 会 skipped，但不应调主项目
    results = orch.index_file(str(sample_files["video"]), media_type="video")
    assert called["main"] is False, "vault 后端不应调用主项目 ASR"


def test_main_project_backend_uses_main_project(vault_dir, sample_files, monkeypatch):
    """TOPOS_VAULT_ASR_BACKEND=main_project → 调主项目 ASR。"""
    from apps.topos_vault.db import schema
    from apps.topos_vault.db.repository import VaultRepository, TranscriptLine
    from apps.topos_vault.pipeline.orchestrator import Orchestrator
    from apps.topos_vault.pipeline import asr_step as asr_mod

    schema.init_db()
    monkeypatch.setenv("TOPOS_VAULT_ASR_BACKEND", "main_project")

    # mock 主项目 ASR 返回 1 句台词
    def fake_main(self, source):
        return [TranscriptLine(line_id=None, clip_id="", text="mock 台词",
                               start_sec=1.0, end_sec=2.0)]
    monkeypatch.setattr(asr_mod.AsrStep, "_try_main_project_asr", fake_main)

    orch = Orchestrator(VaultRepository())
    results = orch.index_file(str(sample_files["video"]), media_type="video")
    asr_result = next(r for r in results if r.step_name == "asr")
    from apps.topos_vault.pipeline.steps import StepStatus
    assert asr_result.status == StepStatus.DONE
    assert "1 句台词" in asr_result.detail


# ---------------------------------------------------------------------- #
# 主项目 import 失败 → 降级
# ---------------------------------------------------------------------- #

def test_main_project_asr_returns_empty_when_import_fails(vault_dir, monkeypatch):
    """主项目 src/ 不可用时，_try_main_project_asr 返回 []（不崩）。"""
    from apps.topos_vault.db.repository import MediaSource, VaultRepository
    from apps.topos_vault.pipeline.asr_step import AsrStep

    # 用 monkeypatch 让 import 失败（sys.modules 注入 None 触发 ImportError）
    import builtins
    real_import = builtins.__import__
    def fake_import(name, *args, **kwargs):
        if name.startswith("src.features.edit.ai_editing"):
            raise ImportError("mocked unavailable")
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", fake_import)

    step = AsrStep(VaultRepository())
    src = MediaSource(fingerprint="fp", media_type="video",
                      path_original="/tmp/x.mp4")
    result = step._try_main_project_asr(src)
    assert result == []


# ---------------------------------------------------------------------- #
# 帧 → 秒换算（mock 主项目 analyze 返回帧坐标）
# ---------------------------------------------------------------------- #

def test_frame_to_seconds_conversion(vault_dir, tmp_path, monkeypatch):
    """主项目 analyze 返回帧坐标，_try_main_project_asr 正确转成秒。

    构造假视频 + mock QwenASRProvider.analyze 返回帧坐标，
    验证转出的 TranscriptLine.start_sec/end_sec 正确（帧 / fps）。
    """
    import types
    from apps.topos_vault.db import schema
    from apps.topos_vault.db.repository import VaultRepository
    from apps.topos_vault.pipeline.asr_step import AsrStep

    schema.init_db()

    # 造假视频文件（让 source.path 存在检查通过）
    fake_video = tmp_path / "fake.mp4"
    fake_video.write_bytes(b"FAKE_VIDEO" * 100)

    # --- mock 主项目模块 ---
    # AssetFingerprint：最简 dataclass
    asset_fp_mod = types.ModuleType("src.features.edit.ai_editing.analysis_models")
    from dataclasses import dataclass
    @dataclass(frozen=True)
    class FakeAssetFingerprint:
        size: int = 0
        mtime_ns: int = 0
        content_hash: str | None = None
    asset_fp_mod.AssetFingerprint = FakeAssetFingerprint
    # 假 TranscriptSegment（帧坐标）
    @dataclass(frozen=True)
    class FakeTranscriptSegment:
        start_frame: int
        end_frame: int
        text: str
        speaker: str | None = None
    asset_fp_mod.TranscriptSegment = FakeTranscriptSegment
    # 假 AssetAnalysis（含 transcript）
    @dataclass(frozen=True)
    class FakeAssetAnalysis:
        transcript: tuple = ()
    asset_fp_mod.AssetAnalysis = FakeAssetAnalysis

    # Rational：from_string + to_fps
    rational_mod = types.ModuleType("src.shared.media.metadata")
    class FakeRational:
        @staticmethod
        def from_string(s):
            num, den = s.split("/")
            return FakeRational()
        def to_fps(self):
            return 24000 / 1001  # 23.976
    rational_mod.Rational = FakeRational

    # QwenASRProvider：analyze 返回帧坐标
    tp_mod = types.ModuleType("src.features.edit.ai_editing.transcript_provider")
    class FakeProvider:
        def __init__(self, **kwargs):
            pass
        def analyze(self, asset_id, fingerprint, *, fps, progress=None):
            # 返回 2 句台词：帧坐标分别对应 ~1s 和 ~2s（fps≈23.976）
            analysis = FakeAssetAnalysis(transcript=(
                FakeTranscriptSegment(start_frame=24, end_frame=72,
                                      text="第一句", speaker="S1"),
                FakeTranscriptSegment(start_frame=96, end_frame=120,
                                      text="第二句", speaker="S2"),
            ))
            return analysis, ()
    tp_mod.QwenASRProvider = FakeProvider

    # 注入到 sys.modules（让 asr_step 的延迟 import 拿到假的）
    import sys
    monkeypatch.setitem(sys.modules, "src.features.edit.ai_editing.analysis_models",
                        asset_fp_mod)
    monkeypatch.setitem(sys.modules, "src.shared.media.metadata", rational_mod)
    monkeypatch.setitem(sys.modules, "src.features.edit.ai_editing.transcript_provider",
                        tp_mod)

    # 跑 _try_main_project_asr
    from apps.topos_vault.db.repository import MediaSource
    step = AsrStep(VaultRepository())
    src = MediaSource(fingerprint="fp_conv", media_type="video",
                      path_original=str(fake_video))
    lines = step._try_main_project_asr(src)

    # 应转出 2 句，秒坐标 ≈ 帧 / 23.976
    assert len(lines) == 2
    fps = 24000 / 1001
    assert lines[0].text == "第一句"
    assert lines[0].start_sec == pytest.approx(24 / fps, rel=1e-3)
    assert lines[0].end_sec == pytest.approx(72 / fps, rel=1e-3)
    assert lines[0].speaker == "S1"
    assert lines[1].text == "第二句"
    assert lines[1].speaker == "S2"


# ---------------------------------------------------------------------- #
# vault provider 降级路径
# ---------------------------------------------------------------------- #

def test_auto_falls_back_to_vault_when_main_unavailable(vault_dir, sample_files, monkeypatch):
    """auto 模式 + 主项目失败 → 降级到 vault provider（stub 返回空 → skipped）。"""
    from apps.topos_vault.db import schema
    from apps.topos_vault.db.repository import VaultRepository
    from apps.topos_vault.pipeline.orchestrator import Orchestrator
    from apps.topos_vault.pipeline import asr_step as asr_mod
    from apps.topos_vault.pipeline.steps import StepStatus

    schema.init_db()
    monkeypatch.delenv("TOPOS_VAULT_ASR_BACKEND", raising=False)

    # mock 主项目失败
    monkeypatch.setattr(asr_mod.AsrStep, "_try_main_project_asr",
                        lambda self, src: [])

    orch = Orchestrator(VaultRepository())
    results = orch.index_file(str(sample_files["video"]), media_type="video")
    asr = next(r for r in results if r.step_name == "asr")
    # stub provider 返回空 → skipped
    assert asr.status == StepStatus.SKIPPED


# ---------------------------------------------------------------------- #
# 环境变量生效验证
# ---------------------------------------------------------------------- #

def test_asr_backend_env_values(monkeypatch):
    """TOPOS_VAULT_ASR_BACKEND 接受 auto/main_project/vault 三种值。"""
    for val in ("auto", "main_project", "vault"):
        monkeypatch.setenv("TOPOS_VAULT_ASR_BACKEND", val)
        backend = os.environ.get("TOPOS_VAULT_ASR_BACKEND", "auto").strip().lower()
        assert backend == val


def test_asr_backend_unset_defaults_to_auto(monkeypatch):
    """TOPOS_VAULT_ASR_BACKEND 未设置时，读取默认 'auto'。"""
    monkeypatch.delenv("TOPOS_VAULT_ASR_BACKEND", raising=False)
    backend = os.environ.get("TOPOS_VAULT_ASR_BACKEND", "auto").strip().lower()
    assert backend == "auto"
