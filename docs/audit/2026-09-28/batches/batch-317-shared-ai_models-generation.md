# batch-317 审查报告 — src/shared/ai_models/generation/

批次：batch-317 [P2]；编号区间 AUD-31700–AUD-31799；日期：2026-09-28。
对照阅读（用于严重度校准，未修改）：`src/features/edit/ai_editing/http_generation_provider.py`、
`media_generation_service.py`、`generation_adapter.py`、`profile_tts_provider.py`、
`app/controllers/ai_editing_mixin.py`（注册点）、`provider_profiles.py`、`generation_models.py`。

## src/shared/ai_models/generation/volcengine_generation_provider.py (行数: 564)

### 发现

- [AUD-31700] [Major] `_open` 的 certifi try 块吞掉 opener 抛出的 OSError 家族异常并无条件重发同一 POST（重复请求/重复计费风险）
  - 位置: src/shared/ai_models/generation/volcengine_generation_provider.py:177-182
  - 证据:
    ```python
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
        return self._opener(request, timeout=timeout, context=context)
    except (ImportError, OSError, TypeError):
        return self._opener(request, timeout=timeout)
    ```
  - 影响: 生产路径（opener 为 stdlib `urlopen`）下 opener 调用在 try 块内。`HTTPError`、`URLError`、`socket.timeout`（3.10+ 为 `TimeoutError`）、`ConnectionResetError` 均为 `OSError` 子类，会被该 except 捕获后再次发送同一生成 POST。任何 4xx/5xx/瞬时网络错误都会触发一次立即的重复提交：对计费型生成接口（图片/TTS）可能产生第二次生成任务即双重计费；若第二次成功还会掩盖首次错误。`_request_json`/`_request_bytes` 外层的 HTTPError/网络错误处理通常因第二次抛同样异常而"看起来正常"，缺陷长期不可见。
  - 建议: 只把 context 构建放进 try（`ImportError/OSError/TypeError` 仅护 `import certifi`/`create_default_context`），opener 调用移到 try 之外，context 可用时再附加参数。
  - 置信度: 高

- [AUD-31701] [Major] endpoint 未做任何验证：空/相对端点使 `urllib.request.Request` 在 try 外抛原始 `ValueError` 逃逸 provider 错误契约
  - 位置: src/shared/ai_models/generation/volcengine_generation_provider.py:198-202（`_url`）、105-110 与 144-149（Request 构造在 try 之外）
  - 证据:
    ```python
    def _url(self, suffix: str) -> str:
        endpoint = self._profile.endpoint.rstrip("/")
        if endpoint.endswith(suffix):
            return endpoint
        return f"{endpoint}{suffix}"
    ```
    ```python
    request = urllib.request.Request(
        url,
        data=json.dumps(dict(body), ensure_ascii=False).encode("utf-8"),
        headers=dict(headers),
        method=method,
    )
    try:
        response = self._open(request)
    ```
  - 影响: `AiProviderProfile.endpoint` 默认 `""`。endpoint 为空或无 scheme 时 `_url` 返回 `"/images/generations"` 这类相对 URL，`Request()` 构造（在 try 之外）直接抛 `ValueError: unknown url type`；`generate()` 只捕获 `_VolcengineProviderError`，原始 ValueError 逃逸出 provider 契约（经 `ProfileAwareTtsProvider.synthesize` 直调路径时无兜底向上冒泡）。注册点 `ai_editing_mixin.py:724-737` 只校验 modalities 与凭据、不校验 endpoint，且 volcengine 工厂命中后不再回退到带 `GEN_PROVIDER_CONFIG_INVALID` 校验的 `HttpGenerationProvider`。同类逃逸：`_cache_b64`/`_cache_audio` 的磁盘 OSError、`int(constraints.get("sample_rate"))` 的 ValueError 也以原始异常冒出。
  - 建议: 仿照 `HttpGenerationProvider.generate` 在入口用 `urlparse` 校验 scheme/netloc，非法时返回 `GEN_PROVIDER_CONFIG_INVALID` 错误结果；`generate()` 兜底捕获非契约异常并映射为统一错误码。
  - 置信度: 高

- [AUD-31702] [Major] `is_cancelled` 仅在网络请求前检查一次，阻塞 HTTP 期间完全不可取消
  - 位置: src/shared/ai_models/generation/volcengine_generation_provider.py:244-253（图像）、376-384（TTS）
  - 证据:
    ```python
    try:
        secret = self._secret(request)
        if is_cancelled is not None and is_cancelled():
            return self._error(
                request,
                _VolcengineProviderError("GEN_PROVIDER_CANCELLED", "generation cancelled"),
            )
        if progress is not None:
            progress(0.05, "正在提交 Seedream 图片生成任务")
        payload = self._request_json(...)
    ```
  - 影响: 取消检查后紧接同步阻塞的 `urlopen`（默认 `request_timeout_seconds=120`），期间用户取消毫无作用；生成完成后结果仍写入缓存并作为成功 `GenerationResult` 返回（服务层 `media_generation_service._execute_generation` 事后丢弃 CANCELLING 任务的结果，但请求已发出、已计费，缓存文件已落盘）。兄弟实现 `HttpGenerationProvider` 在每次轮询迭代中检查 `is_cancelled`，本适配器为单发请求无中途检查。取消顺序也有小疵：先做 keychain 读取（`_secret`）再查取消。
  - 建议: 在响应返回后、写缓存/构造候选前再检查一次 `is_cancelled`；长阻塞请求考虑用可中断机制（如在工作线程包一层可 join/放弃的 future），至少保证结果落盘前尊重取消。
  - 置信度: 高

- [AUD-31703] [Major] 图像尺寸静默强制 2048×2048 正方形，`aspect_ratio` 请求字段被完全忽略
  - 位置: src/shared/ai_models/generation/volcengine_generation_provider.py:288-303
  - 证据:
    ```python
    constraints = dict(request.recipe.output_constraints) if request.recipe else {}
    width = int(request.recipe.width) if request.recipe and request.recipe.width else 2048
    height = int(request.recipe.height) if request.recipe and request.recipe.height else 2048
    # Agent Plan currently rejects the old 1024 default.  Keep the adapter
    # on a safe 2K square minimum until the UI exposes provider dimensions.
    if width * height < 3_686_400:
        width = height = 2048
    return {
        "model": self._profile.model_id,
        "prompt": request.prompt,
        "size": f"{width}x{height}",
    ```
  - 影响: `3_686_400 = 1920×1920`。任何总像素不足的合法请求（如 1280×720、1080×1920 竖屏）都被改写为 2048×2048 正方形——静默改变用户请求的分辨率与宽高比，而非等比放大。更早的问题是 `request.aspect_ratio` 与 `recipe.aspect_ratio` 均未参与映射：harness 主生产路径 `generation_adapter.invocation_to_generation_request`（generation_adapter.py:85-105）构造的请求 recipe=None 但携带 `aspect_ratio="16:9"` 等输入，实际总是生成 1:1。用户在 UI 明确选择的画幅被无声丢弃。
  - 建议: 先按 `request.aspect_ratio`（或 recipe.aspect_ratio）推导宽高，再在不满足 provider 最小像素时等比放大（保持比率）；确无 aspect 信息时才退回 2K 方形。
  - 置信度: 高

- [AUD-31704] [Major] 多个 b64 候选写同一缓存文件 `{request_id}.png`，后者覆盖前者
  - 位置: src/shared/ai_models/generation/volcengine_generation_provider.py:315-323、348-351
  - 证据:
    ```python
    handle = str(raw.get("url") or "").strip()
    if not handle and raw.get("b64_json"):
        try:
            handle = self._cache_b64(
                request.request_id,
                str(raw["b64_json"]),
                suffix=".png",
            )
    ```
    ```python
    root = default_cache_dir() / "generated_media" / "volcengine"
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{request_id}{suffix}"
    ```
  - 影响: 目标文件名只含 request_id，不含候选索引。当响应以 `b64_json` 返回多个候选（循环 index=0,1,...）时，每次迭代都 `os.replace` 到同一路径，最终所有候选的 `preview_handle` 指向同一文件且内容是最后一次写入——候选素材箱中出现"多张相同图片"，前面的候选数据实际丢失。同 request 的 url 候选不受影响。
  - 建议: `_cache_b64` 增加候选索引参数（与 `asset_id` 的 `-c{index+1}` 对齐），文件名形如 `{request_id}-c{index+1}.png`。
  - 置信度: 高

- [AUD-31705] [Major] 参考素材被静默丢弃，但 provenance 仍记录 `reference_asset_ids`/`reference_bindings`，溯源失真
  - 位置: src/shared/ai_models/generation/volcengine_generation_provider.py:215-222（记录）、296-303（请求体未含任何参考输入）、91-95（无数据策略防护）
  - 证据:
    ```python
    input_prompt_hash=request.prompt_hash,
    reference_asset_ids=request.effective_reference_asset_ids,
    parent_skill_id=request.requested_by,
    ...
    reference_bindings=tuple(
        binding.to_dict() for binding in request.effective_reference_bindings
    ),
    ```
    ```python
    return {
        "model": self._profile.model_id,
        "prompt": request.prompt,
        "size": f"{width}x{height}",
        "sequential_image_generation": "disabled",
        "response_format": "url",
        "watermark": bool(constraints.get("watermark", True)),
    }
    ```
  - 影响: harness 路径会携带 `reference_asset_ids`（generation_adapter.py:103），但 `_body` 从不上传参考图，请求实为纯文生图。后果二重：(1) 用户期望的参考图引导生成被无声忽略；(2) `GenerationProvenance` 把从未使用的素材记为输入，审计链路（TAEP provenance 的存在意义）记录与事实不符。对照 `HttpGenerationProvider.generate:157-162`，其有 `allows_media_upload` 策略红线防护（`GEN_PROVIDER_MEDIA_UPLOAD_BLOCKED`），本适配器既不使用也不阻止参考请求。
  - 建议: 请求携带参考素材时要么显式报 `GEN_PROVIDER_MEDIA_UPLOAD_BLOCKED`/不支持参考的错误，要么真正上传；provenance 只应记录实际发送的输入。
  - 置信度: 高（代码行为确定；"故意不支持参考"亦不能解释 provenance 记录未用输入）

- [AUD-31706] [Suggestion] TTS `_body` 与 `_cache_audio` 的 output_format 来源不一致，缓存扩展名可能与实际音频容器不符
  - 位置: src/shared/ai_models/generation/volcengine_generation_provider.py:428-432 vs 524-527
  - 证据:
    ```python
    output_format = str(
        (request.recipe.output_format if request.recipe else "")
        or constraints.get("format")
        or "mp3"
    ).lower()
    ```
    ```python
    output_format = str(
        request.recipe.output_format if request.recipe else "mp3"
    ).lower()
    suffix = output_format if output_format in {"mp3", "wav", "pcm"} else "mp3"
    ```
  - 影响: recipe=None 且 `constraints["format"]="wav"` 时，请求按 wav 编码，但落盘文件名固定 `.mp3`，字节与扩展名不符。ffmpeg 按内容嗅探可解码（profile_tts_provider._decode_audio），但素材箱预览/外部打开时按错误扩展名处理；不在白名单的格式（如 ogg）也被强标 mp3。
  - 建议: 抽取单一 `resolve_output_format(request)` 供两处使用，保证请求参数与文件后缀一致。
  - 置信度: 高

- [AUD-31707] [Suggestion] `max(1, request.budget.max_candidates)` 使预算为 0 时仍回收 1 个候选
  - 位置: src/shared/ai_models/generation/volcengine_generation_provider.py:312
  - 证据:
    ```python
    for index, raw in enumerate(raw_items[: max(1, request.budget.max_candidates)]):
    ```
  - 影响: `GenerationBudget.max_candidates` 文档定义为"一次请求最多回收几个候选"的硬上限；设 0（或负数）的防御性配置被静默放宽为 1，违反预算合同语义。
  - 建议: 直接使用 `max(0, request.budget.max_candidates)`；0 时返回明确的预算错误或不返回候选。
  - 置信度: 高

- [AUD-31708] [Suggestion] `_secret` 先访问凭据存储（keychain/文件 IO）再做模态检查；逐 cue TTS 场景每条字幕重复读取凭据
  - 位置: src/shared/ai_models/generation/volcengine_generation_provider.py:85-96、244-246、377-379
  - 证据:
    ```python
    def _secret(self, request: GenerationRequest) -> str:
        secret = self._credential_store.get_secret(self._profile.credential_ref)
        if not secret:
            raise _VolcengineProviderError(
                "GEN_PROVIDER_CREDENTIAL_MISSING", "credential is not configured",
            )
        if request.modality not in self.supported_modalities:
            raise _VolcengineProviderError(
                "GEN_PROVIDER_UNSUPPORTED_MODALITY",
                f"provider does not support {request.modality.value}",
            )
    ```
  - 影响: 不支持的模态也会触发一次 keychain/配置文件读取（`FileCredentialStore.get_secret` 每次整读配置文件）；`ProfileAwareTtsProvider.synthesize` 按字幕逐 cue 调 `generate`，N 条字幕即 N 次凭据读取，且已取消的请求（检查在 `_secret` 之后）也先做这次 IO。对照 `HttpGenerationProvider` 的顺序：模态→端点→策略→凭据。
  - 建议: 调整 `_secret` 内部顺序（先模态后取密钥），或拆成 `_check_modality` + `_fetch_secret`；provider 实例内可做一次性凭据缓存（注意失效策略）。
  - 置信度: 高

- [AUD-31709] [Suggestion] `profile.limits.max_retries` 完全未使用，429/5xx/瞬时网络错误无任何重试
  - 位置: src/shared/ai_models/generation/volcengine_generation_provider.py:115-120（仅 174 行用到 limits 的 timeout）
  - 证据:
    ```python
    except urllib.error.HTTPError as exc:
        raise self._http_error(exc.code) from exc
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        raise _VolcengineProviderError(
            "GEN_PROVIDER_NETWORK_ERROR", "provider network request failed",
        ) from exc
    ```
  - 影响: `ProviderLimits.max_retries`（默认 2）是既有配置合同，兄弟实现 `HttpGenerationProvider:367-390` 按 408/429/5xx 重试；本适配器一次失败即终态，用户侧表现为偶发抖动直接失败。全文件无 `max_retries` 引用。
  - 建议: 对幂等性可接受的场景（或 AUD-31700 修复时顺带）实现与 `HttpGenerationProvider` 一致的受限重试（仅 408/429/5xx，尊重 `max_retries`）。
  - 置信度: 高

- [AUD-31710] [Suggestion] generated_media 缓存目录无容量上限与清理策略，生成文件无限累积
  - 位置: src/shared/ai_models/generation/volcengine_generation_provider.py:348-349、528-529
  - 证据:
    ```python
    root = default_cache_dir() / "generated_media" / "volcengine"
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{request_id}.{suffix}"
    ```
  - 影响: 每次生成（单响应上限 64MB）都在 `<项目根>/models/ai_editing/generated_media/volcengine/` 新增文件，从不删除；长期使用下缓存无界增长，占用项目所在磁盘。
  - 建议: 增加 LRU/容量上限的清理（注册到应用的缓存管理），或复用统一临时目录并按会话清理。
  - 置信度: 高

- [AUD-31711] [Suggestion] `_parse_json_audio` 无法解析顶层 JSON 数组，而 `_looks_like_json` 明确接受 `[` 前缀
  - 位置: src/shared/ai_models/generation/volcengine_generation_provider.py:445-448、460-486
  - 证据:
    ```python
    @staticmethod
    def _looks_like_json(raw: bytes) -> bool:
        stripped = raw.lstrip()
        return stripped.startswith(b"{") or stripped.startswith(b"[")
    ```
    ```python
    payloads: list[Mapping[str, Any]] = []
    try:
        one = json.loads(text)
        if isinstance(one, Mapping):
            payloads.append(one)
    except json.JSONDecodeError:
        ...
    if not payloads:
        return b"", _VolcengineProviderError(
            "GEN_PROVIDER_SCHEMA_INVALID", "voice response is not valid audio",
        )
    ```
  - 影响: 若响应体为合法的 JSON 数组信封（整体 `json.loads` 成功、非 Mapping、无异常触发逐行分支），`payloads` 保持空，被误判为 `GEN_PROVIDER_SCHEMA_INVALID`。逐行分包分支只在整体解析抛 `JSONDecodeError` 时进入，数组形态落入两不管地带。
  - 建议: 整体解析结果为 list 时遍历其 Mapping 元素并入 payloads。
  - 置信度: 中（取决于服务端是否可能返回数组信封；`_looks_like_json` 接受 `[` 说明作者考虑过该形态）

- [AUD-31712] [Suggestion] 请求字段映射不全：`recipe.seed`、`style`、`negative_prompt` 被忽略
  - 位置: src/shared/ai_models/generation/volcengine_generation_provider.py:296-303
  - 证据:
    ```python
    return {
        "model": self._profile.model_id,
        "prompt": request.prompt,
        "size": f"{width}x{height}",
        "sequential_image_generation": "disabled",
        "response_format": "url",
        "watermark": bool(constraints.get("watermark", True)),
    }
    ```
  - 影响: `GenerationRecipe` 定义了 `seed/style/negative_prompt` 字段（generation_models.py:253-255），Ark images/generations 接口支持 `seed` 参数；可复现生成（provenance 记录 seed 但请求从未发送，与 AUD-31705 同类的溯源失真——`_provenance` 未记 seed，故仅是能力缺失）与风格控制被静默丢弃。
  - 建议: 至少透传 `recipe.seed`；style 可并入 prompt，negative_prompt 视接口能力决定是否上报"不支持"。
  - 置信度: 中（部分字段是否映射属产品决策，但 seed 有明确接口对应）

- [AUD-31713] [Suggestion] 质量杂项：未使用的 `urllib.parse` 导入、`isinstance(x, typing.Mapping)` 弃用用法、公共工厂返回私有类型
  - 位置: src/shared/ai_models/generation/volcengine_generation_provider.py:18、22（导入）、275/313/463/478（isinstance）、546-549（工厂签名）
  - 证据:
    ```python
    import urllib.parse
    ```
    ```python
    from typing import Any, Callable, Mapping
    ...
    if isinstance(usage, Mapping):
    ```
    ```python
    def make_volcengine_generation_provider(
        profile: AiProviderProfile,
        credential_store: CredentialStore,
    ) -> _VolcengineProviderBase | None:
    ```
  - 影响: `urllib.parse` 全文件无引用（死导入）；`typing.Mapping` 用于 isinstance 自 3.9 起弃用，应改 `collections.abc.Mapping`；`make_volcengine_generation_provider` 是公共 API 却以私有类 `_VolcengineProviderBase` 作返回类型，调用方无法以公共协议（`generation_provider.GenerationProvider` Protocol）标注。另有 `_request_json`/`_request_bytes` 的发送与超限检查逻辑整段复制（98-135 vs 137-164），可提取公共 `_send`。
  - 建议: 删除死导入；改用 `collections.abc.Mapping`；返回类型改公共 Protocol；合并两请求方法的重复段。
  - 置信度: 高

### 通过项

- 线程安全：provider 无共享可变状态（profile 为 frozen dataclass、每次请求局部变量）；缓存写入采用 mkstemp + `os.replace` 原子替换，无并发半写文件；`progress`/`is_cancelled` 回调由服务层在工作线程调度，无 Qt 信号参与。
- GPU 资源生命周期：不适用（纯网络适配层，无 ModernGL/OpenGL 资源）。
- GLSL·HDR 正确性：不适用（无着色器与 HDR 数据路径）。
- 信号防递归：不适用（无 Qt 信号连接）。
- 状态提交一致性：不触碰 undo/redo/timeline；失败/取消路径只返回带错误码的 `GenerationResult`，无部分提交；候选由 GenerationService 入"生成素材箱"，符合"结果不自动进 timeline"红线。
- 错误消毒：secret 仅进请求头；错误消息全部静态或仅含 HTTP 状态码，响应体不外泄；`raise ... from exc` 的异常链不含请求头。
- 响应硬限制：`read(_MAX_RESPONSE_BYTES + 1)` + 长度判断的 64MB 上限模式正确（JSON 与 bytes 两路径均有）。
- JSON/b64 输入验证：payload 必须 dict、`data` 必须 list、元素 Mapping 检查、`base64.b64decode(validate=True)`、TTS 逐 envelope `code` 码校验（含 45000030 专项映射）、空 data 终结帧容忍逻辑正确。
- 原子写盘：fsync + `os.replace` + finally 清理临时文件模式正确（两处缓存函数）。
- 类型注解：函数签名与返回类型注解完整（`opener: Callable[..., Any]` 为测试注入需要，可接受）。
