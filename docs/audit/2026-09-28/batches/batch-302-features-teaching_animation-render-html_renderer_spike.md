# batch-302 审查报告 — src/features/teaching_animation/render/html_renderer_spike/

- 批次：batch-302 [P2]，编号区间 AUD-30200–AUD-30299
- 文件：8 个 / 2,781 行，全部 Read 通读
- 结论统计：Critical 0 / Major 9 / Suggestion 11
- 维度适用性说明：本包为 W0-A HTML 离屏捕获 spike，无 GLSL/HDR 着色器代码、无 Qt 信号递归面、无 undo/redo/revision/持久化（自述"不修改 core/model.py/timeline/UI/工程文件格式"），这三类维度按"不适用（有据）"在各文件通过项注明；其余维度（线程安全、资源生命周期、异常与取消、输入验证、性能、类型注解）逐文件审查。

---

## src/features/teaching_animation/render/html_renderer_spike/__init__.py (行数: 106)

### 发现

（无）

### 通过项
- 通过（无逻辑）：纯 re-export 包门面；已逐一核对 `__all__` 的 106 项与各子模块导出一致，全部导入符号在对应模块中存在（含 `qt_capture_backend.QtWebEngineGrabBackend`、`playwright_backend.PlaywrightChromiumBackend`、`security` 全部 12 项）。
- 无文件级副作用、无线程/GPU/信号/持久化相关代码。

---

## src/features/teaching_animation/render/html_renderer_spike/capture_backend.py (行数: 244)

### 发现

- [AUD-30200] [Suggestion] `is_uniform_frame` 缺少零尺寸/空数据防护，姊妹函数有而它没有
  - 位置: src/features/teaching_animation/render/html_renderer_spike/capture_backend.py:44-48
  - 证据:
    ```python
    total = width * height
    expected = total * 4
    if len(data) < expected:
        return False, None
    first = (data[0], data[1], data[2], data[3])
    ```
    对比同文件 `is_effectively_blank`（79 行）的防护：`if width <= 0 or height <= 0 or len(data) < expected:`
  - 影响: `is_uniform_frame(b"", 0, 0)` 时 total=0、expected=0、长度检查通过，随后 `data[0]` 抛 IndexError。该函数是 `__all__` 导出的公共工具，且 `CapturedFrame`/`CaptureSurface` 的正数校验并不能保护独立调用方。
  - 建议: 与 `is_effectively_blank` 对齐，在长度检查前加 `if width <= 0 or height <= 0: return False, None`。
  - 置信度: 高

- [AUD-30201] [Suggestion] `CaptureResult` 是字符串字面量而非类型别名；`font_set` 默认值违反自身 Mapping 注解
  - 位置: src/features/teaching_animation/render/html_renderer_spike/capture_backend.py:175、209
  - 证据:
    ```python
    CaptureResult = "CapturedFrame | CaptureFailure"
    ```
    ```python
        font_set: Mapping[str, str] = (),
    ```
  - 影响: `CaptureResult` 运行时是 `str`，类型检查器与 `isinstance` 均不可用（本文件签名已用 3.10+ `X | Y` 语法，无需字符串前向引用）；`font_set` 默认 `()` 是 tuple 不是 Mapping，注解与默认值自相矛盾（当前两个后端实现都忽略该参数，未触发运行时错误）。
  - 建议: 改为 `CaptureResult = CapturedFrame | CaptureFailure`；`font_set` 默认改为 `field(default_factory=dict)` 风格的可空参数或将注解放宽为 `Mapping[str, str] | tuple[()]`。
  - 置信度: 高

### 通过项
- 类型注解完整（除上条外），数据类均为 frozen、`__post_init__` 做 fail-closed 校验（尺寸正数、DPR 正数、RGBA8 长度精确匹配）。
- `CancelToken` 协作取消：布尔读写在 GIL 下原子，三检查点配合后端实现，无锁需求。
- 线程安全：模块无共享可变状态。
- GLSL·HDR：不适用（本模块为纯 Python 契约层，RGBA8 契约为显式设计决定）。

---

## src/features/teaching_animation/render/html_renderer_spike/determinism.py (行数: 92)

### 发现

（无）

### 通过项
- 通过（未发现问题）：纯函数实现。
- `build_cache_key` 覆盖 backend/build/source/frame/fps/viewport/dpr/font/asset 全部像素影响因素，哈希截断 16 hex（64bit）无实际碰撞风险。
- `summarize_repetition`/`summarize_out_of_order` 对空输入与键集不一致均 fail 返回 False，防御性 `isinstance(CaptureFailure)` 检查不掩盖错误。
- GLSL·HDR / 信号防递归 / 状态提交一致性：不适用（纯字节比较函数，无 UI/持久化面）。

---

## src/features/teaching_animation/render/html_renderer_spike/fixtures.py (行数: 319)

### 发现

- [AUD-30202] [Suggestion] `attack_data_uri_oversized` 描述为"oversized"但 payload 仅约 20 字节，尺寸向量未被真正行使
  - 位置: src/features/teaching_animation/render/html_renderer_spike/fixtures.py:187-193
  - 证据:
    ```python
    AttackFixture(
        fixture_id="attack_data_uri_oversized",
        category="attack_data",
        vector="data_uri",
        expected_denial_reason="data_uri_blocked",
        description="Oversized data: URI is an explosion/exfiltration vector.",
        html="<html><body><img src=\"data:image/png;base64,AAAA\"></body></html>",
    ),
    ```
  - 影响: 安全回归语料声称覆盖"超大 data: URI"向量，但实际 payload 远低于任何上限（`_MAX_INLINE_VISUAL_URI_CHARS`=4MB、`ResourceBudget.max_data_uri_bytes`=32KB），尺寸维度的拒绝路径从未被测试；语料描述与内容不符会误导 ADR 的覆盖矩阵。
  - 建议: 在模块加载时程序化构造达到/略超上限的 data: URI（如 `"data:image/png;base64," + "A" * (4*1024*1024 + 1)`），或将该 fixture 改名为普通 `attack_data_uri` 并新增真正的 oversized 用例。
  - 置信度: 高

### 通过项
- 纯数据模块，frozen dataclass，无逻辑分支可出错。
- `benign_by_id`/`attack_by_id`/`attack_vectors` 线性扫描对小语料无性能问题。
- 线程安全 / GPU 生命周期 / 异常路径 / 状态提交：不适用（无共享状态、无资源、无控制流）。

---

## src/features/teaching_animation/render/html_renderer_spike/html_spike.py (行数: 280)

### 发现

- [AUD-30203] [Major] 安全门实验对脚本/API 类攻击空转：占位请求被"unrecognized_scheme"拒绝即判 denied，`expected_denial_reason` 从未被断言
  - 位置: src/features/teaching_animation/render/html_renderer_spike/html_spike.py:167-176、270-271
  - 证据:
    ```python
            for request in requests:
                decision = policy.evaluate(request)
                if decision.decision.value != "deny":
                    denied = False
                    reason = decision.reason
                    break
                reason = decision.reason
            verdicts.append(SecurityVerdict(fixture.fixture_id, fixture.vector, denied, reason))
    ```
    ```python
        if not requests:
            requests.append(UrlRequest(url="about:blank", kind="navigation"))
        return tuple(requests)
    ```
  - 影响: 逐条核对 24 个攻击 fixture 的 URL 提取结果：`attack_script_inline`、`attack_event_handler`、`attack_network_xhr`（`fetch('//evil.example')` 无 scheme 标记）、`attack_storage_persist`、`attack_permission_api`、`attack_webgl_canvas`、`attack_css_expression` 共 7 个提取不到 URL，映射为 `about:blank` 占位请求，被策略以 "unrecognized_scheme" 拒绝——这些向量（脚本执行、存储、权限、GPU API）实际根本没被测试，gate 却报 `denied=True`；另有约 8 个 fixture 的记录 reason 与预期不符（如 popup 记为 `network_scheme_blocked` 而非 `popup_blocked`、iframe/object/form/download 同理）。`expected_denial_reason` 字段全仓库（src/ 与 tests/ 已 grep）仅在 fixtures.py:95 定义，无任何断言消费。若未来回归使内联脚本可执行，该安全门仍会通过，产生虚假安全感。
  - 建议: verdict 中断言 `reason == fixture.expected_denial_reason`（至少在测试里）；对无 URL 的脚本/API 类 fixture 要么显式归类为"需真实浏览器验证"（denied=None）而非映射 about:blank，要么补充对 CSP 层（`CspPolicy.to_header()` 含 `script-src 'none'`）的独立断言。
  - 置信度: 高

- [AUD-30204] [Suggestion] 死代码与未用导入：两处 `ToposAssetRegistry()` 赋值无消费、多个导入未使用、`SpikeConfig.budget` 无执行点
  - 位置: src/features/teaching_animation/render/html_renderer_spike/html_spike.py:159、223、34-35、39、45、51、65
  - 证据:
    ```python
            registry = ToposAssetRegistry()  # allowed_ids derived below
            allowed_ids = frozenset(bound)
    ```
    ```python
        registry = ToposAssetRegistry()
        # Build the CSP from the fixture's bound assets (benign fixtures only).
        csp = CspPolicy(allowed_asset_ids=frozenset(fixture.bound_asset_ids)).to_header()
    ```
    （`_capture` 中 223 行的 `registry` 此后从未被引用；导入的 `CaptureEvidence`、`is_uniform_frame`、`rgba_sha256`、`build_cache_key`、`BudgetViolation`、`check_stylesheet_budget` 均未在本模块使用；`SpikeConfig.budget` 字段没有传给任何后端——后端 `capture()` 无 budget 参数。）
  - 影响: 死赋值让人误以为 registry 参与了门禁/资源解析；`budget` 字段形同虚设，调用方设置预算无效。
  - 建议: 删除两处死赋值与未用导入；`budget` 要么接入后端（如换算 timeout/节点上限检查），要么在字段注释中明确"W0-A 未接线"。
  - 置信度: 高

### 通过项
- `GateReport.passed` 聚合逻辑正确：backend 不可用直接 False，四项实验全过才算通过，无假阳性路径。
- `probe_cancel` 对无法演示取消的后端返回 None 而非假装通过。
- `probe_determinism`/`probe_out_of_order` 任一失败即带结构化失败提前返回，不吞错。
- 信号防递归 / 状态提交一致性：不适用（无 Qt 信号、无 undo/持久化）。

---

## src/features/teaching_animation/render/html_renderer_spike/playwright_backend.py (行数: 1,061)

### 发现

- [AUD-30205] [Major] `_inject_csp_meta` 的 `<head[^>]*>` 正则同样匹配 `<header>`，无 `<head>` 的页面 CSP meta 会被注入 body 而被浏览器忽略，CSP 防线静默失效
  - 位置: src/features/teaching_animation/render/html_renderer_spike/playwright_backend.py:1051-1054（注入点）、750（脚本开关）
  - 证据:
    ```python
        meta = f'<meta http-equiv="Content-Security-Policy" content="{csp}">'
        head = re.search(r"<head[^>]*>", html, re.IGNORECASE)
        if head:
            return html[:head.end()] + meta + html[head.end():]
    ```
    ```python
                bypass_csp=False,                  # enforce page CSP on authored content
                java_script_enabled=True,          # the trusted bridge needs JS
    ```
  - 影响: `<header ...>` 开标签匹配 `<head[^>]*>`（`[^>]*` 吃掉 "er ..."）。当页面没有 `<head>` 但 body 含 `<header>`（模型生成的极简 HTML 常见形态，如 `<html><body><header>…`）时，CSP meta 落在 body 内；按 CSP3/HTML 规范，meta 交付的 CSP 必须是 head 子元素，浏览器直接忽略。由于本后端恒开 `java_script_enabled=True`，generic profile 下 `script-src 'none'` 的脚本拦截完全依赖这枚 meta——失效后内联脚本/事件处理器将真实执行（网络仍被路由拦截器阻断，但"scripts disabled"的 DISABLED_FEATURES 声明被绕过）。有正常 `<head>` 的页面首个匹配落在真 head，不受影响，因此该缺陷隐蔽。
  - 建议: 正则收紧为 `<head(?:\s[^>]*)?>`（要求 `<head` 后是空白或紧接 `>`），或用 `html.parser` 定位；对无 `<head>` 的文档走已有的 `<html>` 包裹回退路径而非接受 `<header>` 误匹配。
  - 置信度: 高

- [AUD-30206] [Major] screenshot `clip` 以物理像素传入，而 Playwright clip 语义是 CSS 像素：dpr≠1 时超出视口，尺寸不可整除回退的分辨率会全部捕获失败
  - 位置: src/features/teaching_animation/render/html_renderer_spike/playwright_backend.py:740-741、843-846、1013-1015
  - 证据:
    ```python
            css_w = max(1, round(surface.width / surface.dpr))
            css_h = max(1, round(surface.height / surface.dpr))
    ```
    ```python
                png = page.screenshot(
                    clip={"x": 0, "y": 0, "width": surface.width, "height": surface.height},
                    type="png", omit_background=transparent, timeout=timeout_ms,
                )
    ```
    ```python
        if img.size != (width, height):
            # Defensive: screenshot should already match; never silently resample.
            return None
    ```
  - 影响: Playwright 文档定义 clip 坐标为 CSS 像素。dpr≠1 时 clip（=物理尺寸）大于 CSS 视口（css_w×css_h），输出尺寸取决于 Chromium 是否把 clip 裁剪到视口：若裁剪则碰巧正确；若 `round(surface.width / surface.dpr)` 不能乘回原宽（如 dpr=2.0 且宽度为奇数，Python `round` 为银行家舍入：round(1081/2)=540→1080≠1081），输出尺寸≠surface，`_png_to_rgba8` 返回 None，该分辨率的全部捕获报 CRASH "png decode failed"。生产入口 `html_scene_renderer_v2.py:262` 以 `float(context.surface.dpr)` 传入真实 DPR（本批外文件，仅作引用），dpr≠1 是真实路径而非理论路径。
  - 建议: clip 改用 `css_w`/`css_h`（CSS 像素），或干脆去掉 clip 依赖视口尺寸输出物理像素；对 `round(w/dpr)*dpr != w` 的组合显式返回 INVALID_INPUT，而非让解码层背锅。
  - 置信度: 中（clip 单位为 CSS 像素是文档事实；dpr≠1 下 Chromium 的裁剪/扩展行为未能本机实测）

- [AUD-30207] [Major] 虚拟时钟 pump 的步数/实时预算超限后直接跳变到目标时间，破坏模块自身声明的字节级确定性契约
  - 位置: src/features/teaching_animation/render/html_renderer_spike/playwright_backend.py:357-372
  - 证据:
    ```python
        const maxSteps = 2400;
        const realDeadline = nativePerfNow() + 2500;
        ...
        while (state.now < target && steps < maxSteps && nativePerfNow() < realDeadline) {
          state.now = Math.min(target, state.now + step);
          fireDueTimers(state.now);
          fired += fireRafBatch(state.now);
          steps++;
        }
        if (state.now < target) {
          state.now = target;
          fireDueTimers(target);
        }
    ```
  - 影响: 长时间线（帧号 > 2400，24fps 下约 100 秒以后）或重负载（2500ms 实时预算耗尽，复杂 canvas 场景很可能）时，积分式动画（`state += dt` per rAF）被少执行若干步后直接跳到目标时间——同一 build、同一帧在快/慢机器或不同负载下产生不同像素。这与模块 docstring 及 determinism.py 的"same build + viewport + DPR + font + asset → identical RGBA"契约直接冲突，帧缓存哈希跨机器不可复现，且无任何信号表明发生了跳变（evidence 的 clock 字段只有 raf/steps/now，无 jumped 标志）。
  - 建议: 超限时返回结构化失败（如 BLOCKED_RESOURCE/新增 code）而非静默跳变；至少在 pump 返回值与 CaptureEvidence.notes 中加入 `jumped: true` 标志，让 ADR/缓存层能把该帧判为不可缓存。
  - 置信度: 中（机制由代码确定，触发条件取决于场景时长与负载）

- [AUD-30208] [Major] DOM 诊断在每次捕获的主路径无条件执行，对全部 DOM 节点做强制样式/布局计算，注释却称"仅在判定 uniform 时使用"
  - 位置: src/features/teaching_animation/render/html_renderer_spike/playwright_backend.py:825-833（无条件 evaluate）、537-540（注释）、548-556（每节点 getBoundingClientRect/getComputedStyle）
  - 证据:
    ```python
                dom_diagnostics: Mapping[str, object] | None = None
                try:
                    raw_diagnostics = page.evaluate(_DOM_DIAGNOSTICS)
                    if isinstance(raw_diagnostics, Mapping):
                        dom_diagnostics = dict(raw_diagnostics)
                except PWError:
    ```
    ```js
      const visible = nodes.filter((node) => {
        const rect = node.getBoundingClientRect();
        const style = getComputedStyle(node);
        const opacity = Number.parseFloat(style.opacity || '1');
    ```
  - 影响: 模块注释（537-540 行）明确说诊断"used when a capture is classified as uniform"，但实现放在 seek 之后、截图之前的每次捕获主路径：对 `body.querySelectorAll('*')` 的每个节点（`ResourceBudget.max_dom_nodes` 上限 4000）执行 `getBoundingClientRect` + `getComputedStyle` + 二次 rect，触发全文档强制 recompute。视频导出逐帧执行（千帧量级），复杂页每帧额外数十毫秒，直接侵蚀 4K 实时基准；诊断结果在非 uniform 的成功路径上还写入每帧 evidence（notes 膨胀）。
  - 建议: 把 `_DOM_DIAGNOSTICS` 的 evaluate 移到 `is_uniform_frame` 判定为 uniform 之后、返回 BLANK 失败之前执行（context 尚未关闭），成功帧零开销。
  - 置信度: 高

- [AUD-30209] [Suggestion] 仅捕获 PWTimeout/PWError，其他异常会裸抛出 `capture()`，违反 fail-closed 结构化失败契约
  - 位置: src/features/teaching_animation/render/html_renderer_spike/playwright_backend.py:852-859
  - 证据:
    ```python
        except PWTimeout as exc:
            return self._failure(CaptureFailureCode.TIMEOUT, f"playwright timeout: {exc}", None)
        except PWError as exc:
            # The browser/context may be gone; drop this thread's browser so
            # the next capture relaunches a fresh process.  Report CRASH so the
            # service also replaces this worker (defence in depth).
            _drop_thread_browser()
            return self._failure(CaptureFailureCode.CRASH, f"playwright error: {exc}", None)
    ```
  - 影响: playwright sync API 偶发非 PWError 异常（greenlet 切换错误、驱动管道破损引发的 OSError 等）会穿透 `capture()` 直接抛给调用方；`FrameCaptureBackend` 契约要求返回 `CapturedFrame | CaptureFailure`，`HtmlRendererSpike._capture` 无兜底，单次异常中断整轮门禁/导出任务。
  - 建议: 末尾追加进程边界转换 `except Exception as exc: _drop_thread_browser(); return self._failure(CRASH, f"unexpected: {type(exc).__name__}: {exc}", None)`（保留异常类型信息，不掩盖领域错误）。
  - 置信度: 中

- [AUD-30210] [Suggestion] `_png_to_rgba8` 三种失败原因（PIL 缺失/解码异常/尺寸不匹配）合并报同一个 "png decode failed" CRASH
  - 位置: src/features/teaching_animation/render/html_renderer_spike/playwright_backend.py:861-863、1003-1018
  - 证据:
    ```python
        rgba = _png_to_rgba8(png, surface.width, surface.height)
        if rgba is None:
            return self._failure(CaptureFailureCode.CRASH, "png decode failed", None)
    ```
  - 影响: 尺寸不匹配（见 AUD-30206 的 dpr/clip 问题）会被误报为"解码崩溃"，误导排障方向（去查 PIL/浏览器，而非分辨率参数）；PIL 未安装与真实解码失败也无法区分。
  - 建议: 让 `_png_to_rgba8` 返回 `(bytes | None, reason: str)` 或抛细分异常，失败消息区分 `size_mismatch {got} != {want}` / `pil_unavailable` / `decode_error`。
  - 置信度: 高

- [AUD-30211] [Suggestion] 类型注解缺失：`_thread_browser` 无返回注解、`_intercept` 的 `route` 参数未注解
  - 位置: src/features/teaching_animation/render/html_renderer_spike/playwright_backend.py:84-89、905
  - 证据:
    ```python
    def _thread_browser(
        headless: bool,
        allow_no_sandbox: bool,
        *,
        allow_gpu_apis: bool = False,
    ):
    ```
    ```python
    def _intercept(self, route, url_policy: UrlPolicy) -> None:
    ```
  - 影响: 全模块其余函数注解完整，这两处缺失降低可读性与类型检查覆盖。
  - 建议: 补返回注解（playwright Browser 类型或 `Any` 并注明）与 `route: Any`。
  - 置信度: 高

### 通过项
- 线程安全：Playwright sync API 线程亲和问题已正确处理（每线程独立 runtime/browser，`_PW_RUNTIMES` 增删均在 `_PW_LOCK` 内；`atexit` 收敛停机；同线程 profile 切换先 close 再 relaunch）。
- 取消路径：三检查点（启动前/加载后/截图前）+ `finally: context.close()`，取消或异常都不泄漏 context；fresh ephemeral context per capture 满足无残留不变量。
- 异常映射：PWTimeout→TIMEOUT、PWError→CRASH 且丢弃本线程浏览器做崩溃恢复，不返回陈旧帧。
- uniform 帧 fail-closed：默认拒绝、`allow_uniform` 路径用 `dataclasses.replace` 正确处理 frozen evidence（未原地改 frozen 字段）。
- 资源生命周期：浏览器按线程缓存复用（性能权衡有注释与实测数据支撑），`close()` 幂等。
- 输入验证：空 html、非正 fps、取消前置检查均在入口完成。
- 信号防递归 / 状态提交一致性 / GLSL·HDR：不适用（无 Qt 信号、无 undo/持久化、无着色器代码；RGBA8 为契约明确设计）。

---

## src/features/teaching_animation/render/html_renderer_spike/qt_capture_backend.py (行数: 260)

### 发现

- [AUD-30212] [Major] `url_policy` 参数被接受但从未被使用——后端完全没有 URL 层强制，违反 FrameCaptureBackend 契约
  - 位置: src/features/teaching_animation/render/html_renderer_spike/qt_capture_backend.py:110、整个 capture 函数体
  - 证据:
    ```python
        csp: str,
        url_policy: UrlPolicy,
        font_set: Mapping[str, str] = (),
    ```
    （函数体 116-226 行内 `url_policy` 再无任何引用：无 `setUrlRequestInterceptor`、无 scheme handler、无 `acceptNavigationRequest` 覆写；`UrlPolicy` 导入仅用于签名。）
  - 影响: `FrameCaptureBackend` 协议文档要求"apply the supplied CSP and URL policy (no network, no file scheme)"。此后端仅靠注入的 CSP meta 兜底（meta 失效场景见 AUD-30205 同类风险，且 Qt 路径还会丢 head），任何 CSP 未覆盖的请求类型（导航、新窗口请求等）无第二层拦截；若在有显示环境（is_available 为 True 的场景，模块自述"any future display-backed run"）使用此后端渲染攻击语料，URL 策略形同虚设。
  - 建议: 接 `QWebEngineProfile.setUrlRequestInterceptor`，把 `url_policy.evaluate(UrlRequest(...))` 的 DENY 映射为请求取消；或至少在 docstring 显式声明此后端不执行 URL policy 并让 `is_available` 据此降级。
  - 置信度: 高

- [AUD-30213] [Major] 未实现帧 seek（frame/fps 仅写入 evidence），且 `_extract_body` 丢弃 authored `<head>/<style>`，确定性实验证据空洞
  - 位置: src/features/teaching_animation/render/html_renderer_spike/qt_capture_backend.py:121、157-159、204、239-257
  - 证据:
    ```python
        del allow_uniform
    ```
    ```python
        document = f"<!DOCTYPE html><html><head><meta charset=\"utf-8\">{csp_meta}</head>" \
                   f"<body>{_extract_body(html)}</body></html>"
    ```
    ```python
            notes={"csp": csp, "frame": int(frame), "fps": f"{fps_num}/{fps_den}"},
    ```
    （`frame`/`fps_num`/`fps_den` 全函数仅出现在 204 行 evidence notes；没有注入任何 seek 脚本/时钟控制，动画按墙墙钟相位定格。）
  - 影响: 三个 benign fixture 的样式全部在 `<head><style>` 里（fixtures.py:45-81），`_extract_body` 只保留 body → 页面以无样式纯文本渲染。即使 grab() 在有显示环境可用，`probe_determinism` 对无样式文本做字节比较必然相同——确定性"通过"是空洞结论；帧 seek 缺失意味着 CSS 动画相位不受控。W0-A 已判定此后端失败并转投 playwright，但该类仍被导出且 `is_available` 可能为 True（显示环境），证据价值失真。
  - 建议: 若保留此后端作为对照，注入与 playwright 相同的 seek bridge（可复用 `_SEEK_BRIDGE` 思路）并用 `_inject_csp_meta` 式的整文档注入替代 `_extract_body`；否则在类 docstring 声明其仅服务于"grab() 可行性"单一实验，不用于确定性证据。
  - 置信度: 高

- [AUD-30214] [Major] 具名 `QWebEngineProfile` 是磁盘型 profile，"Ephemeral/无残留"注释只对 cookies 成立
  - 位置: src/features/teaching_animation/render/html_renderer_spike/qt_capture_backend.py:141-143
  - 证据:
    ```python
        # Ephemeral profile: no cookies/storage persist across captures.
        profile = QWebEngineProfile("topos-spike-ephemeral")
        profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
    ```
  - 影响: Qt 语义：带 storageName 的 `QWebEngineProfile(name, parent)` 构造的是磁盘型 profile（只有无参/parent-only 构造才是 off-the-record）；`NoPersistentCookies` 仅把 cookies 降为会话级，HTTP 磁盘缓存、localStorage、IndexedDB 仍落到磁盘（如 `~/.QtWebEngine/topos-spike-ephemeral`）并跨捕获、跨进程残留。与注释及"cancel/crash/release 无残留"门禁矛盾（`attack_storage_persist` 向量正对此）。
  - 建议: 改用 off-the-record 构造（`QWebEngineProfile()` 无名），或显式 `setPersistentStoragePath` 指向临时目录并在捕获后删除，同时 `setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)`。
  - 置信度: 中（Qt 文档行为，未在本环境实测落盘）

- [AUD-30215] [Suggestion] `_DEFAULT_CHROMIUM_FLAGS` 无条件包含 `--no-sandbox`，与 playwright 后端明确的安全立场矛盾
  - 位置: src/features/teaching_animation/render/html_renderer_spike/qt_capture_backend.py:46、53-56
  - 证据:
    ```python
    _DEFAULT_CHROMIUM_FLAGS = "--no-sandbox --disable-gpu --enable-software-rasterization"
    ```
    ```python
    def _ensure_offscreen() -> None:
        """Pin the offscreen QPA platform before any QWidget is constructed."""
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", _DEFAULT_CHROMIUM_FLAGS)
    ```
  - 影响: playwright_backend.py:185-187 明确"渲染不可信模型 HTML，--no-sandbox 绝不默认"；此后端加载同一攻击语料却总是以无沙箱启动（除非环境已预设该变量）。当前 JS 被禁用降低了可利用性，但与项目自身安全基线不一致。
  - 建议: 从默认 flags 中移除 `--no-sandbox`，仅在调用方（CI/root 容器）显式注入。
  - 置信度: 高

- [AUD-30216] [Suggestion] `_poll_cancel` 定时器链在 token 未取消时永不停止；cancel/timeout 早退路径跳过 deleteLater 清理
  - 位置: src/features/teaching_animation/render/html_renderer_spike/qt_capture_backend.py:176-177、181-184、228-233、213-218
  - 证据:
    ```python
        if cancel_token is not None:
            QTimer.singleShot(50, lambda: self._poll_cancel(cancel_token, loop))
    ```
    ```python
    def _poll_cancel(self, token: CancelToken, loop: "QEventLoop") -> None:
        if token.is_cancelled:
            loop.quit()
        else:
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(50, lambda: self._poll_cancel(token, loop))
    ```
  - 影响: 捕获（loop）正常结束后，若 token 从未被 cancel，定时器链每 50ms 空唤醒一次并持续持有 loop/self 引用，进程生命周期内每次"带 token 且未取消"的捕获泄漏一条链；另外 181-184 行 cancel/timeout 早退不经过 213-218 行的 `page/view/profile.deleteLater()`，Qt 对象泄漏到事件循环。
  - 建议: 用可停止的 QTimer 实例（捕获结束/loop 退出后 `stop()`）；将清理收敛为单一函数，所有早退路径统一调用。
  - 置信度: 高

- [AUD-30217] [Suggestion] 在调用线程创建 QApplication/WebEngine，未声明主线程约束
  - 位置: src/features/teaching_animation/render/html_renderer_spike/qt_capture_backend.py:139-140
  - 证据:
    ```python
        app = QApplication.instance() or QApplication(["topos-qt-html-capture"])
    ```
  - 影响: Qt 要求 QApplication 与所有 QWidget（含 WebEngine）生命周期在主线程；playwright 后端注释自述 RenderWorker 在池线程跑 capture，若该模式套用到此后端，将在工作线程创建 QApplication/WebEngine，导致崩溃或未定义行为。
  - 建议: 入口断言 `QThread.currentThread() is app.thread()`，或类 docstring 声明"仅限主线程调用"。
  - 置信度: 中

### 通过项
- `is_available` 做真实自探测并 memoize，统一帧 fail-closed（`del allow_uniform` 有注释说明理由，仅 playwright 允许豁免）。
- 异常路径：PyQt 导入失败→UNSUPPORTED；读回长度不足→CRASH（先 detach page 再返回）。
- 导入惰性（import-safe），不强制非 WebEngine 环境起 Qt。
- 信号防递归：`loadFinished→loop.quit()` 单向单次，无递归风险。
- GLSL·HDR / 状态提交一致性：不适用。

---

## src/features/teaching_animation/render/html_renderer_spike/security.py (行数: 419)

### 发现

- [AUD-30218] [Major] 主机白名单解析与 URL 规范不一致：反斜杠混淆可使 `allowed_network_hosts` 被绕过
  - 位置: src/features/teaching_animation/render/html_renderer_spike/security.py:150-153
  - 证据:
    ```python
            host = rest.split("/", 1)[0].split("@")[-1].split(":", 1)[0].lower().rstrip(".")
            if self._allowed_network_hosts and host not in self._allowed_network_hosts:
                return UrlPolicyDecision(ResourceDecision.DENY, "network_host_not_allowed", request)
    ```
  - 影响: 主机提取只按 `/` 分割 authority。构造 `https://evil.example\@allowed.com/x`：候选串为 `evil.example\@allowed.com`，`split("@")[-1]` 得 `allowed.com` → 命中白名单判 ALLOW；而按 WHATWG URL 规范，特殊方案（https）中 `\` 等价 `/`，浏览器实际解析的主机是 `evil.example`，凭据段为空——即策略批准了 allowed.com、实际连接 evil.example。该白名单在生产 freeform 路径真实启用（`export_service.py:299`、`html_layer_commands.py:347/493`、`html_generation_protocol.py:401` 等从 AI 协议 metadata 传入；本批外文件，仅作引用）。缓解因素：若拦截器收到的 URL 已被 Chromium 规范化（`\`→`/`），则此路径不可达——但 UrlPolicy 定位为可独立使用的纯安全组件（`_requests_for_attack` 也以原始字符串喂入），不应依赖上游规范化；且 `\n`/`\r`（浏览器解析前会剥离）也不在 `url_contains_whitespace` 检查内。
  - 建议: 用 `urllib.parse.urlsplit(...).hostname`（行为与浏览器规范更接近）解析主机；对包含 `\` 或 ASCII 控制字符的 URL 直接 DENY。
  - 置信度: 中（解析歧义为规范事实；经由 playwright 拦截器的实际可利用性取决于 Chromium 交付的 URL 是否已规范化）

- [AUD-30219] [Suggestion] data: URI 预算双轨且互不一致：4MB 字符上限生效、`max_data_uri_bytes`=32KB 无任何运行时执行点
  - 位置: src/features/teaching_animation/render/html_renderer_spike/security.py:201、335、357-364
  - 证据:
    ```python
    _MAX_INLINE_VISUAL_URI_CHARS = 4 * 1024 * 1024
    ```
    ```python
        max_data_uri_bytes: int = 32_000      # individual inline data: payload
    ```
    ```python
    def check_stylesheet_budget(css: str, budget: ResourceBudget) -> tuple[BudgetViolation | None, int]:
        """Return the first budget violation in a stylesheet plus its keyframe count."""
        keyframes = css.lower().count("@keyframes")
        if len(css) > budget.max_stylesheet_chars:
    ```
  - 影响: `UrlPolicy` 只按 4MB 字符数限制 data: URI；`ResourceBudget.max_data_uri_bytes`（32KB）经 `to_dict()` 进入 evidence/ADR，却没有任何检查器执行它（`check_stylesheet_budget` 只查样式表字符数与 keyframes 数）——宣称的预算与实际执行不符，攻击面评估失真。
  - 建议: 二选一：统一到单一上限，或让 `check_stylesheet_budget`/独立检查器真正逐 URI 执行 `max_data_uri_bytes`（含 base64 解码后字节）。
  - 置信度: 高

### 通过项
- 默认拒绝的 scheme 矩阵完整：file/javascript/mailto/data/blob/非 https 网络全部有独立 reason；`topos-asset` 严格按绑定 id（含命名空间前缀）拒绝未绑定资产。
- `_split_scheme` 对大小写、非法 scheme 首字符/字符集、opaque 形式（`data:`、`mailto:`）处理正确。
- `_safe_visual_data_uri` 的 MIME 白名单精确排除 `text/html`、`application/javascript`、SVG，且限定请求类别与长度。
- `ensure_font_lock_csp` 合并逻辑幂等（已含 data: 时原样返回）。
- 纯数据/纯函数模块，无线程、GPU、信号、持久化面；类型注解完整。

---

## 附：跨文件核对

- `playwright_backend.py:41` 的 `from ...core.html_page_deck import _PAGE_VIEWPORT_RECOVERY_JS` 解析为 `src/features/teaching_animation/core/html_page_deck.py:160`，符号存在，导入有效。
- `__init__.py` 的 re-export 与 8 个子模块导出逐一比对一致，无悬空导出。

## 统计

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 9 | AUD-30203, 30205, 30206, 30207, 30208, 30212, 30213, 30214, 30218 |
| Suggestion | 11 | AUD-30200, 30201, 30202, 30204, 30209, 30210, 30211, 30215, 30216, 30217, 30219 |
