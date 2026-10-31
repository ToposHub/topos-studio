# 色轮重设计验证（2026-10-31）

- 达芬奇参考图取样：屏幕逆时针角度 0°=蓝、60°=洋红、120°=红、180°=黄、240°=绿、300°=青。
- 色环、鼠标坐标与后端 HSV 使用同一方向，默认红色屏幕角度 120°。
- 数值框复用全局 DragValue 样式，仅添加独立色条；色温橙→蓝青，色调绿→洋红。
- 九项原有参数与新增高光保持原有模型语义；饱和度/色相中性显示50，对比度中性显示1。未新增后端尚无实现的轴心、亮度混合或第四个偏移轮。
- 输入采用相对二维拖动，数值可拖动/双击输入，中键重置；已验证信号提交和切栈保存。
- 回归：192 passed；最终全局样式调整后针对 UI、高光和配置迁移重跑 22 passed。
- 真实视频：`tests/test_videos/2K_h264.mp4`，帧0、30、60，原始1920×1080。三轮×六色×三帧=54次生产OpenGL渲染，全部通道变化方向正确，与CPU权威结果最大差1个8-bit色阶。GPU资源在单独工作线程内创建、使用与释放。
- 保留Topos原有调色算法，未宣称与Resolve的数值算法或拖动灵敏度完全一致。
- 按用户后续澄清，在Lift/Gamma/Gain各轮数值框下方恢复独立的横向主亮度拨盘。拨盘与主亮度数值双向同步，仅调本轮亮度；其他参数继续纯数值。左右相对拖动，双击/中键仅重置亮度，Ctrl/⌘微调、Shift粗调，并支持滚轮和键盘。
- 拨盘补充验证：66项回归通过，包含三轮独立交互、双向同步、无信号程序回写、单手势一次保存及预览。原视频第0帧原始1920×1080验证三轮各±0.25亮度共6次生产GPU渲染，明暗方向全部正确，GPU与CPU最大差1个8-bit色阶。结果见brightness_validation.json和real_video_brightness.jpg。

复跑真实视频验证（项目根目录）：

```sh
PYTHONPATH=. QT_QPA_PLATFORM=offscreen .venv/bin/python .testrun/primary_wheel_redesign/validate_video.py
```

`panel.png` 为应用全局主题下的最终面板。`real_video_directions.jpg` 从左到右为原片、向红、向绿、向蓝调整；从上到下为Lift、Gamma、Gain。

复跑独立主亮度拨盘素材验证：

```sh
PYTHONPATH=. QT_QPA_PLATFORM=offscreen .venv/bin/python .testrun/primary_wheel_redesign/validate_brightness.py
```
