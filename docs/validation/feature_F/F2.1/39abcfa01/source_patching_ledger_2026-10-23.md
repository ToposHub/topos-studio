# F2.1 逐源流/声道 Source Patching — 证据台账

- 日期：2026-10-23（执行于 2026-10-22 计划）
- 结论：**已完成（常用配置范围）**；完整 patch matrix（逐源声道→逐目标轨分轨，
  如 ch0→A1/ch3→A3）与 timeline_panel 入口登记为后续片（见 §5）。
- 提交链（feat/f-plan，均已 ff-merge fix-ui 并推送）：
  - `1329aaa3f` s1 模型片：SourcePatchSpec（V/A 流开关 + 源声道→目标轨内通道
    映射，校验/解析/dict roundtrip）+ EditProject.source_patch_overrides 不可变
    with 方法 + 13 项模型测试。
  - `305455a63` s2 命令+序列化片：UpdateSourcePatchCommand（项目域，apply 即按
    已知声道数校验，undo 快照恢复，None=清除，dict 往返）+ settings 落盘/读回
    （未知载荷原样保留重存不丢，运行时回退直通）+ patch_for_asset 类型加固。
  - `d8a814f72` s3 渲染消费片：模型级 select_channels 单点 → 预览 mix_samples
    选道（decoder._source_channel_map 注入）/ 源监视器 scrub 同映射 /
    成片 _render_audio_master 交付适配前选道 / Stem decode 回调三条返回路径
    统一选道 + 插入/覆盖/追加/拖放四门并入 V/A 开关。
  - `ed4e8e667` s4+s5 UI 片 + R9：SourcePatchDialog 常用配置（直通/L+R/单声道
    ITU 标签/对调/自定义保留 + 混合值提示 + 双关警告）+ SourceViewerBar V/A
    入口 + EditPage 桥接（等价默认提交为清除覆盖）+
    MediaPoolPanel.apply_source_patch 命令入口；R9 跨序列粘贴轨道重映射
    （复制侧记录类型/同类型序号/角色，粘贴侧同 id 优先否则按结构位置解析，
    缺类型只跳该条不整批丢失，锁定查目标轨）。
  - `39abcfa01` s6 入队预检片：_check_source_patch 越界映射入队前 BLOCKING
    （与命令 apply 同谓词）。

## 1. 范围与验收对照（计划 §F2.1）

| 验收要求 | 落地 | 证据 |
|---|---|---|
| 多声道素材只把指定声道送目标轨 | select_channels 于预览/成片/Stem 三路消费；真实 6ch WAV × 真实解码器实测只剩指定声道电平 | tests/edit/test_source_patch_consumption.py::TestExportHonoursChannelMap（2 项，PyAV 写真实 5.1 WAV）|
| video-only/audio-only 不带入其他流 | 四门（insert/overwrite/append/拖放）按 patch 分派 | 同文件 ::TestInsertGatesHonourPatch（6 项）|
| 声道布局 | 选道后不再向下游声明容器布局（自定义布局）；无映射走 BS.775 标准下混 | 消费测试 + timeline_export/stem_export 实现 |
| 时长 | 插入路径复用既有 fps 时基（未改 duration 语义），门测试覆盖 | TestInsertGatesHonourPatch |
| 单次 Undo/Redo | UpdateSourcePatchCommand 项目域单步；面板 undo 恢复 | tests/edit/test_source_patch_model.py::TestUpdateSourcePatchCommand（6 项）+ tests/edit/test_source_patch_ui.py::TestMediaPoolApplySourcePatch::test_undo_restores_previous_state |
| 保存/导出一致 | settings 落盘/读回 roundtrip + 未知载荷保全；导出/Stem 与预览同 select_channels | test_source_patch_model.py::TestSourcePatchPersistence（2 项）|
| 缺流/冲突明确反馈 | 双关拒绝（插入/覆盖/追加/拖放）+ audio-only 无目标轨明确报错 + 对话框双关警告 + 入队预检 BLOCKING + 渲染期映射跳过留警告 | 四门测试 + 对话框测试 + tests/deliver/test_source_patch_preflight.py（6 项）|
| 映射随用户选择持久化（非 UI 临时变量） | overrides 挂 EditProject，经命令提交、随工程保存；等价默认提交为清除覆盖 | s2/s4 实现 + Persistence 测试 |

## 2. 语义决策记录

1. **映射粒度**：源声道 → 目标轨内通道（非逐轨矩阵）。计划"先支持选定单声道/
   立体声常用配置"据此落地：5.1 只送 L/R、单选 C（源 2）做 ADR 对轨均已可用。
2. **布局漂移防御**：select_channels 在实际解码声道数不足以覆盖映射基线时返回
   原对象（调用方识别为"未应用"），退直通；导出侧留警告，预览侧静默回退——
   播放/导出不因环境布局漂移中断。
3. **选道后布局身份**：应用映射后 adapt_channels/adapt_samples_for_delivery 的
   src_layout 置 None（自定义布局按声道数推断），不得再声明容器布局。
4. **命令域归属**：UpdateSourcePatchCommand 为项目域命令（同
   UpdateAssetLogCommand），不登记进时间线宏子命令注册表
   （`_MACRO_SUBCOMMAND_TYPES` 注释明确 EditProject 级命令由
   ProjectMacroCommand 编组，混入时间线宏会在 apply 阶段崩溃）。
5. **等价默认 = 清除覆盖**：EditPage 桥接把与 DEFAULT_SOURCE_PATCH 等价的
   设置提交为 None，避免存无意义覆盖条目。
6. **R9 修复**：跨序列粘贴此前按原 track_id 查目标轨 → 不同序列 id 必不同 →
   整批静默跳过。现复制侧记录 (track_type, 同类型序号, track_role)，粘贴侧
   同 id 优先、否则按结构位置解析；图形轨按角色优先匹配；锁定检查查目标轨。

## 3. 测试账本

| 文件 | 覆盖 | 结果 |
|---|---|---|
| tests/edit/test_source_patch_model.py | spec 校验/解析/roundtrip/选道 + 命令 apply/undo/校验/roundtrip + 持久化 roundtrip/未知载荷 | 16 passed |
| tests/edit/test_source_patch_consumption.py | select_channels 模型 3 + mixer 实测 3 + 真实 WAV 导出/Stem 2 + 四门 6 + R9 跨序列 2 | 16 passed |
| tests/edit/test_source_patch_ui.py | 对话框预设回显/构造/自定义保留/混合值提示/双关警告/无音频降级 7 + 面板提交/undo/拒绝/清除 5 | 12 passed |
| tests/deliver/test_source_patch_preflight.py | 越界 BLOCKING/合法放行/未知声道数/纯视频/未引用素材 | 6 passed |
| 护栏（未回归） | 插入门 R22/F-R03 系列 77、音频/Stem/export parity 98+250、paste/menu 17+56、preflight 31 | 全绿 |

环境：`QT_QPA_PLATFORM=offscreen TOPOS_HEADLESS_GL=1 .venv/bin/python -m pytest -q`。

## 4. B 依赖核对

无 Plan B（Windows 性能线）依赖：全部切片落在共享模型/命令/序列化与
macOS 亦适用的预览/导出/UI 路径；未触碰 Windows 专属渲染/性能代码。
（对照 F0.1 manifest：F2.1 标记为非 B 依赖任务。）

## 5. 登记的后续片（不阻塞本任务验收）

1. **完整 patch matrix**：逐源声道→逐目标轨（ch→A1/A3 分轨）需要轨道级映射
   模型扩展（当前为轨内通道映射）；建议随 F2.5 多机位音频策略一并设计。
2. **timeline_panel 入口**：轨道头 patch 引脚 UI（DaVinci 形态）；当前入口为
   源监视器 V/A 按钮（计划入口三选一已满足）。
3. **混合值提示增强**：对话框提示为文本级；轨头/检视器内联图形化提示后续随
   timeline_panel 入口做。
4. **file_controller.open_asset 映射透传**：媒体池双击打开源监视器路径暂未带
   map（EditPage 内打开路径已带）；影响面=scrub 试听声道选择，不影响时间线。
