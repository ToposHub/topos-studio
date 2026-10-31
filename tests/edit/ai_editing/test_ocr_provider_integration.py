"""R4-a 真实 RapidOCR 集成测试。

依赖真实安装的 rapidocr 包（``pip install rapidocr onnxruntime``）。
未安装时整体 skip——这是真正的端到端验证：合成带文字的图像 → RapidOCR 推理 →
验证 OcrDetection 的文本/置信度/bbox 合理性。

与 test_ocr_provider_real.py 的区别：
- real.py：用 fake engine + monkeypatch 测适配器逻辑（不依赖真实 rapidocr）
- 本文件：真实 RapidOCR 引擎，验证"装了就能跑"的端到端可用性
"""

from __future__ import annotations

import importlib.util

import numpy as np
import pytest

pytestmark = pytest.mark.edit_core

# 整个模块依赖真实 rapidocr；未装则 skip（不报失败）
rapidocr_spec = importlib.util.find_spec("rapidocr")
if rapidocr_spec is None:
    pytest.skip(
        "rapidocr not installed (pip install rapidocr onnxruntime to enable "
        "real OCR integration tests)",
        allow_module_level=True,
    )


from src.features.edit.ai_editing.analysis_models import OcrBox  # noqa: E402
from src.features.edit.ai_editing.ocr_provider import OcrProvider  # noqa: E402


class _TextImageSampler:
    """返回带文字的合成图像（模拟视频帧抽到的 OCR 内容）。

    用 numpy 画一个简单图案——真实 RapidOCR 能否识别取决于图像复杂度。
    本测试主要验证"链路跑通 + 返回结构正确"，而非识别准确率（那需要真实视频帧）。
    """

    def __init__(self, images: dict[int, np.ndarray] | None = None) -> None:
        self._images = images or {}
        self.sampled: list[int] = []

    def sample(self, frame_index: int) -> np.ndarray | None:
        self.sampled.append(frame_index)
        if frame_index in self._images:
            return self._images[frame_index]
        # 默认返回一张带纹理的图（非纯色，避免 RapidOCR 报空）
        img = np.zeros((200, 400, 3), dtype=np.uint8)
        # 画一些随机线条模拟有内容的画面
        for i in range(0, 400, 40):
            img[:, i:i + 2] = 128
        return img

    @property
    def sampled_frames(self) -> tuple[int, ...]:
        return tuple(self.sampled)


def _make_text_image(text: str = "HELLO", width: int = 400, height: int = 100) -> np.ndarray:
    """用 PIL 画一张含清晰大字的白底黑字图（RapidOCR 应能识别）。

    若 PIL 不可用则回退到纯几何图案（识别率低，但验证链路不崩）。
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGB", (width, height), color=(255, 255, 255))
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("Arial.ttf", 60)
        except (OSError, IOError):
            font = ImageFont.load_default()
        # 居中绘制
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x = (width - tw) // 2
        y = (height - th) // 2
        draw.text((x, y), text, fill=(0, 0, 0), font=font)
        return np.array(img)[:, :, ::-1].copy()  # RGB → BGR
    except ImportError:
        # PIL 不可用：返回几何图案（RapidOCR 可能识别不出文字，但不该崩）
        img = np.full((height, width, 3), 255, dtype=np.uint8)
        img[20:80, 50:350] = 0  # 黑色矩形模拟文字区域
        return img


# ================================================================
# 真实链路：RapidOCR 引擎初始化 + 推理
# ================================================================


def test_real_rapidocr_engine_initializes():
    """真实 RapidOCR 引擎能初始化（_get_engine 不抛）。

    这是链路跑通的第一步：确认 rapidocr 包可 import、引擎能构造。
    首次初始化可能触发模型自动下载（~15MB），需联网。
    """
    provider = OcrProvider()
    engine = provider._get_engine()
    assert engine is not None
    # 第二次调用复用（不重建）
    assert provider._get_engine() is engine


def test_real_ocr_run_on_synthetic_text_image():
    """对合成文字图执行真实 OCR，验证返回结构正确。

    用 PIL 画 "HELLO" 大字 → RapidOCR 推理 → 验证：
    - 返回 list[OcrDetection]（可能为空，若识别失败，但不该抛）
    - 若识别到，text/confidence/box 字段合理
    - bbox 归一化到 [0,1]
    """
    text_img = _make_text_image("HELLO", width=400, height=100)
    sampler = _TextImageSampler({0: text_img})
    provider = OcrProvider(sampler=sampler)

    detections = provider._run_ocr_real("test_asset", [0])

    # RapidOCR 可能识别成功（返回检测）或失败（空列表），两者都算链路通
    assert isinstance(detections, list)
    for d in detections:
        assert d.frame == 0
        assert isinstance(d.text, str) and len(d.text) > 0
        assert 0.0 <= d.confidence <= 1.0
        if d.box is not None:
            assert isinstance(d.box, OcrBox)
            assert 0.0 <= d.box.x <= 1.0
            assert 0.0 <= d.box.y <= 1.0
            assert 0.0 <= d.box.w <= 1.0
            assert 0.0 <= d.box.h <= 1.0
    # sampler 被调用
    assert sampler.sampled == [0]


def test_real_ocr_handles_empty_frame():
    """纯色帧（无文字）→ RapidOCR 返回空列表 → 不抛、不产生检测。"""
    # 纯白图（无任何文字特征）
    blank = np.full((100, 200, 3), 255, dtype=np.uint8)
    sampler = _TextImageSampler({0: blank})
    provider = OcrProvider(sampler=sampler)

    detections = provider._run_ocr_real("blank_asset", [0])
    assert detections == []  # 纯色帧不应有 OCR 检测


def test_real_ocr_progress_callback_fires():
    """真实推理路径触发 progress 回调（每帧一次）。"""
    text_img = _make_text_image("TEST")
    sampler = _TextImageSampler({0: text_img, 1: text_img})
    provider = OcrProvider(sampler=sampler)
    progress_calls: list[tuple[int, int]] = []
    provider.set_progress_callback(lambda cur, total: progress_calls.append((cur, total)))

    provider._run_ocr_real("asset", [0, 1])
    assert progress_calls == [(1, 2), (2, 2)]


def test_real_ocr_via_analyze_full_path():
    """端到端：analyze（含 find_spec 探测 + pack 校验 + 选帧 + 真实推理）。

    设置 TOPOS_OCR_REAL=1 使 _run_ocr 走真实路径。验证 AssetAnalysis.ocr 字段
    被填充（即使识别为空，结构也正确）。
    """
    import os

    from src.features.edit.ai_editing.analysis_models import AssetFingerprint

    text_img = _make_text_image("HELLO WORLD", width=500, height=120)
    sampler = _TextImageSampler({0: text_img})

    provider = OcrProvider(
        sampler=sampler,
        shot_spans_provider=lambda aid: (),  # 无 shot → 均匀采样
        total_frames_provider=lambda aid: 1,
    )

    old = os.environ.get("TOPOS_OCR_REAL")
    os.environ["TOPOS_OCR_REAL"] = "1"
    try:
        analysis, segments = provider.analyze(
            "e2e_asset", AssetFingerprint(), fps="24/1",
        )
    finally:
        if old is None:
            os.environ.pop("TOPOS_OCR_REAL", None)
        else:
            os.environ["TOPOS_OCR_REAL"] = old

    # AssetAnalysis 结构正确
    assert analysis.asset_id == "e2e_asset"
    assert isinstance(analysis.ocr, tuple)
    # provenance 标注了真实模型版本（非 rapidocr-stub-v1）
    prov_model = analysis.provenance.model
    assert prov_model != "rapidocr-stub-v1"
    assert "rapidocr" in prov_model.lower()
    # segments 与 ocr 检测数对应（每个检测 → 一个 OCR segment 点事件）
    assert len(segments) == len(analysis.ocr)
