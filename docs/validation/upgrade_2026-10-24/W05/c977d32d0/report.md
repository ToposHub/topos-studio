# W05 · 快照读回修复与严格缓存正确性门禁 —— 验证报告

- 基线提交：c977d32d0（W04 收口）
- 机器：B（Windows 11 Pro 26200 / RTX 4090 621.01 / PyQt6 6.11.0）
- 对应计划：W05（W-I05），前置 W02 ✅

## 核心结论：B2.1 登记的"快照底部带陈旧纹素"是**测试侧伪缺陷**

### 定性证据链（全部真机实测，本包诊断过程）

1. **复现与定位**：120→40→120 交错下，同帧快照差异**恒定**落在
   568×320 快照的行 240..319（= 最后 25%），cuda 驻留与下载 planar
   两路由同现；带内容非黑、跨渲染呈小集合循环（heap 复用特征），
   不等于帧 40 内容、不等于任何移位/翻转/镜像假设。
2. **产品链逐层排除**：
   - 源级 slot 纹理两次逐字节一致（B2.1 已证 + W02 门禁）；
   - composite 源纹理在快照瞬间经 glGetTexImage 读回**逐字节一致**
     （诊断 probe 实测 COMPOSITE-src IDENTICAL）；
   - 快照渲染瞬间 GL viewport=(0,0,568,320) 正确、scissor 关闭
     （状态 probe 实测）；
   - `glFinish()` 前置不改变结果（非同步竞态）；快照纹理清底后带仍
     非黑（非覆盖不足——带被完整写入）。
3. **根因**：旧测试助手 `_qimage_array` 把 **Format_RGB888（3 字节/
   像素）** QImage 按 `w*h*4` 读取——`constBits().setsize(w*h*4)` 越界
   25%，越界段恰为读取缓冲最后 1/4 = 图像行 240..319，读到 QImage
   缓冲区**之外的堆内存**（每次分配不同 →"陈旧带"随交错变动、幅度
   24B–181KB、planar 同族 ndiff=1/727040——全部特征吻合）。
4. **修正后**：正确按 3 字节/像素读取，同帧快照在两路由、seek 交错、
   背景预缓冲交错、分辨率切换下**逐字节一致**（md5 相等，0 容差）。

### 结论影响

- B2.1 登记的"渲染器快照链缺陷"撤销（伪缺陷）；播放 L3 的宽松断言
  （同帧允许 <50% 字节差异）按计划要求替换为**严格 0 容差**。
- seek 快照缓存（`_on_gpu_snapshot_ready` 回填 CPU 帧缓存）从未被
  污染（缓存的 QImage 对象本身完好，缺陷只在测试读取侧）。

## 产品侧交付（诊断过程中发现的真实缺陷）

### 1. present 链 fence 全程失效（真实缺陷，Windows/PyOpenGL 组合）

`_finalize_present_frame`：`glFenceSync` 返回的 GLsync 在本机 PyOpenGL
下是**原始 bytes 句柄**，`int(fence)` 抛 "invalid literal for int() with
base 10: b'\\x01...'" → 每帧吞异常落 `fence_ns=0` → GpuPresentFrame
`has_fence=False` → **Viewer 从不 glWaitSync**，不等 renderer 写入完成
直接 blit 共享纹理（显示链潜在撕裂；Batch 1 present 协议的 fence 同步
在 Windows 上从未生效过）。修复：bytes 句柄按小端 `int.from_bytes`
取整（int 路径保持不变）；cleanup 闭包持有原始 GLsync 对象。真机验证：
present descriptor `fence_ns != 0 && has_fence`（严格门禁用例）。

### 2. 快照 pass 状态卫生（防御性加固，非缺陷修复）

`_capture_frame_snapshot` 渲染前：scissor 归位 + 显式 viewport + 清底。
共享上下文上任何残留裁剪/视口状态曾可能把全屏三角裁成局部；清底后
覆盖不足显性化为**可检黑带**（陈旧纹素不可能冒充有效内容进入 seek
缓存——覆盖区必被重写，未覆盖区为黑）。

## 严格缓存正确性门禁（tests/edit/test_snapshot_strict_w05.py，3 绿 ×2 轮）

- 同帧快照逐字节一致（0 容差）：**cuda 驻留与下载 planar 两路由各跑**；
- 反向 seek 交错（120→40→120→120）；
- 背景预缓冲交错（background 任务不发 frame_ready，只产快照）；
- 分辨率切换 1080p→2160p→1080p（2160p 超过 small_h 下限 320，真正走到
  快照纹理按尺寸重建路径）；
- 底部 1/4 覆盖检查（非黑 ≥30%——清底后黑带即失败）；
- present descriptor 必须携带非零 fence。

既有播放 L3（test_cuda_resident_playback_l3.py）：`_qimage_array` 修正
为 RGB888 正确读取；同帧断言从 ndiff<50% 收紧为 **ndiff==0**（含反向
seek 用例），×2 轮全绿。W03 恢复 L3 同步修正读取助手。

## 回归

- L3 播放 3 绿（严格断言）+ 恢复 1 绿 + 严格门禁 3 绿，×2 轮；
- tests/edit/test_edit_gpu_pipeline.py + test_gpu_present_frame.py 全绿；
- test_present_protocol_cleanup 1 败 = 既有（W03 回归扫描时已对拍基线
  归因为预存在，非本包引入）。

## 与 M05/M06 的协调声明（计划前置要求）

修改集中在 EditGpuPreviewRenderer 的 Windows 快照/present 路径：
- fence bytes 句柄转换对 int 返回平台（macOS）行为不变（else 分支）；
- 快照 pass 状态归位只影响快照自身 FBO（prev_fbo/viewport 恢复契约
  原样保留）；
- 不触碰共享 composite/YUV shader 与帧 ABI（W04.2/M19 范畴）。

## 已知限制与如实登记

- 诊断期间对"纹理本体 vs fbo.read"的 probe 读（attached 纹理在绑定中
  glGetTexImage）触发过驱动 access violation（Windows NVIDIA 驱动对
  反馈环敏感）——probe 已全部移除，不留产品代码。
- 快照 QImage 的 RGB888 3 字节布局是 `small_w` 向 4 取整保证的跨距
  对齐（既有代码），本包不改变。
