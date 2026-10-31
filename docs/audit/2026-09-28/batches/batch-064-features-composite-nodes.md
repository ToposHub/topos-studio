# batch-064 审查报告 — src/features/composite/nodes/（base/blend/blur/camera3d/channel_merge/chroma_keyer/chromatic_aberration/clamp）

编号区间：AUD-6400–AUD-6499（实际使用 AUD-64001–AUD-64020）
审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解
跨模块佐证（只读）：`core/node_engine.py`、`core/graph_snapshot.py`、`evaluators/base_evaluator.py`、`render/render_scheduler.py`、`render3d/math_utils.py`、`render3d/camera_model.py`、其余 nodes/*.py 的 BYPASS_MAPPING/_init_port_revisions 用法。

总计：Critical 0 / Major 6 / Suggestion 14。

---

## src/features/composite/nodes/base_node.py (行数: 1325)

### 发现

- [AUD-64001] [Major] `_init_port_revisions()` 在子类赋值 outputs 之前执行，本批次 8 类节点端口修订跟踪自始为空
  - 位置: src/features/composite/nodes/base_node.py:486（对照 412-413、496-499、501-519）
  - 证据:
    ```python
    self.inputs: List[Port] = []
    self.outputs: List[Port] = []
    ...
    # Initialize parameters from PARAM_DEFS
    self._init_params_from_defs()
    # Initialize port revisions
    self._init_port_revisions()
    ```
    ```python
    def _init_port_revisions(self):
        for port in self.outputs:
            self._port_revisions[port.name] = 1
    ```
    而 `BlurNode.__init__` 等子类在 `super().__init__()` 之后才执行 `self.outputs = [Port("result", ...)]`（blur_node.py:130-132）。全库另有 7 个节点（paint_node.py:113、tracker_node.py:125、warp_node.py:73、roto_shape_node.py:83、ai_inpaint/ai_denoise/ai_generate/ai_expand）在赋值端口后手动补调 `self._init_port_revisions()`，证明作者已知该顺序缺陷。
  - 影响: `node._port_revisions` 恒为空 → `bump_output_revision()`（set_parameter 时调用）为无操作；`node_engine.py:1773` 快照 `port_revisions=dict(node._port_revisions)` 恒空 → `render_scheduler.py:1164-1184` 第 6 层"上游端口修订变化"细粒度脏检测对这些节点永不触发；且 `base_evaluator.py:680/1253-1257` 脏跳过路径复用上次求值的 cache key 而不重算输入修订，上游纹理"同 id 新内容"场景可能取到过期缓存输出。
  - 建议: `bump_output_revision()` 对缺失端口名自动登记；或在 `_init_port_revisions` 中改为惰性（首次访问/首次 bump 时按当前 outputs 补齐），使基类调用顺序不再依赖子类补救。
  - 置信度: 高（机制已核实；最终用户可见影响部分被 param_revision/params_hash 等其它脏检测层补偿）

- [AUD-64002] [Major] `from_dict` 加载 parameters 不做任何类型/范围验证（外部数据未验证）
  - 位置: src/features/composite/nodes/base_node.py:1243-1246
  - 证据:
    ```python
    loaded_params = data.get("parameters", {})
    for key, val in loaded_params.items():
        node.parameters[key] = val
    ```
  - 影响: 工程文件被手改/损坏或前向版本写入后，任意类型的值（字符串、dict、None、越界数值）直接进入运行时参数。各节点 `_do_evaluate` 随后 `float(self.parameters.get("radius_x", 5.0))` 之类强转会抛 TypeError，被 `base_evaluator.py:1034-1039` 捕获后节点静默输出空（渲染丢失）；数值型越界则直接进入 shader/矩阵计算（参见 AUD-64013/AUD-64015）。违反"外部数据须验证"铁律。
  - 建议: 按 `get_param_defs()` 的 ptype/min/max 在加载时做规范化（数值强转+钳制，option 校验合法标签，非法键归入 `_unknown_fields`）。
  - 置信度: 高（无验证为代码事实；后果经 evaluator 异常兜底，是静默渲染错误而非崩溃）

- [AUD-64003] [Major] scene payload 收集时原地改写共享 `payload.node_id` 并用宽泛 `except Exception` 吞错
  - 位置: src/features/composite/nodes/base_node.py:1043-1047、1093-1096
  - 证据:
    ```python
    if payload is not None and isinstance(payload, payload_cls):
        if hasattr(payload, "node_id"):
            try:
                payload.node_id = upstream_id
            except Exception:
                pass
        return payload
    ```
  - 影响: payload 是求值器旁路字典中的共享对象；多个聚合器（Scene3D、虚拟 Viewer 等）先后收集同一 Camera3D payload 时，`node_id` 被后者覆盖，active_camera 匹配与 GPU ID picking 的"pick_id → 节点"映射可能指向错误节点。`except Exception: pass` 同时掩盖 frozen/slots dataclass 赋值失败等领域错误，违反"不得宽泛 except"铁律。
  - 建议: node_id 在 payload 构造时确定（不可变）；或收集时复制 payload 再注入；except 收窄为 AttributeError 并记日志。
  - 置信度: 中（需多消费者场景触发；写入路径已核实）

- [AUD-64004] [Suggestion] `display_title` 的 i18n 回退使用裸 `except Exception: pass`
  - 位置: src/features/composite/nodes/base_node.py:399-405
  - 证据:
    ```python
    try:
        from src.shared.i18n.i18n import tr_or_none
        translated = tr_or_none(self.i18n_title_key)
        if translated:
            base = translated
    except Exception:
        pass
    ```
  - 影响: i18n 系统的真实错误（配置损坏、循环导入）被静默吞掉，界面悄悄回落英文，难以排查。
  - 建议: 至少 `logger.debug` 记录异常类型；回退逻辑本身保留。
  - 置信度: 高

- [AUD-64005] [Suggestion] `ParamDef.set_keyframe_track` 变异类级共享的 PARAM_DEFS 实例（危险死代码）
  - 位置: src/features/composite/nodes/base_node.py:278-286（对照 377、455-458）
  - 证据:
    ```python
    def set_keyframe_track(self, track: Any) -> None:
        if track and self.is_animatable():
            self.animated = True
            self.keyframes = [kf.to_dict() for kf in track.keyframes]
    ```
    PARAM_DEFS 是类属性（`PARAM_DEFS: List[ParamDef] = []`），实例被同类型所有节点共享；456-458 行注释确认历史上正是这种"写 keyframes 到类级 PARAM_DEFS 污染所有实例"的坑。全库 grep 仅 `Node.set_keyframe_track(param_name, track)` 被调用（edit_service.py:805 等），本方法无调用方。
  - 影响: 一旦有人调用即复现跨实例污染（同类型所有节点的该参数同时变成 animated）。
  - 建议: 删除该方法，或改为返回新的 ParamDef 实例（copy-on-write）。
  - 置信度: 高（无调用方与共享事实均已核实）

- [AUD-64006] [Suggestion] `bump_output_revision(port_name)` 对未登记端口静默无操作
  - 位置: src/features/composite/nodes/base_node.py:509-511
  - 证据:
    ```python
    if port_name:
        if port_name in self._port_revisions:
            self._port_revisions[port_name] += 1
    ```
  - 影响: 端口名拼写错误或端口动态新增时修订不递增且无任何日志，下游失效信号丢失，排查困难（与 AUD-64001 叠加后完全不可见）。
  - 建议: 未命中时登记为 2 并记 warning。
  - 置信度: 高

- [AUD-64007] [Suggestion] `memory_bytes` 主深度通道与 AOV "depth" 通道计费口径不一致
  - 位置: src/features/composite/nodes/base_node.py:184-187 与 199-203
  - 证据:
    ```python
    if self.has_depth():
        # Add depth channel memory (single channel, half-float)
        base_memory += self.width * self.height * 1 * 2
    ...
        channels = 1 if str(name) == "depth" else 4
        bytes_per_channel = 4 if str(name) == "depth" else 1
    ```
  - 影响: 189 行注释说明 Scene3D 的深度为 R32F（4 bytes/px），主深度却按 half-float（2 bytes/px）计入，缓存显存预算低估深度占用，可能造成超预算驱逐策略失真。
  - 建议: 统一按实际分配格式计费（主深度也按 R32F 4 bytes/px），或加字段区分格式。
  - 置信度: 中

- [AUD-64008] [Suggestion] 旁路/直通路径直接复用上游 TextureResult 实例（共享可变对象别名）
  - 位置: src/features/composite/nodes/base_node.py:977-980（同类：blur_node.py:332-333、blend_node.py:268-271）
  - 证据:
    ```python
    if self.inputs and self.outputs:
        first_input = next(iter(inputs.values()), None)
        if first_input and first_input.is_valid:
            return {self.outputs[0].name: first_input}
    ```
  - 影响: TextureResult 是可变 dataclass；直通把上游同一实例交给所有下游消费者，任何消费者就地改 metadata（bbox/color_space/revision）都会污染上游缓存条目与其它分支。`with_updates()` 已提供 copy-on-write 惯例但直通未采用。
  - 建议: 直通处返回 `first_input.with_updates()`（或至少在团队规范中禁止就地修改 TextureResult）。
  - 置信度: 中（别名事实确凿；是否有消费者就地修改未在本批次范围内发现）

- [AUD-64009] [Suggestion] 节点默认 ID 仅取 uuid4 前 8 位（约 32 bit 熵）
  - 位置: src/features/composite/nodes/base_node.py:411
  - 证据:
    ```python
    self.id: str = node_id or str(uuid.uuid4())[:8]
    ```
  - 影响: 大图中生日碰撞概率上升（约 n²/2³³）；ID 是连接表与 `scene_payloads` 的键，碰撞会导致连线/payload 归属错乱且难以复现。
  - 建议: 取前 12 位或用 `uuid.uuid4().hex[:12]`（去掉连字符干扰）。
  - 置信度: 高（事实）/影响低

### 通过项
- GPU 资源生命周期：Node 基类不持有 GL 对象，纹理所有权交由 evaluator/cache（`_collect_owned_texture_ids` 协议），无泄漏点。
- 信号防递归：无 Qt 信号；`_on_state_changed` 回调为单向通知，无递归路径。
- 状态提交一致性：`set_parameter`/`set_expression`/`set_keyframe_track` 均正确联动 `_dirty`、`param_revision`、`bump_output_revision` 与 NodeState；`to_dict` 深拷贝参数防止序列化侧污染运行态；A-15 关键帧/表达式互斥在 set 与 load 两端一致。
- 异常与取消路径：`_do_evaluate` 抽象方法以 NotImplementedError 显式失败；evaluate 无静默吞错。
- 类型注解：完整（含 ClassVar/Optional/Tuple 等规范用法）。
- 缓存键策略文档与 `with_updates`/`aov_signature` 元数据契约实现一致。

---

## src/features/composite/nodes/blend_node.py (行数: 313)

### 发现

- [AUD-64010] [Major] MergeNode 未定义 BYPASS_MAPPING，禁用节点时默认旁路取 `iter(inputs.values())` 首项（语义未定义）
  - 位置: src/features/composite/nodes/blend_node.py:113-169（缺失处）；src/features/composite/nodes/base_node.py:959-976（契约与默认实现）
  - 证据（基类默认行为）:
    ```python
    # Default behavior: pass through first input to first output
    # This is suitable for simple filter nodes but may be wrong for
    # channel/merge nodes. They should override BYPASS_MAPPING.
    if self.inputs and self.outputs:
        first_input = next(iter(inputs.values()), None)
    ```
    基类文档明确要求 "Merge/Blend: multiple inputs to one output, should define which input to use for bypass"。全库同类节点均定义了映射（dissolve_node.py:46 `BYPASS_MAPPING = {"result": "B"}`、difference_keyer_node.py:43 `{"result": "input_a"}`、compare_node.py:39 `{"result": "A"}`、deep_merge_node.py:60），唯 MergeNode 没有。
  - 影响: 禁用 Merge 后输出取决于求值器 inputs 字典的插入顺序（A 或 B 任选其一），另一路画面直接消失；与 NUKE"禁用 merge 输出 B"惯例不符，用户禁用节点后合成结果错乱。
  - 建议: 在 MergeNode 上显式 `BYPASS_MAPPING = {"result": "B"}`（背景直通）。
  - 置信度: 高（缺失与契约均核实；具体错向取决于 evaluator 字典顺序）

- [AUD-64011] [Suggestion] 单输入直通分支忽略 `alpha_policy="output_straight"`
  - 位置: src/features/composite/nodes/blend_node.py:268-271
  - 证据:
    ```python
    # Pass through if only one input
    if input_a is None or not input_a.is_valid:
        return {"result": input_b} if input_b else {}
    if input_b is None or not input_b.is_valid:
        return {"result": input_a}
    ```
  - 影响: alpha_policy="output_straight" 时声明输出为 straight alpha，但直通返回的 TextureResult 沿用输入自身的 `alpha_premultiplied` 元数据，下游按声明语义消费会得到错误解释。
  - 建议: 直通分支也按 output_premult 决定是否走 unpremultiply dispatch，或至少修正返回结果的 `alpha_premultiplied` 标注。
  - 置信度: 高（代码事实）/影响低

- [AUD-64012] [Suggestion] 占位 TextureResult 尺寸恒取 input_b，忽略 format_policy（且 `input_b.is_valid` 判断恒真）
  - 位置: src/features/composite/nodes/blend_node.py:284-290
  - 证据:
    ```python
    return {
        "result": TextureResult(
            texture_id=-1,
            width=input_b.width if input_b.is_valid else input_a.width,
            height=input_b.height if input_b.is_valid else input_a.height,
    ```
  - 影响: 270-271 行已保证此处 input_b 有效，条件恒真属冗余；format_policy="from_a"/"larger" 时占位元数据标成 B 的尺寸，误导依赖占位信息的诊断/日志路径（真实尺寸由 GPU dispatch 重算）。
  - 建议: 按 format_policy 推导占位尺寸，或删除恒真条件并注释占位尺寸仅诊断用。
  - 置信度: 高

### 通过项
- 输入验证：opacity/mix 经 `_get_param_clamped` 钳制；mask_channel 字符串带越界回退（279 行）；blend_mode/alpha_policy 越界时 dispatcher 侧有默认值兜底。
- GLSL·HDR 正确性（节点侧契约）：add/subtract 不钳色彩、alpha 钳制由参数控制的设计在文档与 `_effect_params` 传递中一致。
- 状态提交一致性：参数变更经基类 set_parameter 正确置脏。
- marker 协议：`_effect_*` 私有键使用规范，无操作路径只返回真实 TextureResult。
- 类型注解：完整。

---

## src/features/composite/nodes/blur_node.py (行数: 382)

### 发现

- [AUD-64013] [Major] radius/radius_x/radius_y 无上限钳制，ROI/tile padding 直接按原始值展开
  - 位置: src/features/composite/nodes/blur_node.py:296-298、251-253、275-276
  - 证据:
    ```python
    radius_x = max(0.0, float(self.parameters.get("radius_x", 5.0)))
    radius_y = max(0.0, float(self.parameters.get("radius_y", 5.0)))
    radius = max(0.0, float(self.parameters.get("radius", 8.0)))
    ```
    ```python
    radius_x, radius_y = self._get_effective_radius(params)
    max_radius = int(max(radius_x, radius_y))
    expanded_roi = output_roi.expanded(max_radius)
    ```
  - 影响: ParamDef max_val=200 只约束 UI；`set_parameter`/`from_dict`（见 AUD-64002）不强制。外部数据写入 1e9 时 `output_roi.expanded(1_000_000_000)` 会把上游 ROI/瓦片 padding 放大到灾难级，导致显存分配失败或整帧渲染崩溃。另 `_do_evaluate` 的 `float()` 强转对字符串值直接抛 TypeError。
  - 建议: 在 `compute_input_roi`/`_tile_padding` 入口将半径钳制到 ParamDef 上限（或独立安全上限，如 512），与 preview 缩放逻辑共用一处钳制函数。
  - 置信度: 中（scheduler 是否二次钳制未在批次范围内验证；无钳制本身为代码事实）

- [AUD-64014] [Suggestion] `blur_type_param_visibility` 尾部死代码
  - 位置: src/features/composite/nodes/blur_node.py:196-200
  - 证据:
    ```python
    # single_types that are NOT the active type hide the scalar radius too,
    # except separable types already hide it above.
    if blur_type not in single_types and blur_type in separable_types:
        # radius already in hidden set for separable types.
        pass
    ```
  - 影响: 五分支 no-op，误导读者以为存在额外隐藏逻辑；注释与代码意图已由上方分支覆盖。
  - 建议: 删除该块及注释。
  - 置信度: 高

### 通过项
- 性能模式：preview 半径缩放（>15 时按 0.3 缩）方向保守正确；quality→sample_count 映射显式；velocity 端口 ROI 不做核 padding 的区分正确。
- 输入验证：blur_type 旧值 "directional" 双处兼容映射（_get_effective_radius 与 _do_evaluate）；motion_source 越界回退 "angle"；shutter 钳制 [0,2]；channel_mask 打包为 float 四元组。
- 零半径/零 shutter 早退直通符合 marker 协议（无 `_effect_*` 键）。
- metadata 保留：alpha_premultiplied/color_space 透传。
- 状态提交一致性 / GPU 资源生命周期 / 信号防递归：无本文件特有问题（GPU 由 dispatcher 持有；无信号）。
- 类型注解：完整（含 Set 返回注解）。

---

## src/features/composite/nodes/camera3d_node.py (行数: 351)

### 发现

- [AUD-64015] [Major] `_do_evaluate` 读取 cam_fov 无钳制（同函数内其余参数均有 max() 防御），可生成含 inf 的投影矩阵
  - 位置: src/features/composite/nodes/camera3d_node.py:249、287-295；佐证 src/features/composite/render3d/math_utils.py:33-39
  - 证据:
    ```python
    cam_fov = float(self.parameters.get("cam_fov", 50.0))
    ...
    near = max(0.001, float(self.parameters.get("cam_near", 0.1)) * unit_scale)
    far = max(near + 1.0, float(self.parameters.get("cam_far", 1000.0)) * unit_scale)
    ...
    else:
        proj = m3d.perspective(cam_fov, aspect, near, far)
    ```
    `math_utils.perspective` 只对 aspect/near/far 做防御钳制，`f = 1.0 / np.tan(fov_rad * 0.5)` 对 fov 无防护：fov=0 或 360 时 tan→0，f→inf。`set_parameter` 的镜头联动虽把计算用 fov 钳到 [1,170]（208 行），但存储值与 `_do_evaluate` 读取值均未钳制；from_dict（AUD-64002）或越界关键帧/表达式可写入 0。
  - 影响: 投影矩阵含 inf/NaN → Scene3D 整帧渲染错误（全黑/花屏），且无异常抛出难以定位。
  - 建议: `_do_evaluate` 读取处 `cam_fov = min(170.0, max(1.0, ...))`，与 set_parameter 联动钳制一致；或在 `math_utils.perspective` 内对 fov 增加与 aspect/near/far 同样的防御。
  - 置信度: 中（触发需外部越界数据；除零→inf 路径已核实）

- [AUD-64016] [Suggestion] `set_parameter` 镜头联动第二同步块 `except Exception: pass` 吞掉全部错误
  - 位置: src/features/composite/nodes/camera3d_node.py:227-228（对照第一块 180-181 已收窄为 `(TypeError, ValueError)`）
  - 证据:
    ```python
        except Exception:
            pass
        finally:
            self._syncing_lens_params = False
    ```
  - 影响: `camera_model` 导入失败、filmback_spec 对非法名称抛错等真实问题被吞，FOV↔焦距联动静默失效，用户看到两参数脱钩却无日志可查。
  - 建议: 与第一块一致收窄为 `(TypeError, ValueError, KeyError)` 并 `logger.debug` 记录。
  - 置信度: 高

### 通过项
- 信号防递归（参数联动防递归锁）：`_syncing_lens_params` 状态锁实现正确，嵌套写入经 `super().set_parameter` 直调不再进入覆盖方法，无递归/重复联动。
- 状态提交一致性：联动写入均走 `super().set_parameter`，修订/脏传播完整；undo 外层快照可整体覆盖联动副作用。
- GPU 资源生命周期：纯 numpy 计算无 GL 资源，`cleanup()` 显式空实现并说明。
- 线程安全：payload 经 `ctx.scene_payloads` 旁路按帧写入（写入点在 AUD-64003 中另行处理）。
- 单位换算：`world_unit_scale` 后 near/far/dist/ortho_scale/focus 均有下界防御；ortho 模式正确绕过 lens shift 分支。
- 类型注解：完整。

---

## src/features/composite/nodes/channel_merge_node.py (行数: 221)

### 发现

- [AUD-64017] [Suggestion] identity 直通分支仅凭 texture_id 相等判定"四输入同图"
  - 位置: src/features/composite/nodes/channel_merge_node.py:182-189
  - 证据:
    ```python
    all_inputs_same = (
        input_r and input_g and input_b and input_a and
        input_r.texture_id == input_g.texture_id and
        input_g.texture_id == input_b.texture_id and
        input_b.texture_id == input_a.texture_id
    )
    ```
  - 影响: base_node.py:60-61 缓存键策略明确警告 "Avoid treating texture_id alone as content identity; GPU ids can be reused"。deferred-release 场景下，缓存中的过期 TextureResult 与复用同一 glo 的新纹理同时在场时（元数据可能不同），会误判为同图而直通 `first_valid_input`，G/B/A 取到过期内容。
  - 建议: 判等同时比较 `revision` 与 `width/height`，或直接比较对象身份（`input_r is input_g`）。
  - 置信度: 低（需 id 复用与过期缓存并存的边界场景；风险方向已由项目自身文档确认）

### 通过项
- 输入验证：`safe_source_index`/`safe_channel_index` 对非法字符串带回退；无有效输入时返回空。
- BYPASS_MAPPING 显式定义为 `{"result": "input_r"}`，符合基类契约（对照 AUD-64010）。
- bbox/ROI：输出 bbox 按优先级取首个非空输入，ROI 对四个端口统一回传。
- marker 协议：`_effect_params` 内嵌输入纹理符合 channel_merge dispatcher 契约（channel_evaluator.py 消费）。
- GPU 资源生命周期 / 信号防递归 / 状态提交 / 异常路径：无本文件特有问题。
- 类型注解：完整。

---

## src/features/composite/nodes/chroma_keyer_node.py (行数: 127)

### 发现

- [AUD-64018] [Suggestion] tolerance/softness/edge_width/output_mode 未钳制、未强转，与同函数 screen_color 的处理不一致
  - 位置: src/features/composite/nodes/chroma_keyer_node.py:102-109
  - 证据:
    ```python
    screen_color_r = self._get_param_clamped("screen_color_r", 0.0, 1.0)
    ...
    tolerance = self.parameters.get("tolerance", 0.3)
    softness = self.parameters.get("softness", 0.1)
    despill = self.parameters.get("despill", True)
    edge_width = self.parameters.get("edge_width", 2.0)
    output_mode = self.parameters.get("output_mode", "result")
    ```
  - 影响: 外部数据（from_dict，见 AUD-64002）写入越界数值或字符串时：数值越界直接进 shader（GLSL `smoothstep(edge0, edge1, x)` 在 softness=0 即 edge0==edge1 时结果未定义，负值同理），matte 全错；字符串值在 uniform 上传时抛 TypeError。基类 `_get_param_clamped` 的文档明确此钳制用于"preventing GPU shader errors or undefined behavior"。
  - 建议: tolerance/softness 用 `_get_param_clamped(..., 0.0, 1.0)`，edge_width 钳 [0,10]，output_mode 校验在 OUTPUT_MODES 内否则回退 "result"。
  - 置信度: 高（不一致与未钳制为代码事实；shader 侧具体行为未读，按 GLSL 规范推断）

### 通过项
- 输入验证：image 无效早退返回空；screen_color 三分量钳制。
- bbox/ROI：输出 bbox 跟随 image 输入；双输入 ROI 统一。
- marker 协议：`_effect_input`/`_effect_params` 使用规范（keyer_evaluator.py 消费）。
- GPU 资源生命周期 / 信号防递归 / 状态提交 / 异常路径 / 类型注解：无本文件特有问题。

---

## src/features/composite/nodes/chromatic_aberration_node.py (行数: 121)

### 发现

（无）

### 通过项
- 输入验证：offset_r/offset_b 钳制 ±20、center 钳 [0,1]、edge_boost 钳 [0,1]，与 ParamDef 范围一致。
- 性能模式：preview 偏移减半；`_tile_padding` 按全量偏移取整声明，preview 下保守（多 pad 不失真）。
- metadata 保留：format/bbox/alpha_premultiplied/color_space 全部透传，符合缓存键策略要求。
- ROI 契约：`compute_input_roi`/`requires_roi_expansion` 与 `_tile_padding` 一致。
- marker 协议 / GPU 资源生命周期 / 信号防递归 / 状态提交 / 异常路径 / 类型注解（含 ctx/fb_manager 显式 Any 注解）：均通过。
- 说明：`cache_to_disk` 参数在本文件未被消费，经查由 bake_service.py:150 统一识别，非死参数。

---

## src/features/composite/nodes/clamp_node.py (行数: 105)

### 发现

- [AUD-64019] [Suggestion] channels 参数使用未注册的 ptype "string"（规范类型表为 "str"）
  - 位置: src/features/composite/nodes/clamp_node.py:51；对照 src/features/composite/nodes/base_node.py:224
  - 证据:
    ```python
    ParamDef("channels", "string", "rgba"),
    ```
    base_node.py:224 的类型注释为 `# "float", "int", "str", "bool", "option", "color", "file", "vec2", "vec3", "channel", "hdri"`，不含 "string"。
  - 影响: `make_default_parameters` 的 `elif self.ptype == "str"` 分支永不匹配（本例因提供 default 无实际后果），但任何按 ptype 分发的 UI 生成/校验/序列化代码对该参数走默认分支，行为不可预期。
  - 建议: 改为 `"str"`。
  - 置信度: 高（不一致为事实；当前无运行时后果）

- [AUD-64020] [Suggestion] min/max 未校验 min<=max，也未做数值强转
  - 位置: src/features/composite/nodes/clamp_node.py:86-89
  - 证据:
    ```python
    mode = self.parameters.get("mode", "min_max")
    min_val = self.parameters.get("min", 0.0)
    max_val = self.parameters.get("max", 1.0)
    channels = self.parameters.get("channels", "rgba")
    ```
  - 影响: 外部数据（AUD-64002）写入 min>max 或字符串时：钳制区间反转导致 shader 输出未定义/全空；字符串在 uniform 上传时抛 TypeError 被 evaluator 吞掉后节点渲染为空。
  - 建议: `min_val/max_val` 经 `float()` + `min<=max` 校验（反转时交换或回退默认）；channels 校验属于 {"r","g","b","a"} 子集组合。
  - 置信度: 高（无校验为代码事实）

### 通过项
- HDR 语义：本节点即项目契约中"显式钳制"的唯一合法位置，文档注释明确，无静默 clamp 问题。
- bbox/ROI：直通正确。
- marker 协议 / GPU 资源生命周期 / 信号防递归 / 状态提交 / 异常路径 / 类型注解：无本文件特有问题。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 6 | AUD-64001, AUD-64002, AUD-64003, AUD-64010, AUD-64013, AUD-64015 |
| Suggestion | 14 | AUD-64004, AUD-64005, AUD-64006, AUD-64007, AUD-64008, AUD-64009, AUD-64011, AUD-64012, AUD-64014, AUD-64016, AUD-64017, AUD-64018, AUD-64019, AUD-64020 |

合计 20 条发现。分布：base_node.py 9、blend_node.py 3、blur_node.py 2、camera3d_node.py 2、channel_merge_node.py 1、chroma_keyer_node.py 1、clamp_node.py 2、chromatic_aberration_node.py 0（通过）。
