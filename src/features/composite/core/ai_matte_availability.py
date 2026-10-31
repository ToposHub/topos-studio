"""M23.2 · 人脸 detector / ML 能力可用性探测（UI 呈现数据源）。

按真实模型可用性给出结构化状态：detector 未注入 = NEEDS_DETECTOR
（FaceTrackNode.detection_state 同词表）、模型文件缺失 = MODEL_MISSING、
可用 = AVAILABLE。UI/节点面板据此呈现"不可用"而不是运行时失败或
透传冒充检测成功。
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from src.features.composite.nodes.face_track_node import (
    current_face_detector,
)

STATUS_AVAILABLE = "AVAILABLE"
STATUS_NEEDS_DETECTOR = "NEEDS_DETECTOR"
STATUS_MODEL_MISSING = "MODEL_MISSING"


def face_detector_status() -> Dict[str, Any]:
    """人脸 detector 可用性（进程级注入探测，只读）。"""
    detector = current_face_detector()
    if detector is None:
        return {
            "status": STATUS_NEEDS_DETECTOR,
            "detail": "人脸 detector 未注入（模型/许可条件未满足）——"
                      "FaceTrackNode 分析入口将标注 NEEDS_DETECTOR，"
                      "透传图像不冒充检测成功",
            "engine": None,
        }
    return {
        "status": STATUS_AVAILABLE,
        "detail": "detector 已注入",
        "engine": type(detector).__name__,
    }


def model_file_status(
    model_path: Optional[str],
    *,
    label: str = "ML 模型",
) -> Dict[str, Any]:
    """模型文件存在性探测（按显式路径；None=未配置）。"""
    if not model_path:
        return {
            "status": STATUS_MODEL_MISSING,
            "detail": f"{label}路径未配置",
            "path": None,
        }
    if not os.path.isfile(model_path):
        return {
            "status": STATUS_MODEL_MISSING,
            "detail": f"{label}文件缺失：{model_path}",
            "path": model_path,
        }
    return {
        "status": STATUS_AVAILABLE,
        "detail": f"{label}在位（{os.path.getsize(model_path)} 字节）",
        "path": model_path,
    }


__all__ = [
    "STATUS_AVAILABLE",
    "STATUS_NEEDS_DETECTOR",
    "STATUS_MODEL_MISSING",
    "face_detector_status",
    "model_file_status",
]
