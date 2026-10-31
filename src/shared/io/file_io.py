import os
import io
import subprocess
import json
import hashlib
import threading
import logging
from typing import Optional

# Enable OpenEXR support in OpenCV (disabled by default for security in recent versions)
os.environ["OPENCV_IO_ENABLE_OPENEXR"] = "1"

import cv2
import numpy as np
import pillow_heif
from PIL import Image
from PIL.ExifTags import TAGS

# RAW 解码是格式专属能力。缺失 rawpy 不应阻断普通图像、缩略图或时间线 UI 的
# 导入；真正打开 RAW 时由对应分支返回清晰的失败结果。
try:
    import rawpy
except ImportError:  # pragma: no cover - 取决于运行环境是否安装 RAW 支持
    rawpy = None

logger = logging.getLogger(__name__)

try:
    from psd_tools import PSDImage
except ImportError:
    PSDImage = None

class FileIO:
    # Project root is 3 levels up from src/shared/io (points to src/)
    PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    # Cache root is one level above src/ (the top-level project directory)
    _CACHE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    CACHE_DIR = os.path.join(_CACHE_ROOT, ".cache", "topos-color", "images_cache")
    BAKED_CACHE_DIR = os.path.join(_CACHE_ROOT, ".cache", "topos-color", "baked_layers")  # AI Baking cache directory
    LOW_RES_SIZE = 1280
    HIGH_RES_SIZE = 4000

    # RAW instance cache
    _current_raw_obj = None
    _current_raw_path = None
    _raw_lock = threading.Lock()

    @staticmethod
    def _ensure_baked_cache_dir() -> None:
        """Ensure the baked layers cache directory exists."""
        if not os.path.exists(FileIO.BAKED_CACHE_DIR):
            os.makedirs(FileIO.BAKED_CACHE_DIR, exist_ok=True)

    @staticmethod
    def save_baked_layer(filename: str, image_array: np.ndarray) -> bool:
        """
        Save a baked layer result to EXR format with DWAB compression.

        The file is written as HALF float (16-bit) EXR — a size/precision
        balance, not full 32-bit float (AUD-33911: the docstring previously
        claimed full 32-bit float precision while the code below writes
        ``IMWRITE_EXR_TYPE = 1`` (HALF); values beyond the HALF range/
        precision are quantized on disk).

        Args:
            filename: The filename for the baked EXR file (e.g., "baked_layer_uuid.exr")
            image_array: numpy array (float32, BGR format, values in 0-1 range)

        Returns:
            True if save was successful, False otherwise
        """
        if image_array is None:
            return False

        FileIO._ensure_baked_cache_dir()
        filepath = os.path.join(FileIO.BAKED_CACHE_DIR, filename)

        try:
            # Ensure float32 type
            if image_array.dtype != np.float32:
                image_array = image_array.astype(np.float32)

            # EXR parameters:
            # - exr_type=1 (HALF float - 16-bit, good balance of precision and size)
            # - exr_compression=9 (DWAB - best compression for HDR images)
            params = [cv2.IMWRITE_EXR_TYPE, 1, cv2.IMWRITE_EXR_COMPRESSION, 9]

            # Use imencode + tofile to handle Unicode paths on Windows
            success, encoded_img = cv2.imencode('.exr', image_array, params)
            if success:
                encoded_img.tofile(filepath)
                return True
            return False
        except Exception as e:
            logger.error(f"saving baked layer: {e}")
            return False

    @staticmethod
    def load_baked_layer(filename: str) -> Optional[np.ndarray]:
        """
        Load a baked layer from EXR format.

        Args:
            filename: The filename of the baked EXR file

        Returns:
            numpy array (float32, BGR format, values in 0-1 range) or None if failed
        """
        filepath = os.path.join(FileIO.BAKED_CACHE_DIR, filename)

        if not os.path.exists(filepath):
            return None

        try:
            # Use np.fromfile + imdecode to handle Unicode paths on Windows
            img_data = np.fromfile(filepath, dtype=np.uint8)
            img = cv2.imdecode(img_data, cv2.IMREAD_UNCHANGED)

            if img is None:
                return None

            # Ensure float32 output
            if img.dtype != np.float32:
                img = img.astype(np.float32)

            # Convert BGR to RGB for OpenGL texture upload
            # cv2.imdecode returns BGR format, but OpenGL expects RGB
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

            return img
        except Exception as e:
            logger.error(f"loading baked layer: {e}")
            return None

    @staticmethod
    def load_ai_plugin_result(filename: str) -> Optional[np.ndarray]:
        """
        Load an AI plugin result from EXR format.

        Does NOT convert BGR→RGB: the AI save path stores BGR, and callers
        consume the CPU cache in that same format. The GPU upload path is
        the one exception — it expects RGB and converts at the call site
        (P0-COL-03 channel contract: disk BGR / GPU RGB).

        Args:
            filename: The filename of the AI plugin result EXR file

        Returns:
            numpy array (float32, BGR format, values in 0-1 range) or None if failed
        """
        filepath = os.path.join(FileIO.BAKED_CACHE_DIR, filename)

        if not os.path.exists(filepath):
            return None

        try:
            img_data = np.fromfile(filepath, dtype=np.uint8)
            img = cv2.imdecode(img_data, cv2.IMREAD_UNCHANGED)

            if img is None:
                return None

            if img.dtype != np.float32:
                img = img.astype(np.float32)

            return img
        except Exception as e:
            logger.error(f"loading AI plugin result: {e}")
            return None

    @staticmethod
    def delete_baked_layer(filename: str) -> bool:
        """
        Delete a baked layer EXR file from cache.

        Args:
            filename: The filename of the baked EXR file to delete

        Returns:
            True if deletion was successful, False otherwise
        """
        filepath = os.path.join(FileIO.BAKED_CACHE_DIR, filename)

        if os.path.exists(filepath):
            try:
                os.remove(filepath)
                return True
            except Exception as e:
                logger.error(f"deleting baked layer: {e}")
                return False
        return False

    @staticmethod
    def cleanup_orphaned_cache(active_filenames: set) -> int:
        """
        清理 .cache/baked_layers/ 中不在 active_filenames 集合里的孤立 EXR 文件。

        Args:
            active_filenames: 当前仍需要的文件名集合（来自 engine layers + source_bake）

        Returns:
            清理的文件数量
        """
        if not os.path.exists(FileIO.BAKED_CACHE_DIR):
            return 0

        cleaned = 0
        for f in os.listdir(FileIO.BAKED_CACHE_DIR):
            if f.endswith('.exr') and f not in active_filenames:
                try:
                    os.remove(os.path.join(FileIO.BAKED_CACHE_DIR, f))
                    cleaned += 1
                except OSError:
                    pass
        return cleaned

    @classmethod
    def set_cache_directory(cls, custom_dir: str):
        """Override the default baked layers cache directory path."""
        if custom_dir:
            target = os.path.join(custom_dir, "baked_layers")
            os.makedirs(target, exist_ok=True)
            cls.BAKED_CACHE_DIR = target

    @staticmethod
    def cleanup_old_cache(max_age_days: int = 7, cache_dir: str = "",
                          max_total_size_gb: float = 0) -> int:
        """Remove cache files older than max_age_days and enforce total size limit.

        Args:
            max_age_days: Maximum age in days for cached files.
            cache_dir: Override cache directory (uses BAKED_CACHE_DIR if empty).
            max_total_size_gb: Maximum total cache size in GB (0 = no limit).
                When exceeded, oldest files are deleted until under the limit.

        Returns:
            Number of files removed.
        """
        import time as _time

        target_dir = cache_dir or FileIO.BAKED_CACHE_DIR
        if not os.path.exists(target_dir):
            return 0

        now = _time.time()
        max_age_seconds = max_age_days * 86400
        cleaned = 0

        # Phase 1: Remove files older than max_age_days
        for f in os.listdir(target_dir):
            filepath = os.path.join(target_dir, f)
            if os.path.isfile(filepath):
                try:
                    file_age = now - os.path.getmtime(filepath)
                    if file_age > max_age_seconds:
                        os.remove(filepath)
                        cleaned += 1
                except OSError:
                    pass

        # Phase 2: Enforce total size limit (delete oldest files until under limit)
        if max_total_size_gb > 0:
            max_bytes = max_total_size_gb * 1024 * 1024 * 1024
            # Collect remaining files with their mtimes
            files = []
            for f in os.listdir(target_dir):
                filepath = os.path.join(target_dir, f)
                if os.path.isfile(filepath):
                    try:
                        files.append((filepath, os.path.getmtime(filepath), os.path.getsize(filepath)))
                    except OSError:
                        pass

            total_size = sum(sz for _, _, sz in files)
            if total_size > max_bytes:
                # Sort oldest first
                files.sort(key=lambda x: x[1])
                for filepath, _, sz in files:
                    if total_size <= max_bytes:
                        break
                    try:
                        os.remove(filepath)
                        total_size -= sz
                        cleaned += 1
                    except OSError:
                        pass

        return cleaned

    @staticmethod
    def load_image(path):
        """
        Loads an image and returns a tuple (full, high, low).
        """
        if not os.path.exists(path):
            return None, None, None

        # AUD-33910: check-then-create is a TOCTOU race — two threads loading
        # images concurrently can both see "missing" and one makedirs() then
        # raises FileExistsError. makedirs(exist_ok=True) alone is race-free.
        os.makedirs(FileIO.CACHE_DIR, exist_ok=True)

        full_img = FileIO._read_full_image(path)
        if full_img is None:
            return None, None, None

        h, w = full_img.shape[:2]
        
        # 1. Prepare Low Res (Proxy)
        low_img = FileIO._get_cached_preview(path, "low")
        if low_img is None:
            if w > FileIO.LOW_RES_SIZE or h > FileIO.LOW_RES_SIZE:
                low_img = FileIO._resize_to_fit(full_img, FileIO.LOW_RES_SIZE)
                FileIO._cache_preview(path, low_img, "low")
            else:
                low_img = full_img.copy()

        # 2. Prepare High Res
        if w <= FileIO.HIGH_RES_SIZE and h <= FileIO.HIGH_RES_SIZE:
            high_img = full_img.copy()
        else:
            high_img = FileIO._get_cached_preview(path, "high")
            if high_img is None:
                high_img = FileIO._resize_to_fit(full_img, FileIO.HIGH_RES_SIZE)
                FileIO._cache_preview(path, high_img, "high")

        return full_img, high_img, low_img

    @staticmethod
    def _read_full_image(path):
        ext = os.path.splitext(path)[1].lower()
        try:
            img = None
            if ext in ['.heic', '.heif']:
                heif_file = pillow_heif.read_heif(path)
                image = Image.frombytes(heif_file.mode, heif_file.size, heif_file.data, "raw")
                if heif_file.mode == "RGBA":
                    img = cv2.cvtColor(np.array(image), cv2.COLOR_RGBA2BGR)
                else:
                    img = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
            elif ext in ['.cr2', '.cr3', '.nef', '.dng', '.arw', '.orf', '.raf']:
                if rawpy is None:
                    logger.error("RAW decoding requires rawpy; cannot open %s", path)
                    return None
                with FileIO._raw_lock:
                    if FileIO._current_raw_path != path:
                        if FileIO._current_raw_obj:
                            FileIO._current_raw_obj.close()
                        # AUD-33903：先清空单例状态再重开。imread 抛错时若保留
                        # "已 close 对象 + 旧路径"的悬挂组合，此后旧路径请求会因
                        # 路径相等跳过重开、对已关闭对象 postprocess 而永久失败。
                        FileIO._current_raw_obj = None
                        FileIO._current_raw_path = None
                        FileIO._current_raw_obj = rawpy.imread(path)
                        FileIO._current_raw_path = path
                    
                    safe_wb = FileIO._get_normalized_camera_wb(FileIO._current_raw_obj)
                    rgb = FileIO._current_raw_obj.postprocess(
                        use_camera_wb=False, 
                        user_wb=safe_wb,
                        no_auto_bright=True, 
                        bright=1.0, 
                        user_sat=None, 
                        output_bps=16, 
                        gamma=(1, 1),
                        output_color=rawpy.ColorSpace.ProPhoto
                    )
                    img = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
                    img = FileIO._apply_exif_orientation(img, path)
            elif ext == '.psd' and PSDImage:
                psd = PSDImage.open(path)
                img_rgba = np.array(psd.composite())
                img = cv2.cvtColor(img_rgba, cv2.COLOR_RGBA2BGR)
            else:
                stream = open(path, "rb")
                bytes_data = bytearray(stream.read())
                numpyarray = np.asarray(bytes_data, dtype=np.uint8)
                img = cv2.imdecode(numpyarray, cv2.IMREAD_UNCHANGED)
                
                if img is not None:
                    if len(img.shape) == 2:
                        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
                    elif img.shape[2] == 4:
                        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
                    img = FileIO._apply_exif_orientation(img, path)
            
            if ext in ['.heic', '.heif'] and img is not None:
                img = FileIO._apply_exif_orientation(img, path)

            return img
        except Exception as e:
            logger.error(f"loading full image {path}: {e}")
            return None

    @staticmethod
    def _apply_exif_orientation(image, path):
        """Automatically rotates an image based on EXIF orientation tag."""
        try:
            with Image.open(path) as pil_img:
                exif = pil_img.getexif()
                if not exif:
                    return image
                orientation = exif.get(274)
                if orientation == 3:
                    return cv2.rotate(image, cv2.ROTATE_180)
                elif orientation == 6:
                    return cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
                elif orientation == 8:
                    return cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
                elif orientation == 2: 
                    return cv2.flip(image, 1)
                elif orientation == 4: 
                    return cv2.flip(image, 0)
                elif orientation == 5: 
                    image = cv2.flip(image, 1)
                    return cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
                elif orientation == 7: 
                    image = cv2.flip(image, 1)
                    return cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
        except Exception:
            pass
        return image

    @staticmethod
    def _get_normalized_camera_wb(raw):
        cam_wb = raw.camera_whitebalance
        if sum(cam_wb) < 0.1:
            cam_wb = raw.daylight_whitebalance
        if sum(cam_wb) < 0.1:
            cam_wb = [2.0, 1.0, 1.5, 1.0]
        g = max(cam_wb[1], 1e-6)
        return [float(cam_wb[0]/g), 1.0, float(cam_wb[2]/g), 1.0]

    @staticmethod
    def get_cache_key(path, suffix="") -> str:
        if not os.path.exists(path):
            return hashlib.md5(f"{path}_{suffix}".encode()).hexdigest()
        stat = os.stat(path)
        key_str = f"{path}_{stat.st_size}_{stat.st_mtime}_{suffix}_v2"
        return hashlib.md5(key_str.encode()).hexdigest()

    @staticmethod
    def _get_cached_preview(path, res_type):
        key = FileIO.get_cache_key(path, res_type)
        for ext in ['.exr', '.jpg', '.png']:
            cache_path = os.path.join(FileIO.CACHE_DIR, f"{key}{ext}")
            if os.path.exists(cache_path):
                try:
                    # Use np.fromfile + imdecode to handle Unicode paths on Windows
                    img_data = np.fromfile(cache_path, dtype=np.uint8)
                    return cv2.imdecode(img_data, cv2.IMREAD_UNCHANGED)
                except Exception:
                    continue
        return None

    @staticmethod
    def update_cache(path, preview_img, res_type):
        FileIO._cache_preview(path, preview_img, res_type)

    @staticmethod
    def _cache_preview(path, preview_img, res_type):
        if preview_img is None:
            return
        key = FileIO.get_cache_key(path, res_type)
        if not os.path.exists(FileIO.CACHE_DIR):
            os.makedirs(FileIO.CACHE_DIR, exist_ok=True)
        try:
            if preview_img.dtype == np.uint8:
                has_alpha = len(preview_img.shape) == 3 and preview_img.shape[2] == 4
                if has_alpha:
                    cache_path = os.path.join(FileIO.CACHE_DIR, f"{key}.png")
                    params = [cv2.IMWRITE_PNG_COMPRESSION, 1]
                    ext = '.png'
                else:
                    cache_path = os.path.join(FileIO.CACHE_DIR, f"{key}.jpg")
                    params = [cv2.IMWRITE_JPEG_QUALITY, 90]
                    ext = '.jpg'
                # Use imencode + tofile to handle Unicode paths on Windows
                encoded = cv2.imencode(ext, preview_img, params)
                if encoded[0]:
                    encoded[1].tofile(cache_path)
            else:
                cache_path = os.path.join(FileIO.CACHE_DIR, f"{key}.exr")
                save_img = preview_img.astype(np.float32)
                if preview_img.dtype == np.uint16:
                    save_img /= 65535.0
                # T-P1-71（libdeflate SIGILL，AUD-33901）：预览缓存此前用
                # ZIP(3)——opencv 轮子的 libOpenEXR 走 ZIP/PIZ 时经
                # libdeflate 派发，本机间歇性非法指令崩溃（.ips 栈
                # libdeflate_zlib_compress ← cv2.imwrite）。与 save_image
                # 的 T-P1-71 修复同步：统一 RLE(1)（无损、不经 deflate）。
                params = [cv2.IMWRITE_EXR_TYPE, 1, cv2.IMWRITE_EXR_COMPRESSION, 1]
                # Use imencode + tofile to handle Unicode paths on Windows
                encoded = cv2.imencode('.exr', save_img, params)
                if encoded[0]:
                    encoded[1].tofile(cache_path)
        except Exception as e:
            logger.error(f"caching preview: {e}")

    @staticmethod
    def _get_metadata_via_exiftool(path):
        """Attempts to use system 'exiftool' to get metadata for RAW and other files."""
        try:
            # We fetch a broad set of tags including manufacturer styles and exposure info
            cmd = [
                'exiftool', '-j',
                '-Make', '-Model', '-ISO', '-ISOSpeedRatings',
                '-ExposureTime', '-FNumber', '-FocalLength',
                '-LensModel', '-LensID', '-ImageWidth', '-ImageHeight',
                '-DateTimeOriginal', '-ExposureCompensation',
                '-ColorTemperature', '-Tint', '-ColorSpace', '-ProfileDescription',
                '-CreativeStyle', '-PictureStyle', '-PictureControlName', '-FilmMode',
                path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
            if result.returncode == 0:
                data = json.loads(result.stdout)
                if data and len(data) > 0:
                    return data[0]
        except Exception:
            pass
        return None

    @staticmethod
    def get_metadata(path):
        """Extracts metadata from an image file."""
        if not os.path.exists(path):
            return {}
        
        metadata = {
            "Filename": os.path.basename(path),
            "Size": f"{os.path.getsize(path) / (1024*1024):.2f} MB",
            "Format": os.path.splitext(path)[1][1:].upper(),
            "Resolution": "-"
        }

        # 1. Try Exiftool first (most powerful for RAW)
        exiftool_data = FileIO._get_metadata_via_exiftool(path)
        if exiftool_data:
            if 'Make' in exiftool_data: metadata["Manufacturer"] = str(exiftool_data['Make']).strip()
            if 'Model' in exiftool_data: metadata["Camera"] = str(exiftool_data['Model']).strip()
            iso = exiftool_data.get('ISO') or exiftool_data.get('ISOSpeedRatings')
            if iso: metadata["ISO"] = str(iso)
            if 'ExposureTime' in exiftool_data:
                shutter = str(exiftool_data['ExposureTime'])
                if not shutter.endswith('s'): shutter += 's'
                metadata["Shutter"] = shutter
            if 'FNumber' in exiftool_data:
                fnum = str(exiftool_data['FNumber'])
                if not fnum.startswith('f/'): fnum = f'f/{fnum}'
                metadata["Aperture"] = fnum
            if 'FocalLength' in exiftool_data:
                metadata["Focal Length"] = str(exiftool_data['FocalLength'])
            lens = exiftool_data.get('LensModel') or exiftool_data.get('LensID')
            if lens: metadata["Lens"] = str(lens).strip()
            w = exiftool_data.get('ImageWidth')
            h = exiftool_data.get('ImageHeight')
            if w and h: metadata["Resolution"] = f"{w} x {h}"
            
            # Additional advanced fields:
            if 'DateTimeOriginal' in exiftool_data:
                metadata["Date"] = str(exiftool_data['DateTimeOriginal'])
            if 'ExposureCompensation' in exiftool_data:
                val = exiftool_data['ExposureCompensation']
                try:
                    metadata["Exp. Comp."] = f"{float(val):+.1f} EV"
                except Exception:
                    metadata["Exp. Comp."] = str(val)
            
            temp = exiftool_data.get('ColorTemperature')
            tint = exiftool_data.get('Tint')
            if temp:
                wb_str = f"{temp}K"
                if tint: wb_str += f" (Tint: {tint})"
                metadata["White Balance"] = wb_str
            
            style = (exiftool_data.get('CreativeStyle') or 
                     exiftool_data.get('PictureStyle') or 
                     exiftool_data.get('PictureControlName') or 
                     exiftool_data.get('FilmMode'))
            if style:
                metadata["Style"] = str(style)
            
            profile = exiftool_data.get('ProfileDescription') or exiftool_data.get('ColorSpace')
            if profile:
                metadata["Color Space"] = str(profile)
            
            if "Camera" in metadata or "Shutter" in metadata:
                return metadata

        # 2. Fallback to Pillow (cross-platform EXIF reading)
        try:
            with Image.open(path) as img:
                metadata["Resolution"] = f"{img.width} x {img.height}"

                # 更完整的 EXIF 读取逻辑
                exif = img.getexif()
                if exif is not None:
                    # 收集所有 IFD 中的 EXIF 数据
                    exif_dict = {}

                    # 主 IFD
                    for tag_id, value in exif.items():
                        exif_dict[tag_id] = value

                    # EXIF 子 IFD (0x8769)
                    try:
                        exif_ifd = exif.get_ifd(0x8769)
                        if exif_ifd:
                            for tag_id, value in exif_ifd.items():
                                exif_dict[tag_id] = value
                    except Exception:
                        pass

                    # Interop IFD (0xA005) - 可能包含镜头信息
                    try:
                        interop_ifd = exif.get_ifd(0xA005)
                        if interop_ifd:
                            for tag_id, value in interop_ifd.items():
                                exif_dict[tag_id] = value
                    except Exception:
                        pass

                    # 解析 EXIF 数据
                    for tag_id, value in exif_dict.items():
                        if value is None:
                            continue
                        tag = TAGS.get(tag_id, str(tag_id))
                        try:
                            if tag == "Make":
                                metadata["Manufacturer"] = str(value).strip()
                            elif tag == "Model":
                                metadata["Camera"] = str(value).strip()
                            elif tag == "ISOSpeedRatings":
                                metadata["ISO"] = str(value)
                            elif tag == "ExposureTime":
                                num = float(value.numerator) if hasattr(value, 'numerator') else float(value)
                                den = float(value.denominator) if hasattr(value, 'denominator') else 1.0
                                if num >= den:
                                    metadata["Shutter"] = f"{num/den:.1f}s"
                                else:
                                    metadata["Shutter"] = f"1/{int(den/num)}s" if num > 0 else f"{value}s"
                            elif tag == "FNumber":
                                num = float(value.numerator) if hasattr(value, 'numerator') else float(value)
                                den = float(value.denominator) if hasattr(value, 'denominator') else 1.0
                                metadata["Aperture"] = f"f/{num/den:.1f}"
                            elif tag == "FocalLength":
                                num = float(value.numerator) if hasattr(value, 'numerator') else float(value)
                                den = float(value.denominator) if hasattr(value, 'denominator') else 1.0
                                metadata["Focal Length"] = f"{num/den:.1f}mm"
                            elif tag == "LensModel":
                                metadata["Lens"] = str(value).strip()
                            elif tag == "DateTimeOriginal":
                                metadata["Date"] = str(value)
                        except Exception:
                            # 跳过解析失败的标签
                            pass
        except Exception:
            # Pillow 读取失败，尝试 rawpy
            pass
            try:
                ext = os.path.splitext(path)[1].lower()
                if rawpy is not None and ext in ['.cr2', '.cr3', '.nef', '.dng', '.arw', '.orf', '.raf']:
                    with rawpy.imread(path) as raw:
                        # Resolution
                        metadata["Resolution"] = f"{raw.sizes.width} x {raw.sizes.height}"
                        # Extract EXIF-like metadata from rawpy
                        # rawpy exposes camera_maker, camera_model, etc.
                        if hasattr(raw, 'camera_maker') and raw.camera_maker:
                            metadata["Manufacturer"] = str(raw.camera_maker).strip()
                        if hasattr(raw, 'camera_model') and raw.camera_model:
                            metadata["Camera"] = str(raw.camera_model).strip()
                        # Try to get more metadata from the raw object's attributes
                        # rawpy does not expose ISO/shutter/aperture directly in older versions,
                        # so we also try parsing the embedded thumbnail EXIF via Pillow
                        try:
                            # Attempt to extract the embedded JPEG thumbnail for EXIF data
                            thumb = raw.extract_thumb()
                            if hasattr(thumb, 'data') and thumb.data:
                                thumb_img = Image.open(io.BytesIO(thumb.data))
                                thumb_exif = thumb_img.getexif()
                                if thumb_exif:
                                    exif_dict = {}
                                    exif_dict.update(thumb_exif)
                                    try:
                                        exif_sub = thumb_exif.get_ifd(0x8769)
                                        if exif_sub: exif_dict.update(exif_sub)
                                    except Exception:
                                        logger.debug("Failed to get EXIF sub IFD")
                                    for tag_id, value in exif_dict.items():
                                        tag = TAGS.get(tag_id, str(tag_id))
                                        if tag == "Make" and "Manufacturer" not in metadata:
                                            metadata["Manufacturer"] = str(value).strip()
                                        elif tag == "Model" and "Camera" not in metadata:
                                            metadata["Camera"] = str(value).strip()
                                        elif tag == "ISOSpeedRatings" and "ISO" not in metadata:
                                            metadata["ISO"] = str(value)
                                        elif tag == "ExposureTime" and "Shutter" not in metadata:
                                            try:
                                                num = float(value.numerator) if hasattr(value, 'numerator') else float(value)
                                                den = float(value.denominator) if hasattr(value, 'denominator') else 1.0
                                                if num >= den: metadata["Shutter"] = f"{num/den:.1f}s"
                                                else: metadata["Shutter"] = f"1/{int(den/num)}s" if num > 0 else f"{value}s"
                                            except Exception: metadata["Shutter"] = str(value)
                                        elif tag == "FNumber" and "Aperture" not in metadata:
                                            try:
                                                num = float(value.numerator) if hasattr(value, 'numerator') else float(value)
                                                den = float(value.denominator) if hasattr(value, 'denominator') else 1.0
                                                metadata["Aperture"] = f"f/{num/den:.1f}"
                                            except Exception: metadata["Aperture"] = str(value)
                                        elif tag == "FocalLength" and "Focal Length" not in metadata:
                                            try:
                                                num = float(value.numerator) if hasattr(value, 'numerator') else float(value)
                                                den = float(value.denominator) if hasattr(value, 'denominator') else 1.0
                                                metadata["Focal Length"] = f"{num/den:.1f}mm"
                                            except Exception: metadata["Focal Length"] = str(value)
                                        elif tag == "LensModel" and "Lens" not in metadata:
                                            metadata["Lens"] = str(value).strip()
                        except Exception:
                            logger.debug("Thumbnail EXIF extraction failed")
            except Exception:
                logger.debug("Failed to extract metadata from thumbnail")
        return metadata

    @staticmethod
    def calculate_wb_gains(baseline_wb, temp, tint) -> list[float]:
        factors = FileIO.kelvin_to_rgb_factors(temp, tint)
        r_gain = baseline_wb[0] * factors[0]
        b_gain = baseline_wb[2] * factors[2]
        return [float(r_gain), 1.0, float(b_gain), 1.0]

    @staticmethod
    def kelvin_to_rgb_factors(kelvin, tint) -> list[float]:
        import math
        t = max(2000, min(40000, kelvin)) / 100.0
        if t <= 66:
            r = 255.0
            g = max(1.0, 99.4708025861 * math.log(t) - 161.1195681661)
            if t <= 19: b = 1.0
            else: b = max(1.0, 138.5177312231 * math.log(t - 10) - 305.0447927307)
        else:
            r = max(1.0, 329.698727446 * math.pow(t - 60, -0.1332047592))
            g = max(1.0, 288.1221695283 * math.pow(t - 60, -0.0755148492))
            b = 255.0
        g_tinted = g * math.pow(2.0, tint / 100.0)
        return [g_tinted / r, 1.0, g_tinted / b]

    @staticmethod
    def update_raw_decoding(path, raw_params, preview_only=False):
        if rawpy is None:
            logger.error("RAW decoding requires rawpy; cannot update %s", path)
            return None, None, None, raw_params
        if not os.path.exists(path): return None, None, None, raw_params
        applied_params = raw_params.copy()
        with FileIO._raw_lock:
            if FileIO._current_raw_path != path:
                if FileIO._current_raw_obj: FileIO._current_raw_obj.close()
                # AUD-33903：同 _read_full_image——先清空状态再重开，
                # imread 失败不留"已 close 对象 + 旧路径"悬挂组合。
                FileIO._current_raw_obj = None
                FileIO._current_raw_path = None
                try:
                    FileIO._current_raw_obj = rawpy.imread(path)
                    FileIO._current_raw_path = path
                except Exception:
                    logger.exception("RAW reopen failed: %s", path)
                    return None, None, None, raw_params
            raw = FileIO._current_raw_obj
            pp_params = {"no_auto_bright": True, "bright": 1.0, "output_bps": 16, "gamma": (1, 1), "use_camera_wb": False, "use_auto_wb": False, "output_color": rawpy.ColorSpace.ProPhoto}
            baseline_wb = FileIO._get_normalized_camera_wb(raw)
            wb_type = raw_params.get('wb_type', 'camera')
            if wb_type in ['camera', 'as_shot']: pp_params["user_wb"] = baseline_wb
            elif wb_type == 'auto': pp_params["use_auto_wb"] = True
            elif wb_type == 'custom': pp_params["user_wb"] = FileIO.calculate_wb_gains(baseline_wb, raw_params.get('temp', 6500), raw_params.get('tint', 0))
            if pp_params.get("user_wb"): applied_params["user_wb"] = pp_params["user_wb"]
            try:
                rgb = raw.postprocess(half_size=preview_only, **pp_params)
                img = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
                img = FileIO._apply_exif_orientation(img, path)
                low_img = FileIO._resize_to_fit(img, FileIO.LOW_RES_SIZE)
                if preview_only: return None, None, low_img, applied_params
                h, w = img.shape[:2]
                high_img = img if (w <= FileIO.HIGH_RES_SIZE and h <= FileIO.HIGH_RES_SIZE) else FileIO._resize_to_fit(img, FileIO.HIGH_RES_SIZE)
                return img, high_img, low_img, applied_params
            except Exception: return None, None, None, raw_params

    @staticmethod
    def _resize_to_fit(img, max_size):
        h, w = img.shape[:2]
        scale = max_size / max(w, h)
        return cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

    @staticmethod
    def _imwrite_unicode(path: str, image: np.ndarray,
                         params: Optional[list] = None) -> bool:
        """imencode + tofile 写图（AUD-33905）。

        cv2.imwrite 在 Windows 上无法打开含非 ASCII 字符的路径（中文
        用户常见）并静默返回 False；全文件既有策略（_cache_preview /
        save_baked_layer）统一 imencode+tofile，save_image 导出路径此前
        是唯一例外。
        """
        ext = os.path.splitext(path)[1].lower()
        if not ext:
            return False
        success, encoded = cv2.imencode(ext, image, params if params is not None else [])
        if not success:
            return False
        encoded.tofile(path)
        return True

    @staticmethod
    def save_image(path, image, options=None):
        if image is None: return False
        if options is None: options = {"quality": 95}
        ext = os.path.splitext(path)[1].lower()

        def to_uint8(img):
            if img.dtype == np.uint8: return img
            if np.issubdtype(img.dtype, np.floating): return (np.clip(np.nan_to_num(img), 0, 1) * 255).astype(np.uint8)
            if img.dtype == np.uint16: return (img >> 8).astype(np.uint8)
            return img.astype(np.uint8)

        try:
            if ext == '.toos':
                # M2-F9：Topos Image 单帧编码（Quick Export 静帧）。
                # image = float32/uint8/uint16 RGB(A)；经序列编码器单帧
                # 落盘（档位/qp/TMET 与 CLI --tier 同锚，单一注册表）。
                return FileIO._save_toos_image(path, image, options)
            if ext == '.exr':
                # T-P1-71（libdeflate SIGILL）：默认 exr_compression 由 ZIP(3)
                # 改为 RLE(1)——opencv 轮子的 libOpenEXR 走 ZIP/PIZ 时经
                # libdeflate 派发，本机间歇性非法指令崩溃（.ips 栈
                # libdeflate_zlib_compress ← cv2.imwrite）。显式传入 2/3/4
                # 的调用方仍可自选。
                params = [cv2.IMWRITE_EXR_TYPE, int(options.get("exr_type", 1)), cv2.IMWRITE_EXR_COMPRESSION, int(options.get("exr_compression", 1))]
                return FileIO._imwrite_unicode(path, image.astype(np.float32), params)
            if ext in ['.tiff', '.tif']:
                if np.issubdtype(image.dtype, np.floating):
                    # AUD-33906：uint16 量化必须先 clip 到 [0,1]，但 >1.0
                    # 高光的裁剪不得再静默。当前依赖栈无法无损落盘 float32
                    # TIFF（opencv TIFF 编码器 float 写出实测 ±5% 精度损失、
                    # Pillow 无 RGB float 模式），故保留 uint16 路径并显式
                    # 告警，提示改用 EXR（EXR 分支无 clamp）保留 HDR 高光。
                    if image.size:
                        lo = float(np.min(image))
                        hi = float(np.max(image))
                        if hi > 1.0 or lo < 0.0:
                            logger.warning(
                                "TIFF export clips float pixels out of [0,1] "
                                "(min=%.4f, max=%.4f); use EXR to preserve "
                                "HDR highlights: %s", lo, hi, path)
                    save_data = (np.clip(image, 0, 1) * 65535).astype(np.uint16)
                else:
                    save_data = image
                return FileIO._imwrite_unicode(path, save_data)
            u8_image = to_uint8(image)
            quality = int(options.get("quality", 95))
            if ext in ['.jpg', '.jpeg']: params = [cv2.IMWRITE_JPEG_QUALITY, quality]
            elif ext == '.png': params = [cv2.IMWRITE_PNG_COMPRESSION, max(0, min(9, 9 - int(quality / 11)))]
            elif ext == '.webp': params = [cv2.IMWRITE_WEBP_QUALITY, quality]
            else: params = []
            return FileIO._imwrite_unicode(path, u8_image, params)
        except Exception:
            # AUD-33902：不再无声吞错——_save_toos_image 的 fail-closed
            # ValueError（RAW 档要求 CFA 源 / 浮点档不支持 alpha）等设计
            # 性错误文案此前对公共 API 消费方完全不可见。公共 API 契约是
            # bool（全部调用方只消费返回值，raise 会以未处理异常打断导出
            # 槽），故以 ERROR + 堆栈记录原始异常后按失败返回。
            logger.exception("save_image failed: %s", path)
            return False

    @staticmethod
    def toos_export_keeps_linear(path, options=None) -> bool:
        """单帧导出是否保持场景线性（2026-09-21 复查修复）。

        - EXR：场景线性（既有行为）；
        - .toos 浮点档：F2 契约"half 值即线性合成数据"（编码器冻结
          transfer=8 linear）——若先做 linear→sRGB 显示编码再落盘，
          重导入按 linear 解读即双重编码（画面发灰）且 >1 高光被裁；
          composite 线性 readback 必须跳过显示编码（与 EXR 同待遇）；
        - .toos 整数档：H1 写侧 sRGB（transfer=13）——正常显示编码。
        """
        ext = os.path.splitext(str(path))[1].lower()
        if ext == '.exr':
            return True
        if ext == '.toos':
            from src.shared.codec.topos_profiles import (
                get_topos_image_tier_or_none,
            )
            tier = get_topos_image_tier_or_none(
                str((options or {}).get("toos_tier", "") or "")) \
                or get_topos_image_tier_or_none("float-ultra")
            return tier is not None and tier.family == 'float'
        return False

    @staticmethod
    def _save_toos_image(path, image, options):
        """M2-F9：单帧 .toos 编码（Topos Image；临时序列目录 → 单文件）。"""
        import os as _os
        import shutil as _shutil
        import tempfile as _tempfile

        import numpy as _np

        if image is None:
            return False
        from src.shared.export.topos_encoder import ToposImageSequenceEncoder
        from src.shared.export.video_encoder import FFmpegEncoderConfig
        from src.shared.codec.topos_profiles import (
            get_topos_image_tier_or_none,
        )
        from src.shared.media.frame import DecodedFrame
        from src.shared.media.metadata import Rational

        tier = get_topos_image_tier_or_none(
            str((options or {}).get("toos_tier", "") or ""))
        if tier is None:
            tier = get_topos_image_tier_or_none("float-ultra")
        if tier.family == 'raw':
            # M3-R1：RAW 档要求 CFA 相位平面源（RAW→RAW 直出）——
            # 本入口输入是 RGB 像素（debayer 后），编码为 RAW 属语义错误
            raise ValueError(
                f"RAW 档 {tier.tier_id!r} 要求 CFA 源直出：Quick Export "
                f"静帧来自 RGB 合成域，请选择整数/浮点档")
        arr = _np.asarray(image)
        if arr.ndim == 2:
            arr = _np.stack((arr,) * 3, axis=-1)
        h, w = arr.shape[:2]
        if arr.dtype == _np.uint8:
            data = arr.astype(_np.float32) / 255.0
        elif arr.dtype == _np.uint16:
            data = arr.astype(_np.float32) / 65535.0
        else:
            data = arr.astype(_np.float32)
        has_alpha = data.shape[-1] == 4
        if has_alpha and tier.family == 'float':
            # 2026-09-21 复查：读侧按设计拒绝 HALF+alpha（F1 白名单
            # (2,16) 无 alpha 变体，F7 钉扎测试）——写侧此前会产出
            # topos_gbraph16a16 文件而应用内无法再导入。fail-closed：
            # 明确拒绝而非静默写不可读文件。
            raise ValueError(
                f"浮点档 {tier.tier_id!r} 暂不支持 alpha 通道：HALF+alpha "
                f"读侧白名单未开放（F1 设计），请去除 alpha 或选整数档")
        frame = DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            data=_np.ascontiguousarray(data),
            pixel_format="rgba" if has_alpha else "rgb24",
            width=w, height=h,
        )
        pix_fmt = tier.pix_fmt
        if has_alpha and tier.family == 'float':
            # HALF alpha 仅 mode1（a16 名字契约；构造器硬约束）
            pix_fmt = 'topos_gbraph16a16'
        from src.shared.export.video_encoder import ColorMetadataConfig

        color_metadata = ColorMetadataConfig(
            primaries='bt709', matrix='bt709', range='full',
            # M3-H1：静图写侧默认 sRGB EOTF（CICP 13，v1.8 minor≥7 载体）
            # ——图片默认 sRGB（写侧显式标注）；浮点/RAW 档在编码器内由
            # 冻结对覆写（linear 8 / LOG0 20）。
            transfer='iec61966_2_1',
        )
        cfg = FFmpegEncoderConfig(
            codec="topos_image_sequence", width=w, height=h,
            fps=Rational(24, 1),
            # 档位决定 pix_fmt/qp（编码器按注册表 coerce；E3 单一取名）
            pix_fmt=pix_fmt, profile=tier.tier_id,
            crf=int(tier.qp), has_alpha=has_alpha,
            color_metadata=color_metadata,
            # M3-H2：源 ICC profile（调用方检测后以 bytes 注入）
            icc_profile=(options or {}).get("icc_profile") or None,
        )
        tmp_dir = _tempfile.mkdtemp(prefix="toos_export_")
        # AUD-33904：open()/encode_frame() 抛错时此前不保证 close()，
        # 绕过编码器"close 覆盖全部退出路径"的资源契约（进程级
        # slice-threads 偏好注销、pipeline 排空、GOP context 释放）。
        # 异常路径改走 abort()（编码器设计的取消路径：丢弃未定稿数据
        # 并释放全部 native 资源）；abort 自身失败仅告警，不掩盖主错误。
        enc: Optional[ToposImageSequenceEncoder] = None
        finalized = False
        try:
            enc = ToposImageSequenceEncoder(tmp_dir, cfg)
            enc.open()
            enc.encode_frame(frame)
            enc.close()
            finalized = True
            produced = _os.path.join(tmp_dir, "frame_0001.toos")
            if not _os.path.exists(produced):
                return False
            _shutil.move(produced, path)
            return _os.path.exists(path)
        except Exception:
            if enc is not None and not finalized:
                try:
                    enc.abort()
                except Exception as cleanup_err:
                    logger.warning(
                        "toos export: encoder cleanup after failure: %s",
                        cleanup_err)
            raise
        finally:
            _shutil.rmtree(tmp_dir, ignore_errors=True)
