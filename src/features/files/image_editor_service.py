"""G6：image_editor 接管——独立图片编辑 command（安全输出与写入事务）。

G 节行验收：**预览/导出一致、undo、默认安全输出与写入事务，不能照搬
直接写原图接口**。

* **预览/导出一致**：操作栈（旋转/翻转/明暗）经同一 ``_render`` 管线
  出像素——预览就是导出结果（同函数渲染，无第二套变换）；
* **undo**：操作栈后进先出，``undo`` 弹栈重渲；
* **默认安全输出**：导出目标默认 ``<stem>-edited<ext>``（冲突自动
  -1/-2），**源文件绝不作为写目标**（``save_to(path == source)`` 直接
  拒绝——"不能照搬直接写原图接口"的域层强制）；
* **写入事务**：``.part`` + fsync + ``os.replace`` 原子交付；导出前
  **源快照再验**（源被外部改动即拒绝——不留"读了半张图"的导出）。
"""
from __future__ import annotations

import os
import tempfile
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from src.features.files.format_factory_service import InputSnapshot

OPERATIONS = ("rotate-90", "rotate-270", "flip-h", "flip-v",
              "brightness+10", "brightness-10")


class ImageEditorError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _render(image: Any, operations: tuple[str, ...]) -> Any:
    """唯一渲染管线：预览与导出共用（一致性由构造保证）。"""
    from PIL import Image, ImageEnhance

    for op in operations:
        if op == "rotate-90":
            image = image.transpose(Image.Transpose.ROTATE_90)
        elif op == "rotate-270":
            image = image.transpose(Image.Transpose.ROTATE_270)
        elif op == "flip-h":
            image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        elif op == "flip-v":
            image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        elif op.startswith("brightness"):
            delta = int(op.split("+")[-1].split("-")[-1] or 0) * (
                -1 if op.startswith("brightness-") else 1)
            factor = max(0.1, 1.0 + delta / 100.0)
            image = ImageEnhance.Brightness(image).enhance(factor)
    return image


@dataclass(frozen=True)
class EditorState:
    source: InputSnapshot
    operations: tuple[str, ...] = ()

    def apply(self, operation: str) -> "EditorState":
        if operation not in OPERATIONS:
            raise ValueError(f"未知操作：{operation}")
        return replace(self, operations=(*self.operations, operation))

    def undo(self) -> "EditorState":
        if not self.operations:
            return self
        return replace(self, operations=self.operations[:-1])


def default_output_path(source: str, *, exists: Any = os.path.exists) -> str:
    """默认安全输出：<stem>-edited<ext>（冲突 -1/-2；绝不是源路径）。"""
    path = Path(source)
    suffix = path.suffix or ".png"
    candidate = str(path.with_name(f"{path.stem}-edited{suffix}"))
    index = 1
    while exists(candidate):
        candidate = str(path.with_name(f"{path.stem}-edited-{index}{suffix}"))
        index += 1
    return candidate


class ImageEditorService:
    """图片编辑域服务：状态机（apply/undo）+ 一致渲染 + 事务导出。"""

    def __init__(self, source_path: str) -> None:
        self._source_path = str(source_path)
        self._state = EditorState(source=InputSnapshot.capture(self._source_path))
        self._image: Any = None
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def _check_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise ImageEditorError("CANCELLED", "图片编辑已关闭，操作已取消")

    @property
    def state(self) -> EditorState:
        return self._state

    @property
    def source_path(self) -> str:
        return self._source_path

    def _load_source(self) -> Any:
        from PIL import Image

        self._check_cancelled()
        if self._image is None:
            if not self._state.source.matches_disk():
                raise ImageEditorError("SOURCE_DRIFT", "源文件已变化，请重新打开")
            with Image.open(self._source_path) as source:
                if source.width * source.height > 80_000_000:
                    raise ImageEditorError("IMAGE_TOO_LARGE", "图片超过编辑器 8000 万像素预算")
                source.load()
                self._image = source.copy()
        return self._image

    def apply(self, operation: str) -> EditorState:
        self._state = self._state.apply(operation)
        return self._state

    def undo(self) -> EditorState:
        self._state = self._state.undo()
        return self._state

    def render(self) -> Any:
        """预览=导出：唯一管线的当前结果（PIL Image）。"""
        result = _render(self._load_source(), self._state.operations)
        self._check_cancelled()
        return result

    def export(self, target: str | None = None, *,
               format_hint: str | None = None) -> str:
        """事务导出：默认安全输出；源快照再验；.part+fsync 原子交付。"""
        if not self._state.source.matches_disk():
            raise ImageEditorError(
                "SOURCE_DRIFT", "源文件已被外部修改——请重新打开后再导出")
        if target is None:
            target = default_output_path(self._source_path)
        if os.path.abspath(target) == os.path.abspath(self._source_path):
            raise ImageEditorError(
                "OVERWRITE_SOURCE", "不允许写回原图——默认输出为 -edited 副本")
        if os.path.lexists(target):
            raise ImageEditorError("OUTPUT_EXISTS", "目标已存在，请选择新的输出文件")
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(prefix=".image-edit-", suffix=".part",
                                             dir=Path(target).parent, delete=False)
        part = handle.name
        handle.close()
        result = self.render()                    # 与预览同一管线
        try:
            kwargs: dict[str, Any] = {}
            fmt = (format_hint or Path(target).suffix.lstrip(".") or "PNG").upper()
            if fmt in ("JPG", "JPEG"):
                fmt = "JPEG"
                kwargs["quality"] = 95
            if fmt == "JPEG" and result.mode not in ("RGB", "L"):
                result = result.convert("RGB")
            icc = self._load_source().info.get("icc_profile")
            if icc:
                kwargs["icc_profile"] = icc
            result.save(part, format=fmt, **kwargs)
            with open(part, "rb+") as handle:   # fsync 落盘后再改名
                handle.flush()
                os.fsync(handle.fileno())
            if not self._state.source.matches_disk():
                raise ImageEditorError("SOURCE_DRIFT", "源文件在导出期间已变化，请重新打开")
            self._check_cancelled()
            os.link(part, target)   # atomic no-replace: competing output wins safely
        except (OSError, ValueError) as exc:
            raise ImageEditorError("IO_ERROR", f"导出失败（无半成品）：{exc}") from exc
        finally:
            Path(part).unlink(missing_ok=True)
        return target


__all__ = ["ImageEditorService", "ImageEditorError", "EditorState",
           "OPERATIONS", "default_output_path"]
