# batch-021 审查报告 — src/app/controllers（AI Edit Host Bridge）

- 批次: batch-021 [P1]（编号区间 AUD-2100–AUD-2199）
- 文件: `src/app/controllers/ai_edit_host_bridge.py` (445 行)、`src/app/controllers/ai_edit_host_bridge_impl.py` (1,764 行)
- 审查维度: 线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失
- 汇总: Critical 0 · Major 4 · Suggestion 10

---

## src/app/controllers/ai_edit_host_bridge.py (行数: 445)

协议 + Null 对象模块。无 GPU / 线程 / 信号代码；主要问题集中在 Null 实现与协议的一致性。

### 发现

- [AUD-2100] [Major] NullAiEditHostBridge 缺失协议方法 `detect_autonomous_replay` 与 `rollback_ai_command`
  - 位置: src/app/controllers/ai_edit_host_bridge.py:107（协议声明）、:151（协议声明）、:290-445（Null 实现全文无这两个方法）
  - 证据:
    ```python
    def detect_autonomous_replay(self, plan: EditPlan, state: Any) -> Any:
        """Detect an exact already-committed plan after restart/crash recovery."""
        ...
    ```
    ```python
    def rollback_ai_command(
        self,
        timeline_id: str,
        *,
        expected_undo_label: str,
        expected_revision: int | None,
    ) -> Any:
    ```
    而 Null 实现从 `get_plan_by_id`（:325）直接跳到 `commit_ai_command`（:328），全类无对应实现。
  - 影响: 该类被用作协议缺省实现（`src/app/controllers/ai_edit_controller.py:373` `self._bridge: AiEditHostBridge = bridge or NullAiEditHostBridge()`）。(1) `isinstance(NullAiEditHostBridge(), AiEditHostBridge)`（`@runtime_checkable`）返回 False，Null 对象不满足自己宣称实现的协议；(2) 静态检查器对 `bridge or NullAiEditHostBridge()` 报 protocol 成员缺失；(3) 调用方 `autonomous_agent.py:289/325` 直接调 `self._host.rollback_ai_command(...)`（不经 getattr 兜底），若 Null 桥到达该路径即 AttributeError——目前仅靠 Null 的空 project_id 早退间接防护，属脆弱防护，未来新增调用点很容易踩中。
  - 建议: 在 NullAiEditHostBridge 补齐两个方法（`detect_autonomous_replay` 返回 `{"known": False}`，`rollback_ai_command` 返回 None），并加一个协议一致性单测（`isinstance(NullAiEditHostBridge(), AiEditHostBridge)`）防回归。
  - 置信度: 高

- [AUD-2101] [Suggestion] 未使用的导入：SemanticSegment / TimelineDiff / ActionRequest
  - 位置: src/app/controllers/ai_edit_host_bridge.py:25、:32、:34
  - 证据:
    ```python
    from src.features.edit.ai_editing.editorial_models import SemanticSegment
    ...
    from src.features.edit.ai_editing.transaction_service import PreviewResult, TimelineDiff
    from src.features.edit.ai_editing.execution_policy import (
        ActionRequest,
        ExecutionAction,
        PolicyDecision,
    )
    ```
    全文（协议与 Null 类）均未再引用这三个名字（ExecutionAction/PolicyDecision 有使用）。
  - 影响: 死导入扩大模块依赖面（editorial_models、transaction_service 内部符号），增加导入开销与阅读噪声。
  - 建议: 删除这三个未使用导入。
  - 置信度: 高

- [AUD-2102] [Suggestion] 模块声明"协议外方法一律不再被调用"，但 composite 方法游离于本协议之外
  - 位置: src/app/controllers/ai_edit_host_bridge.py:17（声明）、:434-445（Null 的 composite 实现）
  - 证据:
    ```python
    不在协议里的方法一律不再被 controller / service 调用。
    ```
    ```python
    # Composite optional surface: the null host is deliberately read-only.
    def get_composite_snapshot(self) -> dict[str, Any] | None:
    ```
    而 `get_composite_snapshot` / `validate_composite_edit` / `apply_composite_edit` 未出现在 `AiEditHostBridge` 协议中，实际由另一份协议（`src/features/edit/ai_editing/edit_api.py:107-118`）声明，并经 `getattr(self._host, "get_composite_snapshot", None)`（edit_api.py:236）鸭子探测调用。
  - 影响: 同一宿主对象需同时满足两份重叠协议，且 composite 面走 hasattr 回退——恰是本模块 docstring 声明要淘汰的模式；协议演化时两份协议容易漂移（AUD-2100 的缺方法问题就是漂移实例）。
  - 建议: 要么把 composite 三方法并入 `AiEditHostBridge`（返回不可用报告即"未装配"），要么在 docstring 中如实说明 composite 面由 `EditApi` 协议单独管理。
  - 置信度: 高

### 通过项
- 线程安全：纯协议/数据类，无跨线程代码。
- GPU 资源生命周期：不触碰 OpenGL/renderer。
- 信号防递归：无信号连接。
- 异常路径：Null 对象全部返回确定状态（False/None/空串），无 broad except 掩盖装配缺失，符合 M1.2 设计。
- 状态提交一致性：Null 对 `request_plan_approval`/`issue_automatic_plan_approval`/`commit_ai_command` 一律拒绝/None，fail-closed 正确。
- 输入验证：`check_ai_action` 未装配时返回 `PolicyDecision(False, ...)`（fail-closed，与 impl 的 AUD-2105 形成对照）。

---

## src/app/controllers/ai_edit_host_bridge_impl.py (行数: 1,764)

生产桥接实现。GPU/信号防递归维度整体干净；问题集中在导出路径的宿主契约假设、策略门 fail-open 分支与若干输入验证/性能细节。

### 发现

- [AUD-2103] [Major] AI 导出未绑定已验证的 timeline_id，宿主实际导出的是 Edit 页当前时间线
  - 位置: src/app/controllers/ai_edit_host_bridge_impl.py:1075-1081（按 scope.timeline_id 取 sequence 并测量质量合同）、:1228-1299（委托 perform_video_export）
  - 证据:
    ```python
    timeline_id = str(getattr(scope, "timeline_id", "") or "")
    if state is None or not timeline_id:
        return False, "当前没有可导出的时间线"
    sequence = state.sequences.get(timeline_id)
    ```
    随后：
    ```python
    start_export = getattr(self._mw, "perform_video_export", None)
    ...
    start_export({
        "path": path,
        "codec": codec,
        ...
    })
    ```
    而宿主 `perform_video_export`（src/app/controllers/deliver_controller.py:811-814）导出的是 `edit_page._current_timeline`，settings 中不携带 timeline_id。`state.sequences`（`_current_timeline_sequences`）是全工程多条时间线的字典（edit_controller.py:687 整体替换），因此 scope.timeline_id 可以与 Edit 页当前打开的时间线不同。
  - 影响: 预检、字幕安全区测量、品牌元素合同、LUFS/静音门全部针对时间线 X 计算，实际编码的却是时间线 Y 的成片——交付质量合同失真，用户可能拿到错误时间线的导出文件。`commit_ai_command`（:573）有同类一致性校验，导出路径缺失。
  - 建议: 在调用 `start_export` 前校验 `edit_page._current_timeline.timeline_id == timeline_id`（或把 timeline_id 传入 settings 并要求宿主导出指定时间线），不一致时返回确定失败。
  - 置信度: 中（多时间线工程中可触发；单时间线/默认作用域下不发生）

- [AUD-2104] [Major] AI 导出 Job 在宿主"静默取消"路径下永久停留在 running
  - 位置: src/app/controllers/ai_edit_host_bridge_impl.py:1290-1303
  - 证据:
    ```python
    job.mark_running()
    try:
        start_export({
            "path": path,
            ...
            "_post_export": _post_export,
        })
    except Exception as exc:  # noqa: BLE001
        logger.exception("starting AI video export failed")
        job.fail("EXPORT_START_FAILED", str(exc))
        return False, f"导出启动失败：{exc}"
    ```
    仅"抛异常"会 fail。宿主 `perform_video_export` 存在多个不抛异常的提前 return：无时间线时 `QMessageBox.critical(...); return`（deliver_controller.py:812-816）、`config.validate()` 失败 `QMessageBox.critical(...); return`（:879-881）、用户在 DeliveryMatrixDialog 拒绝 `return`（:895-896）。
  - 影响: 这些路径下 job 已 `mark_running()` 但既不 fail 也不 complete（`_post_export` 只在编码 worker 成功前被调用），JobRegistry 留下永久 running 的导出任务，任务列表/查询失真，且用户取消后 AI 侧仍以为导出在进行。
  - 建议: 将 `job.mark_running()` 移到确认宿主已接受之后不可行时，改为让宿主返回启动结果（bool/异常），或在 `_post_export` 之外注册超时/取消回调兜底 fail；至少要求 `perform_video_export` 在提前 return 前抛出或回调失败。
  - 置信度: 高（bridge 侧证据充分，宿主提前 return 已核实）

- [AUD-2105] [Major] `check_ai_action` 在宿主缺 `feature_flags` 时策略门 fail-open
  - 位置: src/app/controllers/ai_edit_host_bridge_impl.py:1613-1622
  - 证据:
    ```python
    project_id = self.get_project_id()
    if not project_id:
        from src.features.edit.ai_editing.execution_policy import PolicyDecision

        # Minimal bridge fakes used by legacy transaction tests do not
        # model a MainWindow or feature flags.  Production MainWindow
        # always has both, so an initialized host still fails closed.
        if getattr(self._mw, "feature_flags", None) is None:
            return PolicyDecision(True, reason="legacy host has no policy context", action=str(action))
        return PolicyDecision(False, reason="no active project", action=str(action))
    ```
  - 影响: 任何未设置 `feature_flags` 属性的宿主对象（无头宿主、装配不完整的 fake、未来 CLI 宿主复用本类）在无工程状态下对所有动作（含 `paid_generation`——生成箱不依赖工程）直接放行，绕过整份 ExecutionPolicy。这是为测试兼容在生产代码里留下的 fail-open 后门，与同函数 docstring"fail closed when the host is not fully initialized"及 .clinerules 默认拒绝原则相悖。生产 MainWindow 确实总有 feature_flags（main_window.py:341），故实际触发面有限。
  - 建议: 把 legacy 分支改为显式注入（构造参数 `legacy_no_policy: bool = False`，仅测试 fake 传 True），或对缺 feature_flags 的宿主一律 fail-closed 并修对应旧测试。
  - 置信度: 中（分支为文档化的测试让步；fail-open 模式本身确定存在）

- [AUD-2106] [Suggestion] 一次性 ApprovalGrant 在提交/应用之前被消耗，失败路径烧掉授权
  - 位置: src/app/controllers/ai_edit_host_bridge_impl.py:1033-1039（字幕路径）、:1737-1749（composite 路径）
  - 证据:
    ```python
    self._approval.consume(grant.grant_id, _expected_commit_scope(plan, diff_hash=preview.diff_hash))
    compiled = compile_plan(plan)
    new_timeline = self.commit_ai_command(
        timeline_id, compiled.macro, undo_label=compiled.undo_label,
    )
    if new_timeline is None:
        return False, "字幕提交失败：找不到活动时间线视图"
    ```
    composite 同型：`self._approval.consume(grant_id, ApprovalScope(...))`（:1737）先于 `result = service.apply(plan)`（:1749）。
  - 影响: consume 之后 compile/commit/apply 失败时，单次授权已被记账消耗，工程却未变更；重试需重新弹用户确认。授权账本与实际写入不一致（审计口径偏差），非数据损坏。
  - 建议: 在失败返回前记录"grant consumed but not applied"审计事件；或改为 commit 成功后 consume（并在 commit 前做双阶段预留）。
  - 置信度: 高（事实确定，影响等级低）

- [AUD-2107] [Suggestion] `_prepare_brand_quality_contract` 对外部输入做 `tuple()` 未防字符串/标量，TypeError 可逃逸 (bool, str) 契约
  - 位置: src/app/controllers/ai_edit_host_bridge_impl.py:176-182、:206-211；调用方异常捕获 :1180-1181
  - 证据:
    ```python
    requirements = (
        (raw_requirements,)
        if isinstance(raw_requirements, Mapping)
        else tuple(raw_requirements)
    )
    ```
    ```python
    else:
        elements = tuple(raw_elements)
    ```
    调用方只捕 ValueError：
    ```python
    except ValueError as exc:
        return False, str(exc)
    ```
  - 影响: `brand_requirements` 来自 AI/JSON inputs（外部数据）。传非空字符串时 `tuple("logo")` 得到逐字符元组，每个字符被当作一项需求进入交付质量门（计数与门判定失真）；传 int/bool/None 以外标量时 `tuple()` 抛 TypeError，逃逸出 `request_ai_export` 的 `(bool, str)` 返回契约（:1180 只捕 ValueError）。
  - 建议: 输入为 str 时显式报 ValueError；非 Mapping/非可迭代统一 `raise ValueError(...) from exc`，保证本方法只抛 ValueError。
  - 置信度: 中

- [AUD-2108] [Suggestion] ASR worker 信号连接的普通 Python 回调在 ASR 线程执行
  - 位置: src/app/controllers/ai_edit_host_bridge_impl.py:939-953
  - 证据:
    ```python
    def _progress(fraction: Any, message: str) -> None:
        # worker 信号可能在 ASR 线程发出；这里只转发纯数据，Qt 状态栏由
        # controller 的主线程 slot 更新。
        on_progress(fraction, str(message or "正在处理…"))
    ...
    worker.progress.connect(_progress)
    worker.completed.connect(on_completed)
    worker.failed.connect(_failed)
    worker.finished.connect(
        lambda: setattr(self._mw, "_ai_transcription_worker", None)
    )
    ```
    `_SpeechToTextWorker.run()` 在工作线程 emit（edit_controller.py:187/213/216）；非 QObject 接收者的连接为直连语义。
  - 影响: controller 注入的 on_progress/on_completed/on_failed 与 finished 的 lambda 都在 ASR 线程执行。当前靠"回调只传纯数据"的口头约定保证安全，协议签名（`Callable[[float | None, str], Any]`）并未约束线程；任何回调触碰 UI/工程对象即违反 UI 线程铁律，无编译期/运行期防护。
  - 建议: 在协议 docstring 明示"回调可能在工作线程执行"；或在 bridge 内把回调包装为 QMetaObject.invokeMethod 到主线程，消除对调用方纪律的依赖。
  - 置信度: 中

- [AUD-2109] [Suggestion] `_GeneratedAssetImporter.import_asset` 吞掉具体失败原因，返回空诊断
  - 位置: src/app/controllers/ai_edit_host_bridge_impl.py:1553-1577
  - 证据:
    ```python
            except Exception as exc:  # noqa: BLE001
                logger.exception("generated asset import failed")
                return None, []
    ```
  - 影响: 失败原因只进日志；空的 diagnostics 使 `import_ai_generated_asset_from_path` 上游只能显示通用文案"导入媒体池失败"（:1456-1457），用户无法得知真实原因（路径无效/元数据构造失败等），违反"失败不静默"的本模块设计基调。
  - 建议: 构造一条携带 `str(exc)` 摘要的 Diagnostic 返回（脱敏后），而非空列表。
  - 置信度: 高

- [AUD-2110] [Suggestion] `request_ai_export` 在 UI 线程同步执行字幕 bbox 逐条测量与导出预检
  - 位置: src/app/controllers/ai_edit_host_bridge_impl.py:1150-1157（preflight）、:1166-1179（质量 cues/品牌元素）
  - 证据:
    ```python
    report = run_export_preflight(
        config,
        sequence,
        generation_bin=generation_bin,
        commercial_ready_required=commercial_ready_required,
    )
    ...
    subtitle_quality_cues = _build_measured_subtitle_quality_cues(
        sequence,
        expected_resolution,
        ...
    ```
  - 影响: `_build_measured_subtitle_quality_cues` 对每条可见字幕 clip 同步调 `measure_subtitle_render_bbox`，叠加 ffprobe 类 preflight，全部发生在主线程（本方法由 controller 在 GUI 线程调用）。字幕数百条时（长节目）可造成可感知的 UI 卡顿，与 4K 实时基准的项目目标不符。
  - 建议: 将 cues 测量与 preflight 移入导出 Job 的后台阶段（编码前），或至少分批/带进度地上报。
  - 置信度: 中（卡顿幅度取决于字幕规模与 measure 实现）

- [AUD-2111] [Suggestion] `get_capability_manifest` 在 UI 线程访问系统 keyring（`get_secret`）
  - 位置: src/app/controllers/ai_edit_host_bridge_impl.py:311-323
  - 证据:
    ```python
    credential_available = bool(
        active_profile is not None
        and (
            getattr(active_profile, "provider_kind", "") in {"ollama", "local_http", "openai_compat"}
            or (
                credential_store is not None
                and getattr(active_profile, "credential_ref", "")
                and credential_store.get_secret(active_profile.credential_ref)
            )
        )
    )
    ```
    `get_secret` 落到 `backend.get_password(_KEYCHAIN_SERVICE, credential_ref)`（src/shared/ai_models/generation/credential_store.py:134-136），即真实 OS 钥匙串/文件 I/O。
  - 影响: manifest 每次构建（对话/能力查询触发）都可能在主线程做 keyring I/O；钥匙串被锁或首次解锁时可能阻塞 UI 数百毫秒以上。
  - 建议: 把 credential 可用性改为缓存布尔（在设置变更时刷新），避免在 manifest 热路径上反复取 secret。
  - 置信度: 中（阻塞时长依赖后端；调用位置确定在主线程）

- [AUD-2112] [Suggestion] `_resolve_timeline_view` 忽略 `timeline_id` 参数，恒返回当前 view
  - 位置: src/app/controllers/ai_edit_host_bridge_impl.py:784-789
  - 证据:
    ```python
    def _resolve_timeline_view(self, timeline_id: str) -> Any:
        """解析活动 timeline 的 TimelineView（弱依赖，缺失返回 None）。"""
        panel = getattr(self._mw, "timeline_panel", None)
        if panel is None:
            return None
        return getattr(panel, "timeline_view", None)
    ```
  - 影响: 参数暗示按 id 解析，实际与 id 无关。`commit_ai_command`（:573）有后置一致性校验兜底，但 `commit_ai_transcript_subtitles`（:970）等调用点传入的 id 不起任何作用，误导后续维护者并掩盖"非活动 timeline 无法寻址"这一事实。
  - 建议: 去掉参数（改名 `_resolve_active_timeline_view`），或在 docstring 明示只解析活动 view 且不做 id 匹配。
  - 置信度: 高

- [AUD-2113] [Suggestion] `get_selected_asset_ids` 语义重载（时间线音频选中覆盖媒体池选中）且异常路径零日志
  - 位置: src/app/controllers/ai_edit_host_bridge_impl.py:807-832
  - 证据:
    ```python
    # 时间线上的音频片段是 AI"把音频转字幕"最明确的作用域；优先使用它，
    # 避免媒体池里残留的选中状态把请求导向另一份素材。
    selected_clip = self.get_selected_audio_clip()
    if selected_clip is not None:
        return (selected_clip[2],)
    ...
    except Exception:  # noqa: BLE001
        pass
    ```
  - 影响: 协议 docstring（ai_edit_host_bridge.py:169-174）定义是"媒体池当前选中素材 ID"，实现却混入时间线选择域。`issue_automatic_plan_approval`（:519-522）用它做 requested⊆selected 校验：音频片段被选中时其他素材的自动计划一律被拒（fail-closed 方向，但语义偏移可能造成误拒）；`except: pass` 无日志掩盖宿主异常。
  - 建议: 拆成两个显式方法（媒体池选中 / 选中音频片段），`get_selected_asset_ids` 回归协议语义；except 分支至少 logger.debug。
  - 置信度: 中

- [AUD-2114] [Suggestion] 实现类未声明实现 `AiEditHostBridge`，导入未使用；`check_ai_action` 返回注解弱化为 Any
  - 位置: src/app/controllers/ai_edit_host_bridge_impl.py:33（未使用导入）、:249（无协议基类）、:1599（`-> Any`）
  - 证据:
    ```python
    from src.app.controllers.ai_edit_host_bridge import AiEditHostBridge
    ...
    class MainWindowAiEditHostBridge:
    ```
    ```python
    ) -> Any:
        """Evaluate the live action policy at the final host boundary."""
    ```
    协议声明为 `-> PolicyDecision`（ai_edit_host_bridge.py:214-227）。
  - 影响: 类注释声称"实现 AiEditHostBridge"但既不继承也不被静态检查约束，协议/实现签名漂移（如 AUD-2100 的 Null 缺方法、本条返回类型弱化）无法在编译期发现；`AiEditHostBridge` 导入仅出现在 docstring 文字里，属死导入。
  - 建议: 删除死导入或让类显式 `AiEditHostBridge` 作基类（Protocol 可作显式基类获得静态检查）；`check_ai_action` 注解改回 `PolicyDecision`。
  - 置信度: 高

### 通过项
- GPU 资源生命周期：本类不导入 renderer/GPU；`get_current_gl_viewer` 仅返回 viewer（:1514-1541），注明必须在 GUI 线程调用、抓帧失败 fail-soft 返回 None，不违反"UI 线程不得操作 OpenGL"边界。
- GLSL·HDR 正确性：无着色器/HDR 数据路径。
- 信号防递归：桥不连接会回发的 widget 信号；提交走 `execute_command` 既有单条刷新链（:589-591），无新增递归面。
- 状态提交一致性（正面项）：`commit_ai_command` 提交前校验 view 的 timeline_id 与目标一致（:572-578）并经策略门（:579-587）；`rollback_ai_command` 先校验 revision + undo label 再 undo（:766-776），撤销目标不可证明时拒绝；`detect_autonomous_replay` 对不支持的操作 kind 返回 known=False fail-closed（:634-635），部分命中判 ambiguous 阻断（:661-669）。
- 异常与取消路径（正面项）：`request_plan_approval` 无 Qt 环境时默认拒绝（:492-495，fail-closed）；`is_autonomous_mode_enabled` 策略查询/kill switch 异常一律返回 False（:431-447）；`teardown_for_project` 不抛（:1758-1764）。
- 授权链（正面项）：`issue_automatic_plan_approval` 签发前逐项复核 autonomous 开关、project/timeline 作用域、selected assets 子集与策略门（:514-536），stale plan 无法隐式获得写权限。
- 输入验证（正面项）：`request_ai_export` 对 LUFS 范围/容差、静音策略枚举、字幕质量门参数（:1094-1119、:1245-1255）与导出路径存在性（:1440-1442）验证充分；`_resolve_subtitle_safe_area_policy` 对 inset 做有限性与 [0,0.5) 区间校验（:79-80）。
- 性能模式（正面项）：`_runtime_capability_snapshot` 进程级缓存 + 显式失效（:344-366），避免重复 subprocess/dylib 探测。

---

## 附：各文件发现计数

| 文件 | Critical | Major | Suggestion |
|---|---|---|---|
| ai_edit_host_bridge.py | 0 | 1 | 2 |
| ai_edit_host_bridge_impl.py | 0 | 3 | 8 |
| 合计 | 0 | 4 | 10 |
