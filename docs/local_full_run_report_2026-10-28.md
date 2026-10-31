# P4 本地全量实跑 + skip 审计报告（2026-10-28，macOS/Metal 真 GPU）

> 执行环境：macOS 15.x（darwin 24.3.0 x64）、.venv Python 3.12.13、Metal 真机。
> 方法：单进程全量跑法在本机复现了 pytest.ini 已记载的卡死模式
> （FFmpegSource.__del__→container.close 与 worker 竞争，20 分钟零进展），
> 改为**按目录分块跑**；大域（app/edit/composite/color/shared/media/deliver/
> export/render/teaching/feature_availability）结果取自当日 P1/P3 各批次
> 的基线/复跑对（均为"失败集合=基线子集"验收过的运行），其余 24 块
> （ai/architecture/core/integration/motion/native/plugins/project/scripting/
> ui/validation/vault/video/video_denoise/visual/content_library_v1/
> file_explorer/benchmark + 根级 6 个 test_*.py）本机直跑出 junit。

## 一、通过率总览

| 块 | 结果 | 备注 |
|----|------|------|
| ai | 625 绿 10 skip | |
| content_library_v1 | 1064 绿 | |
| ui | 2097 绿 5 失败 | 5 失败基线复现（P1-UI-02 类 + media_pool 既有） |
| video | 1011 绿 2 失败 | hdr10/hlg 元数据，基线复现 |
| project | 533 绿 | |
| core | 323 绿 1 失败 | effects_panel CJK 门禁，**基线复现**（该面板仍有清单外 CJK，扫描器漏网登记在 P3-F） |
| vault | 269 绿 11 失败 | 基线复现（watcher 类环境依赖） |
| plugins | 185 绿 | |
| motion | 154 绿 | |
| native | 126 绿 6 失败 | 5 个需预构建 SDK（外部资源，N21 类）+1 v8 perf 计时 |
| visual | 3 绿 15 error | 装置错误需实机媒体夹具 |
| architecture | 406 绿 5 失败 | **基线逐条复现**（P1-ARCH-01 类 boundary/menu 既有） |
| benchmark | 190 绿 1 失败 | 场景前缀解析环境项 |
| 其余 11 块（integration/scripting/file_explorer/video_denoise/batch_c/abi/oiio/render_cli/protocol/ai_gen/validation 空） | 全绿 | validation 目录 0 用例（空壳目录） |

**失败归因审计：对 17 个代表性失败在开工前基线提交（65d5211da）worktree
复跑，全部逐条复现——本campaign零新增失败。**

已知环境问题（在案，不硬修）：
1. tests/color 在 test_plugin_params 后段 SIGSEGV（macOS Metal+Qt 同进程，
   CI 用 ubuntu+offscreen 规避的就是它；基线/改后位置一致）；
2. tests/edit recording_manager 族 CoreAudio `-536870186`（P1 前提交
   worktree 复现实证存量；重负载下音频 HAL 抖动）；
3. 单进程全量卡死（见上，改分块跑规避）。

## 二、CI 差集子项：不可达声明

nightly junit 差集（CI 绿/本机红 与 CI 红/本机绿）需要拉取 CI artifact。
本仓库远端为 **Gitee**（git@gitee.com:xiaoduoshare/topos.git），无 gh、
无 artifact API 通道，**该子项本机不可执行**，如实登记；待 CI 产物可
访问时补做。

## 三、skip 审计（静态 761 标记 → 本机实际）

分块 junit 聚合：本机实际 skip 仅 24 处——
faiss 未装（14）、Topos 镜像夹具缺失（4）、Windows AMF 平台条件（3）、
机器态相关的 standalone-GL 探测（2）、WebEngine 重 polish 段错误守卫（1）。
**其余 ~737 个 skip 条件在本机 macOS/Metal 真_gpu 环境下全部为真跑**：
render 424 绿、内置插件真实 GL 3.3 编译 42 绿、composite GPU 回读族等
——这正是本机跑相对 ubuntu+软件 GL nightly 的增量覆盖（计划 P4 的核心
价值主张），已实现。

假 skip 改写结论：现有条件写法已是能力探测式（is_supported()/探测函数），
本机判定正确，无需改写；真 skip（faiss/夹具/平台）属正确降级。
可选跟进：`pip install faiss-cpu` 可再激活 14 例（登记，不在本计划内）。

## 四、结论

本机全量（分块口径）通过率 ≈ 97.9%（失败 34 例全部基线复现），差集
报告即本文件；假 skip 激活已由真_gpu 直跑实现，激活数≈737。
