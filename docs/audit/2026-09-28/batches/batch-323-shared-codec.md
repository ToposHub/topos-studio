# 批次报告：batch-323 [P2] src/shared/codec//

- 编号区间：AUD-32300–AUD-32399（实际使用 AUD-32300–AUD-32311）
- 文件数：4（2,093 行）
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失
- 总计数：Critical 0 · Major 1 · Suggestion 11

---

## src/shared/codec/topos_image_binding.py (行数: 1045)

### 发现

- [AUD-32300] [Suggestion] decode 最终错误路径未拼入 Python 侧 I/O 回调详情（`_with_io_detail` 覆盖不一致）
  - 位置: src/shared/codec/topos_image_binding.py:609
  - 证据:
    ```python
        finally:
            io.close()
        if rc not in (TC_OK, TC_WARN_CONCEALED):
            raise ToposImageError(rc, self._detail(rc))
    ```
    对比 probe/validate/read_metadata 均为 `self._with_io_detail(rc, self._detail(rc), io)`（471–473、486–488、900–902 行），decode 内 probe 失败分支（540–542 行）也已使用。
  - 影响: C08 设计的 `_CFileIo.last_error`（short read、句柄错误等回调边界诊断）在 decode 失败时丢失，用户只看到 native 状态码（`_detail` 里 `tc_last_error` 为空），排障信息不完整。
  - 建议: 该分支改为 `self._with_io_detail(rc, self._detail(rc), io)`（`io.close()` 后仅读属性，安全）。
  - 置信度: 高

- [AUD-32301] [Suggestion] write_packet 使用固定 `.toos-tmp` 临时名——并发写同一目标路径时互相破坏
  - 位置: src/shared/codec/topos_image_binding.py:770-773
  - 证据:
    ```python
        target = str(path)
        tmp = target + ".toos-tmp" if atomic else target
        parent = os.path.dirname(os.path.abspath(target))
        os.makedirs(parent, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    ```
  - 影响: codec 为进程级共享实例；若两个导出任务（图像序列导出可多次发起）写同一输出路径，双方 `O_TRUNC` 同一 tmp 文件并交错写入，`os.replace` 提交的是两写者混合的损坏文件。
  - 建议: tmp 名掺入 `os.getpid()`/uuid 或 `O_EXCL` 重试，保证写者独占。
  - 置信度: 中

- [AUD-32302] [Suggestion] `durable=True` 未 fsync 父目录，原子替换的崩溃持久性不完整
  - 位置: src/shared/codec/topos_image_binding.py:871-872, 890-891
  - 证据:
    ```python
            rc = int(self._lib.tc_image_write(
                ctypes.byref(params), ctypes.byref(io), ctypes.byref(out_size)))
            if durable and rc == TC_OK:
                os.fsync(fd)
    ```
    ```python
        if atomic:
            os.replace(tmp, target)
    ```
  - 影响: 只 fsync 文件数据不 fsync 目录项——崩溃后 rename 可能未持久化（丢失新文件或回退旧文件），`durable` 语义与 POSIX 持久写入惯例不符。
  - 建议: `os.replace` 后对父目录 `os.open(dir, O_RDONLY)` + `os.fsync` 再关闭。
  - 置信度: 中

- [AUD-32303] [Suggestion] `os.open` 与首个 try 之间存在无保护间隙，异常时泄漏 fd 与临时文件
  - 位置: src/shared/codec/topos_image_binding.py:773-821
  - 证据:
    ```python
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        write_failed = False
        ...（回调定义、_CIo 构造）
        packet_buf = ctypes.create_string_buffer(packet)
        idsc = _CImageIdsc()
        try:
            rc = int(self._lib.tc_image_derive_idsc(
    ```
  - 影响: 773 行打开 fd 后到 821 行 try 之间约 45 行纯 Python 构造；`create_string_buffer(packet)` 对超大包可抛 MemoryError（或 `_CIo` 字段变更引发 TypeError），此时 fd 泄漏、tmp 残留在磁盘上。
  - 建议: `os.open` 成功后立即进入 try/finally（统一 `finally: os.close(fd)` + 失败 unlink）。
  - 置信度: 中

- [AUD-32304] [Suggestion] HALF LUT 惰性初始化无锁（良性竞争、重复建表）
  - 位置: src/shared/codec/topos_image_binding.py:105-125
  - 证据:
    ```python
    def _half_to_code_lut() -> np.ndarray:
        global _HALF_TO_CODE_LUT
        if _HALF_TO_CODE_LUT is None:
            bits = np.arange(65536, dtype=np.uint32)
            m = bits & _HALF_MAG
            neg = (bits & np.uint32(0x8000)) != 0
            _HALF_TO_CODE_LUT = np.where(
                neg, _HALF_MID - m, _HALF_MID + m).astype(np.uint16)
        return _HALF_TO_CODE_LUT
    ```
  - 影响: 共享 codec 支持多线程解码（CDLL 调用释放 GIL），两线程首调并发建表。构造为纯函数且赋值原子，内容确定一致——最坏情况重复建两张 128KB 表，无正确性风险，仅与文件内其余共享状态的加锁风格（`get_image_codec` 双检锁）不一致。
  - 建议: 模块导入期预建，或复用 `_CODEC_LOCK` 同款双检锁。
  - 置信度: 高

- [AUD-32305] [Suggestion] `tc_image_derive_idsc`/`tc_image_write` 未做 hasattr 特性检测，旧库加载即抛 AttributeError
  - 位置: src/shared/codec/topos_image_binding.py:406-407, 423-430, 432
  - 证据:
    ```python
        self._has_native_preview = hasattr(lib, "tc_image_decode_preview")
        ...
        lib.tc_image_derive_idsc.restype = ctypes.c_int32
        lib.tc_image_derive_idsc.argtypes = [
            ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint8,
            ctypes.POINTER(_CImageIdsc)]
        ...
        lib.tc_image_write.restype = ctypes.c_int32
    ```
  - 影响: 本模块对 `tc_image_decode_preview`、`tc_image_half_to_codes` 均有 `hasattr` 特性检测并保留 fallback（"旧库无此符号 → numpy 实现仍可用"，431 行注释），但 `tc_image_derive_idsc`/`tc_image_write` 无保护——缺符号的旧库在 `ToposImageCodec.__init__` 即 AttributeError，经 `get_image_codec` 失败缓存后整个进程（含只做解码的调用方）都不可用，破坏部署兼容策略。
  - 建议: 同样 `hasattr` 检测，`write_packet` 在无符号时报"native 库过旧"的显式错误。
  - 置信度: 中

- [AUD-32306] [Suggestion] 类型注解缺失/不精确（`byref` 无返回注解、`library_identity` 裸 `tuple`、`ToposImageInfo.raw` 裸 `dict`）
  - 位置: src/shared/codec/topos_image_binding.py:987, 955, 367
  - 证据:
    ```python
        def byref(self):
            return ctypes.byref(self._io)
    ```
    ```python
        def library_identity(self) -> tuple:
    ```
    ```python
        raw: dict = field(default_factory=dict)
    ```
  - 影响: 类型注解维度缺失；`tuple`/`dict` 裸泛型使静态检查与调用方推断退化（`raw` 字段还从未被 `_to_info` 填充，属预留 host 字段）。
  - 建议: `byref` 补 `-> Any`；`library_identity` 用 `Tuple[str, ...]`；`raw` 用 `Dict[str, Any]` 并注明预留语义。
  - 置信度: 高

### 通过项
- 线程安全：`get_image_codec` 双检锁实现正确（锁内二次判空、失败原因缓存语义有文档）；解码路径的 ctypes 结构/numpy 缓冲均为调用局部持有，无共享可变状态（LUT 除外，见 AUD-32304）
- GPU 资源生命周期：本文件为纯 ctypes/numpy 编解码绑定，不涉及 OpenGL/GPU 资源，无泄漏面
- GLSL·HDR 正确性：HALF↔code LUT 与 spec §15 冻结公式逐位核对一致（前向 `0x8000±mag`、码 0 前向不可达、逆映射码 0 饱和 0xFFFF=-NaN、±0 归一 0x8000）；float16 路径无 clamp(0,1)；`decode_float` 拒绝 UINT/HALF 样本域混用（显式错误非隐式转换）
- 信号防递归：无 Qt 控件/信号，不适用
- 状态提交一致性：读路径无副作用；写路径单一 `write_packet`，atomic 模式 `os.replace` 原子提交；无 undo/revision 语义要求
- 异常与取消路径：probe/validate/decode/read_metadata 的 try/finally 均保证 `io.close()`；`write_packet` 各错误分支关 fd、unlink tmp；`_CFileIo._read_impl` 捕获 OSError/ValueError 返回 TC_ERR_IO（C08 注释明确 ctypes 回调异常逃逸的后果）；write 回调 OSError 捕获后置 write_failed 并二次校验（native 返回 OK 但回调失败也能拦截）
- 资源保活：`keep_write`/`keep_seek_write` CFUNCTYPE 保活、`chunk_bufs` extra chunk 缓冲保活（852 行注释点明 c_void_p 不持有的陷阱）均正确；numpy 平面在 C 调用期间由 `arrays` 列表持有
- 输入验证：`_normalise_target_size` 对类型/bool/非整数/非正值完备校验；空 packet 显式拒绝；`write_packet` 的 ICCP 权威位、plane ABI 版本（codec 域=2 vs image 域=1）区分正确
- 性能模式：G9 LUT gather 替代逐元素 where 链（避免每平面 2–3 份全帧 uint32 临时）；零拷贝解码直写 caller-provided numpy 平面；预览缩放单次高级索引 gather；`_preview_dimensions` 不放大小图

---

## src/shared/codec/topos_meta.py (行数: 127)

### 发现

- [AUD-32307] [Major] decode_topos_meta 的 UTF-8 解码抛 UnicodeDecodeError 而非 ToposMetaError，击穿"任何失败一律降级"契约
  - 位置: src/shared/codec/topos_meta.py:123-127（调用方 src/shared/codec/topos_image_binding.py:930-934）
  - 证据:
    ```python
        return {
            "tier_id": int(tier_id),
            "vendor": payload[vendor_start:label_start].decode("utf-8"),
            "label": payload[label_start:label_start + label_len].decode("utf-8"),
        }
    ```
    调用方 `read_export_meta` 只捕 ToposMetaError：
    ```python
        try:
            return decode_topos_meta(payload)
        except ToposMetaError as exc:
            logger.debug("TMET 解码降级（%s）: %s", path, exc)
            return None
    ```
  - 影响: 模块 docstring 与 `read_export_meta` docstring 均承诺"任何违反抛 ToposMetaError……任何失败一律降级，不阻塞媒体打开"。CRC 一致但 vendor/label 含非法 UTF-8 字节的载荷（写端缺陷或手工构造文件）会以 UnicodeDecodeError 逃逸降级层，沿 `topos_image_source.py:159 read_export_meta`（媒体打开路径，注释明言"缺失/损坏静默降级"）向上传播，可打断本应成功的媒体打开。
  - 建议: 两处 `decode("utf-8")` 包 try/except UnicodeDecodeError 转 `ToposMetaError`（与 C 侧 MALFORMED 语义对齐）。
  - 置信度: 高

### 通过项
- 输入验证：长度上下界（16..94B）、magic、version、reserved=0、tier 上限（0..8）、串长一致性、CRC 逐项校验，完整镜像 C demux 规则；`encode_topos_meta` tier 越界/串超长 fail-fast 不静默截断
- 线程安全：纯函数、无共享可变状态
- 异常路径：越界/损坏载荷全部走 ToposMetaError（唯一例外见 AUD-32307）
- 性能模式：`struct.Struct` 预编译、12B 定长头一次解包
- 类型注解完整

---

## src/shared/codec/topos_profiles.py (行数: 805)

### 发现

- [AUD-32308] [Suggestion] `available_topos_tiers()` 不过滤 `available=False` 档位；AVAILABLE_TOPOS_TIERS 注释与 raw 档 `available=True` 相互矛盾
  - 位置: src/shared/codec/topos_profiles.py:330-337, 396-398, 318-322
  - 证据:
    ```python
    AVAILABLE_TOPOS_TIERS: Tuple[str, ...] = (
        "netproxy", "proxy", "lt", "standard", "hq", "4444", "4444xq", "lp",
        "raw",
    )
    ```
    上方注释称 "raw 为视频 RAW 声明档（available=False，R7 编码链落地后激活）"，而 raw 档数据为：
    ```python
            available=True, unavailable_reason="",
    ```
    且查询函数不看标志：
    ```python
    def available_topos_tiers() -> List[ToposProfileTier]:
        """当前可用的档位（质量升序）。"""
        return [TOPOS_PROFILE_TIERS[t] for t in AVAILABLE_TOPOS_TIERS]
    ```
  - 影响: 模块核心契约是"档位是显式可查询的能力声明，不允许隐式参数猜测"。`available_topos_tiers`（消费方：runtime_capability_snapshot 能力快照、topos_encoder 错误提示）不读 `t.available`——未来任何档位置 available=False 仍会出现在"可用"输出中；注释与数据矛盾也会误导维护者对 raw 档激活状态的判断。
  - 建议: 函数内过滤 `[... if t.available]`，并修正过期注释（R7 已落地则注释删掉 available=False 说法）。
  - 置信度: 高（不一致确凿；当前全部 available=True，无即时运行时影响）

- [AUD-32309] [Suggestion] `default_topos_image_tier_id` 家族内多 default 歧义——raw16-4 的 `default=True` 永不可达
  - 位置: src/shared/codec/topos_profiles.py:795-800, 713-715, 739-741
  - 证据:
    ```python
    def default_topos_image_tier_id(family: str = "int422") -> str:
        """家族默认档：整数 422-ultra（CLI 无 --tier 默认）、raw12-4/raw16-4。"""
        for t in TOPOS_IMAGE_TIERS.values():
            if t.family == family and t.default:
                return t.tier_id
        raise KeyError(f"family {family!r} 无默认档")
    ```
    raw12-4 与 raw16-4 的 family 同为 `"raw"` 且均 `default=True`（715、741 行）；调用方 `video_export_settings_panel.py:255` `default_topos_image_tier_id("raw")` 按注册序恒返回 `"raw12-4"`。
  - 影响: raw16-4 的 default 标记是死数据；16-bit RAW 家族默认档无法经该 API 表达（签名只收 family 无位深），"家族默认"语义在 raw 家族上歧义，docstring 宣称的两个 raw 默认档实际只能取出一个。
  - 建议: 查询签名加位深参数（family+bit_depth），或 raw 家族只保留一个 `default=True` 并在 docstring 说明 12-bit 为唯一默认。
  - 置信度: 高

- [AUD-32310] [Suggestion] `_bpp_from_mb_s` 与 `TRAW_RATIO_NOMINAL` 无引用死代码
  - 位置: src/shared/codec/topos_profiles.py:123-126, 582
  - 证据:
    ```python
    def _bpp_from_mb_s(mid_mb_s: float) -> float:
        """1080p25 参考数据率（MB/s）中值 → bits-per-pixel。"""
        bits_per_frame = mid_mb_s * 1024.0 * 1024.0 * 8.0
        return bits_per_frame / (1920.0 * 1080.0 * 25.0)
    ```
    ```python
    TRAW_RATIO_NOMINAL = (2, 4, 6, 8, 12, 16)
    ```
    全库 grep（src/、tests/）确认两者均只有定义、无任何调用点；各 tier 的 `target_bpp` 已直接写定值。
  - 影响: 死代码/死常量留在"单一注册表"模块里，暗示了一条已不存在的换算路径，误导后续维护（改动 reference_mb_per_s 时会以为它仍参与定标）。
  - 建议: 删除；若刻意保留作公共换算工具，补 docstring 注明保留原因与预期消费方。
  - 置信度: 高

### 通过项
- 线程安全：全静态只读注册表（frozen dataclass），反查表导入期构建，无运行时可变状态
- GPU 资源生命周期 / 信号防递归 / 状态提交一致性 / 异常与取消路径：纯声明式数据模块，不适用
- 输入验证：`get_topos_tier`/`get_topos_image_tier` 未知 id 显式 KeyError 列出合法域；宽松查询 `_or_none` 语义明确；`tier_pix_fmt_choices` 对 no-alpha 档返回空 alpha 域（不猜测）
- 数据一致性抽查：`_IMAGE_TIER_PIX_FMT` 键覆盖全部 22 档参数组合（无 KeyError 缺口）；float 档锚点与 docstring qp 对应值逐点一致；TOPOS_IMAGE_TIER_IDS 与 topos_meta.py 同值（重复定义有循环依赖说明 + E4 测试钉住）；TOPOS_TIER_IDS netproxy/proxy 同 id 1 的反查首见优先有注释声明
- 类型注解完整（Tuple/Dict/Optional 均显式）

---

## src/shared/codec/topos_trwm.py (行数: 116)

### 发现

- [AUD-32311] [Suggestion] encode_trwm 不校验 wb 增益正有限与 stops/black 有限——编码域与解码域校验不对称
  - 位置: src/shared/codec/topos_trwm.py:63-78（对照 98-106）
  - 证据: encode 侧直接打包，无值域检查：
    ```python
        body = _HEAD.pack(
            TRWM_MAGIC, TRWM_VERSION_MAJOR, TRWM_VERSION_MINOR,
            float(wb_gain_r), float(wb_gain_g), float(wb_gain_b),
            float(exposure_stops), float(black_level),
            layout, int(sensor_matrix_id), 0, 0,
        )
    ```
    decode 侧明确拒绝（2026-09-21 复查收紧）：
    ```python
        for name, v in (("wb_gain_r", wb_r), ("wb_gain_g", wb_g),
                        ("wb_gain_b", wb_b)):
            if not (v > 0.0) or v != v or v == float("inf"):
                raise ToposTrwmError(f"{name} 须为正有限值: {v!r}")
    ```
  - 影响: 写端参数缺陷（NaN/Inf/非正 wb、NaN stops——NaN 经 f32 打包可完整 round-trip 且 CRC 正确）会产出被 `decode_trwm` 及同规则读端判 MALFORMED 的载荷——直到读端才暴露，浪费一次导出且报错位置远离根因。
  - 建议: encode_trwm 对五个 float 施加与 decode 相同的域校验（wb 正有限；stops/black 有限），fail-fast。
  - 置信度: 高

### 通过项
- 输入验证：载荷定长 40B、magic、major 版本锁定（minor 前向兼容）、reserved/tail 双保留域、CRC、cfa_layout 枚举、wb 正有限、stops/black 有限逐项校验；`_HEAD` 布局 36+4=40 与 TRWM_SIZE 自洽
- 线程安全：纯函数、无共享状态
- HDR 正确性：as-shot 基线语义（RC4——只展示不改变码流像素）与注释一致；decode 拒绝 NaN/Inf 防止顺流污染 debayer 增益/黑位减除
- 类型注解完整

---

## 汇总

| 文件 | Critical | Major | Suggestion |
|---|---|---|---|
| src/shared/codec/topos_image_binding.py | 0 | 0 | 7（AUD-32300–32306） |
| src/shared/codec/topos_meta.py | 0 | 1（AUD-32307） | 0 |
| src/shared/codec/topos_profiles.py | 0 | 0 | 3（AUD-32308–32310） |
| src/shared/codec/topos_trwm.py | 0 | 0 | 1（AUD-32311） |
| 合计 | 0 | 1 | 11 |
