# W09 · 固定策略候选 A/B 对比基准（机器 B）—— 验证报告

- 基线提交：e950d5e15（W08 收口）
- 机器：B（RTX 4090 / i9-13900K / Windows 11 / av 18.1.0）
- 对应计划：W09（P2，硬依赖 W00 + W01-W08/W10 声明支持子集 ✅）
- 工具：`scripts/bench_ab_routes_w09.py`（子进程隔离路由环境，杜绝
  env 串扰；每对 5 次重复；原始数据 `_work/ab_results.json`，
  schema `bench_ab_routes_w09/1`）。三轮运行数值一致（首轮探针
  231.0→复跑 231.0→终跑 231.0 等），数据稳定。

## 成对结果（5 对 ≥5 重复 + 覆盖率/失败样本）

| 素材（格式） | 对比 | A 均值 fps | B 均值 fps | Δ | 超噪声 | 正确稳定 |
|---|---|---|---|---|---|---|
| 4K H264 8bit | cpu_planar vs hw_download | **335.7** | 150.5 | -55% | ✅ | ✅ |
| 4K H264 8bit | hw_download vs cuda_resident | 156.2 | **231.0** | +48% | ✅ | ✅（注） |
| 2K H265 10bit | hw_download vs cuda_resident | 264.5 | **914.9** | +246% | ✅ | ✅（注） |
| 322×202 H264 8bit | hw_download vs cuda_resident | 3017.9 | **3612.5** | +20% | ✅ | ✅（注） |
| 2K ProRes422 | cpu_planar vs hw_download | 983.7 | 997.0 | +1.4% | ➖（同路由） | ✅ |
| H264 4:2:2 | 覆盖率样本 | 驻留/下载路由均 fail-closed 回退 `planar` | | | | ✅ |

（注）cuda_resident 帧为设备驻留（0 CPU 字节），CPU 侧 md5 不适用——
正确性证据 = W04 L2 / W02 interop 纹理 md5 与同解码器下载参考**逐字节
一致**（test_windows_cuda_resident、test_cuda_resident_format_matrix_w04）。
下载/CPU 路由的帧 md5 与 hw_download 参照 5/5 一致。

## 固定策略候选判定（仅机器 B；不据此宣称运行时策略——UW01 另行立项）

1. **cuda_resident 是 420 8/10bit 的固定白名单候选**（NVIDIA + opt-in）：
   全部 3 个 H264/H265 尺寸位深组合超噪声领先 hw_download（+20% 至
   3.5×；10bit 收益最大），正确性由 interop md5 对拍锁定；
2. **hw_download 在本机不是候选**：4K H264 上比 cpu_planar **慢 2.2×**
   （PyAV is_hw_owned=False 的同步 GPU→CPU 下载往返成本；与既有 AMD
   机器结论同族）——作为驻留不可用时的回退层保留，不进白名单；
3. **ProRes 族不受路由开关影响**（生产路由本就 CPU planar，两列同路
   由，差 <噪声带）；
4. **4:2:2 显式 fail-closed 回退**（两 hw 路由 → planar），不冒充支持。

## 退化项与覆盖率（计划要求逐项列明）

- 退化项：hw_download 相对 cpu_planar 在 4K H264 -55%（如上）；
  322×202 小尺寸下驻留收益收窄至 +20%（接近噪声放大区，白名单收益
  预估按尺寸分档）；
- 覆盖/失败样本：H264 4:2:2 → 驻留与下载路由均回退 cpu planar
  （`yuv422_h264.mov`，W04 能力门拒绝）；5/5 重复无一次 child_error；
- 源版本恢复/文件锁/接收格式：W01（codec 重建+加载门禁）、W10
  （文件锁/归档往返）、W06（干净 venv 两态）各自报告在案；AAF/SDK
  延期项不阻塞本交付（按计划）。

## 声明

- 本报告为**固定候选**对比原始数据与白名单判定输入；运行时自适应
  路由（UW01 动态控制器）不在本包范围；
- 数据仅代表机器 B（RTX 4090）；AMD/Intel 机器各自运行本脚本产出，
  不平均、不代填（B2.2 纪律）；
- 驻留路由保持 opt-in（TOPOS_WIN_HW_RESIDENT_DECODE）默认关闭 +
  全局门闩 + W03 按素材降级——白名单候选的启用决策留给发布流程。
