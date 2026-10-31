#!/usr/bin/env python3
"""V1 数据地基批验收：GAP-15 索引+WAL / GAP-02 技术元数据落库 / GAP-01 索引器 / GAP-03 监视。

1.  db 迁移：技术列齐全、显式索引存在、WAL 生效
2.  utils.get_tech_meta：mtime/size 恒有；图片分辨率（Pillow 兜底）；exiftool 存在时字段更多
3.  scanner.update_tech_meta：无行时自动建行；不覆盖用户列
4.  全库索引器：新增文件入库（mtime/size）、用户元数据不被覆盖、
    mtime 变更 → updated、删除文件 → missing=1、missing 行不误删
5.  scan_directory 单目录增量（watcher 去抖后的落点）
6.  /api/scan/start(409 并发防护)/status 端点
7.  /api/list 下发 duration/date_taken(_ts)
8.  /api/search 技术过滤：min_duration / date_from..date_to / camera / has_gps
9.  execute_smart_query 同款规则（智能文件夹）
10. 前端静态断言：时长角标/时间线基准切换/日期时长筛选/索引状态块/监视接线

隔离策略：DB 与 CONFIG_FILE 均重定向到临时目录。"""
import json
import os
import sys
import tempfile
import time

FE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, FE_DIR)

import db  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}" + (f"（{detail}）" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f"（{detail}）" if detail else ""))


def make_jpeg(path, size=(64, 48)):
    from PIL import Image
    img = Image.new("RGB", size, (200, 30, 30))
    img.save(path, "JPEG")
    return path


def main():
    tmp = tempfile.mkdtemp(prefix="fe_v1_")
    lib = os.path.join(tmp, "lib")
    os.makedirs(lib)

    # --- 隔离：DB + CONFIG 重定向 ---
    db._topos_db_path = os.path.join(tmp, "topos.db")
    db._fallback_db_path = os.path.join(tmp, "metadata.db")
    db.DB_FILE = db._fallback_db_path
    db.CONFIG_FILE = os.path.join(tmp, "config.json")
    with open(db.CONFIG_FILE, 'w') as f:
        json.dump({"libraries": [{"name": "测试库", "path": lib}]}, f)
    db.init_db()

    import routes
    import scanner
    import utils
    import webapp

    client = webapp.app.test_client()
    auth = {"headers": {"X-Auth-Token": webapp.APP_TOKEN, "Host": "localhost"}}

    print("\n== 1. DB 迁移：技术列 / 索引 / WAL ==")
    conn = db._db()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(file_meta)").fetchall()}
    need_cols = {'mtime', 'size', 'date_taken', 'width', 'height', 'duration', 'codec',
                 'camera', 'lens', 'iso', 'aperture', 'shutter', 'focal',
                 'gps_lat', 'gps_lon', 'dom_color', 'phash', 'stack_id',
                 'missing', 'indexed_at'}
    check("技术元数据列全部就位", need_cols <= cols, f"缺: {need_cols - cols}" if not need_cols <= cols else "20 列")
    idx_names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'").fetchall()}
    check("显式索引存在", {'idx_file_tags_tag', 'idx_meta_rating', 'idx_meta_date_taken',
                          'idx_meta_duration', 'idx_meta_missing', 'idx_meta_stack'} <= idx_names)
    jm = conn.execute("PRAGMA journal_mode").fetchone()[0]
    check("WAL 生效", jm.lower() == 'wal', jm)
    conn.close()

    print("\n== 2. utils.get_tech_meta ==")
    jp = make_jpeg(os.path.join(lib, "red.jpg"))
    meta = utils.get_tech_meta(jp)
    check("mtime/size 恒有", meta.get('mtime') and meta.get('size'))
    check("图片分辨率（Pillow 兜底）", meta.get('width') == 64 and meta.get('height') == 48,
          f"{meta.get('width')}x{meta.get('height')}")
    check("不可读文件返回空", utils.get_tech_meta(os.path.join(lib, "nope.jpg")) == {})

    print("\n== 3. update_tech_meta：自动建行 + 不覆盖用户列 ==")
    key = db._meta_key(jp)
    conn = db._db()
    conn.execute("INSERT INTO file_meta (path, tags, comments, rating, color) VALUES (?, '客户A', '备注x', 4, 3)",
                 (key,))
    conn.commit()
    conn.close()
    ok = scanner.update_tech_meta(jp)
    check("有行回写成功", ok)
    conn = db._db()
    row = conn.execute("SELECT tags, comments, rating, color, width, height FROM file_meta WHERE path = ?",
                       (key,)).fetchone()
    conn.close()
    check("用户列原样保留", row[0] == '客户A' and row[1] == '备注x' and row[2] == 4 and row[3] == 3)
    check("技术列已回填", row[4] == 64 and row[5] == 48)
    jp2 = make_jpeg(os.path.join(lib, "ghost.jpg"))
    check("无行时自动建行", scanner.update_tech_meta(jp2))
    conn = db._db()
    r2 = conn.execute("SELECT rating, tags, width FROM file_meta WHERE path = ?",
                      (db._meta_key(jp2),)).fetchone()
    conn.close()
    check("新建行默认值正确", r2[0] == 0 and r2[1] == '' and r2[2] == 64)

    print("\n== 4. 全库索引器 ==")
    # 库内造文件：一个全新、一个已打标（防覆盖验证）、之后一个删除（missing）
    f_new = os.path.join(lib, "brand_new.jpg")
    make_jpeg(f_new)
    f_tagged = make_jpeg(os.path.join(lib, "tagged.jpg"))
    conn = db._db()
    conn.execute("INSERT INTO file_meta (path, tags, rating, flag) VALUES (?, '精选', 5, 1)",
                 (db._meta_key(f_tagged),))
    conn.commit()
    conn.close()

    check("start 返回 True", scanner.start(lib))
    for _ in range(200):
        if not scanner.status()['running']:
            break
        time.sleep(0.05)
    st = scanner.status()
    check("扫描完成", not st['running'] and st['phase'] == '完成', str(st['last_result']))
    conn = db._db()
    r_new = conn.execute("SELECT mtime, size, missing, indexed_at FROM file_meta WHERE path = ?",
                         (db._meta_key(f_new),)).fetchone()
    r_tag = conn.execute("SELECT tags, rating, flag, missing FROM file_meta WHERE path = ?",
                         (db._meta_key(f_tagged),)).fetchone()
    conn.close()
    check("新文件入库（mtime/size/indexed_at）",
          r_new and r_new[0] > 0 and r_new[1] > 0 and r_new[3], str(r_new))
    check("已有行用户元数据未被覆盖", r_tag[0] == '精选' and r_tag[1] == 5 and r_tag[2] == 1)
    for _ in range(200):
        if not scanner.status()['running']:
            break
        time.sleep(0.05)

    # 变更检测：touch mtime
    fut = time.time() + 10
    os.utime(f_new, (fut, fut))
    scanner.start(lib)
    for _ in range(200):
        if not scanner.status()['running']:
            break
        time.sleep(0.05)
    conn = db._db()
    r_new2 = conn.execute("SELECT mtime FROM file_meta WHERE path = ?", (db._meta_key(f_new),)).fetchone()
    conn.close()
    check("mtime 变更被捕获（updated 路径）", r_new2 and abs(r_new2[0] - fut) < 1)

    # 删除 → missing=1（不删行）
    os.remove(f_new)
    scanner.start(lib)
    for _ in range(200):
        if not scanner.status()['running']:
            break
        time.sleep(0.05)
    conn = db._db()
    r_gone = conn.execute("SELECT missing FROM file_meta WHERE path = ?", (db._meta_key(f_new),)).fetchone()
    conn.close()
    check("删除文件 → missing=1（行保留）", r_gone and r_gone[0] == 1)

    print("\n== 5. scan_directory 单目录增量 ==")
    sub = os.path.join(lib, "sub")
    os.makedirs(sub, exist_ok=True)
    f_sub = make_jpeg(os.path.join(sub, "in_sub.jpg"))
    scanner.scan_directory(sub)
    conn = db._db()
    r_sub = conn.execute("SELECT missing FROM file_meta WHERE path = ?", (db._meta_key(f_sub),)).fetchone()
    conn.close()
    check("新目录文件入库", r_sub is not None and r_sub[0] == 0)
    os.remove(f_sub)
    scanner.scan_directory(sub)
    conn = db._db()
    r_sub2 = conn.execute("SELECT missing FROM file_meta WHERE path = ?", (db._meta_key(f_sub),)).fetchone()
    conn.close()
    check("目录内删除 → missing=1", r_sub2 and r_sub2[0] == 1)

    print("\n== 6. /api/scan 端点 ==")
    r = client.post("/api/scan/start", json={}, **auth)
    check("start 200/409", r.status_code in (200, 409), str(r.status_code))
    r2 = client.post("/api/scan/start", json={}, **auth)
    check("并发 start 返回 409", r2.status_code == 409, str(r2.status_code))
    for _ in range(200):
        if not scanner.status()['running']:
            break
        time.sleep(0.05)
    r = client.get("/api/scan/status", **auth)
    d = r.get_json()
    check("status 结构", r.status_code == 200 and {'running', 'phase', 'scanned', 'total', 'last_result'} <= set(d))
    r = client.get("/api/scan/status")
    check("status 需要 token", r.status_code == 401)

    print("\n== 7. /api/list 下发技术字段 ==")
    # tagged.jpg 有行（rating=5）；给它的 duration/date_taken/camera 手工回填模拟索引产物
    conn = db._db()
    conn.execute("UPDATE file_meta SET duration = 95.5, date_taken = 1700000000, camera = 'Sony A7M4' WHERE path = ?",
                 (db._meta_key(f_tagged),))
    conn.commit()
    conn.close()
    r = client.get(f"/api/list?path={lib}", **auth)
    files = {f['name']: f for f in r.get_json()['files']}
    check("列表带 duration/date_taken 键", 'duration' in files.get('tagged.jpg', {}))
    check("已回填文件携带值", abs(files['tagged.jpg']['duration'] - 95.5) < 0.01
          and files['tagged.jpg']['date_taken'] == '2023-11-14', str(files['tagged.jpg'].get('date_taken')))

    print("\n== 8. /api/search 技术过滤 ==")
    r = client.get("/api/search?min_duration=60", **auth)
    names = {f['name'] for f in r.get_json()['files']}
    check("min_duration 命中长视频", 'tagged.jpg' in names and 'tagged.jpg' not in names or 'tagged.jpg' in names)
    check("min_duration 排除未回填文件", 'red.jpg' not in names and 'ghost.jpg' not in names)
    r = client.get("/api/search?min_duration=120", **auth)
    check("更高端点排除", all(f['name'] != 'tagged.jpg' for f in r.get_json()['files']))
    r = client.get("/api/search?date_from=2023-11-13&date_to=2023-11-15", **auth)
    names = {f['name'] for f in r.get_json()['files']}
    check("拍摄日期范围命中", 'tagged.jpg' in names and 'red.jpg' not in names)
    r = client.get("/api/search?camera=sony", **auth)
    check("相机子串（大小写不敏感 LIKE）", any(f['name'] == 'tagged.jpg' for f in r.get_json()['files']))
    r = client.get("/api/search?has_gps=1", **auth)
    check("has_gps 无匹配（未回填 GPS）", r.get_json()['files'] == [])

    print("\n== 9. 智能文件夹技术规则 ==")
    files = routes.execute_smart_query({'min_duration': 60})
    check("min_duration 规则", any(f['name'] == 'tagged.jpg' for f in files))
    files = routes.execute_smart_query({'date_from': '2023-11-13', 'date_to': '2023-11-15'})
    check("date_from/date_to 规则", any(f['name'] == 'tagged.jpg' for f in files)
          and all(f['name'] != 'red.jpg' for f in files))
    files = routes.execute_smart_query({'camera': 'Sony'})
    check("camera 规则", any(f['name'] == 'tagged.jpg' for f in files))
    files = routes.execute_smart_query({'min_duration': 60})
    hit = next((f for f in files if f['name'] == 'tagged.jpg'), None)
    check("规则结果带 duration/date_taken 字段", hit and hit.get('duration') and hit.get('date_taken') == '2023-11-14')

    print("\n== 10. 前端/接线静态断言 ==")
    def src(rel):
        with open(os.path.join(FE_DIR, rel), encoding='utf-8') as f:
            return f.read()
    html = src("templates/index.html")
    check("筛选菜单含拍摄日期", 'data-change-action="setFilterDateFrom"' in html and 'data-change-action="setFilterDateTo"' in html)
    check("筛选菜单含时长档位", 'data-filter-duration="60"' in html)
    check("侧栏索引状态块", 'id="scanStatusText"' in html and 'startLibraryScan' in html)
    check("时间线基准切换", 'id="timelineBaseSel"' in html and 'setTimelineBase' in html)
    actions = src("static/assets/js/fe-actions.js")
    check("索引状态轮询", 'refreshScanStatus' in actions and '/api/scan/status' in actions)
    check("switchLayout 联动基准选择器", 'timelineBaseSel' in actions)
    render = src("static/assets/js/fe-render.js")
    check("时长角标渲染", 'dur-badge' in render and 'fmtDuration' in render)
    check("时间线键函数", 'tlKeyOf' in render and 'timelineBase' in render)
    check("日期/时长前端过滤", 'date_taken_ts' in render and 'minDuration' in render)
    state = src("static/assets/js/fe-state.js")
    check("STATE.filter 新维度", 'dateFrom' in state and 'timelineBase' in state)
    db_src = src("db.py")
    check("WAL 接线", 'journal_mode=WAL' in db_src)
    bridge_src = src("bridge.py")
    check("ensure_runtime 启动索引器", 'start_autoscan' in bridge_src)
    check("ensure_runtime 启动监视", 'setup_watcher' in bridge_src)
    window_src = src("window.py")
    check("独立模式补挂监视", 'setup_watcher' in window_src)
    watcher_src = src("watcher.py")
    check("监视器去抖/上限/无 Qt 降级", 'WATCH_DEBOUNCE_MS' in watcher_src and 'WATCH_MAX_PATHS' in watcher_src
          and 'QCoreApplication.instance()' in watcher_src)
    thumbs_src = src("thumbs.py")
    check("元数据队列回写技术列", 'update_tech_meta' in thumbs_src)

    print(f"\n{'='*46}\nV1 验收：{PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
