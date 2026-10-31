# file_explorer 升级优化计划（2026-09-07）

> **生成方式**：四路并行深度审计（后端 / 前端 / 插件体系 / 功能盘点）+ 关键发现人工逐条复核（P0 与高影响 P1 已全部验证属实）。
> **审计基线**：`chore/color @ 636e60d5`（第一轮系统检查 R0/R1/R2/R4/R5 完成后，见 `docs/system_check_plan_2026-09-04.md`）。
> **审计范围**：`external/file_explorer/` 全部 16 个 Python 模块（约 4,700 行）、前端三模块 + index.html（约 1,400 行）、9 个插件（约 8,000 行）、SQLite 数据模型。
> **文档性质**：问题总清单 + 专业素材管理软件（DAM）功能差距分析 + 分批升级路线图。本轮只审计不修复。

---

## 0. 执行摘要

**总体判断**：file_explorer 的工程底子好于同类内嵌素材管理器——鉴权模型完整（41 条 /api 路由全覆盖）、三模块前端架构清晰、三引擎播放器是超出一般 DAM 的差异化长板。但本轮审计暴露出三类系统性短板：

1. **重构完成度缺乏闭环验证**：ARCH-1/2/13 三次大拆分各留下一处"迁移了一半、没有对照核验"的回归——前端导出清单漏掉 `updateSelectionUI` 导致重命名主流程崩溃（P0）、音频截取依赖已被删除的 inline onclick 选择器而静默失效（P0）、播放器 Up/Down 键三引擎方向相反。
2. **静默失败文化**：全栈约 30 处裸 `except`/`except: pass` 把"数据丢了/操作失败了"伪装成"功能正常"——重命名后元数据滞留旧路径、十多处 POST 失败无任何提示甚至反向报成功。
3. **产品能力停留在"文件浏览器+"**：标签是扁平逗号字符串且无编辑 UI、搜索只过滤当前目录、无集合/相册、无回收站、无 undo、元数据以绝对路径为主键（应用外移动文件即失联）。与 Adobe Bridge / Eagle / Billfish 相比，组织与检索能力是数量级的差距。

**问题统计**（共 133 项）：

| 层 | P0 崩溃/数据丢失 | P1 功能错误 | P2 隐患/边界 | P3 改进项 | 合计 |
|---|---|---|---|---|---|
| 后端（Flask/SQLite/线程） | 2 | 4 | 15 | 36 | 57 |
| 前端（JS/HTML/CSS） | 2 | 8 | 12 | 16 | 38 |
| 插件（9 个 + 装载器） | 5 | 8 | 14 | 11 | 38 |
| **合计** | **9** | **20** | **41** | **63** | **133** |

**功能差距**：识别出 6 个 T1 核心缺口（标签体系、全库搜索、颜色标记/旗标、集合、回收站/撤销、元数据 re-link 与备份）+ 9 个 T2/T3 重要能力，详见第 2 节。

---

## 1. 缺陷修复清单

严重度定义：**P0** = 崩溃 / 不可恢复数据丢失 / 功能整体死亡；**P1** = 功能错误 / 安全 / 数据一致性；**P2** = 特定条件触发的隐患 / 资源问题 / 边界缺陷；**P3** = 一致性 / 性能 / 可维护性改进。工作量：S（<半天）/ M（1-2 天）/ L（3 天+）。

### 1.1 P0 — 崩溃 / 数据丢失 / 功能死亡（9 项，必须立即修复）

| 编号 | 位置 | 问题 | 影响 | 修复方向 | 量 |
|---|---|---|---|---|---|
| BUG-P0-01 | `window.py:151-155` | 粘贴 worker 先 `append` 再按 `isRunning()` 过滤、最后才 `start()`——未启动的 QThread `isRunning()` 恒 False，刚加入即被移除，`_copy_threads` 永远为空；第二次粘贴覆盖 `copy_thread` 引用后，仍在运行的首个 CopyWorker 被 GC | "QThread: Destroyed while running" 进程崩溃（CRASH-7 防护形同虚设） | 调整顺序：append → start() → 再过滤；退出路径补 CopyWorker 等待 | S |
| BUG-P0-02 | `routes.py:724-726` | 上传对已存在同名文件无冲突检测，`file.save()` 静默覆盖（对比 rename:242 有检查） | 直接、不可恢复的用户数据丢失 | 存在即报 409 或自动改名（加 " 1" 后缀），前端给覆盖/跳过/改名三选 | S |
| BUG-P0-03 | `fe-render.js:416-433` | `updateSelectionUI`（:148）未列入模块尾部 `Object.assign(FE, {...})` 导出清单，而 fe-actions.js:9 解构、:101 调用 | 每次成功重命名（pendingSelection 分支）必抛 TypeError，被 catch 吞掉后误报"目录加载失败"；重命名后不选中、属性面板不弹、`pyBridge.onDirectoryChanged` 中断 | 补进导出清单；建立 data-action↔handler 与 FE 导出↔解构的自动对照检查 | S |
| BUG-P0-04 | `audio_player.js:220` + `index.html:413` | `saveTrim` 用 `querySelector('button[onclick*="saveTrim"]')` 定位按钮，ARCH-13 已将该按钮改为 `data-action`（无 onclick 属性） | `btn` 恒 null，`if (!btn) return` 静默退出——音频"截取"功能整体失效且无提示（重构回归） | 改用 id/data-action 选择器；全插件 grep `onclick*=` 残留选择器 | S |
| BUG-P0-05 | `base_player.py:382-388` | `_late_close` 在 `join(timeout=10)` 再次超时后**无条件** `sess.close()`，违背 375-378 行"线程不退出绝不 close（use-after-free 防线）"的承诺 | 解码线程卡在原生调用超过 JOIN_TIMEOUT+10s 时，从另一线程释放原生解码器 → 原生崩溃。属"有界泄漏 vs 崩溃风险"的权衡，但当前实现选了崩溃侧 | 超时后只记日志+持续泄漏（会话小、可控），或用进程级兜底；至少把强 close 改为可配置 | S |
| BUG-P0-06 | `libav_player.py:259-262` | `av.open` 成功后 `streams.video[0]` 对纯音频/损坏文件抛 IndexError，异常上抛但 container 未 close | 每次误开都泄漏 native demuxer + 文件句柄（构造失败路径到不了 close()） | `__init__` 内 try/except，失败时 close container 再 raise | S |
| BUG-P0-07 | `topos_player.py:347-359` | `_export_clip` 后台线程直接调 `self._toast(...)`（内部 setText/setStyleSheet + QTimer.singleShot） | 工作线程操作 QWidget + 在无事件循环线程建 QTimer，间歇性崩溃/花屏，且回调永不触发 | 改用信号发到 GUI 线程；同类排查 libav `_on_eof` 的 `_icons.set_play_icon`（:418-423，解码线程内 setIcon） | S |
| BUG-P0-08 | `format_factory/converter_gui.py:747-751` | closeEvent 只停 MonitorThread，运行中的 TranscodeThread/RemuxThread 不 cancel 不 wait | 转码中途关窗 = "QThread: Destroyed while running" 进程 abort | 照抄 decode_benchmark（:1114-1133）的完整关停协议 | S |
| BUG-P0-09 | `libav_player.py:418-423` | `_on_eof` 在解码线程内被调用（base_player.py:569）却执行 `_icons.set_play_icon(self.btn_play, ...)` | 解码线程对 QPushButton setIcon/触发重绘，跨线程 UI 访问，间歇崩溃 | EOF 图标更新改信号投递到 GUI 线程 | S |

### 1.2 P1 — 功能错误 / 安全 / 数据一致性（20 项）

**后端（4）**

| 编号 | 位置 | 问题 | 影响 | 修复方向 | 量 |
|---|---|---|---|---|---|
| BUG-P1-01 | `routes.py:140,622,909` | 对 `request.args.get()` 结果二次 `unquote()`，而前端全部用 `encodeURIComponent`（Flask 已解码一次）；且 /api/file、/api/tree、/api/metadata/detail 不做 unquote，行为分叉 | 名含 `%20`/`%2F` 字面量的路径被二次解码破坏（目录 `100%20done` → `100 done`）→ 列目录失败、缩略图 404 | 统一去掉三处 unquote（已验证前端编码方式） | S |
| BUG-P1-02 | `thumb_engine.py:706-739,569-573,616-625` | 音频波形/Pillow/QuickLook/rawpy 四条路径直写 dst 且失败不清理（仅 `_video_via_image` 用 `.part`+`os.replace` 原子写）；路由命中残缺图即返回，无 sidecar 时 `is_stale` 恒 False | 磁盘满/超时（721 行 kill 后）产生的残缺缩略图永久驻留，坏图永不自愈 | 全部缩略图写出统一走 tmp+`os.replace`；失败清理 | M |
| BUG-P1-03 | `routes.py:252-258,427-428` | 单个/批量重命名后 `UPDATE file_meta` 失败被裸 `except: pass` 吞掉，且 `conn.close()` 不在 finally | 文件已改名但 tags/rating/comments 行滞留旧路径——用户元数据静默"丢失"，智能文件夹查不到 | 裸 except 换带日志降级；全部连接操作 finally 化 | S |
| BUG-P1-04 | `db.py:19-23` | DB_FILE 在 import 时一次性判定：topos.db 当时不存在则永久锚定 fallback metadata.db，进程内不回切 | 元数据双库分叉。**现场证据**：仓库根 topos.db（49KB, 9/6）与 external/file_explorer/metadata.db（53KB, 8/28）并存，两库 schema 一致但数据已分叉 | 延迟判定 + 提供一次性合并迁移工具（按 mtime/行数取胜） | M |

**前端（8）**

| 编号 | 位置 | 问题 | 影响 | 修复方向 | 量 |
|---|---|---|---|---|---|
| BUG-P1-05 | `fe-actions.js:208` | `openSmartFolder` 无 loadPath 那样的 seq 竞态令牌（对比 :86 的 FUNC-15 防护），fetch 无 abort | 快速连点两个智能文件夹/点智能文件夹后立刻点目录树时，旧响应后到覆盖新列表，`currentPath` 与显示内容漂移，后续删除/重命名作用在错误目录上 | 复用 seq 令牌 + AbortController | S |
| BUG-P1-06 | `fe-actions.js:588,671-681` | `handleMenuRename` 对 `doRename()` promise 无条件 `.then` 改写树节点，而 doRename 失败分支只 toast 不 reject | 服务端拒绝（重名/非法字符）后左侧树仍显示"已改名"节点，与磁盘不一致 | doRename 失败 reject；或 then 内先校验结果 | S |
| BUG-P1-07 | `fe-actions.js:367` | 评分 ACK 拼接只有第一个路径带 `confirmed=` 前缀，其余成裸参数（对比 :325 thumb 轮询的正确写法） | 一次多条评分广播只 ACK 一条，其余被后端反复推送、重复刷新 | `map(p => 'confirmed=' + encodeURIComponent(p)).join('&')` | S |
| BUG-P1-08 | `fe-actions.js:94-101` | 目录加载失败（`data.error`）不清空旧卡片、不重绘 | 路径栏已显示新路径、卡片仍是旧目录文件——用户在"以为已切换"的视图上右键删除，实际删的是旧目录的真实文件（危险） | 失败时清空视图 + 明显错误态 + 重试按钮 | S |
| BUG-P1-09 | `fe-render.js:361-374` + `fe-actions.js:436` | `loadDeepInfo` 无竞态令牌/abort，键盘方向键路径每步直接调 `showFileProperties`（无 onCardClick 的 200ms 防抖） | 快速导航时旧文件的 EXIF/ffprobe 响应后到覆盖新面板内容 | 加 seq 令牌 + 键盘路径复用防抖 | S |
| BUG-P1-10 | `fe-actions.js:122,215` + `fe-render.js:41` | `openItem`/`navigateFile` 对 `visibleFiles[i]` 无空值检查；`applyFilterAndSort` 只重置 `renderIdx` 不重置 `STATE.lastIndex` | 搜索/筛选缩短列表后按 Space/Enter 抛 TypeError；空列表 `% 0` 崩 | applyFilterAndSort 集中重置 lastIndex/滚动/进度计数三个散落状态；调用处空值守卫 | S |
| BUG-P1-11 | `fe-actions.js:190` | 主题名直接取 CSS 文件名，`$('#icon-theme-Tahoe 26')` 含空格为非法选择器抛 SyntaxError | 该主题样式已切换但持久化与后续代码中断，重启后回退旧主题 | 用 `CSS.escape` 或 data-属性选择器 | S |
| BUG-P1-12 | `index.html:540` | `JSON.parse('{{ file_handlers \| safe }}')` 以单引号包裹 json.dumps 输出（json.dumps 不转义单引号） | 任何插件注册名含 `'` 即整段内联脚本解析失败，三个 fe 模块全部不加载——前端整体瘫痪 | 改 `{{ file_handlers \| tojson }}` | S |

**插件（8）**

| 编号 | 位置 | 问题 | 影响 | 修复方向 | 量 |
|---|---|---|---|---|---|
| BUG-P1-13 | `player_qt.py:268-275` | `_on_playback_state` 未处理 StoppedState | 自然播完后 `is_playing` 恒 True：播放键停留"暂停"图标、控件照常自动隐藏、下次空格误走 pause 分支 | 补 StoppedState 处理 | S |
| BUG-P1-14 | `base_player.py:311-321` | play_video 打开新文件失败路径只 hide()，上一个会话的解码线程继续跑 | 隐藏播放器后台持续解码耗 CPU，libav 场景音频也不会停 | 失败路径先 stop_decoder 再 hide | S |
| BUG-P1-15 | `topos_player.py`（缺钩子） | 未覆写 `_on_eof`（仅 libav_player.py:418 有）；片尾按播放无从头重播逻辑（libav 有 `_request_seek(0)`） | ToposPlayer 播到片尾播放键图标不归位；片尾按播放"没反应"——与 LibavPlayer 行为分叉（ARCH-2 钩子遗漏） | 补 `_on_eof` 钩子 + 片尾重播 | S |
| BUG-P1-16 | `topos_player.py:247-250` vs `libav_player.py:482-485` vs `player_qt.py:503-504` | Up/Down 键语义三家不一致：topos/qt 是 Up→上一个、Down→下一个，libav 相反；基类按钮 tooltip（base_player.py:235,242）只与 libav 吻合 | 同一按键在不同引擎导航方向相反（已验证） | 统一为 Up→prev/Down→next，改 libav | S |
| BUG-P1-17 | `libav_player.py`（全文） | 有声轨道经 QAudioSink 播放但全文件无一处 setVolume，UI 也无音量条（qt 引擎有） | libav 播放音频音量固定不可调不可静音 | 基类加音量条，两引擎接 QAudioSink/QMediaPlayer | M |
| BUG-P1-18 | `shutdown_timer/timer_gui.py:727-735` | `quit_app` 同步调 `ShutdownManager.cancel()`（osascript 提权冻结 UI，:705-707 的 stop_shutdown 已修成异步、此处漏改）；随后 close 被 closeEvent 拒绝（is_running 仍 True） | 点"退出"既退不出，倒计时到 0 还会再发一次 `shutdown -h +0` 强制关机——唯一直接能"意外关掉用户机器"的插件 | cancel 异步化 + 成功回调后再关窗 | S |
| BUG-P1-19 | `libav_player.py:430-434,273` | 文件截断/时长高估（duration×fps 估 frame_count）触发 StopIteration → `_on_decode_stop` 只打 traceback + 静默暂停 | 用户只见画面冻结不知原因，无 toast/play_failed | `_on_decode_stop` 补用户可见提示 | S |
| BUG-P1-20 | `fe-actions.js:190` 相关（主题）+ `routes.py:801` | `/api/action/add_library` 用 GET 做状态变更；SameSite=Lax 下顶层导航携带 cookie，外部网页可让宿主弹出文件夹对话框（CSRF 面） | 安全：状态变更路由可被跨站触发 | 状态变更一律 POST + 前端同步改 | S |

### 1.3 P2 — 隐患 / 边界 / 资源（41 项）

**后端（15）**

| 编号 | 位置 | 问题 | 影响 | 量 |
|---|---|---|---|---|
| BUG-P2-01 | `routes.py:1098-1163` | smart_folders 五个路由 + batch_rename/batch_meta（:418-498）的 `conn.close()` 全在 try 尾部而非 finally | 任何 SQL 异常直接泄漏 sqlite 连接 | S |
| BUG-P2-02 | `routes.py:213` | /api/list 元数据批量查询整体 `except: pass` | DB 锁超时时 tags/rating/cached_info 整目录静默消失 | S |
| BUG-P2-03 | `db.py:44` + `routes.py:152,215` | DIR_CACHE 只在读取时校验 TTL、从不淘汰过期条目，缓存全量 files 列表 | 长会话浏览大量/超大目录内存无限增长 | S |
| BUG-P2-04 | `routes.py:314-315` | `_rating_update_queue` 仅由前端 poll 消费、无上限（thumbs 队列有 500 截断） | 宿主在面板未打开时改评分则队列无界增长 | S |
| BUG-P2-05 | `window.py:100-109` | closeEvent/aboutToQuit 只停缩略图线程，不等 CopyWorker | 复制进行中退出应用触发 QThread 析构崩溃 | S |
| BUG-P2-06 | `bridge.py:41,60,92` | `CopyWorker._is_running` 全仓库无代码置 False（已 grep 证实） | 粘贴不可取消，取消机制是死代码 | S |
| BUG-P2-07 | `routes.py:968-983` | `_volume_available` 对非 /Volumes 挂载点恒 True | NFS/自定义挂载点暂不可达时其孤儿 meta 行被当"已删除"清掉——网络盘用户的标签/评分永久丢失 | M |
| BUG-P2-08 | `routes.py:707-709` | 上传白名单比较 realpath 不做 Unicode 归一，NFD 回退分支（:142-143）会把 NFD 路径写入 `_last_browsed_dir` | ExFAT 等卷上正常上传被"目标目录不在允许范围内"拒绝 | S |
| BUG-P2-09 | `routes.py:849` | audio trim 的 dst 由源路径派生无冲突检测，ffmpeg `-y` 直接覆盖 | 静默覆盖已存在的同名 `*_trim_*` 文件 | S |
| BUG-P2-10 | `routes.py:136-217` | /api/list 无分页无条目上限 | 10 万+文件目录全量 scandir+逐文件 stat+数十 MB JSON，阻塞 waitress 线程并整份进 DIR_CACHE | M |
| BUG-P2-11 | `thumb_engine.py:251` + `routes.py:651,1056` | `get_thumb_path` 读取路径调用 `get_cache_info`（mkdir .cache + 写 .nomedia） | GET /api/thumb 与智能文件夹查询对只读卷/网络卷产生写副作用（/api/list 已用 peek_cache_info 规避，此两处漏网） | S |
| BUG-P2-12 | `db.py:215-216` | save_config 直接 `open('w')` 覆写，无 tmp+rename | 崩溃/断电时 config.json 截断，libraries、窗口状态全丢 | S |
| BUG-P2-13 | `db.py:184-188` | init_db 整体 except 仅记日志后继续运行 | 表创建失败时应用照常启动，后续元数据操作持续 500 | S |
| BUG-P2-14 | `utils.py:322` | `logger` 从未定义（无 import logging），该行是唯一引用 | `get_image_info` 异常分支触发 NameError（当前无调用方，埋雷） | S |
| BUG-P2-15 | `webapp.py`（未配置） | 上传无 `MAX_CONTENT_LENGTH` 限制 | 超大上传可填满磁盘 | S |

**前端（12）**

| 编号 | 位置 | 问题 | 影响 | 量 |
|---|---|---|---|---|
| BUG-P2-16 | `fe-state.js:197-203` | 捕获相位 `data-stop` → `e.stopPropagation()` 切断传播，Bootstrap 关闭下拉唯一依赖的 document 冒泡监听收不到事件 | 设置菜单内所有带 data-stop 的条目（刷新/复制/主题/插件中心/关于）点击后下拉不关闭，需再点一次空白 | S |
| BUG-P2-17 | `fe-render.js:41` | `applyFilterAndSort` 全量重建且从不重置 `#scrollContainer.scrollTop` | 切目录/换排序/筛选后滚动位置残留，进新目录可能停在中部 | S |
| BUG-P2-18 | `fe-render.js:73,94,131` | 每张卡创建即 `thumbLoading.total++`，但 `loading="lazy"` 视口外永不触发 load；重渲染清掉未加载 img 后 `loaded` 永远补不齐 | 大目录下缩略图进度条长期卡半程/永久悬挂，误导用户 | S |
| BUG-P2-19 | `fe-render.js:35,39` | 排序比较器对相等值恒返回 ±1（永不含 0）；date 字段 mtime_ts 数值与 mtime 字符串混合回退可产生 NaN | 同值文件顺序抖动，不稳定排序 | S |
| BUG-P2-20 | `fe-actions.js:129,151,164,180,199,212,243,287,620,637` | 大量 POST（play_native/open_with/open_system/粘贴/addLibrary/deleteSmartFolder/saveSmartFolder/上传/reveal/菜单粘贴）不查 `res.ok`、失败无 toast | 操作失败完全静默；saveSmartFolder/deleteSmartFolder 服务端 500 时仍 toast"已创建/已删除"，反向误导 | M |
| BUG-P2-21 | `fe-actions.js:547` | 插件面板收尾 `data.active.length` 无空值防护（渲染处 :496 有 `data.active &&`，此处漏） | 响应结构异常时 TypeError，标签不更新 | S |
| BUG-P2-22 | `index.html:359` + `fe-render.js:376` | 右键"批量操作..."调 `showBatchProperties()`，面板未打开时函数首行 return | 属性面板关闭时该菜单项点了无反应 | S |
| BUG-P2-23 | `fe-render.js:182-190` | #contextMenu position:fixed 且无 scroll/resize 关闭监听；data-stop 元素点击截断冒泡无法触达关闭监听 | 滚动列表后菜单悬空漂移到错误位置 | S |
| BUG-P2-24 | `fe-render.js:237-252` | expandNode 无并发防护（双击 toggler 触发两个 /api/tree fetch 竞争写同一 innerHTML），两处 catch 完全静默 | 树偶发空展开且无错误提示 | S |
| BUG-P2-25 | `fe-render.js:67` | `title="${f.rating} 星"` 未过 escapeHtml（且 escapeHtml 对非字符串原样返回） | rating 一旦被后端以字符串返回即成属性注入点，XSS 纵深缺失 | S |
| BUG-P2-26 | `fe-render.js:334-335` | updateFileRating 只往 `.file-thumb-wrapper` 补角标 | 音频卡（无 wrapper :90）、图标卡改评分后角标永远不出现 | S |
| BUG-P2-27 | `fe-render.js:262` | revealTreePath 对路径逐段串行 fetch /api/tree；startsWith 大小写敏感 | 深层路径首屏定位慢；盘符大小写不一致时静默不定位 | S |

**插件（14）**

| 编号 | 位置 | 问题 | 影响 | 量 |
|---|---|---|---|---|
| BUG-P2-28 | `libav_player.py:103-107` | `_AudioPipeline.stop()` 在 GUI 线程 join 1.5s（阻塞 UI）；超时后线程遗弃但 QAudioSink 仍在其手中 | 播放器已关声音不停；新 pipeline 建立瞬间可能双 sink 竞音 | M |
| BUG-P2-29 | `libav_player.py:154-168,221-223` | 音频 seek 路径异常直接逃逸到外层 handler，线程仅打印后退出 | 播放中途音频无声死亡、画面继续，无用户提示 | S |
| BUG-P2-30 | `player_host.py:45-48` + `libav_player.py:244-251` | decide_route 在 GUI 线程同步做文件头探测 + 完整 av.open；is_mezzanine 是 lru_cache(path) | 慢盘/网络盘播一次卡一次 UI；原位重编码的文件路由结果永不更新 | M |
| BUG-P2-31 | `player_qt.py:317-323` | stop() 未停 hide_cursor_timer（每 3s 触发） | 关闭后定时器永久空转（基类 stop 两个定时器都停，行为不一致） | S |
| BUG-P2-32 | `player_qt.py:436-441` | execute_clip 后台线程 `except: pass` 吞掉 ffmpeg 全部错误 | 截取失败时"正在截取..."之后永久沉默，成功也无反馈 | S |
| BUG-P2-33 | `player_qt.py:496-502,464` | 右方向键帧进先 pause()，而 update_ui_state 在 not playing 时提前 return | 帧进期间时间标签/进度条冻结，与基类不一致 | S |
| BUG-P2-34 | `format_factory/converter_gui.py:176,491-498` | 拖入即 GUI 线程逐文件同步 ffprobe（timeout 8s）+ 目录递归 os.walk；ffprobe 缺失回退探测失败时卡片静默丢弃 | 多文件/目录拖入整窗冻结；失败无提示 | M |
| BUG-P2-35 | `format_factory/converter_gui.py:518,707` | progress lambda 捕获卡片控件，转码中删除卡片后下个进度信号触达已删 QWidget | RuntimeError | S |
| BUG-P2-36 | `plugin_system.py:146-157,81` | file_handlers 按扩展名覆盖写 + 加载顺序 os.listdir 未排序 | 后加载插件静默抢占同名扩展名，顺序随平台不确定 | S |
| BUG-P2-37 | `plugin_system.py:128-138` | setup() 中途抛异常的插件：已执行的注册不回滚，failed_plugins 仅记录 | 半注册状态永久残留宿主；无运行时停用/卸载 API | M |
| BUG-P2-38 | `image_factory/image_gui.py:681-688` | closeEvent 只等 8s，超时后照常关窗 | "QThread destroyed"风险 + shutdown(wait=False) 遗留孤儿进程 | S |
| BUG-P2-39 | `shutdown_timer/__init__.py:21-22` | launcher 以 `not isVisible()` 判定重建；倒计时运行中关窗被 closeEvent 隐藏 | 下次启动新建第二个实例：双窗口、双托盘图标、重复关机命令 | S |
| BUG-P2-40 | `base_player.py:582-584,599-601` | 解码线程在 `_ring_lock` 下读 `_seek_target`，UI 线程在 `_cond` 下写 | 跨锁无同步读取，GIL 下侥幸正确，锁方案一改即成竞态 | S |
| BUG-P2-41 | `libav_player.py:273` | stream.frames 缺失且无 duration 的流 → frame_count=1 | 进度条坍缩、每次 seek 全量重探（live/mkv 变体） | M |

### 1.4 P3 — 改进项（63 项，可随批次顺手清偿或选择性采纳）

**后端（36）**

| 编号 | 位置 | 问题（一行式） |
|---|---|---|
| BUG-P3-01 | `routes.py:200-201` | sqlite OperationalError 一律降级 legacy schema——"database is locked" 被静默吞掉 cached_info |
| BUG-P3-02 | `routes.py:579` | /api/tree 裸 except: pass 静默返回空列表 |
| BUG-P3-03 | `routes.py:513-516` | metadata/detail 出错返回 {}，失败对用户不可见 |
| BUG-P3-04 | `routes.py:265-268` | 重命名迁移三尺寸缩略图但不迁移 .v sidecar——新路径失去陈旧检测，旧 sidecar 成垃圾 |
| BUG-P3-05 | `routes.py:547-549` | 删除不清 .v/.part → .cache 垃圾累积 |
| BUG-P3-06 | `thumbs.py:99-101` | 任一尺寸失败即 register_failure 封锁 1 小时——preview 超时连坐阻断 small/large 重试 |
| BUG-P3-07 | `thumbs.py:57-63` | meta worker UPDATE rowcount=0 后 INSERT 非原子，与并发 update_meta 竞态被 except: pass 吞掉 |
| BUG-P3-08 | `routes.py:623 vs 916` | /api/thumb 的 size 不校验（任意串当 small），retry 却白名单校验——不一致 |
| BUG-P3-09 | `routes.py:801` | add_library 用 GET 做状态变更（见 BUG-P1-20） |
| BUG-P3-10 | `webapp.py:48` | token 支持 query string 传递——易落入代理日志与浏览器历史 |
| BUG-P3-11 | `routes.py:949-964` | /static_plugin/ 不在 /api 前缀下完全免鉴权（穿越已有双防，剩暴露面） |
| BUG-P3-12 | `routes.py:242-243` | 大小写不敏感卷上 exists(new_path) 命中自身 → 仅大小写差异的重命名报"已存在" |
| BUG-P3-13 | `routes.py:374-396` | 批量重命名编号用原始列表 idx（跳过的文件导致跳号）；int 转换失败静默不改名 |
| BUG-P3-14 | `routes.py:685-700` | /api/file 传目录抛 IsADirectoryError → 500（应 400） |
| BUG-P3-15 | `routes.py:762-777` | open_with/open_system 参数缺失或执行失败均返回 ok（应 4xx/反馈失败） |
| BUG-P3-16 | 全文件 | 返回结构三种并存：{status:ok} / {success:bool} / HTTP 200 承载 {error}——前端需多分支解析 |
| BUG-P3-17 | `routes.py:739,745,751,816,223,279,362` | 多处 request.json.get 无 None 守卫，非 JSON 体 500/415 不一致 |
| BUG-P3-18 | `routes.py:996,1000-1001` | 智能查询 min_rating 转换失败静默丢弃过滤条件；tags 用 LIKE 子串匹配（"red" 命中 "hundred"） |
| BUG-P3-19 | `routes.py:1030-1036` | NFD 自愈循环逐行开连接 + 逐行 os.stat → 大库慢，无分页 |
| BUG-P3-20 | `routes.py:194-196` | 分块硬编码 500，const.SQL_IN_CLAUSE_CHUNK 定义未用 |
| BUG-P3-21 | `routes.py:37-41` | routes 从 thumbs 导入 7 个 _ 前缀私有符号——拆分后层边界被穿透 |
| BUG-P3-22 | `thumbs.py:63,124,133` | worker 循环内 except Exception as e: pass 三处静默吞错 |
| BUG-P3-23 | `thumbs.py:154` + `bridge.py:182` | worker 与 ensure_runtime 均一次性——关停后进程内不可重启缩略图生成（需文档化） |
| BUG-P3-24 | `bridge.py:44-79` | CopyWorker.run 无顶层 try/except——意外异常时 finished_signal 不发，前端进度覆盖层永久悬挂 |
| BUG-P3-25 | `window.py:72,81-82` | 600ms singleShot 加载首页，失败无重试无提示 |
| BUG-P3-26 | `main.py:87-94` | 健康探测最坏 50×(1s+0.1s)≈55 秒才弹失败对话框 |
| BUG-P3-27 | `main.py:78` | waitress threads=4，audio trim（60s 阻塞）、metadata/detail（同步 ffprobe/exiftool）四个并发即耗尽服务线程 |
| BUG-P3-28 | `webapp.py:82-86` | find_available_port 的 except: return port 把"探测异常"当"可用"；随机兜底端口与临时端口区间重叠 |
| BUG-P3-29 | `webapp.py:22` | SEND_FILE_MAX_AGE_DEFAULT=0 → 所有静态资源每次 304 回源 |
| BUG-P3-30 | `plugin_system.py:101` | 动态 import plugins/ 下任意 __init__.py，无签名/清单白名单——落一个目录即任意代码执行（需明确信任边界文档） |
| BUG-P3-31 | `plugin_system.py:81` | os.listdir 顺序不定 → 插件加载顺序不稳定（同 BUG-P2-36） |
| BUG-P3-32 | `plugin_system.py:133` + `routes.py:894-900,98` | 插件完整 config dict 下发前端——配置中敏感字段暴露 |
| BUG-P3-33 | `db.py:191-198` | load_config 裸 except: pass——配置损坏被静默当空（设置"被重置"无提示） |
| BUG-P3-34 | `routes.py:312-317` | rating 值无类型/范围校验（字符串/负数可入库）；queue 存原始 path 未 NFC 归一 |
| BUG-P3-35 | `file_explorer` 无 `__init__.py` | 双导入路径隐患：`from file_explorer.main import`（adapter）与平铺 `import main`（__main__）会产生两套模块对象/两个 APP_TOKEN——当前无同进程双导入者，注释是唯一防线 |
| BUG-P3-36 | `routes.py`（多处） | /api/list 等 5+ 端点可被 41 条路由中的 GET 缓存干扰；建议统一 no-store 头避免 QWebEngineView 磁盘缓存与配置写竞争 |

**前端（16）**

| 编号 | 位置 | 问题（一行式） |
|---|---|---|
| BUG-P3-37 | `index.html:389,394,398` + `fe-render.js:94,98` | 残留 inline onchange/oninput（AudioProPlugin 三控件）与动态 onload/onerror 字符串——与"inline 全迁 data-action"口径不符 |
| BUG-P3-38 | `index.html:327` | data-action="closeCms" 全库无对应 handler（实际靠 $('#cmsClose').onclick）——死引用 |
| BUG-P3-39 | `fe-actions.js:599-618,689` | 删除后无撤销入口；批量重命名一键应用无确认；删除确认文案不一致（"确定要删除" vs "移至回收站"） |
| BUG-P3-40 | `fe-actions.js:94-96` + `fe-render.js:42` | 目录加载无 loading 指示；空态仅一行纯文本无引导；openSmartFolder 失败时主区空白无重试 |
| BUG-P3-41 | `fe-render.js:94,98` + `index.html:332` | 全库 0 处 alt；卡片 div 无 tabindex/role；checkbox display:none 且无 aria-label；菜单项不可聚焦无 role=menu——键盘完全不可达 |
| BUG-P3-42 | `fe-render.js:302-325` | 自绘对话框无焦点陷阱；confirm 无初始焦点（Enter 无法确认）；input 焦点靠 150ms setTimeout 竞态 |
| BUG-P3-43 | `fe-render.js:197-215` + `style.css` | 灯箱无触摸捏合/滑动、无缩放至光标；侧栏拖宽仅 mouse；整站无 @media/prefers-reduced-motion |
| BUG-P3-44 | 全前端 | 全量硬编码中文 UI，混杂英文残留与 emoji，无 i18n 抽象层 |
| BUG-P3-45 | `fe-actions.js:208,277` | 智能文件夹下路径栏显示"智能文件夹: X"，回车仍按路径 loadPath → 必然报错 |
| BUG-P3-46 | `fe-render.js:184-185` + `fe-actions.js:180` | smart:// 虚拟路径下右键仍显示"新建文件夹/粘贴"，pasteFiles 无 smart:// 防护 → 执行必失败无反馈 |
| BUG-P3-47 | `fe-render.js:215` | 灯箱箭头导航只特判 video，.txt/.zip 会以 img 加载二进制 → 破图无提示、无跳过逻辑 |
| BUG-P3-48 | `index.html:196-226` | 排序/筛选菜单 a[href="#"] 未加 data-prevent → 每次点击向 URL 追加 # |
| BUG-P3-49 | `fe-actions.js:181` | $$('.btn-group button') 全局清 active，波及插件插入的 btn-group——脆弱选择器 |
| BUG-P3-50 | `fe-actions.js:610-611` | handleMenuDelete 中 needJump/jumpTarget 计算整段重复两遍（复制粘贴残留） |
| BUG-P3-51 | `style.css:21` | body 级 user-select:none → 列表文件名、属性面板路径无法手动复制 |
| BUG-P3-52 | `fe-actions.js:512` | 插件版本缺失时渲染 "vundefined"（escapeHtml 非字符串原样放行） |

**插件（11）**

| 编号 | 位置 | 问题（一行式） |
|---|---|---|
| BUG-P3-53 | `libav_player.py:314-316` | "跳帧过头保护"分支不可达（idx>index+fps 必先命中 :312 break）——ARCH-2 遗留死代码 |
| BUG-P3-54 | `video_pro/__init__.py:39` | EXT_VIDEO.add('.topos') 冗余（thumb_engine 已含） |
| BUG-P3-55 | `plugin_system.py:104-110` | 收集了 VERSION 但从不做宿主 API 版本兼容校验 |
| BUG-P3-56 | `plugin_system.py:21-27` | get_config() 无 key 时把宿主完整 config 交给任意插件，无命名空间隔离 |
| BUG-P3-57 | `plugin_system.py:95` | 所有插件均无 config.json → "disabled": true 机制实际是死路径 |
| BUG-P3-58 | `base_player.py:684-689` | 多 toast 叠加时各自捕获旧标题的 singleShot 会把更早标题"复活"覆盖新 toast |
| BUG-P3-59 | `player_qt.py:309,440` + `:72-83` | 裸 except ×2；paintEvent 不像基类画布那样 p.end() |
| BUG-P3-60 | `libav_player.py:290-294` | _idx_of 在 frame.time is None 时返回 None → 后续 TypeError，陷入 0.05s 退避死循环 |
| BUG-P3-61 | `image_editor/__init__.py:55-77` | /save 无大小上限（/read 有 128MB 护栏）、直接原地覆盖原图且非原子写 |
| BUG-P3-62 | `code_editor/__init__.py:55-94` | /read+/save 接受任意绝对路径无作用域限制（仅靠 SEC-1 本地 token 兜底） |
| BUG-P3-63 | `base_player.py:130` + `topos_player.py:37-39` | _ToposCanvas 别名、大包 re-export（noqa F401）为兼容保留——建议标注清理期限 |

### 1.5 各层整体评价（审计结论摘录）

- **后端**：鉴权模型完整且无缺口（41 条路由全覆盖 _guard_api + Host 校验 + 每进程轮换 token），上传白名单/应用白名单/文件名校验/穿越防护都有真实实现。剩余风险集中在四个簇：生命周期管理（BUG-P0-01）、静默吞错（约 15 处裸 except）、非原子写（除视频缩略图外全部直写）、无界缓存（DIR_CACHE/rating 队列/失败缓存只进不出）。
- **前端**：三模块架构方向正确，注释纪律远超平均水平。核心短板是重构完成度缺乏闭环验证（两个 P0 均为迁移半途回归）；错误处理整体走乐观路径；可访问性、触摸、i18n 基本空白。
- **插件**：ARCH-2 钩子全部被消费、无"空钩子+子类重复"残留，重构执行度高。真正风险在线程→UI 两条残留通路（BUG-P0-07/09）与 `_late_close` 违约；shutdown_timer 和 format_factory 是两个高风险插件；decode_benchmark 的线程管理是全部插件里最规范的（可作为范式）。

---

## 2. 专业素材管理软件功能差距分析

### 2.1 对标与现状定位

对标 Adobe Bridge（专业浏览+XMP）、Eagle/Billfish（个人素材库+标签检索）、Lightroom（目录+整理工作流）。file_explorer 当前定位是"文件浏览器 + 播放/转码增强"：**播放/编辑/转码是超出一般 DAM 的差异化长板**（三引擎播放、音频波形剪辑、ProRes 工厂、批量图片转码、Monaco 编辑器），**组织与检索是数量级短板**。

当前数据模型只有三个维度：0-5 星评分、扁平逗号 tags 字符串、自由文本备注；无标签表、无层级、无颜色标记、无收藏位、无集合、无回收站、无浏览历史持久化（仅 last_path 一个目录）。

### 2.2 T1 — 核心缺口（专业 DAM 必备，建议优先规划）

| 编号 | 功能域 | 现状 | 专业软件做法 | 建议方案 | 量 |
|---|---|---|---|---|---|
| GAP-01 | **标签体系 2.0** | 扁平逗号字符串；LIKE 子串匹配（"red" 命中 "hundred"）；单文件无标签编辑 UI（tags 只能通过批量面板或智能文件夹间接写入）；无标签管理 | Eagle/Billfish：层级标签 + 颜色 + 管理面板 + 自动补全 + 标签栏多选筛选 | 建 tags 表（id/name/color/parent_id）；file_meta↔tags 多对多；侧栏标签树 + 卡片角标；单文件属性面板加标签编辑（输入即补全）；标签栏 AND/OR 筛选；旧逗号 tags 一次性迁移 | L |
| GAP-02 | **全库搜索** | 搜索框只在**当前目录已加载文件**做前端子串匹配（fe-render.js:13-20）；无跨目录 API、无分词、无拼音、无历史 | Bridge/Eagle：全库即时搜索 + 条件组合 + 保存搜索 | SQLite FTS5 建 file_meta 虚拟表（path/name/tags/comments）；后端 /api/search?scope=dir\|library\|all&q=&type=&rating=&date_range=；前端搜索框升级范围选择器 + 高级筛选面板；搜索历史与"保存为智能文件夹"入口 | L |
| GAP-03 | **颜色标记 + 旗标** | 只有 0-5 星一个维度 | Bridge/Lightroom：红黄绿蓝紫 color label + pick/reject 旗标，评审工作流核心 | file_meta 加 color INTEGER + flag INTEGER 两列；卡片角标/筛选器/批量设置；reject 默认从视图过滤 | M |
| GAP-04 | **集合/相册** | 无任何虚拟组织，完全跟随文件系统目录 | Eagle/Billfish/Lightroom：跨目录虚拟集合，不移动文件 | collections + collection_items 两表；侧栏"集合"区；拖卡片入集合；集合内独立排序；导出清单（路径列表/CSV） | M |
| GAP-05 | **应用内回收站 + 撤销** | 删除直接 send2trash 进系统废纸篓且**同步删除 meta 行**——评分/标签即丢，应用内无法恢复；重命名/删除无 undo | 专业软件：应用内回收站（保留元数据可恢复）+ 会话内 undo | trash 表（path/meta 快照/original_location/deleted_at）；删除先入 trash，30 天自动清理；属性面板/工具栏"回收站"视图 + 恢复/彻底删除；会话内 undo 栈（重命名/移动/删除，Ctrl+Z） | M |
| GAP-06 | **元数据备份与 re-link** | 元数据以绝对路径为主键；用户在应用外移动/改名 = 元数据失联变孤儿行（仅智能文件夹查询时清理）；无导出/备份 | Bridge：XMP sidecar 随文件走；Lightroom：目录 re-link | ① /api/meta/export（JSON/CSV 全量备份）与 import；② re-link 扫描工具（按 文件名+大小+mtime 匹配孤儿行→新路径，批量确认）；③ 智能文件夹清理前自动导出孤儿行备份 | M |

### 2.3 T2 — 重要能力（显著提升日常效率）

| 编号 | 功能域 | 现状 | 建议方案 | 量 |
|---|---|---|---|---|
| GAP-07 | 时间线视图 | 无按时间组织的任何视图 | 第四种布局"时间线"：按天/月分组卡片流，组头带日期与数量，点击组头收起 | M |
| GAP-08 | 对比视图 | 无 | 灯箱模式扩展：选 2-4 张并排锁定缩放对比（调色选片场景） | M |
| GAP-09 | 重复文件查找 | 无 | 按内容 hash（MD5）分组 + 可选感知哈希（dHash 64bit）找相似图；批量勾选保留项 | M |
| GAP-10 | 视频悬停 scrub / 胶片条 | 缩略图只有单帧首图 | 卡片悬停按时间比例切换预生成帧（thumb_engine 抽 6-10 帧存 strip）；属性面板胶片条导航 | M |
| GAP-11 | PDF/文档预览 | **已断裂**：前端 PREVIEW_FORMATS 含 pdf，但 /api/thumb 对 .pdf 直接 204 → 灯箱拿不到图；文档/文本/压缩包被 should_skip_file 显式跳过 | macOS 用 qlmanage 生成 PDF 首页；跨平台用 PyMuPDF；txt/md/js 等用语法高亮渲染（code_editor 已有 Monaco 可复用） | M |
| GAP-12 | 幻灯片放映 | 无 | 灯箱扩展：全屏自动播放、间隔/过渡设置、按当前筛选范围 | S |
| GAP-13 | 面包屑导航 | 路径栏是纯文本输入框（可编辑但不可点） | 可点击的面包屑分段（每段下拉列同级目录）+ 复制路径按钮 | S |
| GAP-14 | 批量操作扩展 | 仅批量重命名 + 批量评分/标签 | 批量旋转（无损 EXIF orientation）、批量设为封面、批量移动到目录/集合、批量转码入口（复用 format_factory/image_factory） | M |
| GAP-15 | 跨平台设计稿缩略图 | ai/eps/psd/sketch/fig 仅靠 macOS QuickLook，Windows/Linux 基本失败 | PSD 用 psd-tools；SVG 原生渲染；ai/fig 用 ghostscript/官方导出兜底或明确降级为图标+格式徽标 | M |

### 2.4 T3 — 远期/锦上添花

| 编号 | 功能域 | 建议 |
|---|---|---|
| GAP-16 | 智能内容索引 | 本地模型自动标签（物体/场景）、OCR 全文（截图内文字可搜）、人脸聚类。可与现有 ai_library_browser/vault 服务整合而非重建 |
| GAP-17 | 地图视图 | 读取 EXIF GPS，按地点聚类展示（专业照片管理常见，素材管理可选） |
| GAP-18 | 联系表导出 | Contact sheet PDF/PNG（选片交付场景） |
| GAP-19 | i18n | 全前端硬编码中文 → 抽取 key/value + 语言包（与 BUG-P3-44 同源） |
| GAP-20 | 可访问性完整化 | alt/焦点管理/aria（与 BUG-P3-41/42 同源） |
| GAP-21 | 触摸/手势 | 灯箱捏合、列表滑动（与 BUG-P3-43 同源） |
| GAP-P1 | 规模化 | /api/list 分页（BUG-P2-10）、DIR_CACHE 淘汰（BUG-P2-03）、10 万+文件库冷启动优化——修复项与能力项重合，随 U2 批次解决 |

---

## 3. 升级路线图（批次规划）

原则沿用第一轮系统检查的"**实现 → 断言 → 实测 → 提交**"完整周期；每批独立分支提交，避免跨批耦合。批次顺序按"先止血、再加固、后升级"排列。

### U1 止血批（P0 全部 + 日常高频 P1）— 工作量 M ✅ 已完成（2026-09-07）

**目标**：消除全部崩溃/数据丢失/功能死亡路径，恢复 ARCH 系列重构的应有行为。

- 范围：BUG-P0-01 ~ P0-09（9 项）+ BUG-P1-01（double unquote，含 % 文件名必现）+ BUG-P1-03（重命名元数据迁移）+ BUG-P1-05/06/07/08（前端高危竞态与误导）+ BUG-P1-12（file_handlers 注入瘫痪风险）+ BUG-P3-38（closeCms 死引用，随对照检查顺手清偿）
- 验收：①重命名→选中→属性面板全链路无异常且树节点与磁盘一致；②音频截取实测出片；③含 `%20` 的目录名列目录/缩略图正常；④连续粘贴两次 + 关窗不崩溃；⑤同名上传有冲突提示；⑥恶意插件名（含单引号）注入后前端正常加载
- 附带：建立 data-action↔handler、FE 导出↔解构两张自动对照检查（防"迁移一半"复发）
- 完成记录：16 项修复全部落地；`test_fe_consistency.py` 两张对照检查通过（首轮曾真实抓到 P0-03 回归）；后端冒烟（%20 目录/上传冲突/重命名 meta 迁移/tojson 注入）与播放器离屏验证（toast 信号/EOF 信号/_late_close 两分支）全部通过。②④两项涉及真实 GUI 交互（波形拖拽出片、连续粘贴关窗）待人工复验。

### U2 数据层与生命周期加固批 — 工作量 M ✅ 已完成（2026-09-07）

**目标**：数据不再静默丢失，资源不再泄漏，缓存不再无界。

- 范围：BUG-P1-02（缩略图原子写）+ BUG-P1-04（DB 双库合并）+ BUG-P2-01~06,08,09,11~15（连接 finally 化、DIR_CACHE LRU+上限、rating 队列截断、CopyWorker 取消与退出等待、save_config 原子化、init_db 失败拒启）+ BUG-P2-07（网络卷 meta 误删——配合 GAP-06 的孤儿行备份）
- 验收：全后端 grep 无裸 `except: pass` 于数据路径；双库合并工具实测；杀进程重启 config.json 完好；拔卷状态下智能文件夹查询不清 meta
- 完成记录：P1-02 四条写图路径（波形/QuickLook/rawpy/Pillow）+ 视频多输出统一 tmp+`os.replace` 原子写；P1-04 延迟判定 + 自动合并（INSERT 独有行/空值回填，旧库留档 `.migrated-<ts>`）；P2 全部 15 项落地（含粘贴取消端到端接线：进度条按钮 → /api/action/paste_cancel → CopyWorker.cancel + 半成品清理）；孤儿行清理前 JSONL 备份。全部修复经隔离环境断言验证（合并语义/缓存上限/队列截断/NFD 上传/trim 冲突/init_db 拒启/原子配置）。

### U3 前端健壮性与体验基线批 — 工作量 M ✅ 已完成（2026-09-07）

**目标**：统一 fetch 层与错误呈现，收编竞态，补齐 loading/空态/错误态。

- 范围：封装 `feFetch()`（超时/abort/res.ok/toast/统一返回结构，配合 BUG-P3-16 后端返回结构统一）→ 收编 BUG-P2-20 全部裸 POST；BUG-P1-09/10 + BUG-P2-16~27；loading/空态/错误态三态模板（BUG-P3-40）；排序稳定性（BUG-P2-19）；applyFilterAndSort 集中重置 lastIndex/scrollTop/进度计数
- 验收：任意接口 500 时用户可见错误并可重试；快速切换目录/智能文件夹无内容漂移；大目录进度条收敛到 100%
- 完成记录：`window.feFetch`（超时中断 + res.ok + JSON 解析 + 失败 toast，silent 可选）收编 15 处裸 POST（播放/打开方式/系统打开/删除/复制/粘贴×2/添加库/取消固定/智能文件夹增删存/上传/定位/截图/重试缩略图/打开目录框），saveSmartFolder/deleteSmartFolder"500 仍报成功"的反向误导一并修正；P2-16 data-stop 捕获相位截断后手动收起 Bootstrap 下拉；P2-18 缩略图计数改 IntersectionObserver（进视口才计 total，进度条必然收敛）；P1-09 loadDeepInfo seq 令牌 + 键盘导航 200ms 防抖；P1-10 空列表守卫（openItem/onCardClick/navigateFile）+ lastIndex/scrollTop 集中重置；P2-19 三态比较器 + date 数值归一；P2-21/22/23/24/25/26/27 全部落地。三模块 JS 语法检查与两张一致性对照检查通过。

### U4 播放器与插件一致化批 — 工作量 M ✅ 已完成（2026-09-07）

**目标**：三引擎行为对齐，两个高风险插件脱险。

- 范围：BUG-P1-13~17,19（StoppedState、失败路径停旧会话、topos _on_eof、Up/Down 统一、音量条、截断文件提示）+ BUG-P1-18（shutdown_timer）+ BUG-P0-08 复验 + BUG-P2-28~35,38,39,41 + BUG-P3-53~60
- 验收：播放矩阵（.topos/ProRes/mp4 × 播放/暂停/seek/片尾/快照/截取/上下键/音量）三引擎行为一致；format_factory 转码中关窗不崩溃；shutdown_timer 全流程（设/取消/退出）无冻结无强制关机
- 完成记录：Up/Down 三引擎统一 Up→prev/Down→next（libav 反转，基类 tooltip 对齐）；基类新增音量条 + libav 接 QAudioSink（线程内应用，topos 无音轨自动隐藏）；topos 补 _on_eof 信号钩子 + 片尾按播从头重播；P1-14 打开新文件失败先停旧会话；P1-18 shutdown_timer 退出改走 stop_shutdown（异步 cancel + 状态复位，不再 freeze/强制关机）；P2-28 音频停止 join 1.5s→0.25s；P2-29 音频 seek 失败不再杀死音频线程；P2-30 路由缓存键含 mtime（原位重编码后正确重算；GUI 线程同步探测仍为已知限制）；P2-32/33/38/39/41、P3-53/54/55/56/58/59/60 全部落地；P3-57（config.json 停用机制）确认代码路径已实现、无需改动。全项经离屏 Qt/隔离环境断言验证；播放矩阵全引擎实测需 GUI 环境待人工。

### U5 组织能力核心批（标签 2.0 + 全库搜索 + 颜色/旗标）— 工作量 L ✅ 已完成（2026-09-07，收尾批同日落地）

**目标**：把"文件浏览器+"升级为真正的素材管理器。

- 范围：GAP-01（标签体系）+ GAP-02（FTS5 全库搜索）+ GAP-03（颜色标记/旗标）
- 数据模型变更：tags/collections/collection_items 新表 + file_meta 加 color/flag 列 + FTS5 虚拟表 + 旧逗号 tags 迁移脚本（带断言与回滚）
- 验收：①给 1000 个文件打层级标签后，按 标签 AND 类型 OR 评分 组合筛选 <500ms；②全库 5 万文件搜索"落日"命中文件名/tags/comments 且支持拼音首字母；③颜色/旗标在卡片角标、筛选器、批量操作、智能文件夹规则四处联动；④迁移脚本幂等且旧数据零丢失

**已完成（2026-09-07，未提交前最后状态 = 工作区）**：

- 数据模型（db.py init_db，全部幂等）：tags 表（含 color/parent_id 预留层级）+ file_tags 多对多；file_meta 增 color(0-5)/flag(0无1旗标2拒收) 列；FTS5 trigram 虚拟表 + AI/AU/AD 三触发器自动同步 + fe_markers 打点的一次性全量回填；旧逗号 tags 幂等迁移进 tags/file_tags（legacy 列保留双写，旧路径零影响）。重复执行迁移已验证零重复零丢失。
- 后端 API（routes.py）：`/api/tags` GET/POST、`/api/tags/<id>` PUT/DELETE（含计数聚合）；`/api/meta/tags` 单文件标签增删（未知名自动创建、空标签引用回收、legacy 列双向同步）；update_meta/batch_meta 扩展 color/flag（值域校验，非法显式报错）；`/api/list` 与智能查询 payload 下发 color/flag；`/api/search` 全库搜索——FTS trigram（≥3 字符）未命中或不可用自动降级 LIKE、空格分词多词 AND、tag= 精确标签检索、type/color/flag 组合过滤、2000 条上限保护。
- 前端：属性面板新增 标签编辑器（chips ✕ 移除 + datalist 自动补全回车添加）与 颜色色板/旗标按钮（即时刷新卡片角标）；侧栏新增"标签"区（计数 + 颜色点，点击=按标签全库检索，右键=删除标签）；筛选菜单新增 颜色/旗标 维度（含"隐藏拒收"评审过滤）；批量元数据面板新增 颜色/旗标；卡片角标覆盖颜色圆点+旗标/拒收徽标（图片/视频/文档/音频/图标五类卡全覆盖）；搜索框新增"目录/全库"范围切换器，全库走 search:// 虚拟视图（goUp/清除搜索有对应退出路径）。
- 验证：后端迁移幂等/标签 CRUD/双写一致/值域校验/搜索七类用例（FTS 英文、LIKE 中文短词、多词 AND、标签、颜色+旗标组合、移除后归零）全部通过断言；三模块 JS 语法 + 两张一致性对照检查通过。

**收尾批完成记录（2026-09-07 第二次提交）**：

1. **智能文件夹规则联动 color/flag（验收③第四处联动补齐）**：`execute_smart_query` 新增 q（名称/标签/备注子串，与 /api/search 同语义）/ tag（file_tags 精确等值，规避 LIKE "red" 命中 "hundred"）/ color（1-5 值域校验）/ flag（pick/noreject/reject）四类规则；智能构建器规则行同步扩为 关键词/标签（精确）/类型/评分/颜色/旗标/标签包含（旧） 七类，旧规则值兼容回显。
2. **搜索历史 + "保存为智能文件夹"（GAP-02 收尾）**：全库搜索（含标签检索）入历史（localStorage，去重置顶，上限 12 条）；搜索框聚焦弹历史面板（单条删除/清空/回放）；search:// 视图显示"保存为智能文件夹"按钮，把当前 q/tag 转成同语义规则预填构建器——search 与 smart 双向已断言同语义。
3. **性能验收**：新脚本 `test_u5_finish.py`（隔离临时库）22/22 通过——组合筛选 1000 库 61ms（<500ms ✅）、智能查询组合规则 47ms、5 万行库 FTS/LIKE 搜索"落日" 155ms（<2s ✅，命中 2000 截断）、单词精确命中 2ms。
4. **验收测试抓出并修复两个真实后端缺陷**：① /api/search 元数据批量补齐在分块循环内赋值覆盖 `meta_map`，>500 条命中时只有最后一块的评分/颜色生效（组合筛选凭空丢一半结果）；② 命中集 `set`→`list` 受进程哈希随机化影响，`[:2000]` 截断子集每次会话不同（同一搜索两次结果不一致）——截断前排序修复。
5. 遗留：拼音首字母检索（需 pypinyin 依赖决策）随 GAP-16 一并评估；前端 GUI 实测（标签补全/色板/search:// 叠加筛选）待人工。

### U6 安全网批（回收站 + 撤销 + 元数据备份）— 工作量 M ✅ 已完成（2026-09-07）

- 范围：GAP-05（应用内回收站 + Ctrl+Z 撤销）+ GAP-06（元数据导出/导入 + re-link 扫描）
- 验收：删除→回收站→恢复后评分/标签/缩略图全在；重命名 Ctrl+Z 还原且树同步；re-link 对"外部改名"的 500 文件批量找回；导出 JSON 重装后导入全量还原
- 完成记录：新增 `trash.py` 模块 + trash 表（meta 快照/原路径/删除时间）；删除改为移入 `<fe>/.trash/`（file_meta 行快照后移除防幽灵记录，FTS 触发器自动同步；跨卷移动失败回退系统废纸篓并标记不可应用内恢复）；`/api/trash` 列表 + `/api/trash/restore`（原路径占用自动"(恢复)"副本、目录后代 meta 前缀平移还原）+ `/api/trash/purge`（物理删除 + 三尺寸缩略图清理）+ 启动/每小时节流的 30 天过期清理；侧栏"回收站"入口 → trash:// 虚拟视图（恢复/彻底删除/清空，goUp/粘贴有守卫）。会话内撤销栈（上限 20）：删除 → /api/trash/restore 批量恢复；重命名 → 反向 rename；Ctrl+Z 快捷键 + 设置菜单"撤销"入口。GAP-06：`/api/meta/export` 全量 JSON 下载、`/api/meta/import`（merge 只回填空值/replace 整行覆盖、失联路径跳过、导入后清目录缓存）、`/api/meta/scan_orphans`（库内同名唯一候选匹配，卷不可达不算失联，2 万文件扫描上限）+ `/api/meta/relink`（meta 路径改写 + 缩略图迁移）+ 设置菜单"失联元数据找回"模态框（全选/全不选/批量找回）。验收测试 `test_u6_safety_net.py` 24/24 通过（隔离环境）：删除入站无幽灵行、恢复后评分/标签/颜色/旗标/结构化标签/缩略图全在、目录+后代 meta 还原、占用冲突副本、过期清理、导出导入 merge 语义、re-link 端到端。范围说明：撤销栈覆盖 重命名/删除 两类（粘贴为复制语义、无移动操作，无 Undo 对象）；"外部改名 500 文件批量找回"以同名唯一候选匹配实现（Size/mtime 维度受 cached_info 存储限制未纳入签名）。

### U7 视图与工作流批 — 工作量 M ✅ 已完成（2026-09-07）

- 范围：GAP-07（时间线）+ GAP-08（对比视图）+ GAP-09（重复查找）+ GAP-10（视频 scrub）+ GAP-11（PDF/文档预览修复）+ GAP-12（幻灯片）+ GAP-13（面包屑）+ GAP-14（批量操作扩展）+ GAP-15（跨平台设计稿兜底）
- 验收：PDF 双击可预览；时间线按月分组正确；重复查找对 1 万图库 <30s；悬停 scrub 帧切换 <100ms
- 完成记录（全部经 `test_u7_views.py` 26/26 + 前端静态断言验证）：
  - **GAP-11**：`should_skip_file` 放行 .pdf（此前整个 EXT_DOCS 跳过 → 前端 PREVIEW_FORMATS 含 pdf 却永远拿不到图）；macOS 走既有 QuickLook，跨平台新增 PyMuPDF 兜底；灯箱新增 txt/md/csv 内联渲染（md 用已内置的 marked 渲染）——二进制破图问题（BUG-P3-47 一部分）一并解决。
  - **GAP-13**：可点击面包屑（真实目录替换路径输入框；分段点击跳转、箭头拉起同级目录下拉、复制路径、铅笔回编辑模式；smart:// search:// trash:// 自动回退输入框）。
  - **GAP-12**：灯箱幻灯片放映（1/2/3/5s 间隔、播放/暂停、仅图片轮播、手动导航重置计时、关闭自动停止）。
  - **GAP-07**：时间线视图（第四种布局；按月分组组头带日期与数量、点击收起；时间线下强制日期排序防分组交错；懒加载分批渲染兼容组结构）。
  - **GAP-08**：对比视图（选中 2-4 张并排，滚轮锁定缩放、拖拽同步平移、ESC/按钮关闭）。
  - **GAP-09**：`/api/duplicates/scan` 体积预分组 + 仅同体积组做 MD5（1 万文件实测 0.4s ≪30s；同体积不同内容不误判）；重复查找模态框（每单选保留一份、其余移入回收站可撤销）。
  - **GAP-10**：`/api/thumb/strip` 六帧横排胶片条（ffmpeg fps+tile，缓存源目录 .cache，缓存直读实测 0.7ms）；卡片悬停按比例切帧（纯样式更新）、属性面板胶片条逐帧预览。
  - **GAP-14**：批量旋转（JPEG 显式量化表 qtables 逼近无损、EXIF 保留；注意 Pillow 的 quality/subsampling='keep' 只对同对象重存有效）；批量移动（同名自动 (n) 后缀、meta/缩略图随迁）；选中工具栏新增 对比/旋转/移动/格式工厂转码入口。
  - **GAP-15**：PyMuPDF（PDF）/psd-tools（PSD）/ghostscript（ai/eps）三级跨平台兜底（库缺失静默退回原行为）；SVG 缩略图直接回源由浏览器渲染。
  - 遗留：感知哈希（相似图）未做（规划"可选"）；批量转码仅入口按钮（复用 format_factory 现有 GUI）；视频 scrub 首次悬停需等条带生成（数秒，之后 <100ms）。

### U8 平台化与远期批 — 按需 🔄 部分完成（2026-09-07，P3 快速清偿 + 插件硬化小项随批落地）

- 范围：GAP-16~21（智能索引/地图/联系表/i18n/a11y/触摸）+ BUG-P3-36~52 清偿 + BUG-P3-30~35 插件系统硬化（版本校验/命名空间/卸载回滚）
- 触发条件：i18n 在有英文用户需求时启动；智能索引待 vault 服务方向明确后整合
- **本批已清偿**（随 U7 批顺手落地，全部经回归验证）：
  - BUG-P3-31/36（插件）：os.listdir → sorted，加载顺序跨平台确定（同名扩展名注册归属不再漂移）
  - BUG-P3-32：/api/manager/plugins 插件 config 脱敏下发（token/secret/password/api_key → ******）
  - BUG-P3-34：update_meta rating 值域校验（0-5 整数，此前字符串/负数可静默入库）
  - BUG-P3-36（缓存）：JSON/204 响应统一 no-store（媒体端点显式 max-age 不受影响）
  - BUG-P3-45：虚拟视图路径栏回车不再必然报错（识别"智能文件夹:/全库搜索:/标签:/回收站"文案与 smart:// search:// trash:// 前缀）
  - BUG-P3-46：smart:// 等虚拟视图右键"新建文件夹"拦截（粘贴守卫已在 U6 落地）
  - BUG-P3-47：灯箱箭头导航跳过不可呈现文件（.zip/.docx 不再按 img 加载破图；视频转交播放器、文本/MD/PDF 可停留）
  - BUG-P3-48：排序/筛选菜单 17 处 a[href="#"] 补 data-prevent（不再向 URL 追加 #）
  - BUG-P3-49：switchLayout 按钮组选择器收敛到布局按钮组（不再全局清 active 波及插件插入的 btn-group）
  - BUG-P3-50：handleMenuDelete needJump/jumpTarget 重复计算删除
  - BUG-P3-51：文件名/属性面板/文本预览等关键区域放行 user-select:text
  - BUG-P3-52：插件版本缺失不再渲染 "vundefined"
  - （P3-33 load_config 损坏日志、P3-38 closeCms、P3-39 删除文案、P3-55 版本校验、P3-56 get_config 命名空间 均已在 U1~U4 批完成）
- **仍按触发条件暂缓**：GAP-16 智能索引（待 vault 方向）、GAP-17 地图、GAP-18 联系表、GAP-19 i18n（待英文用户需求）、GAP-20/21 a11y 与触摸（与 P3-37/41/42/43/44 同源，另立批次）、P3-30 插件信任边界文档（需运维决策）、卸载回滚 API（P2-37 遗留，随插件系统专项）。

### U9 审计修复批（U5–U8 新代码交叉审计）— 工作量 M ✅ 已完成（2026-09-07）

对 U5–U8 新增代码做第二轮交叉审计（双路 Explore：新代码找 bug + 计划修复项逐条落地核对），抓出 1 个数据丢失级、2 个数据一致性级缺陷与一批漏网问题，全部修复并新增 `test_u9_audit_fixes.py`（42 断言）验收。

**数据安全（P0/P1）**：
1. **回收站同秒同名覆盖（数据丢失）**：`_unique_trashed_name` 仅用秒级时间戳做后缀且无存在性检查——同秒删除两个同 basename 文件（批量删除/重复清理的常见场景）第二个静默覆盖第一个。补存在性循环后缀。
2. **FTS 幽灵行（搜索索引污染）**：`INSERT OR REPLACE INTO file_meta`（update_meta/import replace）的隐式 DELETE 因 `recursive_triggers` 默认 OFF 不触发 fts_ad 触发器——每次改评分/备注都堆一条同 path 旧行，旧文本仍可被搜索命中、索引只增不清。`_db()` 全局开启 recursive_triggers + 回填标记升 v2（一次性重建清掉存量幽灵行）。
3. **restore 非原子**：文件先移出 .trash、meta 回写失败仅记日志——留下"文件已恢复但 trash 行还在"的僵尸记录（后续 purge 还会误删刚恢复文件的缩略图）。改为回滚 + 文件移回回收站 + 显式报错。

**标签一致性（P2）**：
4. rename / batch_rename / batch_move / meta re-link 四处路径迁移此前只改 `file_meta.path`，不迁移 `file_tags.file_path`——结构化标签改名后失联、标签计数虚高。四处全部补齐（trash 路径 U6 已有）。
5. `_delete_permanent` 补 file_tags 清理 + 零引用标签回收；目录/后代删除的 LIKE 前缀补 `%`/`_` 转义（`my_dir` 此前会误删 `myXdir` 的 meta）。

**其它修复**：marked 渲染未消毒 XSS（预览恶意 .md 可执行脚本，页面持有 pyBridge）→ 内置 DOMPurify 3.2.4 本地化 + 消毒后入 innerHTML；批量旋转补 EXIF Orientation 处理（先 `exif_transpose` 摆正再叠加旋转、Orientation 归 1，修复 iPhone 直出照片双重旋转）+ 三尺寸缩略图删除 + 目录缓存失效（旧方向图此前最长滞留 1 小时）；批量移动改单连接 finally 化 + 全部源目录缓存失效；execute_smart_query / /api/search 补齐段连接 finally 化；`/api/duplicates/scan` 加上限（5 万文件 / 2GB 哈希预算，返回 truncated）；胶片条路径含扩展名（a.mp4 与 a.mov 此前共用一张 strip）；前端 openTrashView 补 _loadSeq 竞态令牌、undo 栈死条目不再永久卡栈（记录不存在类错误出栈）、purge/empty 后剔除撤销栈对应条目、reloadFiles 对 trash:// smart:// search:// 分流（此前刷新按钮必报错清空视图）、重复文件删除后重绘分组（此前可对已删路径重复提交）。

**计划核对补漏（此前声称完成实际未落地的 5 项）**：BUG-P3-12（大小写不敏感卷仅改大小写的重命名 samefile 放行）、P3-13（批量重命名独立编号计数器，不再跳号）、P3-14（/api/file 目录请求 400）、P3-15（open_system/open_with/reveal 失败不再静默报 ok）、智能查询 LIKE 补 ESCAPE 转义（P3-18 后半，与 P3-19 的 db.py 侧批量自愈配合）。

**顺手清偿**：存量 `test_library_optimizations.py`（仍引用 ARCH-13 已删除的 script.js）与 `test_thumb_fix.py`（断言 .pdf 跳过缩略图，与 U7/GAP-11 行为变更冲突）更新至当前架构；ffmpeg PNG 回落路径补原子写 + 临时文件清理；purge 与 restore 并发窗口的 FileNotFoundError 不再误入 send2trash 分支。

**验收**：`test_u9_audit_fixes.py` 42/42（含 commit 故障注入验证 restore 回滚路径）；U5 22 / U6 24 / U7 26 / 一致性检查 全量回归通过。U8 暂缓项维持原触发条件不变（GAP-16~21、P3-30、插件卸载回滚 API）。

### U10 第二轮交叉审计批（全量复核）— 工作量 M ✅ 已完成（2026-09-07）

对 U1–U9 全部完成状态做三路并行深度复核（后端 / 前端 / 插件），逐条核对各批完成记录与实际代码，抓出 **4 个数据丢失级（P1）+ 一批 P2/P3** 缺陷与多处"计划声称完成但实际有缺陷"项，全部修复并新增 `test_u10_audit2.py`（57 断言）验收。

**数据安全（P1）**：
1. **智能文件夹命中孤儿行必 500**：`_backup_orphan_rows` 按 5 名解包 U5 起的 7 列行（color/flag）→ ValueError 逃出（只捕 OSError），任何含"文件已删 meta 残留"行的智能文件夹整体打不开。改按索引取值 + 调用点兜底 try。
2. **目录重命名不迁移后代**：api_rename 只做精确路径 UPDATE——重命名任何包含已打标文件的目录，其内全部 rating/tags/color/flag 变孤儿行（Ctrl+Z 也救不回）。补 LIKE 前缀平移 file_meta + file_tags（后代缩略图随目录内 .cache 自动迁移）。
3. **restore "(恢复)" 副本无唯一循环**：同一原路径 ≥3 条回收站记录批量恢复时 `shutil.move` 静默覆盖刚恢复的文件。补计数循环（"(恢复) (n)"）。
4. **trash 入库失败文件蒸发**：文件已移入 .trash 但 INSERT 失败（锁/磁盘满）只报错不回滚——文件躺在无记录的槽位里（视图不可见、过期清理永不触及）。补回滚移回原位；移回失败才降级报错带暂存路径。
5. **format_factory 同名转码销毁源文件**（插件 P1）：del_orig 时 `os.rename(tmp, final)` 在 POSIX 上对已存在目标（含 final==src 的 .mov→.mov 与 Remux 同扩展名场景）是原子替换而非报错——源文件字节被成品覆盖后 `move_to_trash(src)` 又把成品丢进废纸篓（双重丢失）。新增 `Utils.finalize_output` 安全落位（占用者先入废纸篓、失败改后缀名）；`move_to_trash` 修正 osascript 非零退出仍报成功的假返回。

**回收站与并发**：purge 目录此前 `os.remove` 必抛 → 目录"彻底删除"恒进系统废纸篓（不释放空间、过期目录无限堆积），改 rmtree 分流；同秒同名删除的 TOCTOU 补 `O_CREAT|O_EXCL`/`mkdir` 独占占位（目录与文件两种占位方式，验收含并发线程实测）；并发粘贴 `copy_finished` 取"最新 worker"改为按信号发送者绑定（退出等待失效 = P0-01 崩溃面回归）。

**一致性**：标签改名/删除回写 legacy 逗号串（原"同步"代码是改名后读取且从未使用的死代码——FTS 旧文本仍可命中、卡片 chips 滞留旧名）；改名撞已有标签名 400；智能文件夹孤儿清理补 file_tags + 零引用标签回收（计数不再虚高）；`api_batch_meta`/孤儿清理块连接 finally 化；`db.init_db` 异常路径关连接。

**搜索与缩略图**：/api/search 截断从"过滤前"移到"过滤后"（2050 命中 + color 过滤的组合筛选此前可静默返回 0；total 改为截断前计数）；/api/search 与 execute_smart_query 补请求级 scan_cache（消除逐文件全目录 scandir 的 O(N²)）；`get_thumb_path` 只读源目录回退全局 hash 缓存（原 is_local 判定是死代码，只读卷缩略图永远生成失败且每小时重试）；filmstrip 文件名去 `replace('.','_')` 非单射（clip.v2.mp4 与 clip_v2.mp4 撞条带）；strip 失败缓存专属 `:strip` 键（普通缩略图失败不再连坐、strip 失败不再每次悬停重跑 ffmpeg）；`_generate_ffmpeg` WebP 直出分支补原子写。

**元数据备份**：导出升 v2（补 tags 表定义与回收站快照——此前"全量备份"重装导入后标签颜色/回收站元数据尽失）；导入 rating/color/flag 值域钳制（损坏备份可写 color=99）；trash 快照仅物理文件在本机时导入。

**前端**：时间线懒加载跨批次 groupEl null 崩溃（月份组跨 100 张批次边界必现，从 DOM 恢复组容器）；背景右键"粘贴"补虚拟视图守卫（trash:// 等串直接发给 CopyWorker 会按 CWD 落盘）；openSmartFolder 补搜索词/缩略图计数清理（残留搜索词二次过滤结果）；search:// 视图跳过本地整句子串过滤（多词 AND 语义不再被阉割）；主题选择器 `CSS.escape`（BUG-P1-11 至今未修的漏网——"Tahoe 26" 主题切换抛 SyntaxError 不持久化）；purge/empty 失败不再无条件清撤销栈；U5 新增 9 个筛选项补 data-prevent；IntersectionObserver 清场 unobserve（跨目录导航节点无限滞留）；对比视图校验可见文件数 + 屏蔽底层全局快捷键（Delete 不再隔空删文件）；搜索历史非数组 JSON 防护；灯箱 txt/md 竞态令牌 + ESC 停幻灯片；拖拽上传虚拟视图拦截；strip URL 单引号 %27 转义；回收站条目 width:100%（grid 模式不再并排挤压）；filter 按钮态补颜色/旗标维度。

**播放器/插件**：libav 音量改音频线程内应用（原 GUI 线程直接跨线程调 QAudioSink，与注释矛盾；换文件继承滑杆音量）；`display_size` 改 GUI 线程维护的缓存（解码线程不再触碰 QWidget 几何）；基类与 player_qt 的 toast 改"真实标题 + 代次令牌"还原（两次叠加 toast 不再把 toast 文案当标题永久残留）；`_on_decode_stop` 复用 EOF 收尾（截断文件停止后音频不停/图标不复位）；`_open_session` 失败路径停定时器复位状态；format_factory `_CardLoaderThread` 纳入关停协议（探测中关窗析构 QThread）+ 重复拖入先停旧线程 + 停止/失败路径清理 `._cvt_*/._remux_*` 半成品 + 输出冲突不再静默覆盖；image_factory 进程池 BrokenProcessPool 不再逃逸（UI 永久"正在处理"）；shutdown_timer 宿主退出前同步取消已排定关机（OS 级命令不随进程退出撤销）、到期分支与最后 1 秒取消的竞态防护、slider 恢复、QTimer 复用；`test_playback_matrix.py` 硬编码旧仓库路径改相对定位。

**验收**：`test_u10_audit2.py` 57/57（含并发删除实测、2050 行截断后置实测、只读目录回退实测、故障注入回滚）；U5 22 / U6 24 / U7 26 / U9 42 / 一致性检查 / 存量测试全量回归通过（U5/U6 两处断言随语义改进更新：search total=截断前命中数、export v2）。

### 优先级矩阵（推荐执行序）

```
U1 止血 ──► U2 数据加固 ──► U3 前端基线 ──► U5 组织核心（可与 U4 并行）
                │                                │
                └──► U4 播放器一致化 ◄───────────┘
                                 ┌───► U6 安全网 ───► U7 视图工作流 ───► U8 平台化 ───► U9 审计修复
```

---

## 4. 继承的存量问题（非本轮发现，来自第一轮检查遗留）

1. tests/ui + tests/app 存在 28 个存量失败（AI result card / keyframes / composite entry source 区域），经 worktree 基线验证早于第一轮工作，建议另立批次。
2. tests/ 全量单进程存在原生崩溃（Illegal instruction/Segmentation fault，崩点不定），为存量环境问题，目前以分目录运行规避。
3. hdri/INDREF-023.jpg（8.8MB）转 Git LFS 需基础设施决策，暂缓。
4. R3 其余项（features/models 空壳目录清理、scripting 模块接线决策、module_loader 删除决策）按特性开发渐进处理。
5. pyflakes 已 pip install 进 .venv 但未写入 pyproject（venv 与 pyproject 轻微不同步）。

---

## 5. 审计方法与证据说明

- 四路并行 Explore 审计（后端 23 次工具调用 / 前端 21 / 插件 45 / 功能盘点 40），全部发现要求 file:line 证据。
- 人工复核：P0 全部 9 项、P1 中影响日常使用的 8 项（double unquote、导出清单、saveTrim 选择器、_late_close、file_handlers 注入、Up/Down 方向、双库并存、粘贴线程顺序）已逐条读码确认，其中双库分叉有文件系统现场证据（topos.db 9/6 与 metadata.db 8/28 并存）。
- 行号以 `636e60d5` 为准，后续提交可能漂移，以问题指纹（函数名+代码片段）为锚。

## 6. 版本历史

- 2026-09-07：初版。四路审计 133 项缺陷 + 21 项功能差距 + 8 批升级路线图。
- 2026-09-07（第二批）：U10 全量复核批——三路交叉审计抓出智能文件夹孤儿行 500、目录重命名丢元数据、restore 覆盖、trash 入库失败文件蒸发、format_factory 同名转码销毁源文件等 4+1 个数据丢失级缺陷 + 50 余项 P2/P3，`test_u10_audit2.py` 57 断言验收通过，全部存量测试回归通过。
- 2026-09-07：U1–U8 批全部落地（U8 按触发条件部分暂缓）；同日追加 U9 审计修复批——U5–U8 新代码交叉审计抓出回收站同秒同名覆盖（数据丢失）、FTS 幽灵行、restore 非原子、file_tags 迁移缺失、marked XSS 等 16 项漏网缺陷 + 5 项计划核对补漏，`test_u9_audit_fixes.py` 42 断言验收通过。
