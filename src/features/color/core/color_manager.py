"""
ColorManager — Centralized color management for the application.

Handles:
- Color primaries/transfer/matrix/range enums (FFmpeg → internal mapping)
- ICC profile detection, assignment, and embedding (via PIL.ImageCms / LittleCMS2)
- Color space conversion for import / export pipelines
- Optional OpenColorIO integration for advanced workflows

All 9 Phase-7 preferences are wired through this module:
  default_color_space, default_bit_depth, gpu_color_processing,
  rendering_intent, ocio_config_path, auto_assign_icc,
  export_color_profile, embed_icc_profile, show_color_space_warnings
"""

from __future__ import annotations

import os
import logging
import threading
import cv2
import numpy as np
from PIL import Image, ImageCms
from typing import Optional, Tuple, Dict, Any

# Import fundamental color types from shared (avoids shared->features circular dependency)
# 历史转口 re-export：async_renderer 与 tests/media 仍经本模块导入以下四个枚举
from src.shared.color.types import (  # noqa: F401
    ColorPrimaries,  # noqa: F401 — 转口 re-export
    ColorTransfer,  # noqa: F401 — 转口 re-export
    ColorMatrix,  # noqa: F401 — 转口 re-export
    ColorRange,  # noqa: F401 — 转口 re-export
)

logger = logging.getLogger(__name__)


# =============================================================================
# Optional: OpenColorIO for advanced studio pipelines
# =============================================================================

try:
    import PyOpenColorIO as OCIO
    OCIO_AVAILABLE = True
except ImportError:
    OCIO_AVAILABLE = False


def get_color_manager():
    """Module-level convenience accessor for the ColorManager singleton."""
    return ColorManager()


class ColorManager:
    """Singleton color management engine backed by LittleCMS2 (Pillow).

    Usage::
        from src.features.color.core.color_manager import get_color_manager
        cm = get_color_manager()          # returns singleton
        cm.reconfigure(config_manager)     # after preferences change
    """

    _instance = None
    # AUD-0306: 首次并发实例化保护（对照 cache_manager.get_cache_manager
    # 的正确做法）。__new__ 与 __init__ 共用这把锁：无锁时两个线程可同时
    # 观察到 _instance is None，各自完整执行昂贵的 _init_profiles（系统
    # ICC 目录扫描 + LittleCMS profile 创建），且先创建者被后创建者覆盖。
    _instance_lock = threading.Lock()

    # ── Constants ──────────────────────────────────────────────────────

    COLOR_SPACES = [
        'sRGB', 'Adobe RGB', 'ProPhoto RGB',
        'Rec. 709', 'Rec. 2020', 'DCI-P3', 'Display P3', 'Linear',
        # Stage-2: print/CMYK spaces (profiles resolved from bundled dir).
        'CMYK (US Web Coated SWOP)', 'CMYK (Coated FOGRA39)',
    ]

    BIT_DEPTHS = ['8-bit', '16-bit', '32-bit float']

    RENDERING_INTENTS = {
        'perceptual': ImageCms.Intent.PERCEPTUAL,
        'relative':   ImageCms.Intent.RELATIVE_COLORIMETRIC,
        'saturation': ImageCms.Intent.SATURATION,
        'absolute':   ImageCms.Intent.ABSOLUTE_COLORIMETRIC,
    }

    # Lowercase substrings used to match a profile description to a space name
    _PROFILE_PATTERNS = {
        'sRGB':         ['srgb', 's rgb', 'iec 61966-2.1'],
        'Adobe RGB':    ['adobe rgb', 'adob1998', 'adobergb'],
        'ProPhoto RGB': ['prophoto', 'romm rgb'],
        'Rec. 709':     ['rec. 709', 'rec709', 'bt.709', 'bt709'],
        'Rec. 2020':    ['rec. 2020', 'rec2020', 'bt.2020', 'bt2020'],
        'DCI-P3':       ['dci-p3', 'dcip3'],
        'Display P3':   ['display p3', 'displayp3'],
        'Linear':       ['linear rgb', 'scene-linear'],
        # CMYK print profiles (stage 2 — resolved from bundled dir / system).
        'CMYK (US Web Coated SWOP)': ['us web coated', 'swop', 'cmyk'],
        'CMYK (Coated FOGRA39)':     ['fogra39', 'coated fogra'],
    }

    # Bundled ICC profile filenames (looked up in src/shared/color/profiles/).
    # The actual .icc files live in that directory; this maps space name →
    # expected filename. Missing files fall back to system profiles then sRGB.
    _BUNDLED_PROFILE_FILES = {
        'sRGB':                         'sRGB-IEC61966-2.1.icc',
        'Adobe RGB':                    'AdobeRGB1998.icc',
        'Rec. 2020':                    'Rec2020.icc',
        'DCI-P3':                       'DCI-P3.icc',
        'Display P3':                   'DisplayP3.icc',
        'CMYK (US Web Coated SWOP)':    'USWebCoatedSWOP.icc',
        'CMYK (Coated FOGRA39)':        'CoatedFOGRA39.icc',
    }

    # ── Singleton boilerplate ──────────────────────────────────────────

    def __new__(cls, *args, **kwargs):
        """Create the singleton instance on first call, then reuse it."""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, config_manager=None):
        """Initialize profiles and OCIO once (repeat calls only refresh config)."""
        with ColorManager._instance_lock:
            if getattr(self, '_initialized', False):
                if config_manager is not None:
                    self.config_manager = config_manager
                return
            self._initialized = True
            self.config_manager = config_manager
            self._ocio_config = None
            self._srgb_profile = None
            self._system_profiles: dict[str, str] = {}

            self._init_profiles()
            self._init_ocio()

    # ── Initialisation helpers ─────────────────────────────────────────

    def _init_profiles(self):
        """Create the built-in sRGB profile and scan the OS for others."""
        try:
            self._srgb_profile = ImageCms.createProfile('sRGB')
        except Exception as e:
            logger.warning(f"Failed to create built-in sRGB ICC profile: {e}")
        self._scan_system_profiles()

    def _scan_system_profiles(self):
        """Find well-known ICC profiles shipped with the OS."""
        import platform
        system = platform.system()

        if system == 'Darwin':
            dirs = ['/System/Library/ColorSync/Profiles',
                    '/Library/ColorSync/Profiles']
        elif system == 'Windows':
            dirs = [os.path.join(os.environ.get('WINDIR', r'C:\Windows'),
                                 'System32', 'spool', 'drivers', 'color')]
        else:
            dirs = ['/usr/share/color/icc',
                    '/usr/local/share/color/icc']

        for profile_dir in dirs:
            if not os.path.isdir(profile_dir):
                continue
            for fname in os.listdir(profile_dir):
                fpath = os.path.join(profile_dir, fname)
                if not os.path.isfile(fpath):
                    continue
                if not fname.lower().endswith(('.icc', '.icm')):
                    continue
                name_lower = fname.lower()
                for space, patterns in self._PROFILE_PATTERNS.items():
                    if any(p in name_lower for p in patterns):
                        self._system_profiles.setdefault(space, fpath)
                        break

    def _init_ocio(self):
        """Try to load an OCIO config when PyOpenColorIO is available."""
        self._ocio_config = None
        if not OCIO_AVAILABLE or not self.config_manager:
            return
        ocio_path = self.config_manager.get('ocio_config_path') or ''
        if ocio_path and os.path.isfile(ocio_path):
            try:
                self._ocio_config = OCIO.Config.CreateFromFile(ocio_path)
            except Exception as e:
                logger.error(f": Failed to load OCIO config: {e}")

    # ── Public API — reconfigure ───────────────────────────────────────

    def reconfigure(self, config_manager=None):
        """Reload settings after the user changes preferences."""
        if config_manager is not None:
            self.config_manager = config_manager
        self._init_ocio()

    # ── Public API — detection ─────────────────────────────────────────

    def detect_icc_profile(self, path: str) -> Optional[Tuple[str, bytes]]:
        """Detect the ICC profile embedded in an image file.

        W2：`.toos` 文件走 ICCP chunk 读取（此前恒 None——嵌入 ICC 的
        .toos 在色彩页被静默当 sRGB 处理）；其余格式沿用 Pillow info。
        Returns ``(color_space_name, icc_bytes)`` or *None*.
        """
        if isinstance(path, str) and path.lower().endswith('.toos'):
            return self._detect_toos_icc_profile(path)
        try:
            with Image.open(path) as pil_img:
                icc_data = pil_img.info.get('icc_profile')
                if icc_data:
                    return self.identify_color_space(icc_data), icc_data
        except Exception:
            pass
        return None

    def _detect_toos_icc_profile(
            self, path: str) -> Optional[Tuple[str, bytes]]:
        """`.toos` 的 ICCP chunk → (space_name, bytes)（W2 消费接线）。

        仅 ICCP chunk 存在（idsc iccp_ref=1，色彩表征权威）时返回；
        读取失败按无 ICC 处理（色彩标签 fallback = CICP 链，已由 G1
        接入 VideoMetadata）。
        """
        try:
            from src.shared.codec.topos_image_binding import (
                TC_IMG_CHUNK_ICCP, get_image_codec,
            )

            icc_data = get_image_codec().read_metadata(
                str(path), TC_IMG_CHUNK_ICCP)
            if icc_data:
                return self.identify_color_space(icc_data), icc_data
        except Exception:
            pass
        return None

    def identify_color_space(self, icc_data: bytes) -> str:
        """Map raw ICC bytes to a canonical colour-space name."""
        try:
            profile = ImageCms.ImageCmsProfile(
                ImageCms.core.profile_from_bytes(icc_data))
            desc = (profile.profile_description or '').lower()
            for space, patterns in self._PROFILE_PATTERNS.items():
                if any(p in desc for p in patterns):
                    return space
        except Exception:
            pass
        return 'Unknown'

    # ── Public API — profile access ────────────────────────────────────

    def get_srgb_profile_bytes(self) -> Optional[bytes]:
        """Return the built-in sRGB ICC profile as raw bytes."""
        try:
            if self._srgb_profile:
                return ImageCms.core.profile_tobytes(self._srgb_profile)
        except Exception:
            pass
        return None

    def _bundled_profiles_dir(self) -> str:
        """Path to the bundled ICC profile directory (may not exist yet).

        color_manager.py lives at ``src/features/color/core/color_manager.py``.
        The profiles dir is at ``src/shared/color/profiles/``: from
        ``core/`` go up 3 levels to ``src/``, then into ``shared/color/profiles``.
        """
        # __file__ = .../src/features/color/core/color_manager.py
        #   ../  -> color/    ../../  -> features/    ../../../  -> src/
        src_root = os.path.normpath(os.path.join(
            os.path.dirname(__file__), "..", "..", ".."
        ))
        return os.path.join(src_root, "shared", "color", "profiles")

    def get_profile_for_space(self, color_space: str) -> Optional[bytes]:
        """Return ICC profile bytes for *color_space*, falling back to sRGB.

        Lookup order:
          1. Bundled profile (src/shared/color/profiles/<file>) — self-contained,
             does not depend on the OS shipping ICC files.
          2. System-installed profile (ColorSync / Windows color dir).
          3. In-process sRGB profile (always available).
        """
        if color_space == 'sRGB':
            # Prefer a bundled sRGB profile (matches official IEC61966-2.1);
            # fall back to the in-process Pillow sRGB profile.
            bundled = os.path.join(self._bundled_profiles_dir(),
                                   self._BUNDLED_PROFILE_FILES.get('sRGB', ''))
            if os.path.isfile(bundled):
                try:
                    with open(bundled, 'rb') as f:
                        return f.read()
                except Exception:
                    pass
            return self.get_srgb_profile_bytes()

        # 1. Bundled profile.
        bundled_name = self._BUNDLED_PROFILE_FILES.get(color_space)
        if bundled_name:
            bundled = os.path.join(self._bundled_profiles_dir(), bundled_name)
            if os.path.isfile(bundled):
                try:
                    with open(bundled, 'rb') as f:
                        return f.read()
                except Exception:
                    pass
        # 2. System profile.
        fpath = self._system_profiles.get(color_space)
        if fpath and os.path.isfile(fpath):
            try:
                with open(fpath, 'rb') as f:
                    return f.read()
            except Exception:
                pass
        # 3. sRGB fallback.
        return self.get_srgb_profile_bytes()

    # ── Public API — export ────────────────────────────────────────────

    def convert_for_export(self, image: np.ndarray,
                           source_space: Optional[str] = None,
                           target_space: Optional[str] = None) -> np.ndarray:
        """Convert *image* (BGR ndarray) to *target_space* for export.

        Falls back gracefully — returns the original on any failure.
        """
        if target_space is None and self.config_manager:
            target_space = self.config_manager.get('export_color_profile') or 'sRGB'
        if not target_space or target_space == source_space:
            return image
        try:
            return self._convert_via_lcms(image, source_space or 'sRGB',
                                          target_space)
        except Exception as e:
            logger.error(f": conversion failed ({e})")
            return image

    def embed_icc(self, path: str,
                  color_space: Optional[str] = None) -> bool:
        """Embed an ICC profile inside a JPEG / PNG / TIFF file.

        Called **after** ``FileIO.save_image()`` succeeds.
        """
        ext = os.path.splitext(path)[1].lower()
        if ext not in ('.jpg', '.jpeg', '.png', '.tif', '.tiff'):
            return False

        if color_space is None and self.config_manager:
            color_space = self.config_manager.get('export_color_profile') or 'sRGB'
        if not color_space:
            color_space = 'sRGB'

        icc_data = self.get_profile_for_space(color_space)
        if not icc_data:
            return False

        try:
            with Image.open(path) as pil_img:
                save_kwargs: Dict[str, Any] = {'icc_profile': icc_data}
                info = pil_img.info
                # AUD-0304: 再开再存必须保留既有元数据并避免二次有损压缩。
                # 此前仅传 icc_profile：JPEG 被 Pillow 以默认 quality=75 重编
                # 码（成品被无声重压），EXIF/XMP 一并丢失。
                if info.get('exif') is not None:
                    save_kwargs['exif'] = info['exif']
                if info.get('xmp') is not None:
                    save_kwargs['xmp'] = info['xmp']
                if ext in ('.jpg', '.jpeg'):
                    # quality='keep' 复用原 JPEG 量化表，仅重写字节流。
                    save_kwargs['quality'] = 'keep'
                try:
                    pil_img.save(path, **save_kwargs)
                except TypeError:
                    # 个别 Pillow 版本/格式组合不接受 exif/xmp 关键字；
                    # 退回仅嵌 ICC（质量保持逻辑不受影响）。
                    logger.debug("ICC embed retry without metadata kwargs")
                    pil_img.save(path, icc_profile=icc_data)
            return True
        except Exception as e:
            logger.error(f": ICC embed failed ({e})")
            return False

    # ── Public API — import ────────────────────────────────────────────

    def auto_assign_icc(self, path: str,
                        image: np.ndarray) -> Tuple[np.ndarray, Optional[str]]:
        """Detect the ICC profile of an imported image.

        If ``auto_assign_icc`` is *True* and no profile is found, assume sRGB.

        Returns ``(image, detected_space_name)``.
        """
        if not self.config_manager or \
                not self.config_manager.get('auto_assign_icc'):
            return image, None

        detection = self.detect_icc_profile(path)
        if detection:
            return image, detection[0]

        return image, 'sRGB (assumed)'

    # ── Public API — warnings ──────────────────────────────────────────

    def check_color_space_mismatch(self, image_path: str) -> Optional[str]:
        """Return a warning string if the image space ≠ default space.

        Respects the ``show_color_space_warnings`` preference.
        """
        if not self.config_manager:
            return None
        if not self.config_manager.get('show_color_space_warnings'):
            return None

        detection = self.detect_icc_profile(image_path)
        if not detection:
            return None

        space_name = detection[0]
        default = self.config_manager.get('default_color_space') or 'sRGB'
        if space_name != 'Unknown' and space_name != default:
            return (f"Image color space ({space_name}) differs from "
                    f"the default ({default}).")
        return None

    # ── Public API — new images ────────────────────────────────────────

    def apply_defaults_to_new_image(self, image: np.ndarray) -> np.ndarray:
        """Apply the configured default bit depth to a newly created image."""
        if not self.config_manager:
            return image

        bit_depth = self.config_manager.get('default_bit_depth') or '8-bit'
        if bit_depth == '16-bit' and image.dtype == np.uint8:
            image = (image.astype(np.float32) / 255.0 * 65535).astype(np.uint16)
        elif bit_depth == '32-bit float':
            if image.dtype == np.float32:
                pass  # 已是目标表示
            elif np.issubdtype(image.dtype, np.floating):
                # AUD-0305: float16/float64 输入本就是线性 0..1（HDR 内部
                # 格式），只做纯 cast——旧的 else 分支会把它们再除以 255，
                # 画面亮度骤降 255 倍（近黑）。
                image = image.astype(np.float32)
            elif image.dtype == np.uint16:
                image = image.astype(np.float32) / 65535.0
            else:
                image = image.astype(np.float32) / 255.0
        return image

    # ── Public API — config accessors ──────────────────────────────────

    def is_gpu_color_processing(self) -> bool:
        """Whether GPU color processing is enabled (defaults to True)."""
        if self.config_manager:
            return self.config_manager.get('gpu_color_processing') is not False
        return True

    def get_rendering_intent(self) -> str:
        """Configured rendering intent key (defaults to 'relative')."""
        if self.config_manager:
            return self.config_manager.get('rendering_intent') or 'relative'
        return 'relative'

    def get_default_color_space(self) -> str:
        """Configured default color space name (defaults to 'sRGB')."""
        if self.config_manager:
            return self.config_manager.get('default_color_space', 'sRGB')
        return 'sRGB'

    # ── Internal — LCMS conversion ─────────────────────────────────────

    def _convert_via_lcms(self, image: np.ndarray,
                          src_space: str, dst_space: str) -> np.ndarray:
        """Colour-space conversion through LittleCMS2.

        Pillow 的 ImageCms 只支持 8-bit RGB 变换（本仓库 Pillow 12.1.1
        实测：3 通道 uint16 无法 fromarray，单通道 I;16 无法 build
        transform），因此高位深输入在本路径以 8-bit 中转。AUD-0303 修复：

        - uint16 输入显式降标到 uint8 后参与变换。此前 ``Image.fromarray``
          直接抛 TypeError，被 ``convert_for_export`` 捕获后静默返回未转换
          图像——导出目标色彩空间根本没有被应用；
        - float 输入含 >1.0 高光时记录 warning（8-bit LCMS 路径必然裁剪
          HDR 高光；接入 float/GPU 导出路径前的诚实降级），量化改用
          四舍五入以降低 banding 偏差，而非截断。
        """
        is_float = np.issubdtype(image.dtype, np.floating)

        # BGR → RGB
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB) \
            if len(image.shape) == 3 else image

        if is_float:
            rgb_f = rgb.astype(np.float32, copy=False)
            if np.any(rgb_f > 1.0):
                logger.warning(
                    "LCMS export path clamps HDR highlights (>1.0) for "
                    "%s -> %s; use a float/GPU export path for HDR output",
                    src_space, dst_space,
                )
            rgb = np.rint(np.clip(rgb_f, 0.0, 1.0) * 255.0).astype(np.uint8)
        elif rgb.dtype == np.uint16:
            rgb = np.rint(rgb.astype(np.float32) / 257.0).astype(np.uint8)
        elif rgb.dtype != np.uint8:
            rgb = np.rint(
                np.clip(rgb.astype(np.float32), 0.0, 255.0)
            ).astype(np.uint8)

        pil_img = Image.fromarray(rgb)

        src_prof = self._get_pil_profile(src_space)
        dst_prof = self._get_pil_profile(dst_space)

        if src_prof and dst_prof:
            intent = self.RENDERING_INTENTS.get(
                self.get_rendering_intent(),
                ImageCms.Intent.RELATIVE_COLORIMETRIC,
            )
            pil_img = ImageCms.profileToProfile(
                pil_img, src_prof, dst_prof, renderingIntent=intent)

        result = np.array(pil_img)
        if is_float:
            result = result.astype(np.float32) / 255.0

        # RGB → BGR
        if len(result.shape) == 3:
            result = cv2.cvtColor(result, cv2.COLOR_RGB2BGR)
        return result

    def _get_pil_profile(self, color_space: str):
        """Return a PIL ImageCms profile object for *color_space*.

        AUD-0307: 与 ``get_profile_for_space`` 使用同一三级查找
        （bundled → system → sRGB）。此前本方法跳过 bundled 目录，在未
        安装对应系统 ICC 的机器上，``convert_for_export`` 的目标空间静默
        退化为 sRGB——嵌入的 ICC（走 get_profile_for_space）与实际像素
        变换（走本方法）来源不一致。最终退化到 sRGB 时记录 warning。
        """
        if color_space == 'sRGB':
            return self._srgb_profile or ImageCms.createProfile('sRGB')

        # 1. Bundled profile（与 get_profile_for_space 同序）。
        bundled_name = self._BUNDLED_PROFILE_FILES.get(color_space)
        if bundled_name:
            bundled = os.path.join(self._bundled_profiles_dir(), bundled_name)
            if os.path.isfile(bundled):
                try:
                    return ImageCms.ImageCmsProfile(bundled)
                except Exception:
                    logger.warning(
                        "Failed to load bundled ICC profile %s for %s",
                        bundled, color_space, exc_info=True,
                    )

        # 2. System profile。
        fpath = self._system_profiles.get(color_space)
        if fpath and os.path.isfile(fpath):
            try:
                return ImageCms.ImageCmsProfile(fpath)
            except Exception:
                logger.warning(
                    "Failed to load system ICC profile %s for %s",
                    fpath, color_space, exc_info=True,
                )

        # 3. Graceful fallback —— 必须可闻，不允许静默色彩空间错换。
        logger.warning(
            "No ICC profile found for colour space %r; pixel conversion "
            "falls back to sRGB (export will be wrong unless sRGB was "
            "intended)", color_space,
        )
        return self._srgb_profile or ImageCms.createProfile('sRGB')
