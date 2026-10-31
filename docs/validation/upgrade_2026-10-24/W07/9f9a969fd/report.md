# W07 · 共享正确性复验和跨层 CI（Windows 侧）—— 验证报告

- 基线提交：9f9a969fd（W10 收口）
- 机器：B（Windows 11 Pro 26200 / RTX 4090 / Python 3.13.12）
- 对应计划：W07（P0，发布前），S01-S12/O02/W-I06 + R01-R12 适用子项

## 1. Windows logic CI 收口（核心正确性 required，不落 continue-on-error）

`windows-logic.yml`（无 GPU runner）纳入 W02-W06/W10 全部新 L1：

- test_windows_cuda_resident.py（L2 真机类在无适配器 runner 诚实 skip）
- test_cuda_resident_lifecycle_w02.py（生命周期/回滚/池预算）
- test_cuda_resident_capability_w04.py（设备对齐四路径/几何门/指针门）
- test_cuda_extras_w06.py（extra 声明/打包收集/能力报告）
- test_resident_failure_recovery_w03.py（按素材降级恢复；渲染器类经
  importorskip 在无 Qt 环境诚实跳过——QApplication 按需内建，不依赖
  pytest-qt 插件）
- test_project_archive_w10.py（跨机归档 Windows 适配）

触发路径补齐对应源文件与测试文件；`pr-fast-gate` 的
continue-on-error `known-regressions` 域经本包清理大幅缩红（见 §3）。

## 2. 真实产品缺陷修复（红基线清理中发现，跨平台生效）

| 缺陷 | 位置 | 影响 |
|---|---|---|
| `SafePathPolicy` 混合分隔符前缀：根串硬补 "/"，Windows 根串以 "\" 结尾 → `_is_within` 对**所有**根内路径判负 | ai_editing/safe_path_policy.py | 授权根路径策略在 Windows 整体不可用（fail-closed 但全拒）→ 改 `Path.relative_to` |
| `InsertLinkedAssetRippleCommand`/`OverwriteClipCommand`/`AppendClipCommand` 的 apply() 引用未声明的 `_source_pts_index` | edit/core/commands.py | 三大编辑命令（A/V 波纹插入/覆盖/追加）apply 即 AttributeError——pts 时间基特性合入后路径完全不可用 |
| `overwrite_clip_at_playhead`/`append_clip_to_timeline` 不收 `source_pts_index`（调用方传了） | edit/core/timeline_ops.py | TypeError；append 还整体丢弃 fps/pts 时间基快照（混合帧率追加回退默认时基） |
| 导出层 `layer.clip` 无防御访问 | edit/core/timeline_export.py | 最小 ClipLayerInfo 契约消费者（遮挡分析 seam）全炸 |
| `_pbo_rgba_to_qimage` Qt 快路径不验缓冲长度 | edit/ui/program_viewer.py | 短缓冲包装 = 越界读 UB（Windows 不抛异常"成功"返回垃圾图）→ 先验 bytesPerLine×height |
| Windows 无 `AF_UNIX` 时 `AttributeError` 裸穿 CLI 契约 | ai_editing/mcp_local_ipc.py | run_cli 的 OSError/ConnectionError except 域接不住 → 改抛 ConnectionError（与 POSIX 连接失败同分支：exit 1 + 结构化诊断） |
| `url_to_local_path` 返回非原生分隔符路径 | adapters/import_builder.py | `get_asset_by_path` 字符串去重漏判（斜杠风格错配）→ normpath |
| 随附 LUT 从未入库（空目录） | luts/cinematic.cube、07.cube | `apply_lut` 工程根解析契约"随附 LUT 可命中"在干净 checkout 不成立 → 补最小有效 .cube 并入库 |
| `make_fingerprint` 路径规范化 Path/str 不一致 | ai_editing/provider_resume.py | 同一路径两种写法不同指纹（OS-09 幂等去重失效）→ 统一 "/" 规范形 |

## 3. 红基线清理（tests/edit：隔离态 76 → 0；审计口径恢复）

2026-09-20 审计曾达 tests/edit 8328 绿 0 败（macOS）。本机隔离态基线
76 败（39 文件）。逐一归因修复（W01 纪律：产品缺陷修产品、平台差异
改测试平台门、过时断言订正契约并注明）：

- **产品缺陷 9 项**（§2）；
- **测试 seam 过时**（产品演进而测试未跟）：pts 命令字段、trace_id
  透传、G14 撤销 AUD-18404 拒绝、R10 动画路由劫持 MagicMock、
  `_apply_source_edit_plan` 未绑定、AUD-17418 的 field 已有合法使用
  （改 AST 级未用 import 检查）；
- **Windows 平台差异**：POSIX basename（rsplit("/")→os.sep）、
  chmod 只读不约束属主（注入式 PermissionError 模拟同契约）、
  POSIX 权限位断言（改验 chmod 调用留痕）、`/proc` 不可写技巧
  （改"以文件为父目录"跨平台）、Py3.13 ntpath.isabs("/")=False
  （按平台构造绝对路径）、QtWebEngine 需先于 QCoreApplication 设
  AA_ShareOpenGLContexts、av 帧缓冲滞后持锁（释放+gc+重试删除）、
  Windows 字体度量（弹窗宽度严格 > 改容纳性不变量）。

**结果**：39 个原失败文件全部复绿（隔离态/小组态 0 失败；10 文件批
217 绿等证据）。全组单进程长跑终态：**11151 passed / 11 failed**（起点
全组 92 failed）——余 11 例集中在 7 个文件，均为顺序污染受害：受害文件
单独跑、两两配对跑全绿（含 CJK 字幕用例的 6 组 pairwise 消融），仅
7 文件以上批次复现（累积型 Qt/全局状态污染；与 2026-09-20 审计
P1-EDIT-01 记录同性质，归 P2-TEST-01 随机顺序 CI 域跟踪，非本包新增、
非本包引入——基线期同样存在）。

## 4. 附加验收项复验（真机）

- 输出/导出事务族：tests/export/ 全组 + topos alpha/timeline export
  151 绿（含 open/flush/rename 失败保旧成片的 W01 契约用例）；
- 文件锁下取消/删除：W01 msvcrt 活性锁契约 + W10 文件锁降级用例；
- RGBA 透明/时间线导出对拍：tests/media/test_topos_alpha_pipeline.py
  + test_topos_timeline_export.py 绿；
- 工程保存重开：test_edit_project_io_root_bin 2 绿 + W10 归档重开；
- 三条黄金工程（真实 Windows 窗口保存重开/Undo/最终像素/PCM）：
  黄金工程由 M 计划（Mac 集成者）产出共享，**本仓库尚未接收 M 提交**
  ——按 §0 协作规则登记"待接收后跑真实生产消费者"，不冒充已验。

## 5. S01-S12 接收状态（如实登记）

S01-S12/R01-R14 的修复由 Mac 主责实施、Windows 接收复验。当前 feature
分支未含对应 M 提交——全部登记为"待接收"。Windows 侧已就绪：全量
逻辑 CI（§1）+ 本机真机矩阵（W02-W06/W10 各报告）可在接收后立即执行
分发验收。

## 6. 回归

- tests/edit 隔离/小组态：原 76 败 → 0（10 文件批 217 绿等证据）；
- 全组单进程终态（L3 三文件除外）：11151 绿 / 11 败（全部顺序污染
  受害、pairwise 消融证明非稳定失败；见 §3）；
- tests/export + 相关 media：151 绿；ai_editing 全组 2117 绿 7 skip；
- W02-W06/W10 既有套件不回归（media 全组在 W06 后 3412 绿 0 败）。

## 已知限制与如实登记

- 跨文件顺序污染（全组单进程 ~11 例受害、隔离全绿）：存量隔离债，
  P2-TEST-01 随机顺序 CI 跟踪；
- 黄金工程三件套与 S01-S12：待 Mac M 提交（§4/§5）；
- `luts/` 两个最小 .cube 是占位实现（路径解析契约用）；如原始随附
  LUT 有既定内容，Mac 侧可替换（文件在库中跟踪，不再依赖本地未提交
  文件）。
