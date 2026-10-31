# MG 编辑器审计整改报告（2026-08-22）

对照 `docs/motion_graphic_web_editor_remaining_optimization_plan_2026-08-21.md`
逐项核实后的修复记录。核实结论：审计所列问题**全部属实**（两处细节修正：
`video` 不是合法 SceneElement 类型，属空真；locale 键本身是对称的，问题是
硬编码字符串未接入 i18n）。

## 逐项状态

| 编号 | 问题 | 状态 | 交付 |
|---|---|---|---|
| R11 | composite 段错误 + keyframes hang | 部分完成 | 陈旧断言已由 035a0a38 修复；`test_keyframes_composition_range` 单独运行 4 连跑 1.8–2.3s 全过，**不再复现**（原报告场景疑为与其他 pytest 进程 CPU 争抢）；段错误根因仍按 `scripts/run_composite_tests.sh` 进程隔离运行，ASan 级根因调查保持待办 |
| R1 | 响应式断点与约束 | 已完成 | 82bddce8（此前 AI 完成） |
| R2 | 动画属性覆盖与 Bezier/Spring 一致性 | **本次完成** | 4532744d：evaluator 采样 font_size/blur/fill_opacity/stroke_width 样式轨道（SVG/捕获/CSS 三路同源）；native CSS 发射 font-size/filter:blur，fill-opacity/stroke-width 走内联 SVG 子选择器；Bezier→真实 cubic-bezier()（归一化控制点，y 允许过冲）；StaggerPlan.motion 持久化 spring 语义（参数/settle frame/overshoot），spring_config JSON 安全往返；Inspector 新增 Animation 只读区解释轨道来源 |
| R3 | 网页元素覆盖完整化 | **本次完成** | 4532744d：image/svg/chart/formula/draw 全部物化为 DOM（缺失资产显示诊断占位而不是静默丢层）；捕获路由接入 asset_root 真实解析；动画 CSS 类型过滤放宽到全部元素 |
| R4 | 模板与预设库统一管理 | **本次完成（读侧）** | 统一只读目录 `core/mg_package_registry.py`（MgPackage/MgPackageCatalog，双 kind、locale 显示名、GIF 预览、来源标注）；Library 面板新增 HTML Templates 分类（统一网格/搜索/GIF）；统一拖拽协议（mime 带 kind，模板 drop → `apply_html_template` + 单命令 SetHtmlLayer 可撤销）。写侧（保存/导入导出合并）仍属后续 |
| R5 | MG 全量多语言 | **本次完成（核心面）** | 7c12d781：Effects 面板 MG Section 去 hardcoded 中文（含 zh/en 键）；模板 manifest 支持 display_name_i18n/description_i18n（粒子模板已配 en_US）；13 个预设全部配 en_US 名称/描述且搜索匹配英文；AI 发布模板描述走 i18n；新增 CJK 扫描守卫测试。module.py 其余 AI 消息文案未在本轮范围 |
| R6 | 粒子流光真实端到端 | **本次完成** | `scripts/e2e_mg_particle_flow.py` 真实窗口+真实 Chromium **6/6 通过**：包加载、画布渲染（5686 lit pixels）、两时点采样证运动、加 3 图层+关键帧零重载、导出 3s 1080p H.264（首/中/尾帧互异）、保存→关闭→重开粒子仍运动 |
| R7 | 实际缓存码率验收 | **本次完成** | bae28a8d：`render/bitrate_probe.py`（ffprobe 回读码率/帧数/时长/codec；WebM 帧数走 count_packets 回退）；缓存 worker 完成后写 `<video>.bitrate.json`（档位/目标/最低/实测）；`scripts/verify_mg_cache_bitrate.py` 真实编码验收（proxy 噪声达标 117%，小分辨率高档合法欠冲如实报告，--strict 可收紧；flat 样本 Void 填充精确达到最低码率）；5 个真实编码测试 |
| R8 | DevTools 受控双向工作流 | **本次完成** | 漂移守卫测试抓到并修复 2 个真实漏洞：`topos-native-selection-style`、`topos-page-deck-capture` 被注入但 Apply 读回不剥离（选择高亮/捕获节点会被烤进作者 HTML）；`scripts/e2e_mg_devtools_apply.py` 真实浏览器 **4/4**：脚手架剥离、Apply 单命令可撤销+原生层从场景再生、undo/redo 精确、Discard 重载已存图层。属性级回写（SetElementProperty 映射）仍属后续 |
| R9 | 组件生产路径 | **本次完成** | bae28a8d：`tests/core/test_component_lifecycle.py` 全链路（建 Master→实例→覆盖→Master 更新传播→变体→JSON 往返→Detach 烘焙+单步撤销）+ Detach 首个核心级测试 |
| R10 | macOS 手工稳定性 | 保持手工 | raise/propagate 警告确认为 offscreen 环境限制；`WindowStaysOnTopHint` 策略变更需真实桌面验证后决定，不做无验证的猜测性改动。手工清单见原审计 §13 |
| R12 | 清洁机器打包验收 | **部分完成** | 发现并修复真实漏洞：`pyproject.toml` package-data **漏配 `presets/**`**（wheel 会丢全部 13 个预设+GIF）；新增 `scripts/verify_packaged_resources.py`（构建真实 wheel 并校验预设/模板/locale 完整性 + 绝对路径泄漏扫描），当前全过。清洁机器全流程仍待做 |

## 验证汇总

- 新增测试：R2 9+2、R3 10、R4 6、R5 7、R7 5、R8 2、R9 3（共 44 个，全绿）
- 真实桌面 E2E：粒子流光 6/6、DevTools Apply 4/4
- 回归：render/motion/core 835 通过；MG UI 套件（asset library、manual controls、
  constraints、catalog、devtools contract）全绿；edit cache lifecycle 28 通过
- 单进程完整 composite 回归仍按进程隔离脚本口径执行（R11 根因未修）

## 剩余待办（按优先级）

1. R11 段错误 ASan 级根因（MallocStackLogging / 前缀二分定位失效原生对象来源）
2. R4 写侧统一：Save as Template/Preset 合并入口、mg_package v2 manifest 迁移
3. R8 属性级回写：Apply 的 DOM diff → 元素属性命令映射
4. R5 module.py AI 消息全量 i18n（约 600 行中文文案）
5. R10 真实 macOS 桌面手工清单执行与记录
6. R12 清洁机器（无 venv 缓存/无用户模板目录）完整发布验收

---

## 第二轮：复测报告 R-01…R-13 的核实与修复（2026-08-22 晚）

输入：`docs/motion_graphic_editor_retest_remaining_plan_2026-08-22.md`。逐项核实结论与处置：

| 项 | 核实 | 处置 | 提交 |
|---|---|---|---|
| R-01 素材库模板详情/插入失效 | 属实（详情走 preset-only 查找，Insert 恒禁用，tooltip 泄漏绝对路径） | 按卡片 kind 分支详情/插入；模板显示 GIF+来源+版本；tooltip 脱敏；4 个回归测试 | 76e5bf2c |
| R-02 WebEngine 级联错误 | 属实（D1 超时后 NoneType 级联；offscreen 下探针可崩宿主） | 子进程隔离的 readiness 探针（版本/flags/console/render-process 诊断 + 一次受控重试）；`--require-browser`/`--allow-skip-browser`；失败=1 根因+N blocked skip。桌面实测粒子 6/6、DevTools 5/5 | eb0008d8 |
| R-03 粒子/DevTools 可重复证据 | 属实（对方环境无显示服务器） | 本机真实桌面复跑全绿；环境矩阵由 readiness 诊断输出承载 | eb0008d8 |
| R-04 码率验收 | 属实（`--help` 崩溃；>0 即过；"79.8M 超上限"实为 **1 秒样本的 VBV 瞬态**——bufsize=2×target 允许短片超 maxrate，10s 1080p 实测收敛 8.3/20.6/42.5 Mbps，恰在 8/20/40 产品档内） | `--help` 修复（`%` 转义）；门禁默认 strict（`--report-only` 探索、`--strict` 兼容保留）；噪声样本须落 [60%,125%]（短片按 VBV 物理上限放宽并显式标注）；moving/flat 样本分别标 INFO（复杂度受限）/静态地板策略；默认分辨率提到 720p（320×180 噪声熵上限 ~16M 物理达不到 40M 目标——老脚本正是用它掩盖） | 89fbcb76 |
| R-05 模板只有读侧 | 属实 | 三层来源（bundled 只读/user 应用数据/project `.topos/`）+ 同 id 优先级 project>user>bundled；文件夹/ZIP 导入（先校验+临时目录+rename 原子写）；ZIP 导出；删除拒 bundled；"打开模板库"改开用户目录；刷新覆盖全部来源 | e9117055 |
| R-06 包写路径分散 | 属实（模板零写侧） | 模板侧 PackageStore 核心（校验/原子写/来源权限）落地并与统一目录联通；preset 侧沿用既有库，两者统一为单一接口留作后续 | e9117055 |
| R-07 DevTools 属性级映射 | 属实（整层替换 + 原生元素编辑被静默剥离） | 纯函数白名单映射（位置/尺寸/透明度/颜色/字体/blend/旋转缩放/文字）→ UpdateElementCommand；白名单外回报原因并回退整层（不静默丢）；Apply=属性+源替换**单一撤销步**；E2E D5 真实浏览器证明 | a83bf65f |
| R-08 module.py 国际化 | 属实（60 处用户可见中文消息） | 28 处最高可见面（全部 error_code 失败路径+恢复/取消+3 状态文本）迁移 I18N.tr 双语；意图关键词与模型 prompt **有意保留中文**（输入解析/LLM 提示，非 UI 文案）；清单 lint 冻结剩余 37 处基线——只能减不能增；AI 套件 642 绿 | b901e7e6 |
| R-09 Composite 确定性失败 | 属实（motion_graphic null + shader 缺 u_output_bbox） | null 占位显式化为 DEFERRED_NODE_TYPES（feature module 启用时注册、卸载还原）+ 生命周期测试；shader 声明并用上 u_output_bbox（sampleLayer 经渲染目标区域映射），真实 GL 编译+uniform 反射测试三端契约 | b1d57d74 |
| R-10 拖拽数值回归 | 属实（6 失败） | 恢复 2026-06 设计契约并提取纯函数 `drag_speed_per_pixel`（固定 200px 参考宽度；默认×0.1/Shift×1/Ctrl×0.01 带 0.1/px 条件下限）——与控件宽度/DPR/平台无关；滑杆把手契约独立（宽度自适应跟手）不再共用常量；GL 依赖测试 offscreen 显式 skip 带原因 | b1d57d74 |
| R-11 干净机器闭环 | 属实 | `verify_clean_install.py`：隔离 target 安装 wheel（repo src 不上 sys.path、cwd 在外）跑 6 探针——**首跑抓到真 bug：shared i18n locales 从未进 wheel**（干净机器所有共享翻译会回退英文），已修 package-data 并纳入 wheel 检查；6/6 PASS | c39ec7af |
| R-12 打包扫描优先级 | 属实（`(a and b) or c`） | 提取可测试谓词并修正分组（presets+templates+**locales**）；正反例单测；二进制按扩展名白名单不进文本扫描 | 89fbcb76 |
| R-13 macOS 手工验收 | 属实（离屏不可证） | `docs/mg_macos_manual_qa_checklist.md`：10 次应用切换、面板恢复、窄/宽+中/英截图、DevTools 单步撤销、码率抽查、用户模板迁移、重启写入卫生、干净机器清单 | c39ec7af |

### 已知的诚实边界

- R-08 剩余 37 处用户可见中文消息（多为多步 AI 流程叙述文案）未迁移，基线已冻结防增长。
- R-06 preset 侧与模板侧的单一 PackageStore 接口统一、R-07 的 Source/DevTools 草稿所有权 UI（当前为既有 MutationObserver 未保存提示）留作后续。
- R-13 第 1–2 节（窗口置前/应用切换）只能人工执行，本机自动化无法替代。
- composite `test_interaction_hint...` 段错误为预存在问题（改动前后同样复现），已按 GL 能力 skip 显式化，根因排查（ASan）仍待做。
