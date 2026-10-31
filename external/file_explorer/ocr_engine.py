"""[V4/GAP-17] 图内文字（OCR）提取：引擎运行时探测 + 单线程限速队列。

引擎优先级（全部运行时探测，缺什么就降级）：
1. macOS Vision（pyobjc-framework-Vision，系统自带中文/英文模型，零下载）
2. RapidOCR（rapidocr_onnxruntime，离线 ONNX，需 pip 安装）
3. 均不可用 → OCR 停用（`/api/ocr/status` 返回 available=false，队列挂起）。

开关：config `ocr_enabled`（默认 false——OCR 全库跑又慢又费 CPU，且
图片文字可能敏感，交由用户显式开启）。
结果存 ocr_text 表 + 同步写 file_meta_fts.ocr 列，全库搜索即可命中。
"""
import logging
import os
import queue
import threading
import time

from db import _db, _meta_key, load_config

logger = logging.getLogger(__name__)

_engine = None
_engine_checked = False
_engine_lock = threading.Lock()

_queue = queue.Queue()
_worker_started = False
_start_lock = threading.Lock()
_PENDING_CAP = 2000


def is_enabled():
    return bool(load_config().get('ocr_enabled', False))


def detect_engine():
    """运行时探测可用 OCR 引擎，返回可调用 fn(image_path)->str 或 None。"""
    global _engine, _engine_checked
    if _engine_checked:
        return _engine
    with _engine_lock:
        if _engine_checked:
            return _engine
        # 1) macOS Vision
        try:
            import Vision  # pyobjc-framework-Vision
            import Quartz

            def _vision_ocr(path, _Vision=Vision, _Quartz=Quartz):
                from Foundation import NSURL
                url = NSURL.fileURLWithPath_(path)
                handler = _Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, None)
                req = _Vision.VNRecognizeTextRequest.alloc().init()
                req.setRecognitionLevel_(_Vision.VNRequestTextRecognitionLevelAccurate)
                req.setRecognitionLanguages_(["zh-Hans", "zh-Hant", "en-US"])
                ok, err = handler.performRequests_error_([req], None)
                if not ok:
                    return ''
                out = []
                for obs in (req.results() or []):
                    cand = obs.topCandidates_(1)
                    if cand:
                        out.append(cand[0].string())
                return '\n'.join(out)

            _engine = _vision_ocr
        except Exception:
            # 2) RapidOCR
            try:
                from rapidocr_onnxruntime import RapidOCR

                _ocr = RapidOCR()

                def _rapid_ocr(path, _fn=_ocr):
                    result, _ = _fn(path)
                    if not result:
                        return ''
                    return '\n'.join(line[1] for line in result)

                _engine = _rapid_ocr
            except Exception:
                _engine = None
        _engine_checked = True
        if _engine is None:
            logger.info("[OCR] 无可用引擎（需要 pyobjc-Vision 或 rapidocr_onnxruntime），OCR 停用")
        else:
            logger.info("[OCR] 引擎就绪")
    return _engine


def status():
    eng = detect_engine()
    return {'available': eng is not None, 'enabled': is_enabled()}


def enqueue(path):
    """OCR 任务入队（未启用/无引擎/重复任务自动忽略，不阻塞调用方）。"""
    global _worker_started
    if not is_enabled() or detect_engine() is None:
        return False
    _ensure_worker()
    if _queue.qsize() >= _PENDING_CAP:
        return False
    _queue.put(str(path))
    return True


def _ensure_worker():
    global _worker_started
    if _worker_started:
        return
    with _start_lock:
        if _worker_started:
            return
        threading.Thread(target=_worker, daemon=True, name='fe-ocr-worker').start()
        _worker_started = True


def _worker():
    while True:
        path = _queue.get()
        try:
            process(path)
            time.sleep(0.02)  # 限速：OCR 吃 CPU，让出一点
        except Exception as e:
            logger.warning(f"[OCR] 处理失败 {path}: {e}")
        finally:
            _queue.task_done()


def process(path):
    """单图 OCR：引擎提取 → ocr_text UPSERT → FTS ocr 列同步。"""
    eng = detect_engine()
    if eng is None or not os.path.exists(path):
        return False
    text = (eng(path) or '').strip()
    key = _meta_key(path)
    conn = _db()
    try:
        c = conn.cursor()
        # [V5 复查] 先保证 file_meta 基础行存在：无行时只写 FTS 的话，稍后
        # 索引器 INSERT file_meta 会再触发 _ai 插入第二条同 path FTS 行
        # （搜索重复命中）。先 INSERT（触发器自动建 FTS 行）再整体重写
        c.execute("INSERT OR IGNORE INTO file_meta (path, tags, comments, rating) VALUES (?, '', '', 0)",
                  (key,))
        c.execute("""INSERT INTO ocr_text (file_path, text, ocr_at) VALUES (?, ?, ?)
                     ON CONFLICT(file_path) DO UPDATE SET text = excluded.text, ocr_at = excluded.ocr_at""",
                  (key, text, time.time()))
        try:
            # FTS 同步（ocr 列）：DELETE+重写（触发器不覆盖 ocr，且历史行可能缺）
            c.execute("DELETE FROM file_meta_fts WHERE path = ?", (key,))
            row = c.execute("SELECT tags, comments FROM file_meta WHERE path = ?", (key,)).fetchone()
            c.execute("""INSERT INTO file_meta_fts (path, name, tags, comments, ocr)
                         VALUES (?, ?, ?, ?, ?)""",
                      (key, key, (row[0] if row else '') or '', (row[1] if row else '') or '', text))
        except Exception as e:
            # [V5 复查] FTS 表不存在/降级环境不让整个 OCR 失败：ocr_text 已
            # 持久化，FTS 是可再生的派生索引
            logger.warning(f"[OCR] FTS 同步失败（降级，仅存 ocr_text）{path}: {e}")
        conn.commit()
        return True
    finally:
        conn.close()
