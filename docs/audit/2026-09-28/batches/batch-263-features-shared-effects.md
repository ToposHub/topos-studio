# 批次报告：batch-263 — src/features/shared/effects/

- 任务书：docs/audit/2026-09-28/assignments/batch-263.md
- 编号区间：AUD-26300–AUD-26399
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解
- 汇总：Critical 0 · Major 2 · Suggestion 11

---

## src/features/shared/effects/__init__.py (行数: 95)

纯 re-export 包入口（无逻辑）：导出 EffectCatalog/get_effect_catalog、effect_capability 的能力与诊断 API（含 resolve_execution_capability）、EffectSpec/ShaderKind、preview_policy 与 plugin_identity 的公共面。已核对 import 与 `__all__` 双向一致，无遗漏导入或悬空导出。

### 发现

- [AUD-26300] [Suggestion] `EffectExecutionCapability` 未随 `resolve_execution_capability` 一并 re-export
  - 位置: src/features/shared/effects/__init__.py:15-30, 55-95
  - 证据:
    ```python
    from src.features.shared.effects.effect_capability import (
        CPU_PREVIEW_EFFECT_TYPES,
        EXPORT_EFFECT_TYPES,
        GPU_EXPORT_EFFECT_TYPES,
        EffectCapability,
        EffectDiagnostic,
        ...
        resolve_execution_capability,
    )
    ```
    （`__all__` 含 `"resolve_execution_capability"`，但同为该模块定义、作为其返回类型的 `EffectExecutionCapability` 既未 import 也未列入 `__all__`。）
  - 影响: 调用方（如 timeline_export、delivery_capability）想对返回值做类型注解或访问 `to_dict()`/`delivery_restriction()` 时必须深入子模块路径导入，包公共面不完整，且未来若调整子模块布局会破坏这些深路径导入。
  - 建议: 在 import 块与 `__all__` 中补充 `EffectExecutionCapability`。
  - 置信度: 高

### 通过项

- 线程安全：无状态纯 re-export，无共享可变状态。
- GPU 资源生命周期：不涉及 GL 对象（符合包 docstring 声明的"只复用描述、不跨 context 共享 program/FBO"）。
- GLSL·HDR 正确性：无着色器内容。
- 信号防递归：无 Qt 信号。
- 状态提交一致性：不涉及 undo/redo/持久化。
- 异常与取消路径：导入期无吞异常逻辑。
- 输入验证：无外部数据入口。
- 性能模式：无运行时逻辑。
- 类型注解：无函数定义，N/A。

---

## src/features/shared/effects/dctl_wrapper.py (行数: 165)

### 发现

- [AUD-26301] [Suggestion] uniform 名 `u_{var_name}` 未做 GLSL 标识符/保留名冲突校验
  - 位置: src/features/shared/effects/dctl_wrapper.py:90-93, 118-124
  - 证据:
    ```python
        var = p.get("var_name", "")
        if not var:
            continue
        lines.append(f"uniform {gltype} u_{var};")
    ```
    以及生成模板头部：
    ```python
    return f"""#version 330
    uniform sampler2D u_texture;
    uniform vec2 u_resolution;
    ```
  - 影响: `var_name` 来自外部 `.tglsl` 文件（plugin_manager 的正则仅约束为 `\w+`，允许 `1radius` 这类以数字开头的非法 GLSL 标识符）。若插件参数名为 `texture` 或 `resolution`，会生成与模板头部重复/类型冲突的 `uniform float u_texture;` → 着色器编译失败；非法标识符同样编译失败。虽然最终会以 FX-M03 的 COMPILE_ERROR 徽章呈现而非静默，但报错信息远离根因，且违反"外部数据须验证"铁律。
  - 建议: 在 `_uniform_declarations` 中校验 `var` 匹配 `^[A-Za-z_]\w*$` 且 `u_{var}` 不在 `{u_texture, u_resolution}` 保留集中，不合法时跳过并 warning。
  - 置信度: 高（代码事实）；触发依赖插件作者命名，发生概率中。

- [AUD-26302] [Suggestion] `default_uniform_values` 文档声称由 EffectCatalog 使用，实际仅测试引用
  - 位置: src/features/shared/effects/dctl_wrapper.py:136-141
  - 证据:
    ```python
    def default_uniform_values(params: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Map each @param's default to the ``u_{var_name}`` uniform value.

        Used by EffectCatalog to seed ``EffectSpec.default_parameters()`` and
        by the runner's generic plugin packer when no per-effect value is set.
    ```
  - 影响: 全库 grep 确认 `default_uniform_values` 仅被 `tests/shared/test_dctl_and_preview.py` 引用；EffectCatalog 实际经由 `ParamDef` 默认值走 `EffectSpec.default_parameters()`，runner 打包器也未调用它。文档漂移会误导维护者以为存在这条数据流。
  - 建议: 修正 docstring（说明现仅测试/工具用途），或删除该死代码。
  - 置信度: 高

### 通过项

- 线程安全：纯字符串操作、无共享状态、无 GL 依赖（与 docstring 声明一致）。
- GPU 资源生命周期：不创建/持有 ModernGL 对象。
- GLSL·HDR 正确性：`fragColor = vec4(result, src.a)` 直通 alpha、结果无 clamp(0.0,1.0)，符合 HDR 铁律；未用的 `u_resolution` 声明由 runner 的 `_set_uniform`（KeyError 静默跳过）兜底，无副作用。
- 信号防递归：无 Qt 控件/信号。
- 状态提交一致性：不涉及。
- 异常与取消路径：无可取消操作；对 `default=None`/畸形 color default 有显式回退分支。
- 性能模式：单次字符串拼接，无循环开销问题。
- 类型注解：完整（`List[Dict[str, Any]]` 等）。

---

## src/features/shared/effects/effect_capability.py (行数: 574)

### 发现

- [AUD-26303] [Suggestion] 文件头 DORMANT 横幅"暂无生产入口"与实际生产接线矛盾
  - 位置: src/features/shared/effects/effect_capability.py:1-2
  - 证据:
    ```python
    # DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
    # 接线前请勿在生产依赖；若半年内无接线计划建议删除。
    ```
  - 影响: 全库核实存在多个生产调用方——`src/features/edit/core/timeline_export.py:782,841`、`src/features/deliver/core/delivery_capability.py:116`、`src/features/edit/ui/edit_effects_panel.py:1369`、`src/features/edit/ai_editing/capability_manifest.py:260,280` 均调用 `resolve_execution_capability`/`resolve_capability`。横幅不仅失实，还带有"建议删除"的指示，可能导致后续维护者误删在用模块。
  - 建议: 移除或更新 DORMANT 横幅（列出现有生产调用方）。
  - 置信度: 高

- [AUD-26304] [Suggestion] `resolve_execution_capability` 内的宽泛 `except Exception` 无日志地吞掉能力求证错误
  - 位置: src/features/shared/effects/effect_capability.py:520-530
  - 证据:
    ```python
        if (not cap.exportable and effect_type.startswith("plugin:")):
            try:
                from src.features.shared.effects.effect_catalog import (
                    get_effect_catalog,
                )
                spec = get_effect_catalog().get_spec(effect_type)
                if spec is not None and "export_opengl" in spec.plugin_capabilities:
                    opengl_export = True
                    plugin_gl_only = True
            except Exception:  # noqa: BLE001 — 能力求证失败按不可交付处理
                plugin_gl_only = False
    ```
  - 影响: fail-closed 方向正确（按不可导出处理），但连 TypeError/AttributeError 等编程错误也一并静默，且无任何日志；插件作者声明了 `@export_support opengl` 却被判定 preview_only 时，无法区分"目录缺陷"与"插件确实未声明"，与 FX-M03"绝不静默"原则相悖。
  - 建议: 保留 fail-closed，但在 except 分支 `logger.warning(...)` 记录异常。
  - 置信度: 高

- [AUD-26305] [Suggestion] `resolve_capability` 在 `has_gpu=False` 时丢弃显式声明的后端集合，CPU-only 效果会被 `check_backend_support` 误判 UNSUPPORTED
  - 位置: src/features/shared/effects/effect_capability.py:161, 168, 200-205
  - 证据:
    ```python
        else:
            resolved_graphics = graphics_backends if has_gpu else frozenset()
    ...
        else:
            resolved_compute = compute_backends if has_gpu else frozenset()
    ```
    以及：
    ```python
        if not capability.graphics_backends and not capability.compute_backends:
            return EffectDiagnostic(
                effect_id=effect_type, effect_type=effect_type,
                state=EffectDiagnosticState.UNSUPPORTED,
                detail="no render backend declared",
            )
    ```
  - 影响: 一个不在 catalog（`has_gpu=False`）但 `cpu_preview=True` 且显式声明了后端集合的效果，两个集合都被清空，`check_backend_support` 第一分支即返回 UNSUPPORTED"no render backend declared"——尽管它明明可走 CPU 预览。当前 blur/sharpen 均在 catalog 内故不可达，属潜伏缺陷；一旦出现 catalog 外的 CPU-only 效果即触发误报。
  - 建议: 清空条件改为仅当效果完全无任何后端（`not has_gpu and effect_type not in cpu_preview_types and ...`）时，或在 `check_backend_support` 首分支先排除 `capability.previewable` 的情况。
  - 置信度: 中（代码行为确定；当前数据下不可达）

- [AUD-26306] [Suggestion] `EffectExecutionCapability` 的容器字段用裸 `tuple`/`set` 注解，丢失元素类型契约
  - 位置: src/features/shared/effects/effect_capability.py:426-427, 536
  - 证据:
    ```python
        reasons: FrozenSet[tuple] = frozenset()       # {(axis, code, message)}
        quality_constraints: tuple = ()               # e.g. metal 参数子集门控
    ...
        reasons: set = set()
    ```
  - 影响: 注解未表达 `(str, str, str)` 三元组与 `Tuple[str, ...]` 约束，静态检查无法捕获放入畸形元素（如二元组）的错误；`reason_for` 解包 `for a, code, message in self.reasons` 时畸形元素会在运行时 ValueError。
  - 建议: 改为 `FrozenSet[Tuple[str, str, str]]`、`Tuple[str, ...]`、`Set[Tuple[str, str, str]]`。
  - 置信度: 高

### 通过项

- 线程安全：纯数据 + 无共享可变状态；frozen dataclass 字段默认值用不可变 frozenset/空元组，无可变默认参数问题。
- GPU 资源生命周期：无 GL 对象（模块 docstring 声明 no GL/Qt deps，核实属实）。
- GLSL·HDR 正确性：不涉及着色器。
- 信号防递归：无 Qt 信号。
- 状态提交一致性：能力数据为纯函数派生，无持久化/revision 状态。
- 异常与取消路径（部分通过）：`parse_backend_declaration` 对未知 token 显式 ValueError、拒绝 'auto'，符合"诊断而非静默忽略"；仅 AUD-26304 所述一处吞异常。
- 输入验证（部分通过）：`parse_backend_declaration`、`supports_graphics/supports_compute` 对空串/大小写有归一化处理。
- 性能模式：纯集合运算，无热点。
- `classify_for_export`/`decide_preview_policy`/`decide_export_policy`/`compute_effect_status` 的状态机分支逐一核对无逻辑错误。

---

## src/features/shared/effects/effect_catalog.py (行数: 743)

### 发现

- [AUD-26307] [Major] `@export_support` 声明值未校验：任意非空/非 none 令牌都会被授予 `export_opengl` 能力并进入 GL 导出链
  - 位置: src/features/shared/effects/effect_catalog.py:552-558
  - 证据:
    ```python
                plugin_capabilities=(
                    ("gpu_preview", "export_opengl")
                    if str((plugin.get("capabilities") or {})
                           .get("export_support", "")).strip().lower()
                    not in ("", "none")
                    else ("gpu_preview",)
                ),
    ```
  - 影响: plugin_manager `_parse_capabilities` 对 capability 值"不设白名单，原样保留"（src/features/color/core/plugin_manager.py:182-211 已核实），因此插件声明 `// @export_support metal`、`// @export_support cpu` 或拼写错误（如 `openggl`）时，本行一律判真并落 `export_opengl` 能力。下游已核实两处消费：`timeline_export._spec_allows_opengl_export`（src/features/edit/core/timeline_export.py:2034 以 `"export_opengl" in plugin_capabilities` 放行 GL 导出链）与 `resolve_execution_capability`（据此标记可交付）。结果：作者明确声明非 OpenGL 导出（或拼错）的插件会违背声明 silently 进入交付物，且 Deliver preflight 不再拒绝——违反"外部数据须验证"铁律与 W06"未声明维持 preview-only"的注释语义。
  - 建议: 收紧判定为 `value == "opengl"`（strip/lower 后），其余非 none 值按解析告警处理并维持 preview-only；或复用 `parse_backend_declaration` 的词表校验。
  - 置信度: 高

- [AUD-26308] [Major] 插件目录扫描失败被宽泛 except 静默吞掉，`exc` 捕获后未使用且无日志
  - 位置: src/features/shared/effects/effect_catalog.py:482-499
  - 证据:
    ```python
        plugins = pm.scan_plugins(_plugins_dir)
    except Exception as exc:  # noqa: BLE001 — catalog must stay robust
        # Plugin manager unavailable (e.g. running headless in tests) —
        # return an empty list rather than crashing the catalog build.
        return specs
    ```
  - 影响: `exc` 被捕获后从未使用、没有任何日志。注释假设的失败场景是"测试 headless"，但同一分支也会吞掉真实的 PermissionError/OSError/`scan_plugins` 内部缺陷，以及打包（frozen）布局下 `_project_root`（由 `__file__` 上溯四级目录推出，491-494 行）指向不存在 `plugins/` 的情况——用户的所有 DCTL 插件会从 Edit 页面静默消失，与 FX-M03"绝不静默丢效果"的设计原则和 .clinerules"不得宽泛 except 掩盖领域错误"相悖。
  - 建议: 至少 `logging.getLogger(__name__).warning("DCTL plugin scan failed: %s", exc)`；对打包布局改用 `sys.frozen` 感知的资源路径解析。
  - 置信度: 高

- [AUD-26309] [Suggestion] `preview_cost` 存入 `PreviewQualityLevel` 枚举，违反 `EffectSpec.preview_cost: str` 契约
  - 位置: src/features/shared/effects/effect_catalog.py:518; src/features/shared/effects/effect_spec.py:127
  - 证据:
    ```python
            preview_cost = plugin.get("preview_quality_level") or "medium"
    ```
    （`preview_quality_level` 由 plugin_manager `_parse_preview_cost` 产出，是 `PreviewQualityLevel` 枚举成员或 None——src/features/color/core/plugin_manager.py:216-231 已核实；而 EffectSpec 注解为 `preview_cost: str = "medium"`，docstring 约定取值 `"low"/"medium"/"high"/"skip"`。）
  - 影响: 普通 `Enum`（非 str-mixin，src/shared/effects/preview_quality.py:37 已核实）成员存入 str 字段：任何未来的 `spec.preview_cost == "high"` 比较（edit_gpu_preview_renderer.py:4619 的 docstring 即示范了这种用法）都将恒为 False，播放降级逻辑对插件静默失效。当前无运行时比较点（仅注释引用），属潜伏类型契约破坏。
  - 建议: 存入前映射为枚举 `.value` 小写字符串（并处理 `FULL` → 约定值），或把 EffectSpec 字段类型改为 `Union[str, PreviewQualityLevel]` 并统一比较工具。
  - 置信度: 高（类型事实）；当前无运行时影响

- [AUD-26310] [Suggestion] 同 `effect_type` 重新注册时类别变更会留下陈旧的 `_by_category` 索引
  - 位置: src/features/shared/effects/effect_catalog.py:655-666
  - 证据:
    ```python
        is_new = spec.effect_type not in self._specs
        self._specs[spec.effect_type] = spec
        if is_new:
            self._by_category.setdefault(spec.category, []).append(spec.effect_type)
    ```
  - 影响: `register()` 的文档化用途是"插件 reload 时替换同 id 的 spec"。若替换时 `category` 发生变化，旧类别下的 id 不会被移除、新类别也不会收录——`get_specs_by_category(新类别)` 找不到该效果，`get_specs_by_category(旧类别)` 返回的 spec 却带着新 category，UI 分组与 spec 自述不一致。当前所有 DCTL 插件 category 固定为 `"plugin"`、内置效果分类亦固定，故暂不可达，属 reload 路径上的明确潜伏风险。
  - 建议: 非 is_new 时先从旧 category 列表移除该 id（必要时清理空列表）再按新 category 收录。
  - 置信度: 高（行为确定）；当前数据下不可达

- [AUD-26311] [Suggestion] `_dctl_param_to_pdef` 导入私有名 `_PARAM_GLTYPE` 且从未使用（死导入）
  - 位置: src/features/shared/effects/effect_catalog.py:416
  - 证据:
    ```python
        from src.features.shared.effects.dctl_wrapper import _PARAM_GLTYPE

        ptype = param.get("type")
    ```
    （grep 确认 `_PARAM_GLTYPE` 在本文件仅出现在该 import 行，函数体按 ptype 手工分支。）
  - 影响: 死代码 + 跨模块引用私有名，误导读者以为类型映射由此驱动；未来 dctl_wrapper 重命名私有常量会无意义地破坏本模块。
  - 建议: 删除该 import；若想单一真相源，则让分支真正查 `_PARAM_GLTYPE`。
  - 置信度: 高

- [AUD-26312] [Suggestion] `_ensure_built` 持锁期间执行插件目录磁盘扫描；`is_known` 文档称"cheap"但首次调用触发全量构建
  - 位置: src/features/shared/effects/effect_catalog.py:631-653, 707-712
  - 证据:
    ```python
        with self._lock:
            if self._built:
                return
            ...
            for spec in _build_dctl_plugin_specs():
                self._register_locked(spec)
            self._built = True
    ...
        def is_known(self, effect_type: str) -> bool:
            """Cheap membership check (used to skip unknown legacy effect_type
            values without raising)."""
    ```
  - 影响: `_build_dctl_plugin_specs` 在持有 `self._lock` 时调用 `pm.scan_plugins(_plugins_dir)` 做文件系统扫描（含读取每个 `.tglsl`），期间任何渲染/undo 路径的 `get_spec`/`is_known` 调用都会阻塞；且 `is_known` 首次调用即触发"导入 6000+ 行 shaders.py + 磁盘扫描"的全量惰性构建，与"cheap"注释不符（注释缓解手段 `__file__` 推根是好的，但注释误导调用方在热路径随意使用）。一次性成本、无死锁风险（scan_plugins 未回调 catalog），影响为首次访问延迟尖峰。
  - 建议: 将插件扫描移出锁（先无锁收集 specs，再持锁注册），或缓存扫描结果；修正 `is_known` 注释。
  - 置信度: 高

### 通过项

- 线程安全（基本通过）：`_ensure_built` 双检锁 + RLock、`_catalog_instance` 双检锁 + 模块级 Lock，均正确；`get_specs_by_category` 持锁拷贝后排序不触碰共享结构；重复构建经 `_register_locked` 的 is_new 检查幂等。
- GPU 资源生命周期：catalog 仅持有 shader 源字符串，不创建 program/纹理，符合"只共享描述"的架构约束。
- GLSL·HDR 正确性：着色器源直接复用 composite 权威实现（未复制第二份），defocus 的 schema/参数桥接在 packer 层且有显式注释跟踪。
- 信号防递归：无 Qt 信号（catalog 刻意保持无 PyQt 依赖，docstring 声明核实属实）。
- 状态提交一致性：不涉及 undo/redo/持久化；`refresh()` 清空重建语义一致。
- 异常与取消路径（部分通过）：单插件构建失败 per-plugin 捕获且 `logging.warning` 带插件 id（569-574 行，正确模式）；仅 AUD-26308 所述外层扫描静默。
- 输入验证（部分通过）：`_dctl_param_to_pdef` 对 select default 越界、color default 形状、缺失 var_name 均有防护；`background_independent` fail-closed 精确匹配；仅 `export_support` 值域未验证（AUD-26307）。
- 性能模式（基本通过）：shaders.py 延迟导入避免启动期拉入大 GLSL 模块，模式良好。
- 类型注解：公共方法签名完整（`_dctl_param_to_pdef(param: dict)` 为裸 dict，轻微）。
