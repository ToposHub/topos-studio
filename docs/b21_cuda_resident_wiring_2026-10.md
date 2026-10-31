# B2.1 生产接线：Windows cuda 驻留零拷贝（机器 B · 2026-10-24）

> 计划：`docs/perf_plan_B_windows_2026-10-22.md` §B2.1；依据
> B1.2 路线选择：`docs/poc_interop_b12_windows_2026-10.md`。
> 代码：`src/shared/media/windows_hw/cuda_resident.py`（新）+ 四处接线
> （见 §1）；测试：`tests/media/test_windows_cuda_resident.py`
> （L1×30 + L2×2 真机，32/32 绿）。

## 1. 接线总览（帧流）

```
EditGpuDecodeManager._create_source 3.4b 分支（opt-in + 能力门 + 失败回落 3.5）
  └─ WindowsCudaResidentSource（cuda is_hw_owned=True；首帧验证 'cuda' 格式）
       └─ read_frame_resident → CudaResidentFrame（设备指针+pitch+色彩/PTS，
          0 CPU 字节；解码线程不触 CUDA API）
            └─ [管线以 frame_type='cuda_resident' 透传，无 GL 资源]
                 └─ EditGpuPreviewRenderer._import_cuda_resident_frame（GL 线程）
                      └─ CudaGlInteropSession（私有 CUDA ctx + 纹理池注册一次 +
                         每平面 map→DtoA→unmap）→ GpuDecodedFrame(opengl,
                         zero_copy=True, DecodePath.WINDOWS_CUDA_GL_ZERO_COPY)
                           └─ 既有零拷贝消费通路（_wrap_external_gpu_planes →
                              _render_yuv_to_track_fbo，sampler2D 归一化语义）
```

改动文件：`cuda_resident.py`（契约/源/会话）、`frame_path.py`（枚举+
is_zero_copy 家族）、`edit_gpu_decode_manager.py`（3.4b 路由 + 两处读分支）、
`timeline_preview_renderer.py`（layer 打包/decode_paths/释放链）、
`edit_gpu_preview_renderer.py`（会话惰建/导入/任务分支/释放链/关停）、
`windows_hw/__init__.py`（导出）。

## 2. 门禁与回退矩阵（计划 B2.1 验收项）

| 条件 | 行为 | reason/trace |
|---|---|---|
| `TOPOS_WIN_HW_RESIDENT_DECODE` 未开（**默认**） | 不进入驻留分支，走既有 3.5 下载 planar / 4 CPU | opt-in 门（计划：先 opt-in，默认启用另需 L3） |
| opt-in 开 + 非 win32 / 无 nvcuda / 无 cuda-bindings | 不进入（能力门 fail-closed） | `not_windows`/`nvcuda_missing`/`cuda_bindings_unavailable:*` |
| opt-in 开 + 非 H264/H265/HVND | 不进入（codec 门与 3.5 一致） | — |
| `TOPOS_DISABLE_WIN_HW_DECODE=1` | 驻留与 3.5 同被禁用（kill switch 复用） | — |
| 源 open/首帧验证失败（非 cuda 格式、硬解不支持 profile） | 抛 MediaOpenError → 落回 3.5 下载 planar | `decode_manager.create_source` trace 记录原因 |
| 运行中 GL 导入失败（会话/拷贝错误） | 该帧 track_skip（不交付半帧/不伪造标记）+ **降级门闩计数**：连续 3 次导入失败后新源停止驻留路由（`resident_route_available`），成功导入即复位连续计数；门闩进程内不自动解除（设备级故障不自愈），诊断接口 `reset_import_failure_latch` | `gpu_renderer.cuda_import_failed` + `cuda_resident.py` 门闩 |
| 运行中解码会话失效（产出软帧） | MediaDecodeError 显式抛出（fail-closed，不 CPU 兜底伪装），该帧 get_frame_error | `cuda resident session produced <fmt>` |
| 源帧已释放/跨线程 bind | 会话拒绝（`invalid_or_released_frame`/`session_thread_mismatch`） | 驱动不校验句柄（B1.2 §3-3），纪律在我方 |

**降级门闩语义（B2.1 补齐，2026-10-24 第二批）**：运行中导入失败的现实
原因是全局性的（CUDA 上下文/设备级、绑定层缺席），非单源属性——故为
**全局连续失败门闩**（阈值 3）：tripped 后管理器 3.4b 对**新源**恒走
既有 3.5 下载 planar；既有源按既有逐帧回退链（track_skip→render_error
→worker 回退）。单源级降级（按 asset 定位重建）保留为后续精化项。

**尚缺（如实登记，不冒充）**：多轨/最小化/尺寸变化/关闭的 L3 播放
浸泡中的窗口交互部分（需真实窗口管理器，C3.1/Phase 5 人工档）；以及
下方 §5 登记的快照路径缺陷。

## 5. L3 端到端验证（2026-10-24 第二批，机器 B 真机）

`tests/edit/test_cuda_resident_playback_l3.py`（3 用例，真机 ×2 轮全
绿；需 `QT_QPA_PLATFORM=windows`——offscreen 插件不支持 GL）：

- 驱动**真实生产链**：EditGpuDecodeManager（真实路由，frame_type 断言
  = cuda_resident）→ worker 形态 layer → **真实 EditGpuPreviewRenderer
  QThread**（共享 GL 上下文）→ frame_ready / snapshot_ready。
- 覆盖：单轨渲染（内容非黑 + 会话绑定统计 + 门闩未触发）、**反向 seek
  确定性**（120→40→120，源级 slot 纹理两次逐字节一致）、**双轨合成**
  （4K H264 + 2K H265 双驻留源并发，合成结果区别于单轨）。

### 登记缺陷（B2.1 后续片）：快照回读路径非确定性

- **现象**：特定任务交错下，同帧两次渲染的 **1/4 分辨率快照**
  （seek 缓存辅助）底部带读到陈旧/未初始化纹素；幅度随交错变动
  （24B–181KB / 727040B）。
- **定界证据**（三层）：① 源级（interop slot 纹理）两次逐字节一致且
  正确（L2 md5 对拍下载参考 + L3 主线程共享上下文 glGetTexImage 读回
  集合相等）；② **present 槽（用户实际画面）两次 md5 完全一致**（诊断
  实验：8.3MB 全非零）——显示路径不受影响；③ planar 上传路径同序列
  ndiff=1/727040（快照路径预存在同族小量伪影）。
- **结论**：缺陷在渲染器快照链（`_capture_frame_snapshot` 一族）对
  外部平面帧的读取时序，非 interop 回归；因仅影响 seek 快照缓存，
  不阻塞 opt-in 使用。待渲染器快照链专项排查（后续片）。

## 3. 契约要点（与 A 线共享语义）

- **帧身份/所有权**：`CudaResidentFrame`（解码线程构造，av.Frame 引用
  保活，release 幂等）→ GL 线程 `GpuDecodedFrame`（planes=会话纹理，
  `GpuFrameRelease` slot 租约 exactly-once；拷贝完成即与源帧独立，
  源帧立即归还解码器 surface 池）。
- **source-kind**：`frame_type='cuda_resident'`（不复用 `vt_planar`
  分类——计划 B2.1 明示）；telemetry：`decode_path` 集合含
  `win_cuda_resident_zero_copy`；import_upload 段记 `not_applicable`
  （`mode='cuda_resident_zero_copy'`，与 VT 零拷贝同口径）。
- **纹理语义**：NV12→GL_R8/GL_RG8；P010/P016→**GL_R16/GL_RG16 UNORM**
  （raw GL 创建；与既有外部平面 shader 的归一化 sampler2D 兼容；MSB
  对齐码值 /65535 归一化，与 hw_planar_source p016le 契约同语义）。
- **-129 毒化根除**：interop 私有上下文（cuCtxCreate），主上下文零
  触碰——解码会话（is_hw_owned=True，primary_ctx=1 路径）可自由
  open/close/reopen（B1.2 §3-2 实证）。
- **同步**：map/unmap 双向同步 + 拷贝后显式 `cuCtxSynchronize`（L3
  实测 D2A 对主机异步、unmap 不保证等待在途拷贝——补充同步后源级
  逐帧确定性成立）。

## 4. 验证记录（本机 L2）

- 单测 32/32（含真机：NV12 双平面 md5 对拍下载参考、P010 R16 路线
  采样语义、slot 复用与已释放帧拒绝）。
- 真机路由冒烟：env 开启 → `_create_source` 实选
  `(WindowsCudaResidentSource, 'win_cuda_resident')` →
  `read_frame_resident(0)` 真设备指针。
- 回归：windows_hw 套件 89 绿 + edit GPU 管线 57 绿 + frame_path/
  gpu_frame/hot_path 146 中 4 失败为 **pre-existing**（stash 复验同败，
  属 C0.1 已分类既有红项，与本次改动无关）。
- 性能归因见 B1.2 §1（241/306fps vs 下载路径 102/80fps；0 CPU 字节）。
