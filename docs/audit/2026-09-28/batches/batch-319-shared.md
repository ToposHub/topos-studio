# batch-319 — src/shared/

编号区间：AUD-31900–AUD-31999。审查日期：2026-09-28。

## src/shared/app_restart.py (行数: 47)

### 发现
- [AUD-31901] [Major] 未处理冻结打包（frozen）场景：以 `[sys.executable, script_path]` 重启在 PyInstaller 等分发形态下会失败
  - 位置: src/shared/app_restart.py:22-24
  - 证据:
    ```python
        # sys.argv[0] 可能是相对路径，需要转换为绝对路径
        script_path = os.path.abspath(sys.argv[0])
        work_dir = os.path.dirname(script_path)
        args = [sys.executable, script_path] + sys.argv[1:]
    ```
  - 影响: 源码运行（python main.py）时正确；但应用若以 PyInstaller/py2app 等冻结方式分发，sys.argv[0] 是可执行文件本身，sys.executable 是打包内嵌的解释器——用解释器去"执行"一个 .exe/.app 会直接失败，重启功能整体不可用。是否已有 frozen 分发未在仓库中找到证据。
  - 建议: 入口处判断 `getattr(sys, "frozen", False)`：冻结时 args = [sys.executable] + sys.argv[1:]（或直接以 sys.argv[0] 启动），仅源码形态走现有拼装。
  - 置信度: 低（缺陷路径明确，但 frozen 分发是否存在未证实）

- [AUD-31902] [Suggestion] subprocess.Popen 失败无异常处理，旧进程既未重启也未给出用户反馈
  - 位置: src/shared/app_restart.py:32-39
  - 证据:
    ```python
        if sys.platform == "win32":
            subprocess.Popen(
                args, cwd=work_dir,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            subprocess.Popen(args, cwd=work_dir, start_new_session=True)
    ```
  - 影响: Popen 可能抛 OSError（脚本路径含空格/权限/可执行缺失等）。异常沿菜单动作调用栈向上冒泡为未处理 traceback；此刻新进程未起、旧进程未退，用户只见报错无恢复指引。调用方（preferences_dialog.py:2272、menu_toolbar_controller.py:717 等四处）也未捕获。
  - 建议: 以 try/except OSError 包裹 Popen，失败时 logger.exception 并向用户提示"重启失败，应用继续运行"。
  - 置信度: 高（事实路径确定）

- [AUD-31903] [Suggestion] 以 time.sleep(0.3) 阻塞调用线程错峰端口竞争，属不可靠的时序假设
  - 位置: src/shared/app_restart.py:41-42
  - 证据:
    ```python
        # 给新进程一点时间完成端口绑定前的启动，错开文件/端口竞争
        time.sleep(0.3)
    ```
  - 影响: 该函数由 UI 动作（菜单/偏好设置）触发，sleep 在主线程冻结 UI 300ms；且固定睡眠并不能保证新进程完成任何阶段性启动（慢机器上不足、快机器上多余），端口冲突实际依赖注释所述"新进程端口检测顺延"兜底，此 sleep 只提供弱启发式。
  - 建议: 保留兜底逻辑即可移除或缩短 sleep；如需确认启动可改为新进程就绪信号（如探测健康端口/心跳文件），非主线程等待。
  - 置信度: 高（阻塞与弱假设为事实；实际端口冲突率未测）

### 通过项
- 方案一致性：三种历史重启路径（detached subprocess / os.execv）统一到本模块，符合 [R2-6] 注释描述；旧进程经 app.quit() 走正常退出链（QThreadPool 清理、配置落盘），优于 execv 原地替换。
- 线程安全：无共享可变状态；QApplication.instance() 为 None 时跳过 quit（守护了非 GUI 上下文调用）。
- GPU 资源生命周期：不直接触碰 OpenGL；退出清理交由应用正常退出链。
- 跨平台分支：win32 用 DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP，POSIX 用 start_new_session=True，均正确实现脱离会话的独立子进程。
- 日志：重启动作有 logger.info 记录命令行与 cwd。
- 类型注解：restart_application() -> None 完整。
