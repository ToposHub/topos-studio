# batch-216 审查报告 [P1] src/products//

编号区间：AUD-21600–AUD-21699。审查日期：2026-09-28。
本批次 6 个文件均已完整 Read（共 426 行），并交叉核对了
`src/shared/feature_flags.py`、`src/app/module_loader.py`、
`tests/architecture/test_product_presets.py`、`scripts/verify_product_bundle.py`。

本包为纯配置/入口层：无 GLSL、无 GPU 资源、无 UI 控件、无信号连接、
无 undo/redo 状态提交，相关维度按构造不适用（在通过项中注明）。

---

## src/products/__init__.py (行数: 167)

### 发现

- [AUD-21600] [Suggestion] `TOPOS_DISABLE_FEATURES` 中的未知特性名被静默忽略，与 `get_current_product()` 的告警行为不一致
  - 位置: src/products/__init__.py:160-165
  - 证据:
    ```python
    disabled_str = os.environ.get("TOPOS_DISABLE_FEATURES", "")
    if disabled_str:
        for name in disabled_str.split(","):
            name = name.strip().lower()
            if name in ALL_FEATURES and name not in ALWAYS_ENABLED:
                enabled.discard(name)
    ```
  - 影响: 外部输入（环境变量）中的拼写错误（如 `edt`、`composit`）不产生任何日志即被丢弃，操作者会误以为特性已禁用。同文件 `get_current_product()` 对未知 `TOPOS_PRODUCT_MODE` 会 `logger.warning`（139-142 行），两处输入验证策略不一致；测试也未覆盖未知名分支。
  - 建议: 对不满足 `name in ALL_FEATURES` 的非空名字增加 `logger.warning`，与 `get_current_product()` 对齐。
  - 置信度: 高

- [AUD-21601] [Major] 与 `src/shared/feature_flags.py` 双轨解析同一环境变量，语义已实际分叉
  - 位置: src/products/__init__.py:146-167（`get_enabled_features`）对照 src/shared/feature_flags.py:195-197、257-271
  - 证据:
    ```python
    # src/products/__init__.py:163-165（小写化；library 不可禁用）
    name = name.strip().lower()
    if name in ALL_FEATURES and name not in ALWAYS_ENABLED:
        enabled.discard(name)

    # src/shared/feature_flags.py:268-271（不小写化；library 可被置 False）
    for name in disabled_str.split(","):
        name = name.strip()
        if name in flags:
            flags[name] = False
    ```
  - 影响: 两个模块独立读取 `TOPOS_PRODUCT_MODE` / `TOPOS_DISABLE_FEATURES` 并各自维护预设表（`PRODUCT_PRESETS` vs `PRODUCT_MODE_PRESETS`），且规则已分叉：`TOPOS_DISABLE_FEATURES=library` 时 feature_flags 运行时会禁用 library，而本模块仍返回启用；未知名/未知模式的告警与回退策略也不同。产品注册、打包（经 `get_packaging_config`/`verify_product_bundle.py`）与运行时门控可能对同一环境得出不同结论；新增特性或新产品只改一边即静默漂移。当前 `get_enabled_features()` 在 src/ 运行时代码中无调用方（仅测试使用），故实际影响暂有限，但漂移面明确存在。
  - 建议: 收敛为单一解析入口（本模块复用 `feature_flags` 的解析结果，或反之提取共享解析函数），并为两套预设表加交叉一致性测试（同一 `TOPOS_PRODUCT_MODE` 下两模块结论必须一致）。
  - 置信度: 高（分叉事实经两边源码核对；实际运行影响为中）

- [AUD-21602] [Suggestion] `from typing import FrozenSet` 未使用
  - 位置: src/products/__init__.py:18
  - 证据:
    ```python
    from typing import FrozenSet
    ```
    （全文返回值/字段均使用内建 `frozenset`，`FrozenSet` 无任何引用。）
  - 影响: 死导入，徒增 lint 噪音。
  - 建议: 删除该导入。
  - 置信度: 高

- [AUD-21603] [Suggestion] dataclass 字段用未参数化的裸 `frozenset` 标注，与本文件 `dict[str, ProductPreset]` 风格不一致
  - 位置: src/products/__init__.py:52-53
  - 证据:
    ```python
    enabled_features: frozenset = field(default_factory=frozenset)
    disabled_features: frozenset = field(default_factory=frozenset)
    ```
  - 影响: 类型信息不完整（元素类型丢失），同文件 71 行已使用 `dict[str, ProductPreset]`（PEP 585），风格混杂，降低静态检查收益。
  - 建议: 改为 `frozenset[str]`。
  - 置信度: 高

### 通过项
- 线程安全：纯同步无状态查询（dataclass frozen、frozenset 不可变），无共享可变状态
- GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归：不涉及（纯配置层，无 OpenGL/控件/信号）
- 状态提交一致性：不涉及 undo/redo；`get_current_product()` 被 `src/shared/project/topos_project_io.py:77-79` 用于持久化产品名，回退值确定（DEFAULT_PRODUCT）
- 异常与取消路径：`get_current_product()` 对未设置/空白/未知值均有确定回退并告警，无裸 except
- 输入验证（产品模式部分）：`TOPOS_PRODUCT_MODE` 经 strip/lower、全名与短名映射、未知回退告警，验证充分（缺禁用列表告警，见 AUD-21600）
- 性能模式：环境变量每次现读，无缓存失效问题；调用频率低，无性能风险
- 预设一致性：`PRODUCT_PRESETS` 与 `src/shared/feature_flags.PRODUCT_MODE_PRESETS` 当前逐特性一致（经逐项比对：studio 全开；color/edit/composite 的 edit、composite、teaching_animation 开关一致）；`ALL_FEATURES` 键与 feature_flags.py:8-42 键名一致

---

## src/products/packaging.py (行数: 104)

### 发现

- [AUD-21604] [Suggestion] `_PLUGIN_MAP.get(product_name, [])` 静默兜底，新增产品漏配映射时不报错
  - 位置: src/products/packaging.py:95
  - 证据:
    ```python
    include_plugins = _PLUGIN_MAP.get(product_name, [])
    ```
  - 影响: `product_name` 已在 71-75 行通过 `PRODUCT_PRESETS` 校验，但若未来向 `PRODUCT_PRESETS` 新增产品而忘记同步 `_PLUGIN_MAP`（两张表键需人工保持一致），打包配置会静默返回“无插件”，复合插件在发行版中悄悄缺失，无任何告警。
  - 建议: 校验后直接使用 `_PLUGIN_MAP[product_name]`（缺项时尽早 KeyError），或在 ValueError 分支中同时校验两张表键集一致。
  - 置信度: 高

- [AUD-21605] [Suggestion] `include_locales` 实际返回特性 ID 而非文档所述的“locale 文件前缀”
  - 位置: src/products/packaging.py:66（docstring）与 91-93（实现）
  - 证据:
    ```python
    include_locales: list of locale file prefixes.   # docstring, 第 66 行

    # Locales: all products ship all locale files (they are shared).
    # The include_locales field documents which feature-related keys are active.
    include_locales = sorted(enabled)                # 第 91-93 行
    ```
  - 影响: 字段语义与 docstring 矛盾：消费者（如 `scripts/verify_product_bundle.py:81` 及未来打包脚本）若按“locale 前缀”使用该列表会得到特性名（如 `["color", "deliver", "library"]`），派生错误文件清单。
  - 建议: 要么按真实 locale 前缀计算，要么将字段改名为 `include_locale_scopes` 并修正 docstring，明确其“仅信息性”。
  - 置信度: 高

### 通过项
- 线程安全：纯函数，无共享可变状态
- GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归：不涉及
- 状态提交一致性：构建期静态配置，不涉及 undo/持久化
- 异常路径：未知产品名抛出带可用列表的 `ValueError`（71-75 行），符合“外部数据须验证”
- 目录映射正确性：`_FEATURE_TO_DIR` 覆盖 `ALL_FEATURES` 全部 6 个键；library 与 color 共享 "color" 目录且 library 恒启用，不存在“禁用特性误伤启用目录”的冲突（对 4 个 preset 逐一核对）
- 性能模式：集合推导 + sorted，数据量为个位数，无风险

---

## src/products/topos_color.py (行数: 39)

### 发现

（无）

### 通过项
- 通过（薄入口，无逻辑风险）：`get_preset()` 直接返回包内常量 `PRODUCT_PRESETS["topos-color"]`（键在同一包 `__init__.py:79-85` 定义，不可能 KeyError）；`register()` 委托 `src/app/module_loader.py:155 register_preset_modules(registry, preset)`（签名经核对一致），函数内延迟导入避免循环依赖
- 线程安全 / GPU 资源 / GLSL·HDR / 信号防递归 / 状态提交：不涉及
- 类型注解：`get_preset() -> ProductPreset`、`register(registry: "FeatureRegistry") -> None` 完整；`FeatureRegistry` 仅 TYPE_CHECKING 导入，运行时零依赖
- 异常路径：无吞异常，无裸 except

---

## src/products/topos_composite.py (行数: 40)

### 发现

（无）

### 通过项
- 通过（薄入口，无逻辑风险）：`get_preset()` 返回 `PRODUCT_PRESETS["topos-composite"]`（键在 `__init__.py:93-102` 定义）；`register()` 委托 `register_preset_modules`，延迟导入防循环依赖
- 线程安全 / GPU 资源 / GLSL·HDR / 信号防递归 / 状态提交：不涉及
- 类型注解：完整（同上）
- 异常路径：无吞异常

---

## src/products/topos_edit.py (行数: 40)

### 发现

（无）

### 通过项
- 通过（薄入口，无逻辑风险）：`get_preset()` 返回 `PRODUCT_PRESETS["topos-edit"]`（键在 `__init__.py:86-92` 定义）；`register()` 委托 `register_preset_modules`，延迟导入防循环依赖
- 线程安全 / GPU 资源 / GLSL·HDR / 信号防递归 / 状态提交：不涉及
- 类型注解：完整（同上）
- 异常路径：无吞异常

---

## src/products/topos_studio.py (行数: 36)

### 发现

（无）

### 通过项
- 通过（薄入口，无逻辑风险）：`get_preset()` 返回 `PRODUCT_PRESETS["topos-studio"]`（键在 `__init__.py:72-78` 定义）；`register()` 委托 `register_preset_modules`，延迟导入防循环依赖
- 线程安全 / GPU 资源 / GLSL·HDR / 信号防递归 / 状态提交：不涉及
- 类型注解：完整（同上）
- 异常路径：无吞异常

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 1 | AUD-21601 |
| Suggestion | 5 | AUD-21600, AUD-21602, AUD-21603, AUD-21604, AUD-21605 |

本包整体为低风险配置/入口层：4 个产品入口是纯委托薄壳；核心风险集中在
`__init__.py` 与 `feature_flags.py` 对同一环境变量的双轨解析（AUD-21601），
其余为输入验证告警缺失与文档/类型层面的质量问题。
