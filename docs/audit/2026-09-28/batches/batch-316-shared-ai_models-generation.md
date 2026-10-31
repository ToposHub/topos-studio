# 批次报告：batch-316 [P2] src/shared/ai_models/generation//

- 审查日期：2026-09-28
- 编号区间：AUD-31600–AUD-31623（共 24 项：Critical 0 / Major 10 / Suggestion 14）
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失
- 说明：本批次为纯数据/凭据/任务层，不涉及 OpenGL、GLSL、Qt 信号与 undo/redo 栈；相关维度以"不涉及"列入通过项。跨层依赖与外部数据处理按 .clinerules 红线（分层、外部数据须验证、不静默降级）审查。

---

## src/shared/ai_models/generation/__init__.py (行数: 8)

### 发现

（无）

### 通过项
- 通过（无逻辑）：8 行纯模块 docstring，说明 shared 生成层与 edit 侧 re-export shim 的关系；无 import、无导出、无代码。

---

## src/shared/ai_models/generation/credential_store.py (行数: 342)

### 发现

- [AUD-31600] [Major] macOS `security` CLI 以 argv 传递明文 API key，进程列表可被本机其他用户窥视
  - 位置: src/shared/ai_models/generation/credential_store.py:65-73
  - 证据:
    ```python
    result = subprocess.run(
        [
            "security", "add-generic-password", "-U",
            "-s", service, "-a", username, "-w", password,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    ```
  - 影响: macOS 默认允许任意本地用户通过 `ps -ax -o command` 看到其他用户进程的完整 argv。注释只排除了 shell history 与日志（"never a shell ... never persisted"），但写入 Keychain 的瞬间明文 key 对同机所有用户可见，是真实的 secret 泄漏面。
  - 建议: 改用 `security -i` 交互模式从 stdin 喂命令与口令，或经 pyobjc 调 Security.framework；至少在注释中如实披露 argv 暴露面并评估受影响平台。
  - 置信度: 高

- [AUD-31601] [Major] HybridCredentialStore 写单后端读定优先：偏好切换后旧后端残留的过期 key 遮蔽新 key
  - 位置: src/shared/ai_models/generation/credential_store.py:197-210
  - 证据:
    ```python
    def get_secret(self, credential_ref: str) -> str | None:
        if self._prefer == CREDENTIAL_STORAGE_FILE:
            secret = self._file_store.get_secret(credential_ref)
            return secret or self._secure_store.get_secret(credential_ref)
        secret = self._secure_store.get_secret(credential_ref)
        return secret or self._file_store.get_secret(credential_ref)

    def set_secret(self, credential_ref: str, secret: str) -> None:
        if self._prefer == CREDENTIAL_STORAGE_FILE:
            self._file_store.set_secret(credential_ref, secret)
            return
    ```
  - 影响: `prefer="file"` 时 set 只写文件、不清理 keychain 旧值。用户在 file 模式下轮换 key 后切回 `prefer="keychain"`（get_credential_storage_preference 切换即生效），读取命中 keychain 里轮换前的旧 key 并遮蔽文件中的新值——新 key 被静默忽略，鉴权持续使用过期/已撤销凭据。
  - 建议: set_secret 写入首选后端成功后删除另一后端同 ref 旧值（或双写保持一致）；get 检测到两后端值不一致时记录告警。
  - 置信度: 高

- [AUD-31602] [Suggestion] 同一 JSON 文件多处 read-modify-write 无并发防护，后写者整文件覆盖
  - 位置: src/shared/ai_models/generation/credential_store.py:236-254（另见 281-288 preference、provider_profiles.save_to_user_file）
  - 证据:
    ```python
    def set_secret(self, credential_ref: str, secret: str) -> None:
        config = load_user_ai_config(self._path)
        credentials = config.get("credentials")
        if not isinstance(credentials, dict):
            credentials = {}
        credentials = dict(credentials)
        credentials[credential_ref] = secret
        config["credentials"] = credentials
        save_user_ai_config(config, self._path)
    ```
  - 影响: FileCredentialStore、set_credential_storage_preference、ProfileRegistry.save_to_user_file 均对 `ai_profiles.json` 做 load→改→整文件覆盖；UI 线程与 worker 线程（或多实例）并发写时，后写者覆盖先写者，凭据/偏好静默丢失。单线程下无影响。
  - 建议: 进程内锁 + 文件锁串行化所有写入，或收敛到单一写入入口。
  - 置信度: 中

- [AUD-31603] [Suggestion] `security` 子进程调用无 timeout，Keychain 受锁时可能无限阻塞调用线程
  - 位置: src/shared/ai_models/generation/credential_store.py:48-53（79-85 同）
  - 证据:
    ```python
    result = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-a", username, "-w"],
        capture_output=True,
        text=True,
        check=False,
    )
    ```
  - 影响: Keychain 被锁定或策略要求交互确认时 `security` 可能挂起；若在 UI 线程解析凭据将冻结界面，且无 timeout 无法中止。
  - 建议: `subprocess.run(..., timeout=N)`，超时按"无凭据"降级并记日志。
  - 置信度: 中

- [AUD-31604] [Suggestion] 模块级构造三个 store 作"协议断言"，`type: ignore` 反而抑制真实的协议检查
  - 位置: src/shared/ai_models/generation/credential_store.py:338-342
  - 证据:
    ```python
    # 鸭子类型断言：KeyringCredentialStore 满足 CredentialStore Protocol
    #（Protocol 是结构化类型，无需显式继承；此行仅用于静态检查器与文档可读性）
    _ASSERT_PROTOCOL: CredentialStore = KeyringCredentialStore()  # type: ignore[assignment]
    _ASSERT_FILE_PROTOCOL: CredentialStore = FileCredentialStore()  # type: ignore[assignment]
    _ASSERT_HYBRID_PROTOCOL: CredentialStore = HybridCredentialStore()  # type: ignore[assignment]
    ```
  - 影响: import 时构造三个无用对象；`type: ignore[assignment]` 把静态检查器对该赋值的任何真实协议偏差报警一并吞掉，"断言"失去其宣称的静态检查作用。
  - 建议: 删除运行时构造，改为 `if TYPE_CHECKING: _assert: CredentialStore = KeyringCredentialStore()` 且不加 ignore，让 mypy 真实验证结构化类型。
  - 置信度: 高

### 通过项
- 异常与取消路径：keyring 后端探测失败有日志并显式降级（macOS 走 security CLI、其余内存降级且警告"重启后失效"）；set_secret 失败记日志后向上抛不静默；delete_secret 幂等。
- 输入验证：storage location 走 frozenset 白名单，`set_credential_storage_preference` 对非法值抛 ValueError；`migrate_credentials_to_file` 过滤非 str/空 ref，且不覆盖已有文件凭据。
- 退火路径设计：Hybrid 双后端读取保证切换偏好不立即失去旧位置 key（AUD-31601 为写侧不一致的残留问题）。
- 线程安全（单实例共享内存降级 dict 的 GIL 级安全；后端探测重复执行无副作用）。
- GLSL·HDR / GPU 资源生命周期 / 信号防递归 / undo·redo·revision：不涉及（纯凭据数据层，无渲染、无 Qt、无工程状态）。

---

## src/shared/ai_models/generation/generation_models.py (行数: 618)

### 发现

- [AUD-31605] [Suggestion] GenerationConsent.is_complete 不校验 provider_id/target_location 非空，§2.3"每次确认 provider/目标位置"未在合同层强制
  - 位置: src/shared/ai_models/generation/generation_models.py:152-159
  - 证据:
    ```python
    @property
    def is_complete(self) -> bool:
        """同意是否完整（confirmed + 四项 acknowledge 都到位）。"""
        return (
            self.confirmed
            and self.cost_acknowledged
            and self.upload_acknowledged
            and self.copyright_acknowledged
        )
    ```
  - 影响: `provider_id=""`、`target_location=""` 的 consent 仍算"完整"；若 GenerationService 以 is_complete 为唯一 gate，可构造出未指明 provider/目标位置的"有效"同意，与 docstring 引用的 §2.3 要求不符。
  - 建议: is_complete 增加 `and bool(self.provider_id)`（按需求可含 target_location）。
  - 置信度: 中（下游服务可能另行校验；本文件是合同层，自身未强制）

- [AUD-31606] [Suggestion] GeneratedAsset.with_state 不校验状态机合法迁移，terminal 态可被复活/绕过
  - 位置: src/shared/ai_models/generation/generation_models.py:540-542（状态机声明见 478-492）
  - 证据:
    ```python
    def with_state(self, state: GeneratedAssetState) -> "GeneratedAsset":
        from dataclasses import replace
        return replace(self, state=state)
    ```
  - 影响: 枚举文档声明 `generated → previewed → imported / discarded` 且 IMPORTED/DISCARDED 为 terminal，但 with_state 接受任意跳转：DISCARDED→IMPORTED 可复活已丢弃候选、IMPORTED→GENERATED 可回退，`importable` gating 因此可被状态回写绕过。
  - 建议: with_state 内校验合法迁移表（至少禁止离开 terminal 态），非法迁移抛 ValueError。
  - 置信度: 高

- [AUD-31607] [Suggestion] GenerationRecipe.migrate 对外部 0.x payload 的数值/枚举转换无容错，抛裸 ValueError 且缺上下文
  - 位置: src/shared/ai_models/generation/generation_models.py:301-306（另 313-318、326-333）
  - 证据:
    ```python
    resolution = str(raw.get("resolution", ""))
    width = raw.get("width")
    height = raw.get("height")
    if (width is None or height is None) and "x" in resolution:
        left, right = resolution.lower().split("x", 1)
        width, height = int(left), int(right)
    ```
  - 影响: `resolution="1080x"` → `int("")` 抛裸 ValueError；`float(raw["fps"])`、`int(item["frame"])`、`ReferenceKind(str(...))` 同理。迁移外部数据时错误信息不含字段名与 payload 上下文，难以诊断，属"外部数据须验证"的验证可诊断性缺口（失败是显式的，非静默）。
  - 建议: 逐字段 try/except 收集错误后抛带字段上下文的迁移异常，或跳过坏字段并记录。
  - 置信度: 高

- [AUD-31608] [Suggestion] GenerationBudget 无 __post_init__ 校验，"硬上限"合同接受非法值
  - 位置: src/shared/ai_models/generation/generation_models.py:178-189
  - 证据:
    ```python
    @dataclass(frozen=True)
    class GenerationBudget:
        """生成成本上限（§10.2："生成视频/音乐另有硬上限"）。
        ...
        max_cost_units: float | None = None    # 硬成本上限（货币/token，provider 解释）
        max_duration_seconds: float | None = None  # 单次生成最大时长（视频/音乐）
        max_candidates: int = 4                 # 一次请求最多回收几个候选
    ```
  - 影响: 负 max_cost_units、0/负 max_candidates、负时长均可构造成功；本模块其他 dataclass（Recipe/ReferenceBinding/Request）都有 __post_init__ 校验，唯独预算合同不设防，把校验责任全推给下游。
  - 建议: 增加 __post_init__：各上限为 None 或 >0，max_candidates >= 1。
  - 置信度: 高

- [AUD-31609] [Suggestion] 类型与结构小问题：reference_bindings 弱类型为 tuple[dict,...]；with_state/with_preview_handle 重复局部 import
  - 位置: src/shared/ai_models/generation/generation_models.py:368、540-547
  - 证据:
    ```python
    reference_bindings: tuple[dict[str, Any], ...] = ()
    ...
    def with_state(self, state: GeneratedAssetState) -> "GeneratedAsset":
        from dataclasses import replace
        return replace(self, state=state)

    def with_preview_handle(self, preview_handle: str) -> "GeneratedAsset":
        from dataclasses import replace
        return replace(self, preview_handle=preview_handle)
    ```
  - 影响: provenance.reference_bindings 用裸 dict 元组而非 ReferenceBinding（或其 to_dict DTO），与 recipe.references 模型不一致、弱类型；两个方法各自局部 import dataclasses.replace（顶部已 import dataclasses 其他成员），纯冗余。
  - 建议: 顶部统一 `from dataclasses import replace`；reference_bindings 收敛为 tuple[ReferenceBinding, ...] 或专用 frozen DTO。
  - 置信度: 高

### 通过项
- 线程安全：全部 frozen dataclass 不可变，可安全跨线程共享（output_constraints 为共享引用的浅不可变是 Python 通用限制，to_dict 均做了拷贝）。
- 状态一致性：不可变更新（with_state 返回新实例）模式正确；gating 派生值（is_commercial_ready/importable/succeeded）在序列化时与字段一致输出。
- 输入验证：Recipe 尺寸/fps/时长正数校验、schema_version 白名单、request_id/prompt/asset_id 非空、weight 0..1、recipe.modality 与 request modality 匹配。
- 隐私红线：to_dict 默认以 prompt_hash 替代原文（retain_prompts 开关）、Provenance 不记 secret/绝对路径——符合 §0 第四条。
- LicenseStatus.commercial_ready 与 ContentSafetyResult/QualityResult 的 blocked gating 逻辑正确（UNKNOWN/PENDING 不可商用就绪）。
- GLSL·HDR / GPU 资源生命周期 / 信号防递归：不涉及。

---

## src/shared/ai_models/generation/image_gen_job.py (行数: 251)

### 发现

- [AUD-31610] [Major] 状态机在 transport 调用前一次性连跳 UPLOADING→RUNNING→DOWNLOADING，可观察状态与真实阶段不符
  - 位置: src/shared/ai_models/generation/image_gen_job.py:210-214
  - 证据:
    ```python
    try:
        self.state.transition(UPLOADING)
        self.state.transition(RUNNING)
        self.state.transition(DOWNLOADING)
        rgba = self.transport(request, api_key, self.output_size)
    ```
  - 影响: 三个阶段状态在网络调用开始前全部置位；transport 执行期间（云端生成可达数十秒）job 状态恒为 DOWNLOADING。上传/运行期出错时 history 已记录到 DOWNLOADING 才转 FAILED，基于状态的 UI 进度指示与真实进度完全脱节，模块宣称的"explicit state machine surfaced to the node/UI"失去观测意义。
  - 建议: 给 transport 增加阶段回调（on_upload/on_run/on_download）驱动真实迁移；至少把 DOWNLOADING 移到 transport 返回后再置位。
  - 置信度: 高

- [AUD-31611] [Major] 仅为计算 cache key 对整幅 4K float32 图像做两次全量拷贝（astype + tobytes，瞬时 ~2×133MB）
  - 位置: src/shared/ai_models/generation/image_gen_job.py:135-138
  - 证据:
    ```python
    image_bytes = np.ascontiguousarray(
        image.astype(np.float32)).tobytes()
    mask_bytes = (np.ascontiguousarray(mask.astype(np.float32)).tobytes()
                  if mask is not None else b"")
    ```
  - 影响: 4K RGBA float32 ≈133MB；`astype(np.float32)` 默认复制一次，`tobytes()` 再复制一次，构造每个 job 都产生 ~266MB 瞬时分配与两次全量拷贝（mask 同理），仅用于喂 sha256。outpaint 节点随参数变更重建 job 时反复付出，违背项目 4K 实时性能基准。
  - 建议: 分块哈希（按行/块切片直接 `digest.update(np.ascontiguousarray(chunk))` 或 memoryview），避免整帧 tobytes；输入已 float32 时 `astype(np.float32, copy=False)`。
  - 置信度: 高

- [AUD-31612] [Suggestion] border_expand_mask / expand_request_params 不校验扩展比例 ∈ [0,1]，越界值产生语义错误的 mask
  - 位置: src/shared/ai_models/generation/image_gen_job.py:61-73（expand_request_params 227-246 未校验）
  - 证据:
    ```python
    mask = np.zeros((int(height), int(width)), dtype=np.float32)
    ml = int(round(float(left) * width))
    ...
    mr = int(round(float(right) * width))
    ...
    if mr > 0:
        mask[:, width - mr:] = 1.0
    ```
  - 影响: docstring 声明比例为 0..1 但无验证：负值被 `if > 0` 守卫静默忽略；`right/bottom > 1` 时 `width - mr` 为负，切片语义改变（mr>width 时等价整幅置 1），生成含义错误的 outpaint mask 仍被送往 provider。
  - 建议: 入口校验 `0 <= ratio <= 1`，越界抛 ValueError。
  - 置信度: 高

- [AUD-31613] [Suggestion] store_cache 元数据明文保存 prompt 前 120 字符，与"默认不保留 prompt 原文"的数据最小化策略不一致
  - 位置: src/shared/ai_models/generation/image_gen_job.py:162-165
  - 证据:
    ```python
    self._cache.store(self._cache_entry(), rgba,
                      {"node_type": "ai_gen_expand",
                       "prompt": self.prompt[:120],
                       "seed": self.seed})
    ```
  - 影响: 本地缓存元数据携带明文 prompt 片段；generation_models 侧默认只落 hash（§0 第四条），缓存目录若被导出/同步/展示会带出提示词内容，两处策略不一致。
  - 建议: 元数据只存 prompt hash（cache_key 已含）或由显式开关控制。
  - 置信度: 中

### 通过项
- GLSL·HDR 正确性：float32 RGBA 全流程无 clamp，HDR 数据保真；mask float32、1.0=生成区语义明确。
- 异常与取消路径：transport 异常转 FAILED 且 detail 携带原因（不静默）；无 key 显式 NEEDS_CONFIG；cost_confirm 拒绝显式 FAILED；缓存读写失败仅告警不影响 job（合理降级）。
- 缓存键设计：长度前缀防碰撞，seed/strength/model/size/mask 全参与摘要，输入任何变化即失效。
- 成本闸门：cache 命中不触发付费调用；网络调用前先过 cost_confirm。
- 信号防递归 / GPU 资源生命周期：不涉及。

---

## src/shared/ai_models/generation/model_pack_manager.py (行数: 872)

### 发现

- [AUD-31614] [Major] get_status/require_usable 每次调用都对 GB 级模型目录全量重算 sha256，无校验结果缓存
  - 位置: src/shared/ai_models/generation/model_pack_manager.py:556-559（_verify_dir 全量遍历 762-775；require_usable 579-589）
  - 证据:
    ```python
    demo = not spec.has_release_integrity and not self._strict_verification
    verified = record.sha256 == spec.sha256 and self._verify_dir(
        record.install_path, spec.sha256, allow_demo=demo,
    )
    ```
    ```python
    h = hashlib.sha256()
    for root, _dirs, files in os.walk(install_path):
        for name in sorted(files):
    ```
  - 影响: 已核实各 provider 在推理前调用 require_usable/get_status（src/features/edit/ai_editing/transcript_provider.py:996、tts_provider.py:85/347/375、ocr_provider.py:370、visual_semantic_provider.py:368/378），UI 侧 capability_status.py:150、runtime_capability_snapshot.py:376 亦调 get_status。pack 体积 0.36–3.4GB（见 BUILTIN 规格），每次转写/TTS/OCR 请求前都要对整个权重目录重读重哈希（SSD 上秒级到几十秒），同一目录反复校验无任何指纹缓存。
  - 建议: _InstalledRecord 增加验证指纹（verified_at + 目录 mtime/size 摘要），未变化则跳过重哈希；或全量校验仅发生在 mark_installed 与显式 verify，get_status 信任 manifest 记录。
  - 置信度: 高

- [AUD-31615] [Major] _verify_dir 目录摘要未对子目录遍历顺序排序，同一目录树可算出不同 digest → 已装 pack 被误判校验失败
  - 位置: src/shared/ai_models/generation/model_pack_manager.py:762-767
  - 证据:
    ```python
    h = hashlib.sha256()
    for root, _dirs, files in os.walk(install_path):
        for name in sorted(files):
            fp = os.path.join(root, name)
            try:
                rel = os.path.relpath(fp, install_path).replace(os.sep, "/")
                h.update(rel.encode("utf-8"))
    ```
  - 影响: 只对每层文件名 `sorted(files)`，子目录访问顺序取决于文件系统枚举顺序（os.walk 未排序 dirs）。发布清单 digest 与本地重算只要子目录枚举顺序不同（跨机器、拷贝/重解压后）即不匹配 → 已正确安装的 pack 被判 `verified=False`（"模型包完整性校验失败"），触发不必要的重新下载与 mark_installed。
  - 建议: `for root, dirs, files in os.walk(...): dirs.sort()`，使摘要与枚举顺序无关；发布清单生成端保持同一算法。
  - 置信度: 中

- [AUD-31616] [Major] verify() 校验成功不回写 record.sha256/verification_mode，成功结果无法在 get_status 生效
  - 位置: src/shared/ai_models/generation/model_pack_manager.py:726-740
  - 证据:
    ```python
    ok = self._verify_dir(record.install_path, spec.sha256, allow_demo=demo)
    if not ok:
        # 标记损坏：保留记录但 sha 记为不匹配（get_status.verified=False）
        record.sha256 = ""
        record.verification_mode = "unverified"
        record.last_error = "sha256 verification failed"
        self._save_manifest()
    return ok
    ```
  - 影响: docstring 称 verify "更新 cache 真相"，但成功分支不更新 record——当 record.sha256 与当前 spec.sha256 不一致（发布清单更新了摘要、或文件被外部替换为匹配新 spec 的内容）时，verify() 返回 True 而 get_status 仍算出 `record.sha256 == spec.sha256` 为 False → verified=False、require_usable 继续抛 ModelPackNotInstalledError，显式校验通过的结论无法持久化。
  - 建议: ok 时同步 `record.sha256 = spec.sha256; record.verification_mode = "strict"; record.last_error = ""` 并 `_save_manifest()`。
  - 置信度: 高

- [AUD-31617] [Suggestion] _save_manifest 非原子写（truncate 式 open("w")），崩溃可致 manifest 损坏 → 全部安装记录静默清零
  - 位置: src/shared/ai_models/generation/model_pack_manager.py:818-819（_load_manifest 静默吞错 799-800）
  - 证据:
    ```python
    with open(self._manifest_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    ```
  - 影响: 对照同包 user_ai_config.save_user_ai_config 的 mkstemp+fsync+os.replace 原子模式，本写入在崩溃/断电时可留半截 JSON；_load_manifest 捕获 JSONDecodeError 后仅 debug 日志并视为空 → 所有 pack 的安装记录与验证状态丢失，表现为"全部模型未安装"，需逐个重新 mark_installed（模型为 GB 级，代价高）。触发概率低但与"不静默降级"红线相悖。
  - 建议: 复用 tmp+os.replace 原子写；损坏时至少 warning 级日志并保留 .bak。
  - 置信度: 高（代码事实；触发依赖崩溃时机）

- [AUD-31618] [Suggestion] default_cache_dir docstring 仍描述旧路径（edit 侧），parents[4] 恰好同深度才未出错，再移动一层即静默写错位置
  - 位置: src/shared/ai_models/generation/model_pack_manager.py:52-58
  - 证据:
    ```python
    由本文件位置向上推导项目根：本文件在
    ``<project>/src/features/edit/ai_editing/model_pack_manager.py``，
    ``parents[4]`` 即 ``<project>``。
    """
    _here = Path(__file__).resolve()
    _project_root = _here.parents[4]
    ```
  - 影响: 文件 C1 迁移后实际位于 `src/shared/ai_models/generation/`，注释与事实不符；两路径同为 4 层深度纯属巧合使 parents[4] 仍指向项目根。后续任何一层目录调整都会把 GB 级模型 cache 静默写到错误根（如 src/ 内或 home），且无任何告警。
  - 建议: 修正注释；改为向上查找 pyproject.toml/.git 等锚点，或由 host 注入 cache_dir 并断言其存在。
  - 置信度: 高

- [AUD-31619] [Suggestion] _installed 与 manifest 保存无锁：下载 worker 回调与 UI/推理线程并发时可抛 RuntimeError
  - 位置: src/shared/ai_models/generation/model_pack_manager.py:628-635、815
  - 证据:
    ```python
    self._installed[pack_id] = _InstalledRecord(
        pack_id=pack_id,
        install_path=path,
        sha256=spec.sha256,
        ...
    )
    self._save_manifest()
    ```
    ```python
    for r in self._installed.values():
    ```
  - 影响: mark_installed/verify 设计为由下载 worker 线程回调（模块注释明言），all_status/get_status/_save_manifest 由 UI 与 provider 线程调用；_installed 无锁，_save_manifest 迭代 `values()` 期间另一线程插入记录会抛 "dictionary changed size during iteration"，使保存/状态查询失败。
  - 建议: threading.Lock 保护 _installed 读写与 manifest 保存（或改为不可变快照替换）。
  - 置信度: 中

### 通过项
- "不静默降级"红线：未装/校验失败 → ModelPackNotInstalledError 显式抛出；demo 验证模式仅限显式 strict_verification=False host，release-manifest-required 哨兵值在严格模式下永不让 directory-exists 通过（_verify_dir 757-760）。
- remove() 安全守卫：os.path.commonpath 限制只删 cache_dir 内目录，越界路径跳过文件删除并告警。
- 输入验证（release manifest 侧）：摘要必须匹配 _SHA256_RE，未知 pack_id / 非法条目拒收并以 rejected 列表回报；根类型校验。
- 无 Qt 依赖、无网络（符合模块自述设计约束）；apply_release_manifest 用 replace 生成新 frozen spec 而非原地突变。
- GLSL·HDR / GPU 资源生命周期 / 信号防递归 / undo·redo：不涉及。

---

## src/shared/ai_models/generation/provider_profiles.py (行数: 351)

### 发现

- [AUD-31620] [Major] shared 层运行时反向 import features.edit.ai_editing（infer_capabilities），违反模块边界且 ImportError 未被加载降级捕获
  - 位置: src/shared/ai_models/generation/provider_profiles.py:122（另 223、235）
  - 证据:
    ```python
    from src.features.edit.ai_editing.ai_provider_catalog import infer_capabilities
    capabilities = infer_capabilities(provider_kind, modalities)
    ```
  - 影响: 本包 __init__ 明言 shared 生成层的存在意义就是"任何模块可不经 edit 使用 generation"（docs/module_boundary_rules.md），而 provider_profiles 在 from_dict/set_active/set_active_for_capability 三处运行时反向硬依赖 features 层（已核实该模块现位于 src/features/edit/ai_editing/ai_provider_catalog.py:214）。一旦打包裁剪或重构掉 edit 侧目录，旧格式 profile 反序列化抛 ImportError；load_from_config/load_from_user_file 的降级只捕 `(KeyError, TypeError, ValueError)`（291/331 行），ImportError 直接中断整个 profile 加载。
  - 建议: 在 shared 层实现纯函数 capability 推断（或经注册表由 features 侧注入），依赖方向不得 shared→features；若保留 import，至少纳入加载异常捕获并显式告警。
  - 置信度: 高

- [AUD-31621] [Major] 单条损坏 profile 的 data_policy/limits 非对象时抛 AttributeError，逃出"单条损坏跳过"策略使全部 profile 加载失败
  - 位置: src/shared/ai_models/generation/provider_profiles.py:111-112、135-141（except 见 289-293、329-332）
  - 证据:
    ```python
    dp = data.get("data_policy", {})
    lm = data.get("limits", {})
    ...
    data_policy=DataPolicy(
        execution=str(dp.get("execution", "local")),
    ```
    ```python
    try:
        profile = AiProviderProfile.from_dict(item)
    except (KeyError, TypeError, ValueError):
        # 损坏的单条 profile 跳过，不整体失败（降级，TAEP §9 可诊断可降级）
    ```
  - 影响: 用户手编 JSON 中 `data_policy: "local"` 之类非映射值使 `dp.get` 抛 AttributeError，不在捕获元组内 → load_from_config/load_from_user_file 整体中断，完好 profile 也一并加载失败（启动路径崩溃面），违背自身声明的单条降级策略。
  - 建议: `dp = data.get("data_policy", {}); dp = dp if isinstance(dp, dict) else {}`（limits 同理），或 except 元组补 AttributeError。
  - 置信度: 高

- [AUD-31622] [Suggestion] from_dict 宽松转换：modalities 字符串被拆成单字符元组；数值字段未强转与注解不符
  - 位置: src/shared/ai_models/generation/provider_profiles.py:113-114、143-147
  - 证据:
    ```python
    modalities = tuple(data.get("modalities", ("text", "tool_call")))
    ```
    ```python
    monthly_cost_alert=lm.get("monthly_cost_alert"),
    ...
    max_cost_units_per_request=lm.get("max_cost_units_per_request"),
    ```
  - 影响: `modalities: "text"`（手编 JSON 常见）被 tuple() 拆为 ('t','e','x','t') 静默入库，allows_media_upload/set_active_for_capability 判断随之失真；monthly_cost_alert/max_cost_units_per_request 不像其他字段做 int()/float() 强转，JSON 字符串 "50" 原样存入 `float | None` 注解字段，后续数值比较异常。
  - 建议: modalities 先 isinstance((list, tuple)) 校验（capabilities 字段已这么做）；两个数值字段统一 `float(...) if ... is not None else None`。
  - 置信度: 高

### 通过项
- secret 隔离红线：profile 仅存 credential_ref，to_dict 不含 secret；save_to_config 明示只写非 secret 配置。
- 输入验证（部分到位）：set_active 未注册 id 抛 KeyError；set_active_for_capability 校验 profile 确实提供该 capability；load_from_config 对顶层非 list、条目非 dict、损坏单条（KeyError/TypeError/ValueError）有过滤降级；active_by_capability 逐项 isinstance 校验且仅接受已注册 profile。
- 状态一致性：load_from_config/load_from_user_file 均清空再加载避免残留；remove 同步清理 active 与 per-capability 映射。
- 不可变模型：AiProviderProfile/DataPolicy/ProviderLimits frozen + __post_init__ 非空校验。
- GLSL·HDR / GPU 资源生命周期 / 信号防递归：不涉及。

---

## src/shared/ai_models/generation/user_ai_config.py (行数: 68)

### 发现

- [AUD-31623] [Major] load_user_ai_config 把暂时性读取失败（OSError）与文件不存在同等返回 {}，配合调用方 read-modify-write 整体覆盖 → profiles 与 API key 全部丢失
  - 位置: src/shared/ai_models/generation/user_ai_config.py:24-29（后果链见 credential_store.py:236-254、281-288 与 provider_profiles.py:308-315）
  - 证据:
    ```python
    try:
        with target.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (FileNotFoundError, OSError, json.JSONDecodeError, UnicodeError):
        return {}
    ```
  - 影响: 权限错误、文件被占用、恢复备份后属主异常等暂时性 OSError 与"文件不存在"同样得到空 dict；FileCredentialStore.set_secret/delete_secret、set_credential_storage_preference、ProfileRegistry.save_to_user_file 全部是 load→改→整文件覆盖，随后的任意一次保存会把仅含增量内容的 config 落盘——原有全部 profiles 与 credentials（明文 API key）永久丢失，且无备份可恢复。JSON 损坏场景同样直接被覆盖（非暂时性，同样无 .bak）。
  - 建议: 区分 FileNotFoundError（返回 {}）与其他 OSError/解析错误（抛出或返回带错误标记的结果，调用方拒绝覆盖）；覆盖前保留 .bak 一代。
  - 置信度: 高

### 通过项
- 原子写实现正确：mkstemp 临时文件 + flush/fsync + os.replace + finally 清理临时文件，无残留半截文件。
- 权限收紧：目录 0700、文件 0600（chmod 失败容忍），且 `parent != Path(".")` 守卫避免误改相对路径调用方的 cwd 权限。
- 类型注解齐全；文件为用户级配置（不入工程文件），与"可重建缓存不入工程"边界一致。
- GLSL·HDR / GPU 资源生命周期 / 信号防递归 / undo·redo：不涉及。
