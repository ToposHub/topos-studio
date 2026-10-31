import json
import os
import logging
import platform
import shutil
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

class ConfigManager:
    DEFAULT_CONFIG = {
        # Window & Layout
        "window_geometry": None,
        "window_state": None,
        "splitter_state": None,
        "last_path": "",
        "last_opened_file": "",
        "current_page": "Color",
        "page_layouts": {},
        # General
        "locale": "en_US",
        "auto_save_interval": 5,
        "max_recent_files": 10,
        "recent_files": [],
        "recent_projects": [],  # EditProject recent list
        "show_welcome_on_startup": True,
        "confirm_before_reset": True,
        "auto_load_last_project": True,
        "undo_history_limit": 50,
        "check_updates_on_startup": True,
        # Feature Flags (Modularization Phase 1)
        "features": {
            "library": True,
            "color": True,  # Color cannot be disabled
            "edit": True,
            "composite": True,
            "deliver": True,
        },
        # Work Mode: only effective for the studio product. Every studio user
        # works in exactly one of two workflows (no "full" mode):
        #   "image"  — image grading: hides the Edit page; Composite page keeps
        #              its node effects but disables keyframe/animation inputs.
        #   "video"  — video grading: timeline-driven workflows; all features on.
        #              (default — the richer workflow and typical entry point.)
        # Other product launchers (topos_color/edit/composite) ignore this and
        # keep their own PRODUCT_MODE preset.
        "work_mode": "video",
        # Developer mode: exposes maintainer-only tools (e.g. Window > Set as
        # Default for the factory layout). Read once at startup — toggling in
        # Preferences requires an application restart to take effect.
        "developer_mode": False,
        # Appearance
        "ui_scale": 100,
        "panel_opacity": 100,
        "show_tooltips": True,
        "animated_transitions": True,
        "transition_preview_mode": "icon",
        "transition_preview_size": "medium",
        "accent_color": "#ff8800",
        "show_panel_titles": True,
        "compact_mode": False,
        "zoom_speed": 100,
        # Color Management
        "default_color_space": "sRGB",
        "default_bit_depth": "8-bit",
        "gpu_color_processing": True,
        "show_color_space_warnings": True,
        "rendering_intent": "relative",
        "ocio_config_path": "",
        "auto_assign_icc": True,
        # Performance
        "gpu_acceleration": True,
        "video_playback_resolution": 1920,
        "cache_size_limit_gb": 10,
        "render_quality": "standard",
        "processing_threads": 0,
        # [BUG-B3-04] 多 pass 渲染融合模式（'conservative' | 'off' |
        # 'aggressive'）。Preferences 的 Performance 页读写该键；此前
        # DEFAULT_CONFIG 缺键且 Restore Defaults 不重置它。
        "pass_fusion_mode": "conservative",
        "background_rendering": False,
        "memory_limit_gb": 0,
        # P2-05: GPU 后端会话级四维选择（plan §1.2/§4.1）。graphics 被
        # Preferences 中的时间线渲染选择读取；auto 保持 OpenGL，Metal 为
        # 明确选择。其余维度继续由各自解码/计算/互操作入口消费。值域由
        # src/shared/gpu/backend_types.all_dimension_values() 定义。
        "backend": {
            "graphics": "auto",
            "compute": "auto",
            "decode": "auto",
            "interop": "auto",
        },
        # Export
        "default_export_format": "JPEG",
        "default_export_quality": 95,
        "default_export_path": "",
        "preserve_metadata": True,
        "export_color_profile": "sRGB",
        "embed_icc_profile": True,
        "open_folder_after_export": False,
        # Shortcuts
        "global_shortcuts": False,
        "show_shortcut_conflicts": True,
        # File & Storage
        "default_open_directory": "",
        "default_save_directory": "",
        # Video-mode project library directory. Empty = default
        # ~/Documents/ToposProjects (created on first use). Only the video-mode
        # Project Manager scans this directory; image mode is unaffected.
        "project_library_directory": "",
        # Back up the saved project every 15 minutes into the backup archive.
        # This is separate from auto-save recovery files.
        "auto_backup_interval": 15,
        "backup_directory": "",
        "max_backup_count": 5,
        "cache_directory": "",
        # Render cache defaults. Topos 422 Proxy/MOV is the preferred fallback
        # for CPU-baked effects and transitions: it is intra-frame, planar, and
        # can be decoded by the project's native Topos path. H.264 remains an
        # explicit user-selectable option in Preferences.
        "render_cache_auto_mode": "off",
        # [BUG-B3-02] smart 模式的自动烘焙阈值（clip 数）。Preferences 的
        # load/apply 均处理该键，Restore Defaults 依赖 DEFAULT_CONFIG 提供
        # 默认值 6，此前缺键导致恢复默认后阈值保持用户值。
        "render_cache_auto_threshold": 6,
        # B1: Edit 页场景检测候选点时间线 overlay 的开关键（EditPage 写入，
        # 菜单构建时读取以恢复勾选状态）。
        "edit_scene_cut_overlay_visible": False,
        # B8: Edit 页全局插入/覆盖拖放模式（"overwrite" | "insert"）。
        # Overwrite 为默认（更安全）；TimelinePanel 工具栏按钮 / Shift+B 切换。
        "edit_drop_mode": "overwrite",
        "render_cache_codec": "topos_proxy",
        "render_cache_directory": "",
        "render_cache_resolution_mode": "timeline",
        "render_cache_max_size_gb": 50,
        # Video proxy format for non-topos sources ('topos_proxy' |
        # 'prores_proxy' | 'h264'). Topos-first default aligned with the
        # render cache: TPIC proxies are intra-frame 4:2:2 10-bit and route
        # to the native decode path at playback. Topos sources always get
        # topos proxies regardless of this key (content sniff in
        # proxy_generator). TPIC MOV is video-only (ADR-C008); playback
        # audio always comes from the master, so no audio regression.
        "proxy_format": "topos_proxy",
        # Composite node disk-cache payload format ('auto' | 'toos' | 'npz').
        # 'auto' prefers native TOPOS .toos (TPIM GBR+alpha 12-bit) and
        # falls back to lossless npz for HDR payloads / missing native lib.
        # The TOPOS_NODE_CACHE_FORMAT env var (CI/diag) overrides this key.
        "composite.node_cache_format": "auto",
        "cleanup_temp_on_exit": True,
        "temp_file_max_age_days": 7,
        # Image Processing
        "default_denoise_strength": 50,
        "default_sharpen_amount": 0,
        "ai_model_path": "",
        "ai_inference_precision": "fp16",
        "ai_tile_size": 512,
        "ai_gpu_device": "auto",
        # Timeline Defaults
        "default_timeline_fps": "25",
        "default_timeline_width": 1920,
        "default_timeline_height": 1080,
        "default_timeline_sample_rate": 48000,
        "default_timeline_pixel_aspect_ratio": "1.0",
        "default_timeline_field_order": "progressive",
        "default_timeline_start_timecode": "00:00:00:00",
        "default_timeline_audio_track_type": "stereo",
        # P10.3 - Subtitle: default clip duration in frames (72 = 3s @ 24fps).
        # Stored as frames (not seconds) so it stays correct when timeline fps
        # changes. The preferences UI converts to/from seconds using the active
        # timeline's fps for display.
        "default_subtitle_clip_duration_frames": 72,
        # Composite (node graph)
        # Shake-to-disconnect sensitivity, 0-100. Higher = easier to disconnect a
        # node by shaking it; lower = harder to trigger and ignores slow movements.
        "composite.shake_disconnect_sensitivity": 50,
        # Composite colour management (stage 2). Working space is the internal
        # compositing space (scene-linear); display space controls the viewer's
        # default ODT (linear→sRGB encode on/off). color_engine selects the
        # transform backend: "ocio" (OpenColorIO, REQUIRED dependency — default)
        # or "builtin" (fast sRGB/BT.709↔linear numpy, fallback).
        "composite.working_space": "linear",
        "composite.display_space": "srgb",
        "composite.color_engine": "ocio",
        # Colour-management MVP: structured preview/output color spaces,
        # chosen separately for the timeline (video) and image (still) sources.
        # Each is (primaries, transfer, range); primaries+transfer combine
        # into a ColorSpaceDescriptor (see color_space_descriptor.py).
        # Defaults: timeline → Rec.709 video; image → sRGB. Users override
        # via the Color & Processing preference page.
        # Preview (viewer ODT) — what you see on screen.
        "color.preview.timeline_primaries": "bt709",
        "color.preview.timeline_transfer": "bt709",      # Rec.709 ~gamma 2.4
        "color.preview.image_primaries": "bt709",        # sRGB shares BT.709 primaries
        "color.preview.image_transfer": "iec61966_2_1",  # sRGB curve ~2.2
        # Output (export) — what gets baked into the file/stream.
        "color.output.timeline_primaries": "bt709",
        "color.output.timeline_transfer": "bt709",
        "color.output.timeline_range": "limited",        # video uses legal range
        "color.output.image_primaries": "bt709",
        "color.output.image_transfer": "iec61966_2_1",   # sRGB curve
        # Smart defaults: when a source's color metadata is missing, infer the
        # preview/output space from whether the source is a timeline (video) or
        # a still image. On = timeline→Rec.709, image→sRGB automatically.
        "color.smart_default": True,
        # Stage-2 HDR preview: when the working space holds HDR linear (nits),
        # tonemap to SDR for display on a normal monitor.
        #   tonemap: 0=off, 1=Reinhard, 2=ACES.
        #   target_peak: SDR display peak nits for the tonemap (default 100).
        "color.preview.hdr_tonemap": 1,        # Reinhard (good general-purpose)
        "color.preview.hdr_target_peak": 100,
        # Tangent Hub control-surface selection. "auto" keeps the managed
        # connection available for whichever supported panel set Hub reports.
        "tangent.control_surface": "auto",
        # Bump when a Tangent factory default changes.  The migration below
        # only replaces values that still equal the previous factory defaults,
        # so a user's explicit calibration is preserved.
        "tangent.settings_version": 5,
        # Tangent grading calibration. Rotation is the visual/physical angle
        # of the hue ring; the default preserves the DaVinci-style layout.
        "tangent.color_wheel_rotation_degrees": 120.0,
        "tangent.hue_direction_inverted": False,
        # 0 = raw trackerball response, 100 = strongest smoothing. Keep the
        # default raw so diagonal movement stays a straight Cartesian path;
        # smoothing is available for noisy hardware.
        "tangent.trackerball_smoothing": 0,
        # Internal values: 100 = native Tangent increment. Trackerball X/Y
        # defaults are 1000%; the preference UI displays this as 100%.
        # Brightness is the independent ring control and remains native.
        "tangent.lift_hue_sensitivity": 1000,
        "tangent.lift_saturation_sensitivity": 1000,
        "tangent.lift_brightness_sensitivity": 100,
        "tangent.gamma_hue_sensitivity": 1000,
        "tangent.gamma_saturation_sensitivity": 1000,
        "tangent.gamma_brightness_sensitivity": 100,
        "tangent.gain_hue_sensitivity": 1000,
        "tangent.gain_saturation_sensitivity": 1000,
        "tangent.gain_brightness_sensitivity": 100,
        # Stage-2 output HDR static metadata (mastering display + MaxCLL/FALL).
        # On = inject ST2086/CTA-861.3 into HDR10 exports; off = tag
        # primaries/transfer only.
        "color.output.hdr_metadata": True,
        # Logs & Diagnostics
        "log_level": "warning",
        "log_file_path": "",
        "enable_file_logging": False,
        "max_log_file_size_mb": 10,
        "show_version_in_title": True,
        "show_gpu_info": False,
        # Plugin Management
        "plugin_directory": "plugins",
        "auto_scan_plugins": True,
        "disabled_plugins": [],
        # Video Pipeline Feature Flags (P0.B) - P0-1 收敛为单一枚举
        # 新的单一配置项（取代旧的 video_pipeline_version + video_pipeline_v3_enabled）
        "video_pipeline_mode": "auto",  # "auto" | "v3" | "v2"
        "video_pipeline_allow_v2_fallback": True,  # v3 失败时是否允许自动回退
        # 旧配置项（已废弃，但保留用于迁移）
        "video_pipeline_version": "v3",  # 废弃：迁移后不再使用
        "video_pipeline_v3_enabled": True,  # 废弃：迁移后不再使用
        # 其他视频管线选项（保留）
        "video_preview_drop_frames": True,
        "video_preview_max_queue": 3,
        # P1-10: Edit 时间线播放期自适应质量（负载驱动降档/自动回退）；
        # 关闭时 engine 钉在 NORMAL 档，仅保留既有 auto2k/full 档位机。
        "video_preview_adaptive_quality": True,
        # P6 v2.1 后续（2026-09-22）：预览跳过输出去块（解码端提速开关）。
        # 默认 False = 保持滤波（块界更干净）；True 时预览解码不跑去块，
        # 解码快 1.19–2.02×（低档最大），代价是低档预览可见 8×8 块界台阶。
        # 只影响预览/播放解码；导出/交付路径始终保持滤波。
        "video_preview_no_deblock": False,
        "video_use_yuv_gpu_upload": True,
        # macOS VT Zero-Copy: "auto" (enable when available) | "off" (force CPU) | "force" (force enable)
        "video_macos_vt_zero_copy": "auto",
        # S4.A: Color 使用共享帧服务
        # V-P1-4: 协议测试全绿前默认禁用，通过 Preferences 手动开启
        # Color、Composite、Export 共享同一 VideoFrameProviderService
        "video_color_use_shared_frame_provider": False,
        # 视频降噪（时域+空域）功能开关（docs/video_denoise_implementation_plan.md §13）
        # 回退链：时域+空域 -> 仅空域 -> 原始帧；任何异常不阻塞播放
        "video_denoise_enabled": True,
        "video_temporal_denoise_enabled": True,
        "video_spatial_denoise_enabled": True,
        "video_denoise_backend": "glsl",       # 目前仅 glsl
        "video_denoise_debug_overlay": False,  # 预留：运动场/置信度可视化
        # External video monitoring (Blackmagic DeckLink). Disabled by default
        # because enabling it takes ownership of the output device.
        "decklink_monitor_enabled": False,
        "decklink_monitor_backend": "native_sdk",
        "decklink_monitor_device": "",
        "decklink_monitor_ffmpeg_path": "ffmpeg",
        "decklink_monitor_width": 1920,
        "decklink_monitor_height": 1080,
        "decklink_monitor_fps": 25.0,
        "decklink_monitor_mode": "1080i50",
        "decklink_monitor_format_code": "Hi50",
        "decklink_monitor_connection": "hdmi",
        "decklink_monitor_native_pixel_format": "yuv",
        "decklink_monitor_debug": False,
    }

    CONFIG_FILENAME = "config.json"
    APP_NAME = "Topos"
    _file_lock = threading.RLock()

    def __init__(self, config_path=None):
        if config_path is None:
            self.config_path = self.get_default_config_path()
        else:
            self.config_path = os.path.abspath(os.path.expanduser(str(config_path)))
        self._dirty_keys: set[str] = set()
        self.config = self.load_config()

    @classmethod
    def get_default_config_path(cls) -> str:
        """
        返回默认配置文件路径。

        ``TOPOS_CONFIG_PATH`` 优先用于隔离源码副本的偏好和布局；未提供
        覆盖路径的打包应用才使用平台用户级目录。默认值由 DEFAULT_CONFIG
        提供；删除配置文件后会自动用默认值重建。
        """
        override = os.environ.get("TOPOS_CONFIG_PATH")
        if override:
            return os.path.abspath(os.path.expanduser(override))

        return os.path.join(cls.get_platform_data_dir(), cls.CONFIG_FILENAME)

    @classmethod
    def get_platform_data_dir(cls) -> str:
        """[R2-5] 平台规范的用户数据目录（日志等伴随数据）。

        ``TOPOS_CONFIG_PATH`` 设置时（源码副本隔离），数据与配置同目录；
        否则用平台用户级目录（macOS Application Support / Windows
        AppData Roaming / Linux XDG config）。此前 log_manager 等各自
        写 ~/.topos，与 ConfigManager 策略不一致。"""
        override = os.environ.get("TOPOS_CONFIG_PATH")
        if override:
            return os.path.dirname(os.path.abspath(os.path.expanduser(override)))

        system_name = platform.system()
        if system_name == "Darwin":
            return str(Path.home() / "Library" / "Application Support" / cls.APP_NAME)
        if system_name == "Windows":
            appdata = os.environ.get("APPDATA")
            base = Path(appdata) / cls.APP_NAME if appdata else Path.home() / "AppData" / "Roaming" / cls.APP_NAME
            return str(base)
        xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
        return str(Path(xdg_config_home) / "topos" if xdg_config_home else Path.home() / ".config" / "topos")

    @classmethod
    def get_platform_cache_dir(cls) -> str:
        """[R2-5] 平台规范的缓存目录（可重建数据：模型/缩略图/AI 缓存）。

        macOS ~/Library/Caches/Topos、Windows %LOCALAPPDATA%/Topos/cache、
        Linux $XDG_CACHE_HOME/topos。TOPOS_CONFIG_PATH 设置时随配置目录
        （保持源码副本自包含）。此前散落的 ~/.topos_cache 与源码树内
        .assetdeck/ 都绕过了平台规范。"""
        override = os.environ.get("TOPOS_CONFIG_PATH")
        if override:
            return os.path.join(os.path.dirname(os.path.abspath(os.path.expanduser(override))), "cache")

        system_name = platform.system()
        if system_name == "Darwin":
            return str(Path.home() / "Library" / "Caches" / cls.APP_NAME)
        if system_name == "Windows":
            local = os.environ.get("LOCALAPPDATA")
            base = Path(local) / cls.APP_NAME / "cache" if local else Path.home() / "AppData" / "Local" / cls.APP_NAME / "cache"
            return str(base)
        xdg_cache_home = os.environ.get("XDG_CACHE_HOME")
        return str(Path(xdg_cache_home) / "topos" if xdg_cache_home else Path.home() / ".cache" / "topos")

    def load_config(self):
        with self._file_lock:
            if os.path.exists(self.config_path):
                try:
                    needs_save = False
                    with open(self.config_path, "r", encoding="utf-8") as f:
                        loaded_config = json.load(f)
                        config = {**self.DEFAULT_CONFIG, **loaded_config}
                        # P0-1: 迁移旧的视频管线配置到新的单一枚举
                        needs_save = self._migrate_video_pipeline_config(config, loaded_config)
                        # 迁移旧默认帧率 23.976 → 25
                        needs_save = self._migrate_timeline_fps_default(config, loaded_config) or needs_save
                        # Render Cache 已移除 ProRes 和独立容器选项；旧配置
                        # 必须迁移到默认 Topos，避免后台任务继续尝试已下线的编码器。
                        needs_save = self._migrate_render_cache_codec(config, loaded_config) or needs_save
                        # P2-05: 迁移/校验四维后端配置段
                        needs_save = self._migrate_backend_config(config, loaded_config) or needs_save
                        # Migrate Tangent defaults without overwriting a
                        # calibration the user has already changed.
                        needs_save = self._migrate_tangent_settings(config, loaded_config) or needs_save
                    # W01（Windows 实案）：必须先关掉读句柄再保存——CPython
                    # 的 open 不带 FILE_SHARE_DELETE，读句柄开着时对目标做
                    # os.replace 恒 WinError 5，迁移保存全程静默失败。
                    if needs_save:
                        self._save_config_dict(config)
                    return config
                except json.JSONDecodeError as e:
                    self._backup_corrupted_config(e)
                except Exception as e:
                    # AUD-32602: 任何加载失败（编码错误 UnicodeDecodeError、
                    # 顶层 JSON 非对象导致合并 TypeError、瞬时读失败）在用
                    # 默认配置覆盖原文件之前，都必须先备份原文件——否则
                    # 用户全部偏好（recent_files/page_layouts/Tangent 校准）
                    # 无 .corrupted 副本地静默丢失。
                    logger.error(f"loading config: {e}")
                    self._backup_corrupted_config(e)

            config = self.DEFAULT_CONFIG.copy()
            self._save_config_dict(config)
            return config

    def _migrate_tangent_settings(self, config: dict, loaded_config: dict) -> bool:
        """Apply the current trackerball defaults to older config files.

        Tangent integration defaults have been tuned more than once.  Update
        only values that still equal a previous factory value; any
        independently adjusted preference remains untouched.
        """

        try:
            version = int(loaded_config.get("tangent.settings_version", 0))
        except (TypeError, ValueError):
            version = 0
        if version >= 5:
            return False

        # Version 1 shipped 300% X/Y sensitivity and 45% smoothing; version 2
        # raised X/Y to 600% and made smoothing raw (0%); version 3 raised
        # X/Y to 2400%. Include all generations so an older config can migrate
        # directly to the current factory values in one pass.
        previous_defaults: dict[str, tuple[int, ...]] = {
            "tangent.trackerball_smoothing": (45, 0),
            "tangent.lift_hue_sensitivity": (300, 600, 2400),
            "tangent.lift_saturation_sensitivity": (300, 600, 2400),
            "tangent.lift_brightness_sensitivity": (100,),
            "tangent.gamma_hue_sensitivity": (300, 600, 2400),
            "tangent.gamma_saturation_sensitivity": (300, 600, 2400),
            "tangent.gamma_brightness_sensitivity": (100,),
            "tangent.gain_hue_sensitivity": (300, 600, 2400),
            "tangent.gain_saturation_sensitivity": (300, 600, 2400),
            "tangent.gain_brightness_sensitivity": (100,),
        }
        changed = False
        for key, previous_default in previous_defaults.items():
            if version < 4 and loaded_config.get(key) in previous_default:
                config[key] = self.DEFAULT_CONFIG[key]
                changed = True

        # Version 5 fixes the Resolve display basis. Migrate the old factory
        # angle once and persist it, so Preferences agrees with the bridge.
        # A later explicit 75° calibration remains valid on subsequent loads.
        rotation_key = "tangent.color_wheel_rotation_degrees"
        if loaded_config.get(rotation_key) == 75.0:
            config[rotation_key] = self.DEFAULT_CONFIG[rotation_key]
            changed = True

        if config.get("tangent.settings_version") != 5:
            config["tangent.settings_version"] = 5
            changed = True
        return changed

    def _migrate_video_pipeline_config(self, config: dict, loaded_config: dict) -> bool:
        """
        P0-1: 迁移旧的视频管线双开关配置到新的单一枚举。

        迁移规则：
        - version=v2 -> mode=v2
        - version=v3 and v3_enabled=true -> mode=v3
        - version=v3 and v3_enabled=false -> mode=auto，并记录一次迁移 warning
        - 缺失配置 -> mode=auto（DEFAULT_CONFIG 已设置）

        Args:
            config: 合并后的完整配置字典（会被修改）
            loaded_config: 从 config.json 加载的原始配置

        Returns:
            True 如果执行了迁移（需要保存），False 如果无需迁移
        """
        # 如果已有 video_pipeline_mode，说明已经迁移过或用户手动设置
        if 'video_pipeline_mode' in loaded_config:
            return False

        # 检查旧的配置项
        old_version = loaded_config.get('video_pipeline_version')
        old_v3_enabled = loaded_config.get('video_pipeline_v3_enabled')

        if old_version is None and old_v3_enabled is None:
            # 旧配置不存在，使用默认值 auto
            logger.debug("Video pipeline config: no legacy keys, using default mode=auto")
            return False

        # 执行迁移
        if old_version == 'v2':
            # v2 GStreamer 链路已移除：旧 v2 配置迁移到 auto（v3）
            config['video_pipeline_mode'] = 'auto'
            config['video_pipeline_allow_v2_fallback'] = True
            logger.info("Video pipeline config migrated: version=v2 -> mode=auto (v2 removed)")
        elif old_version == 'v3':
            if old_v3_enabled is True:
                config['video_pipeline_mode'] = 'v3'
                config['video_pipeline_allow_v2_fallback'] = False
                logger.info("Video pipeline config migrated: version=v3, v3_enabled=true -> mode=v3")
            elif old_v3_enabled is False:
                # 矛盾状态：选择了 v3 但禁用了 v3 -> 回退到 auto
                config['video_pipeline_mode'] = 'auto'
                config['video_pipeline_allow_v2_fallback'] = True
                logger.warning("Video pipeline config migrated: version=v3 but v3_enabled=false "
                              "(inconsistent state) -> mode=auto")
            else:
                # v3_enabled 未设置，使用默认 auto
                config['video_pipeline_mode'] = 'auto'
                config['video_pipeline_allow_v2_fallback'] = True
                logger.debug("Video pipeline config migrated: version=v3, v3_enabled unset -> mode=auto")
        else:
            # version 未设置或非标准值，使用默认 auto
            config['video_pipeline_mode'] = 'auto'
            config['video_pipeline_allow_v2_fallback'] = True
            logger.debug(f"Video pipeline config: version={old_version} unrecognized, using mode=auto")

        # V-P0-1 修复：返回 True 表示需要保存，由 load_config() 调用 _save_config_dict
        return True

    def _migrate_backend_config(self, config: dict, loaded_config: dict) -> bool:
        """
        P2-05: 四维后端配置段的补齐与值校验（.clinerules:12 版本/迁移策略）。

        迁移规则：
        - 段缺失 -> 用 DEFAULT_CONFIG["backend"] 补齐（全 auto，行为不变）并保存
        - 值非法（不在 backend_types 值域，含 force_ 前缀）-> 回退 auto 并记录
          warning（显式诊断，不静默保留坏值）
        """
        loaded_backend = loaded_config.get('backend')
        if not isinstance(loaded_backend, dict):
            if loaded_backend is None and 'backend' not in loaded_config:
                # 旧配置无此段：DEFAULT_CONFIG 已补齐，直接保存即可
                return True
            # 段存在但类型错误：重置为默认
            logger.warning(f"backend config has invalid type {type(loaded_backend).__name__}, "
                           "resetting to defaults")
            config['backend'] = {**self.DEFAULT_CONFIG['backend']}
            return True

        from src.shared.gpu.backend_types import (
            BackendSelection,
            ComputeBackendKind,
            DecodeBackendKind,
            GraphicsBackendKind,
            InteropBackendKind,
        )

        validators = {
            'graphics': GraphicsBackendKind,
            'compute': ComputeBackendKind,
            'decode': DecodeBackendKind,
            'interop': InteropBackendKind,
        }
        changed = False
        merged = {**self.DEFAULT_CONFIG['backend'], **loaded_backend}
        for key, enum_cls in validators.items():
            raw = merged.get(key)
            try:
                BackendSelection.from_dict({key: raw})
            except ValueError:
                logger.warning(f"backend.{key}={raw!r} is not a valid "
                               f"{key} backend, resetting to auto")
                merged[key] = 'auto'
                changed = True
        if merged != loaded_backend:
            config['backend'] = merged
            return True
        config['backend'] = merged
        return changed

    def get_backend_selection(self):
        """P2-05: 当前会话的四维后端选择（src.shared.gpu.BackendSelection）。"""
        from src.shared.gpu.backend_types import BackendSelection

        return BackendSelection.from_dict(self.get('backend') or {})

    def _migrate_timeline_fps_default(self, config: dict, loaded_config: dict) -> bool:
        """将旧的默认帧率 23.976 迁移为 25（仅当用户未手动修改过时）。"""
        if loaded_config.get('default_timeline_fps') == '23.976':
            config['default_timeline_fps'] = '25'
            logger.info("Timeline default FPS migrated: 23.976 -> 25")
            return True
        return False

    def _migrate_render_cache_codec(self, config: dict, loaded_config: dict) -> bool:
        """Migrate removed Render Cache codec/format preferences.

        ProRes is no longer an internal Render Cache encoding option. Existing
        preferences are moved to the Topos default so a restart cannot leave
        the background cache worker with an encoder that the UI no longer
        offers. The former independent container selector is also obsolete;
        the controller derives MOV/MP4 from the selected codec.
        """
        changed = False
        codec = loaded_config.get("render_cache_codec")
        if isinstance(codec, str) and codec.lower().startswith("prores"):
            config["render_cache_codec"] = self.DEFAULT_CONFIG["render_cache_codec"]
            logger.info("Render Cache codec migrated: %s -> topos_proxy", codec)
            changed = True

        if "render_cache_container" in loaded_config:
            config.pop("render_cache_container", None)
            changed = True

        return changed

    def _save_config_dict(self, config_dict: dict) -> bool:
        """
        V-P0-1: 内部方法，直接保存传入的配置字典。

        用于迁移和首次启动保存，避免在 self.config 还未赋值时调用 save_config()。
        使用临时文件 + os.replace() 原子替换，避免写到一半导致 JSON 损坏。

        AUD-32603: 返回是否保存成功。此前保存异常被吞掉后调用方
        （save_config）仍无条件清空 _dirty_keys，磁盘满/只读等持久性
        失败会静默丢掉整批待存偏好且永不重试。

        Args:
            config_dict: 要保存的配置字典

        Returns:
            True 保存成功；False 保存失败（已记录 error 日志）。
        """
        with self._file_lock:
            try:
                config_dir = os.path.dirname(self.config_path)
                if config_dir:
                    os.makedirs(config_dir, exist_ok=True)

                fd, temp_path = tempfile.mkstemp(
                    prefix=f".{self.CONFIG_FILENAME}.",
                    suffix=".tmp",
                    dir=config_dir or None,
                    text=True,
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(config_dict, f, indent=4, ensure_ascii=False)
                        f.write("\n")
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(temp_path, self.config_path)
                finally:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                return True
            except Exception as e:
                logger.error(f"saving config: {e}")
                return False

    def save_config(self):
        with self._file_lock:
            disk_config = self.DEFAULT_CONFIG.copy()
            if os.path.exists(self.config_path):
                try:
                    with open(self.config_path, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        disk_config.update(loaded)
                except Exception as e:
                    logger.warning(f"Could not merge existing config before save: {e}")

            if self._dirty_keys:
                for key in self._dirty_keys:
                    disk_config[key] = self.config.get(key)
                self.config = {**self.DEFAULT_CONFIG, **disk_config}
                # AUD-32603: 仅在持久化成功后清空 dirty 键；失败时保留，
                # 下次 save_config() 自动重试。
                if self._save_config_dict(self.config):
                    self._dirty_keys.clear()
            else:
                # AUD-32604: 无待存键时不写盘——ConfigManager 并非单例
                # （timeline_panel 等处用临时实例直接写盘），空 dirty 路径
                # 用本实例的内存快照全量覆盖磁盘，会回滚其他实例刚写入的
                # 键。仅从磁盘刷新内存态（多实例收敛），保持磁盘原样。
                self.config = {**self.DEFAULT_CONFIG, **disk_config}

    def _backup_corrupted_config(self, error: Exception) -> None:
        """备份损坏的用户配置文件，然后回到默认配置。"""
        backup_path = f"{self.config_path}.corrupted"
        try:
            shutil.copy2(self.config_path, backup_path)
            logger.error(f"Config file corrupted, backed up to {backup_path}: {error}")
        except Exception as backup_error:
            logger.error(f"Config file corrupted and backup failed: {error}; backup_error={backup_error}")

    def get(self, key: str, default=None):
        """Get a config value, matching ``dict.get`` semantics (AUD-32601).

        The two-argument form ``get(key, default)`` is relied on by callers
        throughout the app (page_controller HDR preview prefs,
        color_transform_provider engine selection, color_manager default
        color space, detachable_panel_host export mode, ...). The previous
        single-parameter signature raised ``TypeError`` on those calls,
        which broad ``except`` blocks upstream then swallowed silently.
        """
        return self.config.get(key, default)

    def set(self, key: str, value, auto_save: bool = True) -> None:
        """Set a config value and optionally save immediately.

        Args:
            key: Configuration key
            value: Configuration value
            auto_save: If True (default), save to disk immediately.
                      Set to False when making multiple changes, then call save_config() manually.

        AUD-32605: 内存态变更（self.config / _dirty_keys）全程持
        ``_file_lock``（类级 RLock，可重入）——此前 set() 在锁外修改
        共享状态，与持锁迭代 ``_dirty_keys`` 的 save_config() 构成数据
        竞争（RuntimeError: Set changed size during iteration / 半程
        状态被写入）。渲染线程会临时实例化并使用 ConfigManager，同一
        实例跨线程调用的窗口真实存在。
        """
        with self._file_lock:
            self.config[key] = value
            self._dirty_keys.add(key)
            if auto_save:
                self.save_config()
