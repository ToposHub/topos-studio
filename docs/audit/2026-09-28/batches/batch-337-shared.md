# 批次报告：batch-337 [P2] src/shared/

编号区间：AUD-33700–AUD-33799
文件：src/shared/feature_flags.py (310 行)、src/shared/ffmpeg_locator.py (127 行)
统计：Critical 0 / Major 3 / Suggestion 1

---

## src/shared/feature_flags.py (行数: 310)

### 发现

- [AUD-33700] [Major] is_enabled 对未知 feature id 失败开放（fail-open），与 M0 冻结注释矛盾
  - 位置: src/shared/feature_flags.py:177-183
  - 证据:
    ```python
    def is_enabled(self, feature_id: str) -> bool:
        if feature_id == "color":
            return True
        # Registered AI capability flags have explicit False defaults; an
        # unknown id stays permissive (True) for backward compatibility, but
        # every AI surface is a registered field so it returns its real value.
        return getattr(self, feature_id, True)
    ```
  - 影响: 任何拼写错误或未注册的 capability id（如 `is_enabled("media_analysys")`、模块注册表里 `spec.feature_id` 与字段名不一致，见 src/app/module_loader.py:142）都返回 True，门控被静默打开。这与第 161-164 行注释"referencing an unknown capability must not surface AI analysis/generation/MCP without explicit opt-in"（M0 冻结契约）直接矛盾——安全开关走的是失败开放而非失败关闭。
  - 建议: 对未知 id 返回 False（fail-closed）并 `logger.warning`；至少对 `AI_CAPABILITY_FLAGS`/`COMMERCIAL_AUTOMATION_FLAGS` 前缀或集合内的未知 id 强制 False，普通 UI flag 可保留宽松行为。
  - 置信度: 中（行为本身确定；宽松默认是代码注释明示的取舍，但与 M0 注释矛盾，二文档必有其一是错的）

- [AUD-33701] [Major] 产品模式预设静默覆盖 config 中已显式开启的 AI 能力开关，违反模块级契约
  - 位置: src/shared/feature_flags.py:257-263（预设晚于 config 应用）；预设内 `**_ai_capability_defaults()`（78/87/98/107 行）；契约见 22-25 行注释
  - 证据:
    ```python
    # They are only enabled via an explicit TOPOS_FEATURE_* env var or an
    # explicit config entry.  (模块级契约, 行 23-25)
    ...
    # Step 3: Apply product mode preset (only when explicitly set, overrides config)
    product_mode_env = os.environ.get("TOPOS_PRODUCT_MODE")
    if product_mode_env is not None:
        product_mode = normalize_product_mode(product_mode_env)
        if product_mode in PRODUCT_MODE_PRESETS:
            for key, value in PRODUCT_MODE_PRESETS[product_mode].items():
                flags[key] = value
    ```
  - 影响: 用户在 config 的 `features.ai_assistant=true` 显式开启后，只要设置了 `TOPOS_PRODUCT_MODE`（打包发行版 launcher 的常态），预设里的 `**_ai_capability_defaults()` 会把所有 AI 能力强制回 False 且无任何日志。文档承诺的"explicit config entry"启用路径在打包场景下静默失效，用户只能靠 env var。方向上偏保守（不会误开），但属于文档化的功能路径失效。
  - 建议: 要么预设仅对未在 config 中显式出现的键生效（区分"显式设置"与"默认"），要么修正 22-25 行注释明确"产品模式设置时 config 无法开启 AI 能力，仅 TOPOS_FEATURE_* 有效"，并在覆盖时 `logger.info` 提示。
  - 置信度: 中（代码行为确定；由于函数级 docstring 写明了 preset > config 的优先级，可能是有意为之，但与模块级契约矛盾）

- [AUD-33702] [Suggestion] config 特性值用 bool() 强转，字符串 "false"/"0" 会被解析为 True
  - 位置: src/shared/feature_flags.py:253-255
  - 证据:
    ```python
    for key, value in config_features.items():
        if key in flags:
            flags[key] = bool(value)
    ```
  - 影响: 内部写入方（ai_preferences_page、config_manager 默认值）均写真实布尔，但手工编辑的配置文件若写 `"edit": "false"`，会被解释为开启。同一函数内 env var 解析（278 行 `env_value.lower() not in ("0", "false", "no", "disabled")`）却处理了字符串假值，两条路径语义不一致。
  - 建议: 抽取公共的 `_parse_flag(value)`（bool 直通；str 走小写假值集合），config 与 env 两条路径复用。
  - 置信度: 高（行为确定，实际触发需要手写配置，影响低）

### 通过项
- 线程安全：无共享可变状态，`DEFAULT_FEATURE_FLAGS.copy()` 后局部操作；`os.environ` 读取为 GIL 原子
- GPU 资源生命周期：不涉及
- GLSL·HDR 正确性：不涉及
- 信号防递归：不涉及（纯数据模块）
- 状态提交一致性：FeatureFlags 为 frozen dataclass 只读快照；config 写入路径在其他模块（本文件只读）
- 异常与取消路径：唯一 except 为窄化的 TypeError（213 行），无宽泛 except；`config.get` 均有 hasattr 守卫
- 输入验证：normalize_product_mode 处理 None/空白/大小写/别名；预设查找有 `in PRODUCT_MODE_PRESETS` 守卫；config dict 有 isinstance 检查（见 AUD-33702 的值语义问题）
- 性能模式：O(1) 小字典操作，无循环热点
- 类型注解：完整（`config: Any` 为有意的鸭子类型，可接受）

---

## src/shared/ffmpeg_locator.py (行数: 127)

### 发现

- [AUD-33703] [Major] _resolve_in_dir 未做可执行权限检查，不可执行的 bin/ffmpeg 会遮蔽 PATH 上的有效 ffmpeg
  - 位置: src/shared/ffmpeg_locator.py:71-76
  - 证据:
    ```python
    def _resolve_in_dir(directory: Path, exe: str) -> str | None:
        """在单个目录内解析二进制(带可执行权限检查,PATH 无关)。"""
        path = directory / exe
        if path.is_file():
            return str(path)
        return None
    ```
  - 影响: docstring 声称"带可执行权限检查"，实现只有 `is_file()`。macOS/Linux 下若 `bin/ffmpeg` 存在但无执行位（下载残缺、权限被剥离、Gatekeeper 隔离属性等），定位器会返回该路径并写入缓存（None 之外的永久结果），后续子进程启动报 PermissionError——而 PATH 上明明有可用的系统 ffmpeg 却因优先命中坏文件而永不回退（`shutil.which` 只在 result 为 None 时才走）。
  - 建议: 在 `path.is_file()` 之外增加 `os.access(path, os.X_OK)`（Windows 上可跳过），不满足则继续遍历候选目录；同时修正 docstring 与实现的一致性。
  - 置信度: 高（docstring 与实现不一致、遮蔽逻辑均为代码确证；触发需要 bin/ 下存在坏文件这一前提）

### 通过项
- 线程安全：`_cached` 字典读写在 GIL 下原子，最坏情况为并发重复解析，结果幂等；纯标准库、无 Qt 依赖，符合模块声明的任意线程导入约束
- GPU 资源生命周期：不涉及
- GLSL·HDR 正确性：不涉及
- 信号防递归：不涉及
- 状态提交一致性：不涉及（无 undo/redo；缓存有 clear_cache 供环境变化后重置，None 结果也缓存属文档化行为）
- 异常与取消路径：`Path.is_file()` 吞掉 OSError 按 False 处理，模块按设计"不抛异常、返回 None"，无宽泛 except
- 输入验证：候选目录来自固定来源（env/冻结属性/`__file__` 推导），`_exe_name` 按平台补后缀；调用方仅传 "ffmpeg"/"ffprobe" 固定字面量
- 性能模式：结果缓存命中 O(1)；首次最多遍历约 6-8 个候选目录的 stat 调用，开销可忽略
- 类型注解：完整（含 `str | None` 返回与 `from __future__ import annotations`）
