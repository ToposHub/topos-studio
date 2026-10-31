# 第四轮最终嵌套音频导出门禁

日期：2026-10-16。范围仅限嵌套音频展开、交付预检、Stem 现有入口接线及 focused 回归。

## 根因与结果

原 `flatten_nested_audio_tracks` 对父 Nest 变速、倒放、速度曲线仅 warning 后跳过；循环、深度、缺 child 也会直接返回。`expand_sequence_audio_for_render` 与 Stem 的宽 catch 又将展开失败回退为原音轨，可能产出视频成功但嵌套音频缺失。

增加纯数据 `NestedAudioDiagnostic(code, clip_id, child_timeline_id, message)`、可序列化 `to_dict()`，以及保留 `.diagnostics` 的 `NestedAudioExpansionError`。两个展开函数保留默认 preview 降级语义，可通过 `diagnostics` 列表读取结构诊断；新增 keyword-only `strict`、`frame_range`（父时间线半开区间）。

严格模式拒绝父 Nest 的变速、倒放、速度曲线、冻结/非单位源时钟、分数源相位、父子序列 mixed FPS、循环、超深度、缺 child identity/sequence/resolver。静音轨、禁用轨/clip 以及本次导出范围外 Nest 不触发门禁。递归展开不再先静默剔除循环，诊断携带嵌套实例命名空间。

普通/直通视频交付在任何 encoder 创建前执行严格音频展开。失败写入 `ExportPreflightResult(ok=False, blocking=..., blockers=diagnostics)` 并抛错；不会启动成功 manifest 写入。`include_audio=False` 完全不展开音频，允许视频交付；既有 GIF 无音轨策略保持。音频 DSP 预检使用展开后的轨集合，覆盖子轨依赖。Master、音频 worker、直通音频准备、Stem PCM 和整组 Stem 服务也执行严格展开，领域异常不再被降级为无音轨成功。

`export_manifest.py` 的既有 preflight 段追加 `blockers` 的结构化序列化，消息视图保持兼容。

## Stem 真实调用链

File → `FileOpsMixin.export_stem_tracks` 与 Deliver → `_export_stems` 均复制当前 sequence 字典，再传其 `.get`，避免 worker 读取实时 controller 状态。Dialog 规划包括正常展开的虚拟音轨；Worker 捕获素材字典 lookup，经严格 `export_all_stems` 在规划/WAV writer 创建前校验。异常通过既有 `failed` 信号提示 clip/child 身份，Worker 与 Dialog 的 `failure_diagnostics` 保留结构数据，不发成功结果。不新增交互界面。

## 验证

新增 `tests/edit/test_round4_nested_audio_gate.py` 共 26 项，覆盖 preview 结构诊断与 strict 一致性、retime/倒放/曲线、mixed FPS/循环/缺 child/缺 resolver/深度、静音禁用与范围排除、真实 `export_timeline` 预检与未创建编码器/未写成功 manifest、manifest JSON 结构、`include_audio=False` 经完整 mock 编码器导出成功、custom range、独立 master/worker/direct/Stem 服务失败，以及 File/Deliver snapshot 接线和真实 Dialog/Worker 失败传播、正常 Nest 虚拟轨规划。

回归命令：

```bash
.venv/bin/python -m pytest tests/edit/test_round4_nested_audio_gate.py tests/edit/test_stem_export_dialog.py tests/edit/test_nested_audio_flatten.py tests/edit/test_round4_nested_sampling.py tests/edit/test_stem_export.py tests/edit/test_audio_dsp_timeline_export.py tests/edit/test_timeline_export.py -q --disable-warnings --maxfail=3
```

结果：**135 passed, 5 skipped，12.15 秒**。五项 skipped 为既有 timeline export 用例。`git diff --check`（本次范围）通过。既有 preview 兼容测试及真实素材嵌套 PCM 回归保留并通过。

## 边界

本轮仅建立明确失败门禁，不实现 F07 成熟重采样、不增加新 UI、不修改既有未提交成果，也不提交 Git。preview 仍可降级；严格交付不再把已知不支持的嵌套映射伪装成成功。

## 最终补检：Dialog 初始化规划门

补检发现 Dialog 的规划阶段仍使用默认 preview 展开，Nest-only 不支持映射可能在用户点击导出前就被降级，显示无音轨/空规划而非具体原因。现将 Dialog 规划改为 `strict=True`：捕获 `NestedAudioExpansionError` 保存 `failure_diagnostics`，在既有状态区显示 clip/child 阻断原因并禁用导出；普通规划异常同样明确显示失败，不再冒充“没有音频轨”。`_start_export` 的程序触发也被规划错误直接阻断，不创建 worker，不写输出。正常 Nest 虚拟轨规划保持。

新增真实 qapp 初始化测试两项，覆盖 Nest-only 父 speed=2x 与通用规划错误：显示状态可见、按钮禁用、无误导无音轨文案、程序触发无 worker/无输出；既有 worker 失败测试继续保留。定向回归：

```bash
.venv/bin/python -m pytest tests/edit/test_round4_nested_audio_gate.py tests/edit/test_stem_export_dialog.py -q --disable-warnings --maxfail=3
```

结果：**39 passed，2.95 秒**；本次增补范围 `git diff --check` 通过。本增补只修改 Dialog、新增 focused 测试和本报告，未提交 Git。
