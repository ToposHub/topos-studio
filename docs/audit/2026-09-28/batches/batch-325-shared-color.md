# 批次报告：batch-325 — src/shared/color//

- 任务书：docs/audit/2026-09-28/assignments/batch-325.md（编号区间 AUD-32500–AUD-32599）
- 审查日期：2026-09-28
- 文件数：4（1,915 行），全部完整通读
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解
- 交叉验证：调用方 grep（composite_render_output.py、parallel_frame_render.py、page_controller.py、preferences.py、edit_project_io.py、save_router.py、color_decode.py、color_metadata.py）+ ocio/minimal.ocio space 名核对

---

## src/shared/color/color_space_descriptor.py (行数: 395)

### 发现

- [AUD-32500] [Critical] `to_ocio_name` 对未映射 (primaries, transfer) 组合静默回退 "lin"，默认 OCIO 引擎把已编码信号当线性、整体跳过 ODT 编码
  - 位置: src/shared/color/color_space_descriptor.py:89（叠加 81–86 的 LINEAR 分支）
  - 证据:
    ```python
    if self.transfer == ColorTransfer.LINEAR:
        # ACEScg is scene-linear AP1 — a distinct config space, not the
        # BT.709-gamut working space "lin".
        if self.primaries == ColorPrimaries.AP1:
            return "acescg"
        return OCIO_LINEAR_NAME
    # primaries + transfer combine into a named display/output space.
    prim = self.primaries.value
    return _OCIO_NAME_MAP.get((prim, self.transfer.value), OCIO_LINEAR_NAME)
    ```
  - 影响: 偏好页提供 BT709/P3_DISPLAY/BT2020 × SRGB/BT709/LINEAR/PQ/HLG（available_primaries/available_transfers，L374–395），而 bundled ocio/minimal.ocio 实测仅定义 lin/acescg/srgb/rec709/bt2020_pq/p3_pq/bt2020_hlg/display_p3。(bt2020,bt709)、(bt2020,iec61966_2_1)、(smpte432,smpte2084)、(bt709,smpte2084) 等合法组合映射 miss → 回退 "lin"。此时 OCIOColorProvider.transform（color_transform_provider.py:696–698）中 src_ocio(LINEAR_SCENE→"lin") == dst_ocio → OCIO 调用被整体跳过，**线性数据被 range 压缩后直接当作 BT.709/PQ 编码导出/预览，全程无任何警告**（_get_processor 根本不被调用）。同为 LINEAR 分支时非 AP1 的 BT.2020/P3 primaries 也被折叠进 "lin"，gamut 矩阵丢失。这是用户可选配置下的静默渲染错误；builtin 回退引擎反而能正确处理这些组合。
  - 建议: 未映射组合不得回退 "lin"——应回退到"保留 transfer 的最近空间"（如 srgb/rec709）并 logger.warning；LINEAR+非 BT.709/AP1 primaries 需补 gamut 处理或显式告警；OCIOColorProvider.transform 在 src_ocio/dst_ocio 来自回退值时应记录诊断。
  - 置信度: 高（代码机制与 OCIO config space 名均已实证；触发条件为用户在偏好页选择未映射组合，UI 已提供这些选项）

- [AUD-32501] [Major] `descriptor_from_config` 声称受 `color.smart_default` 控制，但该键从未被读取——死配置 + 文档与行为不符
  - 位置: src/shared/color/color_space_descriptor.py:364（docstring 声明在 332–336）
  - 证据:
    ```python
    # Smart-default fallback: if primaries/transfer missing or smart_default
    # forces it, use the canonical video/image descriptor.
    if not primaries or not transfer:
        return descriptor_for_source_kind(is_video)
    ```
  - 影响: 全库 grep 确认 `color.smart_default` 仅在 preferences.py:421–427/661–662/765–766（checkbox 持久化）与 config_manager.py:198（默认值）出现，无任何决策逻辑读取它。用户勾选/取消"智能默认"毫无效果；docstring（及 page_controller._apply_composite_preview_color_space 的 docstring）误导维护者以为 flag 生效。该键还被列入 COLOR_MANAGEMENT_SETTING_KEYS 随工程持久化，持续污染工程文件。
  - 建议: 实现 flag 语义（如 on 时优先智能默认、off 且键缺失时回退 LINEAR 并提示），或删除该键并修正两处 docstring。
  - 置信度: 高

- [AUD-32502] [Major] `collect_color_management_settings` 宽泛 `except Exception` 且零日志——工程保存时色彩管理设定可能被静默丢弃
  - 位置: src/shared/color/color_space_descriptor.py:313–325
  - 证据:
    ```python
    try:
        from src.shared.config.config_manager import ConfigManager
        manager = ConfigManager()
    except Exception:
        return values
    for key in COLOR_MANAGEMENT_SETTING_KEYS:
        try:
            value = manager.get(key)
        except Exception:
            continue
    ```
  - 影响: ConfigManager 构造或 get 异常（配置文件损坏、权限等）时静默返回空/部分 dict，edit_project_io.py:231 与 save_router.py:630 据此落盘 settings["color_management"]——工程换机/交接时色彩管理设定无声丢失，无任何日志线索。违反 .clinerules"不得宽泛 except 掩盖领域错误"。
  - 建议: 至少 logger.warning 记录异常与受影响键；缩窄捕获类型（ImportError / 配置读取异常）。
  - 置信度: 高

- [AUD-32503] [Suggestion] 多处类型注解缺失/宽松
  - 位置: src/shared/color/color_space_descriptor.py:232（`_enum_from_value(enum_cls, value, default)` 无注解）、287（`set_project_color_overrides(overrides)` 参数无注解）、194（`Dict[tuple, str]`）、264（`_PROJECT_COLOR_OVERRIDES: dict`）、374/383（`-> list` 无元素类型）
  - 证据:
    ```python
    def _enum_from_value(enum_cls, value, default):
        """Look up an enum member by value, returning ``default`` if not found."""
    ```
  - 影响: shared 层公共 API 类型契约不完整，IDE/静态检查无法校验调用方。
  - 建议: 补 `EnumT`, `ValueT` 泛型注解与 `Dict[str, str]` / `list[ColorPrimaries]` 等精确类型。
  - 置信度: 高

### 通过项
- from_dict/to_dict 对缺失/非法枚举值经 `_enum_from_value` 校验并回退默认（外部数据已验证）
- frozen dataclass 不可变、可哈希，预设常量（LINEAR_SCENE/SRGB_IMAGE/REC709_VIDEO 等）transfer/range 组合符合标准
- from_ocio_name 反向映射经 _OCIO_SPACE_RANGE 恢复 LIMITED，round-trip 保 range
- `_PROJECT_COLOR_OVERRIDES` 按模块注释约定仅 UI 线程读写；实际调用点（composite_controller/page_controller/deliver_controller/edit_project_io）均为 UI/保存路径，未发现渲染线程访问
- 无 GPU 资源、无 Qt 信号（防递归不适用）
- LUT/渲染数据无 clamp 问题（本文件不处理像素）

---

## src/shared/color/color_transform_provider.py (行数: 864)

### 发现

- [AUD-32510] [Major] `BuiltinColorProvider.transform` LIMITED→LIMITED 时不做 range 展开，transfer 解码作用于压缩信号（与 OCIO 实现不对称）
  - 位置: src/shared/color/color_transform_provider.py:505–506、538–539（对照 OCIO 版 692–693、720–721）
  - 证据:
    ```python
    # 1. Range expand: if source is limited-range, expand to full first
    if src.range == ColorRange.LIMITED and dst.range != ColorRange.LIMITED:
        out = _limited_to_full_np(out)
    ...
    # 5. Range compress: if dst is limited-range, compress from full.
    if dst.range == ColorRange.LIMITED and src.range != ColorRange.LIMITED:
        out = _full_to_limited_np(out)
    ```
    而 OCIOColorProvider.transform 为无条件版本：
    ```python
    if src.range == ColorRange.LIMITED:
        out = _limited_to_full_np(out)
    ...
    if dst.range == ColorRange.LIMITED:
        out = _full_to_limited_np(out)
    ```
  - 影响: src、dst 均 LIMITED 且 transfer/primaries 不同时（如 REC709_VIDEO→HDR10_VIDEO），第 2/4 步的 BT.709/PQ 解码把 [16/255, 235/255] 压缩信号当满量程信号，颜色系统性偏移。当前上游调用 src 恒为 LINEAR_SCENE（composite_render_output.py:490/883/1193、timeline_export.py:10838），属潜伏缺陷；但该 transform 是"preferred entry point for color conversion throughout the app"，任何新调用方触发即错色，且两 provider 对同一输入行为不一致本身就是回归风险。
  - 建议: 与 OCIO 版对齐——进入时无条件展开 LIMITED，结束时按 dst 压缩；补一条 LIMITED→LIMITED 的 parity 测试。
  - 置信度: 高（逻辑差异确定）/ 中（当前可达性低）

- [AUD-32511] [Major] 模块级 `_provider_cache`/`_provider_diagnostics` 无同步，导出 worker 线程与 UI 线程并发读写
  - 位置: src/shared/color/color_transform_provider.py:732–736、739–751、769–770、800–802
  - 证据:
    ```python
    _provider_cache: dict = {}
    ...
    def _record_diagnostics(engine: str, provider, reason: str,
                            config_path: str = "") -> None:
        _provider_diagnostics.clear()
        _provider_diagnostics.update({...})
    ...
    def get_color_provider_diagnostics(config_manager=None) -> dict:
        get_color_provider(config_manager)
        return dict(_provider_diagnostics)
    ```
  - 影响: parallel_frame_render.py:223–226 以 threading.Thread 起 N 个导出 worker，每个经 `_convert` → `transform_color` → `get_color_provider`；UI 线程（page_controller.py:3088 状态栏、composite_viewport.py:9199）同时调用 get_color_provider(_diagnostics)。`clear()+update()` 非原子，UI 可能读到空/半更新诊断（降级提示偶发丢失）；并发首调还会重复执行 `Config.CreateFromFile` 构建多个 provider 后互相覆盖。
  - 建议: 用 threading.Lock 保护 cache 与 diagnostics 写入，或应用启动时在单线程预构建并固定。
  - 置信度: 中（竞争窗口确定存在，实际后果限于诊断瞬态错误/重复构建）

- [AUD-32512] [Suggestion] transform 的 transfer 分支用 `== ColorTransfer.PQ` 枚举等值比较，漏掉 ST2084 别名及 GAMMA22 等，未列 transfer 静默按线性处理
  - 位置: src/shared/color/color_transform_provider.py:511–519、527–535
  - 证据:
    ```python
    elif src.transfer == ColorTransfer.PQ:
        out = _pq_to_linear_np(out)
    elif src.transfer == ColorTransfer.HLG:
        out = _hlg_to_linear_np(out)
    # LINEAR / others: leave as-is
    ```
  - 影响: types.py 定义 `ST2084 = 'st2084'`（PQ 别名），color_decode.py:373/419 已被迫同时检查 PQ 与 ST2084；若描述符携带 ST2084/GAMMA22/BT2020_10 等，builtin 静默跳过解码，编码信号被当线性。当前 FFmpeg 映射（16→PQ）下可达性低。
  - 建议: 在 types.py 提供共享判定（`is_pq()`/别名归一化函数）供 provider 与 decode 统一使用；未支持 transfer 至少 logger.warning。
  - 置信度: 中（机制确定，现网触发路径未实证）

- [AUD-32513] [Suggestion] 每个转换 helper 全图 `.copy()`，单次 transform 链式最多 7 次全图拷贝
  - 位置: src/shared/color/color_transform_provider.py:149–158、167–176、199–208、218–227、248–252、258–261 等，链式调用 505–545
  - 证据:
    ```python
    rgb = rgba[..., :3]
    mask = rgb <= 0.04045
    converted = np.where(...)
    out = rgba.copy()
    out[..., :3] = converted
    return out
    ```
  - 影响: 4K RGBA float32 单份约 132 MB；expand→decode→primaries→encode→compress→clip 五步链产生约 7 份全图拷贝加 np.where 临时，导出/批量帧路径内存与带宽开销显著（性能模式维度）。
  - 建议: 链首一次性分配输出缓冲、各步骤就地写 `out[..., :3]`，或合并 range+transfer 为单趟 kernel。
  - 置信度: 高

- [AUD-32514] [Suggestion] `_apply_primaries_np` 每次调用对常量矩阵做运行时 `np.linalg.inv`
  - 位置: src/shared/color/color_transform_provider.py:445–467
  - 证据:
    ```python
    def to709(prim):
        if prim == "bt709":
            return np.eye(3, dtype=np.float32)
        if prim == "bt2020":
            return np.linalg.inv(_BT709_TO_BT2020)
        if prim in ("smpte431", "smpte432"):  # DCI-P3 / Display P3
            return np.linalg.inv(_BT709_TO_P3D65)
    ```
  - 影响: 矩阵是编译期常量，逆矩阵每次 transform（每帧）重算，且 inv 走 float64 中间路径。
  - 建议: 模块级预计算 `_BT2020_TO_BT709`、`_P3D65_TO_BT709` 常量（与 `_AP1_TO_BT709` 同风格）。
  - 置信度: 高

- [AUD-32515] [Suggestion] 未使用导入与注释-行为不符：transform 内导入的 `ColorSpaceDescriptor`/`LINEAR_SCENE` 从未使用；显式 config 路径无效时并不回退 bundled config（与注释矛盾）
  - 位置: src/shared/color/color_transform_provider.py:492–495、688（死导入）；619–632（注释 "explicit > bundled fallback" 但 `if path and os.path.isfile(path)` 为假时直接置 `_config = None`，不尝试 bundled 路径）
  - 证据:
    ```python
    from src.shared.color.color_space_descriptor import (
        ColorSpaceDescriptor, LINEAR_SCENE,
    )
    from src.shared.color.types import ColorTransfer, ColorRange
    ```
    （后文仅使用 ColorTransfer/ColorRange）
  - 影响: 死导入增加启动开销与阅读噪音；用户填错 OCIO 路径时直接整体降级 builtin，而非尝试 bundled config，与注释承诺的回退语义不符。
  - 建议: 删除死导入；显式路径无效时按注释回退 `_BUNDLED_OCIO_CONFIG` 或修正注释。
  - 置信度: 高

### 通过项
- sRGB/BT.709 OETF·EOTF 膝点与常数正确（0.04045/0.0031308、0.018/0.081、1.099/0.099），与 GPU shader 公式一致
- PQ 常数为已发表标准值（非 typo 版），PQ/HLG 输入做 nan_to_num + clip 卫生处理；HLG 正逆曲线分段正确
- HDR 铁律遵守：dst 为 LINEAR 时不 clip（nits 域 >1.0 存活），仅非线编出目标 clip [0,1]
- gamut 矩阵（BT.709↔BT.2020/P3-AP1）为标准已发表值，行向量 @ m.T 数学正确；unknown primaries 明确 no-op
- export_encode 未知组合显式 logger.warning 后透传（不崩导出）
- OCIO 路径 3 通道 pad-alpha/裁剪/reshape 逻辑正确；处理器缓存键空间有界
- 工厂永返回可用 provider 且带结构化降级诊断（P0-COLOR-05），reset_provider_cache 提供配置变更出口
- 无 GPU 资源分配、无 Qt 信号（防递归不适用）

---

## src/shared/color/lut_loader.py (行数: 347)

### 发现

- [AUD-32520] [Suggestion] `parse_cube` 头部关键字缺参/坏参抛裸 IndexError/ValueError；DOMAIN_MIN/MAX 少于 3 值静默通过、采样期才以 broadcast 错误炸出
  - 位置: src/shared/color/lut_loader.py:85–99、101–106
  - 证据:
    ```python
    if upper.startswith('LUT_1D_SIZE'):
        one_d_size = int(line.split()[1])
        section = '1d'
    ...
    if upper.startswith('DOMAIN_MIN'):
        parts = line.split()
        domain_min = tuple(float(v) for v in parts[1:4])
    ```
  - 影响: 外部 LUT 文件畸形时（铁律：外部数据须验证）：`LUT_3D_SIZE` 无参数 → 裸 IndexError；`DOMAIN_MIN 0.1 0.2` → 长度 2 tuple 入库，`_apply_domain` 的 `np.asarray(domain_min)` 在采样期才爆 broadcast 错误，远离解析现场、难以定位。
  - 建议: 解析期校验每个关键字参数个数与取值，raise 携带 source_path 与行号的 ValueError。
  - 置信度: 高

- [AUD-32521] [Suggestion] `parse_3dl` 允许 size=0 头通过校验，随后 `data.max()` 对空数组抛 "zero-size array reduction"
  - 位置: src/shared/color/lut_loader.py:156–160、173–178
  - 证据:
    ```python
    if len(dims) == 1:
        dims = dims * 3
    if len(dims) != 3 or len(set(dims)) != 1:
        raise ValueError(f"Non-cubic .3dl unsupported: {dims}")
    size = dims[0]
    ...
    if len(rows) != size ** 3:
        raise ValueError(...)
    data = np.asarray(rows, dtype=np.float64)
    vmax = float(data.max())
    ```
  - 影响: 头 "0 0 0" 的空 .3dl：rows==0==size³ 通过计数校验，`data.max()` 抛与根因脱节的 ValueError。
  - 建议: 校验 size >= 2（3D LUT 最少两点）。
  - 置信度: 高

### 通过项
- parse_cube 严格校验 3D 行数 == size³、1D shaper 点数匹配，不符即 raise（外部数据验证）
- load_lut 按扩展名 + 内容探测（LUT_3D_SIZE 关键字 / 首行三整数）分派合理
- 三线性采样：坐标 clip 到 [0, n-2]、(R,G,B)→(B,G,R) 轴交换正确，8 角权重和为 1
- tetrahedral 采样：6 种降序排列 + `~assigned` 排他掩码覆盖含相等的全部分布；`fracs[channel][mask][:, None]` 广播正确，与三线性同域处理（DOMAIN 重映射一致）
- `_apply_domain` 对 span<=0 用 1.0 兜底，输入先 clip [0,1] 防 OOB 索引
- probe_lut_metadata 只读头部、OSError 捕获返回部分 dict（画廊 tooltip 场景合理）
- 纯函数、无模块级可变状态（线程安全）；无 GPU 资源（CPU 采样与 GPU 三线性的 parity 由测试锁定，见模块 docstring）

---

## src/shared/color/types.py (行数: 309)

### 发现

- [AUD-32530] [Suggestion] ST2084 作为 PQ 的双重枚举别名构成等值比较陷阱（本批已在 provider 侧发现不一致检查）
  - 位置: src/shared/color/types.py:84、92
  - 证据:
    ```python
    PQ = 'smpte2084'             # HDR10
    ...
    ST2084 = 'st2084'            # Alias for PQ
    ```
  - 影响: 语义相同的传输函数存在两个互不相等的枚举成员，所有 `== ColorTransfer.PQ` 判断都会漏掉 ST2084——color_decode.py:373/419/445 已被迫双检，BuiltinColorProvider.transform 未检（AUD-32512）。is_hdr_transfer 已包含 ST2084，全库判定方式不统一。
  - 建议: 移除 ST2084 成员（改用 PQ），或在 types.py 内提供 `is_pq(transfer)` 归一化判定并全库替换裸比较。
  - 置信度: 高

- [AUD-32531] [Suggestion] 显示名映射缺 ColorPrimaries.AP1 与 ColorTransfer.TRAW_LOG0，均落 'Unknown'
  - 位置: src/shared/color/types.py:252–265（primaries 名表无 AP1）、271–285（transfer 名表无 TRAW_LOG0）
  - 证据:
    ```python
    def get_color_primaries_name(primaries: ColorPrimaries) -> str:
        names: Dict[ColorPrimaries, str] = {
            ColorPrimaries.BT709: 'Rec. 709 / sRGB',
            ...
            ColorPrimaries.UNKNOWN: 'Unknown',
        }
        return names.get(primaries, 'Unknown')
    ```
  - 影响: ACEScg 源（AP1，color_space_descriptor 预设使用）与 TRAW PWL-log12 源（v1.6 codec transfer 20，color_decode.py:93 映射）在 UI 显示 "Unknown"。
  - 建议: 补 `ColorPrimaries.AP1: 'ACEScg (AP1)'` 与 `ColorTransfer.TRAW_LOG0: 'TRAW Log (PWL-log12)'` 条目。
  - 置信度: 高

### 通过项
- FFmpeg H.273 映射表抽查与标准一致：primaries（1,4–12,22）、transfer（1,4–8,13–16,18）、matrix（0–10,14，含 5=bt470bg→BT601_PAL）、range（1/2）
- map_ffmpeg_* 对 None 与未知值统一回退 UNKNOWN（外部数据验证）
- 纯数据/纯函数模块，无可变全局状态，无 GPU/信号/持久化关注点；公共函数类型注解完整

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-32500 |
| Major | 4 | AUD-32501, AUD-32502, AUD-32510, AUD-32511 |
| Suggestion | 9 | AUD-32503, AUD-32512, AUD-32513, AUD-32514, AUD-32515, AUD-32520, AUD-32521, AUD-32530, AUD-32531 |

共 14 项发现。
