# batch-157 审查报告（src/features/edit/core/capability_registry.py）

批次：batch-157 [P1] 主题 src/features/edit/core//
编号区间：AUD-15700–AUD-15799

## src/features/edit/core/capability_registry.py (行数: 443)

### 发现

- [AUD-15700] [Major] probe_errors 通道永不写入，is_healthy 的"无探测错误"分支为死代码
  - 位置: src/features/edit/core/capability_registry.py:189
  - 证据:
    ```python
    caps: List[Capability] = []
    probe_errors: List[str] = []
    ```
    （全文件仅此处声明与第 381 行 `probe_errors=tuple(probe_errors)` 出现，无任何 `probe_errors.append(...)`）
    ```python
    @property
    def is_healthy(self) -> bool:
        """无探测错误，且所有条目都带证据。"""
        return not self.probe_errors and all(c.evidence for c in self.capabilities)
    ```
  - 影响: 探测异常（`probe_bwf_supported` 失败、`probe_audio_effect_health` 返回 `(False, -1, ...)`）只以 logger.warning 或条目 notes 文本形式存在，机器可读的 `probe_errors` schema 字段永远为空；`is_healthy` 无法区分"全绿"与"探测崩了但条目仍带证据"，消费方（export_manifest 的 capabilities 段）据此得到的健康信号失真。与模块头第 20 行"构建失败降级为条目级错误"的承诺不符——错误只降级、未被记录。
  - 建议: 在 `build_capability_registry()` 中，各探测函数的异常分支（BWF except、audio health 返回 missing=-1、optional dep 之外的失败）向 `probe_errors` 追加一行描述（如 `f"bwf: {exc}"`），或在设计上明确废弃该字段并同步修改 `is_healthy`/`to_dict`。
  - 置信度: 高（全文件通读 + grep 确认无写入点；tests/edit/test_capability_registry.py 也未断言任何 probe_errors 场景）

- [AUD-15701] [Major] drift 检测把 PARTIAL 折叠为"不可用"，静态表低估方向的分歧永不上报（FCPXML 现已实际漏报）
  - 位置: src/features/edit/core/capability_registry.py:427
  - 证据:
    ```python
        registry_available = cap.status is CapabilityStatus.AVAILABLE
        if registry_available != table_available:
    ```
    对照 interchange.py:379-381 静态表：
    ```python
    InterchangeFormat(
        name="Final Cut Pro XML (FCPXML)",
        extension=".fcpxml",
        available=False,
    ```
  - 影响: 注册表声明 FCPXML 为 PARTIAL 且 P1-16 已接线 File 菜单生产入口（本文件 246-263 行 evidence/notes），而静态表仍 `available=False`、notes 写"建议下一迭代实现"——两处实质分歧，但 `PARTIAL → registry_available=False == table_available=False` 判为无 drift。同类低估（表 False + 注册表 PARTIAL/DORMANT 但已接线）均成检测盲区，与模块头"分裂从测试碰巧覆盖变成每次可检测"的目标相悖。
  - 建议: drift 判定改为三档语义：`table_available=True 且 status != AVAILABLE`、`table_available=False 且 status in (AVAILABLE, PARTIAL 且 notes 声明已接生产入口)` 至少记一条低级提示；或直接更新 interchange.py 的 FCPXML 条目使两表一致并在 CI 中锁定。
  - 置信度: 中（代码行为确定无疑；是否算"缺陷"取决于 drift 的定义严格取 docstring 的布尔口径还是取模块目标口径）

- [AUD-15702] [Suggestion] find_status_drift 对映射前缀在静态表中消失时静默跳过，无任何日志
  - 位置: src/features/edit/core/capability_registry.py:421
  - 证据:
    ```python
    for prefix, cap_id in _INTERCHANGE_ID_MAP.items():
        table_available = table.get(prefix)
        if table_available is None:
            continue
    ```
  - 影响: `_INTERCHANGE_ID_MAP` 的键依赖 interchange.py 条目名前缀的精确拼写（`"final cut pro xml"` 等裸字符串，无断言绑定）。一旦静态表改名（如 "Final Cut Pro XML" 改为 "FCPXML"），该能力的 drift 检查静默失效，模块的核心承诺（分歧"每次可检测"）在无人知晓的情况下缩水。
  - 建议: `table_available is None` 时 `logger.warning("interchange 表缺少预期条目前缀: %s", prefix)`；或加一条单测遍历 `_INTERCHANGE_ID_MAP` 断言每个前缀都能在 `_interchange_capabilities()` 中命中。
  - 置信度: 高（当前 6 个前缀逐一核对均能命中，问题为潜在失效路径）

- [AUD-15703] [Suggestion] 音频效果探测失败时用户可见 notes 出现"缺失 -1 项"
  - 位置: src/features/edit/core/capability_registry.py:158
  - 证据:
    ```python
    except Exception as exc:  # noqa: BLE001
        logger.warning("Audio effect health probe failed", exc_info=True)
        return False, -1, f"probe error: {exc}"
    ```
    消费于第 310 行：
    ```python
    notes="" if healthy else f"缺失 {missing} 项：{detail}",
    ```
  - 影响: 探测异常路径下条目 notes 变成"缺失 -1 项：probe error: ..."，`-1` 是内部哨兵值泄漏到用户可见文案（UI/文档被要求只读本注册表渲染）。
  - 建议: `-1` 仅作内部哨兵，构造 notes 时分支处理：`missing > 0` 才写"缺失 N 项"，否则只写 detail（probe error 原文）。
  - 置信度: 高

- [AUD-15704] [Suggestion] 模块头宣称"状态由运行时探测得出，不手写字面量"，实际 11 个条目中仅 2 个状态来自探测
  - 位置: src/features/edit/core/capability_registry.py:11
  - 证据:
    ```python
    - 状态由**运行时探测**得出（BWF writer 探测、效果注册健康检查、
      可选依赖探测），而不是手写字面量；
    ```
    而第 192-197 行（timecode）、247-251 行（fcpxml）、327-330 行（multicam）等均为手写：
    ```python
    caps.append(Capability(
        capability_id="edit.timecode.drop_frame",
        title="SMPTE drop-frame 时间码往返",
        status=CapabilityStatus.AVAILABLE,
    ```
  - 影响: 仅 `edit.interchange.bwf`（第 208 行）与 `edit.audio_effects`（第 304 行）状态由探测决定，其余 9 条（含 OTIO 的 opentimelineio 探测结果只影响 notes 不影响 status，第 264-281 行）为编译期手写。文档承诺与实现不符，后续维护者可能误以为改环境即可改变状态。
  - 建议: 修正模块头表述为"可探测的状态由探测得出，其余为带证据链的人工声明"；或对 OTIO 缺依赖时把 status 降为 UNAVAILABLE（探测已就手）。
  - 置信度: 高

### 通过项
- 线程安全：纯 frozen dataclass + 惰性导入只读探测，无 Qt/GPU 状态，可在任意线程调用（与模块头约束一致；effect_registry.get_effect_registry_health 自述"safe from any thread, never mutates"，探测确为读侧）
- GPU 资源生命周期：不涉及（无 ModernGL/纹理/缓冲操作）
- GLSL·HDR 正确性：不涉及（无着色器/像素数据）
- 信号防递归：不涉及（无 Qt 控件与信号连接）
- 状态提交一致性：不涉及（纯只读快照，不产生 undo/redo/revision/持久化写入）
- 异常与取消路径：三处宽 except 均为文档化的降级探测（# noqa: BLE001 + logger.warning exc_info=True），不掩盖领域错误；消费方 export_manifest.py:365-374 另有整体 try/except 降级为 capabilities=None
- 输入验证：`probe_optional_dependency` 捕获 (ImportError, ValueError) 且用 find_spec 不导入本体；`find_status_drift(registry=None)` 有默认探测路径，静态表读取失败时产出哨兵 drift 记录而非抛出
- 性能模式：探测廉价（find_spec 不加载模块、无 IO）；build 每次重探测为文档化设计；31 处 evidence 指向的文件已逐一核实存在（含 tests/edit/test_multicam_production.py、src/features/edit/adapters/otio_adapter.py 等）
- 类型注解：公开 API 全部带精确注解（Tuple/Optional/Dict[str, object]），无缺失
- 静态表前缀映射核对：interchange.py 全部 6 条目（bwf / csv cue sheet / audio stems / final cut pro xml / aaf / omf）与 `_INTERCHANGE_ID_MAP` 键逐一命中，当前无映射缺口
