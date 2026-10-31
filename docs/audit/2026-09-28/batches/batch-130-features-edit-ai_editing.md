# batch-130 审查报告 — src/features/edit/ai_editing/

- 批次：batch-130 [P1]
- 编号区间：AUD-13000–AUD-13099（实际使用 AUD-13001–AUD-13025）
- 审查文件：7 个，共 2,955 行（全部 Read 通读）
- 结论汇总：Critical 0 / Major 8 / Suggestion 17

---

## src/features/edit/ai_editing/assistant_capability_router.py (行数: 415)

### 发现

- [AUD-13001] [Major] `_active_profile` 用宽泛 `except Exception` 吞掉 ProfileRegistry 异常，把真实错误伪装成"未配置 Profile"
  - 位置: src/features/edit/ai_editing/assistant_capability_router.py:360-366
  - 证据:
    ```python
    def _active_profile(self, capability: str) -> AiProviderProfile | None:
        if self._profiles is None:
            return None
        try:
            return self._profiles.active_profile_for_capability(capability)
        except Exception:  # pragma: no cover - defensive: registry must not crash routing
            return None
    ```
  - 影响: 注册表的真实异常（配置数据损坏、迁移 bug、键类型错误）被静默吞掉，`resolve` 返回 `UNCONFIGURED` 且 reason 显示"未配置语言模型 Profile"——用户被误导去重新配置，而不是看到真实故障；违反 .clinerules"不得宽泛 except 掩盖领域错误"。且无任何日志留痕，线上不可诊断。
  - 建议: 保留 fail-closed 返回 None，但至少 `logger.warning(..., exc_info=True)` 记录；或按异常类型收窄捕获。
  - 置信度: 高

- [AUD-13002] [Suggestion] 模块中部 import（函数定义之后才导入 provider_profiles），违反 PEP 8 布局
  - 位置: src/features/edit/ai_editing/assistant_capability_router.py:56-65
  - 证据:
    ```python
    def _known_modes() -> frozenset[str]:
        """Return built-in routing ids plus currently discovered plugin modes."""
        return frozenset(MODE_CAPABILITY_REQUIREMENTS.keys()) | frozenset(
            spec.mode_id for spec in get_assistant_mode_specs()
        )
    from src.features.edit.ai_editing.provider_profiles import (
        AiProviderProfile,
        ProfileRegistry,
    )
    ```
  - 影响: 纯风格问题，无功能影响；阅读时容易忽略这段延迟导入。
  - 建议: 移动到文件顶部 import 区。
  - 置信度: 高

### 通过项
- fail-closed 路由：未知 mode 归一化到 chat（不触发付费生成）、未知 capability 返回 NOT_INSTALLED（:261-267）
- demo provider 显式标注 DEMO_ONLY，不冒充真实可用（:304-314）
- vision_understanding 与 image_generation 能力分离，歧义的 legacy "vision" 标签不被复用（:79-88, :250-258；已核实 profile 侧 "vision_understanding" 显式标签存在，探测路径成立）
- 多能力模式（Motion Graphic）聚合探测，missing_capabilities 全量列出且顺序确定（:174-195）
- 无 Qt/ModernGL 依赖；路由器无状态、可并发调用
- 已核实 `capabilities_for_mode` 插件路径与内置表均不可能返回空集（插件空 capabilities 为 fatal 诊断，:294-300 of assistant_plugins.py），`ordered[0]` 不存在 IndexError 路径

---

## src/features/edit/ai_editing/assistant_events.py (行数: 189)

### 发现

- [AUD-13003] [Suggestion] `AssistantStateSnapshot.diff_summary` 使用 `dict[str, Any]`，违反本模块"禁止 dict[str, Any] 跨层传播"的自述契约
  - 位置: src/features/edit/ai_editing/assistant_events.py:189（契约声明 :3-5）
  - 证据:
    ```python
    # 模块 docstring：UI 与 service 之间**禁止**用 ``dict[str, Any]`` 无限传播（TAEP §11.9 UI-0）。
    # 可选：当前 diff 摘要（AWAITING_APPLY_APPROVAL 时）
    diff_summary: dict[str, Any] | None = None
    ```
  - 影响: 类型化状态契约存在旁路：service 端可向 UI 塞任意结构，UI 端只能防御式取值，弱化了 TAEP UI-0 的强类型边界。
  - 建议: 定义 typed `DiffSummary` dataclass（或至少收窄为 `Mapping[str, str]`）。
  - 置信度: 高

### 通过项
- 全部 dataclass 均 frozen，跨线程共享安全；不含 Qt object（服务层无 PyQt 依赖）
- `AssistantRequest.__post_init__` 校验 request_id 非空（:91-93）
- 状态机枚举含 `is_busy` / `is_awaiting_user` 派生属性，语义清晰
- 无 GPU/GLSL/HDR 相关逻辑，无持久化逻辑

---

## src/features/edit/ai_editing/assistant_modes.py (行数: 106)

### 发现

- [AUD-13004] [Suggestion] 兼容常量在 import 时固化插件清单，`reload_assistant_plugins()` 后过期
  - 位置: src/features/edit/ai_editing/assistant_modes.py:71-73
  - 证据:
    ```python
    ASSISTANT_MODE_SPECS: tuple[AssistantModeSpec, ...] = get_assistant_mode_specs()
    DEFAULT_ASSISTANT_MODE = "chat"
    MEDIA_ASSISTANT_MODES = get_media_assistant_modes()
    ```
  - 影响: 设置页/测试调用 reload 后这两个常量仍是旧快照；已核实 src/app 与 src/features 无生产调用点（仅测试），但导出名会诱使新代码误用过期数据（尤其 MEDIA_ASSISTANT_MODES 语义上用于判定付费媒体模式）。
  - 建议: 注释标注 deprecated 指向 getter，或直接删除常量并改写测试。
  - 置信度: 高（已 grep 核实无生产调用方）

- [AUD-13005] [Suggestion] import 即触发磁盘扫描（经 get_assistant_plugin_registry → 注册表构造 + reload）
  - 位置: src/features/edit/ai_editing/assistant_modes.py:51, 63-65
  - 证据:
    ```python
    registry = get_assistant_plugin_registry()
    if registry.exists:
        return registry.media_modes()
    ```
  - 影响: 模块导入产生文件系统 I/O 副作用；`TOPOS_AI_ASSISTANT_PLUGIN_DIR` 在 import 之后设置对已构造的默认注册表无效（必须显式 reload），测试隔离性差。
  - 建议: 与 AUD-13008 一并改为惰性构造。
  - 置信度: 高

### 通过项
- `normalize_assistant_mode` 对任意输入 fail-closed 回落 chat，防误触发写入/付费生成（:76-87）
- `assistant_mode_spec` 的 `next(...)` 不会 StopIteration（normalize 已保证命中）
- 无 Qt 依赖；纯数据模块

---

## src/features/edit/ai_editing/assistant_plugins.py (行数: 457)

### 发现

- [AUD-13006] [Suggestion] `unverified` 信任标志由插件自报的 `source` 字段直接推导，清单可伪造"已验证"
  - 位置: src/features/edit/ai_editing/assistant_plugins.py:408-409
  - 证据:
    ```python
    source=str(payload.get("source", "local_dev") or "local_dev"),
    unverified=str(payload.get("source", "local_dev") or "local_dev") == "local_dev",
    ```
  - 影响: 任意 plugin.json 写 `"source": "official"` 即获得 `unverified=False`（表现为已验证）。模块 docstring 声称"清单不能通过改自己的元数据获得新权限"，而该信任标志恰是自报元数据。已核实当前 src 内无该字段消费方（ai_harness 用的是另一套 manifest），属潜伏风险；违反"外部数据须验证"精神。
  - 建议: `unverified` 由宿主白名单策略决定（枚举可信 source 值），未知值一律 unverified=True。
  - 置信度: 高（代码）；影响为潜伏（当前无消费方）

- [AUD-13007] [Suggestion] `handler` 字段无已知处理器白名单校验，未知 handler 延迟到用户发起请求时才报错
  - 位置: src/features/edit/ai_editing/assistant_plugins.py:302-309
  - 证据:
    ```python
    handler = str(payload.get("handler", "generic_chat") or "").strip()
    if handler == "generic_chat" and "language" not in capabilities:
        diagnostics.append(AssistantPluginDiagnostic(
            ...  "generic_chat 插件必须声明 language 能力", ...))
    ```
  - 影响: 拼写错误的 handler 加载阶段无诊断，用户第一次使用该模式时才发现"处理器尚未装配"（ai_edit_controller.py:989-995 运行时警告）；错误发现被推迟。
  - 建议: `_read_manifest` 内维护宿主已知 handler 集合，未知值发 warning 诊断。
  - 置信度: 高

- [AUD-13008] [Suggestion] 模块 import 即构造默认注册表并扫描磁盘
  - 位置: src/features/edit/ai_editing/assistant_plugins.py:413
  - 证据:
    ```python
    _DEFAULT_ASSISTANT_PLUGIN_REGISTRY = AssistantPluginRegistry()
    ```
  - 影响: 任何 import 链触达本模块（含 provider_capabilities.capabilities_for_mode 的反向依赖路径）都会执行目录 iterdir + 每个插件 JSON 解析；影响启动耗时与测试隔离（import 副作用）。
  - 建议: 改为 `get_assistant_plugin_registry()` 首次调用时惰性构造（加简单的 None 哨兵 + 赋值）。
  - 置信度: 高

### 通过项
- prompt 路径防穿越双保险：`prompt_path` 与 `_read_manifest` 均用 `resolve()` + `relative_to`，symlink 逃逸同样被拒（:89-95, :347-358）
- manifest 逐字段验证完备：id 格式/一致性、kind、api_version、capabilities 类型与空集、label、semver version、order、prompts 类型；fatal 诊断整包拒绝
- reload 原子性：先收集到局部 dict/tuple 再整体赋值，坏插件不移除同批有效插件（:150-189）
- 重复 plugin id 检测（:177-184）；OSError/UnicodeError/JSONDecodeError 显式转为诊断（:234-249）
- 不执行插件 Python 代码、不授予宿主权限（与 docstring 声明一致）
- 线程安全：`_plugins` dict 整体替换，GIL 下读取方看到一致快照

---

## src/features/edit/ai_editing/assistant_service.py (行数: 1043)

### 发现

- [AUD-13009] [Major] `config` 属性在 `start_session()` 之前访问抛 AttributeError：`__init__` 从未初始化 `_config`
  - 位置: src/features/edit/ai_editing/assistant_service.py:191-192（属性）；:115-169（__init__ 无 `_config`）；:417（唯一赋值点，start_session 首行）
  - 证据:
    ```python
    @property
    def config(self) -> "AssistantConfig":
        return self._config
    ```
    调用方被迫防御：ai_editing_mixin.py:1523-1527 `try: project_id = previous_service.config.project_id or None except (AttributeError, RuntimeError): project_id = None`；:866-868 注释明言"Controller 必须在面板首次发送请求前启动会话；否则 AssistantService.config 尚未初始化"。
  - 影响: 任何在会话启动前读 `service.config` 的代码直接崩溃；调用方的 except AttributeError 防御代码本身证明这是真实、已知的风险面。
  - 建议: `__init__` 中初始化 `self._config = AssistantConfig()`，或将属性改为 Optional 并给出明确报错信息。
  - 置信度: 高

- [AUD-13010] [Major] `cancel()` 用 `except Exception: pass` 吞掉 runtime.cancel 失败，状态机进入 CANCELLED 但下游 run 可能仍在执行
  - 位置: src/features/edit/ai_editing/assistant_service.py:984-992
  - 证据:
    ```python
    try:
        self._active_runtime.cancel(self._active_run_id)
    except Exception:  # noqa: BLE001
        pass
    self._set_state(AssistantState.CANCELLED)
    ```
  - 影响: 取消传播失败（provider/job 层异常）完全不可见，UI 已显示"已取消"，付费生成 API 调用可能继续扣费执行；违反"不得宽泛 except 掩盖领域错误"。
  - 建议: 失败时 `logger.warning` 并在 snapshot 的 error_message/诊断中提示"取消请求未能传达，任务可能仍在运行"。
  - 置信度: 高

- [AUD-13011] [Major] `_current_timeline_revision` 宽泛 `except: pass` 把快照失败静默降级为 base_revision=0，plan 以错误基线编译
  - 位置: src/features/edit/ai_editing/assistant_service.py:998-1006（吞错）；:616-623（`base_rev or 0` 进入 PlannerInput）
  - 证据:
    ```python
    try:
        snap = self._session.get_snapshot()
        tl = snap.active_timeline
        if tl and tl.get("timeline_id") == timeline_id:
            return int(tl.get("revision", 0))
    except Exception:
        pass
    return None
    ```
    以及 plan_variants 中 `base_revision=base_rev or 0`。
  - 影响: EditSession 的真实异常被掩盖，plan 针对 revision 0 编译；后续 TransactionService.commit 的乐观锁会拦下不匹配（已核实 commit 为"validate→compile→乐观锁→原子提交"），但用户面对的是莫名其妙的 revision 不匹配失败，且浪费整轮 LLM 规划，根因被吞。
  - 建议: except 分支记录日志，并让 plan_variants 显式携带"revision 未知（降级）"诊断，或直接进入 FAILED。
  - 置信度: 中（影响链有乐观锁兜底，但吞错本身确定）

- [AUD-13012] [Suggestion] `plan_variants` 失败时不清空旧 variants，FAILED 快照仍展示并允许选取上一轮 stale variant
  - 位置: src/features/edit/ai_editing/assistant_service.py:635-650
  - 证据:
    ```python
    self._variants = {v.variant_id: v for v in variants}
    ...
    except Exception as exc:
        self._set_state(AssistantState.FAILED, error=str(exc))
        return []
    ```
    `self._variants` 仅在成功路径替换；`_emit_state` → `_variant_cards()` 仍读取旧值。
  - 影响: FAILED 状态下 UI 仍渲染上一轮 variant 卡片；用户可选取旧方案，`validate_selected` 多数情况报"plan 已过期"（:896-899），但徒增混乱与误导。
  - 建议: 失败时清空 `_variants`/`_decisions` 或将卡片标记为 stale。
  - 置信度: 中

- [AUD-13013] [Suggestion] `set_active_run_id` 不清理旧 runtime：新 run 未带 runtime 时 cancel 会用旧 runtime 打新 run_id
  - 位置: src/features/edit/ai_editing/assistant_service.py:373-381
  - 证据:
    ```python
    self._active_run_id = run_id
    if runtime is not None:
        self._active_runtime = runtime
    ```
  - 影响: 旧 runtime.cancel(新 run_id) 大概率抛"未知 run"异常 → 被 AUD-13010 的 pass 吞掉 → 取消静默失效，状态仍显示 CANCELLED。
  - 建议: `runtime is None` 时同时清空 `self._active_runtime`。
  - 置信度: 高（代码）；中（可达性取决于 controller 是否总传 runtime）

- [AUD-13014] [Suggestion] 动作型入口缺少输入验证：空文本、未知 variant_id、任意 role 字符串均被接受
  - 位置: src/features/edit/ai_editing/assistant_service.py:451-469（append_chat_message 的 role 无白名单）；:531-549（submit_request 空 text 直入 GATHERING_CONTEXT）；:843-849（select_variant 未知 id 仍切 AWAITING_VARIANT_APPROVAL）
  - 证据:
    ```python
    def select_variant(self, variant_id: str) -> None:
        self._set_state(AssistantState.AWAITING_VARIANT_APPROVAL)
        for vid, v in self._variants.items():
            self._variants[vid] = v.with_status(
                VariantStatus.SELECTED if vid == variant_id else VariantStatus.PROPOSED
            )
    ```
  - 影响: 与 router 的 fail-closed 纪律不一致；select 未知 id 会把所有 variant 置回 PROPOSED 且无任何 SELECTED，状态机却进入待批准态。
  - 建议: role 限定 user/assistant/system；空文本快速返回；未知 variant_id 记录错误并保持原状态。
  - 置信度: 高

- [AUD-13015] [Suggestion] `preview_brief_patch` 跨模块访问 BriefService 私有方法 `_merge`
  - 位置: src/features/edit/ai_editing/assistant_service.py:584
  - 证据:
    ```python
    # 复用 BriefService 的合并逻辑（不 save、不 bump）。
    merged = self._brief._merge(current, patch.changes)
    ```
  - 影响: 破坏封装；BriefService 重构内部合并实现会静默破坏 AI 预览路径（已核实 `_merge` 目前存在，brief_service.py:142）。
  - 建议: BriefService 提供公开 `preview_merge(brief, changes)`。
  - 置信度: 高

- [AUD-13016] [Suggestion] 类型注解不精确：`_resolve_job_cards` 返回裸 `tuple`；`run_workflow` 的 cancel_requested/emit_node_event 标注为 `Any`
  - 位置: src/features/edit/ai_editing/assistant_service.py:363, 659-661
  - 证据:
    ```python
    def _resolve_job_cards(self) -> tuple:
    ...
        cancel_requested: Any = None,
        emit_node_event: Any = None,
    ```
  - 影响: 调用方无法从签名得知回调契约（`Callable[[], bool]` / 事件发射器），`cancel_requested()` 直接调用也无类型检查保护。
  - 建议: `tuple[JobCard, ...]`、`Callable[[], bool] | None`、`Callable[..., None] | None`。
  - 置信度: 高

### 通过项
- 状态机错误语义完整：FAILED/CANCELLED 均经 `_set_state` 记录并清除过期 error（:260-266）；plan/preview/apply 失败均进入 FAILED 而非静默
- 状态提交一致性：apply 走 `TransactionService.commit` 的 ApprovalGrant 契约（单次 consume 防重放，:951-971）；plan 经 PlanRepository 存根并以 state fingerprint 防 stale（:905-918）；AI 编辑归并为单 Undo 单元（on_applied 文档 + commit 路径）
- 线程边界：本层纯逻辑无 Qt/ModernGL；已核实 controller 的 `_PlanWorker` 在 worker 线程摘除 `on_state_changed` 回调、经 Qt signal 回主线程统一回填（ai_edit_controller.py:145-169），service 自身不直接跨线程触碰 UI
- run_workflow/run_skill 的取消门槛：调用前 `cancel_requested()` 短路并转入 CANCELLED（:685-693）；`run_official` 对非官方无角色 Skill 明确降级不编造（:1022-1027）
- `_resolve_allow_media_upload`/`_build_context_summary`/`_resolve_job_cards` 对注入 provider 的异常均有兜底（回退 False/默认/空元组）

---

## src/features/edit/ai_editing/assistant_session_store.py (行数: 443)

### 发现

- [AUD-13017] [Major] `persist()` 的 `mkdir` 未纳入异常保护：只读工程目录直接抛 OSError 而非按契约返回 False
  - 位置: src/features/edit/ai_editing/assistant_session_store.py:162-163
  - 证据:
    ```python
    ai_dir = Path(target_dir) / "ai"
    ai_dir.mkdir(parents=True, exist_ok=True)
    self._snapshot.updated_at = time.time()
    ```
    随后两个 `write_text` 各自有 `except OSError: ok = False`（:167-181），且 :182-183 注释专门声称处理"工程目录只读"场景。
  - 影响: 恰好在注释声称支持的只读场景中，`mkdir` 先抛 PermissionError，`persist` 的 bool 契约（"两者都写失败才返回 False"）被打破；异常向上传播可中断宿主的工程保存流程。
  - 建议: 将 mkdir 一并包进 try/except OSError（ok 标志或提前 return False）。
  - 置信度: 高

- [AUD-13018] [Major] `load()`/`restore_catalog` 的异常捕获缺少 TypeError/AttributeError，损坏的 ai/*.json 可使工程打开崩溃
  - 位置: src/features/edit/ai_editing/assistant_session_store.py:198-228（load 两处 except 仅含 OSError/json.JSONDecodeError/ValueError）；:325-328（restore_catalog 无任何保护）
  - 证据:
    ```python
    try:
        data = json.loads(catalog_path.read_text(encoding="utf-8"))
        self._catalog = SessionCatalog.from_dict(data)
        ...
    except (OSError, json.JSONDecodeError, ValueError):
        pass  # 落到 v1 路径
    ```
    而 `SessionSnapshot.from_dict` 的 `dict(data.get("briefs", {}))` 在 briefs 为标量时抛 TypeError；session_models 的 `SessionCatalogEntry.from_dict` 对 messages 列表内非 dict 元素调用 `m.get` 抛 AttributeError——均不在捕获列表。
  - 影响: 手改/损坏的工程 AI payload（外部数据）使 `load()` 向上抛未捕获异常，工程打不开；违反"外部数据须验证"。
  - 建议: 捕获 `(OSError, ValueError, TypeError, AttributeError, KeyError)` 或在 from_dict 层做逐字段 isinstance 验证。
  - 置信度: 高

- [AUD-13019] [Major] `set_session_title` 对"仅存在于本机缓存"的会话改名后不落盘：返回 True 但重启即丢
  - 位置: src/features/edit/ai_editing/assistant_session_store.py:356-368（改名）；:300-313（_sync_catalog_to_cache 仅在 catalog 有非空会话时才写缓存）
  - 证据:
    ```python
    entry = self._catalog.get_session(session_id)
    if entry is None:
        entry = self._cache_sessions.get(session_id)
    ...
    entry.title = title.strip()
    entry.touch()
    ...
    self._sync_catalog_to_cache()
    return True
    ```
    `_sync_catalog_to_cache` 内 `changed` 只由 catalog 会话置位；catalog 为空/全空时 `_save_cache()` 不会执行。
  - 影响: 跨工程恢复的缓存会话（get_session 允许命中 cache-only 条目）改名显示成功，重启后标题回退——用户数据丢失。
  - 建议: 缓存条目发生变更时直接调用 `_save_cache()`，不依赖 catalog 的联动条件。
  - 置信度: 高

- [AUD-13020] [Major] 本机缓存为共享单文件、整文件重写且无锁：多实例并发写互相覆盖（last-writer-wins 丢会话）
  - 位置: src/features/edit/ai_editing/assistant_session_store.py:100-105（默认路径 + 构造时各自 _load_cache 内存快照）；:257-276（_save_cache 全量重写，固定 .tmp 名，无文件锁）
  - 证据:
    ```python
    default_cache = Path.home() / ".topos_cache" / "ai_assistant_history.json"
    ...
    temp_path = self._cache_path.with_suffix(self._cache_path.suffix + ".tmp")
    temp_path.write_text(json.dumps({"version": 1, "sessions": {...}}), ...)
    temp_path.replace(self._cache_path)
    ```
  - 影响: 应用双开（或未来多 store 实例）各自持有独立内存副本，后写者覆盖前者新增的会话——缓存历史丢失；两写者同时使用同一 .tmp 临时文件还可能交叉损坏。属缓存数据（非工程数据）丢失。
  - 建议: 写前重读合并，或按工程分片缓存文件 + 文件锁（fcntl）。
  - 置信度: 中（代码路径确定，触发需多实例并发写）

- [AUD-13021] [Suggestion] `add_message_summary`/`record_run`/`add_compression_checkpoint` 每次触发两轮全量缓存序列化+落盘（`_touch` 内已含 `_sync_catalog_to_cache`，方法尾部又调一次）
  - 位置: src/features/edit/ai_editing/assistant_session_store.py:230-233（_touch）；:413-414 / :426-427 / :441-442（重复调用）
  - 证据:
    ```python
    def _touch(self) -> None:
        self._snapshot.updated_at = time.time()
        self._catalog.touch()
        self._sync_catalog_to_cache()

    def add_message_summary(self, msg, *, session_id=None) -> bool:
        ...
        self._touch()
        self._sync_catalog_to_cache()   # 第二次
        return True
    ```
  - 影响: 每条对话消息双倍 JSON dump + 双次临时文件写+rename；长会话高频消息时磁盘 I/O 放大 2 倍（缓存含全部会话的全部消息摘要）。
  - 建议: 去掉方法尾部的重复 `_sync_catalog_to_cache()`，或让 `_touch` 不自动同步缓存。
  - 置信度: 高

- [AUD-13022] [Suggestion] `clear_history` 只清内存并写空缓存，工程内 session_catalog.json 磁盘文件未重写，未保存工程前旧会话可复活
  - 位置: src/features/edit/ai_editing/assistant_session_store.py:278-289
  - 证据:
    ```python
    def clear_history(self) -> None:
        self._cache_sessions.clear()
        self._catalog.sessions.clear()
        self._catalog.active_session_id = ""
        self._catalog.touch()
        # 即使为空也写回空对象，避免旧 JSON 在下一次启动时复活。
        self._save_cache()
    ```
  - 影响: 缓存写回了空对象，但工程的 `ai/session_catalog.json` 仍是旧内容；用户清空历史后若不保存工程（或崩溃），下次 `load()` 从磁盘恢复全部旧会话——与注释"避免旧 JSON 复活"的意图在工程文件维度不一致。
  - 建议: 文档明示需工程保存才彻底持久，或宿主在 clear 后触发一次 persist。
  - 置信度: 中

### 通过项
- persist 双文件（v1 session.json + v2 session_catalog.json）向后兼容，任一写失败正确汇总为 False（写路径本身）
- `_save_cache` 临时文件 + replace 原子替换；缓存损坏时 `_load_cache` 静默回空（捕获含 TypeError，:254）
- 缓存 50 条上限按 updated_at 淘汰最旧；`set_cache_limit` 有 max(1,...) 下界钳制
- load 顺序正确：v2 优先、v1 迁移（migrate_legacy_snapshot）、两者皆无返回 False（非错误）
- clear_history 明确保留工程级 brief/variant/decision（与"对话历史"边界一致）
- 不保存 secret/绝对路径/素材内容（依赖 session_models 的 sanitize_for_storage，已核实 to_dict 均经过滤）

---

## src/features/edit/ai_editing/audio_extractor.py (行数: 302)

### 发现

- [AUD-13023] [Suggestion] 整段 PCM 缓冲在内存（PyAV bytearray 全量累计 + CLI capture_output 全量 stdout），长素材内存峰值大
  - 位置: src/features/edit/ai_editing/audio_extractor.py:186-216（PyAV 路径）；:258-259（CLI 路径）
  - 证据:
    ```python
    pcm = bytearray()
    ...
    for packet in container.demux(stream):
        ...
        chunk = out_frame.to_ndarray().tobytes()
        if chunk:
            pcm.extend(chunk)
    ...
    result = subprocess.run(
        cmd, capture_output=True, timeout=timeout, check=False,
    )
    ```
  - 影响: 2 小时素材 ≈ 16kHz·s16·mono ≈ 230MB 常驻内存，另有每帧 tobytes() 临时拷贝；CLI 路径 stdout+stderr 双缓冲同量级。面向长视频的 4K 套件中批量 ASR/分离任务存在内存压力。
  - 建议: extract_wav 路径边解码边流式写 wave 文件；CLI 路径用 stdout=PIPE 增量读取。
  - 置信度: 高（内存量级确定）

- [AUD-13024] [Suggestion] `_resolve_ffmpeg` 用 `os.path.isfile("ffmpeg")` 接受 CWD 相对命中，打包环境下可能绑到错误二进制
  - 位置: src/features/edit/ai_editing/audio_extractor.py:285-295
  - 证据:
    ```python
    if os.path.isfile(self._ffmpeg) or os.path.dirname(self._ffmpeg):
        return self._ffmpeg
    located = find_ffmpeg()
    ```
  - 影响: 默认 `"ffmpeg"`（裸名）时，若当前工作目录恰好存在同名文件（如用户项目目录里放了名为 ffmpeg 的文件/脚本），会优先于打包 bin/ 定位结果被采用——行为依赖 CWD，打包版可能执行到非预期二进制。
  - 建议: 裸名称（不含路径分隔符）一律走 find_ffmpeg()；仅显式包含路径分隔符的值原样使用。
  - 置信度: 高（行为确定；实际影响低）

- [AUD-13025] [Suggestion] PyAV 路径仅 ImportError 触发 CLI 回退：半损坏的 PyAV（缺 API，AttributeError）被翻译成"解码失败"而不再尝试 CLI
  - 位置: src/features/edit/ai_editing/audio_extractor.py:163-173
  - 证据:
    ```python
    try:
        return self._decode_pcm_s16_pyav(source_path, sample_rate, timeout)
    except AudioExtractionError:
        raise
    except ImportError:
        logger.debug("PyAV unavailable, falling back to ffmpeg CLI")
    except Exception as exc:
        raise self._translate_pyav_error(exc, source_path) from exc
    ```
  - 影响: 旧版/损坏的 PyAV 安装（import av 成功但 AudioResampler 缺失或签名变化）直接对用户报"audio decode failed"，不给 CLI 一次机会，且无日志区分"文件坏"与"库坏"。
  - 建议: 将 AttributeError/TypeError 也纳入回退条件（或至少 debug 日志标注库级异常）。
  - 置信度: 中

### 通过项
- 无音频流显式抛 AudioExtractionError，不静默降级（provenance 可标 no_audio）；解码头不可用同样显式报错
- 超时双路径一致：PyAV deadline 逐 packet 检查（:197-202），CLI subprocess timeout（run 超时会 kill 子进程）
- 资源清理：container.close() 在 finally；所有失败路径对自建临时文件 _remove_quietly；写后校验非空（:112-117）
- 线程安全：无实例状态，mkstemp 生成唯一临时文件，可在 worker 线程调用（与 docstring 一致）
- subprocess 列表参数、无 shell 注入；source 不存在先验证（isfile）
- extract_samples 用 np.frombuffer 零拷贝；异常类型语义统一为 AudioExtractionError

---

## 汇总统计

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 8 | AUD-13001, 13009, 13010, 13011, 13017, 13018, 13019, 13020 |
| Suggestion | 17 | AUD-13002–13008, 13012–13016, 13021–13025 |

批次整体评价：该目录为纯逻辑编排层（无 Qt/GLSL/HDR 代码），GPU 与着色器维度全部通过。主要风险集中在三类：宽泛 except 吞错（路由探测、取消透传、revision 读取）、持久化契约缺口（mkdir 未保护、损坏 JSON 的 TypeError 逃逸、缓存改名不落盘、多实例覆盖）、以及 service 的 `_config` 未初始化崩溃面。
