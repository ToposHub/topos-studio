"""[V2/GAP-04] 元数据写回文件（XMP），让标记跨工具/跨机器存活。

此前评分/标签/备注/颜色/旗标只存本机 SQLite——换工具、换机器、把素材发给
同事，标记全部丢失。本模块把 DB 标记以 XMP 写回文件：
- Rating        → XMP-xmp:Rating（Lightroom/digiKam 互认）
- 标签          → XMP-dc:Subject + XMP-lr:HierarchicalSubject（层级路径，'/'分隔）
- 备注          → XMP-dc:Description
- 颜色标记      → XMP-digiKam:ColorLabel（digiKam 色板编号映射）
- 旗标          → XMP-lr:Flag（pick/reject）

写入模式（config `meta_embed`）：
- sidecar（默认）：图片写同名 .xmp 旁车（exiftool -o），原文件零改动；
  已有 sidecar 就地更新。
- inplace：直接内嵌文件（-overwrite_original -P 保护原 mtime 尽量不动）。
- off：完全关闭（enqueue 直接丢弃）。

队列：单线程串行 daemon，首次 enqueue 惰性启动；失败记日志不重试阻塞。
sidecar 文件对扫描器/列表不可见（scanner 与 /api/list 均跳过 .xmp）。
"""
import logging
import os
import queue
import subprocess
import threading

import utils
from db import _db, _meta_key, load_config

logger = logging.getLogger(__name__)

MODE_KEY = 'meta_embed'  # sidecar | inplace | off

# 我们的色板（1红2黄3绿4蓝5紫）→ digiKam ColorLabel 枚举
# (digiKam: 0无 1红 2橙 3黄 4绿 5蓝 6品红/紫)
COLOR_TO_DIGIKAM = {1: 1, 2: 3, 3: 4, 4: 5, 5: 6}
DIGIKAM_TO_COLOR = {v: k for k, v in COLOR_TO_DIGIKAM.items()}

_queue = queue.Queue()
_worker_started = False
_start_lock = threading.Lock()


def get_mode():
    return str(load_config().get(MODE_KEY, 'sidecar') or 'sidecar')


def enqueue(path, fields=None):
    """写回任务入队（异步，不阻塞 API 响应）。

    fields 缺省 = 读 DB 当前值全量写回。off 模式直接丢弃。"""
    if get_mode() == 'off':
        return False
    if fields is None:
        conn = _db()
        try:
            row = conn.execute(
                "SELECT rating, tags, comments, color, flag FROM file_meta WHERE path = ?",
                (_meta_key(path),)).fetchone()
        finally:
            conn.close()
        if not row:
            return False
        fields = {'rating': row[0] or 0,
                  'tags': [t.strip() for t in (row[1] or '').split(',') if t.strip()],
                  'comments': row[2] or '',
                  'color': row[3] or 0,
                  'flag': row[4] or 0}
    _ensure_worker()
    _queue.put((str(path).replace('\\', '/'), fields))
    return True


def _ensure_worker():
    global _worker_started
    if _worker_started:
        return
    with _start_lock:
        if _worker_started:
            return
        threading.Thread(target=_worker, daemon=True, name='fe-meta-sync').start()
        _worker_started = True


def _worker():
    while True:
        path, fields = _queue.get()
        try:
            sync_once(path, fields)
        except Exception as e:
            logger.warning(f"[MetaSync] 写回失败 {path}: {e}")
        finally:
            _queue.task_done()


def _tag_hier_paths(names):
    """标签名 → 层级路径（'父/子'）：id→(name,parent) 两跳映射拼出祖先链。"""
    conn = _db()
    try:
        by_name = {r[0]: r[1] for r in conn.execute("SELECT name, id FROM tags").fetchall()}
        by_id = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT id, name, parent_id FROM tags").fetchall()}
    finally:
        conn.close()
    out = []
    for n in names:
        nid = by_name.get(n)
        if nid is None:
            out.append(n)  # 未入库的新标签：平铺
            continue
        chain, cur, seen = [], nid, set()
        while cur is not None and cur not in seen:
            seen.add(cur)
            name, parent = by_id.get(cur, (None, None))
            if name is None:
                break
            chain.append(name)
            cur = parent
        out.append('/'.join(reversed(chain)))
    return out


def sync_once(path, fields):
    """执行一次写回（worker 线程内）。exiftool 缺失/文件不在 → 静默跳过。"""
    exif = utils.get_tool_path('exiftool')
    if not exif or not os.path.exists(path):
        return False
    f = fields or {}
    args = []
    try:
        if 'rating' in f and f['rating'] is not None:
            r = max(0, min(5, int(f['rating'])))
            args += [f'-XMP:Rating={r}']
        if 'comments' in f and f['comments'] is not None:
            # [V5 复查] 空串也要写（exiftool 空= 删除标签）：否则应用里删掉的
            # 备注会在反向导入/换机时从旧 XMP 里复活
            args += [f'-XMP-dc:Description={f["comments"] or ""}']
        tags = f.get('tags')
        if tags is not None:
            names = [str(t) for t in tags if str(t).strip()]
            if not names:
                # 清空标签：显式删除，防旧 Subject 复活
                args += ['-XMP-dc:Subject=', '-XMP-lr:HierarchicalSubject=']
            for t in names:
                args += [f'-XMP-dc:Subject={t}']
            for h in _tag_hier_paths(names):
                args += [f'-XMP-lr:HierarchicalSubject={h}']
        if 'color' in f and f['color'] is not None:
            dk = COLOR_TO_DIGIKAM.get(int(f['color']))
            # 0（无色）→ 空= 清除 ColorLabel
            args += [f'-XMP-digiKam:ColorLabel={dk if dk else ""}']
        if 'flag' in f and f['flag'] is not None:
            fv = int(f['flag'])
            flag_val = 'pick' if fv == 1 else ('reject' if fv == 2 else '')
            args += [f'-XMP-lr:Flag={flag_val}']
    except (TypeError, ValueError):
        return False
    if not args:
        return False

    mode = get_mode()
    if mode == 'inplace':
        cmd = [exif, '-overwrite_original', '-P'] + args + [path]
    else:  # sidecar（默认）
        sidecar = path + '.xmp'
        if os.path.exists(sidecar):
            cmd = [exif, '-overwrite_original'] + args + [sidecar]
        else:
            cmd = [exif, '-o', sidecar] + args + [path]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True,
                             encoding='utf-8', errors='ignore', timeout=15)
        if res.returncode != 0:
            logger.warning(f"[MetaSync] exiftool 写回失败（{mode}）{path}: "
                           f"{(res.stderr or '').strip()[:200]}")
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.warning(f"[MetaSync] exiftool 超时 {path}")
        return False


def read_xmp(path):
    """读文件的 XMP 标记（sidecar 优先，回退内嵌）。返回 {rating, tags}。"""
    exif = utils.get_tool_path('exiftool')
    if not exif:
        return None
    target = path + '.xmp' if os.path.exists(path + '.xmp') else path
    try:
        res = subprocess.run(
            [exif, '-json', '-XMP:Rating', '-XMP-dc:Subject',
             '-XMP-lr:HierarchicalSubject', target],
            capture_output=True, text=True, encoding='utf-8', errors='ignore', timeout=8)
        if res.returncode != 0:
            return None
        d = (res.stdout and __import__('json').loads(res.stdout) or [{}])[0]
    except Exception:
        return None
    rating = 0
    try:
        rating = max(0, min(5, int(float(d.get('Rating') or 0))))
    except (TypeError, ValueError):
        pass
    tags = []
    for key in ('HierarchicalSubject', 'Subject'):
        v = d.get(key)
        if isinstance(v, list):
            tags = [str(x).split('/')[-1].strip() for x in v if str(x).strip()]
            if tags:
                break
        elif isinstance(v, str) and v.strip():
            tags = [x.strip() for x in v.split(',') if x.strip()]
            if tags:
                break
    return {'rating': rating, 'tags': tags}


def import_from_files(library=None, cap=5000):
    """反向导入（Lightroom"从文件读取元数据"同向）：扫描库内文件读 XMP，
    仅在 DB 侧该文件评分与标签均为默认值时采用（冲突不覆盖）。

    返回 {scanned, adopted, conflicts, skipped}。"""
    import db
    libs = [library] if library else [l.get('path') for l in db.load_config().get('libraries', []) if l.get('path')]
    scanned = adopted = conflicts = skipped = 0
    conn = _db()
    try:
        c = conn.cursor()
        for lib in libs:
            if not lib or not os.path.isdir(lib):
                continue
            for dirpath, dirnames, filenames in os.walk(lib, followlinks=False):
                dirnames[:] = [d for d in dirnames if not d.startswith('.')]
                for name in filenames:
                    if name.startswith('.') or name.lower().endswith('.xmp'):
                        continue
                    if scanned >= cap:
                        # [V5 复查] 提前返回前必须提交：连接 close 会回滚整个
                        # 未提交事务，此前达到 cap 时全部采纳结果静默丢失
                        conn.commit()
                        return {'scanned': scanned, 'adopted': adopted,
                                'conflicts': conflicts, 'skipped': skipped + 1}
                    p = os.path.join(dirpath, name)
                    key = _meta_key(p)
                    row = c.execute("SELECT rating, tags FROM file_meta WHERE path = ?",
                                    (key,)).fetchone()
                    if row and (row[0] or 0) != 0 or (row and (row[1] or '').strip()):
                        conflicts += 1  # DB 已有标记：不覆盖
                        continue
                    xmp = read_xmp(p)
                    scanned += 1
                    if not xmp or (not xmp['rating'] and not xmp['tags']):
                        continue
                    tags_str = ','.join(sorted(set(xmp['tags'])))
                    if row:
                        c.execute("UPDATE file_meta SET rating = ?, tags = ? WHERE path = ?",
                                  (xmp['rating'], tags_str, key))
                    else:
                        c.execute("INSERT INTO file_meta (path, tags, comments, rating) VALUES (?, ?, '', ?)",
                                  (key, tags_str, xmp['rating']))
                    for t in xmp['tags']:
                        c.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (t,))
                        c.execute("""INSERT OR IGNORE INTO file_tags (file_path, tag_id)
                                     SELECT ?, id FROM tags WHERE name = ?""", (key, t))
                    adopted += 1
                    # [V5 复查] 分批提交：全库 walk 单事务持锁分钟级（每文件一个
                    # exiftool 子进程），并发写全靠 busy_timeout 硬扛
                    if adopted % 200 == 0:
                        conn.commit()
        conn.commit()
    finally:
        conn.close()
    return {'scanned': scanned, 'adopted': adopted, 'conflicts': conflicts, 'skipped': skipped}
