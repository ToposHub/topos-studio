# 剪辑第七轮：R01–R23 修复执行进度记录

日期：2026-10-22（Asia/Shanghai）
对照：[第七轮审计计划](edit_page_seventh_completion_review_optimization_plan_2026-10-22.md)
基线：`d11615bb8`（fix-ui）；本轮 5 个提交：

| 提交 | 内容 |
|---|---|
| `f5ece3045` | [fix-0A音频] R03/R04/R05/R06/R07 输出正确性五项 |
| `835e7dacf` | [fix-0A交付] R01/R02/R15–R19 交付上下文与图序安全七项 |
| `ce33394fc` | [fix-0B编辑闭环] R10/R11/R12/R13/R14 五项 |
| `c1cf36fe5` | [fix-0C生命周期] R08/R09/R20/R21/R22/R23 六项 |
| （本次收尾） | shared 过期测试签名修复 + 本进度文档 |

## 1. 逐项完成状态（23/23 全部处理）

### 0A 输出与文件安全
- **R01** Deliver 页内编辑导出旧时间线：`_on_shared_timeline_changed` 同步
  `register_sequence` 进队列上下文；`set_sequence` 同时刷新 panel 的
  `_timeline_sequences` map；`build_jobs`/`_resolve_context` 实时读工程
  revision。验收测试：页内剪短（100→50 帧）后 draft/preflight/JobContext
  全指向新序列。
- **R02** 队列恢复误标失联：`register_sequence` 登记后重评估
  NEEDS_RELINK 任务——存在的目标恢复 pending（含 legacy config 内
  `video_job.timeline_id` 定位），真正离线保持失联。
- **R03** 非零入点变速 DC 常量：`ClipRetimeReader` 统一坐标——解码窗口
  用绝对源帧（source_start+first）、采样端用 pair 相对位置；pair 末段
  钳掉小数（a==b）时外推用每帧源推进量兜底。
- **R04** AAC 短块整段静音：主混音短块补零到精确块长（旧 `+=` 广播异常
  被外层 except 吞掉跳过 clip 余下音频）；`decode_pcm_span` 流尾补零
  （重采样索引钳在真实内容内，越界=静音不产生直流）。
- **R05** 离线保调变调：旧 helper 运行时 `np` 未定义（TYPE_CHECKING 导入）
  被 except 静默吞掉；新增共享 `PreservedPitchReader`（WSOLA），成片与
  Stem 同一实现；倒放/速度曲线等不支持组合显式留痕回退。
- **R06** 混帧率时钟：`decode_samples_for_frame` 定位用调用方传入 fps
  （导出传源帧率，实时传时间线帧率=open 值，行为不变）；1x/retime/保调
  全部按源时钟换算；新增 `ClipInstance.source_fps_fraction()`。
- **R15** 图序覆盖安全：同名冲突默认失败（列出冲突文件）；帧写隐藏暂存
  目录、成功 close 才逐帧原子提交；取消只清暂存（旧文件逐字节哈希不
  变——测试断言 sha256）；授权覆盖时清理同基名旧尾帧（长→短不混帧）。
- **R16** 目录语义/manifest：目录目标写到目录内部（不再写父目录）；
  manifest 对目录交付列帧清单/逐帧哈希/首尾帧号；图序的 output 快照
  目标改为实际帧目录，probe 对序列禁用。

### 0A/0C 图序配置与能力
- **R17** 作业配置贯通：`TimelineExportConfig`/`VideoConfig` 新增
  bit_depth/image_sequence_digits/start_index/overwrite；面板四行 UI
  （仅 PNG/TIFF codec 显示）+ 队列往返 + `to_timeline_export_config` 桥；
  bit_depth=16 → pix_fmt rgb48le → `_use_high_precision_export` 高精度
  uint16 渲染管线（真实 16-bit，非上采样）。
- **R18** PNG16 端序：实证 PyAV `from_ndarray` 对 be 格式自行转换——
  去掉手动 `byteswap`（旧双重交换把 [30,250,100,200] 变 ×256）；
  RGB/RGBA16 逐值回读测试（TIFF le 对照）。
- **R19** `supports_audio = False` 类属性——主导出已有检查点据此显式
  设置 `audio_omitted=True`（结果/队列/日志三处披露），不再尝试不存在的
  add_audio_stream 后静默成功。面板对序列 codec 与 GIF 同语义禁用音频。

### 0B 编辑闭环
- **R10** 属性动画编辑闭环：已动画参数数值编辑 → `animated_param_edited`
  → 控制器在播放头帧 Add/替换关键帧（`add_keyframe` 同帧原位替换，单次
  Undo）；◆ 取值优先行控件当前值；播放头移动按帧求值回显（blockSignals
  不反写）；未动画参数保持静态 base 路径；多选批量保持既有语义。
- **R11** 旧工程动画值域迁移：`from_dict` 加载与 `with_keyframe_added`
  打点前把已知 clip 参数（opacity/transform.*）的 AnimatableParam 与
  KeyframeTrack 两层值域迁到权威 schema（求值钳制走 track 的 min/max）；
  关键帧 raw、base_value、未知参数名的持久化值域原样保留。
- **R12** TTML 缺省 tickRate 按 W3C 推导：frameRate×subFrameRate（含
  frameRateMultiplier/subFrameRateMultiplier 全组合）；显式 tickRate 优先；
  无 frameRate 保持 1 tick/秒。
- **R13** TTML 非法时基/帧式时钟/父区间：tickRate/frameRate 为 0/负/非
  数/非有限 → 明确 ValueError（旧实现夹 1e-9 产出 2400 亿帧起点）；
  `HH:MM:SS:FF` 帧式时钟按声明帧率换算（源 30fps `00:00:01:00→00:00:02:00`
  → 目标 24fps 帧 24→48）；子元素活动区间被父 div `[begin,end)` 裁切。
- **R14** 旧跨轨命令 Undo 回 0：`MoveClipToExistingTrackCommand.apply` 把
  真实原位置上填到缺省的 `old_timeline_start`（兄弟命令第六轮已修，本
  变体漏网）；序列化重放同修；测试同时断言位置与关键帧帧位/值。

### 0C 生命周期与一致性
- **R08** 实时 FX 动画从 inactive 激活：`mix_samples` 中动画采样先于
  `chain.is_active` 判断（静态中性 saturation drive=0 的链在关键帧
  drive=100 时被采样唤醒）；无采样器的静态 inactive 链行为不变。
- **R09** 响度分析取消即时生效：取消改为 GUI 线程 lambda 直接调用置位
  原子布尔（旧跨线程排队调用要等 run 结束才执行——实测 100ms 取消时
  标志仍 False）；线程收尾异步化（finished→deleteLater，取消后不再
  `thread.wait(5000)` 阻塞 UI）。
- **R20** DeliveryCapability 输出形态：GIF 容器=gif、PNG/TIFF 序列=
  sequence（此前一律误报 mov）；PNG/TIFF 序列加入 alpha 直通集合。
- **R21** 切源 GL1282（尽力范围）：ProgramViewer 绘制前校验
  program/VBO/VAO 有效性（glIsProgram 等），失效即重建一次，迟到 paint
  不再向驱动刷无效 `glUseProgram`；**长循环显存/lease 全量验收仍需真实
  窗口九素材切换复现（与第六轮 U06 同边界，未关闭）**。
- **R22** Q01 门禁有界终止：生命周期场景进独立子进程，
  `faulthandler.dump_traceback_later(exit=True)` 自带硬超时+全线程栈，
  父进程再兜 `subprocess.run(timeout)`——故意阻塞 close 也保证预算内
  失败退出；`__del__` 计时修正到 del 之前。**根因（单进程全套卡死）仍
  需全套环境复现，未关闭**。
- **R23** 效果面板 deleted-item：`_unbind_responsive_sections` 统一在
  视频/音频/搜索重建 `tree.clear()` 前解绑（对齐 transition 树）；
  `_sync_host_row_height` 目标守卫在宿主 item 已销毁时自解绑，不再把
  wrapped C/C++ object deleted 抛进 Qt 槽。

## 2. 回归证据

| 批次 | 结果 |
|---|---|
| `tests/deliver` 全目录 | **235 passed** |
| `tests/media` 全目录 | **3430 passed, 64 skipped**；唯一失败 `test_declared_sources_exist` 为预存缺失素材 `2K_topos_proxy.mov`（与第六轮基线一致） |
| `tests/shared` 全目录 | **2429 passed, 4 skipped**（含修复 2 个过期测试签名：`_create_with_backend` 假替身缺 `no_debreak` 参数——在第七轮基线上同样失败，属测试自身过期，非本轮回归） |
| 第六轮+第七轮全部回归文件 | **168 passed** |
| edit 关键词批（command/keyframe/properties/caption/subtitle/transition/move/speed/freeze/audio） | **2608 passed, 2 skipped** |
| 0C 受影响套件（audio_playback/three_way/effect_sections/delivery_capability/q01_lifecycle） | **162 passed** |

新增测试文件（第七轮）：`tests/edit/test_fix_round7_audio_output.py`（13）、
`tests/deliver/test_fix_round7_delivery_context.py`（6）、
`tests/edit/test_fix_round7_image_sequence.py`（17）、
`tests/edit/test_fix_round7_r10_animation_edit.py`（7）、
`tests/edit/test_fix_round7_r11_legacy_ranges.py`（4）、
`tests/edit/test_fix_round7_ttml_timebase.py`（15）、
`tests/edit/test_fix_round7_r14_move_undo.py`（4）、
`tests/edit/test_fix_round7_lifecycle_ui.py`（8）——共 74 例。

真实素材核验（`tests/test_videos/2K_h264.mp4`，AAC 44100，25fps）：
- R04 1x 入点 50：peak 0.149、176384/176400 非零（审计基线：全静音 0）；
- R03 2x 入点 50/100、0.5x、reverse、2x 保调：尾帧 std 1.5e-2~4.6e-2
  （审计基线：3.7e-9 DC 块），全程无非零塌缩。

## 4. 复查补遗（2026-10-22 第二遍自查）

对 R01–R23 逐项复核后发现并修复 4 处遗漏/隐形缺口：

1. **R23 遗漏树**：`_populate_favorites`（星标增删触发的重建）也经
   `_build_effect_section` 承载响应式 section，但其 `tree.clear()` 没有
   解绑——补 `_unbind_responsive_sections`。视频/音频/搜索/转场树已覆盖，
   字幕树确认只建普通 item 无需解绑。
2. **R10 scale 锁联动缺口**：静态路径 `with_scale(x, y)` 锁住时双轴同改，
   动画路径原实现只给被编辑轴打点（scale 行默认锁住，常见路径即命中）。
   信号升级为复数形式 `animated_params_edited(clip_id, [(param, value)...],
   frame)`：peer 轴已动画时同帧双轴打点并合并为**单次 Undo 的
   MacroCommand**；peer 未动画时不自动创建轨道（保持静态值）。
3. **R02 旧队列格式遗漏**：更旧的扁平 config（pre-video-batch 队列）把
   `timeline_id` 存在 `config` 顶层而非 `config["video_job"]` 快照里——
   relink 扫描补顶层回退，两条持久化格式都能重链。
4. **R13 非标准组合**：帧式时钟 `HH:MM:SS:FF` 与小数秒 `.frac` 同时出现
   时旧分支互斥会静默丢帧分量——改为两者叠加计入。

复查同时确认安全的项：R06 seek 时钟改动全部 4 处调用方语义核对（实时
路径传时间线 fps=decoder open 值，行为不变；响度 worker 走
`seek_to_time`+`decode_next_samples` 不受影响）；`DeliveryCapability.container`
无生产消费方（改值安全）；`ClipRetimeReader`/`PreservedPitchReader` 边界
（窗口不可用早退不触未定义属性、多 clip 共享 decoder 由显式
`seek_to_time` 重置顺序状态、取消中途返回 None 走静音跳过、staging 并发
冲突显式失败而非静默混写）；真实素材角落组合矩阵（曲线变速/倒放 0.5x/
混帧率 50fps 1x 与 2x/2x 保调）时长精确、无 DC 塌缩、无非零塌缩。

复查后回归：deliver 236、edit 关键词批 3095、六七轮回归文件 178——全部
通过。

## 5. 保留边界（诚实披露）

- **R21 根因**与 **Q01 根因**未关闭：需要真实可见窗口九素材切换长循环 /
  单进程全套复现环境（第六轮遗留同边界）；本轮交付的是目标守卫 + 子进程
  硬超时门禁。
- R04/R16 的"真实 AAC 成片外部 ffprobe 复验"未在 GUI 里重跑（混音层
  PCM 已在真实素材上验证非静音；编码器侧写 PCM 的链路未被本轮改动）。
- 第七轮计划 §8 的专业能力建设（G01–G17 剩余项）不在本轮 23 项范围内，
  未开工。
- 会话中两次误弹旧 stash（工作区干净时 `git stash pop` 弹出历史 WIP），
  均以 `git reset --hard HEAD` 恢复，所有提交完好、旧 stash 列表保持原样。
