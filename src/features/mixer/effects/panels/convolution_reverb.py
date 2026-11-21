from __future__ import annotations

import os
import sys
import math
import copy
import random
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QFrame, QLineEdit, QFileDialog,
    QInputDialog, QListWidget, QListWidgetItem, QStackedWidget,
    QAbstractItemView
)
from PyQt6.QtCore import Qt, QRectF, QPointF, pyqtSignal, QTimer, QSignalBlocker
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QBrush, QLinearGradient,
    QPainterPath, QFont, QPolygonF
)

from src.shared.i18n.i18n import I18N
from src.shared.ui.widgets.chevron_combo import ChevronComboBox


# =====================================================================
# 1. 全局色彩系统与声学常量（统一 Fairlight 深色哑光视觉规范）
# =====================================================================
UI_FONT_CSS = "'Helvetica Neue', 'PingFang SC'"
MONO_FONT_CSS = "'Menlo'"
MONO_FONT_FAM = "Menlo"

SUITE_PALETTE = {
    "reverb":  "#28D8F0",   # 冰晶青: IR 尾音与核心卷积
    "er":      "#F0A848",   # 琥珀金: 早期反射 (ER) 与分割边界
    "tone":    "#4CD490",   # 翡翠绿: 湿声均衡 (IR Tone)
    "output":  "#6495FA",   # 钴蓝:   输出混音 (Output)
    "capture": "#F26D6D",   # 珊瑚红: 扫频录制 (Sine Sweep Capture)
    "direct":  "#F2F8FC",   # 亮白:   直达声 / 刻度高亮
}

# 内置影视后期典型空间 IR 库（用于演示与初始预设，后端加载真实 WAV 后可替换波形数据）
BUILTIN_IR_LIBRARY = {
    # =================================================================
    # 1. 影视后期：对白空间匹配 (Post / Dialogue & ADR)
    # =================================================================
    "Film ADR / Tight Booth": {
        "category": "Post / Dialogue",
        "file_path": "builtin://ADR_Tight_Booth_48k.wav",
        "sample_rate": 48000,
        "native_ms": 380.0,
        "seed": 101,
        "enabled": True, "reverse": False, "wet_lock": False,
        "pre_delay": 2.0, "ir_length": 100.0, "fade_in": 0.0, "fade_out": 15.0,
        "trim_start": 0.0, "trim_end": 100.0,
        "er_split_ms": 28.0, "er_gain": 1.5, "tail_gain": -4.5, "stereo_width": 65.0,
        "lo_freq": 240.0, "hi_freq": 8500.0, "lo_gain": -3.5, "hi_gain": -2.8,
        "dry_wet": 18.0, "direct_db": 0.0, "er_db": 0.0, "tail_db": -3.0,
    },
    "Close Boom / Dry Set": {
        "category": "Post / Dialogue",
        "file_path": "builtin://Close_Boom_Dry_Set_48k.wav",
        "sample_rate": 48000,
        "native_ms": 260.0,
        "seed": 102,
        "enabled": True, "reverse": False, "wet_lock": False,
        "pre_delay": 0.0, "ir_length": 90.0, "fade_in": 0.0, "fade_out": 12.0,
        "trim_start": 0.0, "trim_end": 100.0,
        "er_split_ms": 22.0, "er_gain": 2.2, "tail_gain": -7.5, "stereo_width": 45.0,
        "lo_freq": 260.0, "hi_freq": 9200.0, "lo_gain": -4.0, "hi_gain": -2.0,
        "dry_wet": 15.0, "direct_db": 0.0, "er_db": 0.5, "tail_db": -6.0,
    },
    "Wide Shot / Distant Mic": {
        "category": "Post / Dialogue",
        "file_path": "builtin://Wide_Shot_Distant_48k.wav",
        "sample_rate": 48000,
        "native_ms": 780.0,
        "seed": 103,
        "enabled": True, "reverse": False, "wet_lock": False,
        "pre_delay": 18.0, "ir_length": 105.0, "fade_in": 2.0, "fade_out": 20.0,
        "trim_start": 0.0, "trim_end": 100.0,
        "er_split_ms": 58.0, "er_gain": 3.5, "tail_gain": 0.5, "stereo_width": 110.0,
        "lo_freq": 220.0, "hi_freq": 6200.0, "lo_gain": -2.5, "hi_gain": -5.5,
        "dry_wet": 42.0, "direct_db": -1.5, "er_db": 1.5, "tail_db": 0.0,
    },
    "Phone / Intercom Box": {
        "category": "Post / Dialogue",
        "file_path": "builtin://Small_Booth_Box_48k.wav",
        "sample_rate": 48000,
        "native_ms": 190.0,
        "seed": 104,
        "enabled": True, "reverse": False, "wet_lock": False,
        "pre_delay": 0.0, "ir_length": 85.0, "fade_in": 0.0, "fade_out": 10.0,
        "trim_start": 0.0, "trim_end": 100.0,
        "er_split_ms": 16.0, "er_gain": 4.5, "tail_gain": -12.0, "stereo_width": 20.0,
        "lo_freq": 420.0, "hi_freq": 3800.0, "lo_gain": -9.0, "hi_gain": -10.5,
        "dry_wet": 32.0, "direct_db": 0.0, "er_db": 2.0, "tail_db": -9.0,
    },

    # =================================================================
    # 2. 影视后期：室内实景空间 (Post / Interior Spaces)
    # =================================================================
    "Suburban Living Room": {
        "category": "Post / Interior",
        "file_path": "builtin://Suburban_LivingRoom_48k.wav",
        "sample_rate": 48000,
        "native_ms": 640.0,
        "seed": 201,
        "enabled": True, "reverse": False, "wet_lock": False,
        "pre_delay": 6.0, "ir_length": 100.0, "fade_in": 0.0, "fade_out": 20.0,
        "trim_start": 0.0, "trim_end": 100.0,
        "er_split_ms": 48.0, "er_gain": 2.0, "tail_gain": -2.0, "stereo_width": 95.0,
        "lo_freq": 210.0, "hi_freq": 7200.0, "lo_gain": -2.0, "hi_gain": -4.2,
        "dry_wet": 24.0, "direct_db": 0.0, "er_db": 0.5, "tail_db": -1.5,
    },
    "Tiled Bathroom / Shower": {
        "category": "Post / Interior",
        "file_path": "builtin://Tiled_Bathroom_48k.wav",
        "sample_rate": 48000,
        "native_ms": 1150.0,
        "seed": 202,
        "enabled": True, "reverse": False, "wet_lock": False,
        "pre_delay": 4.0, "ir_length": 100.0, "fade_in": 0.0, "fade_out": 18.0,
        "trim_start": 0.0, "trim_end": 100.0,
        "er_split_ms": 38.0, "er_gain": 4.2, "tail_gain": 1.8, "stereo_width": 115.0,
        "lo_freq": 280.0, "hi_freq": 12500.0, "lo_gain": -3.0, "hi_gain": 3.2,
        "dry_wet": 36.0, "direct_db": 0.0, "er_db": 2.0, "tail_db": 0.5,
    },
    "Modern Office / Glass Room": {
        "category": "Post / Interior",
        "file_path": "builtin://Modern_Glass_Office_48k.wav",
        "sample_rate": 48000,
        "native_ms": 820.0,
        "seed": 203,
        "enabled": True, "reverse": False, "wet_lock": False,
        "pre_delay": 8.0, "ir_length": 100.0, "fade_in": 0.0, "fade_out": 22.0,
        "trim_start": 0.0, "trim_end": 100.0,
        "er_split_ms": 52.0, "er_gain": 2.8, "tail_gain": -1.5, "stereo_width": 105.0,
        "lo_freq": 230.0, "hi_freq": 9800.0, "lo_gain": -2.5, "hi_gain": 1.2,
        "dry_wet": 26.0, "direct_db": 0.0, "er_db": 1.0, "tail_db": -1.0,
    },
    "Hospital Corridor": {
        "category": "Post / Interior",
        "file_path": "builtin://Hospital_Corridor_48k.wav",
        "sample_rate": 48000,
        "native_ms": 1520.0,
        "seed": 204,
        "enabled": True, "reverse": False, "wet_lock": False,
        "pre_delay": 16.0, "ir_length": 100.0, "fade_in": 3.0, "fade_out": 28.0,
        "trim_start": 0.0, "trim_end": 100.0,
        "er_split_ms": 78.0, "er_gain": 3.0, "tail_gain": 1.2, "stereo_width": 125.0,
        "lo_freq": 240.0, "hi_freq": 10500.0, "lo_gain": -2.0, "hi_gain": 1.5,
        "dry_wet": 33.0, "direct_db": 0.0, "er_db": 1.2, "tail_db": 0.0,
    },
    "Wooden Cabin / Library": {
        "category": "Post / Interior",
        "file_path": "builtin://Wooden_Library_48k.wav",
        "sample_rate": 48000,
        "native_ms": 540.0,
        "seed": 205,
        "enabled": True, "reverse": False, "wet_lock": False,
        "pre_delay": 5.0, "ir_length": 100.0, "fade_in": 0.0, "fade_out": 18.0,
        "trim_start": 0.0, "trim_end": 100.0,
        "er_split_ms": 42.0, "er_gain": 1.8, "tail_gain": -2.5, "stereo_width": 90.0,
        "lo_freq": 190.0, "hi_freq": 6000.0, "lo_gain": 1.2, "hi_gain": -5.8,
        "dry_wet": 23.0, "direct_db": 0.0, "er_db": 0.5, "tail_db": -2.0,
    },
    "Courtroom / Lecture Hall": {
        "category": "Post / Interior",
        "file_path": "builtin://Courtroom_Hall_48k.wav",
        "sample_rate": 48000,
        "native_ms": 1340.0,
        "seed": 206,
        "enabled": True, "reverse": False, "wet_lock": False,
        "pre_delay": 15.0, "ir_length": 100.0, "fade_in": 2.0, "fade_out": 25.0,
        "trim_start": 0.0, "trim_end": 100.0,
        "er_split_ms": 68.0, "er_gain": 2.2, "tail_gain": 0.0, "stereo_width": 118.0,
        "lo_freq": 200.0, "hi_freq": 8800.0, "lo_gain": -1.0, "hi_gain": -2.2,
        "dry_wet": 29.0, "direct_db": 0.0, "er_db": 0.8, "tail_db": -1.0,
    },

    # =================================================================
    # 3. 影视后期：交通工具狭小座舱 (Post / Vehicles)
    # =================================================================
    "Car Sedan Interior": {
        "category": "Post / Vehicle",
        "file_path": "builtin://Car_Sedan_Cabin_48k.wav",
        "sample_rate": 48000,
        "native_ms": 210.0,
        "seed": 301,
        "enabled": True, "reverse": False, "wet_lock": False,
        "pre_delay": 0.0, "ir_length": 100.0, "fade_in": 0.0, "fade_out": 10.0,
        "trim_start": 0.0, "trim_end": 100.0,
        "er_split_ms": 18.0, "er_gain": 3.5, "tail_gain": -9.0, "stereo_width": 50.0,
        "lo_freq": 320.0, "hi_freq": 5400.0, "lo_gain": -5.0, "hi_gain": -6.5,
        "dry_wet": 26.0, "direct_db": 0.0, "er_db": 2.0, "tail_db": -8.0,
    },
    "SUV / Van Cabin": {
        "category": "Post / Vehicle",
        "file_path": "builtin://SUV_Van_Cabin_48k.wav",
        "sample_rate": 48000,
        "native_ms": 310.0,
        "seed": 302,
        "enabled": True, "reverse": False, "wet_lock": False,
        "pre_delay": 1.0, "ir_length": 100.0, "fade_in": 0.0, "fade_out": 12.0,
        "trim_start": 0.0, "trim_end": 100.0,
        "er_split_ms": 24.0, "er_gain": 3.0, "tail_gain": -6.5, "stereo_width": 65.0,
        "lo_freq": 280.0, "hi_freq": 6200.0, "lo_gain": -3.5, "hi_gain": -5.0,
        "dry_wet": 28.0, "direct_db": 0.0, "er_db": 1.8, "tail_db": -5.5,
    },
    "Subway / Train Carriage": {
        "category": "Post / Vehicle",
        "file_path": "builtin://Subway_Carriage_48k.wav",
        "sample_rate": 48000,
        "native_ms": 680.0,
        "seed": 303,
        "enabled": True, "reverse": False, "wet_lock": False,
        "pre_delay": 4.0, "ir_length": 100.0, "fade_in": 0.0, "fade_out": 16.0,
        "trim_start": 0.0, "trim_end": 100.0,
        "er_split_ms": 36.0, "er_gain": 3.6, "tail_gain": -1.5, "stereo_width": 95.0,
        "lo_freq": 250.0, "hi_freq": 9500.0, "lo_gain": -3.0, "hi_gain": 1.4,
        "dry_wet": 30.0, "direct_db": 0.0, "er_db": 1.5, "tail_db": -1.5,
    },
    "Aircraft Cabin / Cockpit": {
        "category": "Post / Vehicle",
        "file_path": "builtin://Aircraft_Cabin_48k.wav",
        "sample_rate": 48000,
        "native_ms": 420.0,
        "seed": 304,
        "enabled": True, "reverse": False, "wet_lock": False,
        "pre_delay": 2.0, "ir_length": 95.0, "fade_in": 0.0, "fade_out": 14.0,
        "trim_start": 0.0, "trim_end": 100.0,
        "er_split_ms": 28.0, "er_gain": 2.8, "tail_gain": -5.0, "stereo_width": 75.0,
        "lo_freq": 300.0, "hi_freq": 6800.0, "lo_gain": -4.5, "hi_gain": -3.8,
        "dry_wet": 24.0, "direct_db": 0.0, "er_db": 1.2, "tail_db": -4.5,
    },

    # =================================================================
    # 4. 影视后期：外景、工业与地下空间 (Post / Exterior & Underground)
    # =================================================================
    "Concrete Stairwell": {
        "category": "Post / Exterior & Industrial",
        "file_path": "builtin://Concrete_Stairwell_48k.wav",
        "sample_rate": 48000,
        "native_ms": 1850.0,
        "seed": 401,
        "enabled": True, "reverse": False, "wet_lock": False,
        "pre_delay": 14.0, "ir_length": 100.0, "fade_in": 2.0, "fade_out": 30.0,
        "trim_start": 0.0, "trim_end": 100.0,
        "er_split_ms": 72.0, "er_gain": 3.0, "tail_gain": 1.0, "stereo_width": 120.0,
        "lo_freq": 180.0, "hi_freq": 11500.0, "lo_gain": -1.5, "hi_gain": 1.8,
        "dry_wet": 34.0, "direct_db": 0.0, "er_db": 1.2, "tail_db": 0.0,
    },
    "Underground Parking Garage": {
        "category": "Post / Exterior & Industrial",
        "file_path": "builtin://Underground_Garage_48k.wav",
        "sample_rate": 48000,
        "native_ms": 2350.0,
        "seed": 402,
        "enabled": True, "reverse": False, "wet_lock": False,
        "pre_delay": 24.0, "ir_length": 100.0, "fade_in": 4.0, "fade_out": 35.0,
        "trim_start": 0.0, "trim_end": 100.0,
        "er_split_ms": 95.0, "er_gain": 3.8, "tail_gain": 2.0, "stereo_width": 135.0,
        "lo_freq": 170.0, "hi_freq": 8200.0, "lo_gain": 1.5, "hi_gain": -2.5,
        "dry_wet": 38.0, "direct_db": 0.0, "er_db": 1.5, "tail_db": 0.5,
    },
    "Narrow City Alley": {
        "category": "Post / Exterior & Industrial",
        "file_path": "builtin://Narrow_City_Alley_48k.wav",
        "sample_rate": 48000,
        "native_ms": 980.0,
        "seed": 403,
        "enabled": True, "reverse": False, "wet_lock": False,
        "pre_delay": 22.0, "ir_length": 100.0, "fade_in": 0.0, "fade_out": 24.0,
        "trim_start": 0.0, "trim_end": 100.0,
        "er_split_ms": 82.0, "er_gain": 4.0, "tail_gain": -3.5, "stereo_width": 125.0,
        "lo_freq": 220.0, "hi_freq": 7600.0, "lo_gain": -3.0, "hi_gain": -3.5,
        "dry_wet": 27.0, "direct_db": 0.0, "er_db": 2.2, "tail_db": -3.0,
    },
    "Abandoned Warehouse": {
        "category": "Post / Exterior & Industrial",
        "file_path": "builtin://Abandoned_Warehouse_48k.wav",
        "sample_rate": 48000,
        "native_ms": 2950.0,
        "seed": 404,
        "enabled": True, "reverse": False, "wet_lock": False,
        "pre_delay": 32.0, "ir_length": 100.0, "fade_in": 6.0, "fade_out": 38.0,
        "trim_start": 0.0, "trim_end": 100.0,
        "er_split_ms": 115.0, "er_gain": 2.5, "tail_gain": 2.2, "stereo_width": 145.0,
        "lo_freq": 190.0, "hi_freq": 9500.0, "lo_gain": 0.5, "hi_gain": -1.2,
        "dry_wet": 40.0, "direct_db": 0.0, "er_db": 1.0, "tail_db": 1.0,
    },
    "Dense Forest / Canyon": {
        "category": "Post / Exterior & Industrial",
        "file_path": "builtin://Forest_Canyon_48k.wav",
        "sample_rate": 48000,
        "native_ms": 1650.0,
        "seed": 405,
        "enabled": True, "reverse": False, "wet_lock": False,
        "pre_delay": 48.0, "ir_length": 110.0, "fade_in": 10.0, "fade_out": 42.0,
        "trim_start": 0.0, "trim_end": 100.0,
        "er_split_ms": 130.0, "er_gain": 1.5, "tail_gain": -1.0, "stereo_width": 150.0,
        "lo_freq": 250.0, "hi_freq": 4800.0, "lo_gain": -4.0, "hi_gain": -7.5,
        "dry_wet": 31.0, "direct_db": 0.0, "er_db": 0.5, "tail_db": -1.5,
    },

    # =================================================================
    # 5. 音乐混音：音乐厅、录音棚与经典硬件板式 (Music / Hall & Studio)
    # =================================================================
    "Scoring Stage A (2.4s)": {
        "category": "Music / Hall",
        "file_path": "builtin://Scoring_Stage_A_48k.wav",
        "sample_rate": 48000,
        "native_ms": 2450.0,
        "seed": 501,
        "enabled": True, "reverse": False, "wet_lock": False,
        "pre_delay": 28.0, "ir_length": 100.0, "fade_in": 5.0, "fade_out": 35.0,
        "trim_start": 0.0, "trim_end": 100.0,
        "er_split_ms": 85.0, "er_gain": 0.0, "tail_gain": 0.5, "stereo_width": 135.0,
        "lo_freq": 200.0, "hi_freq": 13000.0, "lo_gain": 0.8, "hi_gain": -1.5,
        "dry_wet": 38.0, "direct_db": 0.0, "er_db": -1.0, "tail_db": 0.0,
    },
    "Grand Cathedral (3.8s)": {
        "category": "Music / Large Space",
        "file_path": "builtin://Grand_Cathedral_48k.wav",
        "sample_rate": 48000,
        "native_ms": 3800.0,
        "seed": 502,
        "enabled": True, "reverse": False, "wet_lock": False,
        "pre_delay": 45.0, "ir_length": 100.0, "fade_in": 12.0, "fade_out": 40.0,
        "trim_start": 0.0, "trim_end": 100.0,
        "er_split_ms": 110.0, "er_gain": -1.0, "tail_gain": 1.5, "stereo_width": 150.0,
        "lo_freq": 260.0, "hi_freq": 10500.0, "lo_gain": 1.2, "hi_gain": -2.5,
        "dry_wet": 48.0, "direct_db": 0.0, "er_db": -1.5, "tail_db": 0.5,
    },
    "EMT 140 Vocal Plate": {
        "category": "Music / Vintage Plate",
        "file_path": "builtin://Vintage_Gold_Plate_48k.wav",
        "sample_rate": 48000,
        "native_ms": 1950.0,
        "seed": 503,
        "enabled": True, "reverse": False, "wet_lock": False,
        "pre_delay": 32.0, "ir_length": 100.0, "fade_in": 0.0, "fade_out": 28.0,
        "trim_start": 0.0, "trim_end": 100.0,
        "er_split_ms": 20.0, "er_gain": -6.0, "tail_gain": 2.0, "stereo_width": 125.0,
        "lo_freq": 280.0, "hi_freq": 12000.0, "lo_gain": -3.5, "hi_gain": 2.4,
        "dry_wet": 28.0, "direct_db": 0.0, "er_db": -8.0, "tail_db": 0.5,
    },
    "Abbey Live Drum Room": {
        "category": "Music / Studio Room",
        "file_path": "builtin://Live_Drum_Room_48k.wav",
        "sample_rate": 48000,
        "native_ms": 1120.0,
        "seed": 504,
        "enabled": True, "reverse": False, "wet_lock": False,
        "pre_delay": 12.0, "ir_length": 100.0, "fade_in": 0.0, "fade_out": 20.0,
        "trim_start": 0.0, "trim_end": 100.0,
        "er_split_ms": 55.0, "er_gain": 3.5, "tail_gain": 1.0, "stereo_width": 130.0,
        "lo_freq": 160.0, "hi_freq": 11000.0, "lo_gain": 1.8, "hi_gain": 1.0,
        "dry_wet": 32.0, "direct_db": 0.0, "er_db": 1.5, "tail_db": 0.0,
    },

    # =================================================================
    # 6. 特殊音效与预告片设计 (SFX & Creative Reverse)
    # =================================================================
    "Trailer Reverse Suctions": {
        "category": "SFX / Creative",
        "file_path": "builtin://Scoring_Stage_A_48k.wav",
        "sample_rate": 48000,
        "native_ms": 2200.0,
        "seed": 601,
        "enabled": True, "reverse": True, "wet_lock": False,
        "pre_delay": 0.0, "ir_length": 115.0, "fade_in": 25.0, "fade_out": 5.0,
        "trim_start": 0.0, "trim_end": 100.0,
        "er_split_ms": 60.0, "er_gain": -3.0, "tail_gain": 3.0, "stereo_width": 160.0,
        "lo_freq": 220.0, "hi_freq": 14000.0, "lo_gain": -2.0, "hi_gain": 3.0,
        "dry_wet": 65.0, "direct_db": -4.0, "er_db": -2.0, "tail_db": 2.0,
    },
    "Sci-Fi Metallic Chamber": {
        "category": "SFX / Creative",
        "file_path": "builtin://Metallic_Tank_48k.wav",
        "sample_rate": 48000,
        "native_ms": 3200.0,
        "seed": 602,
        "enabled": True, "reverse": False, "wet_lock": False,
        "pre_delay": 10.0, "ir_length": 130.0, "fade_in": 4.0, "fade_out": 30.0,
        "trim_start": 0.0, "trim_end": 100.0,
        "er_split_ms": 45.0, "er_gain": 4.5, "tail_gain": 2.5, "stereo_width": 175.0,
        "lo_freq": 240.0, "hi_freq": 13500.0, "lo_gain": -3.0, "hi_gain": 3.8,
        "dry_wet": 55.0, "direct_db": -2.0, "er_db": 2.0, "tail_db": 1.5,
    },
}


def generate_synthetic_ir_peaks(seed: int, native_ms: float, num_bins: int = 360) -> tuple[list[float], list[float]]:
    """生成演示用立体声脉冲响应峰值包络 (L/R 0.0 ~ 1.0)，接入真实后端时可由 load_ir_waveform 替换"""
    rng = random.Random(seed)
    peaks_l, peaks_r = [], []
    rt_sec = max(0.15, native_ms / 1000.0)
    for i in range(num_bins):
        t = (i / max(1, num_bins - 1)) * rt_sec
        # 初期离散反射簇 + 指数衰减扩散尾音
        if t < 0.045:
            spike = 1.0 if (i % 4 == 0 or i < 3) else rng.uniform(0.15, 0.55)
            env = spike * math.exp(-t * 8.0)
        else:
            env = math.exp(-5.8 * (t / rt_sec)) * rng.uniform(0.45, 1.0)
        peaks_l.append(max(0.0, min(1.0, env * rng.uniform(0.85, 1.05))))
        peaks_r.append(max(0.0, min(1.0, env * rng.uniform(0.85, 1.05))))
    return peaks_l, peaks_r


# =====================================================================
# 2. 基础定制控件：电源开关 / 可编辑数字胶囊 / 阳极铝旋钮
# =====================================================================
class PowerToggleSwitch(QWidget):
    toggled = pyqtSignal(bool)

    def __init__(self, checked: bool = True, accent_color: str = "#28D8F0", parent=None):
        super().__init__(parent)
        self._checked = checked
        self._accent = QColor(accent_color)
        self.setFixedSize(34, 17)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, val: bool, emit: bool = False):
        if self._checked != val:
            self._checked = val
            self.update()
            if emit:
                self.toggled.emit(self._checked)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setChecked(not self._checked, emit=True)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        if self._checked:
            bg = QColor(self._accent.red(), self._accent.green(), self._accent.blue(), 34)
            border = self._accent
            knob_color = self._accent
        else:
            bg = QColor("#121215")
            border = QColor("#36363D")
            knob_color = QColor("#666670")

        p.setPen(QPen(border, 1.0))
        p.setBrush(bg)
        p.drawRoundedRect(rect, 8.5, 8.5)

        r = 5.0
        cy = self.height() / 2.0
        cx = (self.width() - 8.6) if self._checked else 8.6
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(knob_color)
        p.drawEllipse(QPointF(cx, cy), r, r)


class EditableValuePill(QWidget):
    valueEdited = pyqtSignal(float)

    def __init__(
        self,
        min_val: float,
        max_val: float,
        decimals: int = 1,
        accent_color: str = "#28D8F0",
        allow_neg_inf: bool = False,
        k_format: bool = False,
        suffix: str = "",
        width: int = 64,
        parent=None,
    ):
        super().__init__(parent)
        self._min = min_val
        self._max = max_val
        self._decimals = decimals
        self._accent = accent_color
        self._allow_neg_inf = allow_neg_inf
        self._k_format = k_format
        self._suffix = suffix
        self._value = 0.0
        self._text = "0.0"
        self._active = True

        self.setFixedSize(width, 21)
        self.setCursor(Qt.CursorShape.IBeamCursor)

        self._editor = QLineEdit(self)
        self._editor.setGeometry(1, 1, width - 2, 19)
        self._editor.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._editor.setStyleSheet(f"""
            QLineEdit {{
                background-color: #151518;
                color: {accent_color};
                border: 1px solid {accent_color};
                border-radius: 3px;
                font-family: {MONO_FONT_CSS};
                font-size: 10.5px;
                font-weight: 700;
                padding: 0px 2px;
            }}
        """)
        self._editor.hide()
        self._editor.returnPressed.connect(self._commit_edit)
        self._editor.editingFinished.connect(self._commit_edit)

    def setValue(self, val: float):
        self._value = max(self._min, min(self._max, val))
        self._text = self._format_val(self._value)
        self.update()

    def setBandActive(self, active: bool):
        self._active = active
        self.update()

    def _format_val(self, val: float) -> str:
        if self._allow_neg_inf and val <= self._min + 0.05:
            return "-Inf"
        if self._k_format and abs(val) >= 1000.0:
            return f"{val / 1000.0:.1f}k{self._suffix}"
        if self._decimals == 0:
            return f"{int(round(val))}{self._suffix}"
        return f"{val:.{self._decimals}f}{self._suffix}"

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            clean = self._text.replace("%", "").replace("ms", "").replace("dB", "").strip()
            self._editor.setText(clean)
            self._editor.show()
            self._editor.setFocus()
            self._editor.selectAll()

    def _commit_edit(self):
        if not self._editor.isVisible():
            return
        raw = self._editor.text().strip().lower().replace("%", "").replace("ms", "").replace("db", "")
        self._editor.hide()
        if not raw:
            return
        try:
            if self._allow_neg_inf and raw in ("-inf", "inf", "-oo"):
                parsed = self._min
            elif raw.endswith("k"):
                parsed = float(raw[:-1]) * 1000.0
            else:
                parsed = float(raw)
            parsed = max(self._min, min(self._max, parsed))
            self.setValue(parsed)
            self.valueEdited.emit(parsed)
        except ValueError:
            pass

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        p.setPen(QPen(QColor("#121214"), 1.0))
        p.setBrush(QColor("#151518"))
        p.drawRoundedRect(rect, 4.0, 4.0)

        p.setPen(QColor("#ECECF0") if self._active else QColor("#6E6E76"))
        font = QFont(MONO_FONT_FAM, 9)
        font.setWeight(QFont.Weight.Bold)
        p.setFont(font)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._text)


class ProRotaryKnob(QWidget):
    valueChanged = pyqtSignal(float)

    def __init__(
        self,
        minimum: float,
        maximum: float,
        value: float,
        default_value: float,
        accent_color: str = "#28D8F0",
        mode: str = "linear",      # "linear" | "log" | "bipolar"
        origin_value: float = 0.0,
        zero_db_marker: bool = False,
        diameter: int = 44,
        parent=None,
    ):
        super().__init__(parent)
        self._min = minimum
        self._max = maximum
        self._value = value
        self._default = default_value
        self._accent = QColor(accent_color)
        self._mode = mode
        self._origin_val = origin_value
        self._zero_db_marker = zero_db_marker
        self._diameter = diameter

        self._active = True
        self._dragging = False
        self._hovered = False
        self._last_y = 0.0

        self.setFixedSize(diameter + 6, diameter)
        self.setCursor(Qt.CursorShape.SizeVerCursor)

    def value(self) -> float:
        return self._value

    def setValue(self, val: float, emit: bool = False):
        clamped = max(self._min, min(self._max, val))
        if abs(clamped - self._value) > 1e-6:
            self._value = clamped
            self.update()
            if emit:
                self.valueChanged.emit(self._value)

    def setRange(self, minimum: float, maximum: float):
        self._min = minimum
        self._max = maximum
        self._value = max(self._min, min(self._max, self._value))
        self.update()

    def setBandActive(self, active: bool):
        self._active = active
        self.update()

    def _val_to_norm(self, val: float) -> float:
        if self._max <= self._min:
            return 0.0
        if self._mode == "log":
            v = max(1e-3, val)
            mn = max(1e-3, self._min)
            mx = max(mn * 1.001, self._max)
            return math.log(v / mn) / math.log(mx / mn)
        if self._mode == "bipolar" and self._min < self._origin_val < self._max:
            if val < self._origin_val:
                return 0.5 * (val - self._min) / (self._origin_val - self._min)
            return 0.5 + 0.5 * (val - self._origin_val) / (self._max - self._origin_val)
        return (val - self._min) / (self._max - self._min)

    def _norm_to_val(self, norm: float) -> float:
        n = max(0.0, min(1.0, norm))
        if self._mode == "log":
            mn = max(1e-3, self._min)
            mx = max(mn * 1.001, self._max)
            return mn * ((mx / mn) ** n)
        if self._mode == "bipolar" and self._min < self._origin_val < self._max:
            if n < 0.5:
                return self._min + (n / 0.5) * (self._origin_val - self._min)
            return self._origin_val + ((n - 0.5) / 0.5) * (self._max - self._origin_val)
        return self._min + n * (self._max - self._min)

    def enterEvent(self, event):
        self._hovered = True
        self.update()

    def leaveEvent(self, event):
        self._hovered = False
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._last_y = event.position().y()
        elif event.button() == Qt.MouseButton.RightButton:
            self.setValue(self._default, emit=True)

    def mouseMoveEvent(self, event):
        if self._dragging:
            dy = self._last_y - event.position().y()
            self._last_y = event.position().y()
            sens = 0.0015 if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 0.0065
            norm = self._val_to_norm(self._value) + dy * sens
            self.setValue(self._norm_to_val(norm), emit=True)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setValue(self._default, emit=True)

    def wheelEvent(self, event):
        delta = event.angleDelta().y() / 120.0
        step = 0.01 if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 0.03
        norm = self._val_to_norm(self._value) + delta * step
        self.setValue(self._norm_to_val(norm), emit=True)
        event.accept()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        cx = self.width() / 2.0
        cy = self.height() / 2.0
        norm = max(0.0, min(1.0, self._val_to_norm(self._value)))

        start_deg = 225.0
        total_span = -270.0
        curr_deg = start_deg + norm * total_span

        arc_r = (self._diameter / 2.0) - 3.0
        arc_rect = QRectF(cx - arc_r, cy - arc_r, arc_r * 2, arc_r * 2)

        p.setPen(QPen(QColor("#141417"), 3.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(arc_rect, int(start_deg * 16), int(total_span * 16))

        if self._mode == "bipolar":
            zero_norm = self._val_to_norm(self._origin_val)
            z_deg = start_deg + zero_norm * total_span
            z_rad = math.radians(z_deg)
            p.setPen(QPen(QColor("#52525B"), 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(
                QPointF(cx + (arc_r - 0.8) * math.cos(z_rad), cy - (arc_r - 0.8) * math.sin(z_rad)),
                QPointF(cx + (arc_r + 2.4) * math.cos(z_rad), cy - (arc_r + 2.4) * math.sin(z_rad)),
            )

        if self._zero_db_marker:
            zero_norm = self._val_to_norm(0.0)
            z_rad = math.radians(start_deg + zero_norm * total_span)
            p1 = QPointF(cx + (arc_r + 1.0) * math.cos(z_rad), cy - (arc_r + 1.0) * math.sin(z_rad))
            p2 = QPointF(cx + (arc_r + 3.6) * math.cos(z_rad), cy - (arc_r + 3.6) * math.sin(z_rad))
            p.setPen(QPen(QColor("#9A9AA2"), 1.2))
            p.drawLine(p1, p2)

        arc_col = self._accent if self._active else QColor("#4A4A52")
        p.setPen(QPen(arc_col, 3.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        if self._mode == "bipolar":
            zero_norm = self._val_to_norm(self._origin_val)
            zero_deg = start_deg + zero_norm * total_span
            span_deg = curr_deg - zero_deg
            if abs(span_deg) > 0.5:
                p.drawArc(arc_rect, int(zero_deg * 16), int(span_deg * 16))
        else:
            span_deg = curr_deg - start_deg
            if abs(span_deg) > 0.5:
                p.drawArc(arc_rect, int(start_deg * 16), int(span_deg * 16))

        knob_r = arc_r - 3.6
        p.setPen(QPen(QColor("#111114"), 1.2))
        p.setBrush(QColor("#8E9096" if (self._hovered or self._dragging) else "#7A7C82"))
        p.drawEllipse(QPointF(cx, cy), knob_r, knob_r)

        rad = math.radians(curr_deg)
        ix1 = cx + math.cos(rad) * (knob_r * 0.30)
        iy1 = cy - math.sin(rad) * (knob_r * 0.30)
        ix2 = cx + math.cos(rad) * (knob_r - 1.5)
        iy2 = cy - math.sin(rad) * (knob_r - 1.5)
        ptr_col = QColor("#141416") if self._active else QColor("#55555C")
        p.setPen(QPen(ptr_col, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(ix1, iy1), QPointF(ix2, iy2))


class KnobControlUnit(QWidget):
    valueChanged = pyqtSignal(str, float)

    def __init__(
        self,
        param_key: str,
        title: str,
        min_val: float,
        max_val: float,
        default_val: float,
        unit: str = "",
        left_hint: str = "",
        right_hint: str = "",
        decimals: int = 1,
        accent_color: str = "#28D8F0",
        mode: str = "linear",
        origin_value: float = 0.0,
        allow_neg_inf: bool = False,
        k_format: bool = False,
        zero_db_marker: bool = False,
        suffix: str = "",
        knob_diameter: int = 44,
        parent=None,
    ):
        super().__init__(parent)
        self.param_key = param_key
        self._accent = accent_color

        l_str = left_hint or self._fmt_hint(min_val, allow_neg_inf, k_format)
        r_str = right_hint or self._fmt_hint(max_val, False, k_format, show_plus=(min_val < 0 and max_val <= 24))

        self.knob = ProRotaryKnob(
            min_val, max_val, default_val, default_val,
            accent_color=accent_color, mode=mode,
            origin_value=origin_value, zero_db_marker=zero_db_marker,
            diameter=knob_diameter,
        )
        self.pill = EditableValuePill(
            min_val, max_val, decimals=decimals, accent_color=accent_color,
            allow_neg_inf=allow_neg_inf, k_format=k_format, suffix=suffix, width=62,
        )
        self.pill.setValue(default_val)

        self.knob.valueChanged.connect(self._on_knob)
        self.pill.valueEdited.connect(self._on_pill)

        root = QVBoxLayout(self)
        root.setContentsMargins(1, 2, 1, 2)
        root.setSpacing(2)

        self.title_lbl = QLabel(title)
        self.title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_lbl.setStyleSheet("color: #9A9AA2; font-size: 10.5px; font-weight: 600;")
        root.addWidget(self.title_lbl)

        root.addWidget(self.knob, 0, Qt.AlignmentFlag.AlignCenter)

        hints_row = QHBoxLayout()
        hints_row.setContentsMargins(2, 0, 2, 0)
        hints_row.setSpacing(2)
        lbl_l = QLabel(l_str)
        lbl_u = QLabel(unit)
        lbl_r = QLabel(r_str)
        for lb in (lbl_l, lbl_u, lbl_r):
            lb.setStyleSheet("color: #70707A; font-size: 8.5px;")
        hints_row.addWidget(lbl_l)
        hints_row.addStretch()
        if unit:
            hints_row.addWidget(lbl_u)
            hints_row.addStretch()
        hints_row.addWidget(lbl_r)
        root.addLayout(hints_row)

        root.addWidget(self.pill, 0, Qt.AlignmentFlag.AlignCenter)

    @staticmethod
    def _fmt_hint(val: float, neg_inf: bool, k_fmt: bool, show_plus: bool = False) -> str:
        if neg_inf:
            return "-Inf"
        if k_fmt and abs(val) >= 1000:
            return f"{int(val // 1000)}k"
        if val == int(val):
            iv = int(val)
            return f"+{iv}" if (show_plus and iv > 0) else str(iv)
        return f"{val:.1f}"

    def value(self) -> float:
        return self.knob.value()

    def setValue(self, val: float, emit: bool = False):
        self.knob.setValue(val, emit=False)
        self.pill.setValue(self.knob.value())
        if emit:
            self.valueChanged.emit(self.param_key, self.knob.value())

    def setBandActive(self, active: bool):
        self.knob.setBandActive(active)
        self.pill.setBandActive(active)
        self.title_lbl.setStyleSheet(
            f"color: {'#9A9AA2' if active else '#64646C'}; font-size: 10.5px; font-weight: 600;"
        )

    def _on_knob(self, val: float):
        self.pill.setValue(val)
        self.valueChanged.emit(self.param_key, val)

    def _on_pill(self, val: float):
        self.knob.setValue(val, emit=False)
        self.valueChanged.emit(self.param_key, val)


# =====================================================================
# 3. 控件：双声道电平表 (Input / Output Stereo Level Meter)
# =====================================================================
class StereoLevelMeter(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._db_l = -26.0
        self._db_r = -27.5
        self._peak_l = -24.0
        self._peak_r = -25.0
        self._clipped = False
        self.setFixedWidth(36)

    def setLevels(self, db_l: float, db_r: float):
        self._db_l = max(-60.0, min(0.0, db_l))
        self._db_r = max(-60.0, min(0.0, db_r))
        self._peak_l = self._db_l if self._db_l >= self._peak_l else max(self._db_l, self._peak_l - 0.35)
        self._peak_r = self._db_r if self._db_r >= self._peak_r else max(self._db_r, self._peak_r - 0.35)
        if db_l >= -0.1 or db_r >= -0.1:
            self._clipped = True
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._clipped = False
            self._peak_l = self._db_l
            self._peak_r = self._db_r
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        top_y = 12.0
        bar_h = max(40.0, self.height() - 24.0)
        bar_l = QRectF(19.0, top_y, 5.0, bar_h)
        bar_r = QRectF(26.0, top_y, 5.0, bar_h)

        p.setFont(QFont(MONO_FONT_FAM, 7))
        for t in (0, -10, -20, -30, -40, -50):
            norm = (t + 60.0) / 60.0
            ty = bar_l.bottom() - norm * bar_h
            p.setPen(QColor("#8A8A94"))
            p.drawText(QRectF(0, ty - 6, 16, 12), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, str(t))

        for bar, lvl, pk in ((bar_l, self._db_l, self._peak_l), (bar_r, self._db_r, self._peak_r)):
            p.setPen(QPen(QColor("#141416"), 1.0))
            p.setBrush(QColor("#0E0E11"))
            p.drawRoundedRect(bar, 1.5, 1.5)

            m_norm = max(0.0, min(1.0, (lvl + 60.0) / 60.0))
            if m_norm > 0.01:
                fh = bar.height() * m_norm
                fill = QRectF(bar.left() + 0.5, bar.bottom() - fh, bar.width() - 1.0, fh)
                grad = QLinearGradient(bar.bottomLeft(), bar.topLeft())
                grad.setColorAt(0.0, QColor("#28C882"))
                grad.setColorAt(0.75, QColor("#E8B838"))
                grad.setColorAt(1.0, QColor("#F05050"))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(grad)
                p.drawRoundedRect(fill, 1.0, 1.0)

            if pk > -59.5:
                py = bar.bottom() - ((pk + 60.0) / 60.0) * bar.height()
                p.setPen(QPen(QColor("#ECECF0"), 1.1))
                p.drawLine(QPointF(bar.left() + 0.5, py), QPointF(bar.right() - 0.5, py))

        clip_rect = QRectF(19.0, 4.0, 12.0, 4.5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#FF4848") if self._clipped else QColor("#18181C"))
        p.drawRoundedRect(clip_rect, 1.0, 1.0)


# =====================================================================
# 4. 控件：交互式 IR 脉冲响应波形 & ER/Tail 分割 & 扫频录制监控画布
# =====================================================================
class ConvolutionIRGraphWidget(QWidget):
    erSplitDragged = pyqtSignal(float)               # er_split_ms
    trimRangeDragged = pyqtSignal(float, float)      # trim_start_pct, trim_end_pct
    irFileDropped = pyqtSignal(str)                  # local_file_path
    sweepStartRequested = pyqtSignal(dict)           # sweep_config
    sweepDeconvolveRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(560, 265)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)

        self._params = copy.deepcopy(BUILTIN_IR_LIBRARY["Film ADR / Tight Booth"])
        self._peaks_l, self._peaks_r = generate_synthetic_ir_peaks(101, 380.0)
        self._view_mode = "WAVE"      # "WAVE" (双声道脉冲波形) | "SPEC" (IR 频谱特征)
        self._capture_mode = False    # False = 卷积响应视图, True = 扫频录制视图
        self._processor_controls = False

        # 扫频录制状态
        self._sweep_recording = False
        self._sweep_progress = 0.0
        self._sweep_captured_ready = False
        self._sweep_duration_sec = 5.0
        self._sweep_start_hz = 20.0
        self._sweep_end_hz = 22000.0
        self._sweep_level_db = -12.0

        # 交互拖拽状态
        self._drag_target = ""        # "er_split" | "trim_start" | "trim_end" | ""
        self._hover_target = ""
        self._mouse_pos: QPointF | None = None

        self._btn_wave_rect = QRectF()
        self._btn_spec_rect = QRectF()
        self._btn_sweep_rec_rect = QRectF()
        self._btn_sweep_deconv_rect = QRectF()

        self._ml = 40.0
        self._mr = 24.0
        self._mt = 10.0
        self._mb = 24.0

    def setState(self, params: dict):
        self._params = copy.deepcopy(params)
        self.update()

    def setWaveformPeaks(self, peaks_l: list[float], peaks_r: list[float]):
        self._peaks_l = peaks_l
        self._peaks_r = peaks_r
        self.update()

    def setCaptureMode(self, enabled: bool):
        self._capture_mode = enabled
        self.update()

    def setSweepProgress(self, progress: float, recording: bool, ready: bool):
        self._sweep_progress = max(0.0, min(1.0, progress))
        self._sweep_recording = recording
        self._sweep_captured_ready = ready
        self.update()

    def _plot_rect(self) -> QRectF:
        return QRectF(
            self._ml,
            self._mt,
            max(10.0, self.width() - self._ml - self._mr),
            max(10.0, self.height() - self._mt - self._mb),
        )

    def _effective_total_ms(self) -> float:
        native = max(50.0, float(self._params.get("native_ms", 500.0)))
        stretch = max(0.1, float(self._params.get("ir_length", 100.0)) / 100.0)
        return native * stretch

    def _ms_to_x(self, ms: float, plot: QRectF) -> float:
        total_ms = self._effective_total_ms()
        norm = max(0.0, min(1.0, ms / total_ms))
        return plot.left() + norm * plot.width()

    def _x_to_ms(self, x: float, plot: QRectF) -> float:
        total_ms = self._effective_total_ms()
        norm = max(0.0, min(1.0, (x - plot.left()) / plot.width()))
        return norm * total_ms

    # -----------------------------------------------------------------
    # 拖放外部 WAV/AIFF 脉冲文件
    # -----------------------------------------------------------------
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.toLocalFile().lower().endswith((".wav", ".aif", ".aiff", ".flac")):
                    event.acceptProposedAction()
                    return

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path.lower().endswith((".wav", ".aif", ".aiff", ".flac")):
                self.irFileDropped.emit(path)
                event.acceptProposedAction()
                return

    # -----------------------------------------------------------------
    # 鼠标交互：拖动 ER/Tail 分割线、Start/End 修剪旗标、扫频录制触发
    # -----------------------------------------------------------------
    def _hit_test(self, pos: QPointF) -> str:
        if self._capture_mode or self._processor_controls:
            return ""
        plot = self._plot_rect()
        if not plot.contains(pos):
            return ""

        split_x = self._ms_to_x(self._params["er_split_ms"], plot)
        trim_s_x = plot.left() + (self._params["trim_start"] / 100.0) * plot.width()
        trim_e_x = plot.left() + (self._params["trim_end"] / 100.0) * plot.width()

        if abs(pos.x() - split_x) <= 8.0:
            return "er_split"
        if abs(pos.x() - trim_s_x) <= 7.0:
            return "trim_start"
        if abs(pos.x() - trim_e_x) <= 7.0:
            return "trim_end"
        return ""

    def leaveEvent(self, event):
        self._mouse_pos = None
        self._hover_target = ""
        self.update()

    def mousePressEvent(self, event):
        pos = event.position()
        if event.button() != Qt.MouseButton.LeftButton:
            return

        if self._capture_mode:
            if self._btn_sweep_rec_rect.contains(pos):
                self.sweepStartRequested.emit({
                    "duration_sec": self._sweep_duration_sec,
                    "start_hz": self._sweep_start_hz,
                    "end_hz": self._sweep_end_hz,
                    "level_db": self._sweep_level_db,
                })
                return
            if self._btn_sweep_deconv_rect.contains(pos) and self._sweep_captured_ready:
                self.sweepDeconvolveRequested.emit()
                return
            return

        if self._btn_wave_rect.contains(pos):
            self._view_mode = "WAVE"
            self.update()
            return
        if self._btn_spec_rect.contains(pos):
            self._view_mode = "SPEC"
            self.update()
            return

        hit = self._hit_test(pos)
        if hit:
            self._drag_target = hit
            self.setCursor(Qt.CursorShape.SplitHCursor)
            self.update()

    def mouseMoveEvent(self, event):
        self._mouse_pos = event.position()
        plot = self._plot_rect()

        if self._drag_target:
            if self._drag_target == "er_split":
                total_ms = self._effective_total_ms()
                new_ms = max(5.0, min(total_ms * 0.85, round(self._x_to_ms(event.position().x(), plot), 1)))
                self.erSplitDragged.emit(new_ms)
            elif self._drag_target == "trim_start":
                pct = max(0.0, min(self._params["trim_end"] - 2.0, round(((event.position().x() - plot.left()) / plot.width()) * 100.0, 1)))
                self.trimRangeDragged.emit(pct, self._params["trim_end"])
            elif self._drag_target == "trim_end":
                pct = max(self._params["trim_start"] + 2.0, min(100.0, round(((event.position().x() - plot.left()) / plot.width()) * 100.0, 1)))
                self.trimRangeDragged.emit(self._params["trim_start"], pct)
            return

        hit = self._hit_test(event.position())
        self._hover_target = hit
        if hit:
            self.setCursor(Qt.CursorShape.SplitHCursor)
        elif plot.contains(event.position()):
            self.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._drag_target:
            self._drag_target = ""
            self.update()

    # -----------------------------------------------------------------
    # 绘制波形 / IR 频响谱 / 扫频录制监控界面
    # -----------------------------------------------------------------
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot = self._plot_rect()

        p.fillRect(plot, QColor("#1D1D20"))

        if self._processor_controls and not self._peaks_l:
            name = os.path.basename(self._params.get("file_path", ""))
            message = name or I18N.tr("audio.fx.no_ir", "No impulse response")
            p.setPen(QColor("#AEB0B9"))
            p.setFont(QFont(MONO_FONT_FAM, 10))
            message = p.fontMetrics().elidedText(message, Qt.TextElideMode.ElideMiddle, int(plot.width()) - 20)
            p.drawText(plot, Qt.AlignmentFlag.AlignCenter, message)
            return

        if self._capture_mode:
            self._paint_sweep_capture_view(p, plot)
            return

        en = bool(self._params.get("enabled", True))
        alpha_scale = 1.0 if en else 0.28
        total_ms = self._effective_total_ms()

        # 1. 网格线与双声道 L/R 中心基准线
        grid_pen = QPen(QColor("#26262A"), 1.0)
        p.setFont(QFont(MONO_FONT_FAM, 8))

        if self._view_mode == "WAVE":
            cy_l = plot.top() + plot.height() * 0.27
            cy_r = plot.top() + plot.height() * 0.73
            mid_y = plot.center().y()

            p.setPen(QPen(QColor("#141416"), 1.0))
            p.drawLine(QPointF(plot.left(), mid_y), QPointF(plot.right(), mid_y))
            p.setPen(QPen(QColor("#2C2C32"), 1.0, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(plot.left(), cy_l), QPointF(plot.right(), cy_l))
            p.drawLine(QPointF(plot.left(), cy_r), QPointF(plot.right(), cy_r))

            p.setPen(QColor("#8E8E98"))
            p.drawText(QRectF(4, cy_l - 8, self._ml - 10, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "L")
            p.drawText(QRectF(4, cy_r - 8, self._ml - 10, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "R")
        else:
            for i in range(5):
                y = plot.top() + (i / 4.0) * plot.height()
                p.setPen(grid_pen)
                p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
                db_lbl = f"{-i * 15}"
                p.setPen(QColor("#8E8E98"))
                p.drawText(QRectF(4, y - 8, self._ml - 10, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, db_lbl)

        # 垂直时间/频率刻度线
        num_v = 8
        for i in range(num_v + 1):
            norm = i / num_v
            vx = plot.left() + norm * plot.width()
            p.setPen(grid_pen)
            p.drawLine(QPointF(vx, plot.top()), QPointF(vx, plot.bottom()))
            if self._view_mode == "WAVE":
                t_val = norm * total_ms
                lbl = f"{t_val / 1000.0:.2f}s" if total_ms >= 1200.0 else f"{int(round(t_val))}ms"
            else:
                f_val = 20.0 * ((20000.0 / 20.0) ** norm)
                lbl = f"{f_val / 1000.0:.1f}k" if f_val >= 1000.0 else f"{int(round(f_val))}"
            p.setPen(QColor("#9A9AA2"))
            p.drawText(QRectF(vx - 24, plot.bottom() + 5, 48, 16), Qt.AlignmentFlag.AlignCenter, lbl)

        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(plot)

        p.save()
        p.setClipRect(plot)

        split_ms = float(self._params["er_split_ms"])
        split_x = self._ms_to_x(split_ms, plot)
        trim_s_norm = self._params["trim_start"] / 100.0
        trim_e_norm = self._params["trim_end"] / 100.0
        trim_s_x = plot.left() + trim_s_norm * plot.width()
        trim_e_x = plot.left() + trim_e_norm * plot.width()
        is_rev = bool(self._params.get("reverse", False))

        if self._view_mode == "WAVE":
            # 2. ER 区域背景微光 tint
            er_zone = QRectF(trim_s_x, plot.top(), max(0.0, min(trim_e_x, split_x) - trim_s_x), plot.height())
            if er_zone.width() > 1.0:
                p.fillRect(er_zone, QColor(240, 168, 72, int(20 * alpha_scale)))

            # 3. 绘制双声道 IR 脉冲波形 (ER 段琥珀金，Tail 段冰晶青)
            n_bins = len(self._peaks_l)
            cy_l = plot.top() + plot.height() * 0.27
            cy_r = plot.top() + plot.height() * 0.73
            max_amp_h = plot.height() * 0.21

            fade_in_norm = (self._params["fade_in"] / 100.0) * (trim_e_norm - trim_s_norm)
            fade_out_norm = (self._params["fade_out"] / 100.0) * (trim_e_norm - trim_s_norm)
            er_lin = 10.0 ** (self._params["er_gain"] / 20.0)
            tail_lin = 10.0 ** (self._params["tail_gain"] / 20.0)

            env_path_l = QPainterPath()
            env_path_r = QPainterPath()

            for i in range(n_bins):
                norm = i / max(1, n_bins - 1)
                src_idx = (n_bins - 1 - i) if is_rev else i
                px = plot.left() + norm * plot.width()

                in_active_trim = (trim_s_norm <= norm <= trim_e_norm)
                # 淡入淡出包络系数
                env_gain = 1.0
                if in_active_trim:
                    if fade_in_norm > 1e-4 and (norm - trim_s_norm) < fade_in_norm:
                        env_gain *= (norm - trim_s_norm) / fade_in_norm
                    if fade_out_norm > 1e-4 and (trim_e_norm - norm) < fade_out_norm:
                        env_gain *= (trim_e_norm - norm) / fade_out_norm
                else:
                    env_gain = 0.22

                is_er = (px <= split_x)
                stage_gain = er_lin if is_er else tail_lin
                amp_l = min(1.0, self._peaks_l[src_idx] * env_gain * stage_gain) * max_amp_h
                amp_r = min(1.0, self._peaks_r[src_idx] * env_gain * stage_gain) * max_amp_h

                if not in_active_trim:
                    col = QColor(90, 92, 102, int(90 * alpha_scale))
                elif is_er:
                    col = QColor(240, 168, 72, int(215 * alpha_scale))
                else:
                    col = QColor(40, 216, 240, int(200 * alpha_scale))

                p.setPen(QPen(col, 1.35))
                p.drawLine(QPointF(px, cy_l - amp_l), QPointF(px, cy_l + amp_l))
                p.drawLine(QPointF(px, cy_r - amp_r), QPointF(px, cy_r + amp_r))

                if i == 0:
                    env_path_l.moveTo(px, cy_l - amp_l)
                    env_path_r.moveTo(px, cy_r - amp_r)
                else:
                    env_path_l.lineTo(px, cy_l - amp_l)
                    env_path_r.lineTo(px, cy_r - amp_r)

            # 包络高亮描边
            p.setPen(QPen(QColor(210, 245, 255, int(130 * alpha_scale)), 1.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(env_path_l)
            p.drawPath(env_path_r)

            # 4. 修剪遮罩区 (Trim Start / End 外侧暗化)
            if trim_s_x > plot.left() + 1:
                p.fillRect(QRectF(plot.left(), plot.top(), trim_s_x - plot.left(), plot.height()), QColor(12, 12, 15, 145))
            if trim_e_x < plot.right() - 1:
                p.fillRect(QRectF(trim_e_x, plot.top(), plot.right() - trim_e_x, plot.height()), QColor(12, 12, 15, 145))

            # 修剪旗标手柄
            for t_key, tx in (("trim_start", trim_s_x), ("trim_end", trim_e_x)):
                hi = (self._hover_target == t_key or self._drag_target == t_key)
                t_col = QColor("#F2F8FC") if hi else QColor("#8A8A94")
                p.setPen(QPen(t_col, 1.3))
                p.drawLine(QPointF(tx, plot.top()), QPointF(tx, plot.bottom()))
                flag = QPolygonF([
                    QPointF(tx, plot.top()),
                    QPointF(tx + (8.0 if t_key == "trim_start" else -8.0), plot.top()),
                    QPointF(tx, plot.top() + 9.0),
                ])
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(t_col)
                p.drawPolygon(flag)

            # 5. 可拖拽 ER / Tail 分割线（线体避开顶部 HUD 信息带，手柄胶囊置于底端，防止与元数据文字叠加）
            sp_hi = (self._hover_target == "er_split" or self._drag_target == "er_split")
            sp_col = QColor("#FFD078") if sp_hi else QColor(SUITE_PALETTE["er"])
            p.setPen(QPen(sp_col, 1.8 if sp_hi else 1.4, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(split_x, plot.top() + 30), QPointF(split_x, plot.bottom() - 20))

            handle_rect = QRectF(split_x - 26, plot.bottom() - 20, 52, 16)
            p.setPen(QPen(sp_col, 1.1))
            p.setBrush(QColor("#2A2214") if sp_hi else QColor("#18181B"))
            p.drawRoundedRect(handle_rect, 3.0, 3.0)
            p.setPen(sp_col)
            p.setFont(QFont(MONO_FONT_FAM, 7, QFont.Weight.Bold))
            p.drawText(handle_rect, Qt.AlignmentFlag.AlignCenter, f"ER {int(round(split_ms))}ms")

        else:
            # SPEC 模式：绘制当前 IR 的空间频率吸收曲线 + 湿声高低架滤波曲线
            spec_path = QPainterPath()
            fill_poly = QPolygonF([QPointF(plot.left(), plot.bottom())])
            steps = 220
            lo_f = self._params["lo_freq"]
            hi_f = self._params["hi_freq"]
            lo_g = self._params["lo_gain"]
            hi_g = self._params["hi_gain"]

            for i in range(steps + 1):
                norm = i / steps
                f = 20.0 * ((20000.0 / 20.0) ** norm)
                x = plot.left() + norm * plot.width()
                # 空间自然空气吸收 + Low/High Shelf 响应模拟
                air_rolloff = -14.0 * max(0.0, math.log10(max(1000.0, f) / 1000.0))
                lo_shelf = lo_g / (1.0 + (f / lo_f) ** 2)
                hi_shelf = hi_g / (1.0 + (hi_f / f) ** 2)
                ripple = 2.2 * math.sin(norm * 38.0 + self._params.get("seed", 1)) + 1.4 * math.cos(norm * 85.0)
                db = max(-58.0, min(-2.0, -14.0 + air_rolloff + lo_shelf + hi_shelf + ripple))
                y = plot.top() + ((-db) / 60.0) * plot.height()
                if i == 0:
                    spec_path.moveTo(x, y)
                else:
                    spec_path.lineTo(x, y)
                fill_poly.append(QPointF(x, y))

            fill_poly.append(QPointF(plot.right(), plot.bottom()))
            s_grad = QLinearGradient(plot.topLeft(), plot.bottomLeft())
            s_grad.setColorAt(0.0, QColor(40, 216, 240, int(72 * alpha_scale)))
            s_grad.setColorAt(1.0, QColor(40, 216, 240, int(10 * alpha_scale)))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(s_grad)
            p.drawPolygon(fill_poly)

            p.setPen(QPen(QColor(40, 216, 240, int(220 * alpha_scale)), 1.7))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(spec_path)

        # 6. 右上角 WAVE / SPEC 模式切换胶囊
        self._btn_wave_rect = QRectF(plot.right() - 88, plot.top() + 8, 40, 18)
        self._btn_spec_rect = QRectF(plot.right() - 44, plot.top() + 8, 36, 18)
        for rect, lbl, active in (
            (self._btn_wave_rect, "WAVE", self._view_mode == "WAVE"),
            (self._btn_spec_rect, "SPEC", self._view_mode == "SPEC"),
        ):
            p.setPen(QPen(QColor("#24788C") if active else QColor("#141416"), 1.0))
            p.setBrush(QColor(24, 34, 42, 215) if active else QColor(21, 21, 24, 185))
            p.drawRoundedRect(rect, 3.0, 3.0)
            p.setPen(QColor("#28D8F0") if active else QColor("#787882"))
            p.setFont(QFont(MONO_FONT_FAM, 7, QFont.Weight.Bold))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, lbl)

        # 7. 左上角 IR 元数据 HUD 胶囊
        fname = os.path.basename(self._params.get("file_path", "Default_IR.wav"))
        sr_khz = self._params.get("sample_rate", 48000) / 1000.0
        rev_tag = "  •  REV" if is_rev else ""
        hud_str = f"{fname}  •  {total_ms / 1000.0:.2f}s  •  {sr_khz:.0f}kHz{rev_tag}"
        hud_w = min(plot.width() - 110, max(230, len(hud_str) * 6.4))
        hud_rect = QRectF(plot.left() + 8, plot.top() + 8, hud_w, 19)
        p.setPen(QPen(QColor("#141416"), 1.0))
        p.setBrush(QColor(21, 21, 24, 215))
        p.drawRoundedRect(hud_rect, 3.5, 3.5)
        p.setPen(QColor("#D0D0D8"))
        p.setFont(QFont(MONO_FONT_FAM, 8))
        p.drawText(hud_rect, Qt.AlignmentFlag.AlignCenter, hud_str)

        p.restore()

    def _paint_sweep_capture_view(self, p: QPainter, plot: QRectF):
        """扫频激励与房间脉冲录制视图 (Sine Sweep Capture & Deconvolution)"""
        p.setPen(QPen(QColor("#F26D6D"), 1.2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(plot)

        # 绘制对数扫频正弦参考网格
        grid_pen = QPen(QColor("#282428"), 1.0)
        for i in range(1, 6):
            y = plot.top() + (i / 6.0) * plot.height()
            p.setPen(grid_pen)
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))

        # 绘制啁啾扫频 (Chirp) 实时可视化曲线
        cy = plot.center().y() + 8.0
        amp_h = plot.height() * 0.28
        chirp_path = QPainterPath()
        steps = 360
        active_limit = self._sweep_progress if self._sweep_recording else (1.0 if self._sweep_captured_ready else 0.0)

        for i in range(steps + 1):
            norm = i / steps
            x = plot.left() + norm * plot.width()
            # 频率随时间指数递增的扫频波形
            phase = 140.0 * (norm ** 2.3)
            env = math.sin(min(1.0, norm * 18.0) * math.pi * 0.5) * math.sin(min(1.0, (1.0 - norm) * 18.0) * math.pi * 0.5)
            y = cy - math.sin(phase) * amp_h * env
            if i == 0:
                chirp_path.moveTo(x, y)
            else:
                chirp_path.lineTo(x, y)

        p.setPen(QPen(QColor(242, 109, 109, 60), 1.2))
        p.drawPath(chirp_path)

        if active_limit > 0.005:
            p.save()
            p.setClipRect(QRectF(plot.left(), plot.top(), plot.width() * active_limit, plot.height()))
            p.setPen(QPen(QColor("#F26D6D"), 1.8))
            p.drawPath(chirp_path)
            p.restore()

            scan_x = plot.left() + plot.width() * active_limit
            p.setPen(QPen(QColor("#F2F8FC"), 1.4))
            p.drawLine(QPointF(scan_x, plot.top() + 34), QPointF(scan_x, plot.bottom() - 34))

        # 顶部扫频状态栏
        status_txt = (
            f"CAPTURING SINE SWEEP... {int(self._sweep_progress * 100)}%"
            if self._sweep_recording
            else (
                "SWEEP CAPTURED  •  READY FOR DECONVOLUTION"
                if self._sweep_captured_ready
                else f"IR CAPTURE MODE  •  Log Sine Sweep {int(self._sweep_start_hz)}Hz → {int(self._sweep_end_hz / 1000)}kHz  •  {self._sweep_duration_sec:.0f}s  •  {self._sweep_level_db:.0f} dBFS"
            )
        )
        p.setPen(QColor("#F26D6D") if self._sweep_recording else QColor("#DCDCE2"))
        p.setFont(QFont(MONO_FONT_FAM, 8, QFont.Weight.Bold))
        p.drawText(QRectF(plot.left() + 12, plot.top() + 8, plot.width() - 24, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, status_txt)

        # 底部操作按钮：开始录制扫频 / 反卷积生成 IR
        btn_w, btn_h = 156, 26
        cx = plot.center().x()
        by = plot.bottom() - 36
        self._btn_sweep_rec_rect = QRectF(cx - btn_w - 8, by, btn_w, btn_h)
        self._btn_sweep_deconv_rect = QRectF(cx + 8, by, btn_w, btn_h)

        p.setPen(QPen(QColor("#F26D6D"), 1.2))
        p.setBrush(QColor("#4A1E22") if self._sweep_recording else QColor("#321A1D"))
        p.drawRoundedRect(self._btn_sweep_rec_rect, 4.0, 4.0)
        p.setPen(QColor("#FF9E9E") if self._sweep_recording else QColor("#F26D6D"))
        p.setFont(QFont(UI_FONT_CSS, 10, QFont.Weight.Bold))
        p.drawText(
            self._btn_sweep_rec_rect,
            Qt.AlignmentFlag.AlignCenter,
            "● Stop Capture" if self._sweep_recording else "● Start Sine Sweep",
        )

        deconv_en = self._sweep_captured_ready and not self._sweep_recording
        p.setPen(QPen(QColor("#28D8F0") if deconv_en else QColor("#3A3A42"), 1.2))
        p.setBrush(QColor("#182E38") if deconv_en else QColor("#222226"))
        p.drawRoundedRect(self._btn_sweep_deconv_rect, 4.0, 4.0)
        p.setPen(QColor("#28D8F0") if deconv_en else QColor("#686872"))
        p.drawText(self._btn_sweep_deconv_rect, Qt.AlignmentFlag.AlignCenter, "Deconvolve & Load IR")


# =====================================================================
# 5. 控件：右侧 IR 空间分类浏览器侧栏 (IR Space Browser)
# =====================================================================
class IRBrowserSidebar(QFrame):
    irPresetSelected = pyqtSignal(str)
    loadCustomIRClicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(215)
        self.setObjectName("IRBrowser")
        self.setStyleSheet("""
            QFrame#IRBrowser {
                background-color: #222226;
                border-right: 1px solid #141416;
            }
            QListWidget {
                background-color: #19191C;
                border: 1px solid #121214;
                border-radius: 3px;
                color: #CFCFD6;
                font-size: 10.5px;
                outline: none;
            }
            QListWidget::item {
                padding: 5px 7px;
                border-bottom: 1px solid #202024;
            }
            QListWidget::item:hover {
                background-color: #2A2A30;
                color: #FFFFFF;
            }
            QListWidget::item:selected {
                background-color: #182E38;
                color: #28D8F0;
                font-weight: 700;
            }

            /* 极简细滚动条：透明轨道 + 哑光深灰圆角滑块，隐藏两端箭头 */
            QScrollBar:vertical {
                background: transparent;
                width: 6px;
                margin: 3px 2px 3px 0px;
                border: none;
            }
            QScrollBar::handle:vertical {
                background-color: #3C3C44;
                border-radius: 3px;
                min-height: 28px;
            }
            QScrollBar::handle:vertical:hover,
            QScrollBar::handle:vertical:pressed {
                background-color: #50505A;
            }
            QScrollBar::sub-line:vertical,
            QScrollBar::add-line:vertical {
                width: 0px;
                height: 0px;
                background: transparent;
                border: none;
            }
            QScrollBar::sub-page:vertical,
            QScrollBar::add-page:vertical {
                background: transparent;
                border: none;
            }
        """)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        hdr = QHBoxLayout()
        lbl = QLabel("IR LIBRARY")
        lbl.setStyleSheet("color: #8E8E98; font-size: 9.5px; font-weight: 700; letter-spacing: 0.8px;")
        hdr.addWidget(lbl)
        hdr.addStretch()

        load_btn = QPushButton("Load WAV...")
        load_btn.setFixedHeight(20)
        load_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        load_btn.setStyleSheet("""
            QPushButton {
                background-color: #323238;
                color: #28D8F0;
                border: 1px solid #24788C;
                border-radius: 3px;
                padding: 0px 7px;
                font-size: 9.5px;
                font-weight: 700;
            }
            QPushButton:hover { background-color: #182E38; }
        """)
        load_btn.clicked.connect(self.loadCustomIRClicked.emit)
        hdr.addWidget(load_btn)
        lay.addLayout(hdr)

        self.list_widget = QListWidget()
        self.list_widget.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for name, meta in BUILTIN_IR_LIBRARY.items():
            item = QListWidgetItem(f"{name}\n  {meta['category']} · {meta['native_ms'] / 1000.0:.2f}s")
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.list_widget.addItem(item)
        self.list_widget.setCurrentRow(0)
        self.list_widget.itemClicked.connect(self._on_item_clicked)
        lay.addWidget(self.list_widget, 1)

        hint = QLabel("Tip: 拖入 .wav / .aiff 文件\n至右侧波形区可直接加载")
        hint.setStyleSheet("color: #6E6E78; font-size: 9px;")
        lay.addWidget(hint)

    def _on_item_clicked(self, item: QListWidgetItem):
        name = item.data(Qt.ItemDataRole.UserRole)
        if name:
            self.irPresetSelected.emit(name)

    def setCurrentIRName(self, name: str):
        for i in range(self.list_widget.count()):
            it = self.list_widget.item(i)
            if it.data(Qt.ItemDataRole.UserRole) == name:
                self.list_widget.blockSignals(True)
                self.list_widget.setCurrentRow(i)
                self.list_widget.blockSignals(False)
                return


# =====================================================================
# 6. 控件：底部参数模块卡片 (StudioModuleCard)
# =====================================================================
class StudioModuleCard(QFrame):
    def __init__(self, title: str, accent_color: str, parent=None):
        super().__init__(parent)
        self._accent = accent_color
        self._active = True

        self.setObjectName("ModuleCard")
        self._apply_style()

        self.card_layout = QVBoxLayout(self)
        self.card_layout.setContentsMargins(0, 0, 0, 0)
        self.card_layout.setSpacing(0)

        header = QWidget()
        header.setFixedHeight(28)
        self.header_lay = QHBoxLayout(header)
        self.header_lay.setContentsMargins(10, 4, 10, 2)
        self.header_lay.setSpacing(6)

        self.title_lbl = QLabel(title)
        self.title_lbl.setStyleSheet(
            f"color: {accent_color}; font-size: 10.5px; font-weight: 700; letter-spacing: 0.4px;"
        )
        self.header_lay.addWidget(self.title_lbl)
        self.header_lay.addStretch()

        self.card_layout.addWidget(header)

    def setBandActive(self, active: bool):
        self._active = active
        self._apply_style()
        self.title_lbl.setStyleSheet(
            f"color: {self._accent if active else '#6E6E76'}; font-size: 10.5px; font-weight: 700; letter-spacing: 0.4px;"
        )

    def _apply_style(self):
        self.setStyleSheet("""
            QFrame#ModuleCard {
                background-color: #27272B;
                border: none;
                border-right: 1px solid #141416;
                border-top: 1px solid #141416;
                border-radius: 0px;
            }
            QFrame#SubSection {
                background-color: transparent;
                border-top: 1px solid #141416;
            }
        """)

    def add_section(self) -> QHBoxLayout:
        sec = QFrame()
        sec.setObjectName("SubSection")
        lay = QHBoxLayout(sec)
        lay.setContentsMargins(8, 5, 8, 6)
        lay.setSpacing(4)
        self.card_layout.addWidget(sec, 1)
        return lay


# =====================================================================
# 7. 主面板：旗舰卷积混响器 (Studio Convolution Reverb)
# =====================================================================
class StudioConvolutionReverbWidget(QWidget):
    """
    卷积混响主界面，提供完整信号接口供后端 DSP 引擎订阅：
      - reverbBypassChanged(bool)
      - irFileLoadRequested(str)                   # 本地 WAV/AIFF 文件路径
      - parameterChanged(str, float)               # 单参数实时调整
      - reverseToggled(bool)                       # 反向脉冲响应开关
      - sweepCaptureRequested(dict)                # 启动扫频录制
      - sweepDeconvolveRequested()                 # 请求对已录制扫频信号执行反卷积
      - stateChanged(dict)                         # 完整参数快照
    """
    reverbBypassChanged = pyqtSignal(bool)
    irFileLoadRequested = pyqtSignal(str)
    parameterChanged = pyqtSignal(str, float)
    reverseToggled = pyqtSignal(bool)
    sweepCaptureRequested = pyqtSignal(dict)
    sweepDeconvolveRequested = pyqtSignal()
    stateChanged = pyqtSignal(dict)

    def __init__(self, track_name: str = "Dialogue Stem", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Convolution Reverb — {track_name}")
        self.resize(1120, 680)
        self.setMinimumSize(1000, 620)

        self._active_bank = "A"
        self._banks = {
            "A": copy.deepcopy(BUILTIN_IR_LIBRARY["Film ADR / Tight Booth"]),
            "B": copy.deepcopy(BUILTIN_IR_LIBRARY["Suburban Living Room"]),
        }
        self._presets = copy.deepcopy(BUILTIN_IR_LIBRARY)
        self._controls: dict[str, KnobControlUnit] = {}
        self._cards: list[StudioModuleCard] = []

        # 演示电平表与扫频录制模拟定时器
        self._anim_phase = 0.0
        self._sweep_sim_active = False
        self._sweep_sim_prog = 0.0
        self._demo_timer = QTimer(self)
        self._demo_timer.timeout.connect(self._tick_demo_meters)
        self._demo_timer.start(45)

        self._processor_mode = False
        self._setup_ui(track_name)
        self._refresh_all_views()

    def _setup_ui(self, track_name: str):
        self.setStyleSheet(f"""
            QWidget {{
                background-color: #27272B;
                color: #D8D8DE;
                font-family: {UI_FONT_CSS};
            }}
            QLabel, ProRotaryKnob, EditableValuePill, PowerToggleSwitch,
            ConvolutionIRGraphWidget, StereoLevelMeter {{
                background-color: transparent;
            }}
            QComboBox {{
                background-color: #18181B;
                color: #DCDCE2;
                border: 1px solid #121214;
                border-radius: 3px;
                padding: 0px 5px;
                font-size: 10px;
                font-weight: 600;
                min-height: 20px;
            }}
            QComboBox:hover {{ border-color: #484852; }}
            QComboBox::drop-down {{ border: none; width: 14px; }}
            QComboBox::down-arrow {{
                image: none;
                border-left: 3.5px solid transparent;
                border-right: 3.5px solid transparent;
                border-top: 4.5px solid #9A9AA4;
                margin-right: 5px;
            }}
            QComboBox QAbstractItemView {{
                background-color: #222226;
                color: #E4E4EA;
                border: 1px solid #141416;
                selection-background-color: #35353D;
                selection-color: #FFFFFF;
                outline: none;
            }}
            QComboBox QAbstractItemView QScrollBar:vertical {{
                background: transparent;
                width: 6px;
                margin: 3px 2px 3px 0px;
                border: none;
            }}
            QComboBox QAbstractItemView QScrollBar::handle:vertical {{
                background-color: #3C3C44;
                border-radius: 3px;
                min-height: 28px;
            }}
            QComboBox QAbstractItemView QScrollBar::handle:vertical:hover,
            QComboBox QAbstractItemView QScrollBar::handle:vertical:pressed {{
                background-color: #50505A;
            }}
            QComboBox QAbstractItemView QScrollBar::sub-line:vertical,
            QComboBox QAbstractItemView QScrollBar::add-line:vertical {{
                width: 0px;
                height: 0px;
                background: transparent;
                border: none;
            }}
            QComboBox QAbstractItemView QScrollBar::sub-page:vertical,
            QComboBox QAbstractItemView QScrollBar::add-page:vertical {{
                background: transparent;
                border: none;
            }}
            QPushButton#ToolBtn {{
                background-color: #323238;
                color: #CFCFD6;
                border: none;
                border-radius: 3px;
                font-size: 10.5px;
                font-weight: 600;
            }}
            QPushButton#ToolBtn:hover {{
                background-color: #3C3C44;
                color: #FFFFFF;
            }}
            QPushButton#ToolBtn:pressed {{
                background-color: #1D1D21;
                padding-top: 1px;
            }}
        """)

        main_lay = QVBoxLayout(self)
        main_lay.setContentsMargins(0, 0, 0, 0)
        main_lay.setSpacing(0)

        # -------------------------------------------------------------
        # 1. 顶部主控栏（电源 + 预设 + A/B 对比 + 扫频录制切换 + Reset）
        # -------------------------------------------------------------
        top_bar = QFrame()
        top_bar.setObjectName("TopBar")
        top_bar.setFixedHeight(38)
        top_bar.setStyleSheet("QFrame#TopBar { background-color: #27272B; border-bottom: 1px solid #141416; }")
        tb_lay = QHBoxLayout(top_bar)
        tb_lay.setContentsMargins(10, 6, 10, 6)
        tb_lay.setSpacing(6)

        self.power_switch = PowerToggleSwitch(True, SUITE_PALETTE["reverb"])
        self.power_switch.toggled.connect(self._on_power_toggled)
        tb_lay.addWidget(self.power_switch)

        title_lbl = QLabel(f"CONVOLUTION REVERB  ·  {track_name}")
        title_lbl.setStyleSheet("color: #D0D0D8; font-size: 11px; font-weight: 700; letter-spacing: 0.6px;")
        tb_lay.addWidget(title_lbl)

        tb_lay.addSpacing(8)

        add_preset_btn = QPushButton("+")
        add_preset_btn.setObjectName("ToolBtn")
        add_preset_btn.setFixedSize(22, 22)
        add_preset_btn.setToolTip("保存为新空间预设")
        add_preset_btn.clicked.connect(self._save_new_preset)
        tb_lay.addWidget(add_preset_btn)

        self.preset_combo = ChevronComboBox()
        self.preset_combo.setFixedWidth(190)
        self.preset_combo.addItems(list(self._presets.keys()))
        self.preset_combo.currentTextChanged.connect(self._load_preset)
        tb_lay.addWidget(self.preset_combo)

        prev_btn = QPushButton("‹")
        prev_btn.setObjectName("ToolBtn")
        prev_btn.setFixedSize(20, 22)
        prev_btn.clicked.connect(lambda: self._step_preset(-1))
        tb_lay.addWidget(prev_btn)

        next_btn = QPushButton("›")
        next_btn.setObjectName("ToolBtn")
        next_btn.setFixedSize(20, 22)
        next_btn.clicked.connect(lambda: self._step_preset(1))
        tb_lay.addWidget(next_btn)

        tb_lay.addSpacing(8)

        # A / B 对比槽
        ab_lay = QHBoxLayout()
        ab_lay.setSpacing(2)
        self.btn_a = QPushButton("A")
        self.btn_a.setFixedSize(24, 22)
        self.btn_a.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_a.clicked.connect(lambda: self._switch_bank("A"))

        self.btn_copy_ab = QPushButton("→")
        self.btn_copy_ab.setFixedSize(24, 22)
        self.btn_copy_ab.setToolTip("复制当前空间配置到另一槽位")
        self.btn_copy_ab.clicked.connect(self._copy_bank)

        self.btn_b = QPushButton("B")
        self.btn_b.setFixedSize(24, 22)
        self.btn_b.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_b.clicked.connect(lambda: self._switch_bank("B"))

        ab_lay.addWidget(self.btn_a)
        ab_lay.addWidget(self.btn_copy_ab)
        ab_lay.addWidget(self.btn_b)
        tb_lay.addLayout(ab_lay)

        tb_lay.addSpacing(10)

        # 扫频录制视图切换按钮 (IR Capture / Sine Sweep)
        self.sweep_mode_btn = QPushButton("● Sweep Capture")
        self.sweep_mode_btn.setCheckable(True)
        self.sweep_mode_btn.setFixedSize(112, 22)
        self.sweep_mode_btn.setToolTip("打开现场空间扫频录制工具 (Sine Sweep IR Recorder)")
        self.sweep_mode_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sweep_mode_btn.clicked.connect(self._on_toggle_sweep_mode)
        tb_lay.addWidget(self.sweep_mode_btn)
        self._ir_toolbar = tb_lay

        tb_lay.addStretch()

        reset_btn = QPushButton("Reset")
        reset_btn.setObjectName("ToolBtn")
        reset_btn.setFixedSize(46, 22)
        reset_btn.clicked.connect(self.reset_reverb)
        tb_lay.addWidget(reset_btn)

        main_lay.addWidget(top_bar)

        # -------------------------------------------------------------
        # 2. 中部核心区：IR 空间库侧栏 | 双声道 IR 波形与分割画布 | IN/OUT 电平表
        # -------------------------------------------------------------
        mid_row = QHBoxLayout()
        mid_row.setContentsMargins(0, 0, 0, 0)
        mid_row.setSpacing(0)

        # 左侧 IR 浏览器
        self.ir_browser = IRBrowserSidebar()
        self.ir_browser.irPresetSelected.connect(self._on_browser_preset_selected)
        self.ir_browser.loadCustomIRClicked.connect(self._open_ir_file_dialog)
        mid_row.addWidget(self.ir_browser)

        # 中央 IR 波形 / 扫频录制监控画布
        self.ir_graph = ConvolutionIRGraphWidget()
        self.ir_graph.erSplitDragged.connect(self._on_er_split_dragged)
        self.ir_graph.trimRangeDragged.connect(self._on_trim_range_dragged)
        self.ir_graph.irFileDropped.connect(self._on_ir_file_loaded)
        self.ir_graph.sweepStartRequested.connect(self._on_sweep_start_requested)
        self.ir_graph.sweepDeconvolveRequested.connect(self._on_sweep_deconvolve_requested)
        mid_row.addWidget(self.ir_graph, 1)

        # 右侧 IN / OUT 立体声电平表列
        meters_frame = QFrame()
        meters_frame.setObjectName("MetersCol")
        meters_frame.setStyleSheet("QFrame#MetersCol { background-color: #222226; border-left: 1px solid #141416; }")
        m_lay = QHBoxLayout(meters_frame)
        m_lay.setContentsMargins(4, 6, 4, 6)
        m_lay.setSpacing(2)

        in_box = QVBoxLayout()
        in_lbl = QLabel("IN")
        in_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        in_lbl.setStyleSheet("color: #787882; font-size: 9px; font-weight: 700;")
        self.in_meter = StereoLevelMeter()
        in_box.addWidget(in_lbl)
        in_box.addWidget(self.in_meter, 1)

        out_box = QVBoxLayout()
        out_lbl = QLabel("OUT")
        out_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        out_lbl.setStyleSheet("color: #787882; font-size: 9px; font-weight: 700;")
        self.out_meter = StereoLevelMeter()
        out_box.addWidget(out_lbl)
        out_box.addWidget(self.out_meter, 1)

        m_lay.addLayout(in_box)
        m_lay.addLayout(out_box)
        mid_row.addWidget(meters_frame)

        main_lay.addLayout(mid_row, 54)

        # -------------------------------------------------------------
        # 3. 底部 4 大功能模块卡片
        # -------------------------------------------------------------
        cards_row = QHBoxLayout()
        cards_row.setContentsMargins(0, 0, 0, 0)
        cards_row.setSpacing(0)

        # Card 1: IR Shaping & Envelope（冰晶青 #28D8F0）
        card_ir = StudioModuleCard("IR Shaping & Length", SUITE_PALETTE["reverb"])
        self.rev_btn = QPushButton("REV")
        self.rev_btn.setCheckable(True)
        self.rev_btn.setFixedSize(36, 18)
        self.rev_btn.setToolTip("反转脉冲响应时间轴 (Reverse Impulse Response)")
        self.rev_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.rev_btn.clicked.connect(self._on_reverse_toggled)
        card_ir.header_lay.addWidget(self.rev_btn)

        ir_top = card_ir.add_section()
        ir_top.addWidget(self._add_ctrl(
            "pre_delay", "Pre-Delay", -100.0, 250.0, 2.0, "ms",
            decimals=0, accent_color=SUITE_PALETTE["reverb"], mode="bipolar", origin_value=0.0,
            knob_diameter=46
        ))
        ir_top.addWidget(self._add_ctrl(
            "ir_length", "IR Length", 20.0, 200.0, 100.0, "%",
            decimals=0, accent_color=SUITE_PALETTE["reverb"], mode="bipolar", origin_value=100.0,
            suffix="%", knob_diameter=46
        ))

        ir_bot = card_ir.add_section()
        ir_bot.addWidget(self._add_ctrl(
            "fade_in", "Fade In", 0.0, 100.0, 0.0, "%",
            decimals=0, accent_color=SUITE_PALETTE["reverb"], suffix="%", knob_diameter=42
        ))
        ir_bot.addWidget(self._add_ctrl(
            "fade_out", "Fade Out", 0.0, 100.0, 15.0, "%",
            decimals=0, accent_color=SUITE_PALETTE["reverb"], suffix="%", knob_diameter=42
        ))

        # Card 2: ER / Tail Separation（琥珀金 #F0A848）
        card_er = StudioModuleCard("ER / Tail Separation", SUITE_PALETTE["er"])
        er_top = card_er.add_section()
        er_top.addWidget(self._add_ctrl(
            "er_split_ms", "ER Split", 5.0, 500.0, 28.0, "ms",
            decimals=0, accent_color=SUITE_PALETTE["er"], mode="log", knob_diameter=46
        ))
        er_top.addWidget(self._add_ctrl(
            "stereo_width", "Width", 0.0, 200.0, 65.0, "%",
            decimals=0, accent_color=SUITE_PALETTE["er"], mode="bipolar", origin_value=100.0,
            suffix="%", knob_diameter=46
        ))

        er_bot = card_er.add_section()
        er_bot.addWidget(self._add_ctrl(
            "er_gain", "ER Level", -24.0, 12.0, 1.5, "dB",
            decimals=1, accent_color=SUITE_PALETTE["er"], mode="bipolar", origin_value=0.0, knob_diameter=42
        ))
        er_bot.addWidget(self._add_ctrl(
            "tail_gain", "Tail Level", -24.0, 12.0, -4.5, "dB",
            decimals=1, accent_color=SUITE_PALETTE["er"], mode="bipolar", origin_value=0.0, knob_diameter=42
        ))

        # Card 3: IR Wet Tone EQ（翡翠绿 #4CD490）
        card_tone = StudioModuleCard("Wet Tone EQ", SUITE_PALETTE["tone"])
        tone_top = card_tone.add_section()
        tone_top.addWidget(self._add_ctrl(
            "lo_freq", "Lo Freq", 60.0, 600.0, 240.0, "Hz",
            decimals=0, accent_color=SUITE_PALETTE["tone"], mode="log", knob_diameter=46
        ))
        tone_top.addWidget(self._add_ctrl(
            "hi_freq", "Hi Freq", 1500.0, 18000.0, 8500.0, "Hz",
            decimals=0, accent_color=SUITE_PALETTE["tone"], mode="log", k_format=True, knob_diameter=46
        ))

        tone_bot = card_tone.add_section()
        tone_bot.addWidget(self._add_ctrl(
            "lo_gain", "Lo Gain", -18.0, 6.0, -3.5, "dB",
            decimals=1, accent_color=SUITE_PALETTE["tone"], mode="bipolar", origin_value=0.0, knob_diameter=42
        ))
        tone_bot.addWidget(self._add_ctrl(
            "hi_gain", "Hi Gain", -18.0, 6.0, -2.8, "dB",
            decimals=1, accent_color=SUITE_PALETTE["tone"], mode="bipolar", origin_value=0.0, knob_diameter=42
        ))

        # Card 4: Output & Mix（钴蓝 #6495FA）
        card_out = StudioModuleCard("Output & Mix", SUITE_PALETTE["output"])
        self.wet_lock_btn = QPushButton("LOCK")
        self.wet_lock_btn.setCheckable(True)
        self.wet_lock_btn.setFixedSize(40, 18)
        self.wet_lock_btn.setToolTip("锁定 Dry/Wet 比例，切换空间 IR 预设时不被覆盖")
        self.wet_lock_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.wet_lock_btn.clicked.connect(self._on_wet_lock_toggled)
        card_out.header_lay.addWidget(self.wet_lock_btn)

        out_top = card_out.add_section()
        self._mix_controls = out_top
        out_top.addStretch()
        out_top.addWidget(self._add_ctrl(
            "dry_wet", "Dry / Wet", 0.0, 100.0, 18.0, "%",
            left_hint="Dry", right_hint="Wet", decimals=1,
            accent_color=SUITE_PALETTE["output"], suffix="%", knob_diameter=48
        ))
        out_top.addStretch()

        out_bot = card_out.add_section()
        out_bot.addWidget(self._add_ctrl(
            "direct_db", "Direct", -60.0, 6.0, 0.0, "dB",
            decimals=1, accent_color=SUITE_PALETTE["output"], allow_neg_inf=True, zero_db_marker=True, knob_diameter=40
        ))
        out_bot.addWidget(self._add_ctrl(
            "er_db", "ER Bus", -60.0, 6.0, 0.0, "dB",
            decimals=1, accent_color=SUITE_PALETTE["output"], allow_neg_inf=True, zero_db_marker=True, knob_diameter=40
        ))
        out_bot.addWidget(self._add_ctrl(
            "tail_db", "Tail Bus", -60.0, 6.0, -3.0, "dB",
            decimals=1, accent_color=SUITE_PALETTE["output"], allow_neg_inf=True, zero_db_marker=True, knob_diameter=40
        ))

        self._cards = [card_ir, card_er, card_tone, card_out]
        cards_row.addWidget(card_ir, 24)
        cards_row.addWidget(card_er, 24)
        cards_row.addWidget(card_tone, 24)
        cards_row.addWidget(card_out, 28)

        main_lay.addLayout(cards_row, 46)

    def use_processor_controls(self) -> None:
        self._processor_mode = True
        self.ir_browser.hide()
        self.sweep_mode_btn.hide()
        self._presets.clear()
        with QSignalBlocker(self.preset_combo):
            self.preset_combo.clear()
        for state in self._banks.values():
            state["file_path"] = ""
            state["dry_level"] = 100.0
        load_button = QPushButton(I18N.tr("audio.fx.choose_ir", "Load WAV"), self)
        load_button.setObjectName("ToolBtn")
        load_button.setCursor(Qt.CursorShape.PointingHandCursor)
        load_button.clicked.connect(self._open_ir_file_dialog)
        self._ir_toolbar.insertWidget(self._ir_toolbar.count() - 2, load_button)
        self._controls["dry_wet"].title_lbl.setText(I18N.tr("audio.fx.wet", "Wet"))
        dry = self._add_ctrl(
            "dry_level", I18N.tr("audio.fx.dry", "Dry"), 0.0, 100.0, 100.0, "%",
            decimals=1, accent_color=SUITE_PALETTE["output"], suffix="%", knob_diameter=48,
        )
        self._mix_controls.insertWidget(1, dry)
        for card in self._cards[:3]:
            card.hide()
        for key in ("direct_db", "er_db", "tail_db"):
            self._controls[key].hide()
        self.wet_lock_btn.hide()
        self.ir_graph._processor_controls = True
        self.ir_graph.setMinimumSize(440, 168)
        self.ir_graph.setToolTip("Load or drop an impulse response WAV file")
        self.in_meter.setLevels(-60.0, -60.0)
        self.out_meter.setLevels(-60.0, -60.0)
        self._refresh_all_views()

    def _add_ctrl(self, key: str, title: str, min_v: float, max_v: float, def_v: float, unit: str, **kwargs) -> KnobControlUnit:
        ctrl = KnobControlUnit(key, title, min_v, max_v, def_v, unit, **kwargs)
        ctrl.valueChanged.connect(self._on_param_changed)
        self._controls[key] = ctrl
        return ctrl

    # -----------------------------------------------------------------
    # 状态刷新与双向联动
    # -----------------------------------------------------------------
    def _current_state(self) -> dict:
        return self._banks[self._active_bank]

    def _update_top_and_toggle_styles(self):
        st = self._current_state()
        active_ab = """
            QPushButton {
                background-color: #182E38;
                color: #28D8F0;
                border: 1px solid #24788C;
                border-radius: 3px;
                font-size: 11px;
                font-weight: 700;
            }
        """
        inactive_ab = """
            QPushButton {
                background-color: #323238;
                color: #CFCFD6;
                border: none;
                border-radius: 3px;
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton:hover { background-color: #3C3C44; color: #FFFFFF; }
        """
        self.btn_a.setStyleSheet(active_ab if self._active_bank == "A" else inactive_ab)
        self.btn_b.setStyleSheet(active_ab if self._active_bank == "B" else inactive_ab)
        self.btn_copy_ab.setStyleSheet(inactive_ab)
        self.btn_copy_ab.setText("→" if self._active_bank == "A" else "←")

        self.sweep_mode_btn.setStyleSheet("""
            QPushButton {
                background-color: #323238;
                color: #CFCFD6;
                border: none;
                border-radius: 3px;
                font-size: 10px;
                font-weight: 700;
            }
            QPushButton:hover { color: #FFFFFF; }
            QPushButton:checked {
                background-color: #3A1C20;
                color: #F26D6D;
                border: 1px solid #F26D6D;
            }
        """)

        self.rev_btn.setChecked(bool(st.get("reverse", False)))
        self.rev_btn.setStyleSheet("""
            QPushButton {
                background-color: #323238;
                color: #9A9AA2;
                border: none;
                border-radius: 3px;
                font-size: 9px;
                font-weight: 700;
            }
            QPushButton:hover { color: #FFFFFF; }
            QPushButton:checked {
                background-color: #182E38;
                color: #28D8F0;
                border: 1px solid #24788C;
            }
        """)

        self.wet_lock_btn.setChecked(bool(st.get("wet_lock", False)))
        self.wet_lock_btn.setStyleSheet("""
            QPushButton {
                background-color: #323238;
                color: #9A9AA2;
                border: none;
                border-radius: 3px;
                font-size: 9px;
                font-weight: 700;
            }
            QPushButton:hover { color: #FFFFFF; }
            QPushButton:checked {
                background-color: #1B2844;
                color: #6495FA;
                border: 1px solid #4A74C8;
            }
        """)

    def _refresh_all_views(self):
        st = self._current_state()
        en = bool(st.get("enabled", True))
        self._update_top_and_toggle_styles()
        if self._processor_mode:
            st.setdefault("dry_level", 100.0)

        self.power_switch.setChecked(en)

        # 动态更新 ER Split 旋钮上限以匹配当前有效 IR 时长
        eff_ms = max(50.0, st.get("native_ms", 500.0) * (st.get("ir_length", 100.0) / 100.0))
        if "er_split_ms" in self._controls:
            self._controls["er_split_ms"].knob.setRange(5.0, max(20.0, eff_ms * 0.85))

        for key, ctrl in self._controls.items():
            if key in st:
                ctrl.setValue(st[key], emit=False)
            ctrl.setBandActive(en)

        for card in self._cards:
            card.setBandActive(en)

        if self._processor_mode:
            self.ir_graph.setWaveformPeaks([], [])
        else:
            peaks_l, peaks_r = generate_synthetic_ir_peaks(st.get("seed", 101), st.get("native_ms", 400.0))
            self.ir_graph.setWaveformPeaks(peaks_l, peaks_r)
        self.ir_graph.setState(st)

    def _switch_bank(self, bank: str):
        if bank != self._active_bank:
            if self._processor_mode and not self._banks[bank]["file_path"]:
                self._banks[bank]["file_path"] = self._current_state()["file_path"]
            self._active_bank = bank
            self._refresh_all_views()
            self.stateChanged.emit(self.get_state())

    def _copy_bank(self):
        target = "B" if self._active_bank == "A" else "A"
        self._banks[target] = copy.deepcopy(self._banks[self._active_bank])

    def _on_power_toggled(self, enabled: bool):
        st = self._current_state()
        st["enabled"] = enabled
        self._refresh_all_views()
        self.reverbBypassChanged.emit(enabled)
        self.stateChanged.emit(self.get_state())

    def _on_reverse_toggled(self, checked: bool):
        st = self._current_state()
        st["reverse"] = checked
        self._update_top_and_toggle_styles()
        self.ir_graph.setState(st)
        self.reverseToggled.emit(checked)
        self.stateChanged.emit(self.get_state())

    def _on_wet_lock_toggled(self, checked: bool):
        st = self._current_state()
        st["wet_lock"] = checked
        self._update_top_and_toggle_styles()
        self.stateChanged.emit(self.get_state())

    def _on_param_changed(self, key: str, val: float):
        st = self._current_state()
        st[key] = val
        if key == "ir_length":
            eff_ms = max(50.0, st.get("native_ms", 500.0) * (val / 100.0))
            self._controls["er_split_ms"].knob.setRange(5.0, max(20.0, eff_ms * 0.85))
            if st["er_split_ms"] > eff_ms * 0.85:
                st["er_split_ms"] = round(eff_ms * 0.85, 1)
                self._controls["er_split_ms"].setValue(st["er_split_ms"], emit=False)
        self.ir_graph.setState(st)
        self.parameterChanged.emit(key, val)
        self.stateChanged.emit(self.get_state())

    def _on_er_split_dragged(self, new_ms: float):
        st = self._current_state()
        st["er_split_ms"] = new_ms
        self._controls["er_split_ms"].setValue(new_ms, emit=False)
        self.ir_graph.setState(st)
        self.parameterChanged.emit("er_split_ms", new_ms)
        self.stateChanged.emit(self.get_state())

    def _on_trim_range_dragged(self, start_pct: float, end_pct: float):
        st = self._current_state()
        st["trim_start"] = start_pct
        st["trim_end"] = end_pct
        self.ir_graph.setState(st)
        self.parameterChanged.emit("trim_start", start_pct)
        self.parameterChanged.emit("trim_end", end_pct)
        self.stateChanged.emit(self.get_state())

    # -----------------------------------------------------------------
    # IR 加载、预设切换与扫频录制交互
    # -----------------------------------------------------------------
    def _on_browser_preset_selected(self, name: str):
        self.preset_combo.setCurrentText(name)

    def _load_preset(self, name: str):
        if name not in self._presets:
            return
        cur = self._current_state()
        keep_wet = cur.get("wet_lock", False)
        saved_dw = cur.get("dry_wet", 25.0)

        new_st = copy.deepcopy(self._presets[name])
        if self._processor_mode and not new_st["file_path"]:
            new_st["file_path"] = cur["file_path"]
        if keep_wet:
            new_st["wet_lock"] = True
            new_st["dry_wet"] = saved_dw

        self._banks[self._active_bank] = new_st
        self.ir_browser.setCurrentIRName(name)
        self._refresh_all_views()
        self.irFileLoadRequested.emit(new_st["file_path"])
        self.stateChanged.emit(self.get_state())

    def _step_preset(self, step: int):
        cnt = self.preset_combo.count()
        if cnt > 0:
            self.preset_combo.setCurrentIndex((self.preset_combo.currentIndex() + step) % cnt)

    def _save_new_preset(self):
        name, ok = QInputDialog.getText(self, "保存空间 IR 预设", "输入预设名称:")
        if ok and name.strip():
            clean = name.strip()
            self._presets[clean] = copy.deepcopy(self._current_state())
            if self.preset_combo.findText(clean) < 0:
                self.preset_combo.addItem(clean)
            self.preset_combo.setCurrentText(clean)

    def _open_ir_file_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "加载脉冲响应文件 (Load Impulse Response)",
            "",
            "WAV (*.wav)" if self._processor_mode else "Audio Files (*.wav *.aif *.aiff *.flac);;All Files (*)",
        )
        if path:
            self._on_ir_file_loaded(path)

    def _on_ir_file_loaded(self, file_path: str):
        if self._processor_mode and (
            not file_path.lower().endswith(".wav") or not os.path.isfile(file_path)
        ):
            return
        st = self._current_state()
        st["file_path"] = file_path
        st["seed"] = abs(hash(file_path)) % 10000
        self._refresh_all_views()
        self.irFileLoadRequested.emit(file_path)
        self.stateChanged.emit(self.get_state())

    def _on_toggle_sweep_mode(self, checked: bool):
        self.ir_graph.setCaptureMode(checked)

    def _on_sweep_start_requested(self, cfg: dict):
        if self._sweep_sim_active:
            self._sweep_sim_active = False
            self.ir_graph.setSweepProgress(0.0, recording=False, ready=False)
            return
        self._sweep_sim_active = True
        self._sweep_sim_prog = 0.0
        self.ir_graph.setSweepProgress(0.0, recording=True, ready=False)
        self.sweepCaptureRequested.emit(cfg)

    def _on_sweep_deconvolve_requested(self):
        self.sweepDeconvolveRequested.emit()
        # 退出扫频录制视图并生成新的自定义空间 IR 状态
        self.sweep_mode_btn.setChecked(False)
        self.ir_graph.setCaptureMode(False)
        st = self._current_state()
        st["file_path"] = "Captured_Room_Sweep_48k.wav"
        st["native_ms"] = 920.0
        st["seed"] = random.randint(1000, 9999)
        self._refresh_all_views()
        self.stateChanged.emit(self.get_state())

    def _tick_demo_meters(self):
        self._anim_phase += 0.14
        if self._sweep_sim_active:
            self._sweep_sim_prog += 0.025
            if self._sweep_sim_prog >= 1.0:
                self._sweep_sim_active = False
                self.ir_graph.setSweepProgress(1.0, recording=False, ready=True)
            else:
                self.ir_graph.setSweepProgress(self._sweep_sim_prog, recording=True, ready=False)

        in_l = -26.0 + 7.5 * math.sin(self._anim_phase) + 2.5 * math.cos(self._anim_phase * 2.1)
        in_r = -26.5 + 7.2 * math.sin(self._anim_phase + 0.3) + 2.2 * math.cos(self._anim_phase * 1.9)
        wet_boost = (self._current_state()["dry_wet"] / 100.0) * 2.0
        self.in_meter.setLevels(in_l, in_r)
        self.out_meter.setLevels(in_l + wet_boost, in_r + wet_boost)

    # -----------------------------------------------------------------
    # 公开 API：供后端 DSP 引擎调用（加载真实波形峰值、实时电平、状态序列化）
    # -----------------------------------------------------------------
    def reset_reverb(self):
        if self._processor_mode:
            self._current_state().update(dry_wet=35.0, dry_level=100.0)
            with QSignalBlocker(self.preset_combo):
                self.preset_combo.setCurrentIndex(-1)
        else:
            self._banks[self._active_bank] = copy.deepcopy(BUILTIN_IR_LIBRARY["Film ADR / Tight Booth"])
            with QSignalBlocker(self.preset_combo):
                self.preset_combo.setCurrentText("Film ADR / Tight Booth")
        self._refresh_all_views()
        self.stateChanged.emit(self.get_state())

    def get_state(self) -> dict:
        return copy.deepcopy(self._current_state())

    def set_state(self, state: dict):
        merged = copy.deepcopy(self._current_state())
        merged.update(state)
        self._banks[self._active_bank] = merged
        self._refresh_all_views()

    def load_ir_waveform(
        self,
        file_path: str,
        sample_rate: int,
        duration_ms: float,
        peaks_l: list[float],
        peaks_r: list[float],
    ):
        """后端解码真实 WAV/AIFF 后调用此方法，更新画布波形与采样率/时长元数据"""
        st = self._current_state()
        st["file_path"] = file_path
        st["sample_rate"] = sample_rate
        st["native_ms"] = duration_ms
        self._refresh_all_views()
        self.ir_graph.setWaveformPeaks(peaks_l, peaks_r)

    def update_meters(self, in_l_db: float, in_r_db: float, out_l_db: float, out_r_db: float):
        """接入真实音频引擎 IN/OUT 立体声峰值电平 (调用后自动停止内置电平演示)"""
        if self._demo_timer.isActive() and not self._sweep_sim_active:
            self._demo_timer.stop()
        self.in_meter.setLevels(in_l_db, in_r_db)
        self.out_meter.setLevels(out_l_db, out_r_db)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = StudioConvolutionReverbWidget("Dialogue Stem")
    win.show()
    sys.exit(app.exec())
