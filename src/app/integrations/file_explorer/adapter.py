import os
import sys
import socket
import json
import errno
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)
from PyQt6.QtCore import QObject, QThread


# ---------------------------------------------------------------------------
# Path Injection — walk up from __file__ to find project root
# ---------------------------------------------------------------------------

def _find_project_root(start: Path) -> Path:
    """Walk upward from *start* to locate the directory that contains
    ``external/file_explorer/main.py``.  This avoids hard-coding dirname
    depth, so the adapter keeps working after future file moves."""
    for candidate in [start, *start.parents]:
        if (candidate / "external" / "file_explorer" / "main.py").exists():
            return candidate
    raise RuntimeError(
        "Cannot locate external/file_explorer from adapter.py"
    )


PROJECT_ROOT = _find_project_root(Path(__file__).resolve())
external_path = str(PROJECT_ROOT / "external")
file_explorer_path = str(PROJECT_ROOT / "external" / "file_explorer")

for _p in (external_path, file_explorer_path):
    if _p not in sys.path:
        # [R2-1] append 而非 insert(0)：file_explorer 目录含 main.py/const.py/
        # utils.py 等通用名模块，放 sys.path 头部会遮蔽其他同名模块
        sys.path.append(_p)

# Import the Flask app and runtime bootstrap (optional: file_explorer may not be installed)
_file_explorer_import_error = None
try:
    from file_explorer.main import (
        app as file_explorer_app,
        find_available_port,
        ensure_runtime as file_explorer_ensure_runtime,
    )
except ImportError as exc:
    file_explorer_app = None
    find_available_port = None
    file_explorer_ensure_runtime = None
    _file_explorer_import_error = exc

try:
    from waitress.server import create_server as _waitress_create_server
except ImportError:
    _waitress_create_server = None
try:
    from werkzeug.serving import make_server
except ImportError:
    make_server = None


class _WaitressServerWrapper:
    """[ARCH-8] waitress 服务的统一接口：run() 阻塞服务，stop() 幂等停止。"""
    backend = 'waitress'

    def __init__(self, server):
        self._server = server
        self.port = server.socket.getsockname()[1]

    def run(self) -> None:
        try:
            self._server.run()
        except OSError as e:
            # stop() 跨线程 close() 后，阻塞中的 select() 会以 EBADF 唤醒——
            # 这是 waitress 预期的停止路径噪音，不是故障；其他 OSError 照常抛出
            if e.errno != errno.EBADF:
                raise

    def stop(self) -> None:
        try:
            self._server.close()
        except Exception:
            pass


class _WerkzeugServerWrapper:
    """[ARCH-8] werkzeug 回退服务的统一接口（[CRASH-1] serve_forever/shutdown 配对）。"""
    backend = 'werkzeug'

    def __init__(self, server):
        self._server = server
        self.port = server.server_port

    def run(self) -> None:
        self._server.serve_forever(poll_interval=0.5)

    def stop(self) -> None:
        try:
            self._server.shutdown()
        finally:
            try:
                self._server.server_close()
            except Exception:
                pass


def create_http_server(app, host, port):
    """[ARCH-8] 创建 HTTP 服务：优先 waitress（生产级 WSGI，线程模型稳定），
    未安装时回退 werkzeug dev server。返回统一接口或 None（均不可用）。"""
    if _waitress_create_server is not None:
        return _WaitressServerWrapper(
            _waitress_create_server(app, host=host, port=port, threads=4))
    if make_server is not None:
        return _WerkzeugServerWrapper(make_server(host, port, app, threaded=True))
    return None


def _validated_host(value: object, default: str = '127.0.0.1') -> str:
    """[AUD-34001] host 白名单校验：仅允许本机回环地址。

    ``.port_config.json`` 是外部 JSON 文件，host 未校验时 ``0.0.0.0``
    等值会让内嵌服务器绑定所有网卡，引入局域网暴露面。
    """
    if isinstance(value, str) and value in ('127.0.0.1', 'localhost'):
        return value
    if value not in (None,):
        logger.warning(
            "file_explorer port config rejected host %r (not loopback); "
            "falling back to %r", value, default)
    return default


def _validated_port(value: object) -> Optional[int]:
    """[AUD-34001] port 值域校验：int 且 1-65535，否则回退默认端口。"""
    if isinstance(value, bool) or not isinstance(value, int):
        if value is not None:
            logger.warning(
                "file_explorer port config rejected port %r (not int); "
                "falling back to default", value)
        return None
    if not (1 <= value <= 65535):
        logger.warning(
            "file_explorer port config rejected port %r (out of range); "
            "falling back to default", value)
        return None
    return value


def read_port_config() -> tuple[str, int]:
    """[AUD-34001] 读取 file_explorer 保存的端口配置。

    读取/解析失败收窄为 ``OSError``/``json.JSONDecodeError`` 并记录
    warning（不再裸 except 吞掉一切，含 KeyboardInterrupt/SystemExit）；
    host/port 取出后经白名单与值域校验，坏配置回退默认值并留下可排查
    的日志，而不是让非法值流入后续端口探测/服务器绑定形成静默失败链。
    """
    port_config_path = os.path.join(file_explorer_path, '.port_config.json')
    host: object = None
    port: object = None
    try:
        if os.path.exists(port_config_path):
            with open(port_config_path, 'r') as f:
                config = json.load(f)
                if isinstance(config, dict):
                    host = config.get('host')
                    port = config.get('port')
                else:
                    logger.warning(
                        "file_explorer port config has non-dict root %r; "
                        "falling back to defaults", type(config).__name__)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "failed to read file_explorer port config %s: %s; falling "
            "back to defaults", port_config_path, exc)
    return (
        _validated_host(host),
        _validated_port(port) or _default_port(),
    )


def _default_port():
    """[ARCH-5] 默认端口单一来源是 file_explorer/const.py（sys.path 已注入）。"""
    try:
        import const
        return const.DEFAULT_PORT
    except ImportError:
        return 8188


class FlaskWorker(QThread):
    def __init__(self, app: Any, host: str, port: int) -> None:
        super().__init__()
        self.app = app
        self.host = host
        self.port = port
        self.server = None
        # [AUD-34002] stop() 到达时 server 可能尚未创建（run() 还在
        # ensure_runtime/create_http_server 里）。只判 self.server 会空转，
        # 只能靠 wait(3000) 超时兜底走到 terminate()。先记录停止请求，
        # run() 在启动服务前/后各检查一次，让早到的 stop() 真正生效。
        self._stop_requested = False

    def run(self) -> None:
        # [CRASH-1] 阻塞运行 + stop() 配对停止语义（waitress: run/close；
        # werkzeug: serve_forever/shutdown），杜绝手写 handle_request 循环
        # 造成的 shutdown 永久挂死。
        try:
            if self._stop_requested:
                return
            # [ARCH-6] import main.py 零副作用：插件/DB/工作线程/端口文件
            # 延迟到此处显式初始化（幂等，独立模式同样走这一入口）
            # [R1-6] 传入本 worker 实际将绑定的端口，确保端口文件与
            # 实际绑定一致，消除探测 TOCTOU 漂移
            if file_explorer_ensure_runtime is not None:
                file_explorer_ensure_runtime(port=self.port)
            self.server = create_http_server(self.app, self.host, self.port)
            if self.server is None:
                logger.error("Cannot start file_explorer server: no WSGI backend "
                             "(install waitress or flask)")
                return
            # [FUNC-14] 以实际绑定成功的端口为准，回写共享端口配置，
            # 消除"端口探测 TOCTOU + 配置漂移"
            self.port = self.server.port
            self._write_port_config()
            if self._stop_requested:
                return  # stop() 在 server 创建后、进入服务循环前到达
            self.server.run()
        except Exception as e:
            logger.error(f"Error: {e}")
        finally:
            if self.server:
                self.server.stop()

    def _write_port_config(self) -> None:
        try:
            config_path = os.path.join(file_explorer_path, '.port_config.json')
            with open(config_path, 'w') as f:
                json.dump({'port': self.port, 'host': self.host}, f)
        except Exception:
            logger.debug("write port config failed", exc_info=True)

    def stop(self) -> None:
        """停止服务器（与 run() 配对的快速停止，见 create_http_server 契约）。

        [AUD-34002] 先置停止请求标志：server 尚未创建（run() 前段）时
        本方法也能让 run() 在进入服务循环前自行退出，而不是空转。
        """
        self._stop_requested = True
        if self.server is not None:
            self.server.stop()


class FileExplorerAdapter(QObject):
    """Flask 服务器适配器，管理后台 Web 服务器"""

    _instance = None

    @classmethod
    def instance(cls):
        """获取单例实例"""
        if cls._instance is None:
            cls._instance = FileExplorerAdapter()
        return cls._instance

    def __init__(self, parent=None):
        # 防止通过直接调用构造函数创建多个实例
        if FileExplorerAdapter._instance is not None and FileExplorerAdapter._instance is not self:
            raise RuntimeError("Use FileExplorerAdapter.instance() to get the singleton")

        super().__init__(parent)
        self.worker = None
        # 优先读取 file_explorer 保存的端口配置
        self.host, self.port = read_port_config()

    def start_server(self) -> bool:
        """Starts the background Flask server thread if it's not running.

        Returns True if the server was started (or already running), False if
        dependencies are missing.
        """
        if file_explorer_app is None or (_waitress_create_server is None and make_server is None):
            logger.warning(
                "Cannot start file_explorer server: app=%s waitress=%s make_server=%s "
                "import_error=%r path=%s",
                file_explorer_app, _waitress_create_server, make_server,
                _file_explorer_import_error, file_explorer_path,
            )
            return False
        if self.worker is None or not self.worker.isRunning():
            # 先停止旧的 worker（如果有），确保资源被正确释放
            if self.worker is not None:
                self.stop_server()

            # [R0-2] ensure_runtime 必须先在宿主 GUI 线程执行一次：插件 setup()
            # 创建的 GuiLauncher(QObject) 线程亲和跟随调用线程，若留到
            # FlaskWorker(QThread) 里首次执行，插件信号会队列投递到无 Qt 事件
            # 循环的 worker 线程，槽永不执行（GUI 插件启动静默失效）。
            # ensure_runtime 幂等，worker.run() 里的再次调用为空转。
            # [R1-6] 传入 adapter 选定的端口，端口文件直接写真实值。
            if file_explorer_ensure_runtime is not None:
                file_explorer_ensure_runtime(port=self.port)

            # 检查端口是否被占用，如果被占用则自动换一个端口
            def is_port_in_use(host, port):
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(0.5)
                    try:
                        return s.connect_ex((host, port)) == 0
                    except Exception:
                        return False

            if is_port_in_use(self.host, self.port):
                logger.info(f"Port {self.port} is in use, finding alternative...")
                # 使用与 file_explorer 相同的算法找可用端口
                self.port = find_available_port(self.port)
                logger.info(f"Using alternative port: {self.port}")

            self.worker = FlaskWorker(file_explorer_app, host=self.host, port=self.port)
            self.worker.start()
        return True

    def get_server_url(self) -> str:
        """Returns the base URL for the background server."""
        # 优先取 worker 实际绑定端口（换端口场景下与配置文件保持一致）
        port = self.port
        worker = self.worker
        if worker is not None and worker.isRunning():
            port = worker.port
        return f"http://{self.host}:{port}"

    def stop_server(self) -> None:
        """Terminates the server thread gracefully.

        [AUD-34002] terminate() 是异步强杀：旧实现随后立刻 ``self.worker
        = None`` 丢弃最后一个 Python 引用，线程尚未真正退出时 QThread 被
        C++ 侧析构，触发 "QThread: Destroyed while thread is still
        running" 硬崩溃。现在 terminate 后追加有界等待；仍未退出则保留
        引用并挂 ``finished → deleteLater``，绝不在运行中弃引用。
        """
        worker = self.worker
        if worker is not None and worker.isRunning():
            # 先调用 worker 的 stop 方法停止 Flask 服务器（server 未创建
            # 时也会置停止请求标志，见 FlaskWorker.stop）。
            if hasattr(worker, 'stop'):
                worker.stop()
            # 等待线程退出
            if not worker.wait(3000):  # 等待最多 3 秒
                logger.warning("Worker thread did not terminate gracefully")
                worker.terminate()
                if not worker.wait(1000):
                    # 极端路径：强杀后仍未退出。保留引用防止带线程析构，
                    # 线程最终退出时再清引用并释放对象。
                    logger.error(
                        "Worker thread survived terminate(); deferring "
                        "deletion until finished")
                    try:
                        worker.finished.connect(
                            lambda ref=worker: self._forget_worker(ref))
                        worker.finished.connect(worker.deleteLater)
                    except (RuntimeError, TypeError):
                        pass
                    return
        self.worker = None
        logger.info("Server stopped")

    def _forget_worker(self, worker: "FlaskWorker") -> None:
        """[AUD-34002] 僵尸线程最终退出后清除引用（幂等）。"""
        if self.worker is worker:
            self.worker = None
            logger.info("Server stopped (deferred worker exit)")
