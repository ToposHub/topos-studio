"""[ARCH-1] 后台工作线程：双缩略图 worker + ffprobe 元数据队列 + 前端轮询状态。"""
import logging
import os
import queue
import threading
import time

import utils
import const
from thumb_engine import engine as thumb_engine, EXT_VIDEO
from db import _db, _meta_key, invalidate_cache
# bridge 实例在 bridge.py 模块级创建且 bridge 对本模块只做调用期导入，
# 此处模块级导入无环。
from bridge import bridge

logger = logging.getLogger(__name__)


# 全局并发限制
# 平衡模式：限制为 2 以避免外接硬盘 IO 过载影响播放
ffmpeg_semaphore = threading.Semaphore(const.FFMPEG_CONCURRENCY)


thumbnail_queue = queue.Queue()
thumbnail_pending = set()
thumbnail_lock = threading.Lock()
_thumbnail_stop_event = threading.Event()
_thumbnail_worker_threads = []

# 用于嵌入模式下前端轮询检查缩略图更新的队列状态
_thumbnail_queue_lock = threading.Lock()
_thumbnail_update_queue = []  # 待刷新路径队列
_thumbnail_last_confirm = 0   # 前端最后确认的时间戳

# 星级评定变更队列，用于通知前端刷新
_rating_update_queue = []
_rating_update_lock = threading.Lock()

# 视频元数据异步队列：ffprobe 写库不再阻塞缩略图生成（队头每视频可省 0.1~5s）
_meta_queue = queue.Queue()


def _process_meta_queue():
    """后台元数据线程：视频缩略图生成成功后异步补写 ffprobe 缓存。"""
    while not _thumbnail_stop_event.is_set():
        try:
            path = _meta_queue.get(timeout=0.2)
            if path is None:
                _meta_queue.task_done()
                break
            try:
                video_info = utils.get_video_info(path)
                # [V1/GAP-02] 技术元数据结构化列同步回写（时长/分辨率/编码…）。
                # 失败不阻塞 cached_info 更新（exiftool/ffprobe 缺失等）。
                try:
                    import scanner
                    scanner.update_tech_meta(path)
                except Exception:
                    pass
                if video_info:
                    conn = _db()
                    try:
                        c = conn.cursor()
                        c.execute("UPDATE file_meta SET cached_info = ? WHERE path = ?", (video_info, _meta_key(path)))
                        if c.rowcount == 0: c.execute("INSERT INTO file_meta (path, cached_info, tags, comments, rating) VALUES (?, ?, '', '', 0)", (_meta_key(path), video_info))
                        conn.commit()
                    finally:
                        # [R1-8] 异常路径此前跳过 conn.close()，连接泄漏
                        conn.close()
            except Exception:
                pass
            finally:
                _meta_queue.task_done()
        except queue.Empty:
            continue
        except Exception:
            pass


def process_thumbnail_queue():
    """异步缩略图生成线程"""
    while not _thumbnail_stop_event.is_set():
        try:
            item = thumbnail_queue.get(timeout=0.2)
            if item is None:
                thumbnail_queue.task_done()
                break
            path, target, size_type = item
            if _thumbnail_stop_event.is_set():
                thumbnail_queue.task_done()
                continue
            ext = os.path.splitext(path)[1].lower()
            success = False
            try:
                if ext in EXT_VIDEO:
                    with ffmpeg_semaphore: # 限制视频处理并发，防止IO阻塞
                        # preview(1920px) 仅在灯箱请求时随生成，浏览冷启动只产 small+large
                        results = thumb_engine.generate_video_all_sizes(
                            path, include_preview=(size_type == 'preview'))
                        success = results.get(size_type, False)
                        if success:
                            _meta_queue.put(path) # ffprobe 移出关键路径，异步补写
                else:
                    success = thumb_engine.generate(path, target, size_type=size_type)

                if not success:
                    # 失败进缓存：防止坏文件被前端反复请求、反复占用生成队列
                    thumb_engine.register_failure(path, 'generate_failed')

                # [FIX] 仅在成功生成缩略图时才通知前端刷新
                if success:
                    # [V3/GAP-08+09] 感知哈希 + 主色桶：读 small 缩略图一次解码
                    # 两用，回写 file_meta（失败不影响缩略图本身）
                    if size_type == 'small':
                        try:
                            from media_features import features_from_file
                            fpath = target if os.path.exists(target) else \
                                thumb_engine.video_thumb_paths(path).get('small', target)
                            if os.path.exists(fpath):
                                ph, bucket = features_from_file(fpath)
                                if ph or bucket is not None:
                                    _c2 = _db()
                                    try:
                                        _cc = _c2.cursor()
                                        if ph:
                                            _cc.execute("UPDATE file_meta SET phash = ? WHERE path = ?",
                                                        (ph, _meta_key(path)))
                                        if bucket is not None:
                                            _cc.execute("UPDATE file_meta SET dom_color = ? WHERE path = ?",
                                                        (bucket, _meta_key(path)))
                                        _c2.commit()
                                    finally:
                                        _c2.close()
                        except Exception:
                            pass
                    # [FUNC-20] 失效该目录列表缓存：下次 /api/list 返回新 t_ver，
                    # 避免旧缓存滞留导致"进目录看到占位图且不再刷新"
                    invalidate_cache(os.path.dirname(path))
                    # 更新嵌入模式下的缩略图更新队列，供前端轮询
                    try:
                        notify_target = target if os.path.exists(target) else \
                            thumb_engine.video_thumb_paths(path).get(size_type, target)
                        mtime = os.path.getmtime(notify_target) if os.path.exists(notify_target) else int(time.time())
                        with _thumbnail_queue_lock:
                            # 修复：使用集合比较路径，避免重复添加
                            existing_paths = {item['path'] for item in _thumbnail_update_queue}
                            if path not in existing_paths:
                                _thumbnail_update_queue.append({'path': path, 'mtime': mtime})
                                # 限制队列大小，防止内存泄漏
                                if len(_thumbnail_update_queue) > 500:
                                    _thumbnail_update_queue[:] = _thumbnail_update_queue[-500:]
                    except: pass

                    bridge.thumbnail_updated.emit(path) # 仅在成功时通知前端
            except Exception as e:
                pass  # 静默处理缩略图生成错误，避免终端输出干扰
            finally:
                with thumbnail_lock:
                    # [FUNC-5] 与入队侧一致：按 (path, 尺寸) 移除
                    thumbnail_pending.discard((path, size_type))
                thumbnail_queue.task_done()
        except queue.Empty:
            continue
        except Exception:
            pass  # 静默处理队列错误，避免终端输出干扰


def shutdown_thumbnail_worker(timeout=2.0):
    """Stop the embedded thumbnail workers before Qt/native teardown."""
    global _thumbnail_worker_threads
    _thumbnail_stop_event.set()
    # Wake workers blocked in Queue.get(); queued jobs are discarded by the
    # stop check above so shutdown does not wait for media decoding.
    workers = list(_thumbnail_worker_threads)
    for _ in workers:
        thumbnail_queue.put(None)
    for worker in workers:
        if worker.is_alive():
            worker.join(timeout=max(0.0, float(timeout)))
    _thumbnail_worker_threads = []


# 双 worker：视频生成是解码密集型，单线程会让千张级媒体库冷启动极慢；
# 实际 ffmpeg 并发仍受 ffmpeg_semaphore(2) 约束，避免外接盘 IO 过载。
_thumbnail_worker_started = False
def _ensure_thumbnail_worker():
    global _thumbnail_worker_started, _thumbnail_worker_threads
    if not _thumbnail_worker_started:
        if os.environ.get("TOPOS_TEST_MODE") == "1":
            # The embedded worker owns media/native resources and is not part
            # of file-explorer unit coverage.  Avoid creating it in the host's
            # Qt test process, where a daemon thread can outlive a test widget.
            _thumbnail_worker_started = True
            return
        _thumbnail_stop_event.clear()
        for i in range(2):
            t = threading.Thread(
                target=process_thumbnail_queue,
                name=f"file-explorer-thumbnail-worker-{i}",
                daemon=True,
            )
            t.start()
            _thumbnail_worker_threads.append(t)
        threading.Thread(target=_process_meta_queue,
                         name="file-explorer-meta-worker", daemon=True).start()
        _thumbnail_worker_started = True
        logger.info("[Thumbnail Worker] Started x2 + meta worker (via ensure_runtime)")
