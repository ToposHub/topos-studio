# Batch 133 审查报告：src/features/edit/ai_editing（8 文件，2,371 行）

编号区间：AUD-13300–AUD-13399。审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 /
信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失。

本批次全部为纯 Python 数据模型与校验/测量模块，无 Qt 控件、无 OpenGL/GLSL 代码，
故 GPU 资源生命周期、GLSL·HDR 正确性、信号防递归三个维度对所有文件均为不适用（通过）。

---

## src/features/edit/ai_editing/creative_assets.py (行数: 151)

### 发现

- [AUD-13300] [Major] register_package 可静默覆盖已注册 package，绕过 update_package 的影响报告契约
  - 位置: src/features/edit/ai_editing/creative_assets.py:90-91, 123-130
  - 证据:
    ```python
    def register_package(self, package: ReferencePackage) -> None:
        self._packages[package.package_id] = package
    ...
    def update_package(self, package: ReferencePackage) -> RecipeImpact:
        """更新 reference 后先保存新版本，再返回待复核的 recipe/shot 清单。"""
        existed = package.package_id in self._packages
        self._packages[package.package_id] = package
    ```
  - 影响: 类契约是"变更返回显式影响报告"（class docstring）。若调用方对已注册 id 再次走
    `register_package`（与 update_package 写法完全相同），引用该 package 的 recipes 会静默变
    stale，不产生任何 RecipeImpact，`impacted_by_package` 的复核链路被绕过，可能带着过期
    reference 直接生成。
  - 建议: `register_package` 对已存在 id 抛错或改为内部统一走 `update_package` 语义；至少在
    重复注册时记录告警。
  - 置信度: 中

- [AUD-13301] [Suggestion] ReferencePackage.to_dict 的 fingerprint 空串分支是死代码
  - 位置: src/features/edit/ai_editing/creative_assets.py:30-32, 56
  - 证据:
    ```python
    def __post_init__(self) -> None:
        if not self.package_id:
            raise ValueError("reference package_id cannot be empty")
    ...
        payload["fingerprint"] = self.fingerprint if self.package_id else ""
    ```
  - 影响: package_id 为空在构造期已必然抛错，`else ""` 分支不可达，误导读者以为存在无
    id 的合法实例。
  - 建议: 直接 `payload["fingerprint"] = self.fingerprint`。
  - 置信度: 高

### 通过项
- 线程安全：会话级内存注册表，当前消费方（context/planner 链路）为单线程访问，未见跨线程共享
- 状态提交一致性：register_recipe 对未注册 package_id 显式抛 KeyError；impact 推导为纯读
- 输入验证：package_id/recipe_id 非空在 shared generation_models 中校验
- 类型注解：完整
- GPU/GLSL/信号防递归：不适用

---

## src/features/edit/ai_editing/credential_store.py (行数: 12)

### 发现
（无）

### 通过项
- 通过（无逻辑）：标准 sys.modules shim，将本模块替换为
  `src/shared/ai_models/generation/credential_store`（已确认该实现文件存在）。历史导入路径
  `src.features.edit.ai_editing.credential_store` 继续可用，私有名亦可解析。

---

## src/features/edit/ai_editing/cross_domain.py (行数: 344)

### 发现

- [AUD-13302] [Major] DependencyGraph 每域只保留最后一个 observed ref，同域多 artifact 时 stale 漏报且依赖输入顺序
  - 位置: src/features/edit/ai_editing/cross_domain.py:99-112（dirty_clip_ids 同型 118-127）
  - 证据:
    ```python
    observed_map: dict[str, DomainArtifactRef] = {}
    for ref in observed_upstreams:
        observed_map[ref.domain] = ref     # 同域多个 artifact 时 last-wins
    stale: set[str] = set()
    for dep in self.dependencies:
        current = observed_map.get(dep.upstream.domain)
        if current and dep.is_stale(current):
            stale.add(dep.downstream_domain)
    ```
  - 影响: 若同域同时观察到旧 plan（与 dep 记录一致）和新 plan（id 不同），stale 与否取决于
    迭代顺序里谁最后写入 map：旧 plan 在后时返回"未过期"，下游继续用基于旧上游的结果——
    恰是 IX-01 要消除的单域失联问题在跨域层的复现。
  - 建议: 按 `(domain, artifact_id)` 建映射，或对同域多 ref 全量参与 is_stale 匹配；任一
    匹配到不同 artifact_id 即判 stale。
  - 置信度: 中

- [AUD-13303] [Major] RenderContract.is_consistent_with 忽略 effects_enabled/proxy_tier/time_range，预览关特效与导出开特效被判"一致"
  - 位置: src/features/edit/ai_editing/cross_domain.py:176-185
  - 证据:
    ```python
    def is_consistent_with(self, other: "RenderContract", *, tolerance: float = 0.001) -> bool:
        """Return whether two contracts would produce visually consistent output."""
        return (
            self.width == other.width
            and self.height == other.height
            and abs(self.fps - other.fps) < tolerance
            and self.color_pipeline == other.color_pipeline
            and self.transfer_function == other.transfer_function
            and self.alpha_mode == other.alpha_mode
        )
    ```
  - 影响: 类 docstring 明确 "a mismatch between preview and export is a consistency bug"
    （IX-02）。effects_enabled 不同的两份契约（预览关特效、导出全开）返回 True，一致性检查
    恰好漏掉它要抓的预览/导出偏差；time_range 不同（不同段落）同样被判一致。
  - 建议: 至少把 effects_enabled 纳入比较；proxy_tier/time_range 若有意豁免，用参数显式
    声明豁免项而非静默忽略。
  - 置信度: 中

- [AUD-13304] [Suggestion] CrossDomainScorecard.weights 未验证非负，负权重可令加权分失真
  - 位置: src/features/edit/ai_editing/cross_domain.py:288-299
  - 证据:
    ```python
    for s in self.scores:
        w = self.weights.get(s.domain, 1.0)
        value = s.human_override if s.human_override is not None else s.score
        weighted += value * w
        total_weight += w
    return weighted / total_weight if total_weight > 0 else 0.0
    ```
  - 影响: DomainScore.score 已验证 [0,1]，但 weights 无约束：负权重可把 weighted_score 拉低
    到失真，或使 total_weight ≤ 0 时静默返回 0.0，掩盖配置错误。
  - 建议: 构造/计算时校验 weight ≥ 0，负值抛 ValueError。
  - 置信度: 中

### 通过项
- 输入验证：DomainArtifactRef/ProtectedRegion/DomainScore 的 domain、kind、score 均在 __post_init__ 校验
- 状态提交一致性：全部 frozen dataclass，hash/to_dict 序列化稳定（sort_keys）
- 异常路径：校验失败显式 ValueError，无宽泛 except
- 类型注解：完整
- GPU/GLSL/信号防递归：不适用

---

## src/features/edit/ai_editing/cross_domain_cost.py (行数: 308)

### 发现

- [AUD-13305] [Major] predict_recompute_cost 把 full_recompute 按 1 个 clip 计费，全量重算成本被系统性低估
  - 位置: src/features/edit/ai_editing/cross_domain_cost.py:136-147
  - 证据:
    ```python
    dirty = compute_dirty_range(graph, changed_domain, changed_clip_ids)
    affected = dirty.clip_ids if not dirty.full_recompute else ("__all__",)
    clip_count = len(affected)
    ...
    for dep in graph.dependencies:
        ...
        count = clip_count if not dirty.full_recompute else 1
        cost = per_clip * max(1, count)
        total += cost
    ```
  - 影响: 依赖声明空 affected_clip_ids（= 整域重算）时，成本按 per_clip×1 计：全片 composite
    重算的预估成本反而低于重算 3 个 clip。`exceeds_budget` 在最需要预算控制的场景几乎必然
    不触发，tier 降级建议（lowres）也不会给出——IX-03 的成本控制在关键路径上失效。
  - 建议: full_recompute 时要求调用方提供总 clip 数（或用 dirty range 的图信息估算），
    缺失时按保守上限计费并在报告中标注"下限估计"。
  - 置信度: 高

- [AUD-13306] [Suggestion] dirty 为空时 can_skip=True 但 estimated_cost_units 仍 >0，报告自相矛盾
  - 位置: src/features/edit/ai_editing/cross_domain_cost.py:147-160
  - 证据:
    ```python
    cost = per_clip * max(1, count)      # count=0 时仍得 per_clip
    total += cost
    can_skip = False
    ...
    if dirty.is_empty:
        can_skip = True
        skip_reason = "无受影响片段"
    ```
  - 影响: 无受影响片段的域报告 can_skip=True 的同时带着非零成本，total_estimated_cost
    也被抬高，可能误触发 exceeds_budget。
  - 建议: dirty.is_empty 时 cost 置 0 并跳过累加。
  - 置信度: 高

- [AUD-13307] [Suggestion] validate_brief_against_delivery 的 platform 参数从未使用；duration_seconds 未验证有限/非负，NaN 可绕过时长上限
  - 位置: src/features/edit/ai_editing/cross_domain_cost.py:215-228, 248-255, 284-291
  - 证据:
    ```python
def validate_brief_against_delivery(
    *,
    canvas_aspect: str,
    duration_seconds: float,
    platform: str,          # 函数体内无任何引用
    ...
    if profile_max_duration is not None and duration_seconds > profile_max_duration:
    ```
  - 影响: platform 形参是死参数，暗示平台相关校验未完成；duration_seconds=NaN 时所有
    比较为 False，平台上限检查与 3 秒提醒双双静默跳过，负时长也只得到 warning 而非 error。
    brief 输入来自用户/LLM，属外部数据，须验证（铁律）。
  - 建议: 删除或使用 platform 参数；入口处校验 `math.isfinite(duration_seconds) and >= 0`，
    否则记 DURATION_INVALID error。
  - 置信度: 中

### 通过项
- 异常路径：纯函数，无吞异常
- 类型注解：完整（cost_per_clip/budget_limit 等关键字参数均标注）
- 线程安全：纯函数无共享态
- GPU/GLSL/信号防递归：不适用

---

## src/features/edit/ai_editing/decision_service.py (行数: 115)

### 发现

- [AUD-13308] [Major] accept/reject/lock 无状态迁移校验，LOCKED 决策可被 reject 覆盖，违反 TAEP §5.5 关键规则
  - 位置: src/features/edit/ai_editing/decision_service.py:47-63
  - 证据:
    ```python
    def reject(self, decision_id: str) -> EditDecision:
        """拒绝一条 decision —— 拒绝成为下一轮规划的约束。"""
        d = self._require(decision_id)
        self.decisions[decision_id] = d.with_state(DecisionState.REJECTED)
        return self.decisions[decision_id]

    def lock(self, decision_id: str) -> EditDecision:
        d = self._require(decision_id)
        self.decisions[decision_id] = d.with_state(DecisionState.LOCKED)
    ```
  - 影响: 模块 docstring 规定"锁定的 decision 在重规划时必须保留"，`record_user_override`
    也专门避开 LOCKED，但 `reject()`/`accept()` 对 LOCKED 决策照常改写状态；REJECTED 也能被
    `accept()` 复活。一次误调即可让锁定保护失效且无任何报错。
  - 建议: 定义合法迁移表（如 LOCKED 仅可解锁后迁移），非法迁移抛 ValueError 或要求显式
    unlock。
  - 置信度: 高

- [AUD-13309] [Suggestion] 已 ACCEPTED 的决策被用户 override 后从 must_preserve 中消失
  - 位置: src/features/edit/ai_editing/decision_service.py:72-77, 109-115
  - 证据:
    ```python
    if d is not None and d.state not in (DecisionState.LOCKED,):
        self.decisions[override.related_decision_id] = d.with_state(
            DecisionState.USER_OVERRIDE
        )
    ...
    "must_preserve": [d.decision_id for d in self.locked + self.accepted],
    ```
  - 影响: ACCEPTED → USER_OVERRIDE 后不再出现在 must_preserve；planner 若只读
    must_preserve，会丢掉"该处曾已被认可、现被人工取代"的保留要求，仅剩 user_overrides
    id 列表（无主体范围信息）兜底。
  - 建议: must_preserve 纳入 USER_OVERRIDE 状态，或在 planning_constraints 中把 override
    的 subject 范围并入 must_preserve。
  - 置信度: 中

### 通过项
- 异常路径：_require 对未知 id 显式 KeyError，无宽泛 except
- 状态提交一致性：docstring 明示 P2 内存态、P5+ 接 assistant_session_store 持久化（当前无持久化承诺，不算缺陷）
- 线程安全：会话级工作集，现有消费方（context_service 只读 producer）单线程
- 类型注解：完整
- GPU/GLSL/信号防递归：不适用

---

## src/features/edit/ai_editing/delivery_analyzer.py (行数: 385)

### 发现

- [AUD-13310] [Suggestion] 三处宽泛 except Exception 丢弃根因，错误码之外无任何日志
  - 位置: src/features/edit/ai_editing/delivery_analyzer.py:171-174, 313-314, 359-360
  - 证据:
    ```python
    except Exception:
        return DeliveryAnalysisResult(
            False, corrupt_frame_count=1, error_code="FRAME_DECODE_FAILED",
        )
    ...
    except Exception:
        return self._audio_failed(video, "LOUDNESS_ANALYZER_FAILED")
    ...
    except Exception:
        return None
    ```
  - 影响: 失败方向是 fail-closed（映射为错误码）值得肯定，但 PyAV 解码异常、响度分析异常的
    原始信息被完全吞掉，线上只能看到笼统 error_code，无法区分损坏文件、编码不支持还是代码
    bug，违反"不得宽泛 except 掩盖领域错误"的精神。
  - 建议: 捕获时用 `logging.debug(..., exc_info=True)` 或 `# noqa` 注明白名单异常类型
    （av.error.FFmpegError / OSError / ValueError）。
  - 置信度: 高

- [AUD-13311] [Suggestion] 音频整段 PCM 常驻内存，长时长交付物内存峰值偏高
  - 位置: src/features/edit/ai_editing/delivery_analyzer.py:302-305, 347-358
  - 证据:
    ```python
    raw = getattr(completed, "stdout", b"") or b""
    ...
    samples = np.frombuffer(raw, dtype=np.float32)
    ...
    chunks: list[bytes] = []
    ...
    chunks.append(array.astype(np.float32).tobytes())
    return b"".join(chunks)
    ```
  - 影响: 48 kHz mono f32 下 600 s（平台 profile 上限）约 115 MB；PyAV 路径 chunks + join
    峰值接近 2 倍。视频侧已做 64×36 灰度有界采样，音频侧未见同等约束，长片分析时内存
    压力集中在导出后校验阶段。
  - 建议: 峰值/静音/LUFS 全部可流式或分块计算（loudness_analyzer 本身支持分块
    block_size），避免整段拼接。
  - 置信度: 中

### 通过项
- 异常与取消路径：所有失败路径 fail-closed 返回结构化错误码（OUTPUT_NOT_FOUND/
  FRAME_DECODE_FAILED/AUDIO_* 等），子进程有 timeout、非 shell、FileNotFoundError/TimeoutExpired 均处理
- 输入验证：__init__ 对 timeout/sample_fps/frame_size/frozen_run_samples/audio_window_seconds 全部校验
- 线程安全：无共享可变态，实例可安全在导出工作线程使用；无 UI/OpenGL 调用
- 性能模式：视频采样内存有界（固定灰度小帧），frozen/black 判定为 O(N) 单遍
- 结果契约：DeliveryAnalysisResult 不含源路径/原始媒体（隐私边界），to_dict 对非有限峰值过滤为 None
- 类型注解：完整
- GPU/GLSL/信号防递归：不适用

---

## src/features/edit/ai_editing/delivery_contracts.py (行数: 866)

### 发现

- [AUD-13312] [Major] validate_platform_delivery 对标量/畸形 resolution 直接下标，外部 metadata 可使校验崩溃
  - 位置: src/features/edit/ai_editing/delivery_contracts.py:227-231
  - 证据:
    ```python
    resolution = observed.get("resolution", getattr(quality, "resolution", None))
    if isinstance(resolution, list):
        resolution = tuple(int(item) for item in resolution)
    if resolution is not None:
        resolution = (int(resolution[0]), int(resolution[1]))
    ```
  - 影响: metadata 来自 ffprobe 式外部探测（铁律：外部数据须验证）。`resolution: 1920`（标量）
    或 `["1920"]`（单元素）会抛未捕获 IndexError/TypeError，校验函数本身崩溃而非给出
    "缺少/无效分辨率测量"错误；三元组则静默取前两个。
  - 建议: 包一层 try/except (TypeError, ValueError, IndexError)，畸形值视为 None 走
    "缺少输出分辨率测量"分支。
  - 置信度: 中

- [AUD-13313] [Major] _fps_matches 用精确 Fraction 相等比较，十进制 NTSC 帧率（29.97/23.976）永远匹配不上分数规格
  - 位置: src/features/edit/ai_editing/delivery_contracts.py:141-162
  - 证据:
    ```python
    return Fraction(str(float(value))).limit_denominator(100_000)
    ...
    return any(
        expected_fraction is not None and expected_fraction == actual_fraction
        for expected_fraction in (_as_fraction(item) for item in allowed)
    )
    ```
  - 影响: metadata 给十进制 "29.97" 时得到 2997/100，与允许列表 "30000/1001"
    （≈29.97003）精确相等失败 → 输出"帧率 29.97 不在平台允许列表"，合法 NTSC 交付物被
    平台校验误杀；23.976 同理。
  - 建议: 比较改为容差匹配（如 |a-b| < 0.02 帧）或将十进制输入先拟合成最近的标准分数
    （denominator ≤ 1001）再比较。
  - 置信度: 中（若上游一律传 "30000/1001" 形式则不触发；函数本身接受 float/str 任意形态）

- [AUD-13314] [Suggestion] PlatformDeliverySpec.from_dict 对畸形外部 JSON 抛裸 TypeError/ValueError
  - 位置: src/features/edit/ai_editing/delivery_contracts.py:65-68, 86-88, 133-138
  - 证据:
    ```python
    normalized_resolutions = tuple(
        (int(item[0]), int(item[1])) for item in resolutions
    )
    ...
    def _optional_int(value: Any) -> int | None:
        return None if value in (None, "") else int(value)
    ```
  - 影响: host/brief 供的 JSON-safe payload 属外部数据：分辨率条目为标量时 `item[0]` 抛
    TypeError，"4.5" 字符串给 max_file_size_bytes 时抛 ValueError，均无字段名上下文，
    调用方难以定位是哪个字段坏。
  - 建议: from_dict 逐字段容错并抛带字段名的 ValueError，或返回解析错误清单。
  - 置信度: 中

- [AUD-13315] [Suggestion] validate_brand_elements 同 element_id 多条测量时取第一条，顺序敏感且不报告歧义
  - 位置: src/features/edit/ai_editing/delivery_contracts.py:504-515
  - 证据:
    ```python
    candidates = by_id.get(requirement.element_id, ())
    candidate = next(
        (
            item for item in candidates
            if _cue_value(item, "present", True) is not False
            and (
                not requirement.kind
                or str(_cue_value(item, "kind", "") or "") == requirement.kind
            )
        ),
        None,
    )
    ```
  - 影响: 同一 logo 在多个 clip/track 出现多条测量记录时，选中哪条取决于输入顺序；若两条
    一条越界一条正常，校验结果不确定。品牌元素是硬门，结果不应依赖测量列表顺序。
  - 建议: 多候选时全部校验（任一违规即计），或选时间线最靠后者并计一次 invalid/
    ambiguous 测量。
  - 置信度: 中

- [AUD-13316] [Suggestion] _reading_units 的 CJK 范围不含假名/韩文，非中文 CJK 字幕阅读速度被低估
  - 位置: src/features/edit/ai_editing/delivery_contracts.py:437-444
  - 证据:
    ```python
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    non_cjk = re.sub(r"[\u3400-\u9fff]", " ", text)
    tokens = re.findall(r"[^\W_]+", non_cjk, flags=re.UNICODE)
    return cjk + len(tokens)
    ```
  - 影响: 日文假名（U+3040–30FF）、韩文（U+AC00–D7AF）不在范围内；日文无空格整句会被
    计为 1 个 token，阅读速度显著低估，超速字幕可能通过 over_speed 门。
  - 建议: 扩展正则覆盖 `\u3040-\u30ff\uac00-\ud7af\uf900-\ufaff`，或按"非拉丁字符逐字计"
    的更通用规则。
  - 置信度: 中

### 通过项
- 状态提交一致性：所有契约类型 frozen + __post_init__ 校验（spec_id、正数分辨率/码率/采样率、min≤max 时长）
- 异常路径：_as_fraction/_cue_range/_first_number/_safe_area_violation 对坏值显式降级（None/True/跳过），不吞领域异常
- 输入验证：validate_subtitle_cues 校验 fps/max_words_per_second/max_chars_per_line 有限正数；build_subtitle_safe_area 校验 inset ∈ [0, 0.5)
- 品牌元素缺测量按 missing/invalid 计失败（fail-closed，不从 plan 标签推断）
- 字幕重叠检测：排序后单遍合并，active_end 或 end 的 0 值在此域不可能（end > start ≥ 0）
- 线程安全：纯函数 + 不可变规格对象
- 类型注解：完整
- GPU/GLSL/信号防递归：不适用

---

## src/features/edit/ai_editing/delivery_quality_gate.py (行数: 190)

### 发现

- [AUD-13317] [Suggestion] "未测量"（默认 0/None）与"测量为 0"不可区分，缺失测量默认放行
  - 位置: src/features/edit/ai_editing/delivery_quality_gate.py:40-42, 119-127, 126-127
  - 证据:
    ```python
    black_frame_count: int = 0
    frozen_frame_count: int = 0
    corrupt_frame_count: int = 0
    ...
    require(inp.black_frame_count == 0, "检测到黑帧")
    ...
    if inp.peak_db is not None:
        require(inp.peak_db <= inp.max_peak_db, "音频存在削波风险")
    ```
  - 影响: 模块 docstring 声称 "fail closed"，且分析器失败路径（corrupt=1 / audio_present=
    False）确实能传导为阻断；但黑帧/冻结/损坏计数字段无"已测量"标志，探测环节被跳过或
    上游漏传时，默认 0 会原样通过，max_peak_db 有非 None 默认值 0.0 却在 peak_db=None 时
    静默跳过——"看似配置了削波门、实际未测则不拦"。
  - 建议: 为测量类字段增加 measured 标志或把计数改为 `int | None`，None 且对应门已配置时
    记 "缺少测量" 阻断项。
  - 置信度: 中

### 通过项
- 输入验证：silence_policy 白名单校验，非法策略直接阻断；静音硬门缺上限/缺测量均 fail-closed
- 交叉验证：expected_* 提供时测量缺失即阻断（分辨率/帧率/采样率/LUFS 后端可用性）
- 异常路径：纯函数无吞异常；阻断项全部生成 QUALITY_GATE_FAILED 诊断（detail 经
  make_diagnostic 的 **message_vars 填入模板，已核实签名匹配）
- 状态提交一致性：结果对象 frozen，to_dict 完整
- 线程安全：纯函数
- 类型注解：完整
- GPU/GLSL/信号防递归：不适用

---

## 汇总

| 严重度 | 数量 |
|--------|------|
| Critical | 0 |
| Major | 7 |
| Suggestion | 10 |

Major：AUD-13300（creative_assets 覆盖绕过影响报告）、AUD-13302（stale 判定 last-wins 漏报）、
AUD-13303（RenderContract 一致性忽略 effects_enabled）、AUD-13305（全量重算按 1 clip 计费）、
AUD-13308（LOCKED 决策可被 reject）、AUD-13312（外部 resolution 畸形崩溃）、
AUD-13313（十进制 NTSC 帧率误判不合规）。
