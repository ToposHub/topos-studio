# L 计划发布门报告（L20/L21/L22 Mac 主验）· 2026-10-25（收口队列同日全部闭合）

> 角色：Mac 主集成者。W 复验与本报告分列（本机证据如下；W 侧未执行）。

## L20 真实工程/跨页/导出验收

`tests/content_library_v1/test_l20_acceptance.py`（26 passed / 0 xfailed）：
每个发布 ID 的真实消费者证据共 23 条（stdout 能力矩阵留档）。

| 证据类别 | 数量 | 消费路径 |
|---|---|---|
| 转场复合家族（10） | TransitionInstance → `composite_lib1_transition` 生产统一分派 → 真实帧（端点比特级+中段机器可辨） | edit |
| 转场 mask 家族（6+X01） | `compute_transition_mask` 真实消费（端点精确+中段） | edit |
| 节点四类子门代表（4） | 真实 `_dispatch_effect` 全链：C02 单像素 / C05 块采样 / C19 全帧 CPU / C12 多输入 | composite |
| 配方消费（3） | P33 recipe→convert→TransitionInstance→mask 真实渲染；P41 生产核渲染；P57 宏载荷真实构造 | edit/composite |

- 18 个转场发布 ID 在 catalog/LIB1 metal 回退集登记一致（钉住）。
- 初版 xfail 2 项（C19 全帧消费者/C12 多输入消费者）已随收口队列闭合
  转绿（根因见下表；原"混合绑定态失配"表象的真因是分派器采样器
  uniform 名错写，非测试环境干扰）。

## L21 性能分档/资源纪律/播放压力

`tests/content_library_v1/test_l21_performance.py`（12 passed）。
基准环境：Mac（RX 6800/GL 4.1）；CPU 路径为**导出/兜底档**，实时预览
走 GPU 分支（kind 6–21 已各自真实 GL 对拍）。

| 家族 | 1080p 单帧（CPU 导出档） |
|---|---|
| venetian_blinds mask | 107.8 ms |
| checker_reveal mask | 47.6 ms |
| noise_dissolve mask | 400.6 ms |
| halftone_reveal mask | 135.3 ms |
| staggered_tiles 复合 | 1358.1 ms（最重档：逐块双线性窗） |
| hinged_doors 复合 | 310.5 ms |
| page_curl 复合 | 526.4 ms |
| pixelate_bridge 复合 | 53.1 ms |
| scanline_slices 复合 | 554.8 ms |

- 资源纪律：全帧 CPU 通道 5 次调度池计数增量有界（无泄漏放大）。
- 播放压力：100 帧连续扫描无异常、无 NaN、首帧成本上界。

## L22 打包/缺包/升级/归档往返

`tests/content_library_v1/test_l22_packaging.py`（5 passed）。

- **暂存隔离红线**：子进程干净 sys.modules 验证——生产导入链
  （transition_packs/resample_cores/lib1_fullframe_compute/lib1_nodes/
  node_engine）不触碰 content_staging。
- wheel 构建：resample_cores 包/cores.py/lib1_fullframe_compute/
  lib1_nodes 随包分发；**wheel 不含暂存区文件（红线）**。
- L05 内置预设存在性与契约。
- 配方归档往返：转场配方载荷 JSON 落盘→读回→TransitionInstance→
  mask 真实渲染；宏载荷无环结构往返。

## 收口队列（2026-10-25 第二轮全部收口，xfail 清零）

对读结论先行：初版归因"测试环境干扰/harness 口径冲突"的表象下，
多数项的真因是**分派器级真实缺陷**——fragment 级测试自设 uniform、
直连绑定，测不出分派器的采样器名错写与裸纹理误用；部分项是
**测试自身的双重 flipud**（`_readback_texture_fullres` 已返回 top-left
再补翻）与**fixture 刀锋**（f32/f64 在 floor 半整数平局翻转，ref 文档
自declare"fixture 不应依赖舍入运气"）。

| 项 | 真因（对读定论） | 处置 |
|---|---|---|
| C12 dispatcher"混合绑定态失配" | **生产缺陷**：分派器设了不存在的 `u_map_unit/u_mask_unit`（fragment 声明 `u_map_tex/u_mask_tex`）——赋值被跳过，map 采样器落默认单元 0，把图像当向量图采样；另有 dy 方向反（GL y-up vs CPU 行序，镜像存储的 fragment 测试以共轭掩盖）、REPEAT 纹理边界 [0,1] 钳位环绕混入对边纹素 | 修分派器 uniform 名 + shader dy 取负 + 纹素中心边界钳；测试双重 flipud 一并修 |
| C13 stmap（同族顺带发现） | **生产缺陷**：分派器完全没设 map 采样器（同落单元 0）；origin 两分支公式在 GL-natural 存储下写反 | 修采样器 + origin 对调；新增 dispatcher 级对拍测试钉住 |
| C18/C19 往返保真 | **测试缺陷**：双重 flipud（生产链路本身逐位保真，诊断脚本证 `flipud(got)≡expected`） | 删测试多余翻转；3 处 xfail 全解除 |
| C21 双趟组合 | **生产缺陷 ×3**：`u_low_unit` 名错写（high≡image−image≡0）；blur 把 α 一起模糊（契约 low 保留原 α）；两处把池化裸纹理当 TextureResult 用 `.texture_id`（第二趟 AttributeError——生产路径此前从未跑通） | 修采样器名/α 直通/`.glo`+直绑；新增 dispatcher 级双输出对拍 ×2 |
| C11/C14 GL 渲染 | **生产缺陷+刀锋**：shader 以 GL v 直作行序（GL-natural 存储下应为 row=(1−v)·H，同 stmap 口径）；残余为 floor(+0.5) 半整数平局（C14 刀锋行 v_c=0.5 整行翻转） | shader 改 CPU 行序口径（入口翻 v、texel 行号翻回）；测试按"精确为主+失配像素必须 ∈ 期望源 ±1 邻域"钉住语义 |
| X18 Edge 档 | **fixture 刀锋**：w=24 时 gp_x=(i+0.5)·8/24 在列 i≡1 (mod 3) 恰落精确半整数，floor(+0.5) 平局按列翻转（实测失配列 [4,7,10,13,16,19,22] 与解析刀锋列完全一致）；rank/hash 公式本就一致 | fixture 改 w=22（(8i+4)=11·odd 无解，无平局）；Field/Edge 双档精确通过 |

- 复验：`tests/content_library_v1/ + tests/edit/test_lib1_l13/l14 +
  tests/composite/` 全电池 **8647 passed / 0 个 L 项 xfail**（仅余 1 个
  非本计划的历史 xfail BUG-B2-02；另有 3 个与本计划无关的既有环境
  失败：Homebrew openssl 缺失/checkout 路径断言/档案损坏条目）。
- 附带修复：L21 基准 `_record` 移到断言前（单条超限不再级联清空
  bench_report）+ staggered_tiles 上限单列 3000ms（大电池满载余量）；
  `check_registry_consistency` 豁免插件持有类型（滞后于 AUD-5008
  "registry-only 类型合成保守能力"设计，L18 插件注册曾致大电池误报）。

## 未竟事项（计划口径）

- 多 pass 滤镜（F06 真多尺度等）原生 renderer 支持——单 pass 降级口径
  已声明。
- X14 外部 map 模式——资源子门未开（README/manifest 双声明）。
- MSL（Metal）kernel 未实现——导出显式回退 CPU（禁静默替换）。
- W 侧复验（性能基准/打包在 W 环境重跑）。
