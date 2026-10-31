# W02 · CUDA 生命周期/异常回滚/池预算（L1 替身 + 真机抽检）报告

> 责任包：W02（`docs/upgrade_plan_W_windows_2026-10-24.md` §3，对应 W-I03/R12）。
> base `620168431`（W01 提交后）；实现 + 测试全在本机（机器 B，RTX 4090/621.01）。

## 1. 实现（`src/shared/media/windows_hw/cuda_resident.py`）

| 项 | 旧行为 | 新行为 |
|---|---|---|
| 上下文销毁 | `destroy()` 从不调 `cuCtxDestroy`（长生命周期多 renderer 累积 CUDA 上下文） | 注销全部资源 → 释放纹理 → `cuCtxDestroy`，幂等；全程序在私有上下文守卫内 |
| 分阶段创建 | 纹理对→注册失败直接抛出：已建纹理+已注册资源全泄漏 | 逐阶段（纹理对→注册Y→注册UV），失败逆序回滚：第二平面注册失败撤销第一平面注册与两张纹理；`_create_plane_textures` 内部第二纹理失败释放第一张 |
| 池预算 | 仅计数上限 32：32 个空闲异 key slot 即耗尽（W-I03 实查） | (计数 max_ring_size, 字节 max_pool_bytes 默认 1GiB) 双预算；超限按 LRU 回收**空闲** slot（活跃租约绝不回收）；无可回收诚实拒绝，reason 带实时占用（count=/bytes=） |
| current-context（R12） | 仅靠 thread id 相同推断正确 | initialize 后 `cuCtxGetCurrent` 核验；bind/acquire/evict/unregister/destroy 全部经 `_private_context` 守卫：进入前按需切到私有上下文，退出恢复调用者原上下文（含"原本无 current"的弹出语义） |
| 可诊断状态 | destroy 后 bind 报 session_not_initialized | 区分 `session_destroyed`；stats 增 `destroyed/pool_bytes/max_pool_bytes/ctx`；销毁后 initialize 不隐式复活 |
| 句柄比较 | — | `_cu_ctx_ptr/same/is_null`：真绑定经 `int(CUcontext)` 取句柄值（实测 `getPtr()` 返回的是 Python 包装对象地址，同值句柄两个包装不同，不能用于比较——W02 首轮 L2 失败即此因，已修） |

## 2. 测试（分列：L1 替身 / 真机）

**L1 替身（新文件 `tests/media/test_cuda_resident_lifecycle_w02.py`，20 用例，
全平台可跑，fake `cuda.bindings.driver` + 纹理替身，无 GPU/GL 依赖）：**

- destroy：cuCtxDestroy 恰一次 + 幂等 + 4 资源全注销 + 纹理全释放 +
  调用者上下文恢复 + 销毁后不复活/bind 带 reason；
- 回滚：第二平面注册失败→1 注销+2 纹理释放+池账目归零；纹理创建失败/
  部分失败零残留；
- 池预算：字节超限 LRU 回收（含注销计数）；活跃租约绝不回收、归还后
  复用；计数上限先回收后拒绝（reason `count=2/2`）；账目往返一致；
  复用更新 LRU 序（被复用 slot 不被回收）；
- current-context：bind 成功/拷贝失败后调用者上下文恢复；原本无
  current 时不残留私有上下文；跨线程 bind 仍拒绝；initialize 校验失败
  → 销毁刚建上下文；
- 压力：100 次 create→bind→release→destroy 循环，cuCtxDestroy 恰 100 次
  （替身层无累积）。

**真机抽检（本机 4090，脚本见本目录 inline 于报告提交说明）：**

- 双会话同线程交错：current=A 时销毁 B → B 的注销/销毁在 B 私有上下文
  执行，结束后 current 恢复 A；A 不受波及；再销毁 A 后 current 为空
  （0x0）——守卫的"保留/恢复"契约在真驱动上成立；
- L3 端到端（`tests/edit/test_cuda_resident_playback_l3.py`，真实渲染线程
  + 共享 GL 上下文）：3/3 通过——真实 stop()/destroy() 路径（含
  cuCtxDestroy）经 L3 teardown 实测。

## 3. 回归

| 套件 | 结果 |
|---|---|
| `tests/media/test_windows_cuda_resident.py`（L1/L2，含 md5 对拍） | **37 passed**（与 W00 基线一致） |
| `tests/media/test_cuda_resident_lifecycle_w02.py`（新） | **20 passed** |
| `tests/edit/test_cuda_resident_playback_l3.py`（L3，`QT_QPA_PLATFORM=windows`） | **3 passed**（含反向 seek 确定性） |
| `tests/media/` 全组 | **3391 passed / 0 failed / 213 skipped** |

## 4. 与验收口径的差异（如实登记）

- "100 次开关 renderer"：本包在会话层做满 100 循环（替身）；**renderer
  实例级** 100 次开停留 W08 浸泡（墙钟口径）；
- "超过 32 种尺寸/格式、饱和队列与 seek"：池行为由替身覆盖契约 +
  L2/L3 真机路径覆盖；**长时间显存趋势回落**需 W08 资源趋势记录；
- 构造器既有语义：max_ring_size 被钳到 ≥ ring_size（测试踩坑记录在案，
  未改生产语义）。

## 5. 对消费端的影响

`edit_gpu_preview_renderer._ensure_cuda_interop_session` 构造参数不变
（ring 8/max 32），默认字节预算 1GiB ≥ 32×4K-p016 下限（~797MB），行为
兼容；stats 新增键为超集，现有断言（bind_count/fail_count）不受影响
（L3 实测）。
