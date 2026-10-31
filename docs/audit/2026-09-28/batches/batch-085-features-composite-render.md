# 批次 batch-085 审查报告 [P1] src/features/composite/render//

编号区间：AUD-8500–AUD-8599。审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解。

汇总：Critical 0 · Major 1 · Suggestion 8。

---

## src/features/composite/render/topos_cache_codec.py (行数: 285)

### 发现

- [AUD-8501] [Major] `encode_toos_payload_float` 异常路径不调用 `close()`/`abort()`，泄漏 native 编码上下文与实例资源
  - 位置: src/features/composite/render/topos_cache_codec.py:223-235
  - 证据:
    ```python
    enc = ToposImageSequenceEncoder(_os.path.join(tmp_dir, "frame"), cfg)
    enc.open()
    frame_rgba = np.ascontiguousarray(rgba[:, :, :4], dtype=np.float32)
    enc.encode_frame(DecodedFrame(
        frame_index=0, pts=0, time_seconds=0.0, data=frame_rgba,
        pixel_format="rgba", width=w, height=h))
    enc.close()
    ...
    finally:
        _shutil.rmtree(tmp_dir, ignore_errors=True)
    ```
  - 影响: `open()` 内部已创建 native `ToposGopContext`（"abort/close 即丢弃未提交事务"，见 src/shared/export/topos_encoder.py:1232-1239）与 `ToposMuxFile`；若 `encode_frame` 抛错（转换/编码错误会被包装为 RuntimeError 再抛，topos_encoder.py:1354-1371），`enc.close()`/`enc.abort()` 永不执行——`close()` 承担的 GOP context 释放、`_restore_slice_threads()` 注销、N01/N02 事务日志清理全部落空，native 资源只能依赖 GC 兜底。`disk_cache_store.store` 对 HALF 失败仅回退 npz 并继续处理后续帧（disk_cache_store.py:215-219），在 HALF 链路持续故障的机器上每次 HDR store 尝试都会重复泄漏一个实例。
  - 建议: 用 try/except 包住 `encode_frame`/`close()`，失败分支调用 `enc.abort()`（幂等）后再 reraise；rmtree 保持在 finally。
  - 置信度: 高（缺失防护为结构事实；encoder 的 close/abort 契约已核实。当前调用 slice_threads=None，进程级线程登记不触发，故定 Major 而非 Critical）

- [AUD-8502] [Suggestion] `Optional` 未从 typing 导入即使用
  - 位置: src/features/composite/render/topos_cache_codec.py:30,62
  - 证据:
    ```python
    from typing import Any, Dict, Tuple
    ...
    _probe_result: Optional[ToposPayloadInfo] = None
    ```
  - 影响: `from __future__ import annotations`（第 24 行）使该注解运行时不求值、不报 NameError，但 mypy/pyright 会将 `Optional` 判为 undefined name——类型检查失败或迫使整文件降级忽略。
  - 建议: 导入行补 `Optional`。
  - 置信度: 高

- [AUD-8503] [Suggestion] 每次调用新建 `ToposCodec` / `ToposImageSequenceEncoder` 实例，丢失持久缓冲复用
  - 位置: src/features/composite/render/topos_cache_codec.py:176-180,223
  - 证据:
    ```python
    codec = ToposCodec()
    # G5 收尾（2026-09-21 复查）：binding 支持指针直传（G7 同款）——去掉
    # 每帧 4× 全帧 tobytes() 序列化拷贝
    packet, _stats = codec.encode_frame(
        fc, [np.ascontiguousarray(p) for p in planes])
    ```
  - 影响: `ToposCodec` 实例持有 grow-only 编码输出 scratch（M8：初始 max(32MB, 输入×2)；4K RGBA u16 输入 ≈ 70MB → 首帧即 ~141MB 量级分配），本函数每帧新建并丢弃实例；视频导出路径特意以持久实例避免该成本。节点缓存按视频逐帧 store 时反复大块分配/释放。绑定文档标注 v1 ABI 实例可多线程共享（内部锁串行化，topos_binding.py:2786-2789），具备复用条件。
  - 建议: 模块级（或调用方 `disk_cache_store` 级）持久复用一个 `ToposCodec` 实例；HALF 路径的 encoder 实例同理可评估复用。
  - 置信度: 高（每次调用新建为事实；分配量按启发式推算，实际收益标中）

- [AUD-8504] [Suggestion] `decode_toos_payload` 对 info 的 bit_depth=0 无防护，可触发除零
  - 位置: src/features/composite/render/topos_cache_codec.py:275-277,126-127
  - 证据:
    ```python
    rgb_max = (1 << int(getattr(info, "bit_depth", _BIT_DEPTH))) - 1
    alpha_max = (1 << int(getattr(
        info, "alpha_bit_depth", _ALPHA_BIT_DEPTH))) - 1
    ...
    rgb = np.stack([r, g, b], axis=-1) / float(rgb_max)
    alpha = a[:, :, np.newaxis] / float(alpha_max)
    ```
  - 影响: 磁盘上的 .toos 属外部数据；`getattr` 默认值仅覆盖"属性缺失"，若损坏/异构文件 probe 报出 `bit_depth=0`，`rgb_max=0` → `_planes_to_rgba` 内 ZeroDivisionError。上层 `disk_cache_store.load` 捕获后删条目可自愈（disk_cache_store.py:307-310），但按"外部数据须验证"应前置校验。
  - 建议: 对 `bit_depth`/`alpha_bit_depth` 校验 > 0（合理上界 16）后再计算 max code。
  - 置信度: 中（native IDSC 校验是否已排除 0 未验证；异常路径有上游兜底）

- [AUD-8505] [Suggestion] manifest 片段键 `file_bytes` 与消费方读取的 `bytes` 不匹配，override 为死代码
  - 位置: src/features/composite/render/topos_cache_codec.py:190,243；消费方 src/features/composite/render/disk_cache_store.py:265-266
  - 证据:
    ```python
    # 本文件返回的片段（两处 encode 均）：
    "file_bytes": int(file_bytes),
    # 消费方：
    manifest["bytes"] = topos_manifest.get(
        "bytes", manifest["bytes"])
    ```
  - 影响: 片段键为 `file_bytes`，`disk_cache_store` 永远取不到 `"bytes"` 而走 getsize 回退——当前数值碰巧一致（store 在 topos payload_path 上重新 getsize），但该 override 分支永不生效；若未来 store 不再重算尺寸，`bytes` 将静默退回旧值。`file_bytes` 作为溯源字段倒是随 `manifest.update` 落盘，形成双字段冗余。
  - 建议: 统一键名（片段改产 `bytes` 或消费方改读 `file_bytes`），删除无效 override。
  - 置信度: 高（两侧代码均已读实）

- [AUD-8506] [Suggestion] `_sizeof` 为死代码
  - 位置: src/features/composite/render/topos_cache_codec.py:283-285
  - 证据:
    ```python
    def _sizeof(struct: Any) -> int:
        import ctypes
        return ctypes.sizeof(struct)
    ```
  - 影响: 全仓 grep 仅此定义、无任何调用（含 tests/）；本文件实际使用的是 `encode_toos_payload` 内联的 `_ctypes.sizeof(fc)`（第 160-161 行）。维护噪音。
  - 建议: 删除该函数。
  - 置信度: 高

### 通过项
- 线程安全：`probe_topos_payload_codec` 双检锁模式正确（GIL 下首检无锁安全），其余函数无共享可变状态
- GPU 资源生命周期：不涉及 OpenGL/ModernGL（纯 CPU 编解码路径）
- GLSL·HDR 正确性：HDR 载荷在 12-bit 编码前显式拒绝（`payload_is_ldr` + ValueError，不无理由 clamp）；HALF 路径 f16 域逐位往返有测试背书（tests/composite/test_topos_cache_payload.py::test_hdr_payload_half_roundtrip，crf=20 无损窗口见 tests/media/test_topos_half_float.py）；NaN 在 `payload_is_ldr` 中比较为 False → 正确路由到 HALF 路径而非被 clip
- 信号防递归：无 Qt 信号
- 状态提交一致性：LDR 路径经 store 侧 tmp+`os.replace` 原子落盘；HALF 路径临时目录建在目标同级、`_os.replace` 同盘原子替换；manifest 携带 codec/profile/layout/bit_depth/alpha 溯源字段
- 输入验证：`encode_*` 对 ndim/shape（HxWx≥4）与取值范围校验齐全
- 解码正确性：GBR 平面序、bit_depth 按文件 info 动态取 max code、target_size 缩放后按实际平面几何组装（G13）均正确

---

## src/features/composite/render/tracker_analyze_worker.py (行数: 169)

### 发现

- [AUD-8507] [Suggestion] 未知 `mode` 静默回退 TRANSLATION，错误被掩盖且 payload 记录失真
  - 位置: src/features/composite/render/tracker_analyze_worker.py:65,119,166
  - 证据:
    ```python
    mode = str(self._params.get("mode", "translation"))
    ...
        mode=_MODE_MAP.get(mode, TrackingMode.TRANSLATION),
    ...
        "mode": mode,
    ```
  - 影响: 节点参数可能来自持久化工程文件（外部数据）；拼错或枚举演进后的旧文件中非法 mode 会被静默按 translation 分析，而 payload 仍原样记录该非法字符串——分析行为与声明模式不符，违反"不得掩盖领域错误/外部数据须验证"。
  - 建议: `_MODE_MAP.get` 未命中时 `failed.emit` 明确报错（列出合法值）而非默认回退。
  - 置信度: 中（UI 侧 ParamDef option 限制正常输入，风险集中在持久化数据）

- [AUD-8508] [Suggestion] 取消语义复用 `failed` 信号 + `"cancelled"` 魔法字符串协议
  - 位置: src/features/composite/render/tracker_analyze_worker.py:132-137；消费方 src/features/composite/ui/composite_page.py:3738
  - 证据:
    ```python
    if self._cancel_event.is_set():
        # Tell the page so the worker bookkeeping is cleared and the
        # node can be analyzed again — a silent return would leave the
        # node stuck in "already analyzing" forever.
        self.failed.emit(self._node_id, "cancelled")
    ```
    消费方：`if error == "cancelled":`
  - 影响: 用户取消（非错误）走错误通道，页面以字符串全等判别；任何底层错误消息恰为 "cancelled"、或协议演进（如本地化错误消息）都会误判分支。功能当前可用（两侧已核对一致），属脆弱契约。
  - 建议: 增加独立 `cancelled = pyqtSignal(str)` 信号（或 payload 带取消标志），页面分别连接。
  - 置信度: 高（协议事实清楚；实际影响低）

- [AUD-8509] [Suggestion] `__init__` 的 `parent` 参数缺类型注解
  - 位置: src/features/composite/render/tracker_analyze_worker.py:37-38
  - 证据:
    ```python
    def __init__(self, node_id: str, media_path: str, params: Dict[str, Any],
                 frame_start: int, frame_end: int, parent=None):
    ```
  - 影响: 其余参数均有注解；`parent` 应为 `Optional[QObject]`（.clinerules 强类型约束）。
  - 建议: 补注解 `parent: Optional[QObject] = None`。
  - 置信度: 高

### 通过项
- 线程安全：QThread run 内跨线程发射信号（queued）安全；取消用 `threading.Event`；`Tracker2D` 无 Qt 依赖，`track()` 内 finally 关闭自持 sampler（tracker_2d.py 已核实）
- GPU 资源生命周期：不涉及 OpenGL/ModernGL
- GLSL·HDR 正确性：不涉及像素数据处理
- 信号防递归：worker 只发射信号；页面侧 `_tracker_workers` 字典防重入（composite_page.py:3668），handler 不回触发分析
- 状态提交一致性：结果以不可变 payload dict 单向返回；`track_data` 的提交/撤销在页面侧（composite_page.py:3712-3725），不在本文件职责内
- 异常与取消路径：`run()` 捕获后 `logger.exception` + `failed` 信号（非静默吞没）；探测失败/无帧/参考帧越界均有明确 failed 消息并 return；取消后显式通知页面释放簿记，避免节点卡在 "already analyzing"
- 输入验证：`reference_frame` 越界（<0 或 ≥total）显式报错；`frame_start`/`frame_end` 钳制到 [0, total-1]（注释明确说明页面传 "reference+100000" 的无界约定及不钳制会整段丢弃的后果）；center/size 参数钳制到 [0,1]/[0.01,1]；pattern 越出帧边界由下游 `FeatureDetector.detect` 与 `_ref_template` 内部钳制（feature_detector.py:62-65、tracker_2d.py:862-864 已核实），search 钳制到帧内后与钳制后的 pattern 包含关系成立
- 性能模式：DRAFT 分析质量、按请求单 worker、进度经信号传递无额外拷贝
- 参数契约：`center_x/center_y/size_x/size_y/reference_frame/mode` 键名与 tracker_node.py ParamDef 逐一对上；`probe_info()` 4 元组解包与 FrameSampler 签名一致；matrix 9 元素/行式两分支均正确序列化
