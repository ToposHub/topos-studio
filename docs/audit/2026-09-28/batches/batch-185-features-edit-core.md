# 批次报告：batch-185 [P1] src/features/edit/core

编号区间：AUD-18500–AUD-18599
文件：timeline_frame_mapper.py (398)、timeline_gpu_export_renderer.py (2,224)

---

## src/features/edit/core/timeline_gpu_export_renderer.py (行数: 2,224)

### 发现

- [AUD-18500] [Critical] deep-u16 效果桥代码块缩进脱出 `if deep_u16:`，效果链在两条主路径上失效或取错纹理
  - 位置: src/features/edit/core/timeline_gpu_export_renderer.py:1634
  - 证据:
    ```python
    1617	                if not grade_uniforms:
    1618	                    return self._tex_mid_u16[0], True
    ...
    1633	                return self._tex_mid_u16[1], True
    1634	            deep_tex_u16 = self._tex_mid_u16[0]      # 缩进 12——已在 if deep_u16: 块外
    1635	            if effect_chain:
    1636	                # 无 grade：输入在槽 0，桥输出写槽 1（避免读写同纹理）
    1637	                merged = self._apply_effect_chain_deep_u16(
    1638	                    deep_tex_u16, effect_chain, width, height,
    1639	                    out_slot=1)
    ```
    （已用脚本核对行前导空格数：`if deep_u16:` 为 12，块体为 16，1634–1641 为 12，即块外。注释"无 grade：输入在槽 0"表明本段本应位于 `if deep_u16:` 块内 no-grade 分支之后。）
  - 影响: 两条症状，同一根因。(A) `deep_u16=True` 且无 grade 且带 effect_chain：1617–1618 在 yuv_u16 渲染后直接返回槽 0 纹理，效果链被静默丢弃——高精度（u16）导出中该片段的 blur/sharpen 等效果不出现在成片中，且无任何告警（渲染错误）。调用方 `timeline_export.py:8143 _gpu_opengl_effect_chain` 并未按模块头注释"deep-u16 链一律回落 CPU"做门控（该注释已过时，T-P1-21 放开了 deep 链），路径可达。(B) `deep_u16=False`（8-bit 导出）+ planar YUV 源 + effect_chain（调用方 `timeline_export.py:7984 render_layer_stack` 传入，元素元组第 5 槽）：1634–1641 对本帧从未渲染过的 `self._tex_mid_u16[0]` 执行 deep 桥——8-bit 导出中该纹理恒为 None（`_ensure_u16_fbo_pair` 仅在 deep 分支调用），`src_tex.use(0)` 抛 AttributeError 被 `_apply_effect_chain_deep_u16` 内部 `except Exception: return None` 吞掉（1361–1362），`_prepare_layer_texture` 返回 None → 整帧回落 CPU。结果是"2026-09-17 导出提速（blur 先行，pass2.5）"对 planar 视频片段在 8-bit 导出（最常见场景）下完全失效且零诊断；若实例生命周期约定变化（`_tex_mid_u16[0]` 残留上一 deep 导出内容且尺寸匹配），会把效果作用于过期帧导致成片内容错误。1668–1672 原本正确的 f1 效果链路径因此对 planar 源不可达（仅 packed/图片层经 1699–1703 仍走 f1 链）。
  - 建议: 将 1634–1641 整块右移一层缩进，放进 `if deep_u16:` 块内、1617–1618 的 no-grade 分支处（即 `if not grade_uniforms:` 不再提前 return，而是落入桥接块）；同时为 deep_u16=False 的 planar 源恢复 1668–1672 的 f1 效果链路径。修复后补两条回归测试：u8 导出 + planar 源 + blur 断言 GPU 命中且像素含效果；u16 导出 + planar 源 + 无 grade + blur 断言效果在输出中。
  - 置信度: 高

- [AUD-18501] [Major] 效果链执行路径宽泛 `except Exception: return None` 无日志无计数，掩盖失败（含 AUD-18500）
  - 位置: src/features/edit/core/timeline_gpu_export_renderer.py:1361
  - 证据:
    ```python
    1356	            src_tex.use(0)
    1357	            self._prog_u16_to_f2["u_input_tex"].value = 0
    1358	            self._fbo_f2[0].use()
    1359	            self._vao["u16_to_f2"].render(
    1360	                mode=self._ctx.TRIANGLE_STRIP)
    1361	        except Exception:
    1362	            return None
    ...
    1534	                out_glo = runner.run(
    1535	                    spec, current.glo, params, width, height, dtype="f1")
    1536	            except Exception:
    1537	                return None
    ```
  - 影响: `_apply_effect_chain_deep_u16`（1361/1368/1382）与 `_apply_effect_chain`（1536）共 4 处静默吞掉一切异常：不写日志、不递增 `gl_errors`、不置 `_warned_render_error`。与外层 render_* 的"捕获一次并告警"模式不一致，也违反 `.clinerules`"不得宽泛 except 掩盖领域错误"。正是它使 AUD-18500（None.use / 效果链死路径）长期不可见——导出静默全量回落 CPU，性能回退无法从日志定位。
  - 建议: 至少在 except 分支 `logger.warning(...)` 一次并递增 `self.gl_errors`；deep 桥失败建议同时记 `_record_gpu_fallback` 类原因串，保持与调用方可观测口径一致。
  - 置信度: 高

- [AUD-18502] [Major] release() 在跨线程分支前先闩死 `_released`，一次误调即永久泄漏整个 GL context
  - 位置: src/features/edit/core/timeline_gpu_export_renderer.py:2152
  - 证据:
    ```python
    2150	        """释放全部 GL 资源（幂等；必须在创建线程调用）。"""
    2151	        if self._released:
    2152	            return
    2153	        self._released = True
    ...
    2156	        if self._owner_thread is not None \
    2157	                and threading.get_ident() != self._owner_thread:
    2158	            # 集成层契约违背（创建线程未释放）——跨线程删除对象未定义，
    2159	            # 只告警并交由进程退出回收，绝不冒险崩掉导出线程
    2160	            logger.warning(
    2161	                "TimelineGpuExportRenderer: release() called off the "
    2162	                "creating thread; leaking GL context intentionally")
    2163	            return
    ```
  - 影响: 跨线程 release 的设计是"本次泄漏但告警"；但 `_released = True` 在线程检查之前置位，若取消/收尾路径（如 `timeline_export.py:3074 _release_gpu_export_renderer` 在异常时序下先于生产者线程 finally 被其他线程调用）触发一次，随后创建线程的合法 finally 释放会因 2151–2152 直接 no-op——context、纹理、FBO 全部泄漏且不可恢复。每次命中泄漏一个 standalone GL context。
  - 建议: 跨线程分支不置位 `_released`（仅告警返回，允许创建线程稍后重试）；或在线程检查通过、资源实际释放完毕后再闩 `_released`。
  - 置信度: 中（代码行为确定；实际触发依赖契约外的调用时序，正常串行/流水线两模式均满足同线程释放）

- [AUD-18503] [Suggestion] release() 资源清单遗漏 f2 ping-pong 与 u16↔f2 桥程序
  - 位置: src/features/edit/core/timeline_gpu_export_renderer.py:2164
  - 证据:
    ```python
    2164	        resources = [
    2165	            self._tex_y, self._tex_u, self._tex_v, self._tex_packed,
    ...
    2179	            self._fbo_wipe, self._tex_wipe, self._tex_mask,
    2180	        ]
    ...
    2195	                     self._prog_wipe, self._prog_blend_f4, self._prog_dip):
    ```
    （清单不含 `self._tex_f2[0..1]`、`self._fbo_f2[0..1]`；程序元组不含 `self._prog_u16_to_f2`、`self._prog_f2_to_u16`。）
  - 影响: 与"逐项 deferred-release"模式不一致；当前靠末尾 `ctx.release()` 销毁 standalone context 兜底，实际泄漏≈0，但若未来 release 演化为部分重建/增量释放（`_ensure_ready` 已有重建语义），将产生真实泄漏。
  - 建议: 将 f2 ping-pong 两个 FBO/纹理与两个桥程序补入清单。
  - 置信度: 高

- [AUD-18504] [Suggestion] opacity uniform 未统一裁剪到 [0,1]，与 direct YUV420 路径口径不一致
  - 位置: src/features/edit/core/timeline_gpu_export_renderer.py:2088
  - 证据:
    ```python
    2086	        prog = self._prog_scale
    2087	        prog["u_input_tex"].value = 0
    2088	        prog["u_opacity"].value = float(opacity)
    ```
    对比 direct 路径 `1245  luma["u_opacity"].value = float(np.clip(opacity, 0.0, 1.0))`；另 `_set_transition_uniforms` 2045–2046 直接 `float(element.left_opacity)`，而 legacy 路径 1993–1996 有 max/min 裁剪。render_layer_stack 1872 同样未裁剪。
  - 影响: 调用方传入 opacity>1/<0 时（转场插值边界浮点外溢等），GPU 各路径行为彼此不一致，且与 CPU 合成（裁剪语义）产生像素偏差。
  - 建议: 在 uniform 上传处统一 `float(min(max(opacity, 0.0), 1.0))`（或入口统一裁剪一次）。
  - 置信度: 高

- [AUD-18505] [Suggestion] release() 后 `_ensure_ready()` 可"复活"实例，且已释放纹理属性未置 None
  - 位置: src/features/edit/core/timeline_gpu_export_renderer.py:961
  - 证据:
    ```python
    961	    def _ensure_ready(self) -> bool:
    962	        """惰性创建 standalone context + 程序/全屏四边形；失败后不再重试。"""
    963	        if self._ctx is not None:
    964	            return True
    965	        if self._failed:
    966	            return False
    ```
    release() 结尾仅 `self._ctx = None`（2220），不检查/清除 `_released`，`_tex_*` 等属性仍指向已释放对象。
  - 影响: 违反自身生命周期文档（"导出结束 release()（幂等）"隐含实例终结）。render-after-release 会静默重建整套 context，并在首次 `_ensure_int_texture` 等处对已释放纹理访问 `tex.size`（moderngl 释放后属性访问行为未定义/可抛错）→ 被外层吞掉变 CPU 回落；`_released` 标志形同虚设。
  - 建议: `_ensure_ready` 首行检查 `self._released` 直接 return False（fail-fast）；release() 将纹理/FBO 属性置 None。
  - 置信度: 高（行为确定；正常流程不触发，属防御缺失）

- [AUD-18506] [Suggestion] dip 着色器编译失败时静默跳过 dip_to_color 而非 fail-closed 回落 CPU
  - 位置: src/features/edit/core/timeline_gpu_export_renderer.py:1883
  - 证据:
    ```python
    1883	            if dip_color is not None and dip_factor > 0.0 \
    1884	                    and self._prog_dip is not None:
    ```
  - 影响: 其余 pass 均以"程序缺失 → return None → CPU 回落"为契约；此处 `_prog_dip is None` 时条件为假，dip 过程被无声省略，成片缺少 dip_to_color 转场（渲染偏差）而不是回落正确 CPU 结果。
  - 建议: 改为显式 `if dip_factor > 0.0: if self._prog_dip is None: return None`。
  - 置信度: 高（逻辑确定；触发需 dip shader 单独编译失败，概率低）

- [AUD-18507] [Suggestion] GpuYuvSource 平面在 pass1 通用路径缺少 dtype/ndim 验证（direct 路径有）
  - 位置: src/features/edit/core/timeline_gpu_export_renderer.py:1088
  - 证据:
    ```python
    1087	        # u2 纹理统一按 uint16 上传（u8 平面升位深拷贝，源分辨率代价小）
    1088	        data = plane if plane.dtype == np.uint16 else plane.astype(np.uint16)
    1089	        tex.write(np.ascontiguousarray(data))
    ```
    对比 `_render_direct_yuv420p` 1230–1232 有 `plane.ndim != 2 or plane.dtype != np.uint8 → return None`。
  - 影响: float/int32 平面经 `astype(np.uint16)` 静默截断回绕、ndim≠2 时 `h, w = plane.shape` 解包错乱（异常虽被外层捕获回落 CPU，但 dtype 错误不抛错、直接产出错误像素），违背"外部数据须验证"红线（GpuYuvSource 数据来自解码器边界）。
  - 建议: `_prepare_layer_texture` 对三平面校验 ndim==2 且 dtype ∈ {uint8, uint16}，不满足 return None。
  - 置信度: 高

### 通过项
- 线程契约：standalone context 记录 `_owner_thread`，渲染/释放均在创建线程；实例按导出 worker 隔离（调用方 `timeline_export.py:7825-7842` 缓存、8020-8026 失败自愈为全新实例），无非同步 GL 访问
- GLSL 规范：所有浮点字面量显式 `.0`，循环上界常量（`i < 3`），无隐式类型转换问题；`u_vertices[48]` 与 spec 构建端 `len(vertices) > 48` 拒绝（transition_mask_gpu.py:181）一致
- HDR/clamp 审查：u8 管线各 clamp(0,1) 均为 8-bit 交付域或镜像 CPU `np.clip/rint` 量化点的既有契约；f4 累积缓冲为 RGBA32F 无截断，shadow/highlights 的有界 clamp 与预览 shader 同源——未发现无理由 clamp
- GPU 失败路径：`_ensure_ready` 失败置 `_failed` 不再重试；可选管线（u16/f2/acc/wipe）编译失败仅禁用对应路径并告警；blend.glsl 读取失败时相关 shader 编译失败自动降级（fail-safe）
- 资源尺寸管理：纹理/FBO 按尺寸变化重建、否则复用，重建前先释放旧对象；YUV420 输出拒绝奇数宽高
- 信号防递归 / 状态提交一致性：不涉及（无 Qt 控件、无 undo/持久化职责）
- 回读帧 `copy()` 保持"可写帧"语义，防止下游字幕烧录等原地写炸掉

---

## src/features/edit/core/timeline_frame_mapper.py (行数: 398)

### 发现

- [AUD-18508] [Major] `map_vfr_frame` 完全忽略 `reverse`，VFR 素材反向播放帧序错误
  - 位置: src/features/edit/core/timeline_frame_mapper.py:196
  - 证据:
    ```python
    214	    timeline_fps = Fraction(request.timeline_fps_num, request.timeline_fps_den)
    215	    timeline_time = Fraction(local, 1) / timeline_fps
    216	    source_time = float(timeline_time * request.speed)
    ...
    220	    idx = bisect.bisect_left(request.source_pts_index, target_pts)
    ...
    234	    return FrameMapResult(
    235	        source_frame=source_frame + request.source_start_frame,
    ```
    函数体（196–240）无任何 `request.reverse` 引用；CFR 路径 159–163 有翻转处理。生产调用方 `timeline_v2.py:422-431` 同时传入 `reverse=spd < 0` 与 `source_pts_index=self._source_pts_index`。
  - 影响: 反向播放的 VFR 片段按正向取帧——预览/导出画面运动方向错误。当前 `_source_pts_index` 仅由工程序列化数据填充（src 内无生产者赋值），触发面受限，但一旦 VFR 工程打开即静默出错。
  - 建议: VFR 路径按 `reverse` 将目标时间翻转为 `(clip_source_duration_time - source_time)` 再 bisect，或明确拒绝 `reverse and source_pts_index` 组合（返回 None 走回退公式）并补测试。
  - 置信度: 高（代码确定忽略；实际触发依赖 VFR 数据来源）

- [AUD-18509] [Suggestion] CFR `cadence_action` 在 reverse 下 repeat/skip 判定反转
  - 位置: src/features/edit/core/timeline_frame_mapper.py:178
  - 证据:
    ```python
    175	    diff = abs(float(source_frame - total_source_frac))
    176	    if diff < 0.01:
    177	        action = "exact"
    178	    elif float(source_frac) % 1.0 < 0.5:
    179	        action = "repeat"
    180	    else:
    181	        action = "skip"
    ```
  - 影响: reverse 时 `total_source_frac = L - source_frac`（L 整数），其小数部分为 `1 - frac(source_frac)`：`frac(source_frac) < 0.5` 实际对应四舍五入向上（skip），代码却标 "repeat"，反之亦然。诊断字段（UI cadence 预览/测试断言）在反向片段给出相反结论。
  - 建议: 判定改用 `total_source_frac` 的小数部分（`diff` 同源），与实际取整方向一致。
  - 置信度: 高

- [AUD-18510] [Suggestion] `compute_cadence` 复制请求时丢弃 `source_pts_index`，VFR 模板静默降级为 CFR
  - 位置: src/features/edit/core/timeline_frame_mapper.py:274
  - 证据:
    ```python
    274	        r = FrameMapRequest(
    275	            timeline_frame=request_template.clip_timeline_start + i,
    ...
    285	            reverse=request_template.reverse,
    286	        )
    287	        result = map_frame(r)
    ```
    （构造参数不含 `source_pts_index`，docstring 未声明此限制。）
  - 影响: 对 VFR 片段调用诊断 API 得到的是 CFR 假设下的 cadence 序列，无告警地失真。
  - 建议: 透传 `source_pts_index=request_template.source_pts_index`，或至少在丢弃时 Document/raise。
  - 置信度: 高

- [AUD-18511] [Suggestion] `source_time_seconds` 实际语义与文档不符（忽略入点偏移与 reverse 翻转）
  - 位置: src/features/edit/core/timeline_frame_mapper.py:185
  - 证据:
    ```python
    183	    return FrameMapResult(
    184	        source_frame=source_frame,
    185	        source_time_seconds=float(source_local_time),
    186	        timeline_time_seconds=float(timeline_local_time),
    ```
    字段文档（103–105）称 "Time offset within the source media"；实际返回的是相对入点映射的耗时，未加 `source_start_frame` 折算、reverse 时也未按末端翻转。VFR 路径 236 同样返回 `target_pts`。
  - 影响: 消费方若按文档当素材内绝对时间使用（如解码 seek）会取错时刻；当前主要消费 `source_frame`，影响限于诊断/潜在新调用方。
  - 建议: 二选一：按文档实现（`(source_start + source_frac)/source_fps`、reverse 取 `end - t`），或改文档明确"相对入点的本地时间"。
  - 置信度: 高（行为确定；影响取决于消费方）

- [AUD-18512] [Suggestion] VFR 时间基准与入点叠加约定未定义，绝对 PTS 语义下双重计入入点
  - 位置: src/features/edit/core/timeline_frame_mapper.py:216
  - 证据:
    ```python
    216	    source_time = float(timeline_time * request.speed)
    ...
    220	    idx = bisect.bisect_left(request.source_pts_index, target_pts)
    ...
    235	        source_frame=source_frame + request.source_start_frame,
    ```
  - 影响: 若 `source_pts_index` 存的是 demuxer 常规的素材绝对 PTS，则目标时间应从 `pts_index[source_start_frame]` 起算；现在用 `elapsed*speed` 从索引 0 起搜再 `+ source_start_frame`，入点被计两次（trimmed VFR 片段取帧整体后移）。若索引约定为"已按入点重定基"则正确——但模块未写明该约定，src 内无非 None 生产者、测试也仅覆盖 `source_start=0`（tests/edit/test_timeline_frame_mapper.py:262-273），无法佐证。
  - 建议: 在 `FrameMapRequest.source_pts_index` 文档中明确时间基准；若为绝对 PTS，bisect 前加上入点帧的 PTS 偏移，并补 `source_start>0` 的 VFR 测试。
  - 置信度: 低（约定不明，代码内无生产者可佐证；标记为契约缺陷而非确定的错值）

- [AUD-18513] [Suggestion] `_apply_frame_blend_layer` 参数无类型注解，且每帧热路径函数内 import
  - 位置: src/features/edit/core/timeline_frame_mapper.py:347
  - 证据:
    ```python
    347	def _apply_frame_blend_layer(
    348	    layer,
    349	    layer_frame,
    350	    frame_idx: int,
    351	    get_layer_frame,
    352	):
    ...
    358	    import dataclasses
    ...
    389	    import numpy as np
    ```
  - 影响: 违反项目强类型注解规范；`import dataclasses`/`import numpy` 位于每帧每层调用的函数体内（预览 timeline_preview_renderer.py:2834、导出 timeline_export.py:6879 两个热循环），虽由 sys.modules 缓存兜底，仍属可上移的每帧开销与风格缺陷。
  - 建议: 移至模块顶部 import；补类型注解（`layer: Any, layer_frame: Optional[np.ndarray], get_layer_frame: Callable[[Any], Optional[np.ndarray]] -> Optional[np.ndarray]`）。
  - 置信度: 高

### 通过项
- 有理数运算：全程 `Fraction`（fps、速度、源位置），29.97/23.976 类 cadence 无浮点累积漂移；仅最终换算 float
- 不可变数据：`FrameMapRequest`/`FrameMapResult` frozen dataclass，跨线程传递安全
- 范围检查：CFR/VFR 均对 `local < 0 or local >= clip_timeline_duration` 返回 None；CFR 结果 clamp 到 `[source_start, source_start+duration-1]`
- frame_blend 降级路径：权重 0、b 帧不可用、尺寸不一致、冻结片段（`freeze_source_frame` 短路）均诚实返回原帧；基准帧不匹配时显式重取 a 帧（380–384）避免 nearest 退化
- CPU 混合量化 `clip(x+0.5, 0, 255)` 与 u8 帧契约一致（`_get_layer_frame` 文档为 BGR uint8）
- 输入验证：`grade/wheel/scalar` 类容错不在此文件；`_effective_source_fps` 对 den=0/None 回退安全
- 线程安全 / GPU 生命周期 / 信号防递归 / 状态提交：纯函数模块，不涉及
