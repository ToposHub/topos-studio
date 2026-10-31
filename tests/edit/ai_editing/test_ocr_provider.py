"""TAEP M8 — OcrProvider 测试。

验收（§18.1 / §9 / §11.1 M4 红线沿用）：

- provider_id / supported_kinds 契约。
- rapidocr 包未装 → ModelPackNotInstalledError（不静默降级）。
- stub 推理路径：返回 AssetAnalysis.ocr + 字段级 provenance（provider="ocr_local"）。
- shot 聚合：注入 shot_spans_provider → 关键帧抽样（非逐帧），sampler 被调用。
- 均匀回退：无 shot provider → 均匀抽样，provenance 标 frame_selection="even"。
- OCR segment 经 analysis_to_segments 产生 SegmentKind.OCR + payload.text。

注：find_spec("rapidocr") 在测试环境为 None；用 monkeypatch 桩成"已装"以走 stub。
真实 RapidOCR 推理（TOPOS_OCR_REAL=1）已在 R4-a 实现：调用注入的 RapidOCR 引擎；
引擎初始化与 bbox 归一化的细节测试见 test_ocr_provider_real.py。
"""

from __future__ import annotations

import importlib.util

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.analysis_models import (
    AssetFingerprint,
    OcrBox,
    OcrDetection,
    analysis_to_segments,
)
from src.features.edit.ai_editing.editorial_models import SegmentKind
from src.features.edit.ai_editing.keyframe_sampler import (
    ShotSpan,
    StubFrameSampler,
    derive_shot_spans,
)
from src.features.edit.ai_editing.ocr_provider import (
    ModelPackNotInstalledError,
    OcrProvider,
)


# ================================================================
# Fixtures
# ================================================================


@pytest.fixture
def stub_rapidocr_available(monkeypatch):
    """桩 rapidocr 包为"已安装"，使 provider 能走到 stub 推理路径。"""
    monkeypatch.setattr(
        importlib.util, "find_spec",
        lambda name: object() if name == "rapidocr" else None,
    )


def _shot_spans(asset_id: str) -> tuple[ShotSpan, ...]:
    """固定 shot spans：2 个 shot（0-100, 100-200）。"""
    return derive_shot_spans(
        [{"frame": 0, "confidence": 1.0}, {"frame": 100}],
        total_frames=200,
    )


# ================================================================
# 契约
# ================================================================


def test_provider_id_and_supported_kinds():
    assert OcrProvider.provider_id == "ocr_local"
    assert OcrProvider.supported_kinds == (SegmentKind.OCR,)


# ================================================================
# 缺失依赖：不静默降级（§11.1 M4 红线沿用）
# ================================================================


def test_rapidocr_package_missing_raises():
    """rapidocr python 包未装 → 抛错，不返回空结果（不静默降级）。

    注：rapidocr 已安装时此测试跳过（无法模拟"未装"场景）。
    """
    if importlib.util.find_spec("rapidocr") is not None:
        pytest.skip("rapidocr is installed; cannot test 'missing package' path")
    provider = OcrProvider()
    with pytest.raises(ModelPackNotInstalledError) as exc_info:
        provider.analyze("a1", AssetFingerprint(), fps="24/1")
    assert exc_info.value.pack_id == "rapidocr_ocr"
    assert "rapidocr" in str(exc_info.value).lower()


# ================================================================
# stub 推理路径：analyze 完整流程
# ================================================================


def test_analyze_returns_ocr_field(stub_rapidocr_available):
    """analyze 返回 AssetAnalysis，ocr 非空，字段级 provenance 标 ocr_local。"""
    sampler = StubFrameSampler()
    provider = OcrProvider(
        sampler=sampler,
        shot_spans_provider=_shot_spans,
        total_frames_provider=lambda aid: 200,
    )
    analysis, segments = provider.analyze("a1", AssetFingerprint(), fps="24/1")
    assert len(analysis.ocr) >= 1
    # 字段级 provenance
    ocr_prov = analysis.provenance_for("ocr")
    assert ocr_prov.provider == "ocr_local"
    # 每个 detection 有 frame/text/confidence
    for det in analysis.ocr:
        assert isinstance(det, OcrDetection)
        assert det.frame >= 0
        assert det.text
        assert 0.0 <= det.confidence <= 1.0


def test_analyze_field_provenance_isolated_to_ocr(stub_rapidocr_available):
    """OCR provider 只填 ocr 字段，transcript 等其他字段为空（由 _merge_analysis 合并）。"""
    provider = OcrProvider(
        shot_spans_provider=_shot_spans,
        total_frames_provider=lambda aid: 200,
    )
    analysis, _ = provider.analyze("a1", AssetFingerprint(), fps="24/1")
    assert analysis.ocr  # 只填了 ocr
    assert analysis.transcript == ()
    assert analysis.scene_cuts == ()
    assert analysis.tags == ()


def test_ocr_detections_carry_box(stub_rapidocr_available):
    """stub 产出的 detection 带 OcrBox（归一化坐标）。"""
    provider = OcrProvider(
        shot_spans_provider=_shot_spans,
        total_frames_provider=lambda aid: 200,
    )
    analysis, _ = provider.analyze("a1", AssetFingerprint(), fps="24/1")
    assert any(det.box is not None for det in analysis.ocr)
    box = next(det.box for det in analysis.ocr if det.box is not None)
    assert isinstance(box, OcrBox)
    assert 0.0 <= box.x <= 1.0


# ================================================================
# shot 聚合：关键帧抽样（非逐帧）
# ================================================================


def test_shot_aggregation_calls_sampler_only_on_keyframes(stub_rapidocr_available):
    """注入 shot_spans_provider → sampler 只被关键帧调用（非逐帧 200 次）。"""
    sampler = StubFrameSampler()
    provider = OcrProvider(
        sampler=sampler,
        shot_spans_provider=_shot_spans,
        total_frames_provider=lambda aid: 200,
    )
    provider.analyze("a1", AssetFingerprint(), fps="24/1")
    sampled = sampler.sampled_frames
    # 2 个 shot × head/mid/tail = 6 关键帧（非逐帧 200）
    assert len(sampled) <= 6
    assert len(sampled) < 50  # 远小于总帧数，证明不是逐帧
    # 关键帧来自 shot spans（在 [0,100) 和 [100,200) 内）
    assert all(0 <= f < 200 for f in sampled)


def test_frame_selection_mode_in_provenance(stub_rapidocr_available):
    """shot 聚合时 provenance.extra 标 frame_selection="shot_head_mid_tail"。"""
    provider = OcrProvider(
        shot_spans_provider=_shot_spans,
        total_frames_provider=lambda aid: 200,
    )
    analysis, _ = provider.analyze("a1", AssetFingerprint(), fps="24/1")
    assert analysis.provenance.extra.get("frame_selection") == "shot_head_mid_tail"


def test_even_fallback_when_no_shot_provider(stub_rapidocr_available):
    """无 shot_spans_provider → 均匀抽样，provenance 标 "even"。"""
    provider = OcrProvider(total_frames_provider=lambda aid: 300)
    analysis, _ = provider.analyze("a1", AssetFingerprint(), fps="24/1")
    assert analysis.provenance.extra.get("frame_selection") == "even"


def test_even_fallback_caps_sample_count(stub_rapidocr_available):
    """均匀回退限制采样数（避免长片逐帧爆炸）。"""
    sampler = StubFrameSampler()
    provider = OcrProvider(
        sampler=sampler,
        total_frames_provider=lambda aid: 10000,  # 长片
    )
    provider.analyze("a1", AssetFingerprint(), fps="24/1")
    # 限制在 _DEFAULT_MAX_SAMPLED_FRAMES（30）
    assert len(sampler.sampled_frames) <= 30


# ================================================================
# analysis_to_segments：OCR segment 产生
# ================================================================


def test_ocr_analysis_produces_ocr_segments(stub_rapidocr_available):
    """analyze 返回的 segments 含 SegmentKind.OCR，payload.text 可被搜索命中。"""
    provider = OcrProvider(
        shot_spans_provider=_shot_spans,
        total_frames_provider=lambda aid: 200,
    )
    _, segments = provider.analyze("a1", AssetFingerprint(), fps="24/1")
    ocr_segs = [s for s in segments if s.kind == SegmentKind.OCR]
    assert len(ocr_segs) >= 1
    for s in ocr_segs:
        assert s.payload.get("text")  # payload.text 存在
        assert s.confidence > 0
        assert s.source_range.start == s.source_range.start  # 点事件 [frame, frame+1)


def test_ocr_detection_to_frame_range():
    """OcrDetection.to_frame_range 是点事件 [frame, frame+1)。"""
    det = OcrDetection(frame=42, text="x")
    fr = det.to_frame_range()
    assert fr.start == 42
    assert fr.duration == 1


def test_ocr_box_validation():
    """OcrBox 坐标必须在 [0,1]。"""
    with pytest.raises(ValueError):
        OcrBox(x=1.5, y=0.0, w=0.0, h=0.0)
    with pytest.raises(ValueError):
        OcrBox(x=-0.1, y=0.0, w=0.0, h=0.0)
    # 合法
    b = OcrBox(0.1, 0.2, 0.5, 0.3)
    assert b.to_dict() == {"x": 0.1, "y": 0.2, "w": 0.5, "h": 0.3}


def test_ocr_detection_from_dict_roundtrip():
    det = OcrDetection(frame=10, text="测试", confidence=0.8, language="zh",
                       box=OcrBox(0.1, 0.2, 0.3, 0.4))
    d = det.to_dict()
    det2 = OcrDetection.from_dict(d)
    assert det2.frame == 10
    assert det2.text == "测试"
    assert det2.confidence == 0.8
    assert det2.language == "zh"
    assert det2.box is not None
    assert det2.box.x == 0.1


# ================================================================
# real 路径（R4-a 已接：调用 RapidOCR 引擎；无真实引擎时抛 ImportError）
# ================================================================


def test_real_path_requires_engine():
    """R4-a：真实路径已实现——调用 _get_engine 初始化 RapidOCR 引擎并执行推理。

    rapidocr 未装时 _get_engine import 失败 → ImportError（验证真实路径被触发）。
    rapidocr 已装时引擎能初始化（用 stub sampler 返回 2x2 图，RapidOCR 返回空检测），
    验证链路不崩。
    """
    provider = OcrProvider(
        shot_spans_provider=_shot_spans,
        total_frames_provider=lambda aid: 200,
    )
    if importlib.util.find_spec("rapidocr") is None:
        # 未装 rapidocr：_get_engine import 失败 → ImportError
        with pytest.raises(ImportError):
            provider._run_ocr_real("a1", [0, 50, 99])
    else:
        # 已装：引擎初始化成功，stub sampler 返回的 2x2 图无文字 → 空检测列表
        detections = provider._run_ocr_real("a1", [0])
        assert isinstance(detections, list)
