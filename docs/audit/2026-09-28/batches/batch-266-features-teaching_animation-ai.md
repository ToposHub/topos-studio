# batch-266 审查报告 — src/features/teaching_animation/ai/（7 文件，2,739 行）

编号区间：AUD-26600–AUD-26699。审查日期：2026-09-28。
说明：本批次均为 Qt-free 纯数据/契约模块（多个带 DORMANT 标记），不涉及 OpenGL、GLSL、HDR 或 Qt 信号，相关维度按 N/A 记入通过项。GPU/渲染维度全部不适用。

## src/features/teaching_animation/ai/__init__.py (行数: 610)

### 发现

- [AUD-26600] [Major] `__all__` 含 40 个未定义名称：已删除的 job_dag / lane_scheduler 模块导出残留 + 未导入的 `TeachingAIContextProvider`
  - 位置: src/features/teaching_animation/ai/__init__.py:491-504（另见 446、133-135）
  - 证据:
    ```python
    # （133-135 行注释）(The dormant Job DAG / scheduler lanes modules
    # were removed in the 2026-08 audit cleanup.)
    ...
    "TeachingAIProposalError", "TeachingAIContextProvider", "TeachingAIProviderDescriptor",
    ...
    "MotionGraphicJobDAG", "MotionGraphicJobNode", "MotionGraphicJobRun",
    "NodeRunState", "build_canonical_dag", "new_run",
    "PHASE_ORDER", "AUTO_KINDS", "APPROVAL_GATE_KINDS",
    ...
    "MotionGraphicLaneScheduler", "LaneConfig", "LaneSlot",
    "DEFAULT_KIND_LANE", "DEFAULT_LANE_CONFIGS", "ALL_LANES",
    ```
  - 影响: AST 核对确认 `__all__` 共 398 项，其中 40 项（`MotionGraphicJobDAG`、`KIND_*`、`NODE_*`、`LANE_*`、`MotionGraphicLaneScheduler`、`TeachingAIContextProvider` 等）在本模块从未导入——job_dag/lane_scheduler 模块已在 2026-08 清理中删除，`TeachingAIContextProvider` 虽存在于 context_provider.py:19 但未导入。任何 `from ...teaching_animation.ai import *` 会直接 AttributeError，按公开 API 名称逐名导入（如 `from ...ai import MotionGraphicJobDAG`）会 ImportError；这批名称是对外文档化的契约，当前是坏契约。
  - 建议: 从 `__all__` 删除全部 40 个已废弃名称；若 `TeachingAIContextProvider` 仍属公开 API，则在顶部 `from .context_provider import TeachingAIContextProvider` 补上。可加一条 lint/测试断言 `__all__ ⊆ dir(module)`。
  - 置信度: 高（静态 AST 核对 + 全库 grep 确认这些名称无其他定义处）

### 通过项
- 纯 re-export 聚合层，无运行时逻辑；其余 358 个 `__all__` 名称与导入一一对应
- 导入分组与注释（B0–B5 分层）清晰，无循环导入风险迹象（各子模块仅向下依赖）
- 线程安全 / GPU 生命周期 / GLSL·HDR / 信号防递归 / 异常路径：N/A（无逻辑）

## src/features/teaching_animation/ai/action_matrix.py (行数: 566)

### 发现

- [AUD-26601] [Suggestion] `evaluate_plan` 对 `Iterable` 二次消费，生成器输入时返回占位元数据
  - 位置: src/features/teaching_animation/ai/action_matrix.py:534-554
  - 证据:
    ```python
        for action in actions:
            decision = self.evaluate(action)
            if not decision.allowed:
                return decision
        last_action = next(reversed(list(actions)), None)
        if last_action is None:
            return MotionGraphicPolicyDecision(
                allowed=True,
                action=MotionGraphicAction.READ_PROJECT,
    ```
  - 影响: 参数类型为 `Iterable[MotionGraphicAction]`。若传入生成器/迭代器，for 循环耗尽后 `list(actions)` 为空，`last_action` 恒为 `None`，放行决策的 `action` 字段错误地报告为占位 `READ_PROJECT`、permission_tier 报为 AUTO——调用方（审计/UI）会拿到与实际执行的最后一个动作不符的元数据。列表输入不受影响。
  - 建议: 函数入口 `actions = list(actions)` 物化一次后再使用。
  - 置信度: 高（Python 迭代器语义确定；影响仅限元数据字段）

- [AUD-26602] [Suggestion] `default_action_matrix()` 声称 immutable，但共享实例的 `profiles` 字典可被就地注入/篡改
  - 位置: src/features/teaching_animation/ai/action_matrix.py:345-347, 374-379
  - 证据:
    ```python
    profiles: Mapping[MotionGraphicAction, MotionGraphicActionProfile] = field(
        default_factory=lambda: dict(_ACTION_PROFILES)
    )
    ...
    def default_action_matrix() -> MotionGraphicActionMatrix:
        """Return the shared default matrix instance (immutable)."""
        return _DEFAULT_MATRIX
    ```
  - 影响: frozen dataclass 只冻结属性赋值，不冻结内部 dict。`default_action_matrix().profiles[action] = <宽松 profile>` 会就地修改全局共享实例，后续所有持有者（evaluate 的 fail-closed 检查 `action not in self._matrix.profiles`）都被同一份被篡改的矩阵放行——"未分类动作默认拒绝"的 fail-closed 契约可被无痕绕过。
  - 建议: 将 `profiles` 包成 `types.MappingProxyType`（default_factory 中 `MappingProxyType(dict(_ACTION_PROFILES))`），或每次返回深拷贝。
  - 置信度: 中（机制确定；需调用方主动误用才触发，且模块 DORMANT）

### 通过项
- fail-closed 设计落实：`evaluate` 对未分类动作先查 `profiles` 拒绝并给出稳定 diagnostic_code（action_matrix.py:453-461）
- ALWAYS_CONFIRM 不被 batch grant 覆盖的逻辑正确（517-532）
- 输入验证：预算、云上传、付费开关前置检查顺序合理；类型注解完整
- 线程安全：模块为无共享可变状态的纯数据/策略对象（explicit_grants 为实例私有），无跨线程共享默认实例的锁需求

## src/features/teaching_animation/ai/advanced_capabilities.py (行数: 390)

### 发现

- [AUD-26603] [Major] `compatibility_issues` 的 codec 条件逻辑近似失效，`export_backends` 白名单对编码从不生效
  - 位置: src/features/teaching_animation/ai/advanced_capabilities.py:225-228
  - 证据:
    ```python
        if preview_backend not in compat.preview_backends:
            issues.append(f"预览后端 {preview_backend!r} 不支持该能力")
        if codec not in compat.export_backends and not compat.supports_codec(codec):
            issues.append(f"导出编码 {codec!r} 不支持该能力")
    ```
  - 影响: `supports_codec(codec)` 即 `codec not in incompatible_codecs`。条件为 `A and not B`：只要 codec 不在显式黑名单里（绝大多数 capability 的 `incompatible_codecs` 为空），`not supports_codec(codec)` 为 False，整条永不触发——`export_backends=("mp4",)` 的白名单（如 CAP_CHARACTER_LIPSYNC、CAP_3D_CAMERA）对 h264/vp9/webm 等真实编码一律放行。与上一行 preview 后端的严格白名单检查明显不对称。模块自述目标"refuse up front rather than failing mid-render"在导出编码维度落空：不兼容的编码组合会漏到渲染中途才失败。
  - 建议: 明确语义后改为 `codec not in compat.export_backends or not compat.supports_codec(codec)`（白名单+黑名单都生效），或统一 namespace（export_backends 与 codec 分属不同词汇表时不应混在一个条件里比较）。
  - 置信度: 中（行为可从代码严格推出；"预期语义"存在 namespace 混用歧义，但当前写法使第一个子句几乎恒为冗余是确定的）

### 通过项
- `AdvancedCapability.__post_init__` 对 capability_id / fallback_strategy / stability_tier / schema_version 的封闭词汇验证完整（143-156）
- registry `require()` 对未注册能力抛 KeyError，符合"未申报特性不得改变行为"的设计
- 数据类均为 frozen + to_dict 拷贝导出；类型注解完整
- 线程安全：registry 无锁但为实例级 append-only 字典，无跨线程共享承诺（与 ArtifactLinkIndex 不同，未宣称线程安全）

## src/features/teaching_animation/ai/approval_scope.py (行数: 410)

### 发现

- [AUD-26604] [Suggestion] `validate()` 用宽泛 `except Exception` 吞掉编程错误，与 .clinerules"不得宽泛 except"铁律冲突
  - 位置: src/features/teaching_animation/ai/approval_scope.py:316-327
  - 证据:
    ```python
        try:
            with self._lock:
                record = self._records.get(grant_id)
                if record is None or record.revoked:
                    return False
                if record.grant.single_use and record.used:
                    return False
                if record.grant.is_expired_at(self._time_fn()):
                    return False
                return record.grant.scope.matches(expected_scope)
        except Exception:  # noqa: BLE001 - validate must never raise
            return False
    ```
  - 影响: 虽有 noqa 注释说明意图（validate 永不抛出），但该 except 同样吞掉 `matches()`/`_time_fn` 中的 TypeError/AttributeError 等实现 bug，掩盖领域错误且表现为"确认无效"的假象，排查困难。失败方向是安全侧（return False），无越权风险。
  - 建议: 收窄为领域异常类型，或将 `matches` 调用移出 try（纯函数无 I/O），只保护时间函数调用；至少记录 debug 日志。
  - 置信度: 中（违规事实确定，实际触发需上游 bug）

- [AUD-26605] [Suggestion] `time_fn: Any = None` 类型注解过宽，丢失可调用签名契约
  - 位置: src/features/teaching_animation/ai/approval_scope.py:231
  - 证据:
    ```python
    def __init__(self, *, time_fn: Any = None) -> None:
        self._time_fn = time_fn or time.time
        self._records: dict[str, _GrantRecord] = {}
        self._lock = RLock()
    ```
  - 影响: 注入方类型错误（如传入返回 str 的时间函数）只有运行到过期比较时才暴露（`now >= self.expires_at` 抛 TypeError），静态检查完全失守；本模块其余注解都很完整，此处是明显洼地。
  - 建议: `time_fn: Callable[[], float] | None = None`（from typing import Callable）。
  - 置信度: 高

### 通过项
- 线程安全：全部状态变更（issue/consume/revoke/purge）在 RLock 内，`consume` 的"校验通过才标记 used"原子语义正确，过期 grant 在锁内弹出（265-309）
- scope 绑定校验完整：`__post_init__` 强制 project_id/brief_id/brief_revision>=1/brief_hash/insert_frame>=0（87-97）
- `matches` 的 None=不受约束语义与 project/brief/revision 硬匹配边界清晰，subset 检查方向正确（放宽侧为受约束方）
- 状态一致性：grant 单次使用 + revoke 生命周期 + purge_expired 无残留泄漏
- 异常路径：`consume` 校验顺序（存在→撤销→过期→scope）失败均不消耗 grant，符合"过期重确认可重试"设计

## src/features/teaching_animation/ai/artifact_links.py (行数: 283)

### 发现

- [AUD-26606] [Suggestion] `label` 参与 `ArtifactRef` 相等性/哈希，同一对象的索引集合会出现重复节点
  - 位置: src/features/teaching_animation/ai/artifact_links.py:64-76, 207-215
  - 证据:
    ```python
    @dataclass(frozen=True)
    class ArtifactRef:
        """A typed pointer to one object in the Motion Graphic pipeline."""
        kind: str
        ref_id: str
        label: str = ""
    ...
            outgoing = self._outgoing.setdefault(source_key, set())
            for target in link.targets:
                outgoing.add(target)
                target_key = (target.kind, target.ref_id)
                self._incoming.setdefault(target_key, set()).add(source)
    ```
  - 影响: 索引键是 `(kind, ref_id)`，但集合成员是含 label 的完整 `ArtifactRef`。两次 `link()` 指向同一目标（label 一个为空、一个为"主标题"）时，`targets_of`/`sources_of` 返回两个"同对象"引用，导航结果重复；下游若以 ref 对象去重会漏并。`reached_from` 因按 `(kind, ref_id)` 去重不受影响。
  - 建议: 令 `label` 不参与相等性（`field(compare=False)`，配 `eq=True`），或集合统一存键、返回时重建。
  - 置信度: 中（行为确定；实际重复概率取决于调用方是否对同一 ref 用不同 label 注册）

### 通过项
- 线程安全：`ArtifactLinkIndex` 所有读写均在 RLock 内，`reached_from` 持锁遍历，无并发撕裂
- 输入验证：`ArtifactRef.__post_init__` 强制 kind ∈ ALL_REF_KINDS 且 ref_id 非空（72-76）
- `reached_from` 递归有 visited 集合 + max_depth=8 双保险，无无限递归/性能失控风险
- 设计规则落实：索引只存 id + 预清洗 label，无 secret/path/prompt 入库；类型注解完整
- GPU/GLSL/HDR/信号：N/A

## src/features/teaching_animation/ai/asset_provenance.py (行数: 283)

### 发现

- [AUD-26607] [Suggestion] `from_dict` 对外部 JSON 字段只做部分类型收敛，provider/model_id/哈希字段原样透传
  - 位置: src/features/teaching_animation/ai/asset_provenance.py:110-128
  - 证据:
    ```python
            asset_id=str(data.get("asset_id", "")),
            source_kind=str(data.get("source_kind", "")),
            provider=data.get("provider"),
            model_id=data.get("model_id"),
            model_version=data.get("model_version"),
            prompt_hash=data.get("prompt_hash"),
            content_hash=data.get("content_hash"),
            cost_units=float(data.get("cost_units", 0.0) or 0.0),
    ```
  - 影响: 持久化/外部数据回读路径上，`asset_id/source_kind/cost_units` 等被强制收敛，但 `provider/model_id/model_version/prompt_hash/content_hash/license_url` 未收敛——JSON 里是数字/嵌套对象时类型注解 `str | None` 被无声破坏，后续 `to_dict` 再落盘会把污染类型带出去；违反"外部数据须验证"铁律（程度较轻：license/import 枚举仍有 `__post_init__` 校验兜底）。
  - 建议: 对上述字段统一 `str(v) if v is not None else None` 收敛，或显式拒绝非字符串类型。
  - 置信度: 高（代码事实确定，触发依赖脏数据）

### 通过项
- 线程安全：store 全方法持 RLock；append-only + `latest` 反向扫描语义正确
- 输入验证：`__post_init__` 校验 asset_id 非空、license/import 封闭枚举、cost_units>=0（73-81）
- `blocking_for_export` 对未知资产构造 UNKNOWN/PENDING 占位记录并阻断，fail-closed 正确（233-240）
- `total_cost` 按 asset_id 去重取最新记录，避免历史版本重复计费（242-251）
- 不可变记录 + 追加式状态更新保留审计链，符合 provenance 设计；类型注解完整（除上条）

## src/features/teaching_animation/ai/audio_plan.py (行数: 197)

### 发现

- [AUD-26608] [Major] `issues()` 音频许可检查只挡 `"missing"`，与 quality_gate/asset_provenance 的许可口径不一致
  - 位置: src/features/teaching_animation/ai/audio_plan.py:57, 180-181
  - 证据:
    ```python
        license_state: str = "unknown"          # AudioTrack 字段，自由字符串
    ...
            if track.license_state == "missing":
                issues.append(f"音频轨 {track.track_id} 缺少许可")
    ```
  - 影响: 同管线内三处许可判定口径不一致：本函数仅 `"missing"` 阻断；quality_gate.py:204 阻断 `state in {"missing", "restricted"}`；asset_provenance 的 `may_enter_commercial_export` 要求 `LICENSE_OK + IMPORT_APPROVED + commercial scope`。结果：`restricted`、`unknown`（后者还是字段默认值）乃至拼写错误（`"Missing"`）的音频轨全部通过 AudioPlan 自检。若接线时以 `issues()` 作为音频侧 MG-18 闸门，受限/未知许可的音频会进入商业导出。license_state 为自由 str、无枚举校验加剧了该风险。
  - 建议: license_state 复用 asset_provenance 的 `ALL_LICENSE_STATES` 并在 `AudioTrack.__post_init__` 校验；阻断条件对齐 quality_gate（`{"missing", "restricted"}`，或更严格地把非 OK 一律列为待审）。
  - 置信度: 中（口径不一致与漏检行为确定；实际影响取决于接线时以哪个检查为准，模块 DORMANT）

- [AUD-26609] [Suggestion] `BeatMarker` 与 `SubtitleMapping` 完全缺失 `__post_init__` 输入验证
  - 位置: src/features/teaching_animation/ai/audio_plan.py:86-99, 102-119
  - 证据:
    ```python
    @dataclass(frozen=True)
    class BeatMarker:
        """A music beat marker used to sync visual cuts."""
        frame: int
        strength: float = 1.0
        label: str = ""
    ...
    @dataclass(frozen=True)
    class SubtitleMapping:
        """Maps a subtitle cue to a narration segment + frame range."""
        subtitle_id: str
        narration_track_id: str | None
        start_frame: int
        end_frame: int
    ```
  - 影响: 同文件 `AudioTrack` 对 track_id/kind/时长/起点均有验证，这两个类却允许负数帧、`end_frame < start_frame`、空 `subtitle_id` 的非法数据入库；坏数据会流入导出质量闸门的对齐检查（CHECK_SUBTITLE_AUDIO 依赖帧区间），产生错误的通过/失败判定。
  - 建议: 补 `__post_init__`：`frame >= 0`、`subtitle_id` 非空、`0 <= start_frame <= end_frame`。
  - 置信度: 高（缺失事实确定）

### 通过项
- `AudioTrack.__post_init__` 验证完整：track_id 非空、kind 枚举、duration>0、start>=0（60-68）
- `duration_seconds` 聚合、`narration_track/music_track` 查询逻辑正确；`plan_hash` 用 canonical JSON（sort_keys+紧凑分隔符）保证确定性
- 浮点字面量规范（0.0、1.0、300.0 等均带 .0）；类型注解完整
- 线程安全/GPU/GLSL·HDR/信号防递归：N/A（纯数据模块）

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 3 | AUD-26600, AUD-26603, AUD-26608 |
| Suggestion | 7 | AUD-26601, AUD-26602, AUD-26604, AUD-26605, AUD-26606, AUD-26607, AUD-26609 |

未使用编号：AUD-26610–AUD-26699（留给复核新增发现）。
