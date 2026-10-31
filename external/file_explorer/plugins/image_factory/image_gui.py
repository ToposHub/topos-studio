# -*- coding: utf-8 -*-
"""
图片工厂 - 图片批量转换工具
支持格式转换、尺寸缩放、质量调整、目录扁平化等功能
Refactored: Added File Mode and Tabbed Interface
"""

# 导入系统标准库
import os          # 操作系统接口，用于文件和目录操作
import sys         # 系统相关参数和功能，用于程序退出等
import multiprocessing  # 多进程支持，用于充分利用CPU多核

# 导入进程池执行器，用于并行处理图片
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import TimeoutError as _FutureTimeout

# 导入 PyQt6 图形界面库相关组件
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QPushButton, QLabel, QFileDialog,
                             QProgressBar, QComboBox, QSpinBox, QTextEdit,
                             QFrame, QSizePolicy, QTabWidget, QListWidget,
                             QAbstractItemView)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSize
from PyQt6.QtGui import QFont, QColor, QIcon, QAction

# 导入 PIL 图像处理库
from PIL import Image

# ============================================================================
# [R1-11] PIL 默认图片像素限制约 30000x30000。解除限制的设置必须放在
# image_worker（ProcessPoolExecutor 子进程）内部进程级生效——此前在插件
# import 时执行，会连带关闭主应用所有 PIL 调用点的解压炸弹保护。
# ============================================================================

# ============================================================================
# 界面样式表 - 采用 macOS 风格的扁平化设计
# 使用 QSS (Qt Style Sheets) 类似 CSS 的语法
# ============================================================================
MAC_STYLE = """
/* 主窗口背景色 */
QMainWindow {
    background-color: #1E1E1E;
}
/* 通用控件样式 */
QWidget {
    color: #FFFFFF;
    font-family: 'SF Pro Display', 'PingFang SC', 'Helvetica Neue';
}
/* 主容器卡片样式 */
QFrame#MainContainer {
    background-color: #282828;
    border-radius: 12px;
    border: 1px solid #3A3A3A;
}
/* 标题文字样式 */
QLabel#HeaderTitle { font-size: 24px; font-weight: 600; }
QLabel#SubTitle { font-size: 13px; color: #8E8E93; }
QLabel#PropertyLabel { font-size: 11px; font-weight: 500; color: #8E8E93; margin-bottom: 2px; }

/* 输入组件样式（下拉框和数字输入框） */
QComboBox, QSpinBox {
    background-color: #1C1C1E;
    border: 1px solid #3A3A3A;
    border-radius: 6px;
    padding: 6px 10px;
    color: #FFFFFF;
}
/* 隐藏 QSpinBox 的上下箭头按钮 */
QSpinBox::up-button, QSpinBox::down-button { width: 0px; }

/* 苹果风格动作按钮 */
QPushButton#ActionButton {
    background-color: #3A3A3C;
    border: 1px solid #48484A;
    border-radius: 6px;
    font-size: 12px;
    padding: 6px 12px;
}
/* 按钮选中状态（绿色表示开启） */
QPushButton#ActionButton:checked {
    background-color: #30D158;
    border-color: #30D158;
    color: #FFFFFF;
}

/* 主运行按钮（蓝色大按钮） */
QPushButton#PrimaryButton {
    background-color: #007AFF;
    border-radius: 10px;
    font-size: 15px;
    font-weight: 600;
    padding: 14px;
    border: none;
}
QPushButton#PrimaryButton:hover { background-color: #3595FF; }
QPushButton#PrimaryButton:pressed { background-color: #0056B3; }
QPushButton#PrimaryButton:disabled { background-color: #3A3A3C; color: #8E8E93; }

/* 进度条样式 */
QProgressBar {
    background-color: #1C1C1E;
    border-radius: 3px;
    height: 4px;
    border: none;
}
QProgressBar::chunk { background-color: #0A84FF; border-radius: 2px; }

/* 日志文本框和列表框样式 */
QTextEdit#LogArea, QListWidget#FileList {
    background-color: #121212;
    border: 1px solid #2C2C2E;
    border-radius: 8px;
    color: #D1D1D6;
    font-size: 12px;
}

/* Tab Widget 样式 */
QTabWidget::pane {
    border: 1px solid #3A3A3A;
    background: #1E1E1E;
    border-radius: 8px;
}
QTabBar::tab {
    background: #2C2C2E;
    color: #8E8E93;
    padding: 8px 20px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    margin-right: 2px;
}
QTabBar::tab:selected {
    background: #3A3A3C;
    color: #FFFFFF;
    border-bottom: 2px solid #0A84FF;
}
QTabBar::tab:hover {
    background: #333333;
}
"""

# ============================================================================
# 图片处理工作函数（在独立进程中运行）
# ============================================================================
def image_worker(task):
    """
    处理单个图片的转换任务
    """
    # [R1-11] 子进程内解除 PIL 像素上限，支持超大图；不影响主应用进程
    Image.MAX_IMAGE_PIXELS = None

    # 解包任务参数
    (input_path, output_path, scale_mode, target_res,
     quality, out_format, delete_original) = task

    try:
        # 使用 with 语句打开图片，确保文件被正确关闭
        with Image.open(input_path) as img:
            # 获取原始图片尺寸（宽度和高度）
            w, h = img.size

            # 标记是否需要调整尺寸
            needs_resize = False

            # 根据缩放模式选择基准边（长边或短边）
            side = max(w, h) if scale_mode == "长边" else min(w, h)

            # 如果选定的边超过了目标分辨率，则按比例缩小
            if side > target_res:
                ratio = target_res / side
                w, h = int(w * ratio), int(h * ratio)
                needs_resize = True

            # WebP 格式有硬件限制：最大边不能超过 16383 像素
            if out_format.upper() == 'WEBP' and max(w, h) > 16383:
                ratio = 16383 / max(w, h)
                w, h = int(w * ratio), int(h * ratio)
                needs_resize = True

            # 如果需要调整尺寸，使用 LANCZOS 算法进行高质量重采样
            if needs_resize:
                img = img.resize((w, h), Image.Resampling.LANCZOS)

            # 格式兼容性处理
            # JPG/BMP 不支持透明通道，需要转换为 RGB
            if out_format.upper() in ['JPG', 'JPEG', 'BMP']:
                if img.mode in ('RGBA', 'LA', 'P'):
                    img = img.convert('RGB')
            
            # ICO 格式通常需要特定尺寸，这里不做强制限制，但建议用户注意
            # GIF 如果是多帧，这里默认只保存第一帧（简单转换）

            # 保存图片到目标路径
            img.save(output_path, out_format, quality=quality)

        # 如果设置要删除原文件
        if delete_original and os.path.exists(output_path):
            if os.path.abspath(input_path) != os.path.abspath(output_path):
                if os.path.getsize(output_path) > 0:
                    os.remove(input_path)

        return True, "DONE: " + os.path.basename(input_path)

    except Exception as e:
        return False, "ERR: " + os.path.basename(input_path) + " -> " + str(e)


# ============================================================================
# 处理线程类
# ============================================================================
class ProcessorThread(QThread):
    """
    后台处理线程类
    """
    progress_update = pyqtSignal(int)
    log_update = pyqtSignal(str)
    finished_signal = pyqtSignal(int)

    def __init__(self, config: dict):
        super().__init__()
        self.config = config
        # [R1-12] 取消标志：处理中关窗时由 closeEvent 置位
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        tasks = []
        
        # 模式 1: 文件夹处理
        if 'dir' in self.config and self.config['dir']:
            target_dir = self.config['dir']
            recursive = self.config['recursive']
            flatten_depth = self.config.get('flatten_depth', 0)
            flatten_enabled = self.config.get('flatten_enabled', False)
            
            base_depth = target_dir.rstrip(os.sep).count(os.sep)

            try:
                for root, dirs, files in os.walk(target_dir):
                    current_depth = root.count(os.sep) - base_depth
                    if not recursive and root != target_dir:
                        continue

                    for f in files:
                        if f.lower().endswith(('.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.tif', '.webp')):
                            input_path = os.path.join(root, f)
                            
                            # 扁平化逻辑
                            if flatten_enabled and flatten_depth > 0 and current_depth >= flatten_depth:
                                parts = root.split(os.sep)
                                dest_dir = os.sep.join(parts[:base_depth + flatten_depth + 1])
                            else:
                                dest_dir = root
                            
                            out_ext = self.config['format'].lower()
                            output_path = os.path.join(dest_dir, os.path.splitext(f)[0] + "." + out_ext)
                            
                            tasks.append((input_path, output_path, self.config['scale_mode'],
                                         self.config['res'], self.config['quality'],
                                         self.config['format'], self.config['delete']))
            except Exception as e:
                self.log_update.emit(f"❌ 扫描目录出错: {e}")
                self.finished_signal.emit(0)
                return

        # 模式 2: 文件列表处理
        elif 'files' in self.config and self.config['files']:
            for input_path in self.config['files']:
                if not os.path.exists(input_path): continue
                
                # 在文件所在目录输出
                dest_dir = os.path.dirname(input_path)
                fname = os.path.basename(input_path)
                out_ext = self.config['format'].lower()
                output_path = os.path.join(dest_dir, os.path.splitext(fname)[0] + "." + out_ext)
                
                tasks.append((input_path, output_path, self.config['scale_mode'],
                             self.config['res'], self.config['quality'],
                             self.config['format'], self.config['delete']))

        if not tasks:
            self.finished_signal.emit(0)
            return

        self.log_update.emit(f"📊 开始处理，共 {len(tasks)} 张图片待转换...")
        processed = 0

        # [R1-12] 带 timeout 的结果循环 + 取消检查：此前 future.result() 无
        # timeout 无取消，处理中关窗有 "QThread: Destroyed while running"
        # 崩溃风险。不用 with：取消路径需要 shutdown(wait=False) 立即返回，
        # with 的 __exit__ 会同步等待进行中的子进程结束。
        executor = ProcessPoolExecutor(max_workers=os.cpu_count())
        futures = [executor.submit(image_worker, t) for t in tasks]
        idx = 0
        while idx < len(futures):
            if self._cancelled:
                self.log_update.emit("⛔ 已取消")
                break
            try:
                success, msg = futures[idx].result(timeout=0.5)
            except _FutureTimeout:
                continue
            except Exception as pool_err:
                # [U10] 子进程池崩溃（大图 OOM 被 kill 等）会以
                # BrokenProcessPool 逃逸：run() 终止且 finished_signal 不发，
                # 两个 Tab 与运行按钮永久禁用。终止循环并保证收尾信号
                self.log_update.emit(f"❌ 处理进程异常终止: {pool_err}")
                break
            idx += 1
            processed += 1
            self.progress_update.emit(int(processed / len(tasks) * 100))
            if not success:
                self.log_update.emit(msg)
        executor.shutdown(wait=False, cancel_futures=True)

        if self._cancelled:
            self.finished_signal.emit(processed)
            return

        # 仅在文件夹模式且启用扁平化时清理空目录
        if 'dir' in self.config and self.config.get('flatten_enabled') and self.config.get('flatten_depth') > 0:
            try:
                target_dir = self.config['dir']
                for root, dirs, files in os.walk(target_dir, topdown=False):
                    if root != target_dir and not os.listdir(root):
                        os.rmdir(root)
            except Exception as e:
                self.log_update.emit(f"⚠️ 清理空目录失败: {e}")

        self.finished_signal.emit(len(tasks))


# ============================================================================
# 通用设置组件 Helper
# ============================================================================
def create_settings_ui(parent):
    """创建通用的设置控件并返回引用"""
    layout = QVBoxLayout()
    controls = {}

    # >>>>>> 第一行：输出格式 + 质量
    r1 = QHBoxLayout()
    controls['combo_format'] = QComboBox()
    controls['combo_format'].addItems(["WEBP", "JPEG", "PNG", "BMP", "TIFF", "ICO", "GIF", "TGA"])
    r1.addLayout(parent.create_group("输出格式", controls['combo_format']))

    controls['spin_quality'] = QSpinBox()
    controls['spin_quality'].setRange(1, 100)
    controls['spin_quality'].setValue(75)
    r1.addLayout(parent.create_group("质量 (1-100)", controls['spin_quality']))
    layout.addLayout(r1)

    # >>>>>> 第二行：缩放基准 + 限制尺寸
    r2 = QHBoxLayout()
    controls['combo_scale'] = QComboBox()
    controls['combo_scale'].addItems(["长边", "短边"])
    r2.addLayout(parent.create_group("缩放基准", controls['combo_scale']))

    controls['spin_res'] = QSpinBox()
    controls['spin_res'].setRange(500, 40000)
    controls['spin_res'].setValue(4000)
    r2.addLayout(parent.create_group("限制尺寸 (px)", controls['spin_res']))
    layout.addLayout(r2)
    
    return layout, controls

# ============================================================================
# 选项卡 1: 文件夹批量处理
# ============================================================================
class BatchFolderWidget(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.target_path = ""
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(15)

        # 路径选择
        path_row = QHBoxLayout()
        self.lbl_path = QLabel("拖入文件夹或点击选择")
        self.lbl_path.setStyleSheet("color: #636366; font-size: 13px;")
        btn_sel = QPushButton("选取文件夹")
        btn_sel.setObjectName("ActionButton")
        btn_sel.clicked.connect(self.select_dir)
        path_row.addWidget(self.lbl_path, 1)
        path_row.addWidget(btn_sel)
        layout.addLayout(path_row)

        # 设置区域
        settings_layout, controls = create_settings_ui(self)
        self.controls = controls
        layout.addLayout(settings_layout)

        # 文件夹特有设置
        r3 = QHBoxLayout()
        self.btn_recursive = QPushButton("关闭")
        self.btn_recursive.setCheckable(True)
        self.btn_recursive.setChecked(False)
        self.btn_recursive.setObjectName("ActionButton")
        self.btn_recursive.toggled.connect(lambda b: self.btn_recursive.setText("开启" if b else "关闭"))
        r3.addLayout(self.create_group("遍历子目录", self.btn_recursive))

        self.btn_flatten = QPushButton("已禁用")
        self.btn_flatten.setCheckable(True)
        self.btn_flatten.setObjectName("ActionButton")
        self.btn_flatten.toggled.connect(self.toggle_flatten)
        r3.addLayout(self.create_group("扁平化归档", self.btn_flatten))

        self.spin_depth = QSpinBox()
        self.spin_depth.setRange(1, 5)
        self.spin_depth.setVisible(False)
        self.lbl_depth = QLabel("深度:")
        self.lbl_depth.setVisible(False)
        self.lbl_depth.setObjectName("PropertyLabel")
        r3.addWidget(self.lbl_depth)
        r3.addWidget(self.spin_depth)
        layout.addLayout(r3)

        # 删除原文件
        self.btn_delete = QPushButton("转换成功后保留原文件")
        self.btn_delete.setCheckable(True)
        self.btn_delete.setObjectName("ActionButton")
        self.btn_delete.toggled.connect(lambda b: self.btn_delete.setText("转换成功后删除原文件" if b else "转换成功后保留原文件"))
        layout.addWidget(self.btn_delete)

        # 运行按钮
        self.btn_run = QPushButton("开始执行任务")
        self.btn_run.setObjectName("PrimaryButton")
        self.btn_run.clicked.connect(self.start_task)
        layout.addWidget(self.btn_run)

        # 允许拖拽
        self.setAcceptDrops(True)

    def create_group(self, text, widget):
        v = QVBoxLayout()
        v.setSpacing(2)
        l = QLabel(text)
        l.setObjectName("PropertyLabel")
        v.addWidget(l)
        v.addWidget(widget)
        return v

    def toggle_flatten(self, enabled):
        self.btn_flatten.setText("已启用" if enabled else "已禁用")
        self.spin_depth.setVisible(enabled)
        self.lbl_depth.setVisible(enabled)

    def select_dir(self):
        path = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if path:
            self.update_path(path)

    def update_path(self, path):
        self.target_path = path
        display = path if len(path) < 40 else "... " + path[-35:]
        self.lbl_path.setText("已就绪: " + display)
        self.lbl_path.setStyleSheet("color: #30D158;")

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.accept()
        else:
            e.ignore()

    def dropEvent(self, e):
        files = [u.toLocalFile() for u in e.mimeData().urls()]
        if files and os.path.isdir(files[0]):
            self.update_path(files[0])

    def start_task(self):
        if not self.target_path:
            return

        config = {
            'dir': self.target_path,
            'format': self.controls['combo_format'].currentText(),
            'quality': self.controls['spin_quality'].value(),
            'scale_mode': self.controls['combo_scale'].currentText(),
            'res': self.controls['spin_res'].value(),
            'recursive': self.btn_recursive.isChecked(),
            'flatten_enabled': self.btn_flatten.isChecked(),
            'flatten_depth': self.spin_depth.value(),
            'delete': self.btn_delete.isChecked()
        }
        self.main_window.run_processor(config, self.btn_run)


# ============================================================================
# 选项卡 2: 单文件/多文件转换
# ============================================================================
class FileConvertWidget(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.file_list = []
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(15)

        # 文件列表操作区域
        btn_layout = QHBoxLayout()
        btn_add = QPushButton("添加文件")
        btn_add.setObjectName("ActionButton")
        btn_add.clicked.connect(self.add_files)
        
        btn_clear = QPushButton("清空列表")
        btn_clear.setObjectName("ActionButton")
        btn_clear.clicked.connect(self.clear_list)
        
        btn_layout.addWidget(btn_add)
        btn_layout.addWidget(btn_clear)
        layout.addLayout(btn_layout)

        # 文件列表展示
        self.list_widget = QListWidget()
        self.list_widget.setObjectName("FileList")
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        layout.addWidget(self.list_widget)

        # 设置区域
        settings_layout, controls = create_settings_ui(self)
        self.controls = controls
        layout.addLayout(settings_layout)

        # 删除原文件
        self.btn_delete = QPushButton("转换成功后保留原文件")
        self.btn_delete.setCheckable(True)
        self.btn_delete.setObjectName("ActionButton")
        self.btn_delete.toggled.connect(lambda b: self.btn_delete.setText("转换成功后删除原文件" if b else "转换成功后保留原文件"))
        layout.addWidget(self.btn_delete)

        # 运行按钮
        self.btn_run = QPushButton("开始转换所选文件")
        self.btn_run.setObjectName("PrimaryButton")
        self.btn_run.clicked.connect(self.start_task)
        layout.addWidget(self.btn_run)

        # 允许拖拽
        self.setAcceptDrops(True)

    def create_group(self, text, widget):
        v = QVBoxLayout()
        v.setSpacing(2)
        l = QLabel(text)
        l.setObjectName("PropertyLabel")
        v.addWidget(l)
        v.addWidget(widget)
        return v

    def add_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "选择图片", "", "Images (*.png *.jpg *.jpeg *.bmp *.webp *.tiff)")
        if files:
            self.file_list.extend(files)
            # 去重
            self.file_list = list(set(self.file_list))
            self.refresh_list()

    def clear_list(self):
        self.file_list = []
        self.refresh_list()

    def refresh_list(self):
        self.list_widget.clear()
        for f in self.file_list:
            self.list_widget.addItem(os.path.basename(f))
        
        self.btn_run.setText(f"开始转换 ({len(self.file_list)} 个文件)")

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.accept()
        else:
            e.ignore()

    def dropEvent(self, e):
        files = [u.toLocalFile() for u in e.mimeData().urls()]
        added = False
        for f in files:
            if os.path.isfile(f) and f.lower().endswith(('.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.tif', '.webp')):
                self.file_list.append(f)
                added = True
        
        if added:
            self.file_list = list(set(self.file_list))
            self.refresh_list()

    def start_task(self):
        if not self.file_list:
            return

        config = {
            'files': self.file_list,
            'format': self.controls['combo_format'].currentText(),
            'quality': self.controls['spin_quality'].value(),
            'scale_mode': self.controls['combo_scale'].currentText(),
            'res': self.controls['spin_res'].value(),
            'delete': self.btn_delete.isChecked()
        }
        self.main_window.run_processor(config, self.btn_run)


# ============================================================================
# 主窗口
# ============================================================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ImageConvertPro 2026")
        self.setMinimumSize(520, 800)
        self.setStyleSheet(MAC_STYLE)
        self.init_ui()

    def init_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        
        root_layout = QVBoxLayout(main_widget)
        root_layout.setContentsMargins(20, 20, 20, 20)
        root_layout.setSpacing(15)

        # 说明文本
        info = QLabel("✨ 支持拖拽文件/文件夹 • 批量格式转换 • 智能尺寸缩放 • 专业格式支持 (WEBP/TIFF/ICO等)")
        info.setStyleSheet("color: #8E8E93; font-size: 12px; margin-bottom: 5px;")
        info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root_layout.addWidget(info)

        # 选项卡
        self.tabs = QTabWidget()
        self.batch_tab = BatchFolderWidget(self)
        self.file_tab = FileConvertWidget(self)
        
        self.tabs.addTab(self.batch_tab, "📁 文件夹模式")
        self.tabs.addTab(self.file_tab, "📄 文件模式")
        
        # 默认激活文件模式
        self.tabs.setCurrentIndex(1)
        root_layout.addWidget(self.tabs)

        # 公共日志区域
        log_label = QLabel("任务日志")
        log_label.setObjectName("PropertyLabel")
        root_layout.addWidget(log_label)
        
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        root_layout.addWidget(self.progress)
        
        self.log_box = QTextEdit()
        self.log_box.setObjectName("LogArea")
        self.log_box.setReadOnly(True)
        root_layout.addWidget(self.log_box)

    def run_processor(self, config, source_btn):
        # 禁用 UI
        self.batch_tab.setEnabled(False)
        self.file_tab.setEnabled(False)
        source_btn.setText("正在处理...")
        
        self.log_box.clear()
        
        self.thread = ProcessorThread(config)
        self.thread.progress_update.connect(self.progress.setValue)
        self.thread.log_update.connect(self.log_box.append)
        
        self.thread.finished_signal.connect(lambda n: self.on_finished(n, source_btn))
        self.thread.start()

    def on_finished(self, count, source_btn):
        self.batch_tab.setEnabled(True)
        self.file_tab.setEnabled(True)

        # 恢复按钮文本
        if source_btn == self.batch_tab.btn_run:
            source_btn.setText("开始执行任务")
        else:
            self.file_tab.refresh_list() # 这会重置按钮文本

        self.log_box.append(f"\n✨ 全部完成，共处理 {count} 个文件")

    def closeEvent(self, event):  # noqa: N802 - Qt override
        # [R1-12] 处理中关窗：置取消标志并等待线程退出，避免
        # "QThread: Destroyed while running" 崩溃
        thread = getattr(self, "thread", None)
        if thread is not None and thread.isRunning():
            thread.cancel()
            if not thread.wait(8000):
                # [BUG-P2-38] 线程仍未退出：拒绝关窗（照抄 decode_benchmark
                # 协议），决不销毁仍持有子进程/工作线程的窗口
                self.log_box.append("⚠️ 处理线程仍在退出，请稍后再关闭窗口")
                event.ignore()
                return
        super().closeEvent(event)
