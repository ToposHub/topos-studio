"""统一的应用重启入口。

[R2-6] 此前三种重启路径并存：menu_toolbar 用 detached subprocess
（走正常退出清理），welcome/preferences/project_manager 用 os.execv
原地替换进程——execv 不做任何清理，Flask/后台线程瞬间蒸发。
全部统一为本模块的 subprocess 方案：新进程独立启动，旧进程走
正常退出链（QThreadPool 清理、缩略图 worker 停止、配置落盘）。
"""

import logging
import os
import subprocess
import sys

logger = logging.getLogger(__name__)


def _build_restart_command() -> tuple[list[str], str]:
    """构建重启命令行与工作目录（区分冻结分发与源码形态）。

    [AUD-31901] PyInstaller/py2app 等冻结形态下 ``sys.executable`` 是应用
    可执行文件本体（内嵌解释器），``sys.argv[0]`` 即该可执行文件——旧逻辑
    以 ``[sys.executable, script_path]`` 重启等于让内嵌解释器去"执行"一个
    .exe/.app，必然失败。冻结形态直接重启可执行文件本体即可。

    Returns:
        ``(args, work_dir)``：新进程 argv 与其工作目录。
    """
    if getattr(sys, "frozen", False):
        exe_path = os.path.abspath(sys.executable)
        return [exe_path] + list(sys.argv[1:]), os.path.dirname(exe_path)
    # 源码运行（python main.py）：sys.argv[0] 是入口脚本，可能是相对路径。
    script_path = os.path.abspath(sys.argv[0])
    return (
        [sys.executable, script_path] + list(sys.argv[1:]),
        os.path.dirname(script_path),
    )


def restart_application() -> None:
    """以 detached 子进程方式重启应用，然后退出当前进程。"""
    args, work_dir = _build_restart_command()

    logger.info("Restarting application: %s (cwd=%s)", args, work_dir)

    # 不主动停 file_explorer 服务器：stop_server() 可能阻塞退出，
    # 新进程已有端口检测和清理逻辑（start_server 端口占用顺延）
    if sys.platform == "win32":
        # Windows: DETACHED_PROCESS 使进程独立运行
        subprocess.Popen(
            args,
            cwd=work_dir,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    else:
        # macOS/Linux: start_new_session 使进程独立运行
        subprocess.Popen(args, cwd=work_dir, start_new_session=True)

    # AUD-31903: 删除旧的固定 0.3 秒启动错峰延时——固定延时是不可靠的
    # 时序假设（机器慢时远不够，快时白白延迟退出），且阻塞调用线程
    # （各 UI 入口）。端口/文件竞争由新进程自身的启动逻辑处理
    # （start_server 端口占用顺延、配置文件原子写），这里立即退出。

    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is not None:
        app.quit()
