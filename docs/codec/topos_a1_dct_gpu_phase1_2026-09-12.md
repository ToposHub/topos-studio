# A1 阶段落档：编码 GPU 化一期——差分机 + 位精确 MSL DCT kernel（2026-09-12）

速度计划 v2 A 轴 A1（DCT+量化 → Metal compute）的**前两步交付**：
①数值等价差分机（native dev 导出 + numpy 规范参考）②位精确 MSL
forward-DCT kernel（三方对拍钉死）+ 传输账首测（计划 §7 要求的批首
任务）。**第三步（编码路径集成）另立批次**——架构点与 kernel 优化
需求见 §4。

## ① 差分机（native + Python）

- `tc_dev_set_fcache_capture/elems/copy/layout`（codec.c，dev/test only，
  默认关零开销）：m7_prepare 成功后捕获当帧 F-cache（i32 自然序
  [by*cols+bx]*64，三 plane 块偏移随布局导出；按当帧几何取精确元素数
  ——fcache_elems 是 grow-only 水位，小几何复用大缓存时大于当帧用量）；
- `tests/native/test_a1_fcache_differential.py`：numpy 整数参考（两趟
  kTransformM 矩阵乘，transform.c:18-41 同式；int64 累加后 cast i32，
  |F| < 2^25 精确；pad = 边缘复制 plane.c 语义）与库输出逐值对拍——
  4/4 几何全过（10/12-bit、对齐/非对齐）。

## ② MSL kernel（src/shared/gpu/metal/kernels_dct.py）

- `forward_dct`：MSL int（32 位补码）两趟乘加，kTransformM 经
  constant buffer 注入（`tc_transform_matrix()` 单一真相源）；
- `tests/shared/gpu/metal/test_metal_kernels_dct.py`：真机（Intel Mac /
  AMD，ADR 0003 已证 compute 可用）三方对拍——**native F-cache ==
  numpy 参考 == Metal 输出，逐位一致**（3/3 几何全过）；
- MSL 注意点（已验证）：指针声明须显式地址空间（`const device
  ushort*`）；int 32 位与 C 同；合法域 |A| < 2^18、|F| ≤ 2^25 无溢出，
  整数域累加次序无关 → 位精确有构造性保证。

## ③ 传输账首测（4K 3840×2160 单 luma 平面，dispatch_sync 口径）

| 模式 | p50 |
|---|---|
| A 全链：上载 15.9MB + kernel + 读回 33.2MB F | 50.7 ms |
| B 仅 kernel（平面驻留） | 35.8 ms |

对照：CPU AVX2 全帧（3 plane）DCT ≈ 4~7ms（16t，终态画像 fill 的
DCT 12% band）。

**判读（诚实边界）**：
1. 朴素 kernel 慢 ~1 个数量级——`int x[8][8]`+`int A[8][8]` 线程局部
   数组在 AMD 上大概率走 scratch memory（寄存器溢出）；集成批需重构
   （行驻留累加/共享内存分块/vector load）；
2. F 平面 int32（33MB/plane，100MB/帧）读回是结构性成本——**集成设计
   必须让 F 驻留 GPU 且量化同场完成**（A1 本就含量化），只回传小体量
   汇总（qn/nnz 掩码或 token 前中间量）；CLI 转码语境（需回 CPU 熵编
   码）若无 IOSurface 零拷贝通道则收益存疑（编辑器语境帧本已驻 GPU）；
3. 本机为 Intel Mac + AMD 独显——Apple Silicon 统一内存语境的账需
   另测（zero-copy 上载/读回）。

## ④ 集成批（下一步）的立项要点

- 架构决策（Explore 结论）：Metal 基建在 Python 层（ADR 0003 K4），
  codec 是跨平台 C11——形态二选一：(a) Python 驱动预产 F-cache +
  native 新入口接受外部 F 平面（复用 fill_color_band_from_f 的 F 消费
  契约）；(b) native 内嵌受平台门控的 Metal 后端（无先例，破坏纯度）；
  倾向 (a)；
- 回退钩子同构 `tc_dev_set_sized_m7`（GPU 失败 → CPU m7 路径，位精确
  同值零风险）；
- AQ 活动度（Σ|F_AC|/带）须 GPU 侧同场归约或 CPU 补算（别吞收益）；
- 验收口径：qp 轨迹差分 + packet 逐字节相等（C2 先例）+ A/B wall。

## 验收（本阶段）

- debug ctest 81/81（fcache 捕获不改生产路径：默认关）；
- tests/native 50/50（含 A1 差分机 4 例）；
- tests/shared/gpu/metal 3/3 真机三方逐位一致。
