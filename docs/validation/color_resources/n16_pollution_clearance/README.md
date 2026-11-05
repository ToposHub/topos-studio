# N16 顺序污染清偿（2026-11-05，工作流 dwfrun-c17b0e3e）

基线：tests/ui 2 失败 / tests/edit 19 失败 / tests/color+app 合并 5 失败；
分类出顺序污染 16 例（既有文件级失败 10 例不动）。本轮容量 12 例：
侦探假说驱动定位前件 + 独立确认员复跑最小对 + 修理工最小修复 + 脚本级
最小对门槛 + 三套件终验。完整过程证据见工作流报告 artifact
（主交付卡片「N16 顺序污染清偿报告」）与看板（24 项状态）。

## 修复清单（全部经独立确认员验证）

| 受害者 | 机制 | 修复 |
|---|---|---|
| ef03_aud_part1 按钮门控 ×2 | 前件 locale 测试 finally `load_locale(previous)` 无法回到空词典态，en_US 词典永久残留 → `tr('ai_decision.lock','锁定')`='Lock' → KeyError | 目标类 i18n 状态自持 fixture（快照/清空/还原） |
| color_space_descriptor ×3 | R06-B bake 测试 save→load 往返把本机 bt709 偏好注入全局 `_PROJECT_COLOR_OVERRIDES` 不清理，遮蔽 `_StubConfig`（与独立 worktree 定位互证） | autouse fixture 快照/清空/还原覆盖层 |
| lut_resource_registry 空会话 | `load_project` 把包 resolver 绑到 LUT+bake 双全局会话且 `bind_project` 合并全部 kind → bake generated 条目残留在 LUT 会话（目录不可见、保存可见） | 目标测试前置显式 reset 全局 LUT 会话 |
| edit_mode_toggle | 前件泄漏的顶层 EditEffectsPanel（GC 冻结跨测试存活）使 activeWindow 指向外来窗口 → shortcut 守卫静默 no-op | autouse 清空激活窗口 |
| fix_ui_aud3 + fix_wd12g65 ×2 | InsightsWindow `load_module_translations` 永久并入类级词典 → i18n 缺键分支**不插值** default（`{s}` 字面量直达 UI） | **生产修复** i18n.py：缺键分支补 default 插值 |
| fix_wd11g53 音频缓存 | 前件真实探测声卡后模块级 `_cached` 残留 → monkeypatch 探针命中惰性缓存 | 目标测试前置 reset 缓存 |

## EXR SIGILL 专项（severity high）

**根因（实证，非预判的缓冲所有权）**：opencv-python 5.0.0.93 wheel 捆绑的
OpenEXR 3.4 + libdeflate 原生栈——ZIP 族压缩经捆绑 libdeflate 的
`_adler32_impl` 可写函数指针 `jmp *` 分派，本机（CPUID 探测跨进程不一致）
指针被写坏后跳到映像外执行 → SIGILL。证据：12 进程 × 200 循环
（ZIP 7/12 崩溃、PIZ/RLE/NONE 0/12）；反汇编确认 PIZ/RLE 路径零
libdeflate 调用；两份历史崩溃报告 12 帧签名一致；lldb（关 ASLR）不复现。

**修复**：压缩 ZIP→无损 PIZ（1080p f32 实测 12.8MB vs 原始 24.9MB，
HDR/负值语义不变）+ ZIP 族文件读取侧进程隔离解码（崩溃只损失本侧、
返回 None 走 JPEG 回退）。

## 残余（如实登记）

- 4 例容量外污染（看板留档）；
- speech_subtitles 完成加载例未复现（间歇性；纯同步路径，三大候选机制被代码结构排除）；
- **四套件合并单进程终验仍崩溃**（三套件各自跑全过；工作流未记录该崩溃点是否仍为 EXR——需下轮定位崩溃栈后判定 PIZ 修复的合并跑效力）；
- 「同一进程连续两遍」N16 完整验收口径未执行；
- 侦探附带发现 dock_layout one_shot 文件内污染 → 会话内已顺手修复（第三处悬垂 300ms singleShot，与既有两处同款守卫）。
