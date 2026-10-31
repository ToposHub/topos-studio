"""R4-a：OcrProvider 真实 RapidOCR 推理路径的覆盖测试。

不依赖真实 rapidocr 安装——用 monkeypatch 注入 fake engine + fake sampler，验证
``_run_ocr_real`` 的：

- engine 惰性初始化 + 复用（不每帧重建）
- RapidOCR 输出格式 ``[box, text, score]`` → ``OcrDetection`` 的正确解析
- 像素坐标 bbox → 归一化 ``OcrBox(x,y,w,h)`` ∈ [0,1]
- 多帧采样 + 检测聚合
- sampler 返回 None 的帧被跳过（不抛）
- progress 回调按帧触发
- 包名兼容（新版 rapidocr / 旧版 rapidocr_onnxruntime）

以及模型路径架构：

- ``default_cache_dir()`` 指向项目 ``models/ai_editing/``
- ``_parse_rapidocr_item`` / ``_four_point_to_ocr_box`` / ``_img_size`` 辅助函数
"""

from __future__ import annotations

import importlib.util
from typing import Any

import numpy as np
import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.analysis_models import OcrBox, OcrDetection  # noqa: E402
from src.features.edit.ai_editing.ocr_provider import (  # noqa: E402
    OcrProvider,
    _build_ocr_detection,
    _four_point_to_ocr_box,
    _img_size,
    _normalize_rapidocr_output,
)


# ================================================================
# 测试夹具：fake RapidOCR engine + fake frame sampler
# ================================================================


class _FakeEngine:
    """模拟 RapidOCR 引擎的 __call__ 协议：返回 (result, elapsed)。

    result = [[box, text, score], ...]，box 为 4 点像素坐标。
    通过 frames_results 字典配置每帧的返回（frame_idx → result list）。
    """

    def __init__(self, frames_results: dict[int, list] | None = None) -> None:
        self.frames_results = frames_results or {}
        self.call_count = 0
        self.called_with: list[Any] = []

    def __call__(self, img: Any) -> tuple[list, float]:
        self.call_count += 1
        self.called_with.append(img)
        # 通过图像内容指纹找帧索引（fake sampler 在像素里编码了 frame_idx）
        frame_idx = int(img.flatten()[0]) if img is not None else 0
        result = self.frames_results.get(frame_idx, [])
        return result, 0.01


class _FakeSampler:
    """返回编码了 frame_idx 的 BGR ndarray（首像素 = frame_idx % 256）。

    使 _FakeEngine 能据图像内容反推帧索引。sample(None_frame) 可配置返回 None。
    """

    def __init__(self, *, none_frames: set[int] | None = None,
                 height: int = 100, width: int = 200) -> None:
        self._none = none_frames or set()
        self._h = height
        self._w = width
        self.sampled: list[int] = []

    def sample(self, frame_index: int) -> Any:
        self.sampled.append(frame_index)
        if frame_index in self._none:
            return None
        # 首像素编码 frame_idx，其余 0；shape=(H, W, 3)
        arr = np.zeros((self._h, self._w, 3), dtype=np.uint8)
        arr[0, 0] = frame_index % 256
        return arr


@pytest.fixture
def stub_rapidocr_available(monkeypatch):
    """桩 rapidocr 包为"已安装"，使 provider analyze 入口的 find_spec 放行。"""
    monkeypatch.setattr(
        importlib.util, "find_spec",
        lambda name: object() if name in ("rapidocr", "rapidocr_onnxruntime") else None,
    )


def _provider_with_engine(engine: Any, sampler: Any) -> OcrProvider:
    """构造 OcrProvider，注入 fake engine + sampler（绕过真实 _get_engine）。"""
    p = OcrProvider(sampler=sampler)
    p._engine = engine  # 直接注入，跳过 _get_engine 的 import 逻辑
    return p


# ================================================================
# _run_ocr_real 核心路径
# ================================================================


def test_run_ocr_real_parses_detections(stub_rapidocr_available):
    """真实路径：RapidOCR 输出 → OcrDetection 列表，bbox 归一化正确。"""
    # 帧 10 识别到 "hello"（4 点像素框，图像 H=100 W=200）
    engine = _FakeEngine({
        10: [[[[20, 10], [180, 10], [180, 50], [20, 50]], "hello", 0.95]],
    })
    sampler = _FakeSampler()
    provider = _provider_with_engine(engine, sampler)

    detections = provider._run_ocr_real("asset_1", [10])

    assert len(detections) == 1
    d = detections[0]
    assert d.frame == 10
    assert d.text == "hello"
    assert abs(d.confidence - 0.95) < 0.01
    # 归一化：x=20/200=0.1, y=10/100=0.1, w=160/200=0.8, h=40/100=0.4
    assert abs(d.box.x - 0.1) < 0.01
    assert abs(d.box.y - 0.1) < 0.01
    assert abs(d.box.w - 0.8) < 0.01
    assert abs(d.box.h - 0.4) < 0.01


def test_run_ocr_real_aggregates_multiple_frames(stub_rapidocr_available):
    """多帧采样：每帧的检测被聚合到一个列表（按帧顺序）。"""
    engine = _FakeEngine({
        0: [[[[10, 10], [50, 10], [50, 30], [10, 30]], "frame0_text", 0.9]],
        50: [[[[60, 40], [100, 40], [100, 60], [60, 60]], "frame50_text", 0.8]],
    })
    sampler = _FakeSampler()
    provider = _provider_with_engine(engine, sampler)

    detections = provider._run_ocr_real("asset_1", [0, 50])

    assert len(detections) == 2
    assert detections[0].frame == 0
    assert detections[0].text == "frame0_text"
    assert detections[1].frame == 50
    assert detections[1].text == "frame50_text"
    # sampler 被调用了两次（每帧一次）
    assert sampler.sampled == [0, 50]


def test_run_ocr_real_skips_none_frames(stub_rapidocr_available):
    """sampler 返回 None 的帧被跳过（不调引擎、不抛）。"""
    engine = _FakeEngine({0: []})
    sampler = _FakeSampler(none_frames={50})  # 帧 50 抽不到
    provider = _provider_with_engine(engine, sampler)

    detections = provider._run_ocr_real("asset_1", [0, 50, 99])

    # 只帧 0 和 99 被引擎处理（50 被 skip）
    assert engine.call_count == 2
    # sampler 仍被调用 3 次（包括返回 None 的那次）
    assert sampler.sampled == [0, 50, 99]
    # 帧 0 无检测（result 为空），帧 99 也无 → 空列表
    assert detections == []


def test_run_ocr_real_skips_empty_result_frames(stub_rapidocr_available):
    """某帧 RapidOCR 返回空列表（无文字）→ 不产生检测，继续处理后续帧。"""
    engine = _FakeEngine({
        0: [],  # 无文字
        1: [[[[10, 10], [20, 10], [20, 20], [10, 20]], "found", 0.7]],
    })
    sampler = _FakeSampler()
    provider = _provider_with_engine(engine, sampler)

    detections = provider._run_ocr_real("asset_1", [0, 1])

    assert len(detections) == 1
    assert detections[0].text == "found"
    assert detections[0].frame == 1


def test_engine_is_lazy_and_reused(stub_rapidocr_available):
    """engine 惰性初始化 + 复用：多次 _run_ocr_real 共用同一 engine 实例。"""
    engine = _FakeEngine({0: []})
    sampler = _FakeSampler()
    provider = _provider_with_engine(engine, sampler)

    provider._run_ocr_real("a1", [0])
    provider._run_ocr_real("a2", [0])
    provider._run_ocr_real("a3", [0])

    # 同一 engine 被 3 次调用（每帧一次），但 engine 对象未重建
    assert engine.call_count == 3


def test_run_ocr_real_invokes_progress_callback(stub_rapidocr_available):
    """progress 回调按帧触发：(current, total) 每帧一次。"""
    engine = _FakeEngine({0: [], 1: [], 2: []})
    sampler = _FakeSampler()
    provider = _provider_with_engine(engine, sampler)
    progress_calls: list[tuple[int, int]] = []
    provider.set_progress_callback(lambda cur, total: progress_calls.append((cur, total)))

    provider._run_ocr_real("a1", [0, 1, 2])

    assert progress_calls == [(1, 3), (2, 3), (3, 3)]


def test_run_ocr_real_handles_engine_exception(stub_rapidocr_available):
    """引擎在某帧抛异常 → 该帧跳过，不中断后续帧。"""
    class _BrokenEngine:
        call_count = 0
        def __call__(self, img):
            self.call_count += 1
            if self.call_count == 2:
                raise RuntimeError("gpu OOM")
            return [], 0.0
    sampler = _FakeSampler()
    provider = _provider_with_engine(_BrokenEngine(), sampler)

    # 不抛（异常被捕获并记日志）
    detections = provider._run_ocr_real("a1", [0, 1, 2])
    assert detections == []
    assert provider._engine.call_count == 3  # 三帧都尝试了


# ================================================================
# _get_engine：惰性初始化 + 包名兼容
# ================================================================


def test_get_engine_prefers_new_rapidocr(stub_rapidocr_available, monkeypatch):
    """_get_engine 优先 import 新版 rapidocr；失败时回退 rapidocr_onnxruntime。"""
    import sys

    class _FakeRapidOCR:
        instances = 0
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            _FakeRapidOCR.instances += 1
        def __call__(self, img):
            return [], 0.0

    # 注入 fake rapidocr 模块
    fake_mod = type(sys)("rapidocr")
    fake_mod.RapidOCR = _FakeRapidOCR
    fake_mod.__version__ = "2.0.1"
    monkeypatch.setitem(sys.modules, "rapidocr", fake_mod)

    _FakeRapidOCR.instances = 0
    provider = OcrProvider()
    engine = provider._get_engine()
    assert _FakeRapidOCR.instances == 1
    assert isinstance(engine, _FakeRapidOCR)
    # 再次调用复用（不重建）
    provider._get_engine()
    assert _FakeRapidOCR.instances == 1


def test_get_engine_passes_local_model_path(stub_rapidocr_available, monkeypatch, tmp_path):
    """pack 目录非空时，_engine_kwargs 传 model_path 让引擎用本地权重。"""
    import sys

    received_kwargs: dict = {}
    class _RapidOCR:
        def __init__(self, **kwargs):
            received_kwargs.update(kwargs)
        def __call__(self, img):
            return [], 0.0
    fake_mod = type(sys)("rapidocr")
    fake_mod.RapidOCR = _RapidOCR
    monkeypatch.setitem(sys.modules, "rapidocr", fake_mod)

    # 造一个已安装的 pack（非空目录）
    pack_dir = tmp_path / "rapidocr_ocr"
    pack_dir.mkdir()
    (pack_dir / "det.onnx").write_bytes(b"fake")

    from src.features.edit.ai_editing.model_pack_manager import ModelPackManager
    mgr = ModelPackManager(cache_dir=tmp_path, strict_verification=False)
    mgr.mark_installed("rapidocr_ocr", str(pack_dir))

    provider = OcrProvider(model_pack_manager=mgr)
    provider._get_engine()
    assert received_kwargs.get("model_path") == str(pack_dir)


# ================================================================
# 辅助函数：_build_ocr_detection / _normalize_rapidocr_output / _four_point_to_ocr_box / _img_size
# ================================================================


def test_build_ocr_detection_normal():
    """正常输入 → OcrDetection（bbox 归一化）。"""
    img = np.zeros((100, 200, 3), dtype=np.uint8)
    box = [[20, 10], [180, 10], [180, 50], [20, 50]]
    det = _build_ocr_detection(box, "text", 0.88, frame_idx=5, img=img)
    assert det is not None
    assert det.text == "text"
    assert det.frame == 5
    assert abs(det.confidence - 0.88) < 0.01


def test_build_ocr_detection_empty_text():
    """空文本 → None（跳过）。"""
    img = np.zeros((100, 200, 3), dtype=np.uint8)
    box = [[10, 10], [20, 10], [20, 20], [10, 20]]
    assert _build_ocr_detection(box, "", 0.5, frame_idx=0, img=img) is None


def test_build_ocr_detection_clamps_confidence():
    """置信度越界（>1 或 <0）→ 钳制到 [0,1]。"""
    img = np.zeros((100, 200, 3), dtype=np.uint8)
    box = [[10, 10], [20, 10], [20, 20], [10, 20]]
    det = _build_ocr_detection(box, "t", 1.5, frame_idx=0, img=img)
    assert det is not None
    assert det.confidence == 1.0

    det2 = _build_ocr_detection(box, "t", -0.5, frame_idx=0, img=img)
    assert det2 is not None
    assert det2.confidence == 0.0


def test_normalize_rapidocr_output_new_version_object():
    """新版 rapidocr (>=3.0)：返回带 .txts/.boxes/.scores 属性的对象。"""
    class _FakeOutput:
        boxes = [[[19, 0], [274, 0], [274, 99], [19, 99]]]
        txts = ("HELLO",)
        scores = (0.998,)

    boxes, txts, scores = _normalize_rapidocr_output(_FakeOutput())
    assert len(txts) == 1
    assert txts[0] == "HELLO"
    assert len(boxes) == 1
    assert scores[0] == pytest.approx(0.998)


def test_normalize_rapidocr_output_old_version_tuple():
    """旧版 rapidocr_onnxruntime (<2.0)：返回 (result, elapsed) 元组。

    result 是 [[box, text, score], ...]——外层 list，每个元素是三元组。
    """
    result = [
        [[[10, 10], [50, 10], [50, 30], [10, 30]], "old", 0.9],
    ]
    boxes, txts, scores = _normalize_rapidocr_output((result, 0.01))
    assert len(txts) == 1
    assert txts[0] == "old"


def test_normalize_rapidocr_output_empty():
    """无检测 → 空三元组。"""
    boxes, txts, scores = _normalize_rapidocr_output(None)
    assert boxes == [] and txts == [] and scores == []



def test_four_point_to_ocr_box_normalizes():
    """4 点像素坐标 → 归一化 OcrBox。"""
    # 图像 100x200；框 (20,10)-(180,50)
    box = _four_point_to_ocr_box(
        [[20, 10], [180, 10], [180, 50], [20, 50]],
        width=200, height=100,
    )
    assert box is not None
    assert abs(box.x - 0.1) < 0.01
    assert abs(box.y - 0.1) < 0.01
    assert abs(box.w - 0.8) < 0.01
    assert abs(box.h - 0.4) < 0.01


def test_four_point_to_ocr_box_clamps_overflow():
    """越界坐标被钳制到 [0,1]（不抛 ValueError）。"""
    # 坐标超过图像尺寸
    box = _four_point_to_ocr_box(
        [[0, 0], [300, 0], [300, 200], [0, 200]],
        width=200, height=100,
    )
    assert box is not None
    assert box.x == 0.0
    assert box.y == 0.0
    assert box.w == 1.0  # 300/200 钳到 1.0
    assert box.h == 1.0  # 200/100 钳到 1.0


def test_four_point_to_ocr_box_zero_area_returns_none():
    """退化框（零面积）→ None。"""
    box = _four_point_to_ocr_box(
        [[50, 50], [50, 50], [50, 50], [50, 50]],
        width=200, height=100,
    )
    assert box is None


def test_img_size_from_ndarray():
    """从 ndarray.shape 取 (H, W)。"""
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    assert _img_size(img) == (480, 640)
    # 灰度图（2D）
    gray = np.zeros((100, 200), dtype=np.uint8)
    assert _img_size(gray) == (100, 200)
    # 无效输入
    assert _img_size(None) == (0, 0)
    assert _img_size("not_an_image") == (0, 0)


# ================================================================
# 模型路径架构
# ================================================================


def test_default_cache_dir_points_to_models_ai_editing():
    """default_cache_dir() 返回项目根的 models/ai_editing/。"""
    from pathlib import Path
    from src.features.edit.ai_editing.model_pack_manager import default_cache_dir

    cache_dir = default_cache_dir()
    # 落在项目内（包含 models/ai_editing）
    assert cache_dir.name == "ai_editing"
    assert cache_dir.parent.name == "models"
    # 向上应能找到项目根标记（pyproject.toml / src/）
    assert (cache_dir.parent.parent / "pyproject.toml").exists() or \
           (cache_dir.parent.parent / "src").is_dir()


def test_builtin_packs_match_directory_names():
    """内置 pack_id 与 models/ai_editing/ 下的子目录名一一对应。

    R4-c：Qwen3 是默认 ASR 链路；WhisperX、Demucs 与 Qwen3-TTS 是按需
    注册的兼容/生成能力。每个内置 pack 都必须有对应目录。
    每个 pack_id 都有对应子目录。
    """
    from pathlib import Path
    from src.features.edit.ai_editing.model_pack_manager import (
        BUILTIN_SPECS, default_cache_dir,
    )

    pack_ids = {s.pack_id for s in BUILTIN_SPECS}
    expected = {
        # ASR + 对齐 + 分离（R4-c）
        "qwen3_asr", "qwen3_asr_large", "qwen3_align", "nemo_sortformer",
        # 可选 ASR 回退与预处理
        "whisperx_base", "demucs_v4",
        # TTS
        "qwen3_tts_customvoice", "qwen3_tts_tokenizer_12hz", "qwen3_tts_base",
        # OCR（R4-a）
        "rapidocr_ocr",
    }
    assert pack_ids == expected

    # 验证每个 pack 都有对应的子目录（含 .gitkeep）
    base = default_cache_dir()
    for pack_id in pack_ids:
        assert (base / pack_id).is_dir(), f"missing dir: {base / pack_id}"
