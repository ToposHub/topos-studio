import os
import json
import logging
import math
import platform
import subprocess
import shutil
import threading
import sys
import time
import plistlib
from PIL import Image, ImageOps, ExifTags

# [BUG-P2-14] get_image_info 异常分支引用了从未定义的 logger（NameError 埋雷）
logger = logging.getLogger(__name__)

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

VALID_IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.hdr', '.bmp', '.tiff', '.ico', '.svg', '.toos'}
VALID_VIDEO_EXTS = {
    '.mp4', '.mov', '.avi', '.mkv', '.webm', '.m4v', '.flv', '.wmv', '.3gp',
    '.mxf', '.ts', '.mts', '.m2ts', '.vob', '.ogv', '.qt', '.divx', '.f4v',
    '.topos'
}

# —— 扩展点：插件可按扩展名注册缩略图 / 元数据提供者（video_pro 等） ——
THUMBNAIL_PROVIDERS = {}   # ext(lower) -> fn(path, thumb_path) -> bool
METADATA_PROVIDERS = {}    # ext(lower) -> fn(path) -> dict

def register_thumbnail_provider(extensions, fn):
    for e in extensions:
        THUMBNAIL_PROVIDERS[e.lower()] = fn

def register_metadata_provider(extensions, fn):
    for e in extensions:
        METADATA_PROVIDERS[e.lower()] = fn

def get_tool_path(tool_name: str) -> str:
    """跨平台获取工具路径（FFmpeg/ExifTool 等）"""
    is_win, is_mac = platform.system() == 'Windows', platform.system() == 'Darwin'
    if getattr(sys, 'frozen', False): # 打包环境
        p = os.path.join(sys._MEIPASS, 'bin', tool_name)
        if is_win and not p.lower().endswith('.exe') and os.path.exists(p+'.exe'): return p+'.exe'
        if os.path.exists(p): return p
    # 本地或系统路径
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bin', tool_name)
    if is_win and not p.lower().endswith('.exe') and os.path.exists(p+'.exe'): return p+'.exe'
    if os.path.exists(p): return p
    # 主工程统一 FFmpeg 定位（TOPOS_FFMPEG_HOME / 仓库根 bin/ → PATH），与
    # 导出、解码管线共用同一份二进制；file_explorer 独立部署时静默跳过。
    if tool_name in ('ffmpeg', 'ffprobe'):
        try:
            from src.shared.ffmpeg_locator import find_binary
            located = find_binary(tool_name)
            if located: return located
        except Exception:
            pass
    sys_p = shutil.which(tool_name)
    if sys_p: return sys_p
    if is_mac: # macOS 补丁
        for fb in [f'/opt/homebrew/bin/{tool_name}', f'/usr/local/bin/{tool_name}']:
            if os.path.exists(fb): return fb
    return None

def get_size_str(size_bytes):
    if size_bytes == 0: return "0 B"
    i = int(math.floor(math.log(size_bytes, 1024)))
    size_name = ("B", "KB", "MB", "GB", "TB")
    p = math.pow(1024, i)
    return f"{round(size_bytes / p, 2)} {size_name[i]}"

def open_in_os(path):
    system_name = platform.system()
    try:
        if system_name == 'Windows': os.startfile(path)
        elif system_name == 'Darwin': subprocess.call(['open', path])
        else: subprocess.call(['xdg-open', path])
        return True
    except: return False

def get_pro_metadata(path):
    """深度解析多媒体元数据（分辨率、编码、拍摄参数等）"""
    ext, meta = os.path.splitext(path)[1].lower(), {}
    prov = METADATA_PROVIDERS.get(ext)
    if prov:  # 插件提供者优先（如 video_pro 的 .topos）
        return prov(path)
    if ext in VALID_VIDEO_EXTS: # 视频流解析
        ffp = get_tool_path('ffprobe')
        if ffp:
            try:
                res = subprocess.run([ffp, '-v', 'quiet', '-print_format', 'json', '-show_streams', '-show_format', path], capture_output=True, text=True, encoding='utf-8', errors='ignore', timeout=5)
                if res.returncode == 0:
                    data = json.loads(res.stdout)
                    v = next((s for s in data.get('streams', []) if s.get('codec_type') == 'video'), None)
                    if v:
                        if v.get('width'): meta['Resolution'] = f"{v['width']}x{v['height']}"
                        if '/' in v.get('r_frame_rate', ''):
                            n, d = map(float, v['r_frame_rate'].split('/'))
                            if d > 0: meta['Frame Rate'] = f"{n/d:.2f} fps"
                        c_name = v.get('codec_name', '').lower()
                        c_long = v.get('codec_long_name', '').lower()
                        profile = v.get('profile', v.get('profile_name', '')).strip()
                        if profile.lower() == 'unknown': profile = ''

                        # 智能构建显示名称
                        c_disp = c_name.upper()
                        
                        if 'prores' in c_name:
                            # ProRes 422 HQ, ProRes 4444, etc.
                            c_disp = f"ProRes {profile}".strip()
                        elif 'dnxhd' in c_name:
                            # DNxHR often identified as dnxhd codec with specific profiles
                            if 'hqx' in profile.lower() or '444' in profile.lower() or 'lb' in profile.lower() or 'sq' in profile.lower():
                                c_disp = f"DNxHR {profile}".strip()
                            else:
                                c_disp = f"DNxHD {profile}".strip()
                        elif 'h264' in c_name:
                            c_disp = "H.264"
                            # H.264 High 4:2:2 等详细信息太长，暂不全部显示，除非是 10bit
                        elif 'hevc' in c_name or 'h265' in c_name:
                            c_disp = "H.265"
                        
                        # 尝试从 tag string 获取更准确的信息 (例如 'apch' -> ProRes 422 HQ)
                        codec_tag = v.get('codec_tag_string', '').lower()
                        if codec_tag == 'apch': c_disp = "ProRes 422 HQ"
                        elif codec_tag == 'apcn': c_disp = "ProRes 422"
                        elif codec_tag == 'apcs': c_disp = "ProRes 422 LT"
                        elif codec_tag == 'apco': c_disp = "ProRes 422 Proxy"
                        elif codec_tag == 'ap4h': c_disp = "ProRes 4444"
                        elif codec_tag == 'ap4x': c_disp = "ProRes 4444 XQ"

                        meta['Codec'] = c_disp

                        # 智能位深检测
                        bits = v.get('bits_per_raw_sample')
                        if not bits or str(bits) == '0':
                            pix_fmt = v.get('pix_fmt', '')
                            if '10le' in pix_fmt or '10be' in pix_fmt: bits = 10
                            elif '12le' in pix_fmt or '12be' in pix_fmt: bits = 12
                            elif '16le' in pix_fmt or '16be' in pix_fmt: bits = 16
                            else: bits = 8
                        
                        meta['Bit Depth'] = f"{bits}-bit"
                        
                        # 如果是 DNxHR/ProRes，通常把 Bit Depth 拼接到 Codec 显示中会让用户更直观
                        # 但为了属性面板的整洁，这里保持分离，前端组合显示
                        if v.get('bit_rate'): meta['Bitrate'] = f"{float(v['bit_rate'])/1e6:.1f} Mbps"
            except: pass
    elif ext in VALID_IMAGE_EXTS or ext in {'.cr2', '.nef', '.arw', '.dng'}: # 图像/RAW 解析
        exif = get_tool_path('exiftool')
        if exif:
            try:
                res = subprocess.run([exif, '-json', path], capture_output=True, text=True, encoding='utf-8', errors='ignore', timeout=3)
                if res.returncode == 0:
                    d = json.loads(res.stdout)[0]
                    for k, mk in [('Make','Make'),('Model','Model'),('LensModel','Lens'),('ISO','ISO'),('FNumber','Aperture'),('ExposureTime','Shutter'),('ImageWidth','W')]:
                        if k in d: meta[mk] = f"f/{d[k]}" if k=='FNumber' else (f"{d[k]}s" if k=='ExposureTime' else d[k])
                    if 'W' in meta: meta['Resolution'] = f"{d['ImageWidth']}x{d['ImageHeight']}"; del meta['W']
            except: pass
        if not meta: # Pillow 兜底
            try:
                with Image.open(path) as img:
                    meta['Resolution'] = f"{img.width}x{img.height}"; meta['Format'] = img.format
            except: pass
    return meta

def get_tech_meta(path):
    """[V1/GAP-02] 结构化技术元数据提取（索引器/元数据队列落库用）。

    与 get_pro_metadata 的展示型字符串不同，这里产出可直接入 file_meta
    技术列的标量 dict：mtime/size/date_taken/width/height/duration/codec/
    camera/lens/iso/aperture/shutter/focal/gps_lat/gps_lon。
    拿不到的键缺省；mtime/size 失败返回空 dict（文件不可读）。"""
    result = {}
    try:
        st = os.stat(path)
        result['mtime'] = st.st_mtime
        result['size'] = st.st_size
    except OSError:
        return result
    ext = os.path.splitext(path)[1].lower()

    def _num(v):
        try:
            f = float(v)
            return f
        except (TypeError, ValueError):
            return None

    def _parse_dt(s):
        """EXIF/容器时间 → epoch 秒；解析失败返回 None（无时区，按本地时区）。"""
        if not s:
            return None
        s = str(s).strip()
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y:%m:%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
            try:
                import calendar
                return calendar.timegm(time.strptime(s[:19], fmt))
            except ValueError:
                continue
        return None

    prov = METADATA_PROVIDERS.get(ext)
    if prov:  # 插件提供者优先（.topos 等）：解析展示 dict 中可入库的键
        try:
            d = prov(path) or {}
            res = d.get('Resolution')
            if isinstance(res, str) and 'x' in res:
                w, _, h = res.partition('x')
                result['width'], result['height'] = int(_num(w) or 0) or None, int(_num(h) or 0) or None
            if d.get('Codec'):
                result['codec'] = str(d['Codec'])
            fr = d.get('Frame Rate')
            if fr and 'duration' not in result:
                pass  # 插件不产出时长，保持缺省
        except Exception:
            pass
        return result

    if ext in VALID_VIDEO_EXTS:  # 视频：ffprobe 流+容器
        ffp = get_tool_path('ffprobe')
        if ffp:
            try:
                res = subprocess.run(
                    [ffp, '-v', 'quiet', '-print_format', 'json', '-show_streams', '-show_format', path],
                    capture_output=True, text=True, encoding='utf-8', errors='ignore', timeout=8)
                if res.returncode == 0:
                    data = json.loads(res.stdout)
                    v = next((s for s in data.get('streams', []) if s.get('codec_type') == 'video'), None)
                    if v:
                        if v.get('width'):
                            result['width'] = int(v['width'])
                        if v.get('height'):
                            result['height'] = int(v['height'])
                        c_name = (v.get('codec_name') or '').lower()
                        if c_name:
                            if 'prores' in c_name:
                                result['codec'] = 'ProRes'
                            elif 'dnxhd' in c_name:
                                result['codec'] = 'DNxHD/DNxHR'
                            elif 'h264' in c_name or 'avc' in c_name:
                                result['codec'] = 'H.264'
                            elif 'hevc' in c_name or 'h265' in c_name:
                                result['codec'] = 'H.265'
                            else:
                                result['codec'] = c_name.upper()
                    fmt = data.get('format') or {}
                    dur = v.get('duration') if (v and v.get('duration')) else fmt.get('duration')
                    dur_n = _num(dur)
                    if dur_n and dur_n > 0:
                        result['duration'] = round(dur_n, 3)
                    tags = fmt.get('tags') or {}
                    dt = _parse_dt(tags.get('creation_time'))
                    if dt:
                        result['date_taken'] = dt
            except Exception:
                pass
    elif ext in VALID_IMAGE_EXTS or ext in {'.cr2', '.cr3', '.nef', '.orf', '.raf', '.sr2',
                                            '.arw', '.rw2', '.dng', '.tga', '.tiff', '.tif',
                                            '.exr', '.hdr', '.jp2'}:
        exif = get_tool_path('exiftool')
        if exif:
            try:
                # -c %+.6f：GPS 输出为带符号十进制度；-d 统一时间格式
                res = subprocess.run(
                    [exif, '-json', '-c', '%+.6f', '-d', '%Y-%m-%d %H:%M:%S', path],
                    capture_output=True, text=True, encoding='utf-8', errors='ignore', timeout=6)
                if res.returncode == 0:
                    d = (json.loads(res.stdout) or [{}])[0]
                    dt = _parse_dt(d.get('DateTimeOriginal') or d.get('CreateDate') or d.get('DateTime'))
                    if dt:
                        result['date_taken'] = dt
                    if d.get('Make') or d.get('Model'):
                        result['camera'] = ' '.join(x for x in (d.get('Make'), d.get('Model')) if x).strip()
                    if d.get('LensModel'):
                        result['lens'] = str(d['LensModel'])
                    iso = _num(d.get('ISO'))
                    if iso:
                        result['iso'] = int(iso)
                    ap = _num(d.get('FNumber'))
                    if ap and ap > 0:
                        result['aperture'] = ap
                    if d.get('ExposureTime') is not None:
                        result['shutter'] = str(d['ExposureTime'])
                    fo = _num(str(d.get('FocalLength', '')).replace(' mm', '').strip())
                    if fo and fo > 0:
                        result['focal'] = fo
                    w, h = _num(d.get('ImageWidth')), _num(d.get('ImageHeight'))
                    if w:
                        result['width'] = int(w)
                    if h:
                        result['height'] = int(h)
                    # exiftool 对 GPSDateStamp 无关；带符号十进制（南纬/西经已带负号）
                    lat, lon = _num(d.get('GPSLatitude')), _num(d.get('GPSLongitude'))
                    if lat is not None and lon is not None and (abs(lat) > 0.0001 or abs(lon) > 0.0001):
                        result['gps_lat'], result['gps_lon'] = lat, lon
            except Exception:
                pass
        if 'width' not in result:  # Pillow 兜底（exiftool 缺失时至少拿分辨率）
            try:
                with Image.open(path) as img:
                    result['width'], result['height'] = img.width, img.height
                    try:
                        exif_ifd = img.getexif()
                        dt = _parse_dt(exif_ifd.get(306))  # DateTime
                        if dt:
                            result['date_taken'] = dt
                    except Exception:
                        pass
            except Exception:
                pass
    return result


def get_video_info(path):
    """
    获取视频信息（旧接口兼容，内部调用 get_pro_metadata）
    返回格式: "Resolution · Codec · FPS · Bitrate"
    """
    meta = get_pro_metadata(path)
    if not meta:
        # 降级尝试 OpenCV
        if HAS_CV2:
            try:
                cap = cv2.VideoCapture(path)
                if cap.isOpened():
                    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    fps = cap.get(cv2.CAP_PROP_FPS)
                    cap.release()
                    parts = [f"{w}x{h}"]
                    if fps > 0: parts.append(f"{int(round(fps))}fps")
                    return " · ".join(parts)
            except: pass
        return None
        
    parts = []
    if 'Resolution' in meta: parts.append(meta['Resolution'])
    
    # 组合 Codec 和 Bit Depth (仅在摘要中显示位深)
    codec = meta.get('Codec', '')
    bit_depth = meta.get('Bit Depth', '')
    # 仅对 10-bit, 12-bit 等高位深进行强调显示
    if codec:
        if bit_depth and '8-bit' not in bit_depth:
            parts.append(f"{codec} {bit_depth}")
        else:
            parts.append(codec)
            
    if 'Frame Rate' in meta: parts.append(meta['Frame Rate'])
    if 'Bitrate' in meta: parts.append(meta['Bitrate'])
    
    return " · ".join(parts)

def format_time(seconds):
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"

def reveal_in_os(path):
    """在资源管理器/Finder中选中文件"""
    system_name = platform.system()
    try:
        if system_name == 'Windows':
            # Windows: explorer /select,"C:\path\to\file"
            subprocess.run(['explorer', '/select,', path])
        elif system_name == 'Darwin':
            # macOS: open -R "/path/to/file"
            subprocess.run(['open', '-R', path])
        else:
            # Linux: try dbus or xdg-open dir
            subprocess.run(['xdg-open', os.path.dirname(path)])
        return True
    except: return False

# --- App Association Logic (macOS only mostly) ---
APP_CACHE = {} # ext -> set of (name, path)
APP_CACHE_LOCK = threading.Lock()
APP_CACHE_SCANNED = False
APP_CACHE_SCANNING = False

def _scan_apps_worker():
    global APP_CACHE_SCANNED, APP_CACHE_SCANNING
    app_dirs = [
        '/Applications',
        '/System/Applications',
        os.path.expanduser('~/Applications'),
        '/System/Applications/Utilities',
        '/Applications/Utilities'
    ]

    # Helper to process an app bundle
    def process_app(app_path):
        plist_path = os.path.join(app_path, 'Contents', 'Info.plist')
        if not os.path.exists(plist_path): return
        try:
            with open(plist_path, 'rb') as f:
                pl = plistlib.load(f)

            name = pl.get('CFBundleDisplayName') or pl.get('CFBundleName') or os.path.splitext(os.path.basename(app_path))[0]
            if 'CFBundleDocumentTypes' in pl:
                for dtype in pl['CFBundleDocumentTypes']:
                    exts = dtype.get('CFBundleTypeExtensions', [])
                    for ext in exts:
                        ext = '.' + ext.lower()
                        # [FUNC-12] 并发读写加锁（此前锁定义了从未使用）
                        with APP_CACHE_LOCK:
                            if ext not in APP_CACHE: APP_CACHE[ext] = set()
                            APP_CACHE[ext].add((name, app_path))
        except: pass

    for d in app_dirs:
        if not os.path.exists(d): continue
        try:
            for item in os.listdir(d):
                if item.endswith('.app'):
                    process_app(os.path.join(d, item))
        except: pass

    with APP_CACHE_LOCK:
        APP_CACHE_SCANNED = True
        APP_CACHE_SCANNING = False

def preload_apps():
    """Start background scan of applications"""
    global APP_CACHE_SCANNING
    if platform.system() == 'Darwin':
        with APP_CACHE_LOCK:
            if APP_CACHE_SCANNED or APP_CACHE_SCANNING: return
            APP_CACHE_SCANNING = True
        threading.Thread(target=_scan_apps_worker, daemon=True).start()

def get_associated_apps(path):
    """Get list of apps that can open the file"""
    if platform.system() != 'Darwin': return []

    # [FUNC-12] 未就绪时只触发后台扫描、立即返回空——不再同步全盘扫描
    # 把 Flask 请求线程阻塞数秒；前端下次打开菜单时缓存已就绪
    global APP_CACHE_SCANNING
    with APP_CACHE_LOCK:
        ready = APP_CACHE_SCANNED
        if not ready and not APP_CACHE_SCANNING:
            APP_CACHE_SCANNING = True
            threading.Thread(target=_scan_apps_worker, daemon=True).start()
        apps = []
        if ready:
            ext = os.path.splitext(path)[1].lower()
            for name, app_path in APP_CACHE.get(ext, set()):
                apps.append({'name': name, 'path': app_path})

    apps.sort(key=lambda x: x['name'])
    return apps

def open_with_app(file_path, app_path):
    try:
        if platform.system() == 'Darwin':
            subprocess.run(['open', '-a', app_path, file_path])
        elif platform.system() == 'Windows':
            # Windows open with specific exe
            subprocess.run([app_path, file_path])
        return True
    except: return False

def get_image_info(path):
    """获取图片详细信息 (Mode, Format, Size, DPI)"""
    try:
        # 优先使用 Pro Metadata
        meta = get_pro_metadata(path)
        if meta and ('Resolution' in meta or 'Make' in meta):
            return meta
            
        with Image.open(path) as img:
            info = {
                "Format": img.format,
                "Mode": img.mode,
                "Size": f"{img.width}x{img.height}",
                "DPI": str(img.info.get('dpi', ''))
            }
            return info
    except Exception as e:
        logger.error(f"Error getting image info: {e}")
        return None
