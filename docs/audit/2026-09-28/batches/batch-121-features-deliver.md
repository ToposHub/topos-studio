# 批次报告：batch-121 [P1] src/features/deliver//

编号区间：AUD-12100–AUD-12199。审查日期：2026-09-28。
四个文件全部完整通读；跨模块引用已核实（subtitle_renderer.frames_to_srt_timecode、timeline_v2.SubtitleClip/EditTrack、shared/media/metadata.Rational、shared/config/config_manager.DEFAULT_CONFIG、app/ui/preferences_dialog 调用方、app/main_window facade 调用方）。

---

## src/features/deliver/facade.py (行数: 38)

### 发现

- [AUD-12100] [Major] facade 只捕获 ImportError，遗漏 module.py 明确防御的 AttributeError 导入失败路径，异常可逃逸致启动崩溃
  - 位置: src/features/deliver/facade.py:31
  - 证据:
    ```python
    try:
        from src.features.deliver.ui.deliver_workspace import DeliverWorkspace
        return DeliverWorkspace
    except ImportError:
        pass
    ```
    对照 src/features/deliver/module.py:53：`except (ImportError, AttributeError) as e:`（同一次导入的兄弟防御）；调用方 src/app/main_window.py:1396 仅 `except ImportError as e:`。
  - 影响: module.py 捕获 AttributeError 证明该导入在真实场景下可能抛 AttributeError（如循环导入/部分损坏安装）。`_deliver_available` 由 feature flag 决定（main_window.py:497），与导入是否成功无关；flag 开启且 workspace 导入抛 AttributeError 时，facade 将异常原样上抛，main_window 的 `except ImportError` 接不住，页面创建路径未捕获异常、启动流程崩溃。
  - 建议: 将 facade 的捕获范围改为 `except (ImportError, AttributeError)`，与 module.py:53 对齐（必要时同步 main_window.py:1396）。
  - 置信度: 中（代码层不一致确凿；触发需 workspace 导入期抛 AttributeError 的场景）

- [AUD-12101] [Suggestion] DeliverWorkspace 导入失败静默降级，无任何日志
  - 位置: src/features/deliver/facade.py:31-32
  - 证据:
    ```python
    except ImportError:
        pass
    try:
        from src.features.deliver.ui.deliver_page import DeliverPage
        return DeliverPage
    except ImportError:
        logger.warning("Deliver deliver_page unavailable")
    ```
  - 影响: 新 workspace 因真实缺陷（坏 import、缺失依赖）导入失败时完全无日志，静默降级到旧版 DeliverPage，线上问题无法定位。
  - 建议: 第一个 except 分支补充 `logger.warning("... falling back: %s", e)`。
  - 置信度: 高

- [AUD-12102] [Suggestion] get_deliver_page_class 缺参数/返回类型注解
  - 位置: src/features/deliver/facade.py:20
  - 证据: `def get_deliver_page_class():`
  - 影响: 返回值可能为 None（禁用态）这一关键契约仅靠 docstring 表达，静态检查与调用方都无法发现 None 解引用风险（main_window.py:1394 已手工判 None）。
  - 建议: `def get_deliver_page_class() -> Optional[type]:`。
  - 置信度: 高

### 通过项
- 纯 re-export shim，无状态、无逻辑分支（懒导入符合 docstring 声明），无 GPU/线程/信号维度风险
- 不涉及 UI 线程 OpenGL 操作、HDR/GLSL、undo/revision、性能路径

---

## src/features/deliver/module.py (行数: 332)

### 发现

- [AUD-12103] [Suggestion] 页面工厂捕获全部 Exception 降级到旧页，可能掩盖 DeliverWorkspace 构造期真实缺陷
  - 位置: src/features/deliver/module.py:89-92
  - 证据:
    ```python
    except Exception as e:  # noqa: BLE001 — keep the page booting
        logger.exception("DeliverWorkspace unavailable, falling back to DeliverPage: %s", e)
        from src.features.deliver.ui.deliver_page import DeliverPage
        self._page = DeliverPage()
    ```
  - 影响: 构造期的任意 bug（TypeError、NameError 等）都会被降级为旧页且仅记日志，用户无感知失去新功能；且内层 `DeliverPage()` 若构造失败，异常会逃逸出工厂函数。
  - 建议: 有日志与 noqa 说明，风险受控；建议收窄到 (ImportError, AttributeError, RuntimeError) 或在 UI 上提示降级，并为内层 DeliverPage() 构造加同样防护。
  - 置信度: 中

- [AUD-12104] [Suggestion] register_menu_actions / register_shortcuts / register_panels 参数缺类型注解
  - 位置: src/features/deliver/module.py:142, 233, 295
  - 证据:
    ```python
    def register_menu_actions(self, action_registry) -> None:
    def register_shortcuts(self, shortcut_manager) -> None:
    def register_panels(self, panel_registry) -> None:
    ```
  - 影响: 不符合 .clinerules 强类型要求；注册器接口无静态校验。
  - 建议: 标注对应 Registry 类型或定义 Protocol。
  - 置信度: 高

### 通过项
- __init__ 导入探测捕获 (ImportError, AttributeError) 并记日志，失败路径自洽（module.py:47-55）
- register_i18n：locales 目录存在（10 种语言 JSON），I18N.load_module_translations 对目录缺失有 os.path.isdir 防护（i18n.py:259-260）
- register_pages 标题对 context.i18n 为 None 有守卫（module.py:97）
- on_page_enter/on_page_leave 为空实现守卫，无状态残留；register_save_handlers 空实现有注释说明由 SaveRouter 承担
- 无 OpenGL/线程/GPU 资源操作；菜单/快捷键/面板注册为启动期一次性，无性能问题
- features 层导入 src.app.page_contract/app_context 为全部 feature module 的既定模式（color/edit/composite/teaching_animation 同样如此），非本文件缺陷

---

## src/features/deliver/preferences.py (行数: 278)

### 发现

- [AUD-12105] [Suggestion] restore_defaults 对 defaults 直接下标访问，与同函数 .get 风格不一致；KeyError 会被调用方静默吞掉
  - 位置: src/features/deliver/preferences.py:242-254
  - 证据:
    ```python
    idx = fmt_combo.findText(defaults['default_export_format'])
    ...
    self._widgets['default_export_quality'].setValue(
        defaults['default_export_quality']
    )
    self._widgets['default_export_path'].setText(
        defaults['default_export_path']
    )
    self._widgets['preserve_metadata'].setChecked(
        defaults['preserve_metadata']
    )
    ```
    同函数后半段及 load() 均用 `defaults.get(...)`；调用方 src/app/ui/preferences_dialog.py:2250-2253 为 `except Exception: pass`。
  - 影响: 当前 DEFAULT_CONFIG 含全部 7 键（config_manager.py:92-98）故不触发；一旦某键缺失，restore_defaults 抛 KeyError 且被调用方 `except Exception: pass` 静默吞掉——本页"恢复默认值"整体静默失效，无日志无提示。
  - 建议: 统一改为 `defaults.get(key, fallback)`，与 load()/restore_defaults 后半段一致。
  - 置信度: 高

- [AUD-12106] [Suggestion] 注释声称 Weak reference 但实际保存强引用
  - 位置: src/features/deliver/preferences.py:45, 51
  - 证据:
    ```python
    self._dialog_ref: Any = None  # Weak reference to PreferencesDialog for _browse_path
    ...
    def create_widget(self, parent) -> QWidget:
        self._dialog_ref = parent
    ```
  - 影响: 注释与实现不符；页面持有对话框强引用形成 Python 引用环（靠 GC 回收）；若同一 page 对象被复用于新对话框实例会持有已关闭的旧对话框作为 QFileDialog parent。
  - 建议: 改用 `weakref.ref(parent)` 并在 _browse_path 解引用，或修正注释。
  - 置信度: 高

- [AUD-12107] [Suggestion] 跨模块导入 export_panel 的私有下划线样式助手
  - 位置: src/features/deliver/preferences.py:27-30
  - 证据:
    ```python
    from src.shared.export.export_panel import (
        _apply_text_input_style,
        _apply_compact_button_style,
    )
    ```
  - 影响: 依赖 shared/export/export_panel 的私有实现细节；对方重构或删除这两个私有函数时，preferences.py 在导入期即失败，进而 register_preference_pages（module.py:230）抛 ImportError。
  - 建议: 将两个样式助手提升为 src/shared/ui 公共 API（去下划线）后双方共用。
  - 置信度: 高

- [AUD-12108] [Suggestion] create_widget 的 parent 参数缺类型注解
  - 位置: src/features/deliver/preferences.py:49
  - 证据: `def create_widget(self, parent) -> QWidget:`
  - 影响: 弱类型契约；与 .clinerules 强类型要求不符。
  - 建议: 标注为 `parent: QWidget`。
  - 置信度: 高

### 通过项
- 信号防递归：本页唯一信号连接为 quality_slider.valueChanged → quality_value.setText（label 不回馈信号），load()/restore_defaults() 程序化设值不构成递归回路；页面为 apply 时一次性写配置模式，无即时 apply-on-change 回路，无需状态锁
- 状态提交一致性：apply() 全部 auto_save=False（preferences.py:214-234），调用方批量委托后统一 cfg.save_config()（preferences_dialog.py:2053-2059），持久化无缺口
- 输入验证：load() 对全部键带默认值；格式/Profile 用 findText 守卫非法值不落选
- _browse_path 使用 QFileDialog.getExistingDirectory 且对空路径守卫，父窗口 _dialog_ref 在 create_widget 中必先赋值
- 纯 UI 线程控件构建，无 OpenGL/线程/GPU/HDR 维度

---

## src/features/deliver/srt_exporter.py (行数: 101)

### 发现

- [AUD-12109] [Suggestion] fps 解析失败时静默回退 24.0，可能整体错位时间码且无日志
  - 位置: src/features/deliver/srt_exporter.py:64-74
  - 证据:
    ```python
    fps = 24.0
    seq_fps = getattr(sequence, 'fps', None)
    if seq_fps is not None:
        if hasattr(seq_fps, 'to_fps'):
            fps = float(seq_fps.to_fps())
        else:
            try:
                fps = float(seq_fps)
            except (TypeError, ValueError):
                pass
    ```
  - 影响: sequence.fps 缺失或不可转换时（异常/降级数据），全部时间码按 24fps 计算；对 23.976/25/29.97/30fps 时间线，导出字幕整体时间错位，且无任何 warning 可查。正常路径（TimelineSequence.fps 为 Rational，含 to_fps，timeline_v2.py:2930 / metadata.py:69）不受影响。
  - 建议: 回退分支补 `logger.warning("sequence fps invalid (%r), falling back to 24.0", seq_fps)`。
  - 置信度: 高

### 通过项
- 时间轴 API 使用正确：SubtitleClip.timeline_end 为 start+duration 属性（timeline_v2.py:1391-1392），frames_to_srt_timecode(frame: int, fps: float) 签名匹配（subtitle_renderer.py:352），其内部对 fps<=0 与负毫秒均有防护
- 输入验证：跳过禁用/不可见轨道、禁用片段、空白文本片段（srt_exporter.py:40-46），与 docstring 声明一致；稳定排序保证同时刻片段按轨道顺序输出
- 异常路径：顶层 except Exception 记录完整 traceback 并按契约返回 False，调用方（deliver_controller.py:1124-1135）对 False 弹错误框，不违反"宽泛 except 掩盖领域错误"红线
- 输出编码 utf-8-sig 有注释说明（部分播放器需 BOM）；纯文件 IO，无 GPU/线程/信号/undo 维度

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 1 | AUD-12100 |
| Suggestion | 9 | AUD-12101…AUD-12109 |

整体评价：该目录为 Deliver 功能的集成壳层（模块注册、偏好页、SRT 导出、re-export facade），无渲染/GPU/线程热点。唯一 Major 是 facade 与 module 两处同源导入防御不一致导致的潜在启动崩溃路径；其余为类型注解、日志与防御性编码质量项。
