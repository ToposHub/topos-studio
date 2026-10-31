# file_explorer 升级优化计划

> 审计基线：commit `e5aeddd8`（2026-09-05）。行号基于该时点，后续改动会漂移，以符号名为准。
> 审计方式：三路并行深度审计（后端服务 / 前端 Web / 集成层与播放插件），关键结论（如 werkzeug 死锁语义、git 跟踪状态）均在本机 `.venv` 动态核实，非推测。
> 工作量标记：S ≤0.5 天｜M 1~2 天｜L ≥3 天。

---

## 0. 总体结论

| 维度 | 评价 |
|---|---|
| 功能完整性 | 好。缩略图管线（刚优化）、三引擎播放、插件体系、双模式运行均已成型 |
| 安全性 | **差**。本地服务无鉴权 + 通配 CORS + 3 个输入未校验的写入/执行端点，浏览器里任意恶意网页可读全盘文件、写任意路径 |
| 稳定性 | 中。存在 2 个确定性死锁/泄漏路径（FlaskWorker 停止、UltimatePlayer 实例泄漏）和 2 个原生崩溃风险点 |
| 架构健康度 | 中下。main.py 1581 行混杂 8 种职责；三播放器约 500-600 行复制代码已产生行为漂移；通知链路（rating/缩略图）三条半残 |
| 前端质量 | 中。985 行单文件 + 40 个全局变量；无虚拟滚动；一处监听器泄漏；HTML 注入面未收口 |

---

## 1. P0 — 安全加固（建议立即做，合计约 2~3 天）

本地 `127.0.0.1` 服务**不等于安全**：GET/multipart 是"简单请求"，用户浏览器里任何恶意网页无需 CORS 预检即可直接打这些接口（drive-by）。嵌入主应用时服务常驻，风险窗口是全量的。

| ID | 严重性 | 问题 | 位置 | 修复方案 | 工作量 |
|---|---|---|---|---|---|
| SEC-1 | **P0** | `/api/file` 任意文件读取，且响应带 `Access-Control-Allow-Origin: *`——恶意网页可 `fetch('http://127.0.0.1:8188/api/file?path=/Users/x/.ssh/id_rsa')` 直接拖走全盘文件 | main.py:1019-1041 | 启动时生成随机 token，注入模板并在所有 `/api/*` 校验（参数或 header）；移除 `*` CORS；校验 Host 头（防 DNS rebinding） | M |
| SEC-2 | **P0** | `/api/upload` 文件名未净化，`filename="../../x.sh"` 可写任意路径（multipart 可 no-cors 发送，绕过 CORS） | main.py:1043-1065 | `secure_filename()`；`target_path` 白名单（限上次浏览目录）；随 SEC-1 一起鉴权 | S |
| SEC-3 | **P0** | `/api/open_with` 接受任意 `app_path` 直接 `subprocess.run([app_path, file])`——本地任意程序执行原语 | utils.py:322 | 应用白名单仅限 APP_CACHE 扫描结果；路径校验存在性 | S |
| SEC-4 | P1 | `/api/rename` 的 `new_name` 含 `/` 可跨目录移动文件；`/api/mkdir` 可建多级目录树 | main.py:624-630, 1185 | 校验 `os.path.basename(name) == name` | S |
| SEC-5 | P1 | `/static_plugin/<path:filename>` 可 `../` 目录穿越读任意文件 | main.py:1227-1238 | 改用 Flask `send_from_directory`（自带穿越防护） | S |
| SEC-6 | P2 | `/api/audio/trim` 参数未校验直接拼命令 + `subprocess.run` 无 timeout，ffmpeg 挂死则永久占用 Flask 线程；dst 可写任意路径 | main.py:1150-1169 | 参数类型校验、`timeout=60`、dst 限制在源目录内 | S |
| SEC-7 | P2 | Python→JS 注入：`trigger_js` 系列只转义单引号，路径含换行/反引号即产生非法或注入 JS；页面又开着 `LocalContentCanAccessFileUrls` | main.py:1482-1532, files_panel.py:188,210 | 抽公共 `_js_quote()`（`json.dumps` 即可）全链路替换 | S |

**验收**：恶意网页 PoC（读文件 / 写文件 / open_with 执行）全部失败；正常功能回归通过。

---

## 2. P1 — 崩溃 / 死锁 / 泄漏（第二批，合计约 3~4 天）

| ID | 问题 | 位置 | 修复方案 | 工作量 |
|---|---|---|---|---|
| CRASH-1 | **FlaskWorker.stop() 确定性死锁**：`run()` 用手动 `handle_request()` 循环从未调 `serve_forever()`，而 `stop()` 调 `server.shutdown()` 会永久等待只有 `serve_forever` 才置位的事件 → GUI 线程挂死。已有注释承认"stop_server() 可能会阻塞"并绕开（menu_toolbar_controller.py:710） | adapter.py:74-91,158-169 | `run()` 改 `serve_forever(poll_interval=0.5)`；stop 路径回归可用 | S |
| CRASH-2 | **服务器清理链断裂**：`adapter.stop_server()` 挂在 FilesPanel.closeEvent 里，而 FilesPanel 装在 QDockWidget 中，dock 关闭/退出**不触发**子控件 closeEvent；主窗口 closeEvent 注释错误假设它会触发 → 退出时 Flask 线程/缩略图线程全靠进程回收 | files_panel.py:181-184, file_controller.py:6376-6377 | stop_server 挪到 MainWindow closeEvent / aboutToQuit；先修 CRASH-1 | S |
| CRASH-3 | **LibavPlayer 缺孤儿保护**：`stop_decoder` join(2s) 超时后无条件 `session.close()`，线程若仍卡在 `frame_rgb`（慢盘 seek）即 use-after-close 原生崩溃。ToposPlayer 有完整的延迟回收逻辑，Libav 没抄到 | libav_player.py:553-568 | 移植 topos_player.py:448-466 的 orphan + `_late_close` 模式 | S |
| CRASH-4 | **ToposPlayer 截图/封面与解码线程并发使用同一非重入会话**：`snapshot()`/`_set_as_cover()` 在 GUI 线程直接调 `frame_rgb()`，解码线程同时写共享缓冲——帧撕裂最坏原生崩溃（libtopos 句柄不自带锁） | topos_player.py:472-490,600-623 | `ToposMovieSession` 加 `threading.Lock` 包住 `frame_rgb/close` | S |
| CRASH-5 | **UltimatePlayer 每次播放泄漏一个完整实例**：`player is None or not player.isVisible()` 为真就新建覆盖引用，旧实例（QMediaPlayer+AudioOutput+画布）永不清除 | file_controller.py:4176-4181 | 持久复用（与 topos/libav 引擎一致），覆盖前 `deleteLater()` | S |
| CRASH-6 | **FilesPanel 加载失败无限重试 + 陈旧 URL**：失败重试用的 `_server_url` 是启动时缓存的旧端口；`start_server()` 换端口后永不自愈，重试无上限无退避；错误页 100ms 后被无条件覆盖 | files_panel.py:93,111-129 | 重试前刷新 URL；加次数上限 + 指数退避；保留错误页；顺带接 `renderProcessTerminated` 崩溃恢复 | M |
| CRASH-7 | 连续粘贴时 `self.copy_thread` 被覆盖，运行中 QThread 可能被 GC（"QThread: Destroyed while running" 崩溃） | main.py:1507-1521 | 进行中线程持列表管理，或粘贴中禁止再次触发 | S |

---

## 3. P1/P2 — 功能性 bug（第三批，合计约 4~6 天）

### 3.1 后端

| ID | 问题 | 位置 | 修复方案 | 工作量 |
|---|---|---|---|---|
| FUNC-1 | `/api/list` 目录里只要有一个断链 symlink，`entry.stat()` 抛错 → **整个目录显示"无法访问"** | main.py:563-570 | per-entry try/except 跳过坏条目（或 `stat(follow_symlinks=False)`） | S |
| FUNC-2 | 独立模式硬编码加载 `127.0.0.1:8188`，端口漂移后白屏 | main.py:1449 | 改用 `FLASK_PORT` 变量 | S |
| FUNC-3 | `CONFIG_FILE="config.json"`、DB 回退路径是相对路径——CWD 不同则配置/元数据分叉丢失（目录里已有一份历史分叉的 metadata.db） | main.py:53-54 | 一律基于 `__file__` 目录或显式注入 | S |
| FUNC-4 | 智能文件夹把 docs/code/archive 全归类为 `'image'`（复制粘贴 bug），类型过滤与图标错乱 | main.py:1287 | 分别映射 doc/code/archive；并把 `classify_ext()` 抽公共函数（与 list_files 共用，见 ARCH-3） | S |
| FUNC-5 | `thumbnail_pending` 按 path 去重不分尺寸：图片类 small 在队时请求 large 永久丢失（视频已全尺寸同产不受影响） | main.py:1007-1010 | pending 键改 `(path, size_type)` | S |
| FUNC-6 | 缩略图 rename/删除只迁移 `['small','large']`，**preview 漏迁**：A 改名后新建的 B 沿用旧名会命中 A 的旧 preview（灯箱显示错误内容）；删除后 file_meta 行永久残留 | main.py:647-651,794-797,908-910 | 统一遍历三尺寸；delete 时 `DELETE FROM file_meta` | S |
| FUNC-7 | 评分广播链路整体死代码：`_rating_update_queue` 只增不减（唯一出口 `/api/rating/poll` 无调用者），Python 推送的 `window.updateFileRating` 前端不存在 | main.py:694-718,1528-1532 | 前端补 `updateFileRating` 并消费，或删整条链路（二选一，倾向补前端，见 UX-7） | S |
| FUNC-8 | 独立模式 AppBridge 缺 `onDirectoryChanged/onRatingChanged` 槽，前端调用抛 TypeError | main.py:394-427 vs script.js:359,699 | AppBridge 补同名 slot；或前端特性检测 | S |
| FUNC-9 | `/api/list` 元数据 IN 子句：空目录 `IN ()` SQL 语法错、>999 文件超变量上限，且被裸 except 静默吞掉 → 标签/评分静默丢失 | main.py:592-604 | `if files:` 包裹 + chunk 500 分批查询 | S |
| FUNC-10 | sqlite 连接异常路径不关闭、无 `busy_timeout`、裸 except 吞 `database is locked`（与主应用共享 topos.db 并发写时静默丢数据） | main.py:多处（668-704,590-604,822-863 等） | 统一 `with sqlite3.connect()` + `PRAGMA busy_timeout=5000` | M |
| FUNC-11 | **缓存失效缺失（已知遗留）**：缓存 key 不含源 mtime/size，同名重导出后缩略图/元数据永久陈旧 | thumb_engine.py:189-224, file_meta 表 | 生成时写 sidecar（`.v` 文件记 mtime+size），generate 入口比对；`file_meta` 加 mtime 列 | M |
| FUNC-12 | APP_CACHE 无锁并发读写（锁定义了从未用），且未就绪时同步全盘扫描阻塞请求数秒 | utils.py:247-310 | 加锁 + 未就绪返回空并异步扫描 | S |
| FUNC-13 | CopyWorker 静默吞错：磁盘满/权限错留半成品文件且 UI 显示成功；`_copy_tree` 跟随 symlink 可递归爆栈 | main.py:345,365,383 | per-file 错误收集上报；`follow_symlinks=False`；失败清理 dst | M |
| FUNC-14 | `find_available_port` TOCTOU + 嵌入 adapter 换端口后不回写 port_config，文件与实际服务漂移 | main.py:63-91, adapter.py:144-148 | 以实际 bind 成功的 `server.server_port` 回写 | S |

### 3.2 前端

| ID | 问题 | 位置 | 修复方案 | 工作量 |
|---|---|---|---|---|
| FUNC-15 | **目录切换竞态**：快速切换目录时旧响应后到覆盖新列表——用户在旧目录画面上删除/重命名，操作的是旧目录真实文件（**误操作风险**） | script.js:340-364 | 递增 `loadSeq` 令牌校验（配合 AbortController） | S |
| FUNC-16 | HTML 注入面：目录树/智能文件夹/关联应用/属性面板等 6+ 处 innerHTML 拼接未转义或只转义单引号——恶意文件名即可破坏布局乃至注入 JS（`escapeHtml` 已存在却未铺开） | script.js:406,460,540,547,889,901-903,962 | 统一 `escapeHtml`；内联 onclick 改事件委托 + dataset | M |
| FUNC-17 | 死 UI 与未定义函数：灯箱"重置缩放"按钮调用的 `resetZoom()` 全库未定义（点击报错）；浮动播放器整块（`openCms/closeFloatingPlayer`）无任何代码能唤起——死模板 | index.html:317-342 | 补 `resetZoom` 实现；删浮动播放器死块 | S |
| FUNC-18 | audio_player.js 每次初始化向 window 挂一对 mousemove/mouseup 监听且从不移除——长期使用监听器与 DOM 持续泄漏 | audio_player.js:15,27-34 | 移到初始化外只绑一次，或 destroy 时移除 | S |
| FUNC-19 | 缩略图进度条三连：迟到 800ms 定时器清掉新目录计数；轮询刷新 src 重复计数可超 100%（"150/100"） | script.js:332,334,868-872 | 定时器加代次令牌；load 事件按 `img.dataset.counted` 去重 | S |
| FUNC-20 | 轮询 ACK 与 60s 目录缓存配合缺陷：后台目录的更新被误 ACK 清队 + 旧 `t_ver` 滞留 → 进目录看到占位图且不再刷新；poll 失败时清空 confirmed 造成重复刷新循环 | script.js:665-683 | 只对当前可见集合内路径 ACK；失败保留 confirmed；后端缩略图完成时 `invalidate_cache(dirname)` | M |
| FUNC-21 | 根目录文件重命名路径计算丢 `/`；全选判断只比数量不比内容（筛选后误清选区）；智能文件夹里点"上级"进入非法路径 | script.js:915-918,127,368 | 各一行修复 | S |
| FUNC-22 | 分批渲染在超高视口下停滞（sentinel 始终可见但无状态跃迁，列表停在 100 项） | script.js:650 | `renderNextBatch` 末尾复查续载 | S |

---

## 4. P2 — 性能与体验（第四批，合计约 4~6 天）

| ID | 主题 | 现状与方案 | 工作量 |
|---|---|---|---|
| UX-1 | **大目录渲染性能** | 千级目录 = 数千 DOM 节点全量渲染。最小改动：卡片加 `content-visibility:auto` + 清理使 `contain:strict` 从未生效的死 CSS（style.css:101,108,128-129）；中期做窗口化渲染 | M |
| UX-2 | **轮询常开** | 每 1.5s 打 `/api/thumb/poll` 不看页面可见性、独立浏览器模式照跑。`visibilitychange` 暂停/恢复；dock 遮挡场景与 Qt 端配合 | S |
| UX-3 | 错误态呈现 | 生成失败的缩略图只有灰图标，无"点击重试"（后端有 failure_cache 但用户无感）；loadPath 网络异常只 console.error，界面停在残留列表。失败卡片加重试；异常 toast + 保留旧列表 | M |
| UX-4 | 排序键数值化 | size 靠反解显示字符串、mtime 粒度只到日（同日文件排序抖动）。后端 `/api/list` 直接下发 `size_bytes`/`mtime_ts` | S |
| UX-5 | 原生 prompt/confirm 替换 | 新建/重命名/删除用原生阻塞弹窗（QWebEngine 下体验差），统一 Bootstrap Modal | M |
| UX-6 | 键盘可达性 | 仅 Ctrl+A/C/V；补方向键网格导航、Enter 打开、Delete 删除、F2 重命名 | M |
| UX-7 | 评分/标签 UI 补全 | DB 里 tags/comments 已有，卡片无星级/标签角标，跨应用改评分不感知（配合 FUNC-7 一起做） | M |
| UX-8 | 搜索增强 | 仅文件名子串；扩展到 tags/comments 匹配 + 命中数显示 | S |
| UX-9 | 中文化收尾 | "Library"/"Player"/`alert('功能开发中')` 等中英混杂占位 | S |
| PERF-1 | `/api/list` 瘦身 | 每请求：创建 `.cache/.nomedia`（GET 带写副作用）、spawn 一个跑已禁用的 clean_cache_task 的线程、无缩略图文件走 O(N×M) 的 .cache 目录模糊扫描、每文件 3 次 stat、每次现场计算扩展名 union。逐项消除：删 clean_task 线程、union 改预计算 frozenset、模糊扫描限小目录并缓存结果 | M |
| PERF-2 | 直读原图分支 | `<500KB` 图片 send_file 无 Cache-Control，与相邻分支不一致；目录误传时 500 | S |
| PERF-3 | 错误响应 JSON 化 | 非 JSON 请求体返回 werkzeug HTML 错误页；注册全局 errorhandler | S |

---

## 5. P3 — 架构与技术债（第五批，按需排期）

| ID | 主题 | 内容 | 工作量 |
|---|---|---|---|
| ARCH-1 | **main.py 拆分** | 1581 行含端口管理/DB/配置/线程池/30+ 路由/SQLite/Qt 窗口/诊断脚本 8 种职责。拆 `routes_api.py / workers.py / db.py / window.py / bridge.py`；NFC/NFD 规范化（重复 8+ 处）与文件类型归类（两处已分叉出错）抽公共函数 | L |
| ARCH-2 | **播放器基类抽取** | ToposPlayer/LibavPlayer 约 500-600 行逐行同构（浮层 UI/空闲隐藏/ring 背压/seek/toast/快照），UltimatePlayer 再复制一遍；CRASH-3/4 与方向键不一致（Up/Down 语义两引擎相反）正是复制漂移。抽 `FloatPlayerBase`，三引擎只留解码/音频差异 | L |
| ARCH-3 | **播放路由统一** | 同一路由规则双实现：独立模式 `VideoProEngine`（且嵌入模式从未 fire `mainwindow_ready`，是死代码）、嵌入模式 `play_video_in_panel`。抽 `route_and_play()` 共用；顺带修 FilesPanel resize 不跟随 topos/libav 播放器、`load_video` 只停 UltimatePlayer 两处分叉 | M |
| ARCH-4 | 日志体系 | 全库 print+emoji → `logging` 分级；桥/runJavaScript 失败可见化（runJavaScript 加回调记录） | M |
| ARCH-5 | 配置集中 | CACHE_TTL/超时/阈值/端口等魔法数 20+ 处收敛为常量模块或 config.json 注入 | S |
| ARCH-6 | import 零副作用 | import main.py 即起 3 线程、写端口文件、建 DB、同步跑 `ffmpeg -encoders` 子进程（最坏 10s）。改为显式 `init()` | M |
| ARCH-7 | 健康检查 | 加 `/api/health`（port/db/ffmpeg 状态），替代独立模式"urlopen 50 次"与嵌入模式 100ms 轮询探测 | S |
| ARCH-8 | WSGI 升级 | werkzeug dev server → waitress（单依赖生产级），配合 SEC-6 的全 subprocess 超时 | S |
| ARCH-9 | 死代码清理 | `clean_cache_task` 不可达体、`/api/rating/poll`（视 FUNC-7 决策）、`run_deep_diagnostic`、integration.py 虚假稳定 API、`utils.generate_thumbnail`、`ImageConvertPro2026.py`、`verify_theme.py`、index.html 浮动播放器块、audio_pro 缩进死代码 | S |
| ARCH-10 | 数据一致性 | init_db 迁移丢弃 tags/comments；NFD 路径在 file_meta 产生双行；DIR_CACHE 60s 内主应用改评分不感知（提供 invalidate 端点供宿主调用）；smart folder 不清理已删文件 | M |
| ARCH-11 | 插件体系加固 | file_explorer/plugins 与仓库根 plugins 命名空间包冲突隐患（加 `__init__.py` 或按路径显式加载）；setup() 延迟到显式调用后；插件启停配置 | M |
| ARCH-12 | 运行时文件治理 | `.port_config.json` 被 git 跟踪且每次运行重写（当前 git status 长期脏）→ 加 .gitignore，端口写用户缓存目录 | S |
| ARCH-13 | 前端模块化 | script.js 985 行单文件 + 40 个 window 全局 → 拆 state/render/actions 三模块（IIFE 即可，不引构建） | M |
| ARCH-14 | 兼容性声明 | `replaceAll`/`:scope`/`aspect-ratio` 需 Chromium ≥88（Qt6 满足，Qt5 不行）；固定"仅支持 Qt6"于 README | S |

---

## 6. 建议排期（里程碑）

| 里程碑 | 内容 | 前置 | 工作量 | 交付判据 |
|---|---|---|---|---|
| **M1 安全加固** | SEC-1~5 | 无 | 2~3 天 | 恶意网页 PoC 三连失败；正常功能回归 |
| **M2 稳定性** | CRASH-1~7 + SEC-6,7 + FUNC-2,3,14（端口/路径类） | M1（同文件） | 3~4 天 | 反复开关面板/退出无挂死；长时间播放无实例累积；截图并发无崩溃 |
| **M3 功能修复** | FUNC-1,4~13,15~22 | 无强依赖，可与 M2 并行 | 4~6 天 | 外接盘坏目录可用；重命名/删除/迁移元数据一致；快速切目录无串台 |
| **M4 体验** | UX-1~9, PERF-1~3 | M3（依赖功能修复） | 4~6 天 | 千级目录滚动流畅；空闲零轮询；错误可重试 |
| **M5 架构** | ARCH-1~14 | M2~M4 稳定后 | 按需 | 双模式行为一致；新播放器只需实现解码差异 |

**快速胜利清单**（半天内可完成、收益直接）：FUNC-1（坏 symlink 整目录不可用）、FUNC-2（端口白屏）、FUNC-4（智能文件夹分类）、SEC-2/3/4/5（输入校验）、CRASH-5（播放器泄漏）、UX-2（轮询暂停）、ARCH-12（.gitignore 端口文件）。

---

## 7. 已确认无虞的项（不需要动）

- Flask 只绑 127.0.0.1，无 `debug=True`，subprocess 全部列表参数无 `shell=True`；
- 缩略图管线（上一轮优化）：双 worker、原子落盘、失败缓存、preview 按需均工作正常；
- `topos_1` 旧目录副本无任何代码引用（仅基准输出文件提及），无遗留耦合；
- 卡片事件用容器级委托，重渲染不累积监听器；灯箱 WaveSurfer 悬停实例 destroy 正确；
- `docs/PLUGIN_DEV_GUIDE.md` 与 file_explorer 插件体系脱节——建议 ARCH 批次顺带补一节，不算 bug。

## 8. 审计覆盖范围

| 区域 | 文件 | 覆盖 |
|---|---|---|
| 后端 | main.py(1581)、utils.py(344)、thumb_engine.py(670)、plugin_system.py、integration.py、config/.port_config | 全文通读 |
| 前端 | script.js(984)、index.html(560)、style.css(337)、audio_player.js(253) | 全文通读（vendor 第三方库未审） |
| 集成 | adapter.py、files_panel.py、library_adapter.py、file_controller.py 播放相关段、main_window.py 信号段 | 通读/精读 |
| 插件 | video_pro 全部 7 文件通读；其余 8 个插件 `__init__.py` 级别 | 部分 |
| 动态核实 | werkzeug 3.1.8 shutdown 语义、socketserver 源码、git ls-files、topos.db/metadata.db schema | 已做 |
