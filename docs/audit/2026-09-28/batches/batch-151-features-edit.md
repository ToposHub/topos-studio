# batch-151 [P1] src/features/edit//

审查范围：`src/features/edit/commands_facade.py`（1 个文件，249 行）。
编号区间：AUD-15100–AUD-15199。审查日期：2026-09-28。

该模块为懒加载 re-export 门面（facade）：不承载业务逻辑，仅在函数内延迟导入
`src.features.edit.core/*`、`src.features.edit.ui/*` 与 `src.features.teaching_animation.host_adapters`
的符号并以 dict/类形式返回，ImportError 时记 warning 并返回 None。
已交叉验证：所引用的全部 11 个命令类、参数/选择/工程/时间线/UI 类名均真实存在于目标模块；
调用方（src/app/main_window.py、src/app/controllers/edit_controller.py、file_controller.py）均对
None 返回做了防御。

## src/features/edit/commands_facade.py (行数: 249)

### 发现

- [AUD-15100] [Suggestion] 全部 12 个公开函数缺少有意义的返回类型注解，且含空 TYPE_CHECKING 块
  - 位置: src/features/edit/commands_facade.py:16-17,35,72,91,110,123,146,162,178,191,204,217
  - 证据:
    ```python
    if TYPE_CHECKING:
        pass

    def get_command_classes():
        """Return the edit command classes as a namespace dict."""
    ```
    ```python
    def create_teaching_animation_clip_command(scene: Any, timeline_start: int = 0,
                                               timeline_duration: int | None = None,
                                               track_id: str | None = None,
                                               mode: str = "append") -> Any:
    ```
  - 影响: 11 个 `get_*` 函数无返回注解，`create_teaching_animation_clip_command` 的 scene/返回值用
    `Any`（实际构造函数签名为 `MotionGraphicClipCommand(scene: TeachingSceneDocument, ...)`，
    host_adapters.py:153），静态检查完全失效，违背项目"所有函数有类型注解"的强类型约束；
    第 16-17 行的空 `if TYPE_CHECKING: pass` 是无意义死代码。
  - 建议: 为各函数标注真实返回类型（如 `-> dict[str, type] | None`、`-> type[EditProjectIO] | None`、
    `-> MotionGraphicClipCommand | None`，可配合 TYPE_CHECKING 导入），删除空 TYPE_CHECKING 块。
  - 置信度: 高

- [AUD-15101] [Suggestion] ImportError 降级日志不含异常详情，会把真实的导入缺陷（如循环导入、名称改错）伪装成"模块不可用"
  - 位置: src/features/edit/commands_facade.py:31,68,87,106,119,142,158,175,188,201,214,248
  - 证据:
    ```python
    except ImportError:
        logger.warning("Edit commands module unavailable")
        return None
    ```
    对比调用方的做法（src/app/main_window.py:1350）：
    ```python
    except ImportError as e:
        logger.warning(f"Edit UI imports failed: {e}")
    ```
  - 影响: `except ImportError` 会连带捕获目标模块内部传导出的 ImportError（部分初始化/循环导入、
    11 个名称中任意一个被重命名、可选依赖缺失等）。此时日志只留一句"module unavailable"且无
    traceback/异常消息，整个 Edit 功能面被静默降级（如 main_window 将 `_edit_available` 置 False），
    根因排查困难。`except ImportError` 本身不算宽泛 except（符合铁律），问题仅在日志丢失细节。
  - 建议: 改为 `except ImportError as e:` 并在 warning 中附带 `e` 或使用 `logger.warning(..., exc_info=True)`；
    可考虑区分"模块整体缺失"与"模块存在但导入失败"两种情形。
  - 置信度: 中

- [AUD-15102] [Suggestion] create_teaching_animation_clip_command 无生产调用方，且生产代码绕过门面直接 deep-import，违背本模块自述的边界约定
  - 位置: src/features/edit/commands_facade.py:22-32
  - 证据:
    ```python
    def create_teaching_animation_clip_command(scene: Any, timeline_start: int = 0,
                                               ...
        try:
            from src.features.teaching_animation.host_adapters import MotionGraphicClipCommand
            return MotionGraphicClipCommand(scene, timeline_start, timeline_duration, track_id, mode)
        except ImportError:
            logger.warning("Teaching Animation clip command unavailable")
            return None
    ```
    而生产代码直接深导入（src/app/controllers/edit_controller.py:5888、src/features/edit/ui/timeline_view.py:14060、
    src/features/teaching_animation/module.py:3462）：`from src.features.teaching_animation.host_adapters import MotionGraphicClipCommand`。
    全仓检索仅 tests/integration/test_teaching_animation_phase4.py:2 引用该门面函数。
  - 影响: 模块 docstring 声称"controller imports from this facade at the feature boundary"，但该函数是
    仅被测试使用的死代码；同一符号存在门面与直连两条路径，后续维护者难以判断应走哪条，边界约定名存实亡。
  - 建议: 要么让 edit_controller/timeline_view 改走该门面函数，要么删除该函数并更新测试为直接导入；
    同步修正 docstring 中关于边界的描述。
  - 置信度: 高

### 通过项
- 线程安全：模块无共享可变状态（仅常量 logger）；函数内延迟导入受 CPython 导入锁保护，多线程首次导入安全。
- GPU 资源生命周期：不创建/持有任何 ModernGL 或纹理资源，无泄漏面。
- GLSL·HDR 正确性：不涉及着色器与像素数据，无 clamp/字面量问题。
- 信号防递归：不操作 Qt 控件、不连接信号，无递归风险。
- 状态提交一致性：门面仅返回命令类/实例，不执行 apply/undo，不触碰 revision 与持久化。
- 异常与取消路径：所有 except 均为窄域 `except ImportError`（符合"不得宽泛 except"铁律）；None 返回契约在
  docstring 中写明，且已验证三个调用方均有 None 防御（edit_controller.py:1482-1484、3828-3830、
  main_window.py:1329-1330）。
- 输入验证：门面为纯转发层，参数直传已具类型化默认值的目标构造函数，无需重复验证（无外部原始数据进入）。
- 性能模式：懒加载设计正确避免循环导入；每次调用重建 dict 的开销为微秒级且仅在用户操作路径触发（非每帧/每像素），不构成 4K 实时瓶颈。
- 导出符号一致性：已逐一验证 11 个命令类（commands.py:2601-14131）、SpeedParams/TransformParams、
  TimelineSelection/SelectionType、EffectInstance、EditProjectIO/EditProjectIOConfig/UIState（重导出，edit_project_io.py:51）、
  EDIT_PROJECT_EXTENSION、EditProject/TimelineSequenceRef、EditTrack/TimelineSequence、
  create_timeline_preview_renderer、ViewMode、QuickProjectDialog 及 11 个 UI 类均真实存在，无失效引用。
