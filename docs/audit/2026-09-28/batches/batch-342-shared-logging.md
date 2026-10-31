# batch-342 审查报告：src/shared/logging（AUD-34200–AUD-34299）

审查人维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失。

## src/shared/logging/__init__.py (行数: 3)

### 发现

（无）

### 通过项
- 通过（无逻辑）：纯 re-export，导出 `LogManager`，`__all__ = ["LogManager"]` 与兄弟包风格一致。

## src/shared/logging/log_manager.py (行数: 119)

### 发现

- [AUD-34201] [Major] 裸文件名 log_file_path 使 os.makedirs("") 抛 FileNotFoundError，文件 handler 建立全程无异常防护，可致启动/偏好变更崩溃
  - 位置: src/shared/logging/log_manager.py:84（关联 89-91）
  - 证据:
    ```python
            log_path = (cfg.get("log_file_path") if cfg else None) or ""
            if not log_path:
                from src.shared.config.config_manager import ConfigManager  # [R2-5] 平台规范目录
                log_path = os.path.join(ConfigManager.get_platform_data_dir(), "logs", "topos.log")

            os.makedirs(os.path.dirname(log_path), exist_ok=True)
    ```
  - 影响: log_file_path 属用户可配置的外部输入（ConfigManager 默认值为 `""`，见 config_manager.py:238）。若用户在偏好中填相对裸文件名（如 `topos.log`），`os.path.dirname` 返回 `""`，`os.makedirs("", exist_ok=True)` 确定性地抛 `FileNotFoundError`；路径不可写/磁盘满时 `makedirs` 或 `RotatingFileHandler` 构造抛出的 `OSError` 同样未捕获，沿 `_add_file_handler → _setup → initialize/reconfigure` 上抛——前者使应用启动崩溃，后者在用户修改 Preferences 后崩溃，且此时 root logger 已被 clear、配置处于半初始化状态。
  - 建议: 对 log_path 先 `os.path.abspath` 归一化并对 `dirname` 判空；用 `try/except OSError` 包住整个文件 handler 建立流程，失败时降级为仅控制台输出并 `logger.warning`，不让异常逃逸到启动/偏好路径。
  - 置信度: 高

- [AUD-34202] [Suggestion] max_log_file_size_mb 未做类型验证，字符串值将触发字符串乘法/TypeError
  - 位置: src/shared/logging/log_manager.py:86-87
  - 证据:
    ```python
            max_size = (cfg.get("max_log_file_size_mb") if cfg else None) or 10
            max_bytes = max_size * 1024 * 1024  # MB → bytes
    ```
  - 影响: config.json 为外部数据（可被手工编辑）。`ConfigManager.get` 原样返回 `self.config.get(key)`（config_manager.py:647-648）不做类型转换。若值被写成 `"10"`，`"10" * 1024 * 1024` 生成约 2MB 字符串并传入 `RotatingFileHandler(maxBytes=...)`，后续 rollover 判断 `stream.tell() >= maxBytes` 抛 `TypeError`；若为 `"10.5"` 则乘法直接抛 `TypeError`。
  - 建议: `max_size = int(float(cfg.get("max_log_file_size_mb") or 10))`，并对 `<= 0` 回退默认值 10。
  - 置信度: 中（JSON 正常往返保持 int 类型，仅手工编辑触发；缺陷代码路径本身确凿）

- [AUD-34203] [Suggestion] root.handlers.clear() 不关闭旧 handler，重复 initialize 泄漏文件句柄，且 clear 与并发 emit 存在竞态窗口
  - 位置: src/shared/logging/log_manager.py:58-68
  - 证据:
    ```python
            root = logging.getLogger()
            root.setLevel(self._log_level)
            root.handlers.clear()
    ```
  - 影响: 二次调用 `initialize()` 时旧 `RotatingFileHandler` 被 clear 但未 `close()`，`self._file_handler` 在 89 行被直接覆盖，文件描述符泄漏。另有一个小竞态窗口：渲染线程正在 `callHandlers` 中迭代 `root.handlers` 时（logging 模块迭代本身不加锁）另一线程执行 `clear()`，理论上可漏发日志或抛 RuntimeError。`reconfigure()` 路径因先调用 `_remove_file_handler()` 正确关闭文件 handler，风险集中在重复 initialize 与并发窗口。
  - 建议: clear 前对现存 handler 逐一 `close()`；`initialize()` 加幂等保护（已有实例先 teardown 再重建）；在文档/调用约定中标注 `reconfigure` 仅在主线程调用。
  - 置信度: 中

- [AUD-34204] [Suggestion] 默认日志路径解析逻辑在 _add_file_handler 与 get_log_file_path 中重复
  - 位置: src/shared/logging/log_manager.py:79-82、115-118
  - 证据:
    ```python
            log_path = (cfg.get("log_file_path") if cfg else None) or ""
            if not log_path:
                from src.shared.config.config_manager import ConfigManager  # [R2-5] 平台规范目录
                log_path = os.path.join(ConfigManager.get_platform_data_dir(), "logs", "topos.log")
    ```
    （115-118 行为逐字相同的另一份）
  - 影响: 默认路径（目录名/文件名）改动需同步两处，属复制粘贴式重复，易漂移导致 UI 显示路径与实际写入路径不一致。
  - 建议: 提取 `_resolve_log_path() -> str` 私有方法供两处共用。
  - 置信度: 高

- [AUD-34205] [Suggestion] config_manager 参数缺少类型注解
  - 位置: src/shared/logging/log_manager.py:25、32
  - 证据:
    ```python
        def __init__(self, config_manager=None) -> None:
        ...
        def initialize(cls, config_manager=None) -> 'LogManager':
    ```
  - 影响: 类型注解缺失维度。调用方无法从签名获知 config_manager 需满足的协议（需有 `.get(key)` 方法）。
  - 建议: 用 `TYPE_CHECKING` 前向引用 `Optional['ConfigManager']`，或定义只含 `get` 的 `Protocol` 以避免循环导入。
  - 置信度: 高

### 通过项
- 线程安全：logging 模块自身 emit 有锁保护；本类仅在启动与偏好变更时调用，无每帧热路径，无共享可变竞态（仅 34203 所述换 handler 的窄窗口）。
- 异常路径：`_remove_file_handler` 对 None 正确判空，remove + close + 置 None 顺序正确；level 解析 `level_map.get(level_str, logging.WARNING)` 对未知字符串安全回退。
- GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性：不涉及（纯日志配置模块，无 Qt 控件、无 OpenGL、无 undo 状态）。
- 性能模式：单次配置，无性能敏感路径；控制台/文件 formatter 均为轻量格式串。
