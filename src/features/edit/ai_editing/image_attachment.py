"""多模态附件 — 让 AI 助手能看图、读文件和理解音视频。

用户可以给一条聊天消息附带一张图片（粘贴的截图，或从视口抓取的当前帧）。
图片被编码为 JPEG data URL，打包进 OpenAI 多模态 ``content`` 数组，使具备
视觉理解能力的聊天模型（如 GLM-4.5V / GLM-5.2）能"看到"它。

设计要点：
- 图片被限制到最长边 ~1024px 并 JPEG 编码，控制 token 成本与请求体体积。
- :class:`ImageAttachment` 是冻结值对象，只携带 data URL + mime + 尺寸 + 来源
  标签，**不含绝对路径**。
- :class:`FileAttachment` 只在本次请求的内存链路中携带本地路径；持久化摘要只保留
  文件名和 MIME，不保存路径或文件字节。
- :func:`message_content` 把 ``text + attachments`` 变成供应商无关的中间
  ``content-parts``，文件上传和 ``file_id`` 替换由 Responses transport 完成。
- :func:`extract_text_from_content` 从 str 或 list 内容里取回纯文本（用于压缩
  /持久化/展示）。

安全（TAEP §12）：图片字节是不可信数据，只作为 content part 里的 data URL
传输，不能改变工具权限或系统提示。data URL 很大，禁止在 INFO 日志里整体打印。
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, TypeAlias

logger = logging.getLogger(__name__)

# 最长边上限（px）。OpenAI 推荐 ~1568px；这里取 1024 平衡清晰度与成本。
DEFAULT_MAX_LONG_EDGE = 1024
DEFAULT_JPEG_QUALITY = 85
# 单张图片的粗略 token 估算（OpenAI high-detail ~765/1024²）。仅用于预算估算，
# 避免按 base64 字符数灾难性高估。
IMAGE_TOKEN_ESTIMATE = 765
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True)
class ImageAttachment:
    """一条附在聊天消息上的图片（已编码为 data URL）。

    Attributes:
        data_url: ``data:image/jpeg;base64,...``（或 png）。
        mime: MIME 类型，如 ``"image/jpeg"``。
        width / height: 编码后的像素尺寸（已降采样）。
        source: 来源标签（``"paste"`` / ``"frame_grab"``），仅用于 UI 展示，
            **不参与权限判断**。
    """

    data_url: str
    mime: str = "image/jpeg"
    width: int = 0
    height: int = 0
    source: str = "user"

    def to_dict(self) -> dict[str, Any]:
        return {
            "mime": self.mime,
            "width": self.width,
            "height": self.height,
            "source": self.source,
            # 注意：不含 data_url（过大，且属不可信数据）。仅记录元信息。
        }


@dataclass(frozen=True)
class FileAttachment:
    """一条附在聊天消息上的本地文件。

    ``path`` 只用于当前请求上传，绝不写入会话摘要。``media_kind`` 可显式指定
    ``video`` / ``audio`` / ``file``；为空时根据 MIME 自动推断。
    """

    path: str
    mime: str = "application/octet-stream"
    filename: str = ""
    size_bytes: int = 0
    source: str = "file"
    media_kind: str = ""

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError("file attachment path cannot be empty")
        if not self.filename:
            object.__setattr__(self, "filename", Path(self.path).name or "attachment")
        if self.size_bytes < 0:
            raise ValueError("file attachment size cannot be negative")

    @property
    def input_kind(self) -> str:
        """Responses content kind: video/audio/file."""
        explicit = str(self.media_kind or "").lower().strip()
        if explicit in {"video", "audio", "file"}:
            return explicit
        mime = str(self.mime or "").lower()
        if mime.startswith("video/"):
            return "video"
        if mime.startswith("audio/"):
            return "audio"
        return "file"

    def to_dict(self) -> dict[str, Any]:
        # 本地绝对路径和文件字节不能进入会话持久化层。
        return {
            "filename": self.filename,
            "mime": self.mime,
            "size_bytes": self.size_bytes,
            "source": self.source,
            "media_kind": self.input_kind,
        }


ChatAttachment: TypeAlias = ImageAttachment | FileAttachment


# ---------------------------------------------------------------------- #
# 编码：QImage / 原始字节 → data URL
# ---------------------------------------------------------------------- #


def encode_bytes_to_data_url(
    data: bytes, *, mime: str = "image/jpeg"
) -> str:
    """把原始图片字节包成 data URL。"""
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def encode_qimage_to_data_url(
    qimage: Any,
    *,
    max_long_edge: int = DEFAULT_MAX_LONG_EDGE,
    quality: int = DEFAULT_JPEG_QUALITY,
) -> tuple[str, int, int]:
    """把 ``QImage`` 降采样并 JPEG 编码为 data URL。

    返回 ``(data_url, width, height)``。Qt 在函数内部惰性导入，避免模块导入
    时强依赖 PyQt6（便于纯逻辑测试）。
    """
    from PyQt6.QtCore import QBuffer, QIODevice, Qt

    if qimage is None or qimage.isNull():
        raise ValueError("cannot encode a null image")

    w, h = qimage.width(), qimage.height()
    long_edge = max(w, h)
    if long_edge > max_long_edge and long_edge > 0:
        scale = max_long_edge / long_edge
        nw = max(1, int(round(w * scale)))
        nh = max(1, int(round(h * scale)))
        qimage = qimage.scaled(
            nw, nh,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        w, h = qimage.width(), qimage.height()

    # JPEG 不支持 alpha；统一转 RGB888 以避免编码异常/黑底。
    qimage = qimage.convertToFormat(qimage.Format.Format_RGB888)

    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    try:
        ok = qimage.save(buf, "JPEG", quality)
    finally:
        buf.close()
    if not ok:
        raise ValueError("JPEG encode failed")
    raw = bytes(buf.data())
    if not raw:
        raise ValueError("empty JPEG buffer")
    url = encode_bytes_to_data_url(raw, mime="image/jpeg")
    return url, w, h


def attachment_from_qimage(
    qimage: Any,
    *,
    source: str = "user",
    max_long_edge: int = DEFAULT_MAX_LONG_EDGE,
    quality: int = DEFAULT_JPEG_QUALITY,
) -> ImageAttachment:
    """便捷构造：QImage → ImageAttachment。"""
    url, w, h = encode_qimage_to_data_url(
        qimage, max_long_edge=max_long_edge, quality=quality
    )
    return ImageAttachment(data_url=url, mime="image/jpeg", width=w, height=h, source=source)


# ---------------------------------------------------------------------- #
# OpenAI content-parts 构造与解析
# ---------------------------------------------------------------------- #


def message_content(
    text: str | None, attachments: Iterable[ChatAttachment] | None
) -> str | list[dict[str, Any]]:
    """构造 OpenAI message 的 ``content``。

    无附件 → 返回纯字符串（与现有纯文本管线完全兼容）。
    有附件 → 返回供应商无关的 ``content-parts`` 列表；文件 part 使用本地路径
    作为 transport 的一次性上传输入，之后会被替换成 ``file_id``。
    """
    atts = tuple(attachments or ())
    if not atts:
        return text or ""
    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"type": "text", "text": text})
    for att in atts:
        if isinstance(att, ImageAttachment):
            parts.append({"type": "image_url", "image_url": {"url": att.data_url}})
        elif isinstance(att, FileAttachment):
            parts.append({
                "type": "file",
                "file_path": att.path,
                "filename": att.filename,
                "mime": att.mime,
                "media_kind": att.input_kind,
            })
    return parts


def has_image_content(content: Any) -> bool:
    """该 message content 是否含图片（即多模态）。"""
    if isinstance(content, list):
        return any(
            isinstance(p, dict) and p.get("type") == "image_url" for p in content
        )
    return False


def extract_text_from_content(content: Any) -> str:
    """从 str 或 content-parts 列表中取回纯文本（用于压缩 / 持久化 / 展示）。

    多模态列表里拼接文本，并为文件保留一个轻量占位符；图片被丢弃。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                t = part.get("text", "")
                if t:
                    texts.append(str(t))
            elif isinstance(part, dict) and part.get("type") == "file":
                name = str(part.get("filename") or "附件")
                texts.append(f"[附件：{name}]")
        return "\n".join(texts)
    return str(content)


def count_files_in_content(content: Any) -> int:
    """统计文件附件数量（不包含图片）。"""
    if not isinstance(content, list):
        return 0
    return sum(1 for p in content if isinstance(p, dict) and p.get("type") == "file")


def count_images_in_content(content: Any) -> int:
    """统计 content 里图片 part 的数量（用于 token 估算）。"""
    if not isinstance(content, list):
        return 0
    return sum(1 for p in content if isinstance(p, dict) and p.get("type") == "image_url")


def estimate_image_tokens(content: Any) -> int:
    """估算 content 中图片占用的 token（每张按固定常数，避免按 base64 字符高估）。"""
    return count_images_in_content(content) * IMAGE_TOKEN_ESTIMATE
