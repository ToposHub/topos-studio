# Color Viewport 后端接入验收记录（CV-B01–CV-B11）

日期：2026-11-09。执行：本仓库 `bugfix` 分支。对应计划：
`docs/plans/2026-11-09-color-viewport-backend-plan.md`。

- **批次起点**：`67b7ad6a`（Edit 复查批次 I01–I07 终点 + 终跑记录）。
- **批次终点**：见 §1 提交表（9 个实现提交）。
- **平台**：macOS 15.3 offscreen（`QT_QPA_PLATFORM=offscreen`），
  Python 3.12.14，PyQt6 6.11.0 / Qt 6.11.2。
- **素材**：`tests/test_videos`（真实 DNxHR/ProRes/H.264/H.265 等），
  真机素材冒烟走 `tests/real_media.py`（缺失按 `real_media` 语义 skip）。

## 1. 提交清单

| 任务 | Commit | 内容 |
|---|---|---|
| CV-B01+B09 | `382da75e` | 能力注册表/宿主/chrome 回填协议；共享 transport 页面隔离回归；状态栏窄窗折叠 + provider 徽标先存 bug 修复 |
| CV-B02 | `74178007` | 三域精确映射、源 TC（DF/NDF）、VFR 协议、transport 精确 TC 注入 |
| CV-B03 | `0ae38a19` | 活路径切 clip 复位 Loop、内容范围（排除空尾）/显式 in-out 验证、停止/倒放能力声明 |
| CV-B10 | `9bef1610` | 能力宿主关门守卫、关闭管线停用 Color 私有预览渲染器（步骤 0.5/3.5） |
| CV-B05 | `c6675e59` | Power Window 线框 overlay 接真实后端、Highlight/overlay 独立、alpha 两级归因 |
| CV-B06 | `8100805f` | 监看静音接 Master 总线（保留先前状态、离页恢复）；Proxy/Resolution/Bypass/Unmix 如实禁用 |
| CV-B07 | `715eb9e9` | 抓帧保存失败零残留（sidecar 清理）；播放暂停/恢复政策验证；Freeze 维持未设计声明 |
| CV-B04 | `83fdd04e` | 多版本比较会话后端（纯域件：provenance/缓存键/LRU/失效/释放，与 wipe 严格隔离） |
| CV-B08 | `2d165095` | 全屏/浮动/焦点作用域如实声明；F11 既有行为保持；focus mode 暂存/恢复验证 |

## 2. 各任务落地要点（对照计划细则）

**CV-B01**：`src/features/color/core/color_viewer_capabilities.py`
（`ColorViewerState`：page/work_mode/`presentation_path`/`time_domain`/
`context_revision`，全部可序列化；`{supported, reason, checked}` 注册表，
未设计服务位如实 False＋稳定 reason）＋
`src/app/controllers/color_viewport_capability_host.py`（聚合真实事实→
评估→回填；revision 守卫丢弃过期回填；`_HANDLED_ACTIONS` 二道门——无
handler 的动作即使域位为真也禁用）。chrome `refresh_capabilities`
（blockSignals 状态锁）回填菜单/按钮/transport。接线：dock 安装、页面
切换、load_video/load_image、clip 上下文 set/clear。旧 View/Wipe/Mask/
Guides/Workspace 行为仍走原信号链。

**CV-B09**：Color↔Composite↔image 连续切换 keyframe/I/O/默认 Loop/
高度不串（12 项往返断言）；程序化回填零回声（set_current_frame 不发
scrub/seek）；`hide_for_image` 重置语义；状态栏窄窗折叠（<380px 链路
文字进 tooltip、<220px 连降噪折叠、provider 徽标保留）；resizeEvent
状态栏搬移统一到 `update_video_control_bar_position` 单一裁决点；
**修复先存 bug：provider 徽标自初始提交起未加入布局、从未可见**。

**CV-B02**：`color_time_domain.py`——`local_to_source_frame`/
`source_to_local_frame`/`local_to_timeline_frame`/`timeline_to_local_frame`
（包住 `ClipInstance` 权威有理数映射：trim/retime/reverse/freeze；
`effective_source_start` 偏移集中）；`SourceTimecodeContext.from_metadata`
（Rational fps、起始 TC、DF/NDF——';' 分隔符权威、非法起始码回落相对
TC 不伪造）；`parse_to_frame` 非法输入 None（播放头不动）；
VFR duck 协议（points 二分，与 Edit `SourceFrameTimeIndex` 结构一致，
不全片扫描）。FileOpsMixin 三方法委托域层（64 既有测试复验等价）；
`_refresh_color_timecode_context` 四入口重建（load_video/切片段/清
上下文/卸载）。`VideoControlBar.set_timecode_formatter`：域优先、异常
回落旧 float；镜像 `timecode_changed` 只发最终字符串。

**CV-B03**：`_set_color_timeline_clip_playback_context` 切 clip 复位
Loop OFF（此前复位只在已停用的 preview 激活路径，活路径继承上一支
Loop、Composite 默认 ON 可能泄漏）；Loop ON=选中 clip 范围、Loop OFF=
内容范围（排除 TimelinePanel 空尾）或显式 in/out；首末按钮保持
clip-local 端点（不直连源全域）；`transport.stop_in_place`（两路径
真实）/`transport.reverse_play`（两路径均无真倒放，如实 False）声明。

**CV-B10**：`ColorViewportCapabilityHost.mark_shutdown`（关门后
refresh/install no-op、菜单刷新请求断开——晚到回调不触碰销毁中的
chrome）；关闭管线步骤 0.5（宿主关门）与 3.5/3.6（停在 Color 页直接
关窗时停用私有预览渲染器＋恢复监看静音）；预览渲染器先断 frame_ready
再 shutdown/deleteLater；停用后晚到帧不上屏；管线幂等。

**CV-B05**：chrome `power_window_button`（Off/All 两态）→
`Canvas.set_overlay_visible`（只改显示，窗口业务状态零触碰）→
`capability_refresh_requested` 按真实状态回填 checked；Highlight（引擎
matte 预览）与线框 overlay 双向独立；`monitor.alpha` 两级归因（素材无
alpha vs 显示路径未实现，均不虚亮、不拿 qualifier matte 冒充）；
alpha 旗标由 B02 探测路径顺手记录。

**CV-B06**：`EditPage.set_monitor_master_muted/monitor_master_muted`
公共 API（`AudioMixer`/`AudioPlaybackManager` 补线程安全
`is_master_muted`）；静音走 Master 总线（设备无声、视频照常、不碰
轨道/片段 volume）；记录并恢复用户先前 master 状态（不擅自打开用户
静着的总线）；音频桥静音期间不起声；离页/关闭恢复（Color 作用域不
泄漏）；chrome mute 按钮→宿主路由→checked 按实际生效状态回填。
Proxy/Resolution/Bypass/Unmix 维持如实禁用（真实服务不存在，不拿
AB/wipe 冒充 bypass、不直改 pipeline）。

**CV-B07**：`on_capture_still` 保存失败清掉本次写盘 thumb/still/EXR
sidecar（不留半份 preset/Gallery 孤儿缩略图）；视频模式抓帧经
`_render_current_video_frame_for_grading` 精确帧路径（不保存显示缓存
旧帧）；仅原播放态恢复（用户暂停不擅自起播）；渲染失败零写盘＋恢复
播放。Grade Freeze 维持 `freeze_undesigned`（无独立快照语义不冒充；
`freeze_graph` 渲染快照≠用户冻结功能）；reference still 单帧天然有界。

**CV-B04**：`compare_session.py` 纯域件——`CompareItem`
（provenance：project/asset/clip/version/revision/frame/size/color；
保真度分级，jpeg_thumb 只可列表不可作比较 master）＋
`ColorCompareSession`（首版 ≤4 项、single/side_by_side/grid_2x2、
缓存键含 revision/frame/size/color、LRU 字节上限、删版本/切工程立即
失效并释放快照、release 回调宿主释放纹理、诊断 JSON 可序列化、释放
回调异常不破坏会话）。与 wipe 严格隔离（引擎零触碰；begin 存 wipe
状态、end 原样交回）。原 wipe/difference/A-B 行为与测试不动。chrome
split_screen 按钮维持禁用，原因如实两级（后端已隔离、grid presenter
未接线）。

**CV-B08**：注册表声明 `view.fullscreen`（既有 F11——toggle 视口顶层
window，dock 时主窗/浮动时浮动 dock 窗，行为不动）、`view.dock_float`
（真实）、`view.fullscreen.viewer_only`（未设计，如实禁用并解释）；
focus mode 既有暂存/恢复语义经验证（退出只恢复进入前原状，不写 True
给原本隐藏的 dock）。

## 3. 真机素材证据（offscreen 真实子集）

`tests/color/test_color_real_media_smoke.py`（标记 `real_media`）：

- 真实 `2K_prores422.mov` 经 `MediaSourceFactory.probe_video_metadata`
  （真实 FFmpeg 容器解析）→ `SourceTimecodeContext`：帧 0 与末帧的
  TC 格式化→解析往返精确一致；25fps 判定 NDF、秒进位正确。
- 源帧恒等域映射在真实素材总长内逐点往返。

**未覆盖（真机证据边界，如实不填通过）**：4K60/HDR 双路吞吐与
p95/10 分钟内存基线、外屏 HDR 监看一致性、音画听感、50 次 clip/模式
切换与 30 次 Color↔Composite/浮动/全屏整机循环（`tests/benchmark/
drive_real_app_*` 可作后续专项驱动，本批未执行）；viewer 外观偏好与
屏幕位置持久化（未实施）；比较会话 grid presenter 与 GPU 网格呈现
（后端已交付、UI 未接线）。

## 4. 测试证据（offscreen）

计划 §3 验证组（既有套件）＋本批新套件全部通过：

```
# 计划列出的七个验证组（339 passed）
QT_QPA_PLATFORM=offscreen pytest \
  tests/color/test_video_control_bar_scrub.py tests/color/test_fix_wd4g23_aud.py \
  tests/color/test_fix_wd3g22_aud.py \
  tests/color/test_ab_reference_wipe.py tests/app/test_clip_version_host.py \
  tests/app/test_clip_version_project_sync.py \
  tests/app/test_color_timeline_clip_playback_mapping.py \
  tests/app/test_color_page_audio_bridge.py \
  tests/app/test_color_entry_playback_and_timeline_refresh.py \
  tests/color/test_canvas_clip_switch_preview.py \
  tests/composite/test_composite_viewport_safe_area.py \
  tests/media/test_timecode.py tests/media/test_f54_vfr_alignment.py \
  tests/render/test_hdr_gpu_contract.py \
  tests/color/test_color_monitor_status_bar.py tests/color/test_quality_control.py -q
  → 339 passed

# 本批新套件（含真机素材冒烟；183 passed）
QT_QPA_PLATFORM=offscreen pytest \
  tests/color/test_color_viewer_capabilities.py tests/color/test_color_time_domain.py \
  tests/color/test_compare_session.py tests/color/test_video_control_bar_page_isolation.py \
  tests/color/test_color_real_media_smoke.py tests/color/test_presets_gallery.py \
  tests/app/test_color_viewport_capability_host.py tests/app/test_color_timecode_host_integration.py \
  tests/app/test_color_playback_loop_policy.py tests/app/test_color_shutdown_lifecycle.py \
  tests/app/test_color_power_window_overlay.py tests/app/test_color_monitor_mute.py \
  tests/app/test_color_viewer_scopes.py tests/app/test_gap027_main_window.py -q
  → 183 passed
```

新增用例分布：域件 50（能力 16＋时间域 26＋比较会话 10 中的域部分）、
宿主/集成 63、transport 隔离 9、抓帧 4、静音 10、作用域 3、真机冒烟 2
（数字按套件粗分，详见各文件）。

过程修复（非本批引入的先存问题）：

- **provider 徽标从未可见**（初始提交起 `_provider_label` 未加入布局；
  CV-B09 安全区回归抓出并修复）。
- **本批自伤修复**：shutdown 测试初版只 `deleteLater` 不 `close`，与
  存活的 `ImageEngineRawWorker` 线程相撞导致组合跑 Qt abort——改走
  gap027 收尾契约（close 停线程→deleteLater→qWait）后组合全绿。

全量 Color 域扫尾（`tests/color/` 全部 ＋ 本批全部宿主/集成/映射/音频
套件，offscreen，124 秒）：

```
1 failed, 4157 passed, 5 skipped
```

唯一失败 `test_curves_soft_clip_model.py::…::
test_legacy_v2_layer_dict_loads_identity_and_resaves_stable` 经基线
worktree（`67b7ad6a`，不含本批任何改动）复现——**先存失败，与本批
无关**（curves 旧字段迁移断言，属曲线序列化域）。其余 4157 项全部
通过。

## 5. 已知边界与后续

1. Proxy Handling／Preview Resolution／Bypass：真实策略服务未设计接入
   （menu 不改 UI 不跳 pipeline）；能力位如实禁用带原因，等管线侧提
   供监看域策略接口。
2. Unmix（转场隔离）与 Grade Freeze（独立快照语义）未设计，保持禁用
   并说明；`freeze_graph` 是渲染快照机制，不冒充用户冻结。
3. 比较会话的 grid presenter／GL 网格呈现与多视频同步播放未实施
   （后端域件已隔离交付，另立资源预算）。
4. viewer-only 全屏作用域与 viewer 外观偏好持久化未实施（F11 保持
   既有顶层 window 语义）。
5. 真机专项（4K60 吞吐/HDR 外屏/听感/长循环）见 §3 边界；
   `tests/benchmark/drive_real_app_*` 可复用。
