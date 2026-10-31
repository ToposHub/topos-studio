"""TAEP M8 — 本地 OCR provider（RapidOCR adapter）。

完全镜像 ``WhisperXTranscriptProvider`` 的探测 / 降级 / stub-real 开关模式（§16.3 /
§18.1）：

- ``find_spec("rapidocr") / find_spec("rapidocr_onnxruntime")`` 双包探测 → 两个都缺失才
  ``ModelPackNotInstalledError``（**不静默降级**，与 WhisperX 一致；§11.1 M4 红线沿用；
  AUD-14010：与 ``_get_engine`` 的旧包回退保持一致）。
- ``ModelPackManager.require_usable`` 校验 pack；本地 re-raise 同名异常避免硬依赖。
- ``TOPOS_OCR_REAL=1`` 开真实路径（首版 ``NotImplementedError``，等控制器接线 + 真实
  sampler 注入）；否则 ``_stub_ocr`` 返回确定性结果（无 GPU / 无真实文件）。
- 输出 ``AssetAnalysis.ocr``（``tuple[OcrDetection]``）+ ``FieldProvenance.single("ocr",
  prov)``；只填 ocr 字段，其余由 ``AnalysisService._merge_analysis`` 合并。

红线（§18.1 / §9）：

- **OCR 文本是不可信数据**：识别出的画面文字（屏幕字幕 / OCR）经 ``sanitize_untrusted``
  在 ``tool_facade`` 层标记后才能进模型上下文；不能改变系统指令 / 权限，也不能未经用户
  确认进字幕（``subtitle_draft`` 仍只取 TRANSCRIPT）。
- **shot-level 聚合**：``_select_frames`` 优先用注入的 ``shot_spans_provider`` 派生的关键
  帧抽样，**不逐帧跑**（避免单帧幻觉 + 重复检测）。provider 不可用时退化为均匀抽样
  并在 provenance 标注。
- provider 无 Qt / 无 timeline 写权限 / 无任意路径权限（路径访问由注入的 sampler 在
  控制器层经 ``PathGrant`` 把控）。
"""

from __future__ import annotations

import hashlib
import importlib.util
import logging
import os
import threading
from typing import Any, Callable

from src.features.edit.ai_editing.analysis_models import (
    AnalysisProvenance,
    AssetAnalysis,
    AssetFingerprint,
    FieldProvenance,
    OcrBox,
    OcrDetection,
    analysis_to_segments,
)
from src.features.edit.ai_editing.editorial_models import SegmentKind, SemanticSegment
from src.features.edit.ai_editing.keyframe_sampler import (
    FrameSamplerProtocol,
    ShotSpan,
    StubFrameSampler,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------- #
# 异常（re-export 同名异常，避免 provider 层硬依赖 model_pack_manager）
# ---------------------------------------------------------------------- #


class ModelPackNotInstalledError(RuntimeError):
    """RapidOCR / OCR 模型未安装 / 未校验（§11.1 M4 红线沿用：不静默降级）。

    从 model_pack_manager 复用同义异常名；这里 re-export 避免 provider 层多依赖。
    """

    def __init__(self, pack_id: str, detail: str = "") -> None:
        self.pack_id = pack_id
        super().__init__(
            f"ModelPack {pack_id!r} is not installed or not verified"
            + (f": {detail}" if detail else "")
        )


# ---------------------------------------------------------------------- #
# OcrProvider
# ---------------------------------------------------------------------- #


# 关键帧抽样的回退（无 shot_spans_provider 时）均匀分片数。
# §18.1：不逐帧；按总帧数均匀采样，限制总帧数避免长片爆炸。
_DEFAULT_MAX_SAMPLED_FRAMES = 30


class OcrProvider:
    """本地 OCR provider（M8 / §18.1 RapidOCR adapter）。

    实现 :class:`AnalysisProvider` Protocol（duck typing，仿 WhisperXTranscriptProvider）。

    输出 ``AssetAnalysis.ocr``（``tuple[OcrDetection]``）+ 字段级 provenance
    （``FieldProvenance.single("ocr", prov)``）；其余字段不填（由 AnalysisService 的
    ``_merge_analysis`` 与其他 provider 结果合并）。
    """

    provider_id = "ocr_local"
    supported_kinds = (SegmentKind.OCR,)

    def __init__(
        self,
        model_pack_manager: Any | None = None,
        *,
        sampler: FrameSamplerProtocol | None = None,
        shot_spans_provider: "Callable[[str], tuple[ShotSpan, ...]] | None" = None,
        total_frames_provider: "Callable[[str], int] | None" = None,
    ) -> None:
        """
        Args:
            model_pack_manager: :class:`ModelPackManager`（探测模型安装）。None 时仅用
                ``find_spec`` 探测 rapidocr 包是否可 import（测试/无 app 场景）。
            sampler: 帧抽取（``FrameSamplerProtocol``）。None 时用 ``StubFrameSampler``
                （仅测试/桩场景；真实路径由控制器注入包装 VideoFrameSampler）。
            shot_spans_provider: ``asset_id → tuple[ShotSpan, ...]``，用于关键帧抽样。
                None 时退化为均匀抽样（在 provenance 标注 ``frame_selection="even"``）。
            total_frames_provider: ``asset_id → int``，均匀抽样回退用。None 时用 0。
        """
        self._pack_manager = model_pack_manager
        self._sampler = sampler or StubFrameSampler()
        self._shot_spans_provider = shot_spans_provider
        self._total_frames_provider = total_frames_provider
        # R4-a：RapidOCR 引擎惰性缓存。真实路径首次调用 _get_engine() 时创建，
        # 之后复用（避免每帧重建；模型加载昂贵）。stub 路径不触碰它。
        self._engine: Any = None
        # AUD-14014：惰性初始化双检锁——防止两个分析线程并发 analyze 时各自
        # 构建 RapidOCR（模型加载昂贵、内存翻倍，先建实例被覆盖后交由 GC）。
        self._engine_lock = threading.Lock()
        # R4-a：进度回调注入点（签名 (current, total)）。None 时不回调。
        # 由控制器注入（驱动 UI 进度条）；provider 自身不依赖其存在。
        self._progress_cb: Callable[[int, int], Any] | None = None

    def set_progress_callback(self, cb: "Callable[[int, int], Any] | None") -> None:
        """注入进度回调（签名 ``(current_frame, total_frames)``）。

        真实推理路径每完成一帧调用一次；stub 路径不调用。由控制器注入以驱动 UI 进度条。
        """
        self._progress_cb = cb

    # ---------------- AnalysisProvider.analyze ---------------- #

    def analyze(
        self,
        asset_id: str,
        fingerprint: AssetFingerprint,
        *,
        fps: str,
        progress: Any | None = None,
    ) -> tuple[AssetAnalysis, tuple[SemanticSegment, ...]]:
        """执行 OCR（M8）。

        Raises:
            ModelPackNotInstalledError: rapidocr 包未装 或 模型 pack 未下载/未校验。
        """
        # 1. 探测 rapidocr 是否可 import（可选依赖，不进基础安装）。
        #    AUD-14010：_get_engine 支持旧包 rapidocr_onnxruntime 回退，此处
        #    必须同步探测两个包名——只装旧包的环境不能在入口直接被拒。
        if (
            importlib.util.find_spec("rapidocr") is None
            and importlib.util.find_spec("rapidocr_onnxruntime") is None
        ):
            raise ModelPackNotInstalledError(
                "rapidocr_ocr",
                detail=(
                    "python package 'rapidocr' (or the legacy "
                    "'rapidocr_onnxruntime') is not installed"
                ),
            )

        # 2. 校验模型 pack（rapidocr_ocr 必须 usable）
        pack_path = self._require_pack("rapidocr_ocr")
        _ = pack_path  # 已校验过；保留供日志

        # 3. 选帧（shot-level 聚合，避免逐帧）
        frames, selection_mode = self._select_frames(asset_id)

        # 4. 推理（首版确定性 stub；真实路径由 env 开关）
        detections = self._run_ocr(asset_id, frames)

        # 5. 组装 AssetAnalysis（只填 ocr + 字段级 provenance）
        extra: dict[str, Any] = {"frame_selection": selection_mode}
        provenance = AnalysisProvenance(
            provider=self.provider_id,
            model=self._model_version(),
            extra=extra,
        )
        analysis = AssetAnalysis(
            asset_id=asset_id,
            fingerprint=fingerprint,
            fps=fps,
            ocr=tuple(detections),
            provenance=provenance,
            field_provenance=FieldProvenance.single("ocr", provenance),
        )
        return analysis, tuple(analysis_to_segments(analysis))

    # ---------------- 选帧（shot 聚合 / 均匀回退）---------------- #

    def _select_frames(self, asset_id: str) -> "tuple[list[int], str]":
        """选关键帧列表 + 返回选择模式标签。

        优先用 shot_spans_provider（head/mid/tail）；否则均匀抽样。
        返回 ``(frames, mode)``，mode ∈ {"shot_head_mid_tail", "even"}。
        """
        if self._shot_spans_provider is not None:
            try:
                spans = self._shot_spans_provider(asset_id)
            except Exception:  # provider 故障不应阻断；退化为均匀
                spans = ()
            if spans:
                frames: list[int] = []
                for span in spans:
                    for f in span.keyframe_indices():
                        if f not in frames:
                            frames.append(f)
                return frames, "shot_head_mid_tail"
        # 均匀回退
        total = self._total_frames(asset_id)
        return _even_sample(total, _DEFAULT_MAX_SAMPLED_FRAMES), "even"

    def _total_frames(self, asset_id: str) -> int:
        if self._total_frames_provider is not None:
            try:
                return max(0, int(self._total_frames_provider(asset_id)))
            except Exception:
                return 0
        return 0

    # ---------------- 推理（首版 stub / 真实开关）---------------- #

    def _run_ocr(self, asset_id: str, frames: list[int]) -> list[OcrDetection]:
        """OCR 推理分发（env 开关）。"""
        if os.environ.get("TOPOS_OCR_REAL") == "1":
            return self._run_ocr_real(asset_id, frames)
        return self._stub_ocr(asset_id, frames)

    def _run_ocr_real(
        self, asset_id: str, frames: list[int]
    ) -> list[OcrDetection]:
        """真实 RapidOCR 推理路径（R4-a 实现）。

        逐个采样帧抽取 → RapidOCR 推理 → 像素坐标 bbox 归一化到 [0,1] → 聚合为
        ``OcrDetection`` 列表。engine 惰性初始化并复用（模型加载昂贵，避免每帧重建）。

        sampler 由控制器层注入（包装 ``VideoFrameSampler.sample_single_frame``，PyAV 解
        码）；测试时注入 fake sampler + monkeypatch ``_get_engine``。无真实视频解码器
        时 ``sampler.sample`` 返回 None → 该帧被跳过（不抛）。

        Args:
            frames: ``_select_frames`` 选出的关键帧索引列表（shot 级聚合，非逐帧）。

        Returns:
            所有采样帧上的 OCR 检测（按帧顺序，帧内按 RapidOCR 输出顺序）。
        """
        engine = self._get_engine()
        detections: list[OcrDetection] = []
        total = len(frames)
        for i, frame_idx in enumerate(frames):
            # progress 回调：每帧处理前报告（current, total）。放在循环顶部，使
            # None 帧 / 异常帧 / 空结果帧也计入进度——进度反映"处理了几帧"，
            # 而非"产生了几条检测"。
            if self._progress_cb is not None:
                try:
                    self._progress_cb(i + 1, total)
                except Exception:
                    logger.debug("progress callback failed", exc_info=True)
            img = self._sampler.sample(frame_idx)
            if img is None:
                logger.debug("OCR: frame %d sampled None (asset=%s), skipped", frame_idx, asset_id)
                continue
            # RapidOCR 接受 BGR ndarray / 文件路径 / PIL。返回值版本差异大：
            # - 新版 rapidocr (>=3.0)：返回 RapidOCROutput 对象，有 .boxes / .txts / .scores
            # - 旧版 rapidocr_onnxruntime (<2.0)：返回 (result, elapsed) 元组，
            #   result = [[box, text, score], ...]
            try:
                raw_output = engine(img)
            except Exception:
                logger.warning("OCR engine failed on frame %d (asset=%s)", frame_idx, asset_id, exc_info=True)
                continue
            # 归一化为统一的 (boxes, txts, scores) 三元组
            boxes, txts, scores = _normalize_rapidocr_output(raw_output)
            if len(txts) == 0:
                continue
            for box, text, score in zip(boxes, txts, scores):
                ocr_det = _build_ocr_detection(box, text, score, frame_idx, img)
                if ocr_det is not None:
                    detections.append(ocr_det)
        return detections

    # ---------------- RapidOCR 引擎（惰性 + 包名兼容）---------------- #

    def _get_engine(self) -> Any:
        """惰性创建并缓存 RapidOCR 引擎。

        包名兼容（新版/旧版 RapidOCR 的 import 路径不同）：
        - 新版 ``rapidocr``（>= 2.0，PyPI: ``pip install rapidocr onnxruntime``）：
          ``from rapidocr import RapidOCR``
        - 旧版 ``rapidocr_onnxruntime``（< 2.0，PyPI: ``rapidocr-onnxruntime``）：
          ``from rapidocr_onnxruntime import RapidOCR``

        两者 ``RapidOCR()`` 构造与 ``__call__(img) → (result, elapsed)`` 协议一致。
        ``find_spec`` 在 ``analyze`` 入口已确认至少一个可用，故此处不重复探测。
        """
        # AUD-14014：双检锁——锁外快命中免锁返回；锁内二次检查保证并发首调
        # 只构建一次引擎（模型加载昂贵，重复加载浪费且先建实例被覆盖）。
        engine = self._engine
        if engine is not None:
            return engine
        with self._engine_lock:
            if self._engine is not None:
                return self._engine
            engine = self._build_engine()
            self._engine = engine
            return engine

    def _build_engine(self) -> Any:
        """实际构建 RapidOCR 引擎（必须在持 ``_engine_lock`` 时调用）。"""
        # 优先新版 rapidocr，回退旧版 rapidocr_onnxruntime
        try:
            from rapidocr import RapidOCR as _RapidOCR  # type: ignore[import-not-found]
        except ImportError:
            from rapidocr_onnxruntime import RapidOCR as _RapidOCR  # type: ignore[import-not-found]
        # 模型路径（pack 目录）传给引擎，使其优先用 models/ai_editing/rapidocr_ocr/
        # 下的权重；None 时 RapidOCR 用其内置默认权重（首次自动下载）。
        pack_path = ""
        try:
            pack_path = self._require_pack("rapidocr_ocr")
        except ModelPackNotInstalledError:
            pack_path = ""  # 未本地安装 pack → 让引擎用默认权重
        engine = _RapidOCR(**self._engine_kwargs(pack_path))
        logger.info("RapidOCR engine initialized (model_pack_path=%r)", pack_path or "<default>")
        return engine

    @staticmethod
    def _engine_kwargs(pack_path: str) -> dict[str, Any]:
        """构造 RapidOCR 引擎参数。

        若 pack_path 非空且指向含模型文件的目录，传 ``model_path`` 让引擎加载本地
        权重（避免首次自动下载）；否则返回空 dict，引擎用内置默认权重。不同版本
        RapidOCR 的参数名可能不同（新版 ``model_path`` / 旧版 ``model_type``），此处
        只传 ``model_path``（新版约定），引擎识别不到时忽略。
        """
        import os

        if pack_path and os.path.isdir(pack_path):
            # 目录非空（有模型文件）才传；空目录会让引擎报错
            if any(os.scandir(pack_path)):
                return {"model_path": pack_path}
        return {}

    def _stub_ocr(self, asset_id: str, frames: list[int]) -> list[OcrDetection]:
        """确定性 stub：按 asset_id 哈希 + 帧列表返回固定 OCR 检测（无 GPU/文件）。

        使 M8 在无 RapidOCR 安装时仍可单测 provider 的字段级 provenance / 选帧 /
        降级路径。返回的检测文本是中文示例（"新品上市" / "立即购买"），仅用于测试
        断言；真实文本由 ``_run_ocr_real`` 产。
        """
        # 用 sampler 抽帧（确保 stub 走了注入的 sampler，测试可断言采样帧集合）
        for f in frames:
            _ = self._sampler.sample(f)  # 触发 sampler 调用（stub 记录采样帧）
        # 按 asset_id 哈希选一组固定文本（确定性）
        h = int(hashlib.sha256(asset_id.encode("utf-8")).hexdigest(), 16)
        candidates = [
            OcrDetection(frame=0, text="新品上市", confidence=0.92, language="zh",
                         box=OcrBox(0.1, 0.05, 0.8, 0.08)),
            OcrDetection(frame=0, text="立即购买", confidence=0.88, language="zh",
                         box=OcrBox(0.3, 0.85, 0.4, 0.1)),
        ]
        # 选帧存在时把检测绑定到首个帧（保证 frame 落在抽样集合内）
        first_frame = frames[0] if frames else 0
        chosen = candidates[h % len(candidates)]
        # 返回该候选 + 若帧数 >= 2 再补一个（多样性）
        out = [OcrDetection(
            frame=first_frame, text=chosen.text, confidence=chosen.confidence,
            language=chosen.language, box=chosen.box,
        )]
        if len(frames) >= 2:
            other = candidates[(h + 1) % len(candidates)]
            out.append(OcrDetection(
                frame=frames[len(frames) // 2], text=other.text,
                confidence=other.confidence, language=other.language, box=other.box,
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

    @staticmethod
    def _model_version() -> str:
        """模型版本标识（写入 provenance，供 stale 判定）。

        AUD-14009：标识必须区分 stub 与真实推理路径。旧实现只看包是否可
        import——analyze 入口已保证至少一个 rapidocr 包可用，于是装了包的
        stub 运行也被标成 ``rapidocr-<version>``：用户打开 ``TOPOS_OCR_REAL``
        后 stale 判定认为模型未变，缓存的 stub 检测（编造文本）继续生效。
        现在以 env 开关为准：stub 路径恒返回 ``rapidocr-stub-v1``；真实路径
        读引擎包版本（读不到时 ``rapidocr-real``），开关切换必然改变指纹 →
        触发重新分析。
        """
        if os.environ.get("TOPOS_OCR_REAL") != "1":
            return "rapidocr-stub-v1"
        try:
            import rapidocr as _r  # type: ignore[import-not-found]
            return f"rapidocr-{getattr(_r, '__version__', None) or 'real'}"
        except Exception:
            try:
                import rapidocr_onnxruntime as _r  # type: ignore[import-not-found]
                return f"rapidocr_onnxruntime-{getattr(_r, '__version__', None) or 'real'}"
            except Exception:
                return "rapidocr-real"


# ---------------------------------------------------------------------- #
# 辅助：均匀抽样（shot provider 不可用时的回退）
# ---------------------------------------------------------------------- #


def _even_sample(total_frames: int, max_samples: int) -> list[int]:
    """在 ``[0, total_frames)`` 上均匀取至多 ``max_samples`` 个帧（避免逐帧 / 长片爆炸）。

    无 shot 边界时的保守回退（§18.1：仍不逐帧）。``total_frames <= 0`` → 空列表。
    """
    if total_frames <= 0:
        return []
    n = min(max(1, max_samples), total_frames)
    if n == 1:
        return [0]
    return [round(i * (total_frames - 1) / (n - 1)) for i in range(n)]


def _normalize_rapidocr_output(raw_output: Any) -> tuple[list, list, list]:
    """把 RapidOCR 不同版本的输出归一化为 (boxes, txts, scores) 三元组。

    - 新版 rapidocr (>=3.0)：返回 RapidOCROutput 对象，有 .boxes / .txts / .scores 属性。
      boxes 是 numpy array [[[x1,y1],...x4], ...]。
    - 旧版 rapidocr_onnxruntime (<2.0)：返回 (result, elapsed) 元组，result=[[box,text,score],...]。
    - 某些版本返回 None（无检测）。

    返回三个等长列表；无检测时均为空列表。
    """
    # 新版：对象有 .txts 属性
    txts = getattr(raw_output, "txts", None)
    if txts is not None:
        boxes = getattr(raw_output, "boxes", None)
        scores = getattr(raw_output, "scores", None)
        # boxes/scores 可能是 numpy array（不能用 `or`/`not` 判空，会触发 ambiguous）
        if boxes is None:
            boxes = []
        if scores is None:
            scores = []
        # numpy array → 转 list；boxes 可能是 ndarray
        try:
            boxes = [list(b) for b in boxes]
        except (TypeError, ValueError):
            boxes = list(boxes)
        return list(boxes), list(txts), list(scores)

    # 旧版：(result, elapsed) 元组
    if isinstance(raw_output, tuple) and len(raw_output) >= 1:
        result = raw_output[0]
        if result is None:
            return [], [], []
        boxes, txts, scores = [], [], []
        for item in result:
            # 每项应是 [box, text, score]（box 是 4 点坐标列表）
            if not isinstance(item, (list, tuple)) or len(item) != 3:
                continue
            box_raw, text, score = item
            boxes.append(box_raw)
            txts.append(text)
            scores.append(score)
        return boxes, txts, scores

    # 其他：无法识别
    return [], [], []


def _build_ocr_detection(
    box_raw: Any, text: Any, score: Any, frame_idx: int, img: Any,
) -> OcrDetection | None:
    """从单个 OCR 检测的原始字段构造 OcrDetection。

    box_raw 为 4 点像素坐标 [[x1,y1],...x4]；按图像尺寸归一化到 [0,1]。
    """
    if not text or not isinstance(text, str):
        return None
    height, width = _img_size(img)
    if height <= 0 or width <= 0:
        return None
    ocr_box = _four_point_to_ocr_box(box_raw, width, height)
    if ocr_box is None:
        return None
    try:
        confidence = max(0.0, min(1.0, float(score)))
    except (TypeError, ValueError):
        confidence = 0.0
    return OcrDetection(
        frame=int(frame_idx),
        text=text,
        confidence=confidence,
        box=ocr_box,
    )


def _img_size(img: Any) -> tuple[int, int]:
    """从图像对象取 (height, width)。

    支持 numpy ndarray（``shape = (H, W, ...)``）；其他类型返回 (0,0)。
    """
    shape = getattr(img, "shape", None)
    if shape is None or len(shape) < 2:
        return 0, 0
    try:
        return int(shape[0]), int(shape[1])
    except (TypeError, ValueError):
        return 0, 0


def _four_point_to_ocr_box(box_raw: Any, width: int, height: int) -> OcrBox | None:
    """4 点像素坐标 → 归一化 OcrBox(x, y, w, h)。

    RapidOCR 的 box 是 4 个角点 ``[[x1,y1], [x2,y2], [x3,y3], [x4,y4]]``（顺时针，左上
    起）。转成左上角 + 宽高的归一化表示：取 min/max 求 axis-aligned bounding box，
    再除以图像宽高归一化到 [0,1]。
    """
    try:
        xs = [float(p[0]) for p in box_raw]
        ys = [float(p[1]) for p in box_raw]
    except (TypeError, ValueError, IndexError):
        return None
    if not xs or not ys:
        return None
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    # 归一化（钳制到 [0,1]，避免越界坐标导致 OcrBox 构造失败）
    nx = max(0.0, min(1.0, x_min / width))
    ny = max(0.0, min(1.0, y_min / height))
    nw = max(0.0, min(1.0, (x_max - x_min) / width))
    nh = max(0.0, min(1.0, (y_max - y_min) / height))
    if nw <= 0.0 or nh <= 0.0:
        return None  # 退化框（零面积），跳过
    return OcrBox(x=nx, y=ny, w=nw, h=nh)
