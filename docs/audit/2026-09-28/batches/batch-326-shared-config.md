# batch-326 [P2] src/shared/config//

任务书：docs/audit/2026-09-28/assignments/batch-326.md（编号区间 AUD-32600–AUD-32699）
审查方式：Read 通读全部文件；对 src/ 全程只读；交叉验证调用方均为只读 grep/sed。

---

## src/shared/config/__init__.py (行数: 3)

### 发现

（无）

### 通过项
- 通过（无逻辑）：纯 re-export，`from src.shared.config.config_manager import ConfigManager`，`__all__ = ["ConfigManager"]`，无其他副作用。

---

## src/shared/config/config_manager.py (行数: 662)

### 发现

- [AUD-32601] [Critical] `get()` 不支持 `default` 参数，多处两参调用抛 TypeError，被 except 吞掉后偏好静默失效（含 HDR 预览 tonemap 与色彩引擎选择）
  - 位置: src/shared/config/config_manager.py:647-648
  - 证据:
    ```python
    def get(self, key):
        return self.config.get(key)
    ```
    而仓库内多个活跃调用点按 `get(key, default)` 约定传两参（`self.config_manager` 确为 ConfigManager 实例，见 main_window.py:355）：
    - src/app/controllers/page_controller.py:863-864
      ```python
      tonemap = int(self.config_manager.get("color.preview.hdr_tonemap", 1) or 0)
      target_peak = float(self.config_manager.get("color.preview.hdr_target_peak", 100) or 100)
      ```
    - src/shared/color/color_space_descriptor.py:354（`descriptor_from_config` 内 `_cfg`，page_controller.py:833 传入 ConfigManager）
      ```python
      try:
          return config_manager.get(key, default)
      except Exception:
          return default
      ```
    - src/shared/color/color_transform_provider.py:795-796：`config_manager.get("composite.color_engine", "ocio")`、`config_manager.get("ocio_config_path", "")`
    - src/features/color/core/color_manager.py:445：`self.config_manager.get('default_color_space', 'sRGB')`（main_window.py:374 以 `ColorManager(self.config_manager)` 注入；该两参调用未包 try，一旦被调用即向上抛 TypeError）
    - src/features/deliver/ui/detachable_panel_host.py:228：`config_manager.get("dock_video_batch_export_mode", "embedded")`
  - 影响: `ConfigManager.get(key, default)` 立即抛 `TypeError: get() takes 2 positional arguments but 3 were given`。page_controller 的 `_apply_composite_preview_color_space` 因 863 行两参调用在外层 `except Exception`（879-884 行，仅 debug 级日志）中止，`set_display_space`/tonemap 永不执行——HDR（PQ/HLG）源在 Composite 预览中不做 tonemap、用户自定义 preview 色彩空间不生效；color_space_descriptor/color_transform_provider 中用户偏好被 except 吞掉后静默回退默认值（如 `composite.color_engine`/`ocio_config_path` 偏好被忽略）；未包 try 的调用点（color_manager.get_default_color_space）则直接崩溃。composite_viewport.py:9188-9196 甚至专门包了一层 `_Cfg` 适配类来绕开此缺陷，佐证 API 不匹配是实际存在的问题。
  - 建议: 将签名改为 `def get(self, key, default=None): return self.config.get(key, default)`，与仓库内 dict-like 调用约定对齐；顺带为 `get`/`set` 补充类型注解。
  - 置信度: 高

- [AUD-32602] [Major] `load_config` 对非 JSONDecodeError 的加载异常不备份，直接用默认配置覆盖原用户配置文件
  - 位置: src/shared/config/config_manager.py:383-390
  - 证据:
    ```python
                except json.JSONDecodeError as e:
                    self._backup_corrupted_config(e)
                except Exception as e:
                    logger.error(f"loading config: {e}")

            config = self.DEFAULT_CONFIG.copy()
            self._save_config_dict(config)
            return config
    ```
  - 影响: 文件损坏但不是 `json.JSONDecodeError` 的场景（如编码错误 UnicodeDecodeError、Windows 共享锁/瞬时 PermissionError、或顶层 JSON 为数组/字符串时 `{**self.DEFAULT_CONFIG, **loaded_config}` 抛 TypeError）都会落入宽泛 `except Exception`，随后 `_save_config_dict` 用默认值直接原子替换原文件——用户的全部偏好（含 recent_files、page_layouts、Tangent 校准）无 `.corrupted` 备份地丢失；瞬时性错误（读取被占用）也会触发覆盖。同时违反"不得宽泛 except 掩盖领域错误"的约束。
  - 建议: 对所有加载失败路径统一走 `_backup_corrupted_config`（把原文件复制为 `.corrupted`/带时间戳副本）后再回写默认值；瞬时性 OSError 可先重试或保留原文件、仅在内存回退默认。
  - 置信度: 高

- [AUD-32603] [Major] `_save_config_dict` 吞掉所有保存异常，`save_config` 仍清空 `_dirty_keys`，待存偏好在持久化失败时静默丢失
  - 位置: src/shared/config/config_manager.py:613-614, 636
  - 证据:
    ```python
                except Exception as e:
                    logger.error(f"saving config: {e}")

    def save_config(self):
        ...
            self.config = {**self.DEFAULT_CONFIG, **disk_config}
            self._save_config_dict(self.config)
            self._dirty_keys.clear()
    ```
  - 影响: 磁盘满、目录只读、权限被收等持久性失败只留一条 error 日志；`_dirty_keys.clear()` 无条件执行后，本轮 `set(..., auto_save=False)` 批量修改（preferences_dialog 确认流程 1932-1958 行大量使用该模式）全部丢失且无重试、无用户可见提示，内存态与磁盘持续不一致。
  - 建议: `_save_config_dict` 返回成功与否（或抛出），失败时保留 `_dirty_keys` 供下次 `save_config` 重试，并向上提供可订阅的失败信号供 UI 提示。
  - 置信度: 高

- [AUD-32604] [Major] `save_config` 空 dirty 路径用内存快照全量覆盖磁盘，多实例场景会回滚其他实例刚写入的配置键
  - 位置: src/shared/config/config_manager.py:616-636（关键 619-632）
  - 证据:
    ```python
            disk_config = self.DEFAULT_CONFIG.copy()
            if os.path.exists(self.config_path):
                try:
                    with open(self.config_path, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        disk_config.update(loaded)
                ...
            if self._dirty_keys:
                for key in self._dirty_keys:
                    disk_config[key] = self.config.get(key)
            else:
                disk_config.update(self.config)
    ```
  - 影响: 代码库中 ConfigManager 并非单例：main_window.py:355 持有长生命周期实例，而 timeline_panel.py:844 `ConfigManager().set(self._EDIT_MODE_CONFIG_KEY, mode)` 等处用临时实例直接写盘。长生命周期实例在自身最近一次保存之后、下一次 `save_config()`（`_dirty_keys` 为空的直接调用，如 `set(k, v)` 已自动保存后的显式 `save_config()`，见 file_controller.py:760、deliver_controller.py:168、library_panel.py:543）之间，磁盘新写入的键会被该实例的陈旧内存快照整体回滚（`disk_config.update(self.config)` 覆盖刚读到的磁盘值）。
  - 建议: 空 dirty 路径直接跳过写盘（内存与磁盘无差异则无需重写）；或统一收敛为单例/共享服务，避免多实例快照各自演化。
  - 置信度: 中（覆盖机制确证；触发依赖多实例写盘的时间窗口，未构造实际复现）

- [AUD-32605] [Major] `set()` 在锁外修改 `self.config` 与 `_dirty_keys`，与持锁迭代 `_dirty_keys` 的 `save_config()` 构成数据竞争
  - 位置: src/shared/config/config_manager.py:659-662（对照 629 的 `for key in self._dirty_keys:`）
  - 证据:
    ```python
    def set(self, key, value, auto_save=True):
        ...
        self.config[key] = value
        self._dirty_keys.add(key)
        if auto_save:
            self.save_config()
    ```
    而 `save_config` 在 `self._file_lock` 内执行 `for key in self._dirty_keys:`（629 行）；`_file_lock` 为类级 RLock（292 行），但 `set` 写共享状态发生在取锁之前。
  - 影响: 同一实例若被两个线程并发调用（ConfigManager 已在渲染线程被实例化使用：video_denoise_pipeline.py:30-36 的 `_cfg_bool` 在"渲染线程专用"的管线中新建实例），线程 A 在 629 行迭代集合时线程 B 执行 `_dirty_keys.add(key)` 可触发 `RuntimeError: Set changed size during iteration`；也可能把尚未完整提交的值写入半程状态。当前未定位到同一实例跨线程 `set` 的活跃调用点，属结构性隐患。
  - 建议: `set()` 全程持 `self._file_lock`（RLock 可重入，内部的 `save_config` 取锁无碍），或将内存态变更与持久化一并移入锁内。
  - 置信度: 中（竞争窗口真实存在；当前代码库内未发现同实例双线程触发的现役路径）

- [AUD-32606] [Suggestion] `DEFAULT_CONFIG` 全程浅拷贝，嵌套容器（features/backend/recent_files/page_layouts 等）与类属性共享同一对象
  - 位置: src/shared/config/config_manager.py:367, 388, 618, 634
  - 证据:
    ```python
                        config = {**self.DEFAULT_CONFIG, **loaded_config}
    ...
            config = self.DEFAULT_CONFIG.copy()
    ...
            disk_config = self.DEFAULT_CONFIG.copy()
    ...
            self.config = {**self.DEFAULT_CONFIG, **disk_config}
    ```
  - 影响: 当用户 config.json 缺失某嵌套键时，`self.config["features"]`/`["backend"]`/`["recent_files"]` 即 `ConfigManager.DEFAULT_CONFIG` 的类级对象；任何调用方对返回的 dict/list 就地修改都会污染类默认值并波及进程内所有实例与后续默认路径。当前审查到的主要调用方（dock_layout_controller 的 page_layouts、file_controller 的 recent_files）都先 `dict(...)`/`list(...)` 复制再写回，暂未实际污染，属潜在陷阱。
  - 建议: 加载/保存时对嵌套 dict/list 做 `copy.deepcopy`（或按 DEFAULT_CONFIG 的结构按需深拷贝一层），确保实例状态与类默认隔离。
  - 置信度: 高（别名机制确证；污染后果为潜在）

- [AUD-32607] [Suggestion] 公开方法缺少类型注解（`get`/`set`/`load_config`/`__init__` 参数）
  - 位置: src/shared/config/config_manager.py:294, 361, 647, 650
  - 证据:
    ```python
    def __init__(self, config_path=None):
    ...
    def load_config(self):
    ...
    def get(self, key):
    ...
    def set(self, key, value, auto_save=True):
    ```
  - 影响: 作为 shared 层被全库 200+ 处调用的公共 API，无注解削弱静态检查与 IDE 提示，也不利于锁定 `get` 的调用约定（与 AUD-32601 相关）。
  - 建议: 补充 `config_path: str | None`、`-> dict`、`get(self, key: str, default=None) -> Any`、`set(self, key: str, value: Any, auto_save: bool = True) -> None`。
  - 置信度: 高

- [AUD-32608] [Suggestion] 外部数据（config.json 值 / `set()` 传入值）缺乏类型与值域校验
  - 位置: src/shared/config/config_manager.py:361-390, 650-662
  - 证据:
    ```python
                        loaded_config = json.load(f)
                        config = {**self.DEFAULT_CONFIG, **loaded_config}
    ...
        self.config[key] = value
        self._dirty_keys.add(key)
    ```
  - 影响: 手工编辑或外部工具写入的 config.json 中类型错误值（如 `"undo_history_limit": "fifty"`、`"ui_scale": "big"`）原样进入运行时被消费；当前仅 backend（`_migrate_backend_config` 用 `BackendSelection.from_dict` 校验）、Tangent 版本、render_cache_codec、video_pipeline 四段有校验/迁移，其余键无保护，违反"外部数据须验证"的约束精神。
  - 建议: 加载时按 DEFAULT_CONFIG 的值类型做一次轻量类型对齐（不匹配则取默认并 warning）；`set()` 至少对已知数值/枚举键做值域检查。
  - 置信度: 中（无现役坏值案例，属防御缺口）

- [AUD-32609] [Suggestion] 每次 `set(auto_save=True)` 全量读盘+全量写盘+fsync；"临时 `ConfigManager()` 读一个键"的模式重复整文件解析
  - 位置: src/shared/config/config_manager.py:616-636, 591-614（使用侧证据：video_denoise_pipeline.py:30-36、edit_controller.py:5338-5350 等）
  - 证据:
    ```python
    def save_config(self):
        with self._file_lock:
            disk_config = self.DEFAULT_CONFIG.copy()
            if os.path.exists(self.config_path):
                ...json.load(f)...
    ...
                        f.flush()
                        os.fsync(f.fileno())
    ```
  - 影响: 每次 `set` 都执行一次磁盘读 + JSON 解析 + 临时文件写 + fsync；`_cfg_bool`/`_render_cache_auto_mode` 等处在读取侧新建 ConfigManager（`__init__` 即 `load_config` 全量读盘+解析+5 个迁移函数），若被频繁调用（偏好拖动、渲染线程标志刷新）会产生不必要的同步 I/O 与 UI/渲染线程卡顿风险。
  - 建议: 提供 mtime 缓存的读取路径（文件未变则复用解析结果），或收敛为进程级单例避免重复实例化；高频读取方注入共享实例。
  - 置信度: 中（机制确证；实际调用频率未逐点测量）

### 通过项
- GPU 资源生命周期：纯配置模块，不创建/持有任何 ModernGL/OpenGL 资源，无泄漏面。
- GLSL·HDR 正确性：不含着色器代码；HDR 相关键（color.preview.hdr_tonemap 等）仅作数据存取，无 clamp/字面量问题。
- 信号防递归：不涉及 Qt 信号/控件，无递归触发面。
- 原子写入实现：`_save_config_dict` 使用同目录 `tempfile.mkstemp` + `os.fsync` + `os.replace` 原子替换，finally 清理残留临时文件，写入崩溃安全性良好。
- 迁移一致性：五条迁移（video_pipeline / timeline_fps / render_cache_codec / backend / tangent）在 load 时按"仅变更时回写"执行，Tangent 迁移保留用户已改校准值，backend 迁移对非法值显式 warning 并回退 auto。
- 跨实例文件互斥：类级 `threading.RLock` 覆盖 load/save 的文件读写，RLock 可重入支持 load 内联保存。
- `__init__`/`get_platform_data_dir`/`get_platform_cache_dir` 的 TOPOS_CONFIG_PATH 覆盖与平台目录分支逻辑正确，Windows 环境变量缺失有回退。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-32601 |
| Major | 4 | AUD-32602, AUD-32603, AUD-32604, AUD-32605 |
| Suggestion | 4 | AUD-32606 – AUD-32609 |
