"""真实 face_recognize provider —— InsightFace（人脸检测 + 识别）。

用 InsightFace 的 buffalo_l 模型（ONNX 推理，CPU 也能跑）。
检测人脸、提取 512 维向量、判断角度/完整度。

降级安全：insightface 未装 → is_available()=False → stub 兜底（返回空人脸）。
"""

from __future__ import annotations

import logging
from typing import Any

from apps.topos_vault.providers.base import Capability, ProviderDescriptor, get_registry
from apps.topos_vault.providers.contracts import FaceResult, SingleFace
from apps.topos_vault.providers.model_loader import LazyModelLoader

logger = logging.getLogger(__name__)


class InsightFaceProvider(LazyModelLoader):
    """InsightFace 人脸识别 provider。"""

    provider_id = "insightface_buffalo_l"
    capability = Capability.FACE_RECOGNIZE

    MODEL_PACK_ID = "insightface_buffalo_l"
    DIM = 512

    def __init__(self):
        super().__init__(provider_id=self.provider_id, model_pack_id=self.MODEL_PACK_ID)

    def _check_dependencies(self) -> bool:
        try:
            import insightface  # noqa: F401
            return True
        except ImportError:
            return False

    def _load_model(self) -> Any:
        """加载 InsightFace FaceAnalysis。"""
        from insightface.app import FaceAnalysis

        # 设置模型存放根目录：优先本地 pack 目录，否则用 InsightFace 默认 (~/.insightface)
        import os
        root = None
        try:
            from apps.topos_vault.model_specs import get_spec
            spec = get_spec(self.MODEL_PACK_ID)
            if spec is not None and spec.is_installed:
                root = str(spec.install_dir)
                os.environ["INSIGHTFACE_HOME"] = root
        except Exception as e:
            logger.debug("InsightFace 模型路径检查失败：%s", e)

        # device 解析：有 GPU 用 CUDAExecutionProvider，否则 CPU
        from apps.topos_vault.providers.model_loader import is_gpu_available
        providers = (["CUDAExecutionProvider", "CPUExecutionProvider"]
                     if is_gpu_available() else ["CPUExecutionProvider"])
        ctx_id = 0 if is_gpu_available() else -1

        logger.info("加载 InsightFace buffalo_l（providers=%s）...", providers)
        app = FaceAnalysis(name="buffalo_l", providers=providers)
        app.prepare(ctx_id=ctx_id, det_size=(640, 640))
        logger.info("InsightFace 加载完成")
        return app

    def detect_faces(self, image_path: str) -> FaceResult:
        """检测图片中的人脸。"""
        if self.model is None:
            raise RuntimeError(f"provider {self.provider_id} 模型未加载")

        import cv2
        import numpy as np

        img = cv2.imread(image_path)
        if img is None:
            return FaceResult(faces=[], model=self.provider_id)

        faces_data = self.model.get(img)

        faces: list[SingleFace] = []
        for face in faces_data:
            bbox = np.asarray(face.bbox).astype(int)  # [x1, y1, x2, y2]
            w = bbox[2] - bbox[0]
            h = bbox[3] - bbox[1]

            # 人脸角度（基于 landmarks 估算）
            face_angle = _estimate_angle(face)

            # 完整度（基于 det_score）
            det_score = float(face.det_score)
            completeness = "完整" if det_score > 0.5 else "遮挡"

            # normed_embedding 可能是 numpy/torch，统一转 list[float]
            emb = face.normed_embedding
            emb_list = emb.tolist() if hasattr(emb, "tolist") else [float(x) for x in emb]

            faces.append(SingleFace(
                bbox=(int(bbox[0]), int(bbox[1]), int(w), int(h)),
                embedding=emb_list,
                confidence=det_score,
                face_angle=face_angle,
                completeness=completeness,
            ))

        return FaceResult(faces=faces, model=self.provider_id)


def _estimate_angle(face) -> str:
    """根据 landmarks 估算人脸角度。"""
    if not hasattr(face, "kps") or face.kps is None:
        return "正面"

    # kps 是 5 个关键点：左眼、右眼、鼻尖、左嘴角、右嘴角
    kps = face.kps
    left_eye = kps[0]
    right_eye = kps[1]

    # 两眼水平偏移 → 判断侧脸
    eye_dx = right_eye[0] - left_eye[0]
    eye_dy = right_eye[1] - left_eye[1]
    angle = abs(eye_dy / (eye_dx + 1e-6))  # tan(倾斜角)

    if angle > 0.5:
        return "侧脸"
    return "正面"


def register_real_face_recognize() -> None:
    """注册真实的 face_recognize provider。"""
    reg = get_registry()
    if reg.get("insightface_buffalo_l") is None:
        reg.register(
            InsightFaceProvider(),
            ProviderDescriptor(
                provider_id="insightface_buffalo_l",
                capability=Capability.FACE_RECOGNIZE,
                display_name="InsightFace buffalo_l",
                requires_gpu=False, min_vram_mb=0,
                model_pack_id="insightface_buffalo_l",
                notes="2026 金标准。ONNX 推理，CPU 也能跑。512 维人脸向量。",
            ),
            is_default=True,
        )
