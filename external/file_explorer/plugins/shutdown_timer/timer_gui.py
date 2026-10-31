import sys
import os
import logging
import subprocess
import time
import math
import platform
import threading
from datetime import datetime, timedelta
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QLabel, QPushButton, QComboBox, 
                             QCheckBox, QProgressBar, QFrame,
                             QGroupBox, QSystemTrayIcon,
                             QMenu, QStyle, QSlider)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QIcon, QFont

logger = logging.getLogger(__name__)


# =======================
# 4. 跨平台关机管理器
# ==========
class ShutdownManager:
    """原生跨平台关机管理器（系统级授权弹窗）"""
    
    @staticmethod
    def shutdown(seconds):
        """
        执行关机命令（原生提权弹窗）
        :param seconds: 关机延时（秒）
        """
        system = platform.system()
        
        if system == "Windows":
            # Windows: 直接使用秒
            cmd = f"shutdown /s /t {seconds}"
            ShutdownManager._run_command(cmd, is_background=True)
        
        elif system == "Darwin":  # macOS
            # macOS: 使用 osascript + AppleScript（原生授权弹窗）
            minutes = math.ceil(seconds / 60)  # 向上取整为分钟
            cmd = f"osascript -e 'do shell script \"shutdown -h +{minutes}\" with administrator privileges'"
            ShutdownManager._run_command(cmd, is_background=True)
        
        elif system == "Linux":
            # Linux: 使用 pkexec (Polkit)（原生授权弹窗）
            minutes = math.ceil(seconds / 60)  # 向上取整为分钟
            cmd = f"pkexec shutdown -h +{minutes}"
            ShutdownManager._run_command(cmd, is_background=True)
        
        else:
            # 未知系统，回退到 Windows
            cmd = f"shutdown /s /t {seconds}"
            ShutdownManager._run_command(cmd, is_background=True)
    
    @staticmethod
    def cancel():
        """
        执行取消关机命令（原生提权弹窗）
        """
        system = platform.system()
        
        if system == "Windows":
            # Windows: shutdown /a
            cmd = "shutdown /a"
            ShutdownManager._run_command(cmd, is_background=True)
        
        elif system == "Darwin":  # macOS
            # macOS: killall shutdown
            cmd = "osascript -e 'do shell script \"killall shutdown\" with administrator privileges'"
            ShutdownManager._run_command(cmd, is_background=True)
        
        elif system == "Linux":
            # Linux: shutdown -c
            cmd = "pkexec shutdown -c"
            ShutdownManager._run_command(cmd, is_background=True)
        
        else:
            # 未知系统，回退到 Windows
            cmd = "shutdown /a"
            ShutdownManager._run_command(cmd, is_background=True)
    
    @staticmethod
    def _run_command(cmd, is_background=False):
        """
        执行系统命令（跨平台）
        :param cmd: 命令字符串
        :param is_background: 是否在后台运行
        """
        system = platform.system()
        
        if system == "Windows":
            # Windows: 使用 STARTUPINFO 隐藏命令行窗口
            if is_background:
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE
                subprocess.run(cmd, shell=True, startupinfo=startupinfo, capture_output=True)
            else:
                subprocess.run(cmd, shell=True, capture_output=True)
        else:
            # macOS/Linux: 直接调用，不传 startupinfo
            subprocess.run(cmd, shell=True, capture_output=True)

# =======================
# 1. 样式表 (现代化暗色主题)
# ==============
STYLESHEET = """
/* ========== 全局样式 ========== */
/* 主窗口背景色 - 使用 #0d1117 模仿 GitHub Dark 主题 */
QMainWindow { background-color: #0d1117; }

/* 所有控件的通用字体和颜色设置 */
QWidget { 
    font-family: "Segoe UI", "Microsoft YaHei", "Roboto", sans-serif; 
    color: #e6edf3; 
    font-size: 13px; 
}

/* 框架背景透明 */
QFrame { background-color: transparent; }

/* ========== 分组框样式 ========== */
/* 分组框边框颜色和圆角 */
QGroupBox {
    border: 1px solid #30363d;      /* 浅灰蓝边框 */
    border-radius: 6px;            /* 圆角 6px */
    margin-top: 10px;              /* 顶部外边距 */
    padding-top: 15px;             /* 顶部内边距（为标题留空） */
}

/* 分组框标题样式 */
QGroupBox::title {
    subcontrol-origin: margin;     /* 标题相对于边框定位 */
    left: 10px;                    /* 左边距 */
    padding: 0 5px;                /* 标题内边距 */
    color: #58a6ff;                /* 标题颜色（亮蓝） */
}

/* ========== 标签样式 ========== */
/* 普通文本标签 */
QLabel { color: #8b949e; }

/* 普通数值标签（小字号） */
QLabel#Value { 
    color: #e6edf3; 
    font-size: 18px; 
    font-weight: bold; 
}

/* 大字号时间显示标签 */
QLabel#TimeValue { 
    color: #58a6ff; 
    font-size: 48px; 
    font-weight: bold; 
    font-family: Consolas, monospace;  /* 等宽字体确保数字对齐 */
}

/* ========== 按钮样式 ========== */
/* 主操作按钮 - 默认状态 (实心绿) */
QPushButton#ActionBtn {
    background-color: #2da44e;  /* 强制实心绿 */
    border: 1px solid #2da44e;  /* 同色边框 */
    border-radius: 8px;
    color: #ffffff;             /* 强制白字 */
    font-weight: bold;
    font-family: "Segoe UI", sans-serif;
    font-size: 18px;            /* 确保字体够大 */
    padding: 5px;
}

/* 默认悬停 (亮绿) */
QPushButton#ActionBtn:hover {
    background-color: #4ac26b;
    border-color: #4ac26b;
}

/* 默认按下 (深绿) */
QPushButton#ActionBtn:pressed {
    background-color: #238636;
    border-color: #238636;
    padding-top: 7px;           /* 按下位移感 */
    padding-left: 6px;
}

/* === 特殊状态：运行中 (实心红) === */
/* 只有当代码设置 property state="running" 时生效 */
QPushButton#ActionBtn[state="running"] {
    background-color: #cf222e;  /* 实心红 */
    border: 1px solid #cf222e;
}

/* 运行中悬停 (亮红) */
QPushButton#ActionBtn[state="running"]:hover {
    background-color: #ff4444;
    border-color: #ff4444;
}

/* 运行中按下 (深红) */
QPushButton#ActionBtn[state="running"]:pressed {
    background-color: #a40e26;
    border-color: #a40e26;
}

/* 禁用按钮 */
QPushButton:disabled { 
    background-color: #21262d;      /* 深灰背景 */
    color: #484f58;                /* 灰色文字 */
    border: 1px solid #30363d;     /* 浅灰边框 */
}

/* ========== 下拉框样式 ========== */
/* 卡片式背景，圆角边框 */
QComboBox { 
    background-color: #21262d;     /* 深灰背景 */
    border: 1px solid #30363d;     /* 浅灰边框 */
    border-radius: 6px;            /* 圆角 6px */
    padding: 8px 4px;              /* 减少左右内边距 */
    color: #e6edf3;                /* 白色文字 */
    min-width: 220px;              /* 增加最小宽度 220px */
    min-height: 32px;              /* 最小高度 32px */
}
QComboBox:focus { border-color: #58a6ff; }

/* 移除下拉框的上下箭头按钮 */
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: right center;
    width: 20px;
    border-left: 1px solid #30363d;
    border-radius: 0 6px 6px 0;
}

QComboBox::down-arrow {
    width: 0;
    height: 0;
    border-left: 6px solid transparent;
    border-right: 6px solid transparent;
    border-top: 7px solid #58a6ff;
    margin-right: 8px;
    margin-top: 6px;
    margin-bottom: 6px;
}
QComboBox::up-button {
    image: none;
    width: 0;
    height: 0;
    margin: 0;
    padding: 0;
}

/* ========== 下拉列表弹窗样式 ========== */
/* 美化下拉弹出的列表，与主色调一致 */
QComboBox QAbstractItemView {
    background-color: #21262d;    /* 深灰背景 */
    border: 1px solid #30363d;    /* 边框 */
    selection-background-color: #1f6feb;  /* 选中项高亮蓝 */
    selection-color: #ffffff;     /* 选中项文字白 */
    outline: none;                /* 去掉虚线框 */
    padding: 4px;
    border-radius: 4px;
}

/* 下拉框选项样式 */
QComboBox::item { 
    padding: 8px 10px;            /* 增加垂直内边距 */
    border-radius: 2px;
}
QComboBox::item:hover {
    background-color: #30363d;    /* 悬停背景 */
}
QComboBox::item:selected {
    background-color: #1f6feb;    /* 选中背景 */
}

/* ========== 滑动条样式 ========== */
/* 滑动条槽（轨道） */
QSlider::groove:horizontal {
    border: 1px solid #30363d;
    height: 8px;
    background: #21262d;
    margin: 2px 0;
    border-radius: 4px;
}

/* 滑动条滑块（圆钮） */
QSlider::handle:horizontal {
    background: #58a6ff;
    border: 1px solid #58a6ff;
    width: 18px;
    height: 18px;
    margin: -6px 0;
    border-radius: 9px;
}

/* 滑块悬停和按下状态 */
QSlider::handle:horizontal:hover {
    background: #79c0ff;
    border-color: #79c0ff;
}
QSlider::handle:horizontal:pressed {
    background: #1f6feb;
    border-color: #1f6feb;
}

/* 滑过区域（左侧进度） */
QSlider::sub-page:horizontal {
    background: #1f6feb;
    border: 1px solid #30363d;
    height: 8px;
    border-radius: 4px;
}

/* 滑动条禁用状态 */
QSlider::groove:horizontal:disabled, QSlider::handle:horizontal:disabled {
    background: #30363d;
    border-color: #30363d;
}

/* ========== 进度条样式 ========== */
/* 纯色进度条，无边框，圆角 */
QProgressBar { 
    border: none;                  /* 无边框 */
    background-color: #21262d;     /* 深灰背景 */
    border-radius: 4px;            /* 圆角 4px */
    height: 8px;                   /* 进度条高度 8px */
    text-align: center;            /* 居中文字 */
}

/* 进度条填充块 */
QProgressBar::chunk { 
    background-color: #2ea043;     /* 绿色填充 */
}

/* ========== 复选框样式 ========== */
/* 复选框文字颜色 */
QCheckBox { 
    color: #8b949e; 
    spacing: 10px;                 /* 文字与复选框间距 */
    padding: 5px 0;                /* 垂直内边距 */
}

/* 复选框指示器（自定义大小和样式） */
QCheckBox::indicator { 
    width: 18px;                   /* 宽度 18px */
    height: 18px;                  /* 高度 18px */
    background-color: #21262d;     /* 背景色 */
    border: 2px solid #30363d;     /* 边框 2px 浅灰 */
    border-radius: 4px;            /* 圆角 4px */
}

/* 复选框选中状态 */
QCheckBox::indicator:checked { 
    background-color: #238636;     /* 绿色背景 */
    border-color: #238636;         /* 绿色边框 */
}

/* ========== 滚动条样式 (美化下拉列表的滚动条) ========== */
QScrollBar:vertical {
    border: none;
    background: #0d1117;    /* 轨道背景与主背景一致 */
    width: 10px;            /* 细长条 */
    margin: 0px;
}

/* 滚动条滑块 */
QScrollBar::handle:vertical {
    background: #30363d;    /* 深灰滑块 */
    min-height: 20px;
    border-radius: 5px;     /* 全圆角 */
}

/* 滑块悬停 */
QScrollBar::handle:vertical:hover {
    background: #58a6ff;    /* 悬停变蓝 */
}

/* 隐藏上下箭头（关键步骤） */
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
    subcontrol-origin: margin;
}

/* 隐藏背景槽 */
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: none;
}
"""

# =======================
# 2. 主界面
# =======================
class ShutdownTimerApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("⏰ 跨平台定时关机工具")
        self.setMinimumSize(500, 760)
        self.resize(600, 760)
        
        # 设置样式
        self.setStyleSheet(STYLESHEET)
        
        # 状态变量
        self.total_duration = 0
        self.target_time = None
        self.timer_id = None
        self.is_running = False
        # [U10] 宿主应用退出时取消已排定的系统关机：OS 级 shutdown 命令
        # 不随进程退出撤销，应用内唯一的取消入口会随窗口一起消失。
        # 此处同步执行（进程即将退出，阻塞已无意义）并带超时兜底
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._cancel_on_app_quit)
        
        # 系统托盘
        self.setup_tray()
        
        # 创建UI
        self.setup_ui()
    
    def setup_tray(self):
        """设置系统托盘图标"""
        # 优先使用项目目录的 ShutdownTimer.ico
        if os.path.exists("ShutdownTimer.ico"):
            self.tray_icon = QSystemTrayIcon(QIcon("ShutdownTimer.ico"), self)
        else:
            # [R1-14] 回退到系统默认图标。此前 `QIcon.fromTheme(...) or
            # QStyle.standardIcon(...)` 是必炸死代码：fromTheme 恒返回 QIcon
            # 对象（或短路永假），且 standardIcon 是实例方法却以类调用
            icon = QIcon.fromTheme("computer")
            if icon.isNull():
                icon = QApplication.style().standardIcon(
                    QStyle.StandardPixmap.SP_ComputerIcon)
            self.tray_icon = QSystemTrayIcon(icon, self)
        
        # 创建托盘菜单
        tray_menu = QMenu()
        
        # 显示/隐藏
        toggle_action = tray_menu.addAction("显示/隐藏")
        toggle_action.triggered.connect(self.toggle_window)
        
        tray_menu.addSeparator()
        
        # 退出
        quit_action = tray_menu.addAction("退出")
        quit_action.triggered.connect(self.quit_app)
        
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.show()
    
    def toggle_window(self):
        """切换窗口显示"""
        if self.isMinimized():
            self.showNormal()
        elif self.isHidden():
            self.show()
        else:
            self.hide()
    
    def setup_ui(self):
        """创建用户界面"""
        main_widget = QWidget()
        main_widget.setStyleSheet("background-color: #0d1117;")
        self.setCentralWidget(main_widget)
        
        layout = QVBoxLayout(main_widget)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(25)
        
        # 顶部标题区域
        header_frame = QFrame()
        header_frame.setFixedHeight(80)
        header_layout = QHBoxLayout(header_frame)
        
        title_label = QLabel("⏰")
        title_label.setStyleSheet("font-size: 36px;")
        title_label.setFixedSize(50, 50)
        
        title_text = QLabel("定时关机工具")
        title_text.setStyleSheet("font-size: 20px; font-weight: bold; color: #58a6ff;")
        
        header_layout.addWidget(title_label)
        header_layout.addWidget(title_text)
        header_layout.addStretch()
        
        layout.addWidget(header_frame)
        
        # 倒计时显示区域
        countdown_group = QGroupBox("倒计时")
        countdown_layout = QVBoxLayout(countdown_group)
        
        self.time_label = QLabel("00:00:00")
        self.time_label.setObjectName("TimeValue")
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.pbar = QProgressBar()
        self.pbar.setValue(0)
        self.pbar.setTextVisible(False)
        
        countdown_layout.addWidget(self.time_label)
        countdown_layout.addWidget(self.pbar)
        
        layout.addWidget(countdown_group)
        
        # 设置区域
        settings_group = QGroupBox("设置关机时间")
        settings_layout = QVBoxLayout(settings_group)
        settings_layout.setSpacing(15)
        
        # 数值显示（移到上方，大字体）
        self.slider_value_label = QLabel("1 小时")
        self.slider_value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.slider_value_label.setStyleSheet("color: #58a6ff; font-size: 32px; font-weight: bold; font-family: 'Segoe UI', sans-serif;")
        settings_layout.addWidget(self.slider_value_label)
        
        # 滑动条
        self.custom_slider = QSlider(Qt.Orientation.Horizontal)
        self.custom_slider.setRange(1, 1440)  # 1分钟 - 24小时
        self.custom_slider.setValue(60)  # 默认1小时
        
        # 美化滑动条高度，方便拖动
        self.custom_slider.setStyleSheet("""
            QSlider::groove:horizontal {
                border: 1px solid #30363d;
                height: 10px;
                background: #21262d;
                margin: 2px 0;
                border-radius: 5px;
            }
            QSlider::handle:horizontal {
                background: #58a6ff;
                border: 1px solid #58a6ff;
                width: 24px;
                height: 24px;
                margin: -7px 0;
                border-radius: 12px;
            }
            QSlider::handle:horizontal:hover {
                background: #79c0ff;
                border-color: #79c0ff;
            }
            QSlider::handle:horizontal:pressed {
                background: #1f6feb;
                border-color: #1f6feb;
            }
            QSlider::sub-page:horizontal {
                background: #1f6feb;
                border: 1px solid #30363d;
                height: 10px;
                border-radius: 5px;
            }
        """)
        
        # 连接滑动条信号
        self.custom_slider.valueChanged.connect(self.update_slider_label)
        settings_layout.addWidget(self.custom_slider)
        
        layout.addWidget(settings_group)
        
        # 操作按钮区域
        self.action_btn = QPushButton("▶ 开始倒计时")
        self.action_btn.setObjectName("ActionBtn")
        self.action_btn.setMinimumHeight(55)
        font = self.action_btn.font()
        font.setPointSize(18)
        self.action_btn.setFont(font)
        self.action_btn.setProperty("state", "normal")
        self.action_btn.clicked.connect(self.toggle_shutdown)
        
        layout.addWidget(self.action_btn)
        
        # Ensure initial button state is rendered correctly
        self.update_button_state()
        
        # 说明信息
        info_group = QGroupBox("使用说明")
        info_layout = QVBoxLayout(info_group)
        
        info_text = QLabel(
            "1. 选择关机时长或输入自定义分钟数\n"
            "2. 点击「开始关机」启动倒计时\n"
            "3. 倒计时结束自动关机\n"
            "4. 点击「停止」可取消关机\n"
            "5. 最小化窗口到托盘继续运行"
        )
        info_text.setWordWrap(True)
        info_layout.addWidget(info_text)
        
        layout.addWidget(info_group)
        
        # 底部状态
        self.status_label = QLabel("就绪")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet("color: #484f58; font-size: 12px;")
        
        layout.addWidget(self.status_label)
        layout.addStretch()
        
        # 确保初始按钮状态正确渲染
        self.update_button_state()
    
    def update_slider_label(self, value):
        """更新滑动条数值标签"""
        # 自动将分钟转换为 "H小时 M分" 或 "X分钟" 格式
        if value >= 60:
            hours = value // 60
            minutes = value % 60
            if minutes > 0:
                self.slider_value_label.setText(f"{hours}小时{minutes}分")
            else:
                self.slider_value_label.setText(f"{hours}小时")
        else:
            self.slider_value_label.setText(f"{value}分钟")
    
    def toggle_shutdown(self):
        """总控入口：切换开始/停止状态"""
        if self.is_running:
            self.stop_shutdown()
        else:
            self.start_shutdown()
    
    def update_button_state(self):
        """更新按钮外观（文字 + 颜色）"""
        if self.is_running:
            self.action_btn.setText("⏹ 停止运行")
            self.action_btn.setProperty("state", "running")
        else:
            self.action_btn.setText("▶ 开始倒计时")
            self.action_btn.setProperty("state", "normal")
        
        # 强制刷新样式
        self.action_btn.style().unpolish(self.action_btn)
        self.action_btn.style().polish(self.action_btn)
    
    def start_shutdown(self):
        """开始关机倒计时"""
        if self.is_running:
            return
        
        # 计算总秒数
        self.total_duration = self.custom_slider.value() * 60  # 分钟转秒
        
        # 异步执行系统命令，防止阻塞 UI
        threading.Thread(target=ShutdownManager.shutdown, args=(self.total_duration,), daemon=True).start()
        
        # 记录目标时间（消除时间漂移）
        self.target_time = datetime.now() + timedelta(seconds=self.total_duration)
        
        # 启动定时器
        # [U10] 复用单一 QTimer：此前每次 start 都 new 一个无 parent 的
        # QTimer（旧对象不 deleteLater，反复开始/停止累积无父对象）
        if self.timer_id is None:
            self.timer_id = QTimer()
            self.timer_id.timeout.connect(self.update_countdown)
        self.timer_id.start(1000)
        
        # 立即更新一次倒计时显示，消除启动延迟
        self.update_countdown()
        
        # 更新UI
        self.is_running = True
        self.custom_slider.setEnabled(False)
        
        # 更新按钮状态
        self.update_button_state()
        
        self.status_label.setText(f"⏳ {self.total_duration} 秒后关机")
        self.status_label.setStyleSheet("color: #f85149; font-size: 12px;")
    
    def update_countdown(self):
        """更新倒计时显示（使用时间差计算）"""
        if self.target_time is None:
            return
        
        # 计算剩余秒数
        remaining = (self.target_time - datetime.now()).total_seconds()
        
        # 检查是否超时
        if remaining <= 0:
            self.timer_id.stop()
            self.is_running = False

            # 系统关机已执行
            self.time_label.setText("00:00:00")
            self.pbar.setValue(0)

            # 更新按钮状态
            self.update_button_state()
            # [U10] 到期分支漏恢复 slider（Windows shutdown /a 取消后
            # 无法再调时长重新开始）
            self.custom_slider.setEnabled(True)

            # macOS/Linux 额外保险：强制立即关机。
            # [U10] 最后 1 秒内点"停止"时，本分支的二次 shutdown -h +0 与
            # stop_shutdown 的异步 cancel 赛跑——cancel 可能落败（用户点了
            # 取消仍被强制关机）。改为仅在无取消痕迹时补发：is_running 已
            # 复位说明用户没在这 1 秒内取消，补发才是安全的
            if platform.system() in ["Darwin", "Linux"] and not self._cancel_requested():
                threading.Thread(target=ShutdownManager.shutdown, args=(0,), daemon=True).start()

            self.status_label.setText("✅ 系统关机已执行")
            self.status_label.setStyleSheet("color: #107c10; font-size: 12px;")
            return
        
        # 格式化时间显示
        hours = int(remaining // 3600)
        minutes = int((remaining % 3600) // 60)
        seconds = int(remaining % 60)
        
        time_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        self.time_label.setText(time_str)
        
        # 计算进度条百分比
        progress = (1 - remaining / self.total_duration) * 100
        self.pbar.setValue(int(progress))
    
    def _cancel_requested(self) -> bool:
        """[U10] 最后 1 秒内的取消请求是否在途（异步 cancel 未落地）。"""
        ts = getattr(self, '_cancel_req_ts', None)
        return ts is not None and (time.monotonic() - ts) < 2.0

    def stop_shutdown(self):
        """停止关机倒计时"""
        if not self.is_running:
            return

        # [U10] 记录取消时刻：到期分支据此跳过"强制立即关机"补发（与
        # 异步 cancel 赛跑会让用户的取消落败）
        self._cancel_req_ts = time.monotonic()
        # [R1-13] 取消命令（osascript with administrator privileges）会弹
        # 授权框，同步执行会让整窗冻结——与 start 一致放 daemon 线程
        threading.Thread(target=ShutdownManager.cancel, daemon=True).start()
        
        # 停止定时器
        if self.timer_id:
            self.timer_id.stop()
        
        # 恢复UI
        self.is_running = False
        self.custom_slider.setEnabled(True)
        
        # 更新按钮状态
        self.update_button_state()
        
        # 重置显示
        self.time_label.setText("00:00:00")
        self.pbar.setValue(0)
        
        self.status_label.setText("关机任务已取消")
        self.status_label.setStyleSheet("color: #58a6ff; font-size: 12px;")
    
    def _cancel_on_app_quit(self):
        """[U10] 宿主退出兜底：倒计时仍在运行则同步取消（15s 超时放弃）。"""
        if not getattr(self, 'is_running', False):
            return
        self.is_running = False
        if self.timer_id:
            self.timer_id.stop()
        try:
            t = threading.Thread(target=ShutdownManager.cancel, daemon=True)
            t.start()
            t.join(timeout=15)
        except Exception as e:
            logger.error(f"[U10] 退出前取消关机失败（需手工 killall shutdown）: {e}")

    def quit_app(self):
        # [BUG-P1-18] 此前同步调 ShutdownManager.cancel()（osascript 提权
        # 弹窗冻结 UI），且 is_running 未复位——closeEvent 把 close 拒成
        # hide：窗口退不出，倒计时到 0 还会再发一次强制关机。
        if self.is_running:
            self.stop_shutdown()  # 异步 cancel + 停定时器 + 状态复位

        # Hide tray icon before closing to prevent ghost icons
        if hasattr(self, 'tray_icon'):
            self.tray_icon.hide()

        self.close() # Just close this window, don't kill the app
    
    def closeEvent(self, event):
        """关闭窗口事件（隐藏到托盘）"""
        # 如果正在运行，隐藏窗口而不是退出
        if self.is_running:
            self.hide()
            
            # 使用系统托盘气泡提示
            self.tray_icon.showMessage(
                "程序运行中", 
                "定时关机任务仍在后台运行", 
                QSystemTrayIcon.MessageIcon.Information, 
                2000
            )
            
            event.ignore()
        else:
            # 非运行状态，正常关闭
            if hasattr(self, 'tray_icon'):
                self.tray_icon.hide()
            event.accept()
