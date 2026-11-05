"""Presets 静帧的 Gallery 元数据与图像 sidecar（P1-COLOR-04，并入 Presets）。

DaVinci Gallery 语义合并进 Presets 面板：每条预设不再只是 .topos 调色包
+ 缩略图，还带两个可选 sidecar：

    {name}.topos       调色工程包（既有）
    {name}.thumb.jpg   512px 缩略图（既有）
    {name}.still.jpg   参考帧（最长边 1920，供参考 wipe 对比）—— 新增
    {name}.meta.json   Gallery 元数据（来源 clip / 帧号 / 色彩空间 / 时间）—— 新增

旧预设没有 sidecar 时功能照常（元数据缺省、参考帧回退缩略图）；
rename/delete 由面板同步搬移/清理 sidecar。
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

# F4.3：EXR 参考帧依赖 cv2 的 OpenEXR codec——必须在 import cv2 前
# 置环境开关（main.py/file_io.py 同模式；后置无效）。
os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")
import cv2  # noqa: E402
import numpy as np

#: 参考帧/缩略图的最长边上限
STILL_IMAGE_MAX = 1920
THUMB_MAX = 512

_META_FIELDS = (
    "display_name", "clip_id", "asset_id", "timeline_frame",
    "source_frame", "source_transfer", "source_primaries",
    "still_precision", "working_space", "display_transform", "source_range",
    "created_at",
)


def _jsonify(obj: Any) -> Any:
    """递归转换为 JSON 可序列化类型（ndarray → 嵌套列表）。"""
    if isinstance(obj, dict):
        return {k: _jsonify(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonify(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    return obj


@dataclass
class PresetMeta:
    """一条预设的 Gallery 元数据（sidecar {name}.meta.json）。"""
    display_name: Optional[str] = None      # 来源片段名（如 A001_C001.mov）
    clip_id: Optional[str] = None
    asset_id: Optional[str] = None
    timeline_frame: Optional[int] = None
    source_frame: Optional[int] = None
    source_transfer: Optional[str] = None   # pq / hlg / ...
    source_primaries: Optional[str] = None  # bt2020 / p3d65 / ...
    source_range: Optional[str] = None      # limited / full
    created_at: Optional[str] = None        # ISO 8601
    # F4.3 schema 2：高精度参考声明（None = 旧 8-bit JPEG，精度受限）
    still_precision: Optional[str] = None   # "float32" / "half" / None=legacy
    working_space: Optional[str] = None     # 如 "scene-linear bt709"
    display_transform: Optional[dict] = None  # 锁定的 ODT 描述（锁定显示变换）

    def describe(self) -> str:
        """工具提示摘要：名称 + 来源 clip + 帧号 + source tag。"""
        parts = []
        if self.display_name:
            parts.append(str(self.display_name))
        if self.source_frame is not None:
            parts.append(f"frame {self.source_frame}")
        if self.source_primaries or self.source_transfer:
            parts.append(f"{self.source_primaries or ''} "
                         f"{self.source_transfer or ''}".strip())
        if self.created_at:
            parts.append(self.created_at)
        if self.still_precision:
            parts.append(f"HDR still ({self.still_precision})")
        else:
            parts.append("8-bit JPEG（精度受限参考，非 HDR）")
        return " | ".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "PresetMeta":
        kwargs = {k: v for k, v in (data or {}).items() if k in _META_FIELDS}
        return PresetMeta(**kwargs)


# ── 路径约定 ─────────────────────────────────────────────────────────

def _sibling(topos_path: str, suffix: str) -> str:
    base = os.path.splitext(topos_path)[0]
    return f"{base}{suffix}"


def meta_path_for(topos_path: str) -> str:
    return _sibling(topos_path, ".meta.json")


def still_path_for(topos_path: str) -> str:
    return _sibling(topos_path, ".still.jpg")


def thumb_path_for(topos_path: str) -> str:
    return _sibling(topos_path, ".thumb.jpg")


def still_exr_path_for(topos_path: str) -> str:
    """F4.3: 高精度参考帧（float32 EXR sidecar）。"""
    return _sibling(topos_path, ".still.exr")


# ── 元数据 sidecar ───────────────────────────────────────────────────

def save_preset_meta(topos_path: str, meta: PresetMeta) -> bool:
    """写 {name}.meta.json；失败不抛（静帧抓取不因此中断）。"""
    try:
        payload = {"schema": 1, **_jsonify(meta.to_dict())}
        with open(meta_path_for(topos_path), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        return True
    except Exception:
        return False


def load_preset_meta(topos_path: str) -> Optional[PresetMeta]:
    """读 sidecar；缺失/损坏返回 None（旧预设完全兼容）。"""
    path = meta_path_for(topos_path)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict):
            return None
        return PresetMeta.from_dict(payload)
    except Exception:
        return None


# ── 图像 sidecar ─────────────────────────────────────────────────────

def _to_uint8_bgr(image: np.ndarray) -> np.ndarray:
    if image.dtype in (np.float32, np.float64):
        img = (np.clip(image, 0.0, 1.0) * 255.0).astype(np.uint8)
    else:
        img = image.copy()
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    return img


def _resize_max(img: np.ndarray, max_side: int) -> np.ndarray:
    h, w = img.shape[:2]
    if max(h, w) <= max_side:
        return img
    scale = max_side / max(h, w)
    return cv2.resize(img, (int(w * scale), int(h * scale)),
                      interpolation=cv2.INTER_AREA)


def _write_jpeg(img: np.ndarray, path: str, quality: int) -> bool:
    try:
        ok, encoded = cv2.imencode(
            '.jpg', img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        if ok:
            encoded.tofile(path)
        return bool(ok)
    except Exception:
        return False


def _alpha_declaration_path(topos_path: str) -> str:
    """M09.3 · EXR 侧车的 alpha 语义声明文件（JSON）。"""
    return still_exr_path_for(topos_path) + ".json"


def alpha_semantics_for(float_img: np.ndarray) -> str:
    """M09.3 · 图像的 alpha 语义声明。

    3 通道 → ``"none"``；4 通道 → ``"straight"``（渲染线程 FBO f4 读回
    为直通 alpha——预乘只发生在 GPU 混合时，读回点在混合后未再预乘；
    声明值由抓帧侧写入 sidecar，消费侧不猜）。
    """
    if float_img is None or getattr(float_img, "ndim", 0) != 3:
        return "none"
    return "straight" if int(float_img.shape[2]) == 4 else "none"


def premultiply_rgba(img: np.ndarray) -> np.ndarray:
    """straight → premultiplied（alpha=0 处 RGB 置零，比较域统一）。"""
    out = np.asarray(img, dtype=np.float32).copy()
    if out.ndim == 3 and out.shape[2] == 4:
        a = out[..., 3:4]
        out[..., :3] = out[..., :3] * a
    return out


def unpremultiply_rgba(img: np.ndarray) -> np.ndarray:
    """premultiplied → straight（alpha=0 像素 RGB 保持零——无信息可恢复，
    不猜色；仅 alpha>0 处除回）。"""
    out = np.asarray(img, dtype=np.float32).copy()
    if out.ndim == 3 and out.shape[2] == 4:
        a = out[..., 3:4]
        safe = np.where(a > 1e-6, a, 1.0)
        out[..., :3] = out[..., :3] / safe
    return out


def write_still_exr(topos_path: str, float_img: np.ndarray,
                    *, alpha_semantics: Optional[str] = None,
                    ) -> Optional[str]:
    """F4.3+M09.3: 写高精度参考帧（float32 EXR；保留 >1.0/负值/渐变）。

    输入 float32/float64（任意数值范围，不 clip）；BGR(A) 约定与
    ``load_baked_layer``/``set_reference_image`` float 分支一致。写失败
    返回 None（调用方仍保留 JPEG 路径，不因 EXR 失败丢预设）。

    M09.3：``alpha_semantics`` 声明（缺省经 :func:`alpha_semantics_for`
    从通道数推导）写入 ``.still.exr.json`` 声明 sidecar——四通道
    straight/premultiplied 不再靠消费方猜；声明写失败不丢 EXR（读取侧
    按 undeclared 处理）。
    """
    if float_img is None or float_img.size == 0:
        return None
    try:
        img = np.asarray(float_img, dtype=np.float32)
        if img.ndim != 3 or img.shape[2] not in (3, 4):
            return None
        path = still_exr_path_for(topos_path)
        # 压缩方式必须是 PIZ（不可 ZIP/ZIPS/PXR24/DWAA/DWAB）：opencv-python
        # 5.0.0.93 wheel 携带的 OpenEXRCore 对 ZIP 族压缩块走捆绑 libdeflate
        # （exr_(un)compress_buffer → libdeflate_zlib_(de)compress_ex → 经
        # 可写函数指针 _adler32_impl 分派），在本机（Alder Lake 黑苹果，
        # libdeflate 的 CPUID 探测跨进程不一致）上该指针被写坏后 `jmp *`
        # 落到映像外 → EXC_BAD_INSTRUCTION(SIGILL)，编码/解码两侧均复现
        # （200 次循环 12 进程：ZIP 7/12 与 3/12 崩溃；PIZ/RLE/NONE 0/12）。
        # 反汇编确认 undo/apply_piz、undo/apply_rle 完全不触碰 libdeflate；
        # PIZ 压缩率与 ZIP 相当（1080p f32 实测 12.8MB vs 原始 24.9MB），
        # 且无损——HDR 值/负值/渐变语义不变。
        ok, encoded = cv2.imencode(
            ".exr", img,
            [int(cv2.IMWRITE_EXR_TYPE), int(cv2.IMWRITE_EXR_TYPE_FLOAT),
             int(cv2.IMWRITE_EXR_COMPRESSION), int(
                 cv2.IMWRITE_EXR_COMPRESSION_PIZ)])
        if not ok:
            return None
        encoded.tofile(path)
        try:
            import json as _json
            semantics = (alpha_semantics if alpha_semantics in (
                "none", "straight", "premultiplied")
                else alpha_semantics_for(img))
            with open(_alpha_declaration_path(topos_path), "w",
                      encoding="utf-8") as fh:
                _json.dump({
                    "alpha_semantics": semantics,
                    "channels": int(img.shape[2]),
                    "precision": "float32",
                }, fh)
        except OSError:
            pass  # 声明失败不丢 EXR（读取侧 undeclared）
        return path
    except Exception:
        return None


def write_still_images(topos_path: str, rendered_img: np.ndarray,
                       *, thumb_only: bool = False,
                       hdr_image: Optional[np.ndarray] = None,
                       ) -> Tuple[Optional[str], Optional[str]]:
    """写参考帧与缩略图 sidecar，返回 (thumb_path, still_path)。

    Args:
        thumb_only: 只写缩略图（旧行为，兼容旧调用方）。
        hdr_image: M09.2——渲染线程 float 读回的 BGR float 帧；提供时
            额外写 ``.still.exr`` 侧车（JPEG 仍写，供低精度消费方），
            HDR 值经 :func:`load_still_best` 优先回到参考/Color Match。
    """
    if rendered_img is None or rendered_img.size == 0:
        return None, None
    full = _to_uint8_bgr(rendered_img)

    thumb_path = thumb_path_for(topos_path)
    thumb = _resize_max(full, THUMB_MAX)
    thumb_ok = _write_jpeg(thumb, thumb_path, 85)

    still_path: Optional[str] = None
    if not thumb_only:
        still_path = still_path_for(topos_path)
        if not _write_jpeg(_resize_max(full, STILL_IMAGE_MAX),
                           still_path, 90):
            still_path = None
        if hdr_image is not None:
            write_still_exr(topos_path, hdr_image)

    return (thumb_path if thumb_ok else None), still_path


def _exr_compression_code(path: str) -> Optional[int]:
    """解析 EXR 头部的 compression 属性字节（0=NO 1=RLE 2=ZIPS 3=ZIP
    4=PIZ 5=PXR24 8/9=DWAA/DWAB）；非 EXR / 头损坏 / 未找到返回 None。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return None
    if len(head) < 8 or head[:4] != b"\x76\x2f\x31\x01":
        return None
    pos = 8
    while pos < len(head):
        z = head.find(b"\x00", pos)
        if z < 0:
            return None
        name = head[pos:z].decode("latin-1", "replace")
        pos = z + 1
        if name == "":  # 头结束仍未见 compression → 异常头
            return None
        z = head.find(b"\x00", pos)
        if z < 0:
            return None
        typ = head[pos:z]
        pos = z + 1
        if pos + 4 > len(head):
            return None
        size = int.from_bytes(head[pos:pos + 4], "little")
        pos += 4
        if name == "compression" and typ == b"compression" and size >= 1:
            return head[pos]
        pos += size
    return None


#: ZIP 族压缩：OpenEXRCore 经捆绑 libdeflate 解压（undo_zip/undo_pxr24
#: 调 exr_uncompress_buffer），编码解码皆可能触发 wheel 捆绑 libdeflate 的
#: _adler32_impl 分派 SIGILL（见 write_still_exr 注释）——读取侧对这类
#: 文件必须进程隔离解码，崩溃只损失本侧车（返回 None 走 JPEG 回退），
#: 不再击落宿主进程。PIZ/RLE/NONE 为 OpenEXRCore 自带实现，进程内安全。
_EXR_DEFLATE_COMPRESSION = frozenset({2, 3, 5, 8, 9})

_EXR_CHILD_DECODE_SRC = (
    "import os, sys\n"
    "os.environ.setdefault('OPENCV_IO_ENABLE_OPENEXR', '1')\n"
    "import cv2, numpy as np\n"
    "arr = cv2.imread(sys.argv[1], cv2.IMREAD_UNCHANGED)\n"
    "if arr is None:\n"
    "    sys.stdout.buffer.write(b'FAIL\\n')\n"
    "    sys.exit(0)\n"
    "buf = np.ascontiguousarray(arr, dtype=np.float32)\n"
    "shape = ' '.join(str(d) for d in buf.shape)\n"
    "sys.stdout.buffer.write(\n"
    "    ('OK ' + shape + '\\n').encode() + buf.tobytes())\n"
)


def _decode_exr_isolated(path: str) -> Optional[np.ndarray]:
    """在子进程中解码 EXR（崩溃隔离）：子进程 SIGILL/超时返回 None。

    子进程经管道回传 ``OK <shape>\\n<raw float32 bytes>``；不能用
    ``np.save(sys.stdout.buffer, ...)``——numpy 的 tofile 需可 seek 的
    fd，管道不支持（OSError: obtaining file position failed）。
    """
    import subprocess
    import sys
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-c", _EXR_CHILD_DECODE_SRC, path],
            capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    nl = proc.stdout.find(b"\n")
    head = proc.stdout[:nl].split() if nl >= 0 else []
    if not head or head[0] != b"OK" or len(head) < 2:
        return None
    try:
        shape = tuple(int(d) for d in head[1:])
        arr = np.frombuffer(proc.stdout[nl + 1:], dtype=np.float32)
        if arr.size != int(np.prod(shape)) or arr.size == 0:
            return None
        return arr.reshape(shape).copy()
    except (ValueError, TypeError):
        return None


def load_still_exr(topos_path: str) -> Optional[np.ndarray]:
    """读高精度参考帧（float32；缺文件/损坏返回 None）。

    本函数写入的侧车为 PIZ 压缩（进程内解码安全）；旧版本写的 ZIP 侧车
    或外来 EXR 若属 ZIP 族压缩，经子进程隔离解码（见 _EXR_DEFLATE_
    COMPRESSION 注释），解码失败按缺文件处理（调用方回退 JPEG）。
    """
    path = still_exr_path_for(topos_path)
    if not os.path.isfile(path):
        return None
    try:
        comp = _exr_compression_code(path)
        if comp is None or comp in _EXR_DEFLATE_COMPRESSION:
            return _decode_exr_isolated(path)
        img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if img is None:
            return None
        return np.asarray(img, dtype=np.float32)
    except Exception:
        return None


def load_still_image(topos_path: str) -> Optional[np.ndarray]:
    """读取预设的参考帧：still.jpg 优先，缺失回退缩略图。"""
    for path in (still_path_for(topos_path), thumb_path_for(topos_path)):
        if os.path.isfile(path):
            img = cv2.imread(path, cv2.IMREAD_COLOR)
            if img is not None:
                return img
    return None


def load_still_best(topos_path: str) -> "tuple[Optional[np.ndarray], str]":
    """M09（S12）· 参考帧最佳精度加载：float32 EXR 优先，JPEG 回退。

    返回 ``(image, precision)``——precision ∈ {"exr_float32", "jpeg8",
    "none"}。EXR 保留 >1.0/负值（HDR 参考与 scope 消费 float 域），
    JPEG 回退时调用方应向用户明示精度降级（不静默）。
    """
    exr = load_still_exr(topos_path)
    if exr is not None:
        return exr, "exr_float32"
    img = load_still_image(topos_path)
    if img is not None:
        return img, "jpeg8"
    return None, "none"


def still_alpha_declaration(topos_path: str) -> str:
    """M09.3 · 读 EXR 声明 sidecar 的 alpha 语义。

    返回 "straight" / "premultiplied" / "none"（3 通道）；缺声明文件或
    声明损坏 → ``"undeclared"``——四通道消费方（比较/wipe）不得把
    undeclared 当成任一语义静默用（转域前须显式选择）。
    """
    path = _alpha_declaration_path(topos_path)
    if not os.path.isfile(path):
        return "undeclared"
    try:
        import json as _json
        with open(path, "r", encoding="utf-8") as fh:
            decl = _json.load(fh)
        value = str(decl.get("alpha_semantics", ""))
        return value if value in ("none", "straight", "premultiplied") \
            else "undeclared"
    except (OSError, ValueError):
        return "undeclared"


def align_alpha_domain(img: np.ndarray, semantics: str,
                       target: str = "premultiplied",
                       ) -> np.ndarray:
    """M09.3 · 按声明对齐 alpha 比较域（straight↔premultiplied 转换）。

    ``semantics="undeclared"`` 时抛 ValueError——比较方不得猜语义
    （透明边缘被错误预乘即像素错误）。3 通道原样返回。
    """
    if img is None or getattr(img, "ndim", 0) != 3 or img.shape[2] != 4:
        return img
    if semantics not in ("none", "straight", "premultiplied"):
        raise ValueError(
            f"alpha 语义未声明（{semantics!r}）——比较前须显式选择转域")
    if semantics == target or semantics == "none":
        return img
    return (premultiply_rgba(img) if target == "premultiplied"
            else unpremultiply_rgba(img))


def matches_query(preset_name: str, meta: Optional[PresetMeta],
                  query: str) -> bool:
    """搜索匹配：预设名 / 来源片段名 / 元数据（大小写不敏感子串）。"""
    q = (query or "").strip().lower()
    if not q:
        return True
    hay = preset_name
    if meta is not None:
        hay += " " + " ".join(
            str(v) for v in (
                meta.display_name, meta.clip_id, meta.source_transfer,
                meta.source_primaries, meta.source_range,
            ) if v
        )
    return q in hay.lower()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
