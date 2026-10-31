# batch-034 审查报告 — src/app/integrations/file_explorer//

- 任务书：docs/audit/2026-09-28/assignments/batch-034.md
- 编号区间：AUD-3400–AUD-3499（实际使用 AUD-34001–AUD-34009）
- 审查日期：2026-09-28
- 审查范围：3 个文件，共 745 行（`__init__.py` 为 0 字节空文件）
- 跨模块核实记录：`frame_ready = pyqtSignal(int, object)`（src/features/edit/core/timeline_preview_renderer.py:5734）、`engine.frame_presented = pyqtSignal(int)`（src/features/edit/core/realtime_playback/engine.py:109）、`controller.state_changed = pyqtSignal(str)`（src/features/edit/core/edit_playback_controller.py:200）、`timeline_panel.start_playback(direction, speed)/pause_playback/get_playback_controller`（src/features/edit/ui/timeline_panel.py:975/1011/2220）、trace collector `start_session/end_session/get_summary`（src/shared/video/playback_frame_trace.py:245/254/320）、`ensure_runtime(port=None)`（external/file_explorer/bridge.py:183）。engine.stats() 的 total_* 计数为引擎自有计数器（engine.py:199/816/1075-1085），`telemetry.reset()` 仅清空聚合器窗口（telemetry.py:187-195），二者口径已核实不冲突。

---

## src/app/integrations/file_explorer/__init__.py (行数: 0)

### 发现

- 无。

### 通过项

- 通过（无逻辑）：0 字节空文件，仅作为包标记存在，未导出任何符号、无副作用。

---

## src/app/integrations/file_explorer/adapter.py (行数: 280)

### 发现

- [AUD-34001] [Major] read_port_config 裸 `except:` 吞掉一切异常且不校验 host/port 值，坏配置可致服务器静默启动失败并陷入无限重试
  - 位置: src/app/integrations/file_explorer/adapter.py:120-130
  - 证据:
    ```python
    def read_port_config():
        """读取 file_explorer 保存的端口配置"""
        port_config_path = os.path.join(file_explorer_path, '.port_config.json')
        try:
            if os.path.exists(port_config_path):
                with open(port_config_path, 'r') as f:
                    config = json.load(f)
                    return config.get('host', '127.0.0.1'), config.get('port', _default_port())
        except:
            pass
        return '127.0.0.1', _default_port()
    ```
  - 影响: 违反两条铁律（不得宽泛 except 掩盖领域错误；外部数据须验证）。`.port_config.json` 是外部 JSON 文件，但 host/port 取出后未做任何类型/范围校验：(1) 损坏的 JSON 被裸 except 静默吞掉后回退默认值，无任何日志，配置漂移不可排查；(2) 若 port 为非 int（如字符串），`is_port_in_use` 里的 TypeError 也被吞（见 AUD-34003）返回"未占用"，随后 `create_http_server` 在 worker 线程内抛 ValueError 被 run() 的兜底 except 吃掉——`start_server()` 已返回 True，FilesPanel 进入 4 秒一次的 `_load_url` 无限重试（src/app/ui/files_panel.py:169-176），服务器永远起不来；(3) host 未校验时 `0.0.0.0` 会让内嵌服务器绑定所有网卡，引入局域网暴露面。
  - 建议: 收窄为 `except (OSError, json.JSONDecodeError)` 并记录 warning；对 host 做白名单校验（限定 `127.0.0.1`/`localhost`），对 port 做 `isinstance(port, int) and 1 <= port <= 65535` 校验后再使用。
  - 置信度: 高

- [AUD-34002] [Major] stop_server 兜底 terminate() 后立即丢弃 worker 引用，且 stop() 在 server 尚未创建时会空转，可致 QThread 带线程销毁崩溃
  - 位置: src/app/integrations/file_explorer/adapter.py:276-279（关联 185-188、161）
  - 证据:
    ```python
            if not self.worker.wait(3000):  # 等待最多 3 秒
                logger.info(f"Worker thread did not terminate gracefully")
                self.worker.terminate()
            self.worker = None
            logger.info(f"Server stopped")
    ```
  - 影响: 两处叠加风险。(1) `FlaskWorker.stop()` 以 `if self.server is not None` 为前提（185-188 行），而 `self.server` 要等 run() 执行到 161 行才赋值——`start_server()` 返回后立即 `stop_server()` 的窗口期内 stop() 空转，只能靠 `wait(3000)` 超时兜底；若 `create_http_server` 对未校验 host 做 DNS 解析阻塞（见 AUD-34001）超过 3 秒，就会走到 `terminate()`。(2) `terminate()` 是异步强杀（Qt 文档明确不推荐，可能死锁/破坏锁状态/中断 SQLite 写导致 DB 损坏），且随后立刻 `self.worker = None` 丢弃最后一个 Python 引用——线程尚未真正退出时 QThread 被 C++ 侧析构，触发 "QThread: Destroyed while thread is still running" 硬崩溃。该路径在 MainWindow.closeEvent → FilesPanel.shutdown（files_panel.py:271）每次退出都会走到（正常情况下 3 秒内优雅退出，触发需时序异常）。
  - 建议: stop() 改为记录"已请求停止"标志（或在 start_server 中同步完成 server 创建后再返回）；terminate() 后追加 `self.worker.wait(1000)` 再清引用；或用 `deleteLater` + 等待 `finished` 信号后再置 None。
  - 置信度: 中

- [AUD-34003] [Suggestion] is_port_in_use 裸 `except:` 一律返回 False，把探测异常当成"端口空闲"
  - 位置: src/app/integrations/file_explorer/adapter.py:242-248
  - 证据:
    ```python
            def is_port_in_use(host, port):
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(0.5)
                    try:
                        return s.connect_ex((host, port)) == 0
                    except:
                        return False
    ```
  - 影响: 裸 except 同时捕获 KeyboardInterrupt/SystemExit，且把 TypeError（port 非法）、OSError（网络栈故障）统一解释为"端口可用"，掩盖真实错误并放行非法端口进入后续绑定（与 AUD-34001 叠加成静默失败链）。
  - 建议: 收窄为 `except OSError: return False`，TypeError 应向上抛出或提前在配置读取处拦截。
  - 置信度: 高

- [AUD-34004] [Suggestion] FlaskWorker.run 兜底 except 仅记录 str(e)，丢失堆栈，服务器启动失败难排查
  - 位置: src/app/integrations/file_explorer/adapter.py:171-172
  - 证据:
    ```python
            self.server.run()
        except Exception as e:
            logger.error(f"Error: {e}")
        finally:
            if self.server:
                self.server.stop()
    ```
  - 影响: worker 线程内任何启动/运行期异常只留下一条无堆栈的单行 error（消息可能为空字符串），现场不可复现；与 start_server 已返回 True 组合后，故障对上层完全不可见。
  - 建议: 改为 `logger.exception("FlaskWorker run failed")` 或 `logger.error(..., exc_info=True)`；消息附带 host/port 上下文。
  - 置信度: 高

- [AUD-34005] [Suggestion] 单例约束不完整：__init__ 不登记 _instance，直接构造两次可绕过防护
  - 位置: src/app/integrations/file_explorer/adapter.py:196-206
  - 证据:
    ```python
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
    ```
  - 影响: `_instance` 只在 instance() 里赋值。若首个对象由直接构造产生（`_instance` 仍为 None），随后 instance() 会再造一个"正牌"单例——两个 adapter 各自可启动 FlaskWorker，端口配置互写，行为难以解释。当前唯一调用方 files_panel.py:110 走 instance()，实际影响有限。
  - 建议: 在 `__init__` 末尾（或 finally）设置 `FileExplorerAdapter._instance = self`，使直接构造也纳入登记。
  - 置信度: 高（机制确定，触发概率低）

- [AUD-34006] [Suggestion] import 期副作用重：目录树上溯搜索 + 全局 sys.path 注入，环境不满足时直接 RuntimeError
  - 位置: src/app/integrations/file_explorer/adapter.py:30-38
  - 证据:
    ```python
    PROJECT_ROOT = _find_project_root(Path(__file__).resolve())
    external_path = str(PROJECT_ROOT / "external")
    file_explorer_path = str(PROJECT_ROOT / "external" / "file_explorer")

    for _p in (external_path, file_explorer_path):
        if _p not in sys.path:
            # [R2-1] append 而非 insert(0)：...
            sys.path.append(_p)
    ```
  - 影响: 模块导入即执行全局 sys.path 变更（虽已用 append 缓解通用名遮蔽，且仓库内无其他 const.py，已核实）；部署环境若缺少 external/file_explorer 目录（打包/裁剪安装），`_find_project_root` 在 import 期抛 RuntimeError，而 files_panel.py:15 在模块顶层无条件 import 本模块，导入链会直接炸掉主窗口。模块内对 flask/waitress 缺失做了优雅降级，唯独目录缺失没有同等处理。
  - 建议: 将 sys.path 注入与根目录定位延迟到 `start_server()` 首次调用时执行，或对 RuntimeError 提供与 ImportError 相同的降级路径（返回不可用状态而非中断导入）。
  - 置信度: 中（dev 仓库必然满足；风险仅在裁剪部署形态）

- [AUD-34007] [Suggestion] 多个公开函数与 FlaskWorker 参数缺少类型注解
  - 位置: src/app/integrations/file_explorer/adapter.py:109-117、120-121、133-139、142-148
  - 证据:
    ```python
    def create_http_server(app, host, port):
        """[ARCH-8] 创建 HTTP 服务：优先 waitress（生产级 WSGI，线程模型稳定），
        未安装时回退 werkzeug dev server。返回统一接口或 None（均不可用）。"""

    def read_port_config():
        """读取 file_explorer 保存的端口配置"""
    ```
  - 影响: `create_http_server` / `read_port_config` / `_default_port` / `FlaskWorker.__init__` 均无参数与返回类型注解，`read_port_config` 的 `(str, int)` 元组返回只能靠读实现得知，不符合项目强类型约定（.clinerules）。
  - 建议: 补齐 `-> Optional[object]`、`-> tuple[str, int]`、`app: Any, host: str, port: int` 等注解。
  - 置信度: 高

### 通过项

- GPU 资源生命周期：本模块为纯服务器适配层，无任何 ModernGL/OpenGL 对象创建或接触，无 GL 泄漏面（全文已核对）。
- GLSL·HDR 正确性：无着色器代码，无浮点字面量/clamp 相关问题。
- 信号防递归：不涉及程序化更新 Qt 控件，无信号回路风险。
- WSGI 停止配对正确：waitress run/close + EBADF 过滤（72-85）、werkzeug serve_forever/shutdown + server_close（96-106），语义幂等。
- 依赖缺失优雅降级：flask/waitress/werkzeug 全缺时 `start_server` 返回 False 并输出含 import_error 的详细诊断日志（219-226），不抛异常。
- ensure_runtime 先在 GUI 线程执行一次再交给 worker（232-239），规避 QObject 线程亲和导致的插件信号投递失效，注释完整；`ensure_runtime(port=...)` 签名已核实（external/file_explorer/bridge.py:183）。
- 端口漂移控制：占用探测 + find_available_port + worker 以实际绑定端口回写配置（161-169），与注释声称一致。
- stop_server 对 worker 为 None 的重复调用安全（幂等）。

---

## src/app/integrations/file_explorer/edit_benchmark_service.py (行数: 465)

### 发现

- [AUD-34008] [Suggestion] start_session 对外部插件传入的 frame_count/warmup_frames 直接 int() 转换，无类型守卫
  - 位置: src/app/integrations/file_explorer/edit_benchmark_service.py:290-291
  - 证据:
    ```python
        requested = max(1, min(int(frame_count), 10000))
        warmup = max(0, min(int(warmup_frames), 240))
    ```
  - 影响: 该方法经 file_explorer bridge 暴露给插件（main_window.py:1210-1218 注册），参数来自插件侧 JSON，属外部数据。数值范围有 clamp，但类型不做校验：传入不可转换值（如 `"abc"`、list）时 ValueError/TypeError 会从排队投递到 Qt 线程的槽里抛出，走 sys.excepthook，插件侧得不到结构化错误响应。违反"外部数据须验证"铁律的边界场景。
  - 建议: 用 try/except (TypeError, ValueError) 包裹转换并返回 `{"ok": False, "error": "参数必须是整数"}`，或提供 `_coerce_int(value, default)` 辅助函数。
  - 置信度: 中（机制确定；bridge 对参数是否已做序列化清洗未逐行核实）

- [AUD-34009] [Suggestion] telemetry.reset() 抛异常会连带丢弃已成功采集的 engine 基线，增量指标静默虚高
  - 位置: src/app/integrations/file_explorer/edit_benchmark_service.py:292-296
  - 证据:
    ```python
        try:
            baseline = engine.stats()
            engine.telemetry.reset()
        except Exception:
            baseline = {}
    ```
  - 影响: 若 `engine.stats()` 成功而 `engine.telemetry.reset()` 抛异常，`baseline` 被整体置 `{}`。snapshot 的增量计算（188-192 行）以 `baseline.get(key, 0)` 为基准，届时 delta 变成"进程启动以来的累计值"，且 max(0, ...) 掩盖负差，结果看似正常实则失真，无任何错误标记。已核实 telemetry.reset() 只清聚合器、不影响 engine.stats() 的 total_* 计数（engine.py:1075-1085），故该问题仅出现在 reset 抛异常的分支。
  - 建议: 拆成两个 try：stats 失败才置 `{}`；reset 失败单独记录 warning（reset 失败只影响窗口统计，不应作废基线）。
  - 置信度: 高（机制确定，触发罕见）

### 通过项

- 线程安全：`snapshot`/`start_session`/`stop_session` 全部入口有 `_on_qt_thread()` 守卫（132、271-272、438-439），Flask 线程误调用返回结构化错误而非触碰 Qt 对象；`_host()` 对 weakref 悬空 RuntimeError 有防护（68-72）。
- GPU 资源生命周期：全文件无 ModernGL/OpenGL 引用，仅读取 `renderer.gpu_pipeline_stats()`，GL 上下文仍归渲染器所有，与模块 docstring 声明一致（已核实 import 列表 18-25 行）。
- 信号防递归：`_on_playback_state_changed` 以 phase 集合短路（389-390），`_finishing` 标志防止 `_finish_session`/`_finalize_session` 重入（397-400、412-415）；`QTimer.singleShot(200)` 排空在途 GPU 任务后才取终态快照，设计合理。
- 信号连接/断开配对：`_renderer_connected`/`_engine_connected`/`_panel_connected` 三组标志与 `_disconnect_signals`（238-266）一一对应，断开失败有 (TypeError, RuntimeError) 捕获；信号元数与宿主实现已核实匹配（frame_ready(int,object)、frame_presented(int)、state_changed(str)）。
- 会话状态一致性：warmup→measuring→completed/cancelled 状态机闭合；completed/cancelled 后允许开新会话（273）；短时间线提前停止保留部分结果（392-395）；start 失败路径会回滚 trace session 并清空 self._session（337-344）。
- 取消/退出路径：`shutdown()` 挂接 aboutToQuit（60-62），暂停播放、结束 trace、断开信号、复位标志，顺序完整；`_finalize_session` 在 shutdown 之后触发时以 `self._session is None` 安全返回（413-415）。
- 性能模式：`_on_rendered_frame` 刻意不在每帧构建完整快照（372-374 注释），配合插件 100ms 轮询，避免测量扰动。
- 外部对象不泄漏：`_json_safe` 把不可序列化值降级为字符串（28-39），项目对象不出现在插件 JSON 里。
- 类型注解：公共方法均有完整参数/返回注解，内部宿主引用以 `Any` 显式标注（51-58），符合边界层惯例。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 2 | AUD-34001、AUD-34002 |
| Suggestion | 7 | AUD-34003–AUD-34009 |
