# 批次报告：batch-231 — src/features/ai_workflow/adapters//

任务书：docs/audit/2026-09-28/assignments/batch-231.md（编号区间 AUD-23100–AUD-23199）
审查范围：5 个文件，1,969 行。本批次全部为纯 Python 数据契约 / 节点 spec 声明 / mock 服务，
无 GLSL、无 OpenGL、无 PyQt 信号，相关维度按不适用处理（在通过项注明）。

汇总：Critical 0 · Major 7 · Suggestion 10。

交叉验证说明：核心依赖 src/features/ai_workflow/core/node_specs.py（NodeSpecRegistry /
WorkflowNodeSpec）与生产装配点 src/app/controllers/dock_layout_controller.py:700-701、
src/app/controllers/ai_editing_mixin.py:772-786 已 Read/grep 核实；params_schema 的生产消费方
为 src/features/ai_workflow/ui/workflow_inspector.py:86 与 workflow_form_page.py:104（无
validator 强制校验，见 AUD-23114 影响）。

---

## src/features/ai_workflow/adapters/comfyui_models.py (行数: 808)

### 发现

- [AUD-23100] [Major] 内置节点白名单条目 `" ConditioningConcat "` 带首尾空格，真实 class_type 永不匹配
  - 位置: src/features/ai_workflow/adapters/comfyui_models.py:380
  - 证据:
    ```python
    _BUILTIN_CLASS_TYPES: Tuple[str, ...] = (
        ...
        "UNETLoader", "DualCLIPLoader", "CLIPLoader",
        " ConditioningConcat ",
    )
    ```
  - 影响: 任何使用内置节点 `ConditioningConcat` 的 Comfy workflow，在 `from_api_format` 中
    `class_type not in _BUILTIN_CLASS_TYPES`（:543）恒为 True，被误标
    `is_unknown_custom_node=True` 并生成一条 `repo_url=""` 的假 `ComfyCustomNodeRequirement`
    （:547-550）。依赖报告出现虚假 custom node 警告，用户被误导手动确认。
  - 建议: 去掉首尾空格；为 `_BUILTIN_CLASS_TYPES`/`_OUTPUT_CLASS_TYPES` 等常量表加一条
    单测（遍历断言无空白字符），防止同类笔误。
  - 置信度: 高

- [AUD-23101] [Major] `_canonical_slot_id` 去重计数只递增最终 slot_id，第 3 个同名输入起产生重复 slot_id
  - 位置: src/features/ai_workflow/adapters/comfyui_models.py:505-506, 596-609
  - 证据:
    ```python
    slot_id = cls._canonical_slot_id(iname, seen_slot_ids)
    seen_slot_ids[slot_id] = seen_slot_ids.get(slot_id, 0) + 1
    ...
    @staticmethod
    def _canonical_slot_id(input_name: str, seen: Dict[str, int]) -> str:
        sid = canonical.get(input_name, input_name)
        if sid in seen:
            sid = f"{sid}_{seen[sid] + 1}"
        return sid
    ```
  - 影响: 计数器按**最终** slot_id（如 `seed_2`）递增，基准名 `seen["seed"]` 永远停在 1。
    追踪：3 个 KSampler 各有 `seed` 字面量输入 → 生成 `seed`、`seed_2`、**`seed_2`**（重复）。
    多采样器/多 CLIPTextEncode 的常见 workflow 中，`input_slots` 出现重复 slot_id，
    `ComfyInputBinding` 按 slot_id 寻址时产生歧义（绑定到错误节点）。
  - 建议: 对**规范化基准名**计数（先 `seen[base] += 1` 再据此生成后缀），并在
    `from_api_format` 末尾断言 slot_id 唯一。
  - 置信度: 高

- [AUD-23102] [Major] `raw_payload` 仅浅拷贝且为 frozen dataclass 的可变字段，"只读快照 + content_hash 防篡改"红线可被绕过
  - 位置: src/features/ai_workflow/adapters/comfyui_models.py:445, 565-579
  - 证据:
    ```python
    raw_payload: Dict[str, Any] = field(default_factory=dict)
    ...
    normalized = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    content_hash = "sha256:" + hashlib.sha256(...).hexdigest()
    ...
    raw_payload=dict(raw) if isinstance(raw, dict) else {},
    ```
  - 影响: `dict(raw)` 是浅拷贝——嵌套的节点 dict 与调用方原始 `raw` 共享引用；调用方后续
    修改原始 dict（或任何持有 `snap.raw_payload` 的代码直接改嵌套值）都会改变快照内容，
    而 `content_hash` 不会重算。`ComfyJobRef.workflow_snapshot_hash` 的"防篡改"校验
    （:697 注释）因此失效；且 frozen dataclass 持有可变 dict，破坏模块声明的
    "全部 frozen"不变量。
  - 建议: 构造时深拷贝（`copy.deepcopy(raw)` 或 JSON round-trip 冻结），或把 payload 存为
    序列化后的不可变字符串；提交前用 content_hash 重算比对。
  - 置信度: 高

- [AUD-23103] [Suggestion] `from_api_format` 未把 node_id 归一为 str：summary 用 `str(node_id)` 而 slot 保留原值，int 型 node_id=0 时 slot 被静默丢弃
  - 位置: src/features/ai_workflow/adapters/comfyui_models.py:509-519, 552-553
  - 证据:
    ```python
    input_slots.append(ComfyInputSlot(
        slot_id=slot_id,
        node_id=node_id,          # 未 str() 归一
        ...
    ))
    except ValueError:
        # 某些 input_name 可能不在 _ALLOWED_SLOT_TYPES 对齐，跳过
        pass
    ...
    nodes.append(ComfyNodeSummary(
        node_id=str(node_id),     # 归一为 str
    ```
  - 影响: 程序化构造（非 json.loads）的 payload 可能用 int 节点键：节点 0 的
    `ComfyInputSlot.__post_init__` 中 `if not self.node_id` 对 `0` 为真 → 抛
    ValueError → 被上面的宽 except 吞掉，该节点可绑定槽位**静默丢失**；同时同一 node_id
    在 `nodes`（str）与 `input_slots`（int）两处类型不一致，按 node_id 关联时错配。
  - 建议: 循环开头 `node_id = str(node_id)` 统一归一；`ComfyInputSlot` 的 node_id 注解
    明确为 str 并做 `isinstance` 校验。
  - 置信度: 中

- [AUD-23104] [Suggestion] `content_hash` 计算对非 JSON 兼容 payload 抛未处理的 TypeError，外部数据校验不完整
  - 位置: src/features/ai_workflow/adapters/comfyui_models.py:565-568
  - 证据:
    ```python
    normalized = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    content_hash = "sha256:" + hashlib.sha256(
        normalized.encode("utf-8")
    ).hexdigest()
    ```
  - 影响: `from_api_format` 只验证了顶层/节点/inputs 三层 dict 结构；inputs 的**值**未验证。
    含非 JSON 序列化对象（set、自定义对象）或嵌套 dict 混用 int/str 键的 payload 会让
    `json.dumps(sort_keys=True)` 抛裸 TypeError（而非本模块一贯的 ValueError 契约），
    违反"外部数据须验证"的铁律，调用方难以按领域错误处理。
  - 建议: 包一层 try/except TypeError 转成带定位信息的 ValueError；或校验失败时降级为
    对 repr 的哈希并显式标注 hash 方式。
  - 置信度: 中

- [AUD-23105] [Suggestion] `base_url` 校验不拒绝 userinfo，`https://user:key@host` 可把凭证内联进持久化配置
  - 位置: src/features/ai_workflow/adapters/comfyui_models.py:145-157
  - 证据:
    ```python
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(...)
    if not parsed.netloc:
        raise ValueError(f"base_url missing host: {url!r}")
    ```
  - 影响: 模块红线声明"不存 API key 原文"（§0 第四条），但 URL userinfo（用户名:密码或
    key）可随 base_url 通过校验，并经 `to_dict()` → `ComfyProfileRegistry.save_to_config`
    明文写入用户配置，绕开 credential_ref 机制。
  - 建议: `_validate_base_url` 中显式拒绝 `parsed.username is not None or
    parsed.password is not None`，提示改用 credential_ref。
  - 置信度: 中

### 通过项
- 线程安全：纯 frozen dataclass 常量契约，无共享可变状态（raw_payload 例外已单列 AUD-23102）
- GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归：不适用（模块声明并核实无 moderngl/PyQt6 import，无渲染代码）
- 异常路径：各 dataclass `__post_init__` 用显式 ValueError 拒绝非法 kind/state/progress/artifact_kind，NaN progress 亦被拒绝
- 输入验证（部分）：`from_api_format` 拒绝非 dict / 空 payload / 缺 class_type / inputs 非 dict；`ComfyOutputArtifact.content_hash` 必填、kind 受控不含可执行
- 状态提交一致性：schema_version 字段齐全，to_dict/from_dict 对 Profile/Slot/Binding/JobRef 可 roundtrip（Snapshot 的 raw_payload 不 roundtrip 为文档化的刻意行为，:639-641）
- 性能模式：无热路径，静态解析 O(n)

---

## src/features/ai_workflow/adapters/comfyui_node_specs.py (行数: 107)

### 发现

（无独立发现。与 generation_node_specs.py 共享的 executor_id 语义问题见 AUD-23117，此处交叉引用。）

### 通过项
- 纯静态 spec 声明，无执行逻辑、无可变状态；线程安全 / GPU / GLSL·HDR / 信号维度不适用或无风险
- 红线核对：`params_schema` 顶层 `additionalProperties=False`，未暴露任意 class_type 改写；LoRA/ControlNet 等 opaque 字段只进 `bound_inputs` 命名空间（:84-91）
- `network_required=True` + `estimated_cost=0.3 > 0` 满足模块声明的云/本机确认红线（:60）
- spec_id 命名 / category / executor_id / retry_policy 均通过 `WorkflowNodeSpec.__post_init__` 受控校验（core/node_specs.py:172-185）
- 注册经 `NodeSpecRegistry.register` 幂等处理（同版本去重）
- executor_id=TTS_PROVIDER 的语义问题归并到 AUD-23117

---

## src/features/ai_workflow/adapters/comfyui_profiles.py (行数: 92)

### 发现

- [AUD-23106] [Major] `load_from_config` 宽泛 `except Exception` 且零日志，坏 profile 条目被静默丢弃
  - 位置: src/features/ai_workflow/adapters/comfyui_profiles.py:78-84
  - 证据:
    ```python
    for item in raw_profiles:
        if not isinstance(item, dict):
            continue
        try:
            profile = ComfyConnectionProfile.from_dict(item)
        except Exception:  # noqa: BLE001 — 单条坏配置不阻断整体加载
            continue
    ```
  - 影响: 违反 .clinerules 铁律"不得宽泛 except 掩盖领域错误"。`from_dict` 抛出的
    ValueError（如 cloud profile 缺 credential_ref、base_url 非法）属于领域错误，被
    `Exception` 一并吞掉且无任何日志——用户配置的云 profile 会在下次启动时无声消失，
    运行时表现为 "profile not registered"，无法排查。也掩盖了 TypeError 等程序性错误。
  - 建议: 收窄为 `except (ValueError, KeyError, TypeError)` 并至少 `logger.warning`
    记录被丢弃条目的 profile_id 与原因。
  - 置信度: 中（丢弃单条坏配置的意图有注释说明，但零日志使其成为明确风险）

- [AUD-23107] [Suggestion] `save_to_config` / `load_from_config` 的 `config` 参数为 `Any`，缺协议/接口注解
  - 位置: src/features/ai_workflow/adapters/comfyui_profiles.py:65, 75
  - 证据:
    ```python
    def save_to_config(self, config: Any) -> None:
    ...
    def load_from_config(self, config: Any) -> None:
    ```
  - 影响: duck-typed 依赖（`set`/`get`）无静态检查；调用点传错对象（如传 dict）要到运行时
    才炸，IDE/mypy 无法提前发现。
  - 建议: 定义 `Protocol`（含 `set(key, value, auto_save)` / `get(key, default)`）替代 `Any`。
  - 置信度: 高

### 通过项
- 状态一致性：`remove` 同步清理悬空 active id（:37-40）；`load_from_config` 对失效 active id 回退空串（:86-89）；`set_active` 拒绝未注册 id
- 凭据红线：本类只存 credential_ref 引用，原文校验由 `ComfyConnectionProfile.__post_init__` 保证（云必填）
- 线程安全：实例仅由 UI/控制器线程构造使用（ai_editing_mixin.py:775 单点构造），当前无跨线程共享；无信号/GPU/GLSL 维度
- 持久化写序：profiles `auto_save=False` + active `auto_save=True` 合并为一次落盘

---

## src/features/ai_workflow/adapters/composite_adapter.py (行数: 599)

### 发现

- [AUD-23108] [Major] `fetch_result` SUCCESS 路径访问 `self._svc._jobs` 私有属性，破坏自述的 duck-type 替换契约
  - 位置: src/features/ai_workflow/adapters/composite_adapter.py:456-458, 496-498
  - 证据:
    ```python
    # render_service duck-types FakeCompositeRenderService（submit / cancel / get_job）
    self._svc = render_service
    ...
    # SUCCESS：取 output_asset_id（来自 _FakeRenderJob）
    job_obj = self._svc._jobs[ref.render_job_id]  # type: ignore[attr-defined]
    out = job_obj.output_asset_id
    ```
  - 影响: 模块 docstring 承诺"生产环境替换为真实 Composite render service，接口一致"，
    但成功取结果这一核心路径依赖 Fake 服务的**私有** `_jobs` 字典与内部 job 对象的
    `output_asset_id` 属性。真实服务一旦没有 `_jobs`，渲染成功瞬间抛 AttributeError
    （成功路径崩溃，比失败路径更糟），且 `# type: ignore` 已自证契约破裂。
  - 建议: 在服务接口中增加公开的 `get_output(render_job_id) -> str`（或在
    `CompositeRenderJobRef` 之外定义带 output 的结果类型），adapter 只走公开 API。
  - 置信度: 高

- [AUD-23109] [Suggestion] `quality` / `range_spec` / `import_strategy` 未按各自 `ALL` 枚举校验（Request 与 JobRef 均缺）
  - 位置: src/features/ai_workflow/adapters/composite_adapter.py:260-269, 157-167
  - 证据:
    ```python
    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("request_id cannot be empty")
        # 至少要有输入（asset_refs / selection_set）或显式 whole_timeline
        if not self.input_asset_ids and not self.selection_set_id:
            ...
    ```
    （`CompositeRenderQuality.ALL` / `CompositeRenderRange.ALL` /
    `CompositeImportStrategy.ALL` 定义于 :110-137 但从未用于校验；JobRef 只校验
    id/state/progress。）
  - 影响: `quality="ultra"`、`import_strategy="直接进timeline"` 等非法值被静默接受并流入
    job ref 与下游服务；同类枚举在 comfyui_models.py 中都有 `__post_init__` 校验，此处
    契约不一致，与"输入验证"维度要求相悖。
  - 建议: 两个 `__post_init__` 补 `if self.quality not in CompositeRenderQuality.ALL` 等
    三项检查；`range_spec=CUSTOM` 时校验 `0 <= range_in_frame <= range_out_frame`。
  - 置信度: 高

- [AUD-23110] [Suggestion] `poll_render` / `fetch_result` / `cancel_render` 对未知 render_job_id 抛裸 KeyError，与 `submit_render` 的异常包装不一致
  - 位置: src/features/ai_workflow/adapters/composite_adapter.py:354-356, 471-472, 477, 510-511
  - 证据:
    ```python
    def get_job(self, render_job_id: str) -> CompositeRenderJobRef:
        j = self._jobs[render_job_id]
        return self._to_ref(j)
    ...
    def poll_render(self, ref: CompositeRenderJobRef) -> CompositeRenderJobRef:
        return self._svc.get_job(ref.render_job_id)
    ```
  - 影响: 过期/重启后丢失的 job id 会让 poll/cancel/fetch 直接抛 KeyError 而非领域错误；
    只有 `submit_render`（:460-469）做了 `CompositeSubgraphError(SUBMIT_FAILED)` 包装，
    调用方无法统一按 error code 分支处理取消/恢复场景。
  - 建议: `get_job`/`cancel` 对缺失 id 抛 `CompositeSubgraphError`（可增
    `JOB_NOT_FOUND` 码），或在 adapter 三个方法里统一 try/except KeyError 包装。
  - 置信度: 高

- [AUD-23111] [Suggestion] `fetch_result` 把 QUEUED/RUNNING 的 job 映射为 `error_code=RENDER_FAILED`，"未就绪"被误标为终态失败
  - 位置: src/features/ai_workflow/adapters/composite_adapter.py:490-495
  - 证据:
    ```python
    if latest.state != CompositeRenderJobState.SUCCESS:
        return CompositeSubgraphResult(
            request_id=req.request_id, succeeded=False,
            error_code=CompositeSubgraphErrorCode.RENDER_FAILED,
            error_message=f"render not ready (state={latest.state})",
        )
    ```
  - 影响: 调用方只看 `error_code` 分支时（消息字符串不可靠），仍在排队/渲染中的 job 会被
    当作 RENDER_FAILED 终态失败处理，可能触发 retry_policy=fail 的整条 workflow 失败。
  - 建议: 为"未就绪"增加独立结果形态（如 `succeeded=False, error_code=""` + `pending=True`
    字段，或新增 NOT_READY 码），与真失败区分。
  - 置信度: 中

- [AUD-23112] [Suggestion] 节点 spec 默认 `range_spec=WHOLE` 与 `CompositeSubgraphRequest` 默认 `SELECTION` 不一致
  - 位置: src/features/ai_workflow/adapters/composite_adapter.py:255, 558-562
  - 证据:
    ```python
    range_spec: str = CompositeRenderRange.SELECTION   # Request 默认
    ...
    default_params={
        "quality": CompositeRenderQuality.PROXY,
        "range_spec": CompositeRenderRange.WHOLE,       # spec 默认
    ```
  - 影响: 同一概念两处默认值相反：从节点参数构造的请求默认渲染整条时间线，而直接构造
    Request 默认只渲染选中区间——行为取决于构造路径，易引入"意外渲染全长"的成本/时间差异。
  - 建议: 统一两处默认值（建议以 spec 的 default_params 为单一事实来源，Request 去掉
    默认或改为必填）。
  - 置信度: 低（两处默认值不一致是事实；具体行为差异取决于 executor 构造路径，本仓库
    内暂无生产 executor 消费此 spec）

### 通过项
- 取消路径（§10.3.6）：`cancel` 对终态 job 是 no-op（:423-428）；取消/失败不产生半写入——模块不写 timeline，`timeline_write_required` 保持默认 False（三处 spec 均未开启）
- GPU 边界：全文件无 moderngl/PyQt6/composite.core import，端口类型为 asset_ref/render_job_ref，不跨图传 texture（与红线一致）
- GRAPH_STALE / GRAPH_NOT_FOUND：submit 前 hash 校验（map 精确匹配优先，集合存在性兜底），错误码语义正确（:360-384）
- `submit_render` 异常包装正确：CompositeSubgraphError 透传、其余包成 SUBMIT_FAILED 并链上原异常
- 状态提交一致性：JobRef/Request/Result 均可 to_dict 序列化，id 类字段必填校验齐全
- 线程安全：FakeCompositeRenderService 的 `_jobs` 无锁，但该类自述为 mock 且当前仅测试使用（已 grep 核实生产无引用）；生产并发安全应由真实服务负责，此处不计发现

---

## src/features/ai_workflow/adapters/generation_node_specs.py (行数: 363)

### 发现

- [AUD-23113] [Major] `register_generation_node_specs` 未接入生产装配，6 个生成节点在应用内不可达（仅测试调用）
  - 位置: src/features/ai_workflow/adapters/generation_node_specs.py:353-356（定义）；缺失点 core/node_specs.py:457-485
  - 证据:
    ```python
    def register_generation_node_specs(registry: NodeSpecRegistry) -> None:
        """注册 6 个生成节点 spec 到 :class:`NodeSpecRegistry`。"""
        for spec in GENERATION_SPECS:
            registry.register(spec)
    ```
    而 `register_official_specs`（core/node_specs.py:469-485）只注册 M4 16 节点 +
    `register_composite_node_specs` + `register_comfyui_node_specs`，不含 generation；
    生产唯一装配点 dock_layout_controller.py:700-701 只调 `register_official_specs`；
    全库 grep 显示 `register_generation_node_specs` 仅被 tests/ 引用。
  - 影响: 文生图/图生图/Inpaint/文生视频/图生视频/Upscale 六个 P6.5 节点在运行时画布
    Tab 搜索中不存在（功能整块未上线），与注释"生产 wiring"预期不符；属功能接线缺陷。
  - 建议: 在 `register_official_specs` 中按 composite/comfyui 同模式追加
    try/except 注册 generation specs（或在其注释中明确"暂不接线"的原因与计划）。
  - 置信度: 高

- [AUD-23114] [Major] `inpaint` / `image_to_video` / `upscale` 三个 spec 完全没有 `params_schema`，违反本模块"每个字段必须有 JSON Schema"红线
  - 位置: src/features/ai_workflow/adapters/generation_node_specs.py:102-113, 128-139, 141-151
  - 证据:
    ```python
    WorkflowNodeSpec(
        spec_id="topos.generation.inpaint",
        ...
        required_capabilities=("image_gen",),
        executor_id=NodeExecutorId.TTS_PROVIDER, resource_lane="generation_local",
        estimated_cost=0.3, network_required=True,
    ),   # 无 params_schema / default_params —— image_to_video、upscale 同
    ```
    而模块红线（:30-31）要求"每个字段必须有 JSON Schema + 单位/范围 + cache/upload/cost
    影响标注"。
  - 影响: params_schema 的生产消费方是 UI（workflow_inspector.py:86 / workflow_form_page.py:104
    按 schema 渲染控件）且无 validator 兜底——这三个节点在 Inspector 中渲染不出任何参数控件，
    seed/denoise/motion 等无法配置；同时 `additionalProperties=False` 的"未知字段不进核心
    schema"防线对它们不存在，任意参数 dict 均可通过。
  - 建议: 为三个节点补 `GenerationControlsSchema` 能力组工厂（inpaint 需 denoise/mask
    相关字段；upscale 需 scale_factor 等）并接入 `params_schema=`。
  - 置信度: 高

- [AUD-23115] [Suggestion] `_FieldMeta.range` 声明 `Tuple[float, float]` 却填入 int 值，且 `(0.0, 0.0)` 哨兵与真实 0..0 范围冲突
  - 位置: src/features/ai_workflow/adapters/generation_node_specs.py:172, 184-186, 256-257, 266
  - 证据:
    ```python
    range: Tuple[float, float] = (0.0, 0.0)   # 范围（min, max）；空 = 无限制
    ...
    if self.range != (0.0, 0.0):
        d["minimum"] = self.range[0]
        d["maximum"] = self.range[1]
    ...
    ("width", _FieldMeta(
        type="integer", unit="pixel", range=(64, 8192), default=1024,
    ```
  - 影响: int 元组与 float 注解不符（mypy 严格模式报错）；更实质的是哨兵语义——
    某字段若合法范围恰为 `(0, 0)` 会被判为"无限制"而静默省略 min/max，且 `range` 覆写
    内建名。当前能力组无 0..0 字段，属埋雷。
  - 建议: 改为 `Optional[Tuple[float, float]] = None` 表达"无限制"，字段名换 `value_range`；
    int 型字段用 `Tuple[int, int]` 或统一 float。
  - 置信度: 中

- [AUD-23116] [Suggestion] `resolution` 为自由字符串，无 pattern 校验
  - 位置: src/features/ai_workflow/adapters/generation_node_specs.py:327-330
  - 证据:
    ```python
    ("resolution", _FieldMeta(
        type="string", unit="", default="1280x720",
        cache_impact=True, cost_impact=True,
        description="output resolution (WxH)",
    )),
    ```
  - 影响: "abc"、"1280" 等任意串通过 schema；`_FieldMeta.to_json_schema` 对 string 类型
    不输出 pattern/enum，下游 provider 收到后行为未定义，错误延迟到生成请求阶段才暴露。
  - 建议: 增加 `pattern: "^\\d{2,5}x\\d{2,5}$"`（或 enum of presets），并在
    `_FieldMeta` 支持输出 pattern。
  - 置信度: 中

- [AUD-23117] [Suggestion] 图像/视频生成节点复用 `executor_id=TTS_PROVIDER`，executor 语义与能力错配（comfyui_node_specs.py 同）
  - 位置: src/features/ai_workflow/adapters/generation_node_specs.py:83, 97, 111, 124, 137, 149；src/features/ai_workflow/adapters/comfyui_node_specs.py:59
  - 证据:
    ```python
    executor_id=NodeExecutorId.TTS_PROVIDER, resource_lane="generation_local",
    required_capabilities=("image_gen",),
    ```
  - 影响: 注释说明是刻意"复用 generation provider lane"，但 TTS_PROVIDER 字面含义与
    image_gen/video_gen 能力（由 required_capabilities 单独声明）不一致：审计/成本统计/
    权限展示按 executor 分组时会把图片视频生成计入 TTS 通道，误导排查；未来若对
    TTS_PROVIDER 增加音频专属调度逻辑会意外波及生成节点。
  - 建议: 在 `NodeExecutorId` 增加 `GENERATION_PROVIDER`（或改用 WORKFLOW_RUNNER），
    迁移这 7 处引用；短期至少在 NodeExecutorId 注释中写明该 lane 被 image/video 复用。
  - 置信度: 中

### 通过项
- 线程安全：`GENERATION_SPECS` 模块级常量、dataclass frozen，加载后只读
- GPU / GLSL·HDR / 信号维度不适用：无渲染代码、无 Qt 对象；输出类型为 media_generation_ref/asset_ref，不输出 texture（红线满足）
- 红线核对：有 schema 的节点均 `additionalProperties=False`（:235）；未暴露 class_type/node_type 改写参数（测试 test_generation_node_specs.py:244-245 亦断言）；所有生成节点 `timeline_write_required` 保持默认 False、network_required=True、estimated_cost>0
- 模块级 `GENERATION_SPECS = _generation_specs()` 在 schema 类定义后求值，前向引用无运行时问题（:155 注释与 :350 实际顺序核实一致）
- 类型注解：公开 API 注解完整（`__all__` 导出三项均有显式类型）

---

## 审计方法备注

- 五个文件全部完整 Read（无跳读）；核心依赖 node_specs.py、测试 test_composite_adapter.py、
  生产装配点 dock_layout_controller.py / ai_editing_mixin.py 相关片段已 Read 核实。
- "生产未接线"（AUD-23113）与 "params_schema 仅 UI 消费"（AUD-23114 影响）基于全库
  grep（排除 __pycache__ 与 tests 的生产引用检索）。
- 无 Critical：未发现可致崩溃/数据丢失/渲染错误的路径（AUD-23108 的 AttributeError 发生在
  mock→生产替换的假设场景，且该 adapter 当前无生产调用方，故定 Major）。
