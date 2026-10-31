"""[ARCH-5] file_explorer 共享常量集中定义。

此前端口/超时/TTL 等魔法数散落在 main.py、thumb_engine.py、adapter.py
（宿主侧）各处，两处默认端口曾各自硬编码 8188，改动时容易漂移。
宿主侧（src/app/integrations/file_explorer/adapter.py）在 sys.path 注入后
惰性 import 本模块，保证双端单一来源。
"""

# ── 网络 / 端口 ────────────────────────────────────────────────────────────────
DEFAULT_PORT = 8188            # 独立/嵌入模式共用默认端口
FIND_PORT_MAX_ATTEMPTS = 10    # 端口被占时向后顺延尝试的上限

# ── SQLite（与主应用共享 topos.db）────────────────────────────────────────────
DB_CONNECT_TIMEOUT_S = 5.0     # sqlite3.connect 的锁等待秒数
DB_BUSY_TIMEOUT_MS = 5000      # PRAGMA busy_timeout（与上一个保持一致）

# ── 缓存 TTL ─────────────────────────────────────────────────────────────────
DIR_CACHE_TTL = 60             # 目录列表缓存有效期（秒）
DIR_CACHE_MAX_ENTRIES = 256    # [BUG-P2-03] 目录缓存条目上限（防长会话内存无界增长）
RATING_QUEUE_MAX = 500         # [BUG-P2-04] 评分广播队列上限（面板未打开时不无限堆积）
THUMB_FAILURE_TTL = 3600       # 缩略图失败缓存保留时长（秒），期内不重试

# ── 子进程超时 ────────────────────────────────────────────────────────────────
AUDIO_TRIM_TIMEOUT_S = 60      # ffmpeg 音频截辑
FFMPEG_TIMEOUT_S = 90          # 缩略图视频抽帧（含 NFD 重试路径）

# ── [R1-16] 缩略图管线阈值（原散落 main.py 的魔法数）─────────────────────────
SMALL_IMAGE_DIRECT_BYTES = 500 * 1024  # 小图直读阈值：小于此值直接回源文件
THUMB_HTTP_MAX_AGE_S = 300             # 小图直读响应的浏览器缓存秒数
THUMB_CACHE_HTTP_MAX_AGE_S = 3600      # 缓存缩略图响应的浏览器缓存秒数
THUMB_BATCH_CHUNK = 500                # /api/thumbs 批量接口的分块大小
SQL_IN_CLAUSE_CHUNK = 500              # IN() 长列表分块（函数内同名参数除外）
FFMPEG_CONCURRENCY = 2                 # ffmpeg 并发上限（防外接盘 IO 过载）
HEALTH_PROBE_ATTEMPTS = 50             # 独立模式启动探活 /api/health 次数
UPLOAD_MAX_CONTENT_BYTES = 4 * 1024 * 1024 * 1024  # [BUG-P2-15] 上传体积上限 4GB（防超大上传填满磁盘）

# ── [V1/GAP-01] 全库索引器（scanner.py）───────────────────────────────────────
SCAN_STARTUP_DELAY_S = 5      # ensure_runtime 后延迟首扫（避开启动 IO 高峰）
SCAN_INTERVAL_MIN = 30        # 周期增量扫描间隔（分钟）；config scan_interval_min 可覆盖，0=关闭
SCAN_META_PAUSE_EVERY = 50    # 每提取 N 个文件的技术元数据让出 CPU（外接盘友好）
SCAN_META_PAUSE_S = 0.05
SCAN_THUMB_CAP = 800          # 单次扫描入队缩略图生成上限（thumbs 队列自身 ≤500 有界）
SCAN_COMMIT_EVERY = 500       # 每 N 行 UPSERT 提交一次事务
SCAN_ENUM_CAP = 200000        # 单库枚举文件数护栏（防误挂载根目录全盘扫）

# ── [V1/GAP-03] 目录监视（watcher.py，QFileSystemWatcher 非递归）──────────────
WATCH_DEBOUNCE_MS = 2000      # directoryChanged 去抖合并窗口
WATCH_MAX_PATHS = 2000        # 监视目录数上限（超限只警告不崩溃，30min 兜底全量扫）
