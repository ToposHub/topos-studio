# C3 落档：Python 绑定/驱动开销——encode scratch 稳态重建修复（2026-09-12）

速度计划 v2 C 轴 C3 项。**结论：定位并修复绑定层 `_encode_scratch`
的稳态缓冲重建 bug——4K 产品路径稳态编码 35.3 → 23.0 ms/帧
（−35%，远超立项预估 +1~3%；实测每帧 ~12.4ms 的 94MB mmap+缺页
churn 是"绑定开销"的主体，旧剖析的 ~6% 归因显著低估）。**

## 根因（src/shared/codec/topos_binding.py `_encode_scratch`）

旧早退条件 `self._enc_buf_cap >= need_cap` 以 packet_bound（4K ≈ 797MB
最坏上限）为判据，而实际分配走启发式（输入 2×，下限 32MB → 4K 444
≈ 94MB）——**条件永假 → 每帧无条件重建等大缓冲**：

- `create_string_buffer(94MB)` = mmap；编码仅触 ~5MB → free 后 mmap
  不复用 → 每帧 ~12.4ms 缺页/分配器开销（实测分解：encode_frame
  全路径 41.9ms vs 裸 ctypes 调用 27.9ms，差值主体即此；string_at
  精确单拷仅 0.85ms）；
- M8 当年修的是"每帧按最坏上限新分配"（零填充 27ms/帧），grow-only
  意图正确，但判据写错目标（need_cap 而非启发式尺寸）导致修复不
  完整——启发式路径从未进入复用分支。

修复：早退对【启发式目标尺寸】判稳（`cap >= heuristic`）。容量轨迹
与旧版逐值一致（首帧启发式 → 溢出 -15 才升全上限），仅消除稳态重建。
溢出重试路径（encode/encode_sized 显式升全上限）不变。

## 验收

| 项 | 结果 |
|---|---|
| 分解计时（4K 444 10bit pro444，同进程对照） | 全路径 41.9→29.5ms；裸调用 27.9→28.2ms（不变）→ 包装开销 14.0→1.2ms |
| 产品路径（prof_p0 长循环稳态中位） | 35.3 → **23.0 ms/帧** |
| 位精确 | 输出缓冲容量轨迹不变、native 调用序列不变 → 包字节不变（media/export 153 测试全过） |
| tests/native | 46/46 |
| tests/media（source/alpha/export） | 153/153 |

## 连带：test_decode_soak_rss_converges 口径重校

修复移除了 fixture 编码期缓冲 churn 对 maxrss 的预抬峰，暴露出旧判据
的两处口径缺陷：maxrss 历史峰值混入 pymalloc arena 阵发噪声（每 200
帧数~十余 MiB 突刺，两轮窗口不可判）；无状态 decode() 每帧新分配输出
平面使增长语义混杂。重校为**产品热路径浸泡**（ToposDecoder + make_views
预分配，gc 后当前 RSS）：深预热 400 帧 + 3×200 帧总增长 ≤ 4 MiB
（实测 ≤ 64 KiB/轮，5/5 稳定）——增长即纯 native，任何逐帧 KB 级
native 泄漏必超。

## 复验钩子

- 分解计时脚本口径见本文（c3_py2 型对照：全路径 vs 裸 ctypes vs
  string_at）；
- `pytest tests/native tests/media -q`；
- prof_p0 产品路径三臂中位对比。
