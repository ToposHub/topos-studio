# 批次报告：batch-193 [P1] src/features/edit//

编号区间：AUD-19300–AUD-19399。审查日期：2026-09-28。
文件：facade.py / module.py / preferences.py（共 3 个文件，1,281 行，全部通读）。
审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解。

---

## src/features/edit/facade.py (行数: 191)

### 发现

- [AUD-19300] [Suggestion] except ImportError 包裹整个构造/调用过程，可掩盖运行期导入错误并静默返回 None
  - 位置: src/features/edit/facade.py:23-28, 112-121, 133-138, 146-151, 163-174, 186-191
  - 证据:
    ```python
    def create_render_cache_worker(manifest: Any, edit_project: Any,
                                   timeline_sequences: dict) -> Any:
        try:
            from src.features.edit.core.render_cache_worker import RenderCacheWorker
            return RenderCacheWorker(
                manifest=manifest,
                edit_project=edit_project,
                timeline_sequences=timeline_sequences,
            )
        except ImportError:
            logger.warning("RenderCacheWorker module unavailable")
            return None
    ```
    同一模式还出现在 open_teaching_animation_clip_editor（23-28，包裹整个 open_clip_editor 执行）、create_edit_gpu_decode_manager（133-138）、create_edit_gpu_preview_renderer（146-151）、create_edit_render_task（163-174）、create_timeline_clip_frame_provider（186-191）。
  - 影响: try 块不仅覆盖 import 语句，还覆盖构造器/函数体的完整执行。若构造器或 open_clip_editor 内部（其内部会继续调用 open_scene_editor 及动态插件加载）在运行期抛出 ImportError（如可选依赖缺失），会被误报为 "module unavailable" 并返回 None——领域错误被掩盖，调用方拿到 None 后在远处抛 NoneType 异常，难以定位真实原因。违反 .clinerules“不得宽泛 except 掩盖领域错误”的精神（虽仅捕 ImportError，但捕获范围过宽）。
  - 建议: 只把 import 语句放进 try/except ImportError，把构造/调用移到 try 块之外单独执行，让其异常正常传播。
  - 置信度: 高（代码模式确定；实际触发取决于内部是否存在运行期导入，故定 Suggestion）

- [AUD-19301] [Suggestion] parse_composite_graph_data 对非 dict 输入原样返回，外部数据未验证且与 docstring 不符
  - 位置: src/features/edit/facade.py:48-50
  - 证据:
    ```python
    if isinstance(data, dict):
        return CompositeGraphData.from_dict(data)
    return data
    ```
  - 影响: docstring 声明“If *data* is already a CompositeGraphData instance, return as-is”，但代码没有 isinstance 校验，任何 list/str/None/损坏类型都原样透传。反序列化数据属外部输入（.clinerules：外部数据须验证），调用方若假定返回 CompositeGraphData|None 会在后续使用处才失败，错误被推迟且难归因。
  - 建议: 非 dict 时校验 isinstance(data, CompositeGraphData) 后返回，否则记 warning 并返回 None（或抛 TypeError）。
  - 置信度: 高（行为确定；实际影响取决于调用方约定）

### 通过项
- 线程安全：无共享可变状态，全部为无状态函数，惰性导入模式正确。
- GPU 资源生命周期：仅转发构造调用，不持有/不释放 GPU 对象。
- GLSL·HDR 正确性：不涉及着色器与像素数据。
- 信号防递归：无 Qt 信号连接。
- 状态提交一致性：不涉及 undo/redo/持久化。
- 性能模式：导入全部惰性（函数内），符合“feature disabled 也能加载”的设计目标。

---

## src/features/edit/module.py (行数: 657)

### 发现

- [AUD-19302] [Major] 5 个菜单动作注册到不可达的 View/Timeline 路径，playback HUD 与性能报告导出入口完全不可达
  - 位置: src/features/edit/module.py:287-331（zoom_in 287-295、zoom_out 296-304、fit 305-313、playback.hud 315-322、export_perf_report 324-331）；自认注释 332-334
  - 证据:
    ```python
    action_registry.register(MenuActionDescriptor(
        action_id="edit.playback.hud",
        menu_path="View/Timeline",
        label=I18N.tr("action.playback_hud", "Playback &HUD"),
        callback_name="on_toggle_playback_hud",
        order=130,
        feature_id="edit",
    ))
    # B1: 场景检测候选点时间线 overlay（checkable 开关，状态经 ConfigManager
    # 持久化）。挂在 Edit/Timeline（与 Scene Cut Detection 相邻）；"View"
    # 顶级菜单未注册进 MenuBuilder，View/Timeline 路径目前不可达。
    ```
    佐证（仓库事实，已核实）：src/app/controllers/menu_toolbar_controller.py:1693-1701 仅向 MenuBuilder 注册了 "File"/"Edit"/"Color"/"Composite"/"Deliver" 五个顶级菜单；src/app/menu/menu_builder.py 的 _resolve_menu 对未注册的顶级菜单返回 None，build() 随即 skip 并只打 debug 日志。
  - 影响: edit.timeline.zoom_in/zoom_out/fit 的菜单项永不出现（仅快捷键 Ctrl+=/Ctrl+-/Shift+Z 仍可用，因 register_shortcuts 有注册）；edit.playback.hud 与 edit.playback.export_perf_report 无 shortcut_id，菜单是其唯一入口 → P1-23 播放 HUD 与 P0-10 性能报告导出两个功能对用户完全不可达。文件内注释已明确承认该路径不可达，但只为 scene_cut_overlay 改挂到 Edit/Timeline，其余 5 个动作未同步修复。
  - 建议: 将这 5 个动作改挂到已注册的顶级菜单（如 Edit/Timeline，与 scene_cut_overlay 同路），或在 menu_toolbar_controller 注册 View 顶级菜单。
  - 置信度: 高

- [AUD-19303] [Suggestion] __init__ 导入 EditPage 仅作可用性探测，导入即弃置且触发重型 UI 依赖链
  - 位置: src/features/edit/module.py:49-54
  - 证据:
    ```python
    try:
        from src.features.edit.ui.edit_page import EditPage
        self._available = True
    except ImportError as e:
        logger.warning(f"Edit module unavailable: {e}")
        self._available = False
    ```
  - 影响: EditPage 绑定后从未使用（lint F401）；更重要的是构造 EditModule 即触发整个 edit UI 依赖链的模块级加载，而 register_pages 的工厂（133-136）本可延迟到真正建页时。可用性探测也可用 importlib.util.find_spec 等轻量方式实现。
  - 建议: 可用性探测改用 find_spec 或 importlib.import_module(module)（不绑定类名）；或直接依赖 register_pages 工厂内的惰性导入 + 异常兜底。
  - 置信度: 高（代码事实确定；影响为启动开销与冗余导入，故定 Suggestion）

- [AUD-19304] [Suggestion] 多个注册方法参数与返回值缺类型注解
  - 位置: src/features/edit/module.py:193, 433, 553, 557（register_menu_actions(self, action_registry)、register_shortcuts(self, shortcut_manager)、get_edit_page(self) -> Optional[Any]、register_panels(self, panel_registry)）
  - 证据:
    ```python
    def register_menu_actions(self, action_registry) -> None:
    ...
    def register_shortcuts(self, shortcut_manager) -> None:
    ...
    def get_edit_page(self) -> Optional[Any]:
    ```
  - 影响: action_registry/shortcut_manager/panel_registry 均为隐式 Any，丢失接口约束与 IDE 检查；get_edit_page 返回 Optional[Any] 过弱（实际为 EditPage|None）。get_edit_page 在 _page 未建时返回 None，调用方需可选处理，类型上应精确到具体页面协议。
  - 建议: 用 app/shared 层的协议类型（ActionRegistry、ShortcutManager、PanelRegistry）注解参数；get_edit_page 返回类型收窄。
  - 置信度: 高

### 通过项
- GPU 资源生命周期：不创建/持有 GPU 对象。
- GLSL·HDR 正确性：不涉及。
- 信号防递归：本文件无 Qt 信号连接。
- 线程安全：EditModule 于主线程单次构造；adapter 注册为幂等 check-then-set，当前调用路径无并发构造证据（未见多线程实例化点）。
- 异常路径：Edit UI 与 adapter 注册的 ImportError 降级路径均记录日志且不崩溃，符合 feature-flag 设计。
- 状态提交一致性：register_save_handlers/register_actions 为显式占位，注释说明了 SaveRouter 兜底路径。
- 输入验证：注册描述符全部为字面量常量，无外部输入。
- 性能模式：注册均为一次性 O(1) 调用；菜单/快捷键/面板注册无循环热点。

---

## src/features/edit/preferences.py (行数: 433)

### 发现

- [AUD-19305] [Major] load/restore_defaults/get_scene_cut_settings_from_config 直接消费外部 config 值，无类型与范围验证，异常路径下静默半加载并可能把默认值写回覆盖用户设置
  - 位置: src/features/edit/preferences.py:303-313, 406-416, 419-433
  - 证据:
    ```python
    # P9-6: Load scene cut settings
    sensitivity = config.get('scene_cut_sensitivity', 0.55)
    # Scale 0.35-0.75 to 35-75
    sensitivity_slider_value = int(sensitivity * 100)
    self._widgets['scene_cut_sensitivity'].setValue(sensitivity_slider_value)

    self._widgets['scene_cut_min_scene_frames'].setValue(
        config.get('scene_cut_min_scene_frames', 8)
    )
    ...
    return {
        'sensitivity': config.get('scene_cut_sensitivity', 0.55),
        'use_proxy': config.get('scene_cut_use_proxy', True),
        'min_scene_frames': config.get('scene_cut_min_scene_frames', 8),
    }
    ```
  - 影响: config 为持久化外部数据（.clinerules：外部数据须验证）。若 scene_cut_sensitivity / min_scene_frames / video_preview_max_queue 等键类型异常（手改配置、旧版迁移遗留字符串），int(sensitivity * 100) 抛 TypeError；越界数值（如 0.9/0.1）仅靠 QSlider 静默 clamp 到 35-75。对比同文件 295-300 行 decklink 字段已做 str(... or '') 强转，数值键未做同等防护。已核实调用方 src/app/ui/preferences_dialog.py:1899-1902/2053-2056/2250-2253 用 `except Exception: pass` 吞掉模块页异常：load 中途抛错 → 页面后半段控件保持创建时默认值，用户下次点 Apply 时 apply() 会把这些默认值连同正常键一起写回配置 → 用户既有设置被静默重置（偏好数据丢失，无任何提示）。
  - 建议: 在 load/restore/get_scene_cut_settings_from_config 入口对数值键做类型与范围归一（float()/int() + clamp + 记 warning），与 decklink 字段的 str() 防护保持一致。
  - 置信度: 中（代码缺陷确定存在；实际触发需配置文件类型损坏/越界，非常规路径）

- [AUD-19306] [Suggestion] video_preview_max_queue 三处默认值不一致（控件 4，load/restore 3）
  - 位置: src/features/edit/preferences.py:123, 280-282, 384-386
  - 证据:
    ```python
    queue_spin = PrefDragValue(min_v=2, max_v=32, default=4)   # L123
    ...
    self._widgets['video_preview_max_queue'].setValue(
        config.get('video_preview_max_queue', 3)                # L281
    )
    ...
    self._widgets['video_preview_max_queue'].setValue(
        defaults.get('video_preview_max_queue', 3)              # L385
    )
    ```
  - 影响: 控件构造默认 4 与配置缺省 3 不一致：任何 load/restore_defaults 调用后显示 3，控件 default=4 形同虚设；“恢复默认值”与新建控件的初始显示不一致，易让用户误解默认行为。
  - 建议: 统一为单一常量（3 或 4）并引用同一来源。
  - 置信度: 高

- [AUD-19307] [Suggestion] 灵敏度浮点换算用 int() 截断而非 round()，保存值经一次往返漂移
  - 位置: src/features/edit/preferences.py:305, 408
  - 证据:
    ```python
    sensitivity = config.get('scene_cut_sensitivity', 0.55)
    # Scale 0.35-0.75 to 35-75
    sensitivity_slider_value = int(sensitivity * 100)
    self._widgets['scene_cut_sensitivity'].setValue(sensitivity_slider_value)
    ```
  - 影响: 二进制浮点误差使部分值 *100 后略小于整数（如 0.57*100 = 56.999…），int() 截断为 56；apply() 再以 56/100.0 = 0.56 写回 → 用户设的 0.57 经一次 打开偏好设置→Apply 往返后永久漂移为 0.56。属持久化往返精度缺陷（.clinerules 浮点规范精神）。
  - 建议: 改用 `int(round(sensitivity * 100))`（两处：load 与 restore_defaults）。
  - 置信度: 高（浮点算术可复现）

- [AUD-19308] [Suggestion] apply() 每次硬编码覆写 7 个 decklink_monitor_* 配置键，会冲掉任何外部修改
  - 位置: src/features/edit/preferences.py:347-353
  - 证据:
    ```python
    config_manager.set('decklink_monitor_width', 1920, auto_save=False)
    config_manager.set('decklink_monitor_height', 1080, auto_save=False)
    config_manager.set('decklink_monitor_fps', 25.0, auto_save=False)
    config_manager.set('decklink_monitor_mode', '1080i50', auto_save=False)
    config_manager.set('decklink_monitor_format_code', 'Hi50', auto_save=False)
    config_manager.set('decklink_monitor_connection', 'hdmi', auto_save=False)
    config_manager.set('decklink_monitor_native_pixel_format', 'yuv', auto_save=False)
    ```
  - 影响: 本页 UI 并未暴露这 7 个键的控件，但每次 Apply 都无条件写死值。描述文本称“第一阶段固定 1080i50”，属有意为之；但若用户/其他页面/未来版本修改过这些键（如改 1080p25 或 sdi 连接），在本页任意点 Apply 即被静默重置。
  - 建议: 将固定值收敛为模块级常量并在注释中声明“首阶段锁定”；或在仅当键缺失时写入（如 config_manager.get(...) is None 才 set）。
  - 置信度: 高（行为确定；实际冲突取决于是否存在其他写入口）

- [AUD-19309] [Suggestion] create_widget 的 parent 参数缺类型注解
  - 位置: src/features/edit/preferences.py:49
  - 证据:
    ```python
    def create_widget(self, parent) -> QWidget:
    ```
  - 影响: parent 应为 QWidget | None；缺注解降低可读性与静态检查覆盖（同批 module.py 的 AUD-19304 同类问题，此处单独列出便于逐条修复）。
  - 建议: 改为 `def create_widget(self, parent: QWidget | None) -> QWidget:`。
  - 置信度: 高

### 通过项
- 线程安全：仅操作 UI 线程控件，无跨线程访问，无 OpenGL 调用（铁律“UI 线程不得操作 OpenGL”无关违反点）。
- GPU 资源生命周期：不涉及 GPU 对象。
- GLSL·HDR 正确性：不涉及着色器与像素数据。
- 信号防递归：类内未连接任何 Qt 信号，load/apply 的程序化 setChecked/setValue/setText 不会触发本类回调，无递归风险（也无须状态锁）。
- 状态提交一致性：apply() 统一 auto_save=False 交由框架一次性保存（preferences_dialog _on_apply 末尾 cfg.save_config()），无部分持久化窗口；load/restore_defaults 守卫（'video_pipeline_mode' not in self._widgets 时早退）防未建控件访问。
- 性能模式：控件一次性创建；load/apply 为 O(键数) 直接赋值，无热点。
- i18n：全部用户可见字符串经 I18N.tr 且带 fallback。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 2 | AUD-19302, AUD-19305 |
| Suggestion | 8 | AUD-19300, AUD-19301, AUD-19303, AUD-19304, AUD-19306, AUD-19307, AUD-19308, AUD-19309 |

合计 10 条发现（facade.py 2 / module.py 3 / preferences.py 5）。
