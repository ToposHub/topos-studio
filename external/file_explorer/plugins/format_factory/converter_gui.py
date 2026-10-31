import sys
import os
import json
import logging
import shutil
import time
import subprocess
import signal

# ===========================
# 0. 环境依赖检查
# ===========================
# psutil 用于监控系统 CPU 和内存状态，这是必须的第三方库
try:
    import psutil
except ImportError:
    # [R1-14] ARCH-4：插件内不再用 print，走 logging
    logging.getLogger(__name__).warning("psutil unavailable; pip install psutil")
    psutil = None

# [R1-14] ffmpeg/ffprobe 绝对路径解析，不依赖裸名 PATH 查找
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"

# [R1-14] 进程组隔离仅在 POSIX 可用（Windows 无 os.setsid）
_SETSID = getattr(os, "setsid", None)

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QLabel, QPushButton, QScrollArea, 
                             QProgressBar, QComboBox, QCheckBox, QMessageBox, 
                             QFrame, QTextEdit, QRadioButton, QSizePolicy, 
                             QSplitter, QTabWidget, QStyle, QFileDialog)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QDragEnterEvent, QDropEvent, QFontMetrics, QIcon

# ===========================
# 1. 样式表 (macOS Tahoe Concept Design)
# ===========================
STYLESHEET = """
QMainWindow { background-color: #1e1e1e; }
QWidget { font-family: -apple-system, "PingFang SC", "Helvetica Neue", sans-serif; color: #e0e0e0; font-size: 13px; }

/* --- 标签页 (Tab Widget) --- */
QTabWidget::pane { border: none; background-color: #1e1e1e; }
QTabWidget::tab-bar { alignment: center; }
QTabBar::tab {
    background: transparent; color: #888; padding: 10px 24px;
    margin: 4px; border-radius: 8px; font-weight: 600; font-size: 14px;
}
QTabBar::tab:selected { background: #333; color: #fff; }
QTabBar::tab:hover { background: #2a2a2a; color: #ccc; }

/* --- 顶部控制面板 --- */
QFrame#TopPanel { 
    background-color: #252525; 
    border-bottom: 1px solid #333; 
    border-radius: 12px;
    margin: 10px;
}
QLabel { color: #ccc; }
QLabel#GroupTitle { font-weight: bold; color: #fff; font-size: 14px; }

/* --- 底部状态栏 --- */
QFrame#StatusBar { background-color: #252525; border-top: 1px solid #333; padding: 8px 15px; }
QLabel#StatusText { font-family: "SF Mono", monospace; font-size: 11px; color: #888; }
QLabel#StatusValue { font-family: "SF Mono", monospace; font-size: 11px; font-weight: bold; color: #ddd; }
QLabel#StatusGPU[active="true"] { color: #32D74B; font-weight: bold; }
QLabel#StatusGPU[active="false"] { color: #666; }

/* --- 按钮通用样式 --- */
QPushButton { 
    background-color: #3a3a3c; border: 1px solid #48484a; border-radius: 8px; 
    color: #fff; padding: 6px 16px; font-weight: 500;
}
QPushButton:hover { background-color: #48484a; border-color: #555; }
QPushButton:pressed { background-color: #2c2c2e; }
QPushButton:disabled { background-color: #252525; color: #555; border-color: #333; }

/* 主按钮 (强调色) */
QPushButton#PrimaryBtn { background-color: #007AFF; border: 1px solid #007AFF; font-weight: 600; }
QPushButton#PrimaryBtn:hover { background-color: #0062cc; border-color: #0062cc; }

/* 停止按钮 (警示色) */
QPushButton#StopBtn { background-color: rgba(255, 69, 58, 0.1); border: 1px solid rgba(255, 69, 58, 0.4); color: #ff453a; }
QPushButton#StopBtn:hover { background-color: rgba(255, 69, 58, 0.2); }

/* 删除按钮 (微型) */
QPushButton#DeleteBtn { 
    background-color: transparent; border: none; color: #666; font-weight: bold; font-size: 16px; 
    padding: 0px; margin: 0px;
}
QPushButton#DeleteBtn:hover { color: #ff453a; }

/* --- 输入控件 --- */
QComboBox { 
    background-color: #2c2c2e; border: 1px solid #3a3a3c; border-radius: 6px; 
    padding: 4px 10px; color: #eee; min-width: 100px;
}
QComboBox:hover { border-color: #555; }
QComboBox::drop-down { border: none; width: 20px; }

QCheckBox { color: #aaa; spacing: 8px; }
QCheckBox::indicator { width: 16px; height: 16px; border-radius: 4px; border: 1px solid #555; background: #2c2c2e; }
QCheckBox::indicator:checked { background: #007AFF; border-color: #007AFF; image: url(data:image/svg+xml;base64,...); }

/* --- 列表与卡片 --- */
QScrollArea { border: none; background-color: transparent; }
QWidget#ScrollContent { background-color: transparent; }

/* 视频任务卡片 */
QFrame#Card { 
    background-color: #2c2c2e; 
    border-radius: 12px; 
    border: 1px solid #38383a;
}
QFrame#Card:hover { border-color: #555; background-color: #303032; }
QFrame#Card[state="skipped"] { opacity: 0.5; border: 1px dashed #444; background-color: #252525; }

/* 标签 Badges */
QLabel#Badge { 
    background-color: #3a3a3c; color: #ccc; border-radius: 4px; 
    padding: 2px 8px; font-size: 11px; font-family: "SF Mono", monospace; 
}

/* 进度条 */
QProgressBar { border: none; background-color: #1a1a1a; border-radius: 3px; height: 6px; }
QProgressBar::chunk { background-color: #007AFF; border-radius: 3px; }

/* 日志框 */
QTextEdit { 
    background-color: #111; color: #999; font-size: 11px; 
    border: none; border-top: 1px solid #333; font-family: "SF Mono", monospace;
}
"""

# ===========================
# 2. 核心配置与工具类
# ===========================
class AppConfig:
    PRORES_TARGETS = {
        "ProRes 422 Proxy": {'val': 0, 'rank': 20, 'flavor': 'proxy'},
        "ProRes 422 LT":    {'val': 1, 'rank': 30, 'flavor': 'lt'},
        "ProRes 422":       {'val': 2, 'rank': 40, 'flavor': '422'},
        "ProRes 422 HQ":    {'val': 3, 'rank': 50, 'flavor': 'hq'}
    }
    RESOLUTIONS = ["原分辨率", "1920x1080 (HD)", "3840x2160 (UHD)", "7680x4320 (8K)", "1280x720 (HD)"]
    CODEC_RANK = {'h264': 10, 'hevc': 10, 'prores': 40, 'dnxh': 30} 
    CONTAINERS = [".mp4", ".mov", ".mkv"]
    REMUX_CODECS = {
        "保持原样 (Copy)": "copy",
        "H.264 (硬件加速)": "h264_videotoolbox",
        "H.265 (HEVC 硬件加速)": "hevc_videotoolbox",
        "ProRes 422 LT": "prores_videotoolbox_lt",
        "ProRes 422": "prores_videotoolbox_std"
    }

class Utils:
    @staticmethod
    def format_size(size_bytes):
        if size_bytes >= 1024 ** 3: return f"{size_bytes / (1024**3):.2f} GB"
        return f"{size_bytes / (1024**2):.1f} MB"

    @staticmethod
    def _cleanup_tmp(tmp):
        """[U10] 停止/失败/异常路径的半成品清理（._cvt_* / ._remux_*）。"""
        try:
            if tmp and os.path.exists(tmp): os.remove(tmp)
        except OSError: pass

    @staticmethod
    def finalize_output(tmp, final_name, fallback_name, log=None):
        """[U10] 转码产物安全落位。

        POSIX rename(2) 对已存在目标是原子替换而非报错——del_orig 且
        源文件与目标同扩展名时（ProRes 转码 .mov→.mov、Remux 保持原扩展），
        final_name 常常就是 src 本身：直接 rename 会把源文件字节静默替换为
        成品，随后的 move_to_trash(src) 再把刚生成的成品丢进废纸篓（源数据
        双重丢失）。规则：目标被占用时先把占用者移入废纸篓，成功才落位；
        失败改用带后缀的回退名（同样避免静默覆盖既有文件）。"""
        if not os.path.exists(final_name):
            os.rename(tmp, final_name)
            return final_name
        if Utils.move_to_trash(final_name) and not os.path.exists(final_name):
            os.rename(tmp, final_name)
            return final_name
        stem, ext = os.path.splitext(fallback_name)
        cand, n = fallback_name, 1
        while os.path.exists(cand):
            n += 1
            cand = f"{stem} ({n}){ext}"
        os.rename(tmp, cand)
        if log: log.emit(f"[警告] 目标名被占用，已改存: {os.path.basename(cand)}")
        return cand

    @staticmethod
    def move_to_trash(path):
        try:
            safe_path = path.replace('"', '\\"')
            applescript = f'tell application "Finder" to delete POSIX file "{safe_path}"'
            res = subprocess.run(['osascript', '-e', applescript], capture_output=True, timeout=15)
            # [U10] 此前 capture_output 不因非零退出抛错即恒返回 True——
            # osascript 失败也报成功，调用方据此跳过兜底逻辑
            return res.returncode == 0
        except: return False

    @staticmethod
    def get_metadata(path):
        try:
            cmd = [FFPROBE, '-v', 'quiet', '-print_format', 'json', '-show_format', '-show_streams', path]
            output = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=8, encoding='utf-8', errors='ignore')
            data = json.loads(output)
            video_stream = next((s for s in data.get('streams', []) if s.get('codec_type') == 'video'), None)
            if not video_stream: return None
            audio_tracks = []
            subtitle_tracks = []
            for s in data.get('streams', []):
                stype = s.get('codec_type')
                codec = s.get('codec_name', 'unk')
                tags = s.get('tags', {})
                lang = tags.get('language', 'und')
                title = tags.get('title', '')
                if stype == 'audio':
                    label = f"Audio #{s['index']} [{lang}] {codec}"
                    if title: label += f" - {title}"
                    audio_tracks.append({'index': s['index'], 'label': label})
                elif stype == 'subtitle':
                    label = f"Sub #{s['index']} [{lang}] {codec}"
                    if title: label += f" - {title}"
                    subtitle_tracks.append({'index': s['index'], 'label': label, 'codec': codec})
            fmt = data['format']
            codec_name = video_stream.get('codec_name', 'unknown').lower()
            profile = video_stream.get('profile', '').lower()
            display_codec = codec_name.upper()
            codec_key = codec_name
            if codec_name in ['h264', 'avc1']:
                display_codec = "H.264"
                if 'high' in profile: display_codec += " High"
                if '422' in profile: display_codec += " 4:2:2"
            elif codec_name in ['hevc', 'h265']:
                display_codec = "H.265 (HEVC)"
                if 'main 10' in profile: display_codec += " 10bit"
            elif 'prores' in codec_name:
                ctag = video_stream.get('codec_tag_string', '').lower()
                if 'apco' in ctag: display_codec = "ProRes Proxy"
                elif 'apcs' in ctag: display_codec = "ProRes LT"
                elif 'apcn' in ctag: display_codec = "ProRes 422"
                elif 'apch' in ctag: display_codec = "ProRes HQ"
                elif 'ap4h' in ctag: display_codec = "ProRes 4444"
                else: display_codec = "ProRes"
            fps_str = video_stream.get('r_frame_rate', '0/0')
            if '/' in fps_str:
                n, d = map(int, fps_str.split('/'))
                fps = n / d if d > 0 else 0
            else: fps = float(fps_str)
            return {
                'path': path, 'filename': os.path.basename(path),
                'width': int(video_stream.get('width', 0)), 'height': int(video_stream.get('height', 0)),
                'codec_key': codec_key, 'display_codec': display_codec,
                'fps': fps, 'duration': float(fmt.get('duration', 0)), 'size': int(fmt.get('size', 0)),
                'audio_tracks': audio_tracks, 'subtitle_tracks': subtitle_tracks
            }
        except: return None

    @staticmethod
    def estimate_size(duration, width, height, flavor):
        rates = {'proxy': 5.5, 'lt': 12.5, '422': 18.0, 'hq': 27.5}
        base = 1920 * 1080; ratio = (width * height) / base
        return (rates.get(flavor, 12.5) * ratio) * duration

class AutoElideLabel(QLabel):
    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self._full_text = text; self.setToolTip(text)
        self.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    def paintEvent(self, event):
        metrics = QFontMetrics(self.font())
        elided = metrics.elidedText(self._full_text, Qt.TextElideMode.ElideLeft, self.width())
        super().setText(elided)
        super().paintEvent(event)

class MonitorThread(QThread):
    stats_updated = pyqtSignal(float, float)
    def __init__(self, parent=None):
        super().__init__(parent)
        # [R1-9] 可停止：此前 while True 无退出条件，窗口关闭后仍每秒
        # psutil 轮询直到进程退出
        self._stop = False
    def stop(self):
        self._stop = True
    def run(self):
        if not psutil:
            return
        while not self._stop:
            self.stats_updated.emit(psutil.cpu_percent(interval=1), psutil.virtual_memory().percent)

class _CardLoaderThread(QThread):
    """[BUG-P2-34] 拖入文件/目录的后台探测器：os.walk + 逐文件 ffprobe
    均在本线程执行，按文件逐个发 card_ready（探测失败发 probe_failed）。"""
    card_ready = pyqtSignal(dict)
    probe_failed = pyqtSignal(str)

    def __init__(self, paths, exts, parent=None):
        super().__init__(parent)
        self._paths = paths
        self._exts = exts
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        for p in self._paths:
            if self._stop:
                return
            if os.path.isdir(p):
                for r, _, fs in os.walk(p):
                    if self._stop:
                        return
                    for f in fs:
                        if self._stop:
                            return
                        if f.lower().endswith(self._exts) and not f.startswith("._"):
                            self._emit(os.path.join(r, f))
            elif p.lower().endswith(self._exts):
                self._emit(p)

    def _emit(self, path):
        try:
            m = Utils.get_metadata(path)
        except Exception:
            m = None
        if m:
            self.card_ready.emit(m)
        else:
            self.probe_failed.emit(os.path.basename(path))


# ===========================
# 3. 基础标签页类 (BaseTab)
# ===========================
class BaseTab(QWidget):
    log_signal = pyqtSignal(str)
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.items = {}
        self.widgets = {}
        self.queue = []
        self.counter = 0
        self.worker = None
        self.output_dir = None
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)
        self.top_panel = QFrame(); self.top_panel.setObjectName("TopPanel")
        self.top_layout = QHBoxLayout(self.top_panel)
        self.main_layout.addWidget(self.top_panel)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.list_content = QWidget(); self.list_content.setObjectName("ScrollContent")
        self.list_layout = QVBoxLayout(self.list_content)
        self.list_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.list_layout.setSpacing(12)
        self.scroll.setWidget(self.list_content)
        self.hint = QLabel("请将视频文件或文件夹拖拽至此")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint.setStyleSheet("color: #444; font-size: 18px; font-weight: 600;")
        self.list_layout.addWidget(self.hint)
        self.main_layout.addWidget(self.scroll)

    def select_output_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择输出文件夹")
        if d:
            self.output_dir = d
            self.lbl_out.setText(f"输出: {os.path.basename(d)}")
            self.lbl_out.setToolTip(d)
        else:
            self.output_dir = None
            self.lbl_out.setText("输出: 原文件同级")

    def dragEnterEvent(self, e): e.accept() if e.mimeData().hasUrls() else e.ignore()
    def dropEvent(self, e):
        self.hint.hide()
        # [BUG-P2-34] 只收集路径，遍历与 ffprobe 全部移到后台线程
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.toLocalFile()]
        if paths:
            self._start_background_load(paths)

    # [BUG-P2-34] 拖入探测线程：目录递归 os.walk 与逐文件 ffprobe
    # （单文件最坏 8s 超时）此前全在 GUI 线程执行——多文件/目录拖入整窗冻结
    DROP_EXTS = ('.mov', '.mp4', '.mxf', '.mts', '.avi', '.mkv', '.webm')

    def add_card(self, path): raise NotImplementedError

    def _start_background_load(self, paths):
        # [U10] 旧 loader 仍运行时先停再等：直接覆盖引用会让运行中的
        # QThread 失去持有者（GC 析构 abort）且双线程并发发卡
        old = getattr(self, '_loader', None)
        if old is not None and old.isRunning():
            old.stop()
            old.wait(3000)
        self._loader = _CardLoaderThread(paths, self.DROP_EXTS)
        self._loader.card_ready.connect(self.add_card_meta)
        self._loader.probe_failed.connect(
            lambda name: self.log_signal.emit(f"⚠️ 无法读取（已跳过）: {name}"))
        self._loader.start()

    def add_card_meta(self, m): raise NotImplementedError

    def _set_progress(self, tid, p):
        # [BUG-P2-35] 转码中删除卡片后，积压的进度信号此前仍触达已删 QWidget
        w = self.widgets.get(tid)
        if w is not None:
            w.pbar.setValue(p)

    def remove_card(self, tid):
        if tid in self.widgets: 
            self.widgets[tid].deleteLater()
            del self.widgets[tid]
            del self.items[tid]
        if tid in self.queue: self.queue.remove(tid)
    def stop_batch(self):
        self.queue = [] 
        if self.worker: self.worker.stop_process()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)

# ===========================
# 4. ProRes 编码页 (逻辑实现)
# ===========================
class TranscodeCard(QFrame):
    delete_clicked = pyqtSignal(int)
    def __init__(self, data, parent=None):
        super().__init__(parent)
        self.data = data; self.id = data['id']
        self.setObjectName("Card"); self.setProperty("state", "normal")
        layout = QHBoxLayout(self); layout.setContentsMargins(20, 16, 20, 16); layout.setSpacing(20)
        info_lyt = QVBoxLayout(); info_lyt.setSpacing(8)
        top_line = QHBoxLayout()
        name_lbl = QLabel(data['filename']); name_lbl.setStyleSheet("color: #fff; font-size: 15px; font-weight: 700;")
        top_line.addWidget(name_lbl)
        badges = [data['display_codec'], f"{data['width']}x{data['height']}", f"{data['fps']:.2f}fps", Utils.format_size(data['size'])]
        for b in badges: top_line.addWidget(QLabel(b, objectName="Badge"))
        top_line.addStretch()
        info_lyt.addLayout(top_line)
        self.status_lbl = QLabel("等待中..."); self.status_lbl.setStyleSheet("color: #0A84FF; font-size: 12px;")
        info_lyt.addWidget(self.status_lbl)
        self.pbar = QProgressBar(); self.pbar.hide(); info_lyt.addWidget(self.pbar)
        layout.addLayout(info_lyt, stretch=0)
        ctrl_lyt = QVBoxLayout(); ctrl_lyt.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter); ctrl_lyt.setSpacing(8)
        ctrl_lyt.addWidget(AutoElideLabel(os.path.dirname(data['path'])))
        btn_row = QHBoxLayout(); btn_row.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.chk_force = QCheckBox("强制"); self.chk_force.stateChanged.connect(self.refresh_logic)
        btn_row.addWidget(self.chk_force); btn_row.addSpacing(15)
        self.cb_codec = QComboBox(); self.cb_codec.addItems(AppConfig.PRORES_TARGETS.keys())
        self.cb_codec.currentIndexChanged.connect(self.refresh_logic)
        btn_row.addWidget(self.cb_codec)
        self.cb_res = QComboBox(); self.cb_res.addItems(AppConfig.RESOLUTIONS)
        self.cb_res.currentIndexChanged.connect(self.refresh_logic)
        btn_row.addWidget(self.cb_res)
        self.del_btn = QPushButton("✕"); self.del_btn.setObjectName("DeleteBtn")
        self.del_btn.clicked.connect(lambda: self.delete_clicked.emit(self.id))
        btn_row.addWidget(self.del_btn)
        ctrl_lyt.addLayout(btn_row)
        layout.addLayout(ctrl_lyt, stretch=1)

    def refresh_logic(self):
        if self.data.get('status') in ['processing', 'done']: return
        tgt_name = self.cb_codec.currentText(); tgt_cfg = AppConfig.PRORES_TARGETS[tgt_name]
        is_forced = self.chk_force.isChecked(); should_skip = False
        src_rank = AppConfig.CODEC_RANK.get(self.data['codec_key'].lower(), 15)
        if not is_forced and src_rank <= tgt_cfg['rank']: should_skip = True
        est_w = self.data['width']; tgt_res_str = self.cb_res.currentText()
        if "原分辨率" not in tgt_res_str:
            try:
                tgt_w = int(tgt_res_str.split('x')[0])
                if not is_forced and should_skip and (self.data['width'] <= tgt_w): should_skip = True
                elif should_skip and (self.data['width'] > tgt_w): should_skip = False; est_w = tgt_w
                elif not should_skip: est_w = tgt_w
                else: est_w = tgt_w
            except: pass
        if should_skip:
            self.setProperty("state", "skipped"); self.status_lbl.setText(f"无需转码 ({self.data['display_codec']})")
            self.data['skip'] = True
        else:
            self.setProperty("state", "normal")
            est = Utils.estimate_size(self.data['duration'], est_w, self.data['height'], tgt_cfg['flavor'])
            txt = f"目标: {tgt_cfg['flavor'].upper()} | 预估: {Utils.format_size(est * 1024 * 1024)}"
            if is_forced: txt = "[强制] " + txt
            self.status_lbl.setText(txt)
            self.data['skip'] = False
        self.style().unpolish(self); self.style().polish(self)

    def set_global(self, c_idx, r_idx):
        self.cb_codec.blockSignals(True); self.cb_res.blockSignals(True)
        self.cb_codec.setCurrentIndex(c_idx); self.cb_res.setCurrentIndex(r_idx)
        self.cb_codec.blockSignals(False); self.cb_res.blockSignals(False); self.refresh_logic()

class TranscodeThread(QThread):
    progress = pyqtSignal(int, int); finished = pyqtSignal(int, bool, str); log = pyqtSignal(str)
    def __init__(self, task, del_orig, output_dir): 
        super().__init__()
        self.task = task
        self.del_orig = del_orig
        self.output_dir = output_dir
        self.is_killed = False
    def run(self):
        src = self.task['path']
        name = os.path.splitext(os.path.basename(src))[0]
        target_dir = self.output_dir if self.output_dir else os.path.dirname(src)
        tmp = os.path.join(target_dir, f"._cvt_{name}.mov")
        cmd = [
            FFMPEG, '-y', 
            '-hwaccel', 'videotoolbox', 
            '-i', src, 
            '-c:v', 'prores_videotoolbox', 
            '-profile:v', str(self.task['profile_val']), 
            '-allow_sw', '1', 
            '-c:a', 'pcm_s16le', 
            '-threads', '8'
        ]
        if "原分辨率" not in self.task['res']:
            try: cmd.extend(['-vf', f"scale={self.task['res'].split('x')[0]}:-2"])
            except: pass
        cmd.append(tmp)
        self.log.emit(f"[转码] 开始: {os.path.basename(src)}")
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8', errors='ignore', preexec_fn=_SETSID)
            while True:
                if self.is_killed: os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM); Utils._cleanup_tmp(tmp); self.finished.emit(self.task['id'], False, "已停止"); return
                line = self.proc.stderr.readline()
                if not line and self.proc.poll() is not None: break
                if "time=" in line:
                    try:
                        t = line.split("time=")[1].split()[0].split(':')
                        s = float(t[0])*3600 + float(t[1])*60 + float(t[2])
                        self.progress.emit(self.task['id'], int((s/self.task['dur'])*100))
                    except: pass
            if self.proc.returncode == 0:
                if self.del_orig:
                    # [U10] 占用者（含 final_name==src 场景）先入废纸篓再落位；
                    # 源文件若仍在（目标名不同）再单独清理
                    final_name = Utils.finalize_output(
                        tmp, os.path.join(target_dir, name + ".mov"),
                        os.path.join(target_dir, name + "_converted.mov"), self.log)
                    if os.path.exists(src):
                        if Utils.move_to_trash(src): self.log.emit("源文件已移入废纸篓")
                        else: self.log.emit(f"[警告] 源文件移入废纸篓失败，已保留: {src}")
                else:
                    Utils.finalize_output(
                        tmp, os.path.join(target_dir, name + "_converted.mov"),
                        os.path.join(target_dir, name + "_converted (2).mov"), self.log)
                self.finished.emit(self.task['id'], True, "完成")
            else:
                Utils._cleanup_tmp(tmp)
                self.finished.emit(self.task['id'], False, "失败")
        except Exception as e:
            Utils._cleanup_tmp(tmp)
            self.finished.emit(self.task['id'], False, str(e))
    def stop_process(self): self.is_killed = True

class TranscodeTab(BaseTab):
    def __init__(self):
        super().__init__()
        self.top_layout.addWidget(QLabel("全局设置:"))
        self.cb_c = QComboBox(); self.cb_c.addItems(AppConfig.PRORES_TARGETS.keys())
        self.cb_c.setCurrentIndex(1)
        self.cb_c.currentIndexChanged.connect(self.sync)
        self.top_layout.addWidget(self.cb_c)
        self.cb_r = QComboBox(); self.cb_r.addItems(AppConfig.RESOLUTIONS)
        self.cb_r.currentIndexChanged.connect(self.sync)
        self.top_layout.addWidget(self.cb_r)
        self.chk_d = QCheckBox("移入废纸篓")
        self.chk_d.setChecked(True)
        self.chk_d.setStyleSheet("color: #ff453a; font-weight: bold; margin-left: 10px;")
        self.top_layout.addWidget(self.chk_d)
        self.top_layout.addStretch()
        self.lbl_out = QLabel("输出: 原文件同级")
        self.lbl_out.setStyleSheet("color: #888; margin-right: 5px;")
        self.top_layout.addWidget(self.lbl_out)
        btn_out = QPushButton("选择..."); btn_out.setFixedSize(75, 28)
        btn_out.clicked.connect(self.select_output_dir)
        self.top_layout.addWidget(btn_out)
        line = QFrame(); line.setFrameShape(QFrame.Shape.VLine); line.setStyleSheet("color:#444; margin:0 10px;"); self.top_layout.addWidget(line)
        self.btn_stop = QPushButton("停止"); self.btn_stop.setObjectName("StopBtn")
        self.btn_stop.setEnabled(False); self.btn_stop.clicked.connect(self.stop_batch)
        self.top_layout.addWidget(self.btn_stop)
        self.btn_start = QPushButton("开始转码"); self.btn_start.setObjectName("PrimaryBtn")
        self.btn_start.clicked.connect(self.run)
        self.top_layout.addWidget(self.btn_start)

    def add_card(self, path):
        if any(i['path'] == path for i in self.items.values()): return
        m = Utils.get_metadata(path)
        if not m:
            self.log_signal.emit(f"⚠️ 无法读取（已跳过）: {os.path.basename(path)}")
            return
        self.add_card_meta(m)

    def add_card_meta(self, m):
        if any(i['path'] == m['path'] for i in self.items.values()): return
        tid = self.counter; self.counter+=1; m['id']=tid; c = TranscodeCard(m); c.delete_clicked.connect(self.remove_card)
        c.set_global(self.cb_c.currentIndex(), self.cb_r.currentIndex())
        self.hint.hide()
        self.list_layout.addWidget(c); self.items[tid]=m; self.widgets[tid]=c
    
    def sync(self): 
        for w in self.widgets.values(): 
            if w.data.get('status') != 'processing': w.set_global(self.cb_c.currentIndex(), self.cb_r.currentIndex())
    
    def run(self):
        self.queue = [w.id for w in self.widgets.values() if not w.data.get('skip', False) and w.data.get('status')!='done']
        if not self.queue: QMessageBox.information(self, "提示", "没有任务"); return
        self.btn_start.setEnabled(False); self.btn_stop.setEnabled(True); self.next()
    
    def next(self):
        if not self.queue: self.btn_start.setEnabled(True); self.btn_stop.setEnabled(False); return
        tid = self.queue.pop(0); w = self.widgets[tid]; w.pbar.show(); w.status_lbl.setText("转换中..."); w.data['status']='processing'
        task = {
            'id': tid, 'path': w.data['path'], 'dur': w.data['duration'], 
            'profile_val': AppConfig.PRORES_TARGETS[w.cb_codec.currentText()]['val'], 
            'res': w.cb_res.currentText()
        }
        self.worker = TranscodeThread(task, self.chk_d.isChecked(), self.output_dir)
        # [BUG-P2-35] 直连安全回调：lambda 捕获 w 在卡片被删后触达已删控件
        self.worker.progress.connect(self._set_progress)
        self.worker.log.connect(self.log_signal.emit)
        self.worker.finished.connect(self.done)
        self.worker.start()
        
    def done(self, tid, s, m):
        if tid in self.widgets: 
            self.widgets[tid].pbar.hide(); self.widgets[tid].status_lbl.setText(m)
            self.widgets[tid].status_lbl.setStyleSheet("color: #32D74B;" if s else "color: #FF453A;")
            self.widgets[tid].data['status'] = 'done' if s else 'error'
        if "停止" not in m: QTimer.singleShot(500, self.next)
        else: self.btn_start.setEnabled(True); self.btn_stop.setEnabled(False)

# ===========================
# 5. 格式转换页 (增强版)
# ===========================
class RemuxCard(QFrame):
    delete_clicked = pyqtSignal(int)
    def __init__(self, data, parent=None):
        super().__init__(parent)
        self.data = data; self.id = data['id']; self.manual_override = False
        self.setObjectName("Card")
        layout = QHBoxLayout(self); layout.setContentsMargins(20, 16, 20, 16); layout.setSpacing(20)
        info_lyt = QVBoxLayout(); info_lyt.setSpacing(8)
        name_lbl = QLabel(data['filename']); name_lbl.setStyleSheet("color: #fff; font-size: 15px; font-weight: 700;")
        info_lyt.addWidget(name_lbl)
        sub_info = QHBoxLayout(); 
        for b in [data['display_codec'], Utils.format_size(data['size'])]: sub_info.addWidget(QLabel(b, objectName="Badge"))
        self.status_lbl = QLabel("准备就绪"); self.status_lbl.setStyleSheet("color: #0A84FF; font-size: 12px;")
        sub_info.addWidget(self.status_lbl); sub_info.addStretch(); info_lyt.addLayout(sub_info)
        self.pbar = QProgressBar(); self.pbar.hide(); info_lyt.addWidget(self.pbar)
        layout.addLayout(info_lyt, stretch=1)
        ctrl_lyt = QHBoxLayout(); ctrl_lyt.setSpacing(10); ctrl_lyt.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.cb_audio = QComboBox()
        if len(data.get('audio_tracks', [])) > 1:
            for t in data['audio_tracks']: self.cb_audio.addItem(t['label'], t['index'])
            ctrl_lyt.addWidget(self.cb_audio)
        else: self.cb_audio.hide()
        self.cb_sub = QComboBox(); self.cb_sub.addItem("不提取字幕", -1)
        if len(data.get('subtitle_tracks', [])) > 0:
            for t in data['subtitle_tracks']: self.cb_sub.addItem(f"提取: {t['label']}", t['index'])
            ctrl_lyt.addWidget(self.cb_sub)
        else: self.cb_sub.hide()
        ctrl_lyt.addWidget(QLabel("|"))
        self.cb_codec = QComboBox(); self.cb_codec.addItems(AppConfig.REMUX_CODECS.keys())
        self.cb_codec.currentIndexChanged.connect(self.on_user_change)
        ctrl_lyt.addWidget(self.cb_codec)
        self.cb_container = QComboBox(); self.cb_container.addItems(AppConfig.CONTAINERS)
        ctrl_lyt.addWidget(self.cb_container)
        self.del_btn = QPushButton("✕"); self.del_btn.setObjectName("DeleteBtn")
        self.del_btn.clicked.connect(lambda: self.delete_clicked.emit(self.id))
        ctrl_lyt.addWidget(self.del_btn)
        layout.addLayout(ctrl_lyt)

    def on_user_change(self): self.manual_override = True
    def set_global(self, codec_idx, container_idx):
        if not self.manual_override:
            self.cb_codec.blockSignals(True); self.cb_codec.setCurrentIndex(codec_idx); self.cb_codec.blockSignals(False)
        self.cb_container.setCurrentIndex(container_idx)

class RemuxThread(QThread):
    progress = pyqtSignal(int, int); finished = pyqtSignal(int, bool, str); log = pyqtSignal(str)
    def __init__(self, task, to_aac, del_orig, output_dir): 
        super().__init__()
        self.task = task
        self.to_aac = to_aac
        self.del_orig = del_orig
        self.output_dir = output_dir
        self.is_killed = False
    def run(self):
        src = self.task['path']
        name = os.path.splitext(os.path.basename(src))[0]
        ext = self.task['container']
        target_dir = self.output_dir if self.output_dir else os.path.dirname(src)
        tmp = os.path.join(target_dir, f"._remux_{name}{ext}")
        sub_idx = self.task.get('sub_index', -1)
        if sub_idx != -1:
            sub_codec = 'srt'
            if 'subtitle_tracks' in self.task:
                for t in self.task['subtitle_tracks']:
                    if t['index'] == sub_idx: sub_codec = t.get('codec', 'srt'); break
            sub_ext = '.ass' if 'ass' in sub_codec or 'ssa' in sub_codec else '.srt'
            sub_file = os.path.join(target_dir, f"{name}{sub_ext}")
            self.log.emit(f"[字幕] 提取 Track #{sub_idx} ({sub_codec}) -> {sub_ext}")
            sub_cmd = [FFMPEG, '-y', '-i', src, '-map', f'0:{sub_idx}']
            if sub_ext == '.ass': sub_cmd.extend(['-c:s', 'copy', sub_file])
            else: sub_cmd.extend(['-c:s', 'srt', sub_file])
            try: subprocess.run(sub_cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore', timeout=600)
            except Exception as e: self.log.emit(f"[字幕] 提取失败: {e}")
        cmd = [FFMPEG, '-y', '-hwaccel', 'videotoolbox', '-i', src]
        v_key = AppConfig.REMUX_CODECS[self.task['video_codec']]
        if v_key == "copy": cmd.extend(['-map', '0:v:0', '-c:v', 'copy'])
        elif "prores" in v_key:
            p = '1' if 'lt' in v_key else '2'
            cmd.extend(['-map', '0:v:0', '-c:v', 'prores_videotoolbox', '-profile:v', p])
        elif "h264" in v_key: cmd.extend(['-map', '0:v:0', '-c:v', 'h264_videotoolbox', '-b:v', '8000k'])
        elif "hevc" in v_key: cmd.extend(['-map', '0:v:0', '-c:v', 'hevc_videotoolbox', '-q:v', '60'])
        a_idx = self.task.get('audio_index', None)
        cmd.extend(['-map', f'0:{a_idx}' if a_idx is not None else '0:a:0'])
        if self.to_aac: cmd.extend(['-c:a', 'aac_at', '-b:a', '320k', '-ar', '48000', '-ac', '2'])
        else: cmd.extend(['-c:a', 'copy'])
        cmd.append(tmp)
        self.execute(cmd, src, tmp, target_dir, name, ext)
    def execute(self, cmd, src, tmp, target_dir, name, ext):
        self.log.emit(f"[转换] 开始: {os.path.basename(src)}")
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8', errors='ignore', preexec_fn=_SETSID)
            err_log = []
            while True:
                if self.is_killed: os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM); Utils._cleanup_tmp(tmp); self.finished.emit(self.task['id'], False, "已停止"); return
                line = self.proc.stderr.readline()
                if not line and self.proc.poll() is not None: break
                if "Error" in line or "Failed" in line: err_log.append(line.strip())
                if "time=" in line:
                    try: t=line.split("time=")[1].split()[0].split(':'); s=float(t[0])*3600+float(t[1])*60+float(t[2]); self.progress.emit(self.task['id'], int((s/self.task['dur'])*100))
                    except: pass
            if self.proc.returncode == 0:
                if self.del_orig:
                    # [U10] Remux 保持原扩展名——final_name==src 是默认输出目录下的
                    # 必然场景，直接 rename 会销毁源文件（同 Transcode 批注）
                    Utils.finalize_output(
                        tmp, os.path.join(target_dir, name + ext),
                        os.path.join(target_dir, name + "_out" + ext), self.log)
                    if os.path.exists(src):
                        if Utils.move_to_trash(src): self.log.emit("源文件已移入废纸篓")
                        else: self.log.emit(f"[警告] 源文件移入废纸篓失败，已保留: {src}")
                else:
                    Utils.finalize_output(
                        tmp, os.path.join(target_dir, name + "_out" + ext),
                        os.path.join(target_dir, name + "_out (2)" + ext), self.log)
                self.finished.emit(self.task['id'], True, "完成")
            else:
                msg = err_log[-1] if err_log else "FFmpeg 异常退出"
                self.log.emit(f"❌ 错误: {msg}")
                Utils._cleanup_tmp(tmp)
                self.finished.emit(self.task['id'], False, "失败")
        except Exception as e:
            Utils._cleanup_tmp(tmp)
            self.finished.emit(self.task['id'], False, str(e))
    def stop_process(self): self.is_killed = True

class RemuxTab(BaseTab):
    def __init__(self):
        super().__init__()
        self.top_layout.addWidget(QLabel("全局设置:"))
        self.cb_g_codec = QComboBox(); self.cb_g_codec.addItems(AppConfig.REMUX_CODECS.keys())
        self.cb_g_codec.currentIndexChanged.connect(self.sync)
        self.top_layout.addWidget(self.cb_g_codec)
        self.cb_g_cont = QComboBox(); self.cb_g_cont.addItems(AppConfig.CONTAINERS)
        self.cb_g_cont.currentIndexChanged.connect(self.sync)
        self.top_layout.addWidget(self.cb_g_cont)
        self.chk_aac = QCheckBox("音频转AAC")
        self.chk_aac.setChecked(True)
        self.chk_aac.setToolTip("推荐勾选，解决MKV兼容性")
        self.top_layout.addWidget(self.chk_aac)
        self.chk_del = QCheckBox("移入废纸篓")
        self.chk_del.setChecked(True)
        self.chk_del.setStyleSheet("color: #ff453a; font-weight: bold; margin-left:10px;")
        self.top_layout.addWidget(self.chk_del)
        self.top_layout.addStretch()
        self.lbl_out = QLabel("输出: 原文件同级")
        self.lbl_out.setStyleSheet("color: #888; margin-right: 5px;")
        self.top_layout.addWidget(self.lbl_out)
        btn_out = QPushButton("选择..."); btn_out.setFixedSize(75, 28)
        btn_out.clicked.connect(self.select_output_dir)
        self.top_layout.addWidget(btn_out)
        line = QFrame(); line.setFrameShape(QFrame.Shape.VLine); line.setStyleSheet("color:#444; margin:0 10px;"); self.top_layout.addWidget(line)
        self.btn_stop = QPushButton("停止"); self.btn_stop.setObjectName("StopBtn"); self.btn_stop.setEnabled(False); self.btn_stop.clicked.connect(self.stop_batch); self.top_layout.addWidget(self.btn_stop)
        self.btn_start = QPushButton("开始转换"); self.btn_start.setObjectName("PrimaryBtn"); self.btn_start.clicked.connect(self.run); self.top_layout.addWidget(self.btn_start)

    def add_card(self, path):
        if any(i['path'] == path for i in self.items.values()): return
        m = Utils.get_metadata(path)
        if not m:
            self.log_signal.emit(f"⚠️ 无法读取（已跳过）: {os.path.basename(path)}")
            return
        self.add_card_meta(m)

    def add_card_meta(self, m):
        if any(i['path'] == m['path'] for i in self.items.values()): return
        tid = self.counter; self.counter+=1; m['id']=tid; c = RemuxCard(m); c.delete_clicked.connect(self.remove_card)
        c.set_global(self.cb_g_codec.currentIndex(), self.cb_g_cont.currentIndex())
        self.hint.hide()
        self.list_layout.addWidget(c); self.items[tid]=m; self.widgets[tid]=c
    def sync(self):
        for w in self.widgets.values(): w.set_global(self.cb_g_codec.currentIndex(), self.cb_g_cont.currentIndex())
    def run(self):
        self.queue = [w.id for w in self.widgets.values() if w.data.get('status') != 'done']
        if not self.queue: QMessageBox.information(self, "提示", "没有任务"); return
        self.btn_start.setEnabled(False); self.btn_stop.setEnabled(True); self.next()
    def next(self):
        if not self.queue: self.btn_start.setEnabled(True); self.btn_stop.setEnabled(False); return
        tid = self.queue.pop(0); w = self.widgets[tid]; w.status_lbl.setText("处理中..."); w.status_lbl.setStyleSheet("color: #FF9500;"); w.pbar.show(); w.pbar.setValue(0)
        task = {
            'id': tid, 'path': w.data['path'], 'dur': w.data['duration'],
            'container': w.cb_container.currentText(),
            'video_codec': w.cb_codec.currentText(),
            'audio_index': w.cb_audio.currentData() if w.cb_audio.isVisible() else None,
            'sub_index': w.cb_sub.currentData() if w.cb_sub.isVisible() else -1,
            'subtitle_tracks': w.data.get('subtitle_tracks', [])
        }
        self.worker = RemuxThread(task, self.chk_aac.isChecked(), self.chk_del.isChecked(), self.output_dir)
        self.worker.log.connect(self.log_signal.emit); self.worker.progress.connect(self._set_progress)
        self.worker.finished.connect(self.done); self.worker.start()
    def done(self, tid, s, m):
        if tid in self.widgets: 
            self.widgets[tid].pbar.hide(); self.widgets[tid].status_lbl.setText(m)
            self.widgets[tid].status_lbl.setStyleSheet("color: #32D74B;" if s else "color: #FF453A;")
            self.widgets[tid].data['status'] = 'done' if s else 'error'
        if "停止" not in m: QTimer.singleShot(100, self.next)
        else: self.btn_start.setEnabled(True); self.btn_stop.setEnabled(False)

# ===========================
# 6. 主程序
# ===========================
class MainWindow(QMainWindow):
    def __init__(self, file_path=None):
        super().__init__()
        self.setWindowTitle("ProRes Batch Master v21 (Tahoe Design)"); self.resize(1200, 780); self.setStyleSheet(STYLESHEET)
        central = QWidget(); self.setCentralWidget(central); main = QVBoxLayout(central); main.setContentsMargins(0,0,0,0)
        self.tabs = QTabWidget()
        self.tab_1 = TranscodeTab()
        self.tab_2 = RemuxTab()
        self.tabs.addTab(self.tab_1, "ProRes 编码")
        self.tabs.addTab(self.tab_2, "格式与编码转换")
        splitter = QSplitter(Qt.Orientation.Vertical); splitter.addWidget(self.tabs)
        log_widget = QWidget(); log_lyt = QVBoxLayout(log_widget); log_lyt.setContentsMargins(0,0,0,0)
        self.log = QTextEdit(); self.log.setReadOnly(True)
        log_lyt.addWidget(self.log); splitter.addWidget(log_widget); splitter.setSizes([630, 150])
        main.addWidget(splitter)
        self.tab_1.log_signal.connect(self.log_msg); self.tab_2.log_signal.connect(self.log_msg)
        bar = QFrame(); bar.setObjectName("StatusBar"); b_lyt = QHBoxLayout(bar); b_lyt.setContentsMargins(0,0,0,0)
        b_lyt.addWidget(QLabel("CPU:")); self.l_cpu = QLabel("0%", objectName="StatusValue"); b_lyt.addWidget(self.l_cpu)
        b_lyt.addWidget(QLabel("MEM:")); self.l_mem = QLabel("0%", objectName="StatusValue"); b_lyt.addWidget(self.l_mem)
        b_lyt.addStretch(); b_lyt.addWidget(QLabel(" GPU:")); self.l_gpu = QLabel("Idle", objectName="StatusGPU"); b_lyt.addWidget(self.l_gpu)
        main.addWidget(bar)
        self.mon = MonitorThread(self); self.mon.stats_updated.connect(self.upd_stat); self.mon.start()

        if file_path:
            self.tab_1.add_card(file_path)
            self.tab_2.add_card(file_path)

    def closeEvent(self, event):  # noqa: N802 - Qt override
        # [R1-9] 停止监控线程，避免窗口关闭后 psutil 轮询空转
        self.mon.stop()
        self.mon.wait(2000)
        # [BUG-P0-08] 转码/封装进行中关窗 = QThread 随窗口析构（进程 abort）。
        # 照 decode_benchmark 的关停协议：cancel → wait，超时则拒绝关窗，
        # 决不销毁仍持有 ffmpeg 子进程的线程。
        busy_workers = [tab.worker for tab in (self.tab_1, self.tab_2)
                        if tab.worker is not None and tab.worker.isRunning()]
        for w in busy_workers:
            w.stop_process()
        if busy_workers and not all(w.wait(3000) for w in busy_workers):
            self.log_msg("⚠️ 转码线程仍在退出，请稍后再关闭窗口")
            event.ignore()
            return
        # [U10] 拖入探测线程纳入关停协议：探测中（单文件最坏 8s 超时）
        # 关窗同样会析构运行中的 QThread
        loaders = [tab._loader for tab in (self.tab_1, self.tab_2)
                   if getattr(tab, '_loader', None) is not None and tab._loader.isRunning()]
        for ld in loaders:
            ld.stop()
        if loaders and not all(ld.wait(9000) for ld in loaders):
            self.log_msg("⚠️ 文件探测仍在进行，请稍后再关闭窗口")
            event.ignore()
            return
        super().closeEvent(event)

    def upd_stat(self, c, m):
        self.l_cpu.setText(f"{c:.1f}%"); self.l_mem.setText(f"{m:.1f}%")
        active = (self.tab_1.worker and self.tab_1.worker.isRunning()) or (self.tab_2.worker and self.tab_2.worker.isRunning())
        self.l_gpu.setText("Active" if active else "Idle"); self.l_gpu.setProperty("active", active); self.l_gpu.style().unpolish(self.l_gpu); self.l_gpu.style().polish(self.l_gpu)
    def log_msg(self, m): self.log.append(f"[{time.strftime('%H:%M:%S')}] {m}"); self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

def main():
    app = QApplication(sys.argv)
    if not shutil.which("ffmpeg"): QMessageBox.critical(None, "错误", "未安装 FFmpeg"); sys.exit(1)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
