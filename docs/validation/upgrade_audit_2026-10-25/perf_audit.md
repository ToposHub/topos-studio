# M16/M17/M18 与 CI 只读审计（2026-10-25）

基线 e6c3b6db126574857306b757203de55286d6496d。仅源码、现有证据、focused 测试和纯逻辑探针；未运行真实产品窗口/长压/DeckLink/Windows。

## 完成判定

| 包 | 当前证据可支持 | 仍不能支持 |
|---|---|---|
| M16 | 25 次各素材独立 Qt 窗口下 decode/import/swap 功能完成；冷首帧、seek 与帧间耗时分布；解码指标/worker 测试 | 24fps 实时无丢 deadline；音钟/真实产品调度；同进程多次切换/关闭再开；2h/10h；当前总帧 p95 超预算不能用 decode p50 免责 |
| M17 | 独立离屏 GPU 缩放候选 helper 和 UYVY 候选，当前 focused 5 passed | 生产纹理直接缩放后回读；减少真实每帧回读流量；监看开关窗口 A/B；DeckLink 真实输出与时序 |
| M18 | 三链模型组件、工程 IO、版本编译、归档、打包头解析准备件 | runner 最终像素/PCM 端到端；合并后 Windows 二轮分发；冻结 exe 全量严格审计/干净安装；跨 OS 卸载原盘往返；外部 NLE/设备条件项 |

## 应纳入新计划的全部明确问题

1. **P1 M16 验收统计错误（静态确定）**：`scripts/bench_m16_real_window.py:346` pending 时停止推进，`:394` 顺序等全部帧，`:508` drop_ratio = 1 - measured/requested，完整播完永远0，即使每帧耗时1秒。`:515` ok 只要求≥50%帧且无异常，`:723` overall 不测 deadline/实际 fps。`manifest_M16_real_window.md:42` 把此计数写“24fps稳定/零掉帧”；`:50` 用 decode p50 在预算内解释总帧 p95=132.8ms。原始 JSON 首组 H265 frame_total_p50=47.092ms，本身也超过41.667ms（manifest 声称 p50 均预算内不成立）。建议保留历史原始 JSON，用新 schema 分离完成率、音钟 deadline miss、墙钟有效 fps、重复/过期帧和显示间隔；预登记阈值/支持矩阵后按真实产品播放调度重测，父包保持开放。
2. **P1 同进程 GL 资源/上下文风险未复验（历史已观察、生产影响待定位）**：manifest 第20行明确第3个零拷贝窗口曾 FBO invalid operation，改每素材子进程后消失。这是隔离结果，不能当同进程资源修复。应在同一产品进程多次开关工程/换素材/多窗口定位 owner context、桥/环槽释放、GL id 重用，验证GPU资源回落；不能仅凭该 harness 故障称产品确定崩溃。
3. **P1 M17 生产接线欠项（静态确定）**：`gpu_scale_readback` 全仓仅测试调用，无产品消费者；接口输入已经是 CPU ndarray，会重新上传 GPU，而非从原生产纹理避免完整回读。`src/app/controllers/file_controller.py:3433` 仍 glReadPixels(input width,height,RGBA/u8)，PBO在同文件3097。应在现有渲染线程从 ColorOutput/Program纹理缩放到监看目标，重用资源/既有释放与背压，保留 contain/range/stride/时序；再A/B真实回读量与p95。改为“候选 helper 完成，生产优化未验收”。
4. **P1/P2 M17 helper 资源和指标 bug（确定）**：`monitor_gpu_scale.py:84` 创建vbo，成功释放列表91–94漏vbo；异常97路径返回None只在自有context finally释放上下文，传共享ctx时所有已创建纹理/FBO/VAO/program/buffer不释放。`ab_metrics:132` 固定*4漏dtype.itemsize：float32 RGBA 4×2实际128B却报32B，目标也少4倍。可小修资源列表逆序finally+dtype限定/实际字节，测试注入各阶段失败和共享ctx不被释放。uint8 `tex_dtype='u1'` 搭浮点sampler2D值得核对，但当前AMD driver 5用例实测通过，不能记已复现错色；补normalized f1和明确LINEAR后需重验像素。
5. **P0/P1 M18 二轮脚本会误绿探针（静态确定）**：`scripts/m18_machine_b_recheck.py:85` 只看returncode；两个逻辑探针文档明确exit0只表示运行，issue_present true/None才是问题/未验；脚本未解析。probe stdout也只保留尾4000，可能截断JSON。加结构化完整结果校验，unknown不能记pass，故障不阻止剩余域及报告落盘。
6. **P0/P1 M18 PE审计误绿（本轮纯逻辑复现）**：`src/shared/platform/bundle_abi_audit_pe.py` consistent仅len(arch_set)<=1，与empty/malformed无关。不存在目录返回{pe_count:0,consistent:true}；仅一畸形bad.dll返回malformed非空但consistent:true；`:95` recheck只看consistent。max_files超限静默跳过无truncated标志；不校支持或目标架构，只有统一x86也能在x64包验过。需有效目录、非空PE、必有exe/主入口、无malformed、全遍历/显式截断拒绝、目标架构与可选头合理性。Mach-O audit同类逻辑也建议检查。
7. **P1 M18 黄金链验收范围不足（确定测试边界）**：`tests/edit/test_m18_golden_chain3.py:75` 只compile_version_jobs并强制timeline_exists/all_assets_online=True；真实视频只用存在性检查+资产路径；三测试编译→IO→再编译→归档，无真实TimelineExporter/job runner或最终像素/PCM。可称模型集成；不能当三链用户交付。补真实sequence/音轨/字幕/grade载荷，经正式队列runner生成文件、探测解码和参考像素/PCM对拍，保存重开/Undo/失败路径，Windows同源工程复验。
8. **P1 CI 关键升级逻辑未必阻断（静态确定）**：`scripts/m00_focused_gate.py:30` 仅旧13文件（12+M02），绝大多数M新增edit/shared测试不在此required gate。`.github/workflows/pr-fast-gate.yml:161` known-regressions continue-on-error:true且architecture先失败时后续edit不会运行；shared诸多M新增测试仅nightly `||true` 非阻断。Color/deliver/composite目录已required，不能笼统称全库都不挡。应将当前新增关键序列化/发布/QC/资源/状态流程测试单列分域隔离required（GPU/模型/真实媒体独立），CI触发包含对应shared/app/packaging改变。Windows-logic虽纳入W新文件但最小deps不装Qt，W03中Qt renderer测试诚实skip，W03真实handler仍需Qt logic job/真机门。

## 本轮执行

- `QT_QPA_PLATFORM=offscreen TOPOS_TEST_MODE=1 .venv/bin/python -m pytest tests/shared/test_m17_gpu_scale_readback.py -q --no-header -p no:cacheprovider` → 5 passed, 1 warning / 1.91s。warning=shared_core marker未注册（小文档/配置问题）。
- 纯逻辑probe：missing PE directory consistent=True；malformed-only dir consistent=True；float32 ab_metrics实际128字节报32字节。临时目录自动清理，未修改项目文件。

## 补充：Mach-O ABI 同类漏洞（本轮纯逻辑复现）

`src/shared/platform/bundle_abi_audit.py:69–72` 对 LC_BUILD_VERSION 取 offset+8（platform）而非 offset+12（minos），minor又取低8位补丁位而非bits8..15。正确合成macOS platform=1/minos=13.2.1返回min_os='0.1'。此类 manifest 的最低系统声明不可用，应重生真实bundle报告。`:128` consistent同样仅len(arch_set)<=1，缺目录/空目录consistent=True；max_files截断静默。文档称min OS不高于构建声明目标但函数没有目标参数或比较；Universal/FAT Mach-O不解析而当非Mach-O跳过，当前单x86_64包证据不能外推未来Universal包。加真实头/版本/畸形/空/截断/Universal覆盖，再对实际包重审。
