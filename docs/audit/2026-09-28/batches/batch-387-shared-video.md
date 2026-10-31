# 批次报告：batch-387（src/shared/video//）

- 任务书：docs/audit/2026-09-28/assignments/batch-387.md
- 编号区间：AUD-38700–AUD-38799
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解
- 验证手段：全部文件逐行 Read；关键数值断言用仓库 .venv 实测（moderngl standalone context 对拍 CPU/GPU debayer、mat3 注入约定、色度锚点、fbo.use() viewport 行为）

---

## src/shared/video/__init__.py (行数: 263)

### 发现

（无）

### 通过项

- 通过（无逻辑）：纯 re-export 聚合模块，无自有逻辑。导入 58 个 `__all__` 名称并实测 `import src.shared.video` + `hasattr` 逐一核对，全部可解析，无悬空导出（含 fallback_types/pipeline_decision/playback_frame_trace/color_frame_provider_adapter/frame_provider_protocol）
- 无信号、无状态、无线程、无 GPU 资源——不涉及其余维度

---

## src/shared/video/camera_gamut_matrices.py (行数: 280)

### 发现

- [AUD-38700] [Major] resolve_debayer_params 整对象优先级：RawParams 中性默认静默覆盖 TRWM as-shot WB/黑位
  - 位置: src/shared/video/camera_gamut_matrices.py:249-267
  - 证据:
    ```python
    if raw is not None:
        wb_gains = (float(getattr(raw, 'wb_gain_r', 1.0)),
                    float(getattr(raw, 'wb_gain_g', 1.0)),
                    float(getattr(raw, 'wb_gain_b', 1.0)))
        exposure_stops = float(getattr(raw, 'exposure_stops', 0.0))
        black_level = float(getattr(raw, 'black_level', 0.0))
    else:
        # RC4：clip 无 RAW 参数 → 文件 as-shot 基线（TRWM）
        wb_gains = (float(trwm.get('wb_gain_r', 1.0)), ...
    ```
  - 影响: 优先级是"整对象"而非"逐字段"。RawParams（clip_params.py:961）默认值即中性（wb=1/1/1、black_level=0.0、color_space="native"）；RAW 面板首次编辑任意参数时 `edit_properties_panel.py:1560` 以 `getattr(clip, "raw", None) or RawParams()` 从中性默认重建整个对象。实测：TRWM as-shot (wb 2.2/1/1.6, black 256) 下传入默认 RawParams → 输出 wb=(1.0,1.0,1.0)、black_level=0.0。即用户仅轻推一下曝光，as-shot 白平衡与黑位即被中性值冻结替换，预览/导出同步出现色偏与黑位抬起，且与"未动 RAW 面板"时的画面不一致（三链同参但与 RC4 基线断链）。
  - 建议: 二选一收口——(a) 逐字段优先级：RawParams 字段引入"未设置"哨兵（如 None），resolve 时逐项回退 TRWM；(b) 面板首次编辑时以 TRWM as-shot 值播种 base（`base = clip.raw or RawParams.from_trwm(trwm)`）。任一方案需同时覆盖 GPU 预览/CPU 回退/导出三链。
  - 置信度: 中（机制经实测确证；"整对象覆盖"是否为有意产品语义需产品确认，但与面板注释"TRWM as-shot 值经 RC4 预览回退生效"的意图矛盾）

- [AUD-38701] [Suggestion] resolve_gamut_id 对"显式但未知"的 color_space 静默落到 TRWM id
  - 位置: src/shared/video/camera_gamut_matrices.py:203-212
  - 证据:
    ```python
    cs = str(color_space or "").strip()
    if cs and cs != "native":
        gamut = get_camera_gamut(cs)
        if gamut is not None:
            return gamut.gamut_id
    if trwm_sensor_matrix_id:
        gamut_id = TRWM_ID_GAMUTS.get(int(trwm_sensor_matrix_id))
    ```
  - 影响: 实测 `resolve_gamut_id('arri-awg4', 2)` → `'sony-sgamut3'`：用户显式选择了一个无法识别的 id（工程序列化旧值/手改工程文件）时，不提示"未知"，而是静默采用 TRWM 携带的另一家厂商色域——违反本模块自己声明的"未知相机显式提示，不静默猜"（模块 docstring 43-45 行）。当前 UI 下拉只出预设 id，可达性低，但工程文件为外部输入。
  - 建议: `cs` 非空且非 "native" 但查不到预设时，直接返回 None（走 identity + 上层"未知相机"提示路径），不再下落到 TRWM id。
  - 置信度: 高

### 通过项

- GLSL·HDR 正确性：矩阵推导实测双锚点通过——BT.709 原色推导 vs ITU-R BT.709 规范矩阵最大误差 3.4e-7；V-Gamut→709 首行 [1.8066, -0.6957, -0.1109] 与 Panasonic 官方手册值一致；8 预设均 D65 白点，Bradford 分支为正确的 dead-code 防御
- matrix3_column_major 列主序注入约定实测正确：GLSL `m * v` 结果与 numpy `M @ v` 最大差 6e-8（float32 精度内）
- 线程安全：模块级常量在 import 期一次性构造（frozen dataclass + __post_init__），运行期只读
- 异常路径：TRWM 值在 topos_trwm.py 解析层已做 float()/int()/有限性校验，本模块的强制转换属二次防御；未知 sensor_matrix_id (9..255) 实测返回 None → identity + 显式日志，符合规范
- 类型注解完整

---

## src/shared/video/cfa_cpu_debayer.py (行数: 149)

### 发现

- [AUD-38702] [Major] black_level 应用点与 GPU shader 不一致：逐平面"先减后平均" vs GPU"先平均后减"，阴影区两链可见漂移
  - 位置: src/shared/video/cfa_cpu_debayer.py:104-109（对照 src/shared/video/cfa_gpu_debayer.py:150-153）
  - 证据:
    ```python
    if black_level:
        # RC8：码值域黑位减除（与 GPU shader 同点：debayer 前逐相位平面）
        r = np.maximum(r - black_level, 0.0)
        gr = np.maximum(gr - black_level, 0.0)
        ...
    ```
    GPU 侧（cfa_gpu_debayer.py:150-153，插值组装之后才减除）：
    ```glsl
    // RC8：black level 减除（码值域，fetch 后减除，clamp 0 防负值进 log 段）
    r = max(r - black_level, 0.0);
    g = max(g - black_level, 0.0);
    b = max(b - black_level, 0.0);
    ```
  - 影响: `max(·,0)` 非线性，"逐采样减除再插值" ≠ "插值再减除"。实测（12-bit 随机平面、bl=200、WB/EI/gamut 同参）：bl=0 时两链最大差 5e-4（纯 float 舍入，等价）；bl=200 时 96 像素中 23 个差 >1e-4、最大 0.0388（输出域，最差点 CPU=0.0388 / GPU=0.0）——样本跨越黑位时（阴影噪声区常态）出现可见分歧。CPU 链是 CPU 回退预览与交付导出（timeline_export._debayer_cfa_to_bgr）的实现，GPU 链是预览主路径：同一素材"GPU 预览 ≠ CPU 回退预览 ≠ 导出"的阴影渲染，正是本模块 docstring 声称要防的链间漂移；注释"与 GPU shader 同点"与事实不符。
  - 建议: 两链统一为同一式（建议按物理语义取逐采样减除：黑位是 per-photosite 偏移，应在插值前扣除——即把 GPU shader 的减除移到 fetch 值参与加权前；或反向统一 CPU），并在两文件注释同步更正"同点"表述。
  - 置信度: 高（已实测复现并定位到逐样本 clamp 顺序）

- [AUD-38703] [Suggestion] 位深分界谓词与 GPU 不一致：CPU `>12` 走线性、GPU `<16` 走 LOG0，13–15 位输入两链完全分歧
  - 位置: src/shared/video/cfa_cpu_debayer.py:118（对照 src/shared/video/cfa_gpu_debayer.py:295-296）
  - 证据:
    ```python
    if bit_depth <= 12:
        a, k, c0 = traw_log12_constants()
        ...
    else:
        fw = 65535.0
        lin_r, lin_g, lin_b = r_full, g_full, b_full
    ```
    GPU 侧：`fs = (_LINEAR16_FS_TEMPLATE if self._bit_depth >= 16 else _DEBAYER_FS_TEMPLATE)`
  - 影响: bit_depth=13/14/15 时 CPU 走 16-bit 线性归一化、GPU 走 12-bit LOG0 逆曲线，实测两链最大差 0.71（完全不同画面）。当前 TRAW 格式仅定义 12/16 且多数调用方已归一化到 12/16，属潜伏缺陷；但 edit_gpu_preview_renderer/timeline_export 直接取 `plane_infos[0].bit_depth`，若上游出现 14-bit 平面即触发。
  - 建议: 统一分界谓词（两处同为 `>= 16` 或同为 `<= 12`），并对 12/16 之外的位深显式告警或拒绝，而非各自落到不同分支。
  - 置信度: 高（代码与实测确证；13–15 位输入当前可达性低）

- [AUD-38704] [Suggestion] _assemble_rgb 每次组装触发 12 次全平面 np.pad 拷贝
  - 位置: src/shared/video/cfa_cpu_debayer.py:33-40, 43-79
  - 证据:
    ```python
    def _fetch(arr: np.ndarray, dy: int, dx: int) -> np.ndarray:
        padded = np.pad(arr, ((1, 1), (1, 1)), mode="edge")
        h, w = arr.shape
        return padded[1 + dy: 1 + dy + h, 1 + dx: 1 + dx + w]
    ```
    （_assemble_rgb 内 r10/r01/r11×4/b01/b10/b00×4 共 12 次 `_fetch` 调用）
  - 影响: 每次 _fetch 都对整个相位平面做一次 edge pad 拷贝；4K 帧（平面 1920×1080 float32 ≈ 8.3MB）单次 debayer 约 12×8.3MB ≈ 100MB 额外内存搬运。CPU 链承担回退预览与整段交付导出，长序列导出时为可观的无效带宽。
  - 建议: 每个平面 pad 一次（共 4 次）得到 padded 视图，_fetch 改为对同一 padded 的纯切片；或用 np.roll/显式边界分支。语义不变，纯性能收口。
  - 置信度: 高

### 通过项

- GLSL·HDR 数值同源（除上述两点外）：实测 CPU vs GPU 对拍——12-bit LOG0 逆曲线、WB/EI、gamut（右乘 M.T ≡ M@v）、clamp(0,1)→gamma2.2/sRGB 折点、奇数帧尺寸（13×9 截断语义）、16-bit 线性路径（差 <6e-7）全部一致；sRGB 分段 `np.where(lin >= 0.0031308, ...)` 与 GLSL `step/mix` 语义等价
- clamp(0,1) 为显示域（查看伽马前）设计语义，CPU/GPU/导出三链同点，非无理由 HDR 截断（gamma_mode=0 时同样 clamp 属查看域契约，由调用方注释背书）
- 输入验证：4 平面数量校验；异常为显式 ValueError，无宽泛 except
- 类型注解基本完整（`planes` 参数未标注类型，小瑕疵不计）

---

## src/shared/video/cfa_gpu_debayer.py (行数: 433)

### 发现

- [AUD-38705] [Major] upload_and_render 的 viewport 参数被 fbo.use() 覆盖，形同虚设
  - 位置: src/shared/video/cfa_gpu_debayer.py:410-418
  - 证据:
    ```python
    if viewport is not None:
        prev = self._ctx.viewport
        self._ctx.viewport = tuple(int(v) for v in viewport)
    try:
        fbo.use()
        self._vao.render(moderngl.TRIANGLE_STRIP)
    finally:
        if viewport is not None:
            self._ctx.viewport = prev
    ```
  - 影响: ModernGL 的 `Framebuffer.use()` 会把 GL viewport 重置为 FBO 尺寸（已用 standalone context 实测：`ctx.viewport=(0,0,10,10)` → `fbo.use()` 后变 `(0,0,64,48)`）。此处先设 ctx.viewport 再调 fbo.use()，自定义视口在 render 前即被覆盖——渲染恒铺满整个 FBO，`viewport` 子区域语义完全不生效，且无任何报错。当前唯一调用方 edit_gpu_preview_renderer._render_cfa_frame 未传 viewport，故为潜伏缺陷；一旦有调用方依赖子区域渲染（如 track FBO 分区）将得到静默错误画面。
  - 建议: 将 `fbo.use()` 提前到设置 viewport 之前（先 use 再设 viewport 再 render），或改用 `fbo.viewport`/scissor 实现子区域；finally 恢复逻辑保持。
  - 置信度: 高（moderngl 实测确证）

- [AUD-38706] [Suggestion] dtype 构造参数为死代码；_write_plane 对非 uint16 输入静默截断
  - 位置: src/shared/video/cfa_gpu_debayer.py:278-281, 329-331
  - 证据:
    ```python
    def __init__(self, ctx: moderngl.Context, dtype: str = 'f2',
                 bit_depth: int = 12) -> None:
        self._ctx = ctx
        self._dtype = dtype          # 全文件唯一引用，无消费者
    ...
    def _write_plane(self, idx: int, plane: np.ndarray) -> None:
        data = np.ascontiguousarray(plane, dtype=np.uint16)
        self._textures[idx].write(data.tobytes())
    ```
  - 影响: `dtype='f2'` 参数与 `self._dtype` 从未被读取（纹理恒为 `dtype='u2'`），误导调用方以为可配置；`np.ascontiguousarray(..., dtype=np.uint16)` 会把 float 平面静默截断为整型（无告警地上屏垃圾数据），输入验证只覆盖平面数量与形状，不覆盖 dtype/取值域。
  - 建议: 删除 dtype 参数与 `self._dtype`；_write_plane 对 float 输入显式 raise 或告警（当前解码链产出 uint16，属防御性收口）。
  - 置信度: 高

### 通过项

- GLSL 正确性：两模板均符合项目 GLSL 铁律——数字显式 `.0`、无循环（无循环边界问题）、`psize = ivec2((out_size + 1.0) * 0.5)` 的 ceil 截断语义对奇偶尺寸均正确（实测 13×9 对拍一致）；12-bit 模板的 `fw_e/curve_*` uniform 注入有 `< 16` 守卫，正确规避 16-bit 模板无该声明导致的 KeyError→黑屏（注释已说明该坑）
- gamut 注入：mat3 列主序 9 元组约定实测正确（见 camera_gamut_matrices 通过项）；None → 显式 identity，规避 GLSL uniform 默认非 identity
- GPU 资源生命周期：纹理按尺寸变化惰性重建并即时 release 旧纹理、release() 全量释放且可重入（`_textures=[]`、`_vao/_vbo/_prog` 置 None）；类 docstring 明确"只允许持锁渲染线程调用"约束，符合项目渲染线程独占 GL 的铁律；viewport 恢复走 finally
- 线程安全：单 context 渲染线程约束有文档；uniform/纹理每帧全量重设，无跨帧脏状态
- 输入验证：平面数量、平面尺寸 vs 帧尺寸 ceil 期望校验（371-375），错误为显式 ValueError
- 异常路径：无宽泛 except；位深模板选择与 uniform 注入守卫一致

---

## src/shared/video/color_frame_provider_adapter.py (行数: 662)

### 发现

- [AUD-38707] [Suggestion] request_proxy 返回注解引用了从未导入的 "GpuDecodedFrame"
  - 位置: src/shared/video/color_frame_provider_adapter.py:287, 68-76
  - 证据:
    ```python
    def request_proxy(self, frame_index: int) -> Optional["GpuDecodedFrame"]:
    ```
    TYPE_CHECKING 块（68-76 行）仅导入 `DecodedFrame/VideoMetadata/VideoFrameProviderService/ClipInfo/FrameQuality`，无 GpuDecodedFrame（该类实际位于 src/shared/media/gpu_frame.py）。
  - 影响: 悬空前向引用。运行期因 `from __future__ import annotations` 不报错，但任何类型检查/运行时注解内省（typing.get_type_hints）都会 NameError；注解也与实际返回集（GpuDecodedFrame | DecodedFrame | None，见 315-321 行 CPU 回退）不符。
  - 建议: TYPE_CHECKING 块补 `from ..media.gpu_frame import GpuDecodedFrame`，注解改为 `Optional[Union["GpuDecodedFrame", DecodedFrame]]`。
  - 置信度: 高

- [AUD-38708] [Suggestion] open() 宽泛 except 掩盖领域错误；失败路径状态残留
  - 位置: src/shared/video/color_frame_provider_adapter.py:232-256
  - 证据:
    ```python
        try:
            clip_id = self._service.open_clip(path)
            self._clip_id = clip_id
            self._path = path
            self._clip_info = self._service.get_clip_info(clip_id)
            if self._clip_info is None:
                logger.error(f"Failed to get clip info: {path}")
                self._service.close_clip(clip_id)
                return False
        except Exception as e:
            logger.error(f"Failed to open video: {e}")
            return False
    ```
  - 影响: (1) 裸 `except Exception` 把编程错误（AttributeError/TypeError 等）与"文件打不开"领域错误一并吞成 False，违反项目"不得宽泛 except 掩盖领域错误"铁律，排障时只剩一行日志；(2) get_clip_info 返回 None 的失败分支已 close_clip 但 `_clip_id`/`_path` 保持已赋值、`_is_open=False`——残留状态下 `get_stats()`（447 行 `if self._clip_id`）仍会查询服务统计，状态机不自洽。
  - 建议: 收窄为领域异常类型（或至少分离 `except (OSError, ValueError, ...)`）；失败分支返回前将 `_clip_id/_path` 复位为 None。
  - 置信度: 高

- [AUD-38709] [Suggestion] request_master 的 timeout_ms 参数完全未使用
  - 位置: src/shared/video/color_frame_provider_adapter.py:337-341, 355-357
  - 证据:
    ```python
    def request_master(
        self,
        frame_index: int,
        timeout_ms: int = 300
    ) -> Optional[DecodedFrame]:
    ```
    （函数体 359-384 行从未引用 timeout_ms；docstring 自述"第一版不实现 timeout 等待逻辑"）
  - 影响: 调用方传入超时预算被静默忽略，接口契约失真——UI 若以 timeout_ms 控制暂停取帧的最长等待，实际行为是无限期同步请求（阻塞取决于 service 内部策略），签名与行为不符。
  - 建议: 要么接线到底层 `request_gpu_frame(deadline_s=timeout_ms/1000)`（服务已支持 deadline 参数），要么从签名移除并更新 docstring。
  - 置信度: 高（服务端 deadline_s 能力已核实存在：frame_provider_service.py:1400-1414）

- [AUD-38710] [Suggestion] get_stats 返回注解与实际结构不符（嵌套 dict）
  - 位置: src/shared/video/color_frame_provider_adapter.py:440-456
  - 证据:
    ```python
    def get_stats(self) -> dict[str, int | float | str]:
        service_stats = self._service.get_stats() if self._clip_id else {}
        return {
            ...
            'service_stats': service_stats,
        }
    ```
  - 影响: `service_stats` 是 `Dict[str, float | int]`（服务端 2152 行签名），装进声明为 `dict[str, int | float | str]` 的返回值，注解失真；下游若按注解做类型消费（mypy/pyright 严格模式）会误判。
  - 建议: 改为 `dict[str, int | float | str | dict]` 或引入 TypedDict。
  - 置信度: 高

### 通过项

- 状态与生命周期：open/close 状态机（_is_open 门禁所有请求入口）与统计复位一致；close 幂等（未打开直接返回）；共享 clip 引用计数语义正确依赖服务端 close_clip
- 异常与取消路径（除 AUD-38708 外）：各请求入口对未打开状态一律安全返回 None，无半更新状态；GPU→CPU 回退路径（request_proxy 315-321）与 request_gpu_frame 内建回退语义衔接正确（服务端签名 quality: str 已核实，传 'proxy'/'master' 合法）
- _ClipInfoMetadataAdapter 对 ClipInfo 缺失字段（rotation/pixel_format）用 getattr 默认值兜底，外部元数据访问安全；其余字段（codec_name/color_primaries/color_transfer/color_matrix/bit_depth/is_hdr）已核实均存在于 ClipInfo（frame_provider_service.py:484-516）
- _RationalAdapter 提供 to_fps/to_float 双方法，兼容既有 VideoMetadata 消费面
- 线程安全：适配器自身无锁，但状态由 VideoSession 在 _state_lock 保护下的 load/unload 设置，请求只读；与 worker 线程并发 close 存在良性 check-then-use 窗口（_clip_id 置 None 前的旧值经服务端 clip 查找miss 返回 None，不崩溃），风险可忽略
- 信号防递归 / undo·redo·revision·持久化：纯帧服务适配层，无 Qt 信号、无 undo 状态，不涉及
- 性能模式：request_proxy GPU-first + CPU 回退、prefetch 触发式预读符合设计；日志走 debug 级不碍热路径
- 类型注解：除 AUD-38707/38710 两处失真外完整

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 3 | AUD-38700, AUD-38702, AUD-38705 |
| Suggestion | 8 | AUD-38701, AUD-38703, AUD-38704, AUD-38706, AUD-38707, AUD-38708, AUD-38709, AUD-38710 |

无 Critical。三个 Major 均为实测复现：debayer 黑位应用点 CPU/GPU 分歧（预览≠导出阴影）、GPU viewport 参数被 fbo.use() 覆盖（潜伏）、resolve_debayer_params 整对象优先级导致 RAW 面板首触后 as-shot 基线丢失。
