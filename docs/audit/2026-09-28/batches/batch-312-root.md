# 批次报告：batch-312 [P2] 主题 src//

- 编号区间：AUD-31200–AUD-31299
- 审查文件：1 个（src/main.py，312 行，已全文通读）
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解

## src/main.py (行数: 312)

应用入口：macOS dlopen 符号隔离、环境变量预设（OpenEXR/Qt/Chromium 软渲染）、QSurfaceFormat 与上下文共享配置、CLI 解析、模块注册、MainWindow 装配、有序退出（`_orderly_shutdown`）。

### 发现

- [AUD-31200] [Critical] QTimer 未导入：可选模块加载失败时启动即 FATAL 退出
  - 位置: src/main.py:291（使用处）；src/main.py:104-106（导入处缺失）
  - 证据:
    ```python
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtGui import QSurfaceFormat
    ```
    ```python
    if module_failures:
        _detail = ", ".join(sorted(module_failures))
        logger.warning("Feature modules unavailable: %s", _detail)
        from src.shared.i18n.i18n import I18N as _I18N
        QTimer.singleShot(1500, lambda: window.status_bar.showMessage(
    ```
  - 影响: `module_failures` 在 flag 启用但导入/注册失败时由 `register_builtin_modules` 填充（src/app/module_loader.py:150-151，真实可达的生产路径，如可选依赖缺失）。此时 `window.show()` 之后执行 `QTimer.singleShot` 抛 `NameError: name 'QTimer' is not defined`，被顶层 `except Exception`（main.py:302）捕获并 `sys.exit(1)`——应用刚显示窗口即退出，本应展示的用户提示（[R2-8] 功能不可用通知）永远无法出现，且日志误报为 "FATAL ERROR during application startup"。
  - 建议: 在 main.py:104 的 QtCore 导入中加入 QTimer（`from PyQt6.QtCore import Qt, QTimer`）。
  - 置信度: 高（全文件 grep 确认 QTimer 仅第 291 行出现，无任何导入；failures 填充路径已在 module_loader.py 核实）

- [AUD-31201] [Major] 致命错误路径用 sys.exit(1) 而非 _orderly_shutdown，重新暴露已文档化的解释器终结化崩溃
  - 位置: src/main.py:309（对照 main.py:137 的 `os._exit(exit_code)`）
  - 证据:
    ```python
    进程在 interpreter shutdown 阶段存在长期的崩溃问题（Qt6Core fail-fast
    0xc0000409）：CPython 以不可预测的 GC 顺序销毁 Qt 原生对象，与仍在
    派发的事件/运行中的线程竞争。……因此这里停完线程池后用 os._exit
    直接退出，保证退出路径确定性、不再随机段错误。
    ```
    ```python
        except Exception as e:
            import traceback
            logger.error(f"\n{'='*60}")
            logger.error(f"FATAL ERROR during application startup:")
            logger.error(f"{'='*60}")
            traceback.print_exc()
            logger.error(f"{'='*60}")
            sys.exit(1)
    ```
  - 影响: 启动阶段任何异常走 `sys.exit(1)` → SystemExit → 完整解释器终结化——正是本文件 docstring 记载的崩溃场景（Windows Qt6Core fail-fast 0xc0000409 / 随机段错误）。此路径不停 QThreadPool、不停 file_explorer 缩略图 ffmpeg 子进程；若异常发生在窗口/线程已创建之后（如 MainWindow 装配失败），原生崩溃可能在 traceback 可见之前发生，掩盖真实根因。
  - 建议: except 分支记录 traceback 后同样调用 `_orderly_shutdown(app, 1)`（或至少先停线程池与缩略图 worker 再退出）。
  - 置信度: 高（代码路径不一致为事实；实际是否触发原生崩溃取决于异常发生时的线程状态）

- [AUD-31202] [Suggestion] --project 参数未做存在性校验，非法路径静默无效
  - 位置: src/main.py:161-166, 263（下游 src/app/controllers/file_controller.py:429-430）
  - 证据:
    ```python
        parser.add_argument(
            "--project",
            default=None,
            metavar="PATH",
            help="Open the given .topos project after startup",
        )
    ```
    ```python
        window = MainWindow(
            feature_flags=feature_flags,
            feature_registry=registry,
            startup_project=args.project,
    ```
    （下游 file_controller.py:429-430：`if not filepath or not os.path.exists(filepath): return False`，无任何用户反馈）
  - 影响: 用户显式传入的 `--project` 若路径不存在或非项目文件，启动后静默无动作（下游直接 return False），用户无从得知项目未打开的原因。
  - 建议: main.py 在装配 MainWindow 前校验 `Path(args.project).exists()`（及后缀），失败时 logger.error 并提示/以非零码退出。
  - 置信度: 中（main.py 侧缺校验为事实；下游静默行为已核对 file_controller，但整体影响属体验缺陷）

- [AUD-31203] [Suggestion] 类型注解缺失/不精确与不可达返回
  - 位置: src/main.py:140（`def main():`）、249（`module_failures: dict = {}`）、280-281（`return 0`）
  - 证据:
    ```python
    def main():
        try:
    ```
    ```python
        module_failures: dict = {}
    ```
    ```python
        if args.test_startup:
            logger.info("Startup verification successful — exiting")
            _orderly_shutdown(app, 0)
            return 0
    ```
  - 影响: `main()` 无返回类型注解；`module_failures` 实际结构为 `{feature_id: str}`（module_loader.py 返回错误原因字符串），裸 `dict` 丢失信息；`_orderly_shutdown` 内 `os._exit` 永不返回，其后 `return 0` 为不可达死代码，且 `main()` 其余路径隐式返回 None，语义不一致。
  - 建议: 标注 `def main() -> int:`（或注明永不正常返回）、改 `dict[str, str]`、删除不可达 `return 0`。
  - 置信度: 高

### 通过项

- 线程安全：全部启动初始化均在主线程、app.exec() 之前完成；退出路径先 `QThreadPool.clear()/waitForDone(3000)` 再停缩略图 worker（main.py:118-131），无跨线程 OpenGL 操作
- GPU 资源生命周期：默认 QSurfaceFormat（4.5 Core + 双缓冲）与 `AA_ShareOpenGLContexts` 均在 QApplication 创建之前设置（main.py:178-206）；QtWebEngine 在任何 QCoreApplication 之前导入（main.py:99-102），顺序符合 Qt 约束
- GLSL·HDR 正确性：本文件无 GLSL/HDR 数据处理；默认 surface 为 8-bit RGBA 仅是控件表面格式，内部 HDR float 精度由渲染管线 FBO 承担，非问题
- 信号防递归：无程序化更新控件或信号回环路径（QTimer 用法为一次性触发，导入缺陷见 AUD-31200）
- 状态提交一致性：无 undo/redo/revision 逻辑；保存路由经 `registry.register_all_save_handlers()` 统一接线（main.py:275）
- 异常与取消路径：`_orderly_shutdown` 内两处宽泛 except 为带 debug 日志的尽力清理且注释说明缘由，未掩盖领域错误；顶层 except 打印完整 traceback（sys.exit 路径问题见 AUD-31201）
- 输入验证：`--product` 由 argparse `choices` 约束（main.py:146）；`parse_known_args` 保留 Qt 自身参数为刻意设计（`--project` 校验缺失见 AUD-31202）
- 性能模式：仅启动期一次 `QApplication.processEvents()` 触发布局（main.py:298），无热点路径
- 类型注解：除 AUD-31203 所列三项外，`_isolate_macos_native_extension_symbols` / `_orderly_shutdown` 注解完整；macOS dlopen 隔离与软渲染环境变量的平台分支正确（win32/非 win32 分界清晰）
