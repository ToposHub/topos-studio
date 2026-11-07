# 节点 UUID 标签、调色动画拖动闪烁与解码日志修复

2026-11-07。针对用户截图和真实日志复现并修复，保留此前工作区变更。

## 根因与修复

| 现象 / 日志 | 根因 | 本次修复 |
| --- | --- | --- |
| 节点标签过一会变成 UUID | 新建 Corrector 没有 label 时，生产 Layer DTO 使用 node_id 作为 name，保存一致化再把该 name 回填 label。重命名与旧 Layer name 也可能不同步。 | 默认名称不再取内部 ID；UI/DTO 对精确匹配 node_id 或源 Layer ID 的 UUID 旧错误名作显示修复，保留其他自定义名字；重命名/清空同步旧 Layer name，并允许空名字在保存时保持为空。 |
| `grading_params 结构非法（mappingproxy），忽略` | 工程快照为只读 Mapping；旧加载器只接受 dict，忽略了本来有效的参数。嵌套插件、曲线与窗口也有同类可变性问题。 | `Layer.from_dict` 接受 Mapping，并递归复制为独立可变容器；不修改工程快照，不丢 HDR 值、曲线或嵌套窗口。 |
| `CMBlockBufferCreateWithMemoryBlock failed: -12704` | 真机复现为 PyAV EOF flush 包（0 字节）被当作压缩视频样本提交。CoreMedia 报长度错误，重复 demux EOF 会重复触发。 | demux 排除空包，不占真实 packet 限额/帧索引；VT submit 再拒绝空数据。非空坏样本仍记录真实错误，未屏蔽 CoreMedia 日志。 |
| `GLError 1282` 在 `glGetIntegerv(GL_FRAMEBUFFER_BINDING)` 报出 | 首发错误实际来自前面的 ModernGL FBO 创建：临时 FBO use→release 后，缓存绑定仍指向已删除 GL 名称，下一次创建 FBO 恢复它时产生 invalid operation。PyOpenGL 查询只是后续检查点。 | 临时目标用统一作用域，在创建前修复失效绑定、释放前恢复有效目标；恢复 viewport 及 scissor 的实际启用状态，避免错误开启 1×1 裁剪。修正 Corrector、source/output copy、key/Outside、GL 辅助窗口与预览拷贝。没有清除 glGetError 来掩盖问题。 |
| `TextureLease ... force release from WRITING` | 渲染异常提前退出，主帧没有完成发布，因此清理仍处于 WRITING 的 slot。这是上游错误的后果。 | 修复 FBO 根因；可选快照的分配、绘制和回读都放在隔离边界内，快照失败不再让主帧提前中断。快照纹理/FBO、program/VAO 成对分配，失败释放并可在下一帧恢复。 |
| 拖动时交替出现旧调色 | 图命令已绕过旧栈保存，但预览 worker 持有自己的工程快照，没有收到新的图。旧缓存仍可能参与 seek。 | 图发布后轻量更新 Edit / Color 两套 timeline renderer 的快照并失效旧 generation/缓存，同时失效视频渲染参数并请求当前 Color 帧；复用现有 update_color_stacks，不重开解码器。 |

## 验证

- [扩大相关回归](../../validation/color_keyframes_2026-11-07_flicker_fix/regression.txt)：**1660 passed，1 skipped，91.46 秒**。涵盖图执行、保存、UI 宿主、关键帧、GL/Metal 既有出口、VideoToolbox 与 present 协议。
- [最后的失败恢复回归](../../validation/color_keyframes_2026-11-07_flicker_fix/fbo_failure_recovery.txt)：**42 passed**。包含快照分配失败后下一帧恢复、viewport/scissor 状态、连续帧及资源清理。
- [既有 packet 限额与 UI 回归](../../validation/color_keyframes_2026-11-07_flicker_fix/vt_shared_and_ui.txt)：**66 passed，1 skipped**。旧 packet 测试的假样本改成真实非空背存，空包仅代表 EOF。
- 名称/Mapping 定向组 199 项通过；VT 定向组 94 项通过，均包含在上方扩大矩阵中，不将重叠用例相加。
- 静态编译与 `git diff --check` 通过。

## 真实素材 GPU 预览

复用 `tests/test_videos/测试素材01.mov`，1920×1080、25 fps、124 帧。使用 Cocoa MainWindow、正常素材导入与工程命令、实际滑块拖动及撤销，再建立曝光动画。显式装配真实 Timeline GPU 预览，并用 snapshot 回读像素。

| 用例 | 往返时间线帧 | 结果 |
| --- | --- | --- |
| [普通速度](../../validation/color_keyframes_2026-11-07_flicker_fix/native_normal.json) | 0 → 41 → 82 → 123 → 82 → 41 → 0 | 同一帧再次生成的像素 SHA-256 完全一致；渲染错误为空；11.50 秒。 |
| [2× 速度](../../validation/color_keyframes_2026-11-07_flicker_fix/native_2x.json) | 0 → 21 → 41 → 61 → 41 → 21 → 0 | 同一帧再次生成的像素 SHA-256 完全一致；源帧映射保持一致；渲染错误为空；13.28 秒。 |

两次原生日志都未出现 `-12704`、`GLError`、`mappingproxy` 忽略或 WRITING 强制释放。此验证为实际 Timeline GPU snapshot，不能据此宣称所有色彩后端/素材组合都已验证。

可复跑：

```sh
QT_QPA_PLATFORM=cocoa TOPOS_FFMPEG_HOME=/path/to/ffmpeg/bin \
  .venv/bin/python scripts/validate_color_keyframes_media.py \
  tests/test_videos/测试素材01.mov --scrub-check --output /tmp/keyframes-scrub
# 加 --speed 2 验证变速；使用临时配置、新建工程，不修改已有工程。
```

## 使用

已运行的应用进程不会重新加载 Python 修改。重新启动项目应用后再打开工程验证；已有错误 UUID 标签在界面投影中会被过滤，自定义名称保留。无需删除工程、素材或缓存来应用此次修复。
