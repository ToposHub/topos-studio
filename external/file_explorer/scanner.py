"""[V1/GAP-01] 全库后台索引器：把"浏览过的文件才有元数据"变成"全库可检索"。

职责：
- 遍历 config.json libraries，把磁盘文件 UPSERT 进 file_meta（path/mtime/size/
  missing），绝不覆盖用户资产列（tags/comments/rating/color/flag/cached_info）；
- 新文件/变更文件的媒体类顺带提取技术元数据（GAP-02 列）并入队缩略图生成；
- 磁盘已失联的行标 missing=1（搜索默认仍可命中，UI 可过滤），不删除——
  卷暂时离线不等于用户想丢元数据；
- ensure_runtime 启动 5s 后首扫，此后按 config scan_interval_min 周期增量。

线程模型：扫描在独立 daemon 线程串行执行（同一时刻至多一次，可API触发）。
缩略图生成走 thumbs 既有队列（有界）；技术元数据在本线程内联提取并节流。
"""
import logging
import os
import threading
import time

import const
import utils
from db import _db, _meta_key, load_config

logger = logging.getLogger(__name__)

# 扫描跳过目录（隐藏目录统一按 . 前缀过滤，这里列显式的已知噪音）
SKIP_DIR_NAMES = {'.trash', '.git', 'node_modules', '$RECYCLE.BIN',
                  'System Volume Information', '.cache'}

# 参与技术元数据提取/缩略图的媒体扩展（保守集合：thumbs 管线覆盖的类型）
from thumb_engine import (
    EXT_VIDEO, EXT_IMAGE_PRO, EXT_RAW_PHOTO, EXT_IMAGE_NORMAL, EXT_AUDIO,
)
MEDIA_EXTS = EXT_VIDEO | EXT_IMAGE_PRO | EXT_RAW_PHOTO | EXT_IMAGE_NORMAL | EXT_AUDIO

_state_lock = threading.Lock()
_state = {
    'running': False, 'phase': '', 'library': '', 'scanned': 0, 'total': 0,
    'last_result': None, 'started_at': None,
}
_stop_event = threading.Event()
_scan_thread = None


def status():
    with _state_lock:
        return dict(_state)


def start(library=None):
    """启动一次全量增量扫描；已有扫描在跑返回 False（前端提示即可）。"""
    global _scan_thread
    with _state_lock:
        if _state['running']:
            return False
        _state.update({'running': True, 'phase': '准备', 'scanned': 0, 'total': 0,
                       'library': library or '全部素材库', 'started_at': time.time()})
    _stop_event.clear()
    _scan_thread = threading.Thread(target=_run, args=(library,), daemon=True,
                                    name='fe-library-scanner')
    _scan_thread.start()
    return True


def stop():
    _stop_event.set()


def _libraries():
    cfg = load_config()
    return [l.get('path') for l in cfg.get('libraries', []) if l.get('path')]


def _iter_files(root):
    """遍历单库：跳过隐藏/噪音目录与隐藏文件；不跟随 symlink。"""
    n = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith('.') and d not in SKIP_DIR_NAMES]
        for name in filenames:
            if name.startswith('.') or name.lower().endswith('.xmp'):
                continue  # [V2/GAP-04] XMP sidecar 不是素材，不入库不显示
            n += 1
            if n > const.SCAN_ENUM_CAP:
                logger.warning(f"[Scanner] 单库枚举达到上限 {const.SCAN_ENUM_CAP}，截断: {root}")
                return
            yield os.path.join(dirpath, name)


def _run(library=None):
    added = updated = tech_done = 0
    libs = [library] if library else _libraries()
    try:
        for lib in libs:
            if _stop_event.is_set():
                break
            if not lib or not os.path.isdir(lib):
                continue
            a, u, t = _scan_library(lib)
            added += a
            updated += u
            tech_done += t
    except Exception as e:
        logger.error(f"[Scanner] 扫描异常: {e}")
        with _state_lock:
            _state['running'] = False
            _state['phase'] = f'失败: {e}'
            _state['last_result'] = {'error': str(e), 'finished_at': time.time()}
        return
    # 完成后清目录缓存：下次 /api/list 直接反映新文件（thumb_ver 变化）
    try:
        import db
        with db.CACHE_LOCK:
            db.DIR_CACHE.clear()
    except Exception:
        pass
    elapsed = 0.0
    with _state_lock:
        started = _state.get('started_at')
        if started:
            elapsed = round(time.time() - started, 1)
        _state['running'] = False
        _state['phase'] = '完成'
        _state['last_result'] = {'added': added, 'updated': updated,
                                 'tech_meta': tech_done, 'elapsed': elapsed,
                                 'finished_at': time.time()}
    logger.info(f"[Scanner] 扫描完成：新增 {added}，更新 {updated}，"
                f"技术元数据 {tech_done}（{elapsed}s）")


def _escape_like(s: str) -> str:
    return s.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def _scan_library(lib):
    """单库增量：枚举 → 与 DB 比对 → UPSERT（不覆盖用户列）→ 失联标记。

    返回 (added, updated, tech_done)。"""
    lib_key = _meta_key(lib)
    with _state_lock:
        _state['phase'] = f'枚举 {os.path.basename(lib) or lib}'
        _state['scanned'] = 0

    entries = []
    for p in _iter_files(lib):
        try:
            st = os.stat(p)
        except OSError:
            continue
        entries.append((_meta_key(p), st.st_mtime, st.st_size))
        if _stop_event.is_set():
            return 0, 0, 0
    # [V5 复查] 枚举被 SCAN_ENUM_CAP 截断时 seen 不完整——此时判"失联"
    # 会把未枚举到的几十万行全部误标 missing，必须跳过失联标记阶段
    truncated = len(entries) >= const.SCAN_ENUM_CAP
    with _state_lock:
        _state['total'] = len(entries)
        _state['phase'] = '对比数据库'

    like = _escape_like(lib_key) + '/%'
    conn = _db()
    try:
        db_rows = {r[0]: (r[1], r[2], r[3] or 0) for r in conn.execute(
            "SELECT path, mtime, size, missing FROM file_meta WHERE path LIKE ? ESCAPE '\\'",
            (like,)).fetchall()}
    finally:
        conn.close()

    new_files, changed, resurrect, tech_jobs = [], [], [], []
    seen = set()
    for key, mt, sz in entries:
        seen.add(key)
        row = db_rows.get(key)
        if row is None:
            new_files.append((key, mt, sz))
        elif row[0] != mt or row[1] != sz:
            changed.append((key, mt, sz, row[1]))
        elif row[2]:
            # [V5 复查] 盘上仍在、mtime/size 未变但 missing=1（此前被误标或
            # 卷离线时标过）→ 复位。原实现只有"新增/变更"两个分支，missing
            # 一旦置位全量扫描永远不会自愈
            resurrect.append(key)
    ext_of = lambda p: os.path.splitext(p)[1].lower()
    for lst in (new_files, changed):
        tech_jobs.extend(k for k, *_ in lst
                         if ext_of(k) in MEDIA_EXTS and not _stop_event.is_set())
    tech_set = set(tech_jobs)

    added = updated = tech_done = 0
    conn = _db()
    try:
        c = conn.cursor()
        now = time.time()

        def _commit(check):
            nonlocal added
            if check % const.SCAN_COMMIT_EVERY == 0:
                conn.commit()

        for i, (key, mt, sz) in enumerate(new_files, 1):
            # ON CONFLICT 只 touch 技术列：用户资产列原样保留
            c.execute("""INSERT INTO file_meta (path, tags, comments, rating, mtime, size, missing, indexed_at)
                         VALUES (?, '', '', 0, ?, ?, 0, ?)
                         ON CONFLICT(path) DO UPDATE SET
                           mtime = excluded.mtime, size = excluded.size,
                           missing = 0, indexed_at = excluded.indexed_at""",
                      (key, mt, sz, now))
            added += 1
            _commit(i)
        for i, (key, mt, sz, old_sz) in enumerate(changed, 1):
            # 内容变化（体积不同）→ 旧视频摘要作废，等元数据队列重取
            if sz != old_sz:
                c.execute("""UPDATE file_meta SET mtime = ?, size = ?, missing = 0,
                             indexed_at = ?, cached_info = NULL WHERE path = ?""", (mt, sz, now, key))
            else:
                c.execute("UPDATE file_meta SET mtime = ?, size = ?, missing = 0, indexed_at = ? WHERE path = ?",
                          (mt, sz, now, key))
            updated += 1
            _commit(i)
        # 失联标记：库内已有行但本次枚举未见（卷在线才有资格判失联）。
        # [V5 复查] 截断时 seen 不完整，跳过；missing 复位单独一批
        gone = [] if truncated else [k for k in db_rows if k not in seen]
        if resurrect:
            c.executemany("UPDATE file_meta SET missing = 0, indexed_at = ? WHERE path = ?",
                          [(now, k) for k in resurrect])
        if gone:
            c.executemany("UPDATE file_meta SET missing = 1 WHERE path = ?", [(k,) for k in gone])
        conn.commit()
    finally:
        conn.close()

    with _state_lock:
        _state['phase'] = '提取技术元数据'
    # 技术元数据在本线程内联提取（ffprobe/exiftool 子进程），节流防 IO 独占
    for i, key in enumerate(tech_jobs, 1):
        if _stop_event.is_set():
            break
        try:
            if update_tech_meta(key):
                tech_done += 1
        except Exception as e:
            logger.debug(f"[Scanner] 技术元数据提取失败 {key}: {e}")
        if i % const.SCAN_META_PAUSE_EVERY == 0:
            time.sleep(const.SCAN_META_PAUSE_S)
        with _state_lock:
            _state['scanned'] = i

    # [V4/GAP-17] OCR（设置开启且有引擎才投递，独立单线程队列限速）
    try:
        import ocr_engine
        if ocr_engine.is_enabled() and ocr_engine.detect_engine() is not None:
            n_ocr = 0
            for key in tech_jobs:
                if ext_of(key) in EXT_IMAGE_NORMAL and n_ocr < 800:
                    ocr_engine.enqueue(key)
                    n_ocr += 1
    except Exception as e:
        logger.debug(f"[Scanner] OCR 投递失败: {e}")
    # [V4/GAP-21] AI 打标（配置 ai_vault_url 才启用；上限 200/次扫描；
    # vault 不可达时熔断，不再逐文件空等超时）
    try:
        import ai_tagging
        if ai_tagging.vault_url():
            n_ai = 0
            for key in tech_jobs:
                if ext_of(key) in EXT_IMAGE_NORMAL and n_ai < 200:
                    if ai_tagging.tag_file(key) < 0:
                        logger.info("[Scanner] AI vault 不可达，本轮打标熔断")
                        break
                    n_ai += 1
    except Exception as e:
        logger.debug(f"[Scanner] AI 打标失败: {e}")

    # 新媒体文件入队缩略图（有界上限；已失败缓存的不重试）
    try:
        _enqueue_thumbs([k for k, *_ in new_files if ext_of(k) in MEDIA_EXTS][:const.SCAN_THUMB_CAP])
    except Exception as e:
        logger.debug(f"[Scanner] 缩略图入队失败: {e}")

    with _state_lock:
        _state['scanned'] = len(entries)
    return added, updated, tech_done


def update_tech_meta(path):
    """get_tech_meta → file_meta 技术列回写（不动用户资产列）。

    供扫描器（后台线程）与缩略图元数据队列（thumbs._meta_queue）共用。
    无行时 INSERT 基础行（浏览可能尚未创建过）。"""
    meta = utils.get_tech_meta(path)
    if not meta:
        return False
    cols, vals = [], []
    for k in ('mtime', 'size', 'date_taken', 'width', 'height', 'duration', 'codec',
              'camera', 'lens', 'iso', 'aperture', 'shutter', 'focal', 'gps_lat', 'gps_lon'):
        v = meta.get(k)
        if v is not None:
            cols.append(f"{k} = ?")
            vals.append(v)
    if not cols:
        return False
    key = _meta_key(path)
    conn = _db()
    try:
        c = conn.cursor()
        c.execute(f"UPDATE file_meta SET {', '.join(cols)} WHERE path = ?", (*vals, key))
        if c.rowcount == 0:
            c.execute("INSERT OR IGNORE INTO file_meta (path, tags, comments, rating) VALUES (?, '', '', 0)",
                      (key,))
            c.execute(f"UPDATE file_meta SET {', '.join(cols)} WHERE path = ?", (*vals, key))
        conn.commit()
        return True
    finally:
        conn.close()


def _enqueue_thumbs(paths):
    """新媒体文件入队 small 缩略图（与 /api/thumb 同协议：pending 去重）。"""
    from thumbs import thumbnail_queue, thumbnail_pending, thumbnail_lock
    from thumb_engine import engine as thumb_engine
    enqueued = 0
    for p in paths:
        try:
            target = thumb_engine.get_thumb_path(p, size_type='small')
        except Exception:
            continue
        with thumbnail_lock:
            k = (p, 'small')
            if k in thumbnail_pending:
                continue
            thumbnail_pending.add(k)
        thumbnail_queue.put((p, target, 'small'))
        enqueued += 1
    if enqueued:
        logger.info(f"[Scanner] 已入队 {enqueued} 个缩略图生成任务")


def scan_directory(path):
    """[V1/GAP-03] 单目录快速增量（目录监视去抖后调用）：
    只做该目录的文件枚举 + DB diff + 失联翻转，技术元数据/缩略图投递后台
    队列（此函数可能跑在 Qt GUI 线程，禁止子进程提取）。"""
    try:
        if not os.path.isdir(path):
            return
        dir_key = _meta_key(path)
        disk = {}
        try:
            with os.scandir(path) as it:
                for entry in it:
                    if entry.name.startswith('.') or entry.name.lower().endswith('.xmp'):
                        continue
                    try:
                        if entry.is_file():
                            st = entry.stat()
                            disk[_meta_key(os.path.join(path, entry.name))] = (st.st_mtime, st.st_size)
                    except OSError:
                        continue
        except OSError:
            return
        like = _escape_like(dir_key) + '/%'
        prefix = dir_key + '/'
        conn = _db()
        try:
            c = conn.cursor()
            rows = {r[0]: (r[1], r[2], r[3] or 0) for r in c.execute(
                "SELECT path, mtime, size, missing FROM file_meta WHERE path LIKE ? ESCAPE '\\'",
                (like,)).fetchall()}
            now = time.time()
            ups = []
            touched = set()
            for key, (mt, sz) in disk.items():
                old = rows.get(key)
                if old is None:
                    ups.append(("INSERT INTO file_meta (path, tags, comments, rating, mtime, size, missing, indexed_at) "
                                "VALUES (?, '', '', 0, ?, ?, 0, ?)", (key, mt, sz, now)))
                    touched.add(key)
                elif old[0] != mt or old[1] != sz or old[2]:
                    # [V5 复查] 含 missing 复位：盘上仍在的文件曾被标 missing
                    # （卷离线/误标）在此翻回，否则单目录增量永不自愈
                    ups.append(("UPDATE file_meta SET mtime = ?, size = ?, missing = 0, indexed_at = ? WHERE path = ?",
                                (mt, sz, now, key)))
                    touched.add(key)
            # [V5 复查] rows 是 LIKE 前缀匹配的全部后代（含子目录深层文件），
            # disk 只有本目录直接子文件——失联判定必须限定直接子文件，
            # 否则任何一次目录变更都会把子目录里的深层文件全部误标 missing
            gone = [k for k in rows
                    if k not in disk and '/' not in k[len(prefix):]
                    and not (rows[k][2])]
            for sql, params in ups:
                c.execute(sql, params)
            if gone:
                c.executemany("UPDATE file_meta SET missing = 1 WHERE path = ?", [(k,) for k in gone])
            conn.commit()
            # 变更媒体文件投递元数据队列（后台线程做子进程提取）
            # [V5 复查] 只投递本次真正新增/变更的文件：此前目录内任一变更
            # 都会把全目录（可能上千文件）灌进队列
            if touched:
                from thumbs import _meta_queue
                for key in touched:
                    if ext_in_media(key):
                        _meta_queue.put(key)
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"[Scanner] 目录增量失败 {path}: {e}")


def ext_in_media(path):
    return os.path.splitext(path)[1].lower() in MEDIA_EXTS


def start_autoscan():
    """ensure_runtime 调用：延迟首扫 + 周期增量（TOPOS_TEST_MODE 下不启动）。"""
    if os.environ.get('TOPOS_TEST_MODE') == '1':
        return

    def _loop():
        _stop_event.wait(const.SCAN_STARTUP_DELAY_S)
        while not _stop_event.is_set():
            try:
                cfg = load_config()
                interval_min = int(cfg.get('scan_interval_min', const.SCAN_INTERVAL_MIN) or 0)
                if interval_min > 0 and _libraries():
                    start()
            except Exception as e:
                logger.warning(f"[Scanner] 自动扫描触发失败: {e}")
            # 1s 切片等待：配置改间隔/关闭后下一轮即生效
            interval_min = 0
            try:
                interval_min = int(load_config().get('scan_interval_min', const.SCAN_INTERVAL_MIN) or 0)
            except Exception:
                pass
            wait_s = max(interval_min * 60, 60)
            slept = 0
            while slept < wait_s and not _stop_event.is_set():
                _stop_event.wait(1)
                slept += 1

    threading.Thread(target=_loop, daemon=True, name='fe-scanner-autoscan').start()
    logger.info("[Scanner] 自动索引已启用（首次延迟 5s）")
