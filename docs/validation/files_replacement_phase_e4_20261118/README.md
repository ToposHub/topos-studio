# E 阶段第三批（E4）：几何/时基/色彩一致性——旋转、SAR、奇数尺寸、色彩矩阵

日期：2026-11-18 · 分支：fix-ui · 范围：`frame_geometry`（共享几何）+
`libav_player`（旋转探取/落地）+ `topos_player`（契约对齐）。

## 交付内容

| 文件 | 变更 |
| --- | --- |
| `frame_geometry.py` | `display_rotation_quarters(matrix)`：FFmpeg DISPLAYMATRIX（16.16 定点）→ 90° 象限（顺时针显示旋转）；**镜像（det<0）与任意角返回 0**——不静默错误呈现，不凑比例假转。`rotate_rgb_quarters`（np.rot90 顺时针）。`fit_aspect_size` 增 `rotation_quarters`：90/270 显示宽高互换 → 等价框转置后拟合，解码后旋转得到框内直立画面 |
| `libav_player.py` | `_LibavSession` 打开时解首帧探取 DISPLAYMATRIX（`_probe_rotation`；探测失败按未旋转处理）；`frame_rgb` 出口统一旋转；fit 传旋转象限；播放器 `_decode_frame`：有显示旋转**不走 YUV/GPU 路径**（该路径不支持旋转，直接 RGB） |
| `topos_player.py` | 会话 `rotation_quarters = 0`（Topos 容器无旋转概念，契约对齐）；GPU 守卫同款，两引擎语义一致 |

实测口径（ffmpeg `-display_rotation W` 写入 → PyAV 首帧 side data 读回）：
90→`[[0,-1],[1,0]]`、180→`[[-1,0],[0,-1]]`、270→`[[0,1],[-1,0]]`，
公式 `atan2(-b, a) mod 360` 象限化。包级 side data 不携带（实测），
只有解码帧携带——故探取在打开时解一帧。

## 覆盖矩阵

* **旋转**：写入 90/180/270 → 象限 1/2/3；镜像/任意角/junk → 0；
  旋转感知拟合（400×200 框 + 90° → 解码 200×112 → 旋转后 112×200 竖幅）；
  **逐像素**：旋转素材全尺寸帧 == 源帧 `rot90(k=-1)`（同一解码器）；
  播放器守卫：`rotation_quarters≠0` 时 GPU YUV 不被调用、RGB 被调用；
  未旋转素材照常优先 GPU YUV。
* **SAR**：2:1 SAR 的 320×180（显示 640×180）装 400×200 → 400×112；
  SAR 与旋转同时存在的显示比例保持。
* **奇数尺寸**：853×477 源等比装框，偶对齐输出、比例不拉伸。
* **时基**：24/25 精确帧率（timescale=每秒 tick、ticks=1 与 ticks=2 两型）
  矩阵补齐；30000÷1001 为既有 FR-19 用例。
* **色彩/HDR**：BT.2020+PQ 合成素材 → `frame_yuv().matrix == 9`
  （H.273 原样）、RGB 路径同源可用。

## 验证

- `tests/ui/test_playback_geometry_e4.py`（新，20 项：纯函数 9 + ffmpeg
  合成集成 7 + 时基矩阵 3 + 播放器守卫 1）全绿；
- 既有回归：`test_playback_semantics_regressions`（22，含 Topos RGB 等
  比回归保留）、`test_playback_eof_e3`（9）、`test_gpu_yuv_frame`
  （5+3 真媒体跳过）全绿——合计 57 通过。
- 修复途中引入并即时纠正的一处回归：`_idx_of` 参数名误改（本批提交前
  已恢复原签名 `def _idx_of(self, frame)`）。

## 诚实边界

- **HDR 仍为元数据级**：PQ/HLG 直通不做色调映射（与整改前一致，
  FUP-07 中 HDR 展示口径未变）；GPU 上传有 BT.2020 矩阵（既有）。
- **旋转只在 RGB 路径**：YUV/GPU 路径不支持（播放器守卫回退 RGB），
  即旋转素材不用 10/12-bit GPU 直上——预览正确性优先于位深。
- 合成素材为 ffmpeg lavfi 生成（临时目录，不入库）；**真实手机竖拍
  素材/真机显示验证未做**（归 E7 真实格式矩阵）。
- Topos 格式无 SAR/旋转概念：按 1:1/0 契约处理（如实标注，非缺失）。
- "共享 decoder"：图片路径已并入 E1 共享 runtime；视频会话仍按引擎
  各持一个（绑定层单实例契约），跨引擎合并属 E5 议题。

## 隐私

ffmpeg lavfi 合成素材（testsrc2 测试卡）+ 临时目录；无真实媒体、
无隐私路径。
