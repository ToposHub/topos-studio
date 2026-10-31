import os
import subprocess
import shutil
import platform
import tempfile
import hashlib
import json
import ctypes
import time
import threading
import unicodedata
from PIL import Image, ImageOps
import utils
import logging

logger = logging.getLogger(__name__)
import const

# 系统文件黑名单 (macOS 和 Windows 系统文件)
SYSTEM_FILE_BLACKLIST = {
    'desktop.ini', '.ds_store', 'thumbs.db', '.localized',
    '._.trashes', '.fseventsd', '.spotlight-v100', '.trashes',
    '$recycle.bin', 'system volume information'
}

# 纯文本文件格式（不生成预览图，使用图标显示）
TEXT_FILE_EXTENSIONS = {'.txt', '.log', '.md', '.markdown', '.csv', '.rtf', '.rtfd'}

# 文件格式定义
EXT_VIDEO = {'.mov', '.mp4', '.m4v', '.avi', '.mkv', '.mxf', '.flv', '.webm', '.mpg', '.mpeg', '.m1v', '.m2v', '.mp2', '.mpv', '.3gp', '.3g2', '.ts', '.mts', '.m2ts', '.vob', '.ogv', '.divx', '.xvid', '.wmv', '.asf', '.rm', '.rmvb', '.dv', '.f4v', '.qt', '.yuv', '.c3d', '.topos'}
EXT_IMAGE_TOPOS = {'.toos'}
EXT_IMAGE_PRO = {'.exr', '.dpx', '.cin', '.r3d', '.ari', '.arx', '.hdr', '.pic', '.sgi', '.tga', '.rgb', '.rgba', '.bw', '.tiff', '.tif', '.jp2', '.j2k', '.jpf', '.jpx', '.jpm', '.mj2'}
EXT_DESIGN = {'.ai', '.eps', '.pdf', '.psd', '.psb', '.cdr', '.svg', '.wmf', '.emf', '.icns', '.ico', '.cur', '.sketch', '.fig', '.xd'}
EXT_DOCS = {'.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.key', '.pages', '.numbers', '.rtf', '.rtfd', '.csv', '.md', '.markdown', '.html', '.xml', '.json', '.txt', '.log', '.pdf'}
EXT_CODE = {'.py', '.js', '.css', '.html', '.php', '.java', '.c', '.cpp', '.h', '.cs', '.go', '.rb', '.rs', '.swift', '.kt', '.sql', '.sh', '.bash', '.yaml', '.toml', '.ini', '.cfg', '.conf', '.json', '.xml', '.plist'}
EXT_AUDIO = {'.mp3', '.aac', '.wav', '.aiff', '.flac', '.alac', '.ogg', '.wma', '.m4a', '.m4b', '.m4p', '.ac3', '.dts', '.amr', '.au', '.mid', '.midi'}
EXT_ARCHIVE = {'.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz', '.iso', '.dmg', '.img', '.pkg'}
EXT_RAW_PHOTO = {'.cr2', '.cr3', '.nef', '.orf', '.raf', '.sr2', '.arw', '.rw2', '.dng', '.pef', '.mos', '.mef', '.fff', '.3fr', '.kdc', '.mrw', '.erf', '.srw'}
EXT_IMAGE_NORMAL = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.heic', '.heif', '.avif', '.pict', '.pct', '.xbm', '.xpm', '.pcx'}
ALL_SUPPORTED = EXT_VIDEO | EXT_IMAGE_TOPOS | EXT_IMAGE_PRO | EXT_DESIGN | EXT_DOCS | EXT_CODE | EXT_AUDIO | EXT_ARCHIVE | EXT_RAW_PHOTO | EXT_IMAGE_NORMAL

# [OPTIMIZATION] 缩略图生成失败缓存，防止无限重试
# 存储格式: {path: (timestamp, error_reason)}
_thumb_failure_cache = {}
_thumb_failure_cache_lock = threading.Lock()
_FAILURE_CACHE_TTL = const.THUMB_FAILURE_TTL  # 失败缓存保留时长（[ARCH-5] 见 const.py）

# 视频三档尺寸/质量（small=卡片，large=大卡片，preview=灯箱）
VIDEO_SIZE_SPECS = {'small': (300, 60), 'large': (600, 70), 'preview': (1920, 80)}


def register_failure(path: str, reason: str):
    """注册缩略图生成失败，防止短时间内重复尝试"""
    with _thumb_failure_cache_lock:
        _thumb_failure_cache[path] = (time.time(), reason)


def check_failure_cache(path: str) -> bool:
    """检查路径是否在失败缓存中，如果是则应该跳过"""
    with _thumb_failure_cache_lock:
        if path in _thumb_failure_cache:
            timestamp, _ = _thumb_failure_cache[path]
            if time.time() - timestamp < _FAILURE_CACHE_TTL:
                return True  # 在缓存期内，应该跳过
            else:
                # 过期，移除
                del _thumb_failure_cache[path]
    return False


def clear_failure(path: str):
    """[UX-3] 用户手动重试时移除失败缓存，允许重新排队生成"""
    with _thumb_failure_cache_lock:
        _thumb_failure_cache.pop(path, None)


def should_skip_file(filename: str) -> tuple:
    """
    检查文件是否应该跳过缩略图生成。
    返回 (should_skip: bool, reason: str or None)
    """
    # 检查系统文件黑名单 (不区分大小写)
    lower_name = filename.lower()
    if lower_name in SYSTEM_FILE_BLACKLIST:
        return True, 'system_file'

    # 检查 macOS 资源分支文件 (以 ._ 开头)
    if filename.startswith('._'):
        return True, 'macos_resource'

    ext = os.path.splitext(filename)[1].lower()

    # 检查纯文本文件（不生成预览图，使用图标显示）
    if ext in TEXT_FILE_EXTENSIONS:
        return True, 'text_file'

    # 检查代码文件
    if ext in EXT_CODE:
        return True, 'code_file'

    # [U7/GAP-11] PDF 放行走 QuickLook/PyMuPDF 首页预览：此前整个 EXT_DOCS
    # 被跳过 → 前端 PREVIEW_FORMATS 含 pdf 却永远拿不到图（预览断裂）
    if ext == '.pdf':
        return False, None

    # 检查其余文档文件跳过缩略图生成，直接使用图标
    # 原因：1. qlmanage 超时风险高 2. 跨平台兼容性差 3. 图标展示更美观统一
    if ext in EXT_DOCS:
        return True, 'doc_file'

    return False, None

class ThumbnailEngine:
    # 类级缓存：ffmpeg 编码器探测结果（进程内只探测一次）
    _encoder_cache = {}

    def __init__(self):
        self.system = platform.system()
        self.ffmpeg_path = utils.get_tool_path('ffmpeg')
        # [DEBUG] 明确打印 FFmpeg 加载状态，方便排查外接驱动上的执行权限或路径问题
        self._has_webp_encoder = False
        if self.ffmpeg_path:
            logger.info(f"[ThumbEngine] FFmpeg found and loaded: {self.ffmpeg_path}")
            # 自定义构建的 ffmpeg（带 libtopos）可能没有 libwebp。
            # 每张图先试 WebP 再回落 PNG 是双倍进程开销，这里只探测一次。
            # ffmpeg 本身缺失时不要再打"无 libwebp"——那只是误报噪音。
            self._has_webp_encoder = self._check_encoder('libwebp')
            if not self._has_webp_encoder:
                logger.warning("[ThumbEngine] ffmpeg 无 libwebp 编码器，视频缩略图走 PNG+Pillow 直转")
        else:
            logger.error("[ThumbEngine] FFmpeg NOT found! Video thumbnails will fail.")

        self.qlmanage_path = utils.get_tool_path('qlmanage') or '/usr/bin/qlmanage'

        # 定义兜底用的全局缓存 (位于项目根目录，便携且不污染系统)
        project_root = os.path.dirname(os.path.abspath(__file__))
        self.global_cache = os.path.join(project_root, ".assetdeck", "thumbnails")

    def _check_encoder(self, name: str) -> bool:
        """探测 ffmpeg 是否带指定编码器（结果按二进制路径缓存）。"""
        if not self.ffmpeg_path:
            return False
        key = (self.ffmpeg_path, name)
        if key not in ThumbnailEngine._encoder_cache:
            try:
                res = subprocess.run(
                    [self.ffmpeg_path, '-hide_banner', '-encoders'],
                    capture_output=True, text=True, encoding='utf-8',
                    errors='ignore', timeout=10)
                ThumbnailEngine._encoder_cache[key] = bool(
                    res.returncode == 0 and
                    any(line.split()[2:3] == [name]
                        for line in res.stdout.splitlines() if line.startswith(' V'))
                )
            except Exception:
                ThumbnailEngine._encoder_cache[key] = False
        return ThumbnailEngine._encoder_cache[key]

    def get_cache_info(self, source_dir):
        """获取缩略图缓存路径（优先本地 .cache 目录，自动创建）"""
        local_cache = os.path.join(source_dir, '.cache')
        try:
            os.makedirs(local_cache, exist_ok=True)
            # 写入 .nomedia 防止媒体库扫描
            nomedia = os.path.join(local_cache, '.nomedia')
            if not os.path.exists(nomedia):
                with open(nomedia, 'w') as f: pass
            return local_cache, True
        except:
            # 只有在本地目录创建失败时才使用全局缓存
            if not os.path.exists(self.global_cache):
                try: os.makedirs(self.global_cache, exist_ok=True)
                except: pass
            return self.global_cache, False

    def peek_cache_info(self, source_dir):
        """[PERF-1] 只读版 get_cache_info：目录浏览（GET）路径上不产生
        mkdir / 写 .nomedia 副作用。.cache 不存在 ⇒ 该目录必然无本地缓存。"""
        local_cache = os.path.join(source_dir, '.cache')
        if os.path.isdir(local_cache):
            return local_cache, True
        return self.global_cache, False

    def get_thumb_path(self, source_path: str, cache_info: tuple = None, size_type: str = 'small',
                       scan_cache: dict = None) -> str:
        """
        [智能路径计算]
        1. 检查是否存在：先看本地 .cache 有没有？再看全局 .assetdeck 有没有？有就直接返回。
        2. 如果都不存在：根据写入权限决定新图存哪里。

        scan_cache: [PERF-1] 调用方（如目录列表）传入的共享 dict——同一
        .cache 目录只扫描一次，避免"每文件一次模糊扫描"的 O(N×M) 开销。
        """
        # 统一 NFC 规范化并统一斜杠，解决 macOS NFD 编码与 ExFAT 路径不一致导致的外接硬盘缓存命中失败问题
        source_path = unicodedata.normalize('NFC', source_path.replace('\\', '/'))
        
        source_dir = os.path.dirname(source_path)
        filename = os.path.basename(source_path)
        
        # 调整不同尺寸的后缀，确保 small/large/preview 隔离
        if size_type == 'large': 
            suffix = '_lg.webp'
        elif size_type == 'preview': 
            suffix = '_preview.webp' # 预览图后缀 (用于空格预览/灯箱)
        else: 
            suffix = '.webp' # 默认为 small 尺寸
        
        # 1. 预计算两种可能的路径
        # A. 本地路径 (Source/.cache/Name.webp)
        local_dir = os.path.join(source_dir, '.cache')
        local_path = os.path.join(local_dir, filename + suffix)
        
        # B. 全局路径 (Global/Hash.webp)
        # 统一使用正斜杠计算 Hash，确保跨平台一致
        norm_path = source_path.replace('\\', '/')
        name_hash = hashlib.md5(norm_path.encode('utf-8')).hexdigest()
        global_path = os.path.join(self.global_cache, f"{name_hash}{suffix}")
        
        # 2. [读取优先] 哪里有图就用哪里 (解决判定飘忽导致的丢失)
        # 策略 A: 直接检查本地路径 (NFC)
        if os.path.exists(local_path):
            return local_path
            
        # 策略 B: 尝试 NFD 形式作为兜底 (ExFAT 在 Mac 上常以 NFD 形式存储或由系统返回)
        nfd_local_path = unicodedata.normalize('NFD', local_path)
        if nfd_local_path != local_path and os.path.exists(nfd_local_path):
            return nfd_local_path

        # 策略 C: [超级兜底] 目录扫描模糊匹配 (解决 ExFAT 上极其顽固的编码问题)
        # 当直接路径检查都失败时，扫描 .cache 目录并比较 NFC 规范化后的文件名
        if os.path.exists(local_dir):
            try:
                target_name_nfc = unicodedata.normalize('NFC', os.path.basename(local_path))
                listing = None
                if scan_cache is not None:
                    listing = scan_cache.get(local_dir)
                if listing is None:
                    listing = {}
                    with os.scandir(local_dir) as it:
                        for entry in it:
                            if entry.is_file() and entry.name.endswith('.webp'):
                                # 规范化目录中的文件名后进行比对
                                listing[unicodedata.normalize('NFC', entry.name)] = entry.path
                    if scan_cache is not None:
                        scan_cache[local_dir] = listing
                hit = listing.get(target_name_nfc)
                if hit:
                    return hit
            except OSError:
                pass

        # 策略 D: 检查全局缓存
        if os.path.exists(global_path):
            return global_path
            
        # 3. [写入决策] 如果都不存在，决定新图生成位置
        # [BUG-P2-11] 只读探测：GET 路径不产生 mkdir/写 .nomedia 副作用。
        # [U10] 此前 is_local 算出后从未使用、恒返回本地路径——只读卷/
        # 网络卷上写出必抛 Read-only file system → register_failure →
        # 每小时重试每小时失败，缩略图永远生成不出来。改为源目录不可写时
        # 写全局 hash 缓存；可写卷仍保持 .cache 跟随源目录的设计
        if not cache_info:
            cache_info = self.peek_cache_info(source_dir)
        _cache_dir, is_local = cache_info
        if is_local or os.access(source_dir, os.W_OK):
            return local_path
        return global_path

    def ensure_cache_ready(self, cache_path):
        """确保缓存目录存在，并应用隐藏/优化属性"""
        cache_dir = os.path.dirname(cache_path)
        if not os.path.exists(cache_dir):
            try:
                os.makedirs(cache_dir, exist_ok=True)

                # 1. 写入 .nomedia 防止媒体库扫描
                nomedia = os.path.join(cache_dir, '.nomedia')
                if not os.path.exists(nomedia):
                    with open(nomedia, 'w') as f: pass

                # 2. Windows 下设置隐藏属性
                if self.system == 'Windows':
                    FILE_ATTRIBUTE_HIDDEN = 0x02
                    try:
                        ctypes.windll.kernel32.SetFileAttributesW(cache_dir, FILE_ATTRIBUTE_HIDDEN)
                    except: pass
            except: pass

    def video_thumb_paths(self, source_path: str, cache_info: tuple = None) -> dict:
        """三尺寸目标路径（基于 small 命中路径推导后缀，含 NFD 兜底语义）。"""
        base = self.get_thumb_path(source_path, cache_info=cache_info, size_type='small')
        stem = base[:-len('.webp')] if base.endswith('.webp') else base
        return {'small': base, 'large': stem + '_lg.webp', 'preview': stem + '_preview.webp'}

    def generate_video_all_sizes(self, source_path: str, include_preview: bool = False) -> dict:
        """视频缩略图：一次解码产出缺失尺寸（返回 {size_type: 是否落盘}）。

        preview(1920px) 编码成本高，默认不随浏览请求生成——仅当灯箱
        请求 size=preview（或它已存在）时才纳入，保持卡片浏览冷启动最快。

        策略：
        1. 插件原生 provider（如 .topos 的 libtopos 解码）优先——不依赖带
           TPIC 补丁的 ffmpeg；provider 只产部分尺寸时余下回落 ffmpeg。
        2. FFmpeg 一次解码：有 libwebp 时 filter_complex 多输出直出；
           否则输出最大尺寸的 MJPEG 单帧，Pillow 链式派生全部尺寸
           （杜绝"先试 WebP 失败再重跑 PNG"的双倍进程开销）。
        """
        paths = self.video_thumb_paths(source_path)
        result = {s: os.path.exists(p) for s, p in paths.items()}
        missing = {s: p for s, p in paths.items() if not result[s]}
        if not include_preview:
            missing.pop('preview', None)
        if not missing:
            return result
        for p in missing.values():
            self.ensure_cache_ready(p)

        ext = os.path.splitext(source_path)[1].lower()
        provider = getattr(utils, 'THUMBNAIL_PROVIDERS', {}).get(ext)
        if provider is not None:
            try:
                provider(source_path, paths['small'])
            except Exception:
                pass
            for s, p in list(missing.items()):
                if os.path.exists(p):
                    result[s] = True
                    del missing[s]
            if not missing:
                self.write_sidecar(source_path, paths['small'])
                return result

        if self.ffmpeg_path:
            if self._generate_video_ffmpeg(source_path, missing):
                for s, p in missing.items():
                    result[s] = os.path.exists(p)
        if result.get('small'):
            self.write_sidecar(source_path, paths['small'])
        return result

    # -- [FUNC-11] 缓存有效性 sidecar -------------------------------------
    # 缓存 key 只含路径，同名重导出（mtime/size 变化）后缩略图永久陈旧。
    # 生成成功时写 `<thumb>.v` 记录源 mtime+size+hash；读取端 is_stale() 比对。

    # -- [BUG-P1-02] 原子落盘 ---------------------------------------------
    # 此前音频波形/Pillow/QuickLook/rawpy 四条路径直写 dst：磁盘满或 ffmpeg
    # 超时（kill）会留下残缺图，路由命中即返回、且无 sidecar 时 is_stale 恒
    # False——坏图永久驻留。统一改为 dst.part 临时写 + os.replace 原子替换。

    @staticmethod
    def _atomic_save_webp(img, dst: str, quality: int) -> None:
        part = dst + '.part'
        try:
            img.save(part, 'WEBP', quality=quality)
            os.replace(part, dst)
        finally:
            if os.path.exists(part):
                try: os.remove(part)
                except OSError: pass

    def _atomic_save_webp_file(self, src_img_path: str, dst: str, quality: int) -> None:
        """从已存在的图片文件转存 WebP（QuickLook/PNG 回落路径）。"""
        with Image.open(src_img_path) as img:
            self._atomic_save_webp(img, dst, quality)

    def write_sidecar(self, source_path: str, thumb_path: str):
        try:
            st = os.stat(source_path)
            digest = hashlib.sha256()
            with open(source_path, 'rb') as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(chunk)
            payload = {
                "schema_version": 1,
                "source_mtime_ns": st.st_mtime_ns,
                "source_size": st.st_size,
                "source_sha256": digest.hexdigest(),
            }
            sidecar = thumb_path + '.v'
            part = sidecar + '.part'
            with open(part, 'w', encoding='utf-8') as f:
                json.dump(payload, f, sort_keys=True, separators=(',', ':'))
                f.write('\n')
            os.replace(part, sidecar)
        except OSError:
            try:
                if os.path.exists(thumb_path + '.v.part'):
                    os.remove(thumb_path + '.v.part')
            except OSError:
                pass

    def is_stale(self, source_path: str, thumb_path: str) -> bool:
        """sidecar 存在且与当前源 stat 不匹配 → 缓存失效。

        无 sidecar 的历史缓存视为有效，避免升级后一次性全量重建。
        """
        sp = thumb_path + '.v'
        if not os.path.exists(sp):
            return False
        try:
            st = os.stat(source_path)
            with open(sp, encoding='utf-8') as f:
                raw = f.read().strip()
            # Accept the pre-O3 sidecar format so existing caches remain usable.
            if raw.startswith('{'):
                payload = json.loads(raw)
                return (
                    int(payload.get('source_mtime_ns', -1)) != st.st_mtime_ns
                    or int(payload.get('source_size', -1)) != st.st_size
                    or not payload.get('source_sha256')
                )
            return raw != f"{st.st_mtime_ns}:{st.st_size}"
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return True

    # -- 失败缓存（模块级函数的实例包装：main.py 经 engine 实例调用，
    #    此前 AttributeError 被裸 except 吞掉，失败缓存从未生效） --

    def register_failure(self, path: str, reason: str):
        register_failure(path, reason)

    def check_failure_cache(self, path: str) -> bool:
        return check_failure_cache(path)

    def clear_failure(self, path: str):
        clear_failure(path)

    def _generate_video_ffmpeg(self, src: str, targets: dict) -> bool:
        """FFmpeg 一次解码产出多尺寸（targets: {size_type: dst_path}）。

        探测参数先用默认值（对 MOV/MP4 足够，避免每张图读 50MB），
        失败才升大探测重试一次——兼容 TS/异常流。
        """
        if self._has_webp_encoder:
            return self._video_multi_output(src, targets)
        return self._video_via_image(src, targets)

    def _run_ffmpeg_with_nfd_retry(self, cmd: list, src: str, timeout: int = const.FFMPEG_TIMEOUT_S):
        """执行 ffmpeg；macOS ExFAT NFD 文件名失败时用 NFD 路径重试一次。"""
        res = subprocess.run(cmd, capture_output=True, text=True,
                             encoding='utf-8', errors='ignore', timeout=timeout)
        if res.returncode != 0 and self.system == 'Darwin':
            nfd = unicodedata.normalize('NFD', src)
            if nfd != src:
                cmd2 = [nfd if a == src else a for a in cmd]
                res = subprocess.run(cmd2, capture_output=True, text=True,
                                     encoding='utf-8', errors='ignore', timeout=timeout)
        return res

    def _video_via_image(self, src: str, targets: dict) -> bool:
        """ffmpeg 无 libwebp 时的路径：最大尺寸 MJPEG 单帧 → Pillow 链式派生。

        一次解码 + 一次编码；method=0 的 WebP 编码比默认快数倍，
        300/600px 卡片图肉眼无差异。
        """
        max_px = max(VIDEO_SIZE_SPECS[s][0] for s in targets)
        for probe_heavy in (False, True):
            tmp = None
            try:
                fd, tmp = tempfile.mkstemp(suffix='.jpg')
                os.close(fd)
                cmd = [self.ffmpeg_path, '-y', '-nostdin', '-ss', '1']
                if probe_heavy:
                    cmd += ['-probesize', '50M', '-analyzeduration', '100M']
                cmd += ['-i', src, '-vf', f'scale={max_px}:-1', '-frames:v', '1',
                        '-c:v', 'mjpeg', '-q:v', '3', '-f', 'image2', tmp]
                res = self._run_ffmpeg_with_nfd_retry(cmd, src)
                if res.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
                    # 先写同目录临时文件再原子替换，避免前端命中写入中的半成品
                    with Image.open(tmp) as img:
                        img.load()  # tmp 随后删除，数据先落内存
                        # thumbnail 原地缩小，必须从大到小派生
                        for s, dst in sorted(targets.items(),
                                             key=lambda kv: -VIDEO_SIZE_SPECS[kv[0]][0]):
                            px, q = VIDEO_SIZE_SPECS[s]
                            img.thumbnail((px, px))
                            part = dst + '.part'
                            img.save(part, 'WEBP', quality=q, method=0)
                            os.replace(part, dst)
                    return True
                logger.warning(f"[ThumbEngine] 视频缩略图未生成 "
                            f"(probe_heavy={probe_heavy}, code={res.returncode}): "
                            f"{os.path.basename(src)}")
            except subprocess.TimeoutExpired:
                logger.error(f"[FFmpeg Timeout] {src}")
            except Exception as e:
                logger.error(f"[FFmpeg Engine Error] {src}: {e}")
            finally:
                if tmp and os.path.exists(tmp):
                    try: os.remove(tmp)
                    except: pass
        return False

    def _video_multi_output(self, src: str, targets: dict) -> bool:
        """ffmpeg 带 libwebp 时：filter_complex 一次解码多尺寸直出 WebP。"""
        order = list(targets.items())
        for probe_heavy in (False, True):
            outs = []
            try:
                for s, _dst in order:
                    fd, tmp = tempfile.mkstemp(suffix='.webp')
                    os.close(fd)
                    outs.append((s, tmp))
                cmd = [self.ffmpeg_path, '-y', '-nostdin', '-ss', '1']
                if probe_heavy:
                    cmd += ['-probesize', '50M', '-analyzeduration', '100M']
                cmd += ['-i', src, '-filter_complex',
                        ';'.join(f'[0:v]scale={VIDEO_SIZE_SPECS[s][0]}:-1[v{i}]'
                                 for i, (s, _) in enumerate(outs))]
                for i, (s, tmp) in enumerate(outs):
                    cmd += ['-map', f'[v{i}]', '-frames:v', '1', '-c:v', 'libwebp',
                            '-quality', str(VIDEO_SIZE_SPECS[s][1]), tmp]
                res = self._run_ffmpeg_with_nfd_retry(cmd, src)
                valid = [tmp for _, tmp in outs
                         if os.path.exists(tmp) and os.path.getsize(tmp) > 0]
                if res.returncode == 0 and len(valid) == len(outs):
                    import shutil
                    # [BUG-P1-02] tmp 可与 dst 跨卷，shutil.move 退化为非原子
                    # copy——中断会留半成品 dst。统一 copy 到 dst.part 再原子替换。
                    for (s, dst), (_, tmp) in zip(order, outs):
                        part = dst + '.part'
                        try:
                            shutil.copyfile(tmp, part)
                            os.replace(part, dst)
                        finally:
                            if os.path.exists(part):
                                try: os.remove(part)
                                except OSError: pass
                    return True
                logger.warning(f"[ThumbEngine] 视频多尺寸生成未全成 "
                            f"(probe_heavy={probe_heavy}, code={res.returncode}): "
                            f"{os.path.basename(src)}")
            except subprocess.TimeoutExpired:
                logger.error(f"[FFmpeg Timeout] {src}")
            except Exception as e:
                logger.error(f"[FFmpeg Engine Error] {src}: {e}")
            finally:
                for _, tmp in outs:
                    if tmp and os.path.exists(tmp):
                        try: os.remove(tmp)
                        except: pass
        return False

    def generate(self, src_path, thumb_path, size_type='small'):
        """智能生成入口"""
        if os.path.exists(thumb_path): return True
        
        # [OPTIMIZATION] 检查失败缓存，避免重复尝试
        if check_failure_cache(src_path):
            return False

        # 确保目录结构和属性就绪
        self.ensure_cache_ready(thumb_path)
        
        # 确定目标尺寸与质量
        # small: 300px, quality 60 (速度优先)
        # large: 600px, quality 70 (清晰度优先)
        # preview: 1920px (全屏预览)
        quality = 60
        if size_type == 'large': 
            target_size = 600
            quality = 70
        elif size_type == 'preview': 
            target_size = 1920
            quality = 80 # 预览图稍微高一点质量
        else: 
            target_size = 300 # small
        
        ext = os.path.splitext(src_path)[1].lower()
        success = False

        # Native providers (for example Topos .toos) must run before generic
        # Pillow/QuickLook/FFmpeg fallbacks.  The provider API historically
        # accepted two arguments, so retain that form here.
        provider = getattr(utils, 'THUMBNAIL_PROVIDERS', {}).get(ext)
        if provider is not None:
            try:
                success = bool(provider(src_path, thumb_path))
            except Exception as exc:
                logger.warning("thumbnail provider failed for %s: %s", src_path, exc)
            if success:
                self.write_sidecar(src_path, thumb_path)
                return True

        # 策略 1: 视频与专业影像 -> 优先 FFmpeg
        # (FFmpeg 截取视频帧比 QuickLook 更可控，不会有播放图标遮挡)
        if (ext in EXT_VIDEO or ext in EXT_IMAGE_PRO) and self.ffmpeg_path:
            success = self._generate_ffmpeg(src_path, thumb_path, target_size, quality=quality, is_video=(ext in EXT_VIDEO))

        # [CRITICAL Fix] 视频文件绝对禁止回落到 QuickLook
        # QuickLook 处理大视频极其缓慢且容易超时，导致生成循环。如果 FFmpeg 失败，直接视为失败或用 Pillow 兜底。
        if ext in EXT_VIDEO:
            return success

        # 策略 2: macOS 原生 QuickLook -> 处理文档、RAW、设计稿、HEIC
        # 这是处理 Apple 生态格式和 Adobe 格式的神器
        if not success and self.system == 'Darwin':
            # 排除普通图片(Pillow更快)和音频(除非需要图标)
            if ext not in EXT_IMAGE_NORMAL or ext in {'.heic', '.heif'}:
                success = self._generate_quicklook(src_path, thumb_path, target_size, quality=quality)

        # 策略 2.5: RAW 照片 -> rawpy 解码 (跨平台，Windows/Linux 必须依赖此路径)
        if not success and ext in EXT_RAW_PHOTO:
            success = self._generate_rawpy(src_path, thumb_path, target_size, quality=quality)

        # 策略 3: 音频 -> FFmpeg 波形图
        if not success and ext in EXT_AUDIO and self.ffmpeg_path:
            success = self._generate_audio_waveform(src_path, thumb_path, target_size)

        # [U7/GAP-11/15] 设计稿/文档兜底（非 macOS 或 QuickLook 失败时；
        # 库缺失/未安装时各方法静默返回 False，行为退回原状）
        if not success and ext == '.pdf':
            success = self._generate_pymupdf(src_path, thumb_path, target_size, quality)
        if not success and ext in {'.psd', '.psb'}:
            success = self._generate_psd_tools(src_path, thumb_path, target_size, quality)
        if not success and ext in {'.ai', '.eps'}:
            success = self._generate_ghostscript(src_path, thumb_path, target_size, quality)

        # 策略 4: 普通图片/兜底 -> Pillow
        if not success:
            success = self._generate_pillow(src_path, thumb_path, target_size, quality=quality)

        if success:
            self.write_sidecar(src_path, thumb_path)
        return success

    def _generate_pymupdf(self, src, dst, size, quality=60):
        """[U7/GAP-11] PyMuPDF 渲染 PDF 首页（跨平台兜底；未安装返回 False）。"""
        try:
            import fitz  # PyMuPDF
        except ImportError:
            return False
        try:
            with fitz.open(src) as doc:
                page = doc.load_page(0)
                zoom = max(1.0, size / max(page.rect.width, 1.0))
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                img = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
                img.thumbnail((size, size))
                self._atomic_save_webp(img, dst, quality)
            return True
        except Exception as e:
            register_failure(src, f'pymupdf_{str(e)[:30]}')
            return False

    def _generate_psd_tools(self, src, dst, size, quality=60):
        """[U7/GAP-15] psd-tools 合成 PSD 预览（跨平台兜底；未安装返回 False）。"""
        try:
            from psd_tools import PSDImage
        except ImportError:
            return False
        try:
            psd = PSDImage.open(src)
            img = psd.composite()
            if img is None:
                return False
            if img.mode not in ('RGB', 'RGBA'):
                img = img.convert('RGB')
            img.thumbnail((size, size))
            self._atomic_save_webp(img, dst, quality)
            return True
        except Exception as e:
            register_failure(src, f'psdtools_{str(e)[:30]}')
            return False

    def _generate_ghostscript(self, src, dst, size, quality=60):
        """[U7/GAP-15] ghostscript 兜底 ai/eps 首页（未安装返回 False）。"""
        gs = shutil.which('gs') or shutil.which('gswin64c')
        if not gs:
            return False
        try:
            with tempfile.TemporaryDirectory() as td:
                out = os.path.join(td, 'page.png')
                res = max(36, min(300, int(size * 72 / 800)))
                subprocess.run(
                    [gs, '-dSAFER', '-dBATCH', '-dNOPAUSE', '-sDEVICE=png16m',
                     f'-r{res}', '-dFirstPage=1', '-dLastPage=1',
                     f'-sOutputFile={out}', src],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    check=True, timeout=30)
                with Image.open(out) as img:
                    img.thumbnail((size, size))
                    self._atomic_save_webp(img, dst, quality)
            return True
        except Exception as e:
            register_failure(src, f'gs_{str(e)[:30]}')
            return False

    # [U7/GAP-10] 视频胶片条（6 帧横排 tile）：卡片悬停 scrub 与属性面板
    # 导航共用；生成一次后缓存于源目录 .cache，切换 <100ms（纯样式更新）
    def get_filmstrip_path(self, source_path: str) -> str:
        source_path = unicodedata.normalize('NFC', str(source_path).replace('\\', '/'))
        # [U9] 文件名含扩展名：剥掉扩展名会让 a.mp4 与 a.mov（代理片对很
        # 常见）共用一张 strip，第二个视频显示第一个视频的帧
        # [U10] 此前整名 replace('.','_') 非单射——clip.v2.mp4 与 clip_v2.mp4
        # 映射到同名 strip。文件名里的点本身合法，直接用全名保证单射
        base = os.path.basename(source_path)
        return os.path.join(os.path.dirname(source_path), '.cache', base + '_strip.jpg')

    def generate_filmstrip(self, src: str, dst: str, frames: int = 6, width: int = 320) -> bool:
        if not self.ffmpeg_path:
            return False
        if os.path.exists(dst):
            return True
        tmp = dst + '.part.jpg'
        try:
            dur = self._probe_duration(src)
            if not dur or dur <= 0:
                register_failure(src + ':strip', 'no_duration')
                return False
            # [U10] makedirs 此前在 try 之外——只读卷直接把 OSError 抛给
            # /api/thumb/strip → 500（对比普通缩略图路径静默 204）
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            cmd = [self.ffmpeg_path, '-y', '-i', src,
                   '-vf', f'fps={frames}/{dur:.3f},scale={width}:-2,tile={frames}x1',
                   '-frames:v', '1', tmp]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=True, timeout=const.FFMPEG_TIMEOUT_S)
            os.replace(tmp, dst)
            return True
        except Exception as e:
            logger.warning(f"[U7/GAP-10] 胶片条生成失败 {src}: {e}")
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            # [U10] 失败进缓存（strip 专属键，不与普通缩略图互伤）：
            # 损坏/不可抽帧的视频此前每次悬停都同步重跑 ffprobe+ffmpeg
            # （90s 超时占死 waitress 线程）
            register_failure(src + ':strip', f'strip_{str(e)[:30]}')
            return False

    def _probe_duration(self, src: str):
        ffp = utils.get_tool_path('ffprobe')
        if not ffp:
            return None
        try:
            out = subprocess.run(
                [ffp, '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', src],
                capture_output=True, text=True, timeout=15)
            return float(out.stdout.strip())
        except Exception:
            return None

    def _generate_quicklook(self, src, dst, size, quality=60):
        """
        [核心] 调用 macOS 系统级预览引擎
        支持: Office, iWork, PDF, PSD, AI, RAW, HEIC, Text, Code, Archive
        
        [OPTIMIZATION] 针对文档文件设置更短的超时时间（10秒），快速失败
        """
        ext = os.path.splitext(src)[1].lower()
        # 文档文件更容易超时，设置更短的超时
        is_doc = ext in EXT_DOCS and ext not in EXT_IMAGE_PRO
        timeout = 10 if is_doc else 30  # 文档10秒，其他30秒
        
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                # qlmanage -t -s <size> -o <tmp> <file>
                cmd = [
                    self.qlmanage_path, '-t', '-s', str(size),
                    '-o', tmp_dir, src
                ]
                # 屏蔽输出
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=timeout)

                # qlmanage 会生成 "filename.png" 或 "filename.jpg"
                generated_file = None
                # Normalize both to NFC for comparison to handle macOS NFD filenames
                target_basename = unicodedata.normalize('NFC', os.path.basename(src))
                for f in os.listdir(tmp_dir):
                    f_nfc = unicodedata.normalize('NFC', f)
                    if f_nfc.startswith(target_basename) or f_nfc.endswith('.png'):
                        generated_file = os.path.join(tmp_dir, f)
                        break

                if generated_file:
                    # 将生成的 PNG 转为 WebP 统一格式
                    # [BUG-P1-02] 原子写：不再直写 dst 留半成品
                    with Image.open(generated_file) as img:
                        img.thumbnail((size, size))
                        self._atomic_save_webp(img, dst, quality)
                    return True
        except subprocess.TimeoutExpired:
            logger.warning(f"[QuickLook Timeout] {src} (timeout={timeout}s)")
            register_failure(src, f'quicklook_timeout_{timeout}s')
        except Exception as e:
            logger.error(f"Thumb Gen Error [{src}]: {e}")
            register_failure(src, str(e)[:50])
        return False

    def _generate_rawpy(self, src: str, dst: str, size: int, quality: int = 60) -> bool:
        """
        [RAW 照片解码] 使用 rawpy 库读取 RAW 照片并生成缩略图。
        支持: CR2, CR3, NEF, ARW, DNG, ORF, RAF, SR2, RW2, PEF 等所有 RAW 格式。
        跨平台可用，Windows/Linux 上作为 RAW 文件的主路径。
        
        [优化] 解码一次 RAW，同时生成缩略图 + 灯箱预览图(1920px)，避免重复解码。
        """
        try:
            import rawpy
            import numpy as np
        except ImportError:
            logger.warning("[ThumbEngine] rawpy not installed, cannot decode RAW file")
            return False

        try:
            with rawpy.imread(src) as raw:
                # 使用默认参数后处理，得到 sRGB 色彩空间的图像
                rgb = raw.postprocess(
                    use_camera_wb=True,       # 使用相机白平衡
                    half_size=True,            # 半尺寸解码，速度更快
                    output_bps=8,              # 8-bit 输出
                )

            # rawpy 返回 numpy 数组，转为 PIL Image
            img = Image.fromarray(rgb, mode='RGB')

            # [灯箱预览] 同时生成 _preview.webp (1920px) 用于灯箱全屏预览
            # 仅在 preview 缓存不存在时生成，避免重复计算
            preview_size = 1920
            preview_quality = 80
            cache_dir = os.path.dirname(dst)
            src_filename = os.path.basename(src)
            preview_path = os.path.join(cache_dir, src_filename + '_preview.webp')

            if not os.path.exists(preview_path):
                preview_img = img.copy()
                preview_img.thumbnail((preview_size, preview_size))
                # [BUG-P1-02] 原子写
                self._atomic_save_webp(preview_img, preview_path, preview_quality)

            # 生成当前请求尺寸的缩略图
            img.thumbnail((size, size))
            self._atomic_save_webp(img, dst, quality)
            return True
        except Exception as e:
            logger.error(f"[Rawpy Thumb Error] {src}: {e}")
            return False

    def _generate_ffmpeg(self, src: str, dst: str, size: int, quality: int = 75, is_video: bool = True) -> bool:
        """
        [Enhanced FFmpeg Engine]
        Attempts direct WebP generation with fallback to PNG -> WebP (via Pillow).
        Also handles macOS ExFAT NFD path issues (Exit Code 183).
        """
        import shutil

        def run_ffmpeg_cmd(cmd_list, input_path):
            """Helper to run ffmpeg with NFD retry for macOS/ExFAT compatibility."""
            res = subprocess.run(cmd_list, capture_output=True, text=True, encoding='utf-8', errors='ignore', timeout=60)
            if res.returncode != 0 and self.system == 'Darwin':
                # Try NFD normalization (ExFAT on Mac often requires this for non-ASCII names)
                input_nfd = unicodedata.normalize('NFD', input_path)
                if input_nfd != input_path:
                    logger.warning(f"[FFmpeg] Code {res.returncode}, retrying with NFD path for: {os.path.basename(input_path)}")
                    # Replace input path in command list
                    new_cmd = [input_nfd if arg == input_path else arg for arg in cmd_list]
                    res = subprocess.run(new_cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore', timeout=60)
            return res

        tmp_webp = None
        tmp_png = None
        
        try:
            # Base command setup
            base_cmd = [self.ffmpeg_path, '-y']
            if is_video:
                base_cmd.extend(['-ss', '1'])
            base_cmd.extend(['-probesize', '50M', '-analyzeduration', '100M', '-i', src])

            vf = f"scale={size}:-1"
            if src.lower().endswith(('.exr', '.hdr', '.r3d', '.dng')):
                vf += ",tonemap=hable:desat=0"

            # --- Attempt 1: Direct WebP ---
            fd, tmp_webp = tempfile.mkstemp(suffix=".webp")
            os.close(fd)

            cmd_webp = base_cmd + ['-vf', vf, '-vframes', '1', '-c:v', 'libwebp', '-quality', str(quality), '-f', 'image2', tmp_webp]
            result = run_ffmpeg_cmd(cmd_webp, src)
            
            if result.returncode == 0 and os.path.exists(tmp_webp) and os.path.getsize(tmp_webp) > 0:
                # [U10] 原子写：mkstemp 在 /tmp，与 .cache 常跨卷——
                # shutil.move 退化为 copy+unlink，中断留半成品 dst
                shutil.copyfile(tmp_webp, dst + '.part')
                os.replace(dst + '.part', dst)
                os.remove(tmp_webp)
                return True

            # --- Attempt 2: PNG Fallback ---
            logger.warning(f"[ThumbEngine] FFmpeg WebP failed (Code {result.returncode}), trying PNG fallback...")
            
            fd_png, tmp_png = tempfile.mkstemp(suffix=".png")
            os.close(fd_png)

            cmd_png = base_cmd + ['-vf', vf, '-vframes', '1', '-c:v', 'png', '-f', 'image2', tmp_png]
            result_png = run_ffmpeg_cmd(cmd_png, src)
            
            if result_png.returncode == 0 and os.path.exists(tmp_png):
                try:
                    with Image.open(tmp_png) as img:
                        # [U9] 原子写：直写 dst 会在崩溃/磁盘满时留残缺图
                        self._atomic_save_webp(img, dst, quality)
                    return True
                finally:
                    try:
                        os.remove(tmp_png)
                    except OSError:
                        pass
            
            logger.error(f"[FFmpeg Engine Failed] {src} (WebP: {result.returncode}, PNG: {result_png.returncode})")
            return False

        except subprocess.TimeoutExpired:
            logger.error(f"[FFmpeg Timeout] {src}")
            return False
        except Exception as e:
            logger.error(f"[FFmpeg Engine Error] {src}: {e}")
            return False
        finally:
            for tmp in [tmp_webp, tmp_png]:
                if tmp and os.path.exists(tmp):
                    try: os.remove(tmp)
                    except: pass

    def _generate_audio_waveform(self, src, dst, size):
        """生成音频波形图"""
        # [BUG-P1-02] 原子写：ffmpeg 直写 dst 时超时（10s kill）会留半成品，
        # 且该图无 sidecar → 残缺图永久驻留。先写 .part.png 再 os.replace。
        part = dst + '.part.png'
        try:
            height = int(size * 0.6)
            cmd = [
                self.ffmpeg_path, '-y', '-i', src,
                '-filter_complex', f"showwavespic=s={size}x{height}:colors=#0d6efd",
                '-frames:v', '1', part
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=10)
            if not os.path.exists(part) or os.path.getsize(part) == 0:
                raise RuntimeError('waveform empty output')
            os.replace(part, dst)
            return True
        except Exception as e:
            if os.path.exists(part):
                try: os.remove(part)
                except OSError: pass
            logger.error(f"Thumb Gen Error [{src}]: {e}")
            return False

    def _generate_pillow(self, src, dst, size, quality=60):
        """普通图片兜底"""
        try:
            # Pillow 10.0+ 支持 HEIC (需外部库), 但这里主要用于 JPG/PNG/WebP
            with Image.open(src) as img:
                img = ImageOps.exif_transpose(img)
                if img.mode in ('CMYK', 'P', 'RGBA', 'LA'):
                    if img.mode == 'CMYK': img = img.convert('RGB')
                    # RGBA 转 RGB 保持透明背景需要处理，这里简化为直接转
                    # WebP 支持透明，所以保持 RGBA 也行

                img.thumbnail((size, size))
                # [BUG-P1-02] 原子写：不再直写 dst 留半成品
                self._atomic_save_webp(img, dst, quality)
            return True
        except Exception as e:
            logger.error(f"Thumb Gen Error [{src}]: {e}")
            register_failure(src, f'pillow: {str(e)[:50]}')
            return False

engine = ThumbnailEngine()
