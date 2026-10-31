# 批次报告：batch-330 [P2] src/shared/diagnostics/

审查范围：`src/shared/diagnostics/`（4 文件，498 行），编号区间 AUD-33000–AUD-33099。
审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交一致性 / 异常与取消 / 输入验证 / 性能模式 / 类型注解。

---

## src/shared/diagnostics/__init__.py (行数: 0)

### 发现

（无）

### 通过项
- 通过（无逻辑）：0 字节空文件，无任何导入或导出；仅使 `src.shared.diagnostics` 成为可导入包。

---

## src/shared/diagnostics/flash_diag.py (行数: 47)

### 发现

- [AUD-33001] [Suggestion] 每条事件重新 open/close 文件，高频发布链路下 I/O 开销放大
  - 位置: src/shared/diagnostics/flash_diag.py:43-45
  - 证据:
    ```python
    with _LOCK:
        with open(_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    ```
  - 影响: 模块文档说明事件挂在"每次 front-texture 发布 / 播放提交"上（调用方含 async_renderer.py 多处），启用诊断后 60Hz 级别的事件流意味着每秒数十次 open+write+close 系统调用，且与被诊断的渲染路径在同一进程内竞争 I/O。
  - 建议: 首次事件时打开持久句柄（或 `threading.local` 句柄），进程退出时关闭；或改为缓冲批量 flush。
  - 置信度: 高

- [AUD-33002] [Suggestion] `except Exception: pass` 完全静默，诊断失效无从察觉
  - 位置: src/shared/diagnostics/flash_diag.py:41-47
  - 证据:
    ```python
    try:
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _LOCK:
            with open(_PATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass
    ```
  - 影响: "Never raises" 契约本身合理，但若 `TOPOS_FLASH_DIAG` 指向不存在的目录、磁盘满或权限错误，每条事件都静默丢弃，用户以为在采集实则拿到空文件，浪费整轮诊断。属于宽泛 except 掩盖持续性失败（非领域错误，故仅 Suggestion）。
  - 建议: 记录一次性 warning（`logging.getLogger(__name__).warning` 加去重标志）或暴露 `dropped_events` 计数器。
  - 置信度: 高

- [AUD-33003] [Suggestion] `record.update(fields)` 允许调用方关键字覆盖保留字 `t`/`k`
  - 位置: src/shared/diagnostics/flash_diag.py:39-40
  - 证据:
    ```python
    record = {"t": round(now_ms(), 3), "k": kind}
    record.update(fields)
    ```
  - 影响: 任何调用方传 `t=...` 或 `k=...` 会覆盖时间戳/事件类型，破坏 JSONL 的可解析约定，且无报错。
  - 建议: `record.update({k2: v for k2, v in fields.items() if k2 not in ("t", "k")})` 或对保留字断言/剔除。
  - 置信度: 高（机制确定；当前调用方未传保留字，影响为防御性）

### 通过项
- 线程安全：`_LOCK` 串行化追加；POSIX `"a"` 模式底层 O_APPEND，行级追加原子性足够。
- 异常与取消路径：`default=str` 兜底不可序列化对象；启用判定 `_PATH` 导入期读取并文档化（须启动前设置），行为一致。
- 输入验证：`enabled()` 布尔判定直接短路，未启用时零成本，符合"常驻插桩"设计。
- 类型注解完整（`-> bool`/`-> float`/`-> None`，`Any` 字段）。
- GPU/GLSL·HDR/信号防递归/状态提交维度：不适用（纯文件诊断工具，无 Qt、无 OpenGL）。

---

## src/shared/diagnostics/performance_bundle.py (行数: 171)

### 发现

- [AUD-33010] [Major] 绝对路径正则漏掉 `/Volumes`、`/root`、UNC 路径，素材路径可未脱敏进入导出报告
  - 位置: src/shared/diagnostics/performance_bundle.py:44-46
  - 证据:
    ```python
    _ABS_PATH_RE = re.compile(
        r"(?:/Users/[^\s'\"]+|[A-Za-z]:\\[^\s'\"]+|/home/[^\s'\"]+)"
    )
    ```
  - 影响: 模块契约是"移除/遮蔽素材路径、工程路径、用户目录"（§16.3）。macOS 视频编辑器的素材常在外置卷 `/Volumes/<卷名>/...`，Linux root 运行时为 `/root/...`，Windows 网络盘为 `\\server\share\...`——这些路径出现在任何 telemetry/trace 字符串（含 error 消息）中都会原样写入"已脱敏"报告并随报告外发。
  - 建议: 正则补充 `/Volumes/`、`/root/`、`\\\\[^\\]+\\[^\\]+`（UNC），或改为更通用的"绝对路径启发式 + 白名单键"策略。
  - 置信度: 中（正则覆盖缺口确定；实际泄露取决于采集数据中是否含上述样式路径）

- [AUD-33011] [Suggestion] `machine` 段绕过 `sanitize_value`，脱敏契约仅靠外部函数自觉维持
  - 位置: src/shared/diagnostics/performance_bundle.py:134-151
  - 证据:
    ```python
    try:
        from tools.freeze_benchmark_media_manifest import machine_info
        machine = machine_info()
    except Exception:
        machine = {"platform": sys.platform, "python": sys.version.split()[0]}
    return {
        ...
        "machine": machine,
        ...
        "sections": sanitize_value(sections),
    }
    ```
  - 影响: `sections` 强制脱敏而 `machine` 直通。当前 `machine_info()`（tools/freeze_benchmark_media_manifest.py:196-213）仅含 platform/machine/python/git_commit，暂无泄露；但该函数一旦新增字段（如 hostname、用户名），将无声绕过本模块的脱敏保证。
  - 建议: `"machine": sanitize_value(machine)` 统一过闸，防纵深缺失。
  - 置信度: 高（绕过为事实；当前无实际泄露）

- [AUD-33012] [Suggestion] `export_performance_bundle` 不校验输出路径，且非原子写入可残留半截 JSON
  - 位置: src/shared/diagnostics/performance_bundle.py:158-166
  - 证据:
    ```python
    bundle = collect_performance_bundle(**kwargs)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(bundle, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    ```
  - 影响: `output_path=""` 时 `Path("") == Path(".")`，`write_text` 对目录抛 `IsADirectoryError` 直接进调用方（timeline_panel.py:2136 导出按钮路径）；写入中途失败（磁盘满/崩溃）会留下残缺 JSON，用户无法区分报告是否可信。
  - 建议: 入口校验非空且父目录可写；先写 `path.with_suffix(".tmp")` 再 `os.replace` 原子落盘。
  - 置信度: 高

- [AUD-33013] [Suggestion] 类型注解 `Optional[Any]` 恒等于 `Any`，无约束意义
  - 位置: src/shared/diagnostics/performance_bundle.py:71-74
  - 证据:
    ```python
    trace_collector: Optional[Any] = None,
    engine: Optional[Any] = None,
    gpu_renderer: Optional[Any] = None,
    resource_sampler: Optional[Any] = None,
    ```
  - 影响: `Any` 已含 `None`，`Optional[Any]` 对类型检查器与阅读者都不提供信息，掩盖了"期望 FrameTraceCollector 等鸭子类型"的意图。
  - 建议: 定义 `Protocol`（如 `HasSummary`、`HasStats`）或至少注释期望属性；`Optional` 保留但内层用具体协议。
  - 置信度: 高

### 通过项
- 异常与取消路径：每个 section 独立 try/except 并把 `str(e)` 写入 `{"error": ...}`，失败不伤其他段与业务，符合"诊断不伤业务"契约；`error` 字符串随 sections 最终过 `_ABS_PATH_RE`，错误消息中的家目录路径也会被遮蔽。
- 输入验证：`sanitize_value` 深度上限 12 防 self-referential 结构；`_REDACT_KEYS` 键名小写归一匹配；`pixel_copy_counters` 惰性导入且包在 try 内，ImportError 有兜底；`tools.` 导入失败降级最小 machine 信息。
- 状态提交一致性 / GPU 资源生命周期 / GLSL·HDR / 信号防递归：不适用（纯数据汇总导出，无 undo/持久化、无 OpenGL、无 Qt 信号）。
- 性能模式：导出为一次性用户触发动作，无热路径问题。

---

## src/shared/diagnostics/resource_snapshot.py (行数: 280)

### 发现

- [AUD-33020] [Major] `cpu_percent` 每次新建 `psutil.Process()`，首调用恒返回 0.0，指标永远为零
  - 位置: src/shared/diagnostics/resource_snapshot.py:186-195
  - 证据:
    ```python
    import psutil

    proc = psutil.Process()
    mem = proc.memory_info()
    stats["rss_bytes"] = int(mem.rss)
    stats["cpu_percent"] = proc.cpu_percent(None)
    ```
  - 影响: psutil 的 `cpu_percent(interval=None)` 基于实例内上次调用做差分，首次调用返回 0.0。`_system_stats` 每次采样都新建 Process 实例，等于每次都是"首次调用"，导出的 CPU% 永远是 0.0——资源快照的核心进程指标静默失真，无法据此定位 CPU 饱和。
  - 建议: 模块级或实例级缓存 `psutil.Process()` 单例（进程不退出时 pid 稳定，`NoSuchProcess` 时重建），或改用 `proc.cpu_percent(interval=0.0)` + 实例复用。
  - 置信度: 高

- [AUD-33021] [Major] 注册表对 provider 持强引用且生产代码从不注销，渲染器对象图泄漏 + 快照调用已关闭对象
  - 位置: src/shared/diagnostics/resource_snapshot.py:72-78
  - 证据:
    ```python
    def register(
        self, category: str, name: str, provider: StatsProvider
    ) -> None:
        if category not in self._providers:
            raise ValueError(f"unknown resource category: {category!r}")
        with self._lock:
            self._providers[category][name] = provider
    ```
  - 影响: 全局单例 registry 以固定名（`edit_present_pool`/`edit_gpu_pipeline`/`edit_gpu_frame_pool`）持有绑定方法强引用（调用方 src/features/edit/core/timeline_preview_renderer.py:6681-6696 注册）。全仓库 grep 确认生产代码无任何 `registry.unregister`/`unregister_owner` 调用（仅测试使用）：工程/页面关闭后最后一个 TimelinePreviewRenderer 及其 GPU renderer、frame pool 整个对象图被 registry 钉住无法回收（内存泄漏）；且后续 `collect()` 会对已 shutdown 的对象继续调用 stats provider，读到的是死对象上的陈旧/异常数据。
  - 建议: registry 内部改存 `weakref.WeakMethod`（死亡自动摘除），或注册方在 teardown 路径调用已有的 `unregister_owner("edit_")`；`collect()` 对失效 weakref 跳过。
  - 置信度: 高（未注销经 grep 验证；强引用钉住对象为确定性语义）

- [AUD-33022] [Major] `stop()` join 超时后无条件置空线程引用，重启会产生双采样线程且僵尸线程永不退出
  - 位置: src/shared/diagnostics/resource_snapshot.py:138-151
  - 证据:
    ```python
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="ResourceSnapshotSampler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
    ```
  - 影响: 若某个 provider 阻塞超过 2s（契约要求 provider 快速，但无任何超时强制；provider 内部自持业务锁被卡住即可触发），`join` 超时后仍把 `self._thread` 置 None。此后再 `start()` 会新建第二个线程，且 `self._stop.clear()` 把停止事件清除——旧线程从 `wait` 醒来发现 stop 已清，继续无限采样：两个线程双倍频率采样，ring buffer 数据交错、线程泄漏，且再无句柄能停掉旧线程。另外 `start()` 的检查-创建非原子，并发调用同样可建双线程。
  - 建议: join 后检查 `is_alive()`，存活则告警并保留引用（或强制新建 Event 按线程绑定）；`start()` 全程持锁；Event 改为每次 start 新建而非复用 clear。
  - 置信度: 中（缺陷机制确定；触发需 provider 阻塞 >2s 或并发 start，当前 src 内该类仅测试使用）

### 通过项
- 线程安全（registry）：`register/unregister/unregister_owner` 持锁；`collect()` 先在锁内浅拷贝 provider 表、锁外调用 provider，避免快照线程持注册表锁等业务锁，符合模块约束。
- 异常与取消路径：provider 异常按 `{"error": str}` 逐条隔离，不影响其他类别；`_run` 捕获采样异常仅 debug 日志、线程不死循环退出；`_system_stats` 对 `/proc/self/task`（OSError）、psutil 缺失（ImportError）、`num_fds` 平台差异（AttributeError/AccessDenied）均有兜底。
- 输入验证：`interval_s` 下限 0.1、`max_samples` 下限 10；未知 category 注册直接 ValueError。
- 线程安全（采样数据）：`_samples` deque 追加与读取、`_peaks` 读写均持 `self._lock`；`get_resource_registry()` 双检锁为 CPython 下标准安全写法。
- 性能模式：`summary()` 先取 `rss[-1]` 再排序（注释明确，last/max 语义正确）。
- 类型注解完整（`StatsProvider` 别名、`Deque[dict]`、`Optional` 使用规范）。
- GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交一致性：不适用（本模块仅聚合只读 stats，不触碰 OpenGL/Qt/undo 状态）。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 4 | AUD-33010, AUD-33020, AUD-33021, AUD-33022 |
| Suggestion | 6 | AUD-33001, AUD-33002, AUD-33003, AUD-33011, AUD-33012, AUD-33013 |
