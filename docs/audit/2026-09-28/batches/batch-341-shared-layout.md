# 批次报告：batch-341 [P2] src/shared/layout//

编号区间：AUD-34100–AUD-34199。审查日期：2026-09-28。
文件：`src/shared/layout/__init__.py`、`src/shared/layout/profile.py`、`src/shared/layout/store.py`。
交叉验证引用：`src/app/controllers/dock_layout_controller.py`、`src/shared/feature_flags.py`、`src/shared/config/config_manager.py`、`src/features/color/module.py`。

汇总：Critical 0 / Major 3 / Suggestion 6。

---

## src/shared/layout/__init__.py (行数: 18)

### 发现

（无）

### 通过项
- 通过（无逻辑）：纯 re-export 模块，导出 `LayoutProfile`（来自 `src.shared.layout.profile`）与 `LayoutStore`（来自 `src.shared.layout.store`），`__all__` 与导出一致。

---

## src/shared/layout/profile.py (行数: 133)

### 发现

- [AUD-34100] [Major] from_dict 对外部配置数据仅部分校验，畸形 profile 条目可沿启动路径抛未捕获异常
  - 位置: src/shared/layout/profile.py:66-76
  - 证据:
    ```python
    return LayoutProfile(
        product_mode=data.get("product_mode", ""),
        page_id=data.get("page_id", ""),
        visible_panels=tuple(visible) if isinstance(visible, list) else tuple(),
        qt_state_hex=data.get("qt_state_hex", ""),
        floating_panels=data.get("floating_panels", {}),
        schema_version=data.get("schema_version", LAYOUT_PROFILE_SCHEMA_VERSION),
    )
    ```
    仅 `visible_panels` 做了 isinstance 校验；`qt_state_hex`/`floating_panels`/`schema_version` 直接取用。缺 `page_id`/`product_mode` 时由 `__post_init__` 抛 `ValueError`（profile.py:44-48）。调用链：`LayoutStore.get_profiles_for_mode`（store.py:481，无 try）→ `_load_layouts_from_store`（dock_layout_controller.py:1504-1514，无 try，且 `qt_state_hex.encode()` 对非 str 会 AttributeError）→ `initialize_page_layouts`（dock_layout_controller.py:1103，无 try）→ `main_window.py:561` 直接调用。
  - 影响: 违反"外部数据须验证"铁律。配置文件为外部 JSON：手改/半损坏的 `layout_profiles` 条目（合法 JSON 但缺字段或类型错误）会让 `from_dict` 抛 ValueError 或后续 `.encode()` 抛 AttributeError，且该路径在 MainWindow `__init__` 中未被捕获，可致应用启动失败。
  - 建议: `from_dict` 对各字段做类型校验，非法时返回 None 或跳过该字段默认值；`get_profiles_for_mode`/`get_profile` 对单条目用窄 try/except 记 warning 后跳过，防止单条坏数据阻断全部布局恢复；顺带为 `schema_version` 增加消费点（当前存取但从未检查，前向迁移钩子缺失）。
  - 置信度: 高（代码路径已核实；触发依赖配置损坏，概率中等）

- [AUD-34101] [Suggestion] frozen 不可变契约被 mutable dict 字段破坏：with_* 系列共享引用、实例不可哈希
  - 位置: src/shared/layout/profile.py:41, 99, 110, 123
  - 证据:
    ```python
    floating_panels: dict[str, dict[str, int]] = field(default_factory=dict)
    ...
    def with_qt_state(self, qt_state_hex: str) -> LayoutProfile:
        return LayoutProfile(
            ...
            floating_panels=self.floating_panels,
    ```
    `with_visible_panels`/`with_qt_state`/`with_floating_panels` 均原样传递 `self.floating_panels` 引用；`from_dict` 的 `data.get("floating_panels", {})` 亦不拷贝。
  - 影响: 类文档自称 "Immutable data class"，但新旧 profile 共享同一可变 dict，任一持有者原地修改会波及所有别名实例（含 LayoutStore 缓存中的副本）；`@dataclass(frozen=True)` 默认生成的 `__hash__` 以字段为基，dict 字段使 `hash(profile)` 必然 TypeError（当前无调用方，属潜在）。
  - 建议: `with_*` 与 `from_dict` 中对 `floating_panels` 做 `copy.deepcopy`（嵌套 {x,y,w,h} 亦共享）；或改用不可变结构并在文档注明，必要时 `eq=False` 或自定义 `__hash__`。
  - 置信度: 高（别名事实确凿；当前无触发点，风险为潜在）

### 通过项
- 类型注解完整（所有公开方法均有注解）。
- GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归：不涉及（纯数据类，无 Qt 信号、无 OpenGL）。
- `__post_init__` 对空 product_mode/page_id 抛 ValueError，键构造（make_key/config_key）一致性正确。
- `to_dict`/`from_dict` 正常数据下往返对称（to_dict 将 tuple 转 list，JSON 兼容）。

---

## src/shared/layout/store.py (行数: 593)

### 发现

- [AUD-34102] [Major] _panel_feature_enabled 将带点的 workspace.*/advanced.* 面板按特性前缀过滤，默认布局的工作区面板被全部剔除
  - 位置: src/shared/layout/store.py:293-317（对照 store.py:327-330、feature_flags.py:8-24）
  - 证据:
    ```python
    if "." in panel_id:
        feature_prefix = panel_id.split(".")[0]
        feature_map = { "color": "color", ... }
        feature_name = feature_map.get(feature_prefix, feature_prefix)
        return feature_name in enabled_features
    # Panels without feature prefix (e.g., workspace panels) are always enabled
    return True
    ```
    注释假设 workspace 面板"无点分隔"，但本文件 DEFAULT_PANELS 明确使用 `"workspace.viewport", "workspace.library", "workspace.layers"`（store.py:328-330），注册表亦有 `workspace.files`、`advanced.script`（src/features/color/module.py:270-528）。而 `enabled_features` 来自 `FeatureFlags.get_enabled_features()`，其键为 library/color/edit/composite/deliver/teaching_animation/ai_*（feature_flags.py:8-24、188-189），永不含 "workspace"/"advanced"。
  - 影响: `filter_panels`/`get_or_default` 一旦传入 enabled_features（dock_layout_controller.py:1506-1510、1556-1560 正是如此），所有 workspace.*（视口/媒体库/图层）与 advanced.* 面板都会从 visible_panels 中被错误过滤掉，与"workspace 面板始终启用"的设计意图相反。当前控制器只消费 qt_state_hex，缺陷暂为潜伏；一旦 visible_panels 被消费（该层正是为此设计），默认布局将丢失全部工作区面板。
  - 建议: 维护显式的"非特性前缀"白名单（如 {"workspace", "advanced"}）直接返回 True，或将前缀映射到真实特性集，并修正 docstring 与实现对不上的部分。
  - 置信度: 高（本文件内部注释与 DEFAULT_PANELS 自相矛盾，已核实 feature 集不含 workspace）

- [AUD-34103] [Major] migrate_legacy_page_layouts 仅识别扁平 {page: hex}，对应用实际写入的 mode-scoped 结构静默迁移 0 条
  - 位置: src/shared/layout/store.py:202, 210-211
  - 证据:
    ```python
    legacy = self._config.get(LEGACY_PAGE_LAYOUTS_KEY)
    ...
    for page_id, state_hex in legacy.items():
        if not isinstance(state_hex, str):
            continue
    ```
    而同一应用写入的 page_layouts 可为 mode-scoped：`saved_layouts[mode_key] = bucket`（dock_layout_controller.py:1694-1710，另见 1766、1864、1952），读取端也兼容两种形态（dock_layout_controller.py:1078-1095）。
  - 影响: 对 mode-scoped 配置（现代常见形态），每页的值是 dict 而非 str，`continue` 全部跳过，迁移永远返回 0——模块文档宣称的"legacy page_layouts migration"功能对这类配置静默失效，LayoutStore 得不到预置数据，仅靠后续 `_save_current_layout_to_store` 增量补写。
  - 建议: 迁移时向下递归一层 mode bucket（`{mode: {page: hex}}` → 以该 mode 为 product_mode 逐条迁移），与读取端 dock_layout_controller.py:1078-1095 的双形态判定保持一致。
  - 置信度: 高（两侧代码均已核实；对存量用户的实际影响面取决于配置形态分布，为中等）

- [AUD-34104] [Suggestion] list_profiles 首段循环为死代码（循环体 pass），_get_all_profiles_raw 结果未被使用
  - 位置: src/shared/layout/store.py:153-162
  - 证据:
    ```python
    all_profiles = self._get_all_profiles_raw()
    keys: list[str] = []

    for pm_key in all_profiles.values():
        for page_key in pm_key.values():
            if isinstance(page_key, dict):
                # Reconstruct key from the structure
                pass

    # Alternative: scan config for layout_profiles/* keys
    ```
  - 影响: 功能实际由第 163-173 行的第二段扫描正确实现，但首段死循环白白遍历数据且误导维护者（看似负责 key 重建）；第 164 行又直接 `self._config.get(...)` 重复读取，未复用 `all_profiles`。
  - 建议: 删除首段死代码与重复注释，直接复用 `_get_all_profiles_raw()` 的返回值遍历。
  - 置信度: 高

- [AUD-34105] [Suggestion] feature_map 为恒等映射，属死代码
  - 位置: src/shared/layout/store.py:307-314
  - 证据:
    ```python
    feature_map = {
        "color": "color",
        "edit": "edit",
        "composite": "composite",
        "deliver": "deliver",
        "library": "library",
    }
    feature_name = feature_map.get(feature_prefix, feature_prefix)
    ```
  - 影响: 五个键值完全相同，`.get(prefix, prefix)` 有无映射结果一致，制造"存在前缀→特性转换"的假象，增加维护噪音（真正需要的 workspace/advanced 特例反而缺失，见 AUD-34102）。
  - 建议: 删除该映射，改为显式的白名单/别名表并使其真正承担映射职责。
  - 置信度: 高

- [AUD-34106] [Suggestion] 迁移不清理 legacy page_layouts 键且每次启动重跑；用户重置布局后旧布局可被"复活"
  - 位置: src/shared/layout/store.py:188-239（对照 dock_layout_controller.py:1074、2020-2034）
  - 证据:
    ```python
    legacy = self._config.get(LEGACY_PAGE_LAYOUTS_KEY)
    ...
    for page_id, state_hex in legacy.items():
        ...
        existing = self.get_profile(product_mode, page_id)
        if existing is not None:
            ... continue
        profile = LayoutProfile(...)
        self.save_profile(profile)
    ```
    迁移后从不删除/标记 `page_layouts` 键；`initialize_page_layouts` 每次启动无条件调用迁移（dock_layout_controller.py:1074）；`reset_product_layout` 只删除 store profiles 与内存 page_layouts，不清除配置中的 legacy 键（dock_layout_controller.py:2020-2034）。
  - 影响: 扁平 legacy 配置下，用户执行"重置当前模式布局"并重启后，迁移发现该 mode 无 profile，会把仍留在配置中的 legacy 布局重新灌回，重置被静默撤销；legacy 数据永不过期也与"迁移"语义不符。
  - 建议: 迁移成功后删除或重命名 `page_layouts` 键（或写入已迁移标记），使迁移严格一次性；`reset_product_layout` 同步清除 legacy 键。
  - 置信度: 中（复活路径依赖扁平格式配置，逻辑链已核实）

- [AUD-34107] [Suggestion] delete_profile 返回值只反映缓存是否存在，未反映配置中是否真的删除
  - 位置: src/shared/layout/store.py:129-141
  - 证据:
    ```python
    key = LayoutProfile.make_key(product_mode, page_id)
    existed = key in self._cache

    self._cache.pop(key, None)

    if self._config is not None:
        self._delete_config_nested(key)
    ...
    return existed
    ```
  - 影响: 若目标 profile 存在于配置但从未被加载进缓存（如启动后未访问该页面），`_delete_config_nested` 实际删除了配置条目却返回 False，与 docstring "True if a profile was deleted" 不符；依赖返回值做提示/计数的调用方会误判（当前唯一调用方 dock_layout_controller.py:2028 忽略返回值，暂无实际故障）。
  - 建议: 以配置删除结果（`_delete_config_nested` 返回是否找到叶子）或 `get_profile` 探测结果作为 `existed` 依据。
  - 置信度: 高（语义偏差确定；当前无受影响调用方）

- [AUD-34108] [Suggestion] 缓存无失效机制：get_profile 永远优先缓存，clear_cache 无任何调用方
  - 位置: src/shared/layout/store.py:103-114, 588-590
  - 证据:
    ```python
    # Check in-memory cache first
    if key in self._cache:
        return self._cache[key]
    ...
    def clear_cache(self) -> None:
        """Clear the in-memory profile cache."""
        self._cache.clear()
    ```
    全库检索 `clear_cache` 调用点：LayoutStore.clear_cache 无外部调用（其余命中均属其他模块）。
  - 影响: 配置被外部途径修改（如设置导入/恢复默认后 ConfigManager 重新载入）时，store 仍返回陈旧 profile，恢复出已删除的布局，属状态提交一致性的潜在缺口。
  - 建议: 在配置 reload/导入路径上调用 clear_cache，或为缓存条目引入与 ConfigManager 变更联动的失效信号。
  - 置信度: 中（陈旧场景需配置被外部重载，代码事实确定）

### 通过项
- 线程安全：LayoutStore 仅由 dock_layout_controller 在 UI 线程创建与调用（dock_layout_controller.py:1072 起的全部调用点），无渲染线程访问，无锁但当前单线程使用成立。
- GPU 资源生命周期：不涉及 OpenGL/ModernGL 资源。
- GLSL·HDR 正确性：不涉及着色器与像素数据，无 clamp 相关问题。
- 信号防递归：纯数据层，无 Qt 信号连接。
- 异常路径：`_get_product_mode` 仅捕获窄 `ImportError` 并回退 "studio"（store.py:575-582），未用宽泛 except 掩盖领域错误，符合铁律。
- 内部配置辅助：`_get/_set/_delete_config_nested` 与 ConfigManager `get(key)/set(key, value, auto_save=...)` 签名匹配（config_manager.py:647-662）；`_set_config_nested` 的 root 获取-修改-回写序列正确；key 结构（"layout_profiles/pm/page"）读写一致。
- 性能模式：filter_panels/list_profiles 均为 O(n) 遍历，save_profile 单次写盘（auto_save），无热点循环。
- 类型注解：公开方法注解完整（`config: Any` 宽松但有 docstring 说明，可接受）。
- 跨产品模式隔离：get_profiles_for_mode/list_profiles 按 product_mode 正确分桶，缓存与配置合并逻辑无双计数。

---

## 审查维度覆盖说明

九个维度（线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失）均已逐文件检查；本批次为纯数据/持久化层，GPU、GLSL、信号维度以"不涉及"记录。所有发现均 Read 到真实行号并交叉验证调用方（dock_layout_controller.py、feature_flags.py、config_manager.py、features 各 module.py 注册表）。
