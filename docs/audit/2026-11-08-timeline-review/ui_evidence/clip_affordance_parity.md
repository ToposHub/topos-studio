# 片段淡变、端点修剪与滚动修剪对齐

2026-11-08，按用户本轮要求直接实施UI/交互。保留片段配色、时间线电平表和既有命令提交边界，不修改core、内容schema、预览/导出算法。

## 实测参考

直接使用本机 **DaVinci Resolve Studio 20 / Edit** 的已有测试工程观察：

- 视频与音频淡变手柄均为贴顶的白色窄圆角标签；拖动后标签跟随淡变终点，图像/波形区域出现渐变斜线。不是常显的大三角按钮。
- 单击片段侧边，端点以绿色细全高边与短向内端帽表示。单击片段整体与选中编辑点是不同状态。
- 共用接缝可选中两侧编辑点，使用滚动修剪。原生观察/试验仅在测试工程中操作；两个测试淡变均已Undo，最终恢复A模式并取消选择，没有保存工程。

官方依据：[Resolve 20 Beginner's Guide](https://documents.blackmagicdesign.com/UserManuals/DaVinci-Resolve-20-Beginners-Guide.pdf)第81页说明A/T均可滚动修剪，共用接缝同时修剪两段；[官方 Edit 页](https://www.blackmagicdesign.com/products/davinciresolve/edit)说明T按鼠标位置选择ripple/roll/slip/slide。键盘微调参照[Resolve 20 Colorist Guide](https://documents.blackmagicdesign.com/UserManuals/DaVinci-Resolve-20-Colorist-Guide.pdf)第161页的comma/period逐帧修剪，以及[Resolve 20 Fairlight Guide](https://documents.blackmagicdesign.com/UserManuals/DaVinci-Resolve-20-Fairlight-Audio-Post.pdf)第68页Shift组合多帧步进。此处只引用对应规则，不声称已逐页审读全部手册。

## 本轮修复

| 操作 | 已实施行为与外观 | 内容提交 |
|---|---|---|
| 淡入/淡出 | 6×9逻辑像素白色圆角顶标签、细白色斜线/半透明区域；斜线止于名称条上方，不盖文件名 | 复用ChangeClipFadeCommand；拖动只改预览，release一次提交 |
| 淡变命中 | 顶部13px控制带与7px半径；宽24px以上小片段也可操作；A/T均可拖动；两个淡变端点相遇时左右半区分别可抓 | 不改fade duration字段语义或混音/导出曲线 |
| 首/尾长度 | 两像素绿色全高括号与短端帽，窄幅低透明度高亮；替换10–15px大色块和中间箭头 | A为既有普通trim；T边缘为既有ripple trim |
| 编辑点选中 | 按下端点即保留该侧括号；hover离开仍保留；点击片段主体或清空选择退出编辑点；linked侧同样标识 | 编辑点是view transient state，不写工程、不产生history |
| 接缝滚动 | 接缝中央独立细命中区、双竖边和双向滚动光标；选中两侧绿色括号；顶部让给fade tab | 复用linked_roll完整预检及命令；两段总长不变；Option临时独立 |
| 鼠标光标 | trim in/out、ripple in/out、roll、slip、slide、fade八种不同黑白矢量；2x渲染、准确热点 | UI层缓存；无GPU调用；不再以通用系统resize代替滚动工具 |
| 键盘微调 | 已选编辑点`,`/`.`按1帧调整对应端点/接缝；Shift按5帧；识别macOS实际发送的`<`/`>`键值 | 复用numeric trim/linked roll；不在鼠标拖动过程中插入第二条键盘提交 |
| Retime交叠 | 打开Retime时不保留被控制条遮住的fade命中区；关闭按钮、底部速度菜单光标与动作一致 | 不影响已有速度命令 |
| 取消与撤销 | fade/trim/roll的Esc与迟到release零提交；正常完成单步Undo；真正切窗仍取消 | 不放宽状态锁/轨道锁/源窗检查 |

独立矢量实现位于`src/features/edit/ui/timeline_edit_affordances.py`，主集成为`timeline_items.py`和`timeline_view.py`。图形为本地几何重绘，未提取/复制达芬奇专有资源，也没有全系统逐像素等同的声明。

## 验证与边界

- 组合回归：**376 passed / 26.77s**，包含新的手柄/命中/括号测试和既有trim、roll、slip/slide、fade、host拖动、锁、恢复及Undo用例。随后增加鼠标/键盘并发保护、Retime隐藏命中与macOS实际Shift标点回归，另跑专项，不重复累计。
- 真实macOS素材窗口：白色顶标签可拖，淡入斜线终点跟随；片尾点击留下绿色单边括号；接缝点击留下双边括号；拖动接缝后A/V共同改边，Undo恢复；Esc清空编辑点。
- 键盘原生QA捕获了QTest未覆盖的差异：macOS物理Shift+period生成`Key_Greater`，已归一到相同多帧操作；不只验证合成的Key_Period+Shift。
- UI像素测试验证标签上下近等宽（非三角）、名称条未被淡变覆盖、绿色编辑边没有扩成宽块、光标图像无裁剪且彼此不同。真实QTest验证顶部fade/下部trim/中央roll互不抢事件、短片段、Option、零变化、取消和单步Undo。
- **后端边界继续有效**：自由淡变曲线/曲率控制点尚无持久模型与音视频消费者，见新计划B16；速度点拖动/高级速度设置等见B14/B15/B17/B18。未把仅画曲线或发信号当作实际音视频处理；也未因此声称“所有片段操作100%等同Resolve”。

可复核的新增/调整测试：`test_timeline_clip_affordance_gestures.py`、`test_timeline_edit_affordances.py`、`test_clip_item_trim_preview.py`；旧键盘trim测试的fake宿主补齐已有view接口，保留其命令与undo断言。

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q tests/edit/test_timeline_clip_affordance_gestures.py tests/edit/test_timeline_edit_affordances.py tests/edit/test_trim_professional.py tests/edit/test_clip_item_trim_preview.py
```

补充B19：复现linked数值trim在源余量10/3时分别提交5/3的后端问题，UI的键盘和数值窗口提交前现在比较返回delta，不一致则提示并零提交。共同钳制策略和服务/AI统一事务见新计划B19；本轮未修改后端计算。

最终原生Shift+period验证通过：A/V接缝共同向右5帧，Cmd+Z恢复，Esc退出编辑点。最后还对Blade/Hand/Zoom顶部光标做了分流，避免提示淡变却执行切割/平移/缩放；不支持滚动的工具不再由接缝item抢hover。后续专项验证包含该分流、B19拒绝与键盘并发保护。

最终专项组合：手柄手势、旧trim预览、A/T交互、取消/工具切换、数值trim与光标/主题回归，共122项通过。与前述376项有重叠，不相加。
