# N16 Edit 单进程回归（2026-11-06）

## 基线

命令：

```bash
QT_QPA_PLATFORM=offscreen PYTHONFAULTHANDLER=1 \
  .venv/bin/python -m pytest tests/edit -q
```

原始结果：10 failed、11,866 passed、42 skipped，耗时 1,104.13 秒；完整
traceback 见 `baseline_failed.log`。失败分为：转场静态/动画预览资源契约、
Color Dock 迁移测试旧断言、畸形效果规格隔离、未安装的可选 AI 包、
TTS 空音频报错顺序、Qualifier 显式降级开关旧测试，以及 Metal
fractional preblur 的 CPU 对拍。

## 修复与聚焦验证

- 给 18 个尚无单独 SVG/GIF 的 Library 1 转场提供通用静态图标；动画预览
  保持可选，测试按实际存在的 GIF 计数。
- 在 `pyproject.toml` 明确打包 Edit 面板的图标与 GIF。无隔离构建 wheel
  后检查得到 21 个效果 SVG、38 个转场 SVG（含通用图标）、35 个 GIF。
- 规格注册时将 `internal` 读取纳入逐项异常隔离；TTS 在确认所有 cue
  都失败后才加载可选音频依赖。
- Color Dock 测试按当前 410px 上限计算；Qualifier 的旧测试显式开启
  `allow_degraded_grade`；未安装的 `qwen_asr` 用例按可选依赖跳过。
- Metal 边缘检测预模糊保留 float32 中间标量，直到 Sobel 输出才量化。
  随机画面的 CPU/Metal 差异约束为平均不超过 0.4、单像素不超过 2 个
  8 位码值，保留不同浮点运算顺序造成的舍入空间。

聚焦的失败用例及 `tests/edit/test_timeline_metal_export_renderer.py` 已通过；
后者为 107 passed、1 skipped。修复后的同进程 Edit 全套复跑为
**11,875 passed、43 skipped**，耗时 1,079.94 秒；完整输出见
`edit_passed.log`。跳过项包括本机未安装的可选 `qwen_asr`。

## 五套件同进程首轮

`tests/color tests/app tests/ui tests/shared tests/edit` 收集 22,402 项；
首轮为 4 failed、22,336 passed、63 skipped、1 teardown error，耗时
1,447.62 秒，见 `combined_first_run.log`。其中两项 AI 回放测试继承了前面
套件写入的全局渲染诊断，非空返回的诊断码在传输协议中是
`{value, trust, source}` 对象，旧测试 helper 把它当作可哈希字符串；
已给回放用例隔离诊断 sink。另两项变体时长测试按中文文案查找标签，
前面套件将 UI locale 切换为英文后返回空字符串；现在给时长标签稳定的
`objectName`，测试按控件身份查找。

最后的 teardown error 是本轮运行期间编辑了计划表文件，触发 Color
测试的「工作区不可在运行中变化」门禁；这不是应用异常。下一轮同进程
运行期间不修改仓库文件。上述跨套件前序的 48 项聚焦回归已通过。

## 五套件同进程第二轮

保持仓库文件不变，重跑同一命令，结果 **22,340 passed、63 skipped、
7 warnings**，耗时 1,534.85 秒、退出码 0；见 `combined_passed.log`。
首轮的四项失败与最后的洁净门禁错误均未复现。历史 macOS 间歇性 Qt
原生段错误在本轮未出现，但这不构成根因已经完全消除的证明。

通过后补做两项低风险清理：LUT HSV 饱和度用掩码除法避免 `np.where`
预先求值造成的除零告警；两项 mock GPU 导入测试在断言所有权之后
显式释放帧。相关 5 项聚焦测试以 `-W error::RuntimeWarning` 通过，
这两项收尾未再运行五套件全量。
