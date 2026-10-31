#!/usr/bin/env python3
"""U10 第二轮交叉审计验收：三路并行审计（后端/前端/插件）发现的新缺陷。

数据安全：
1. 智能文件夹命中孤儿行不再 500（_backup_orphan_rows 7 列解包崩溃）
2. 目录重命名迁移全部后代 meta/file_tags（此前元数据集体失联）
3. restore "(恢复)" 副本唯一循环（此前 shutil.move 静默覆盖刚恢复的文件）
4. trash 入库失败回滚（此前文件"人间蒸发"）
5. purge 目录物理删除（此前恒进系统废纸篓）
6. 并发删除同名文件槽位互斥（O_CREAT|O_EXCL 占位）
一致性：
7. 标签改名/删除同步 legacy 串 + 撞名拒绝；改名撞名 400
8. 智能文件夹孤儿清理补 file_tags（标签计数不虚高）
9. 元数据导出 v2 含 tags/trash；导入值域钳制（color=99 → 5）
10. /api/search 截断后置到过滤之后（组合筛选不再静默不完整）
其它：
11. get_thumb_path 只读源目录回退全局缓存；filmstrip 路径单射
12. 前端静态断言：时间线跨批次组恢复/粘贴守卫/主题 CSS.escape/purge 撤销栈
13. 插件静态断言：format_factory 安全落位/关停协议、libav 音量线程侧、
    toast 真实标题、shutdown_timer 退出取消

隔离策略：DB 与 CONFIG_FILE 重定向到临时目录；trash 目录指向临时目录。"""
import json
import os
import sys
import tempfile
import threading

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


def src_of(rel):
    return os.path.join(FE_DIR, rel)


def main():
    tmp = tempfile.mkdtemp(prefix="fe_u10_")
    lib = os.path.join(tmp, "lib")
    os.makedirs(lib)
    trash_dir = os.path.join(tmp, "trash")
    os.makedirs(trash_dir)

    # --- 隔离：DB + CONFIG + 回收站目录重定向 ---
    db._topos_db_path = os.path.join(tmp, "topos.db")
    db._fallback_db_path = os.path.join(tmp, "metadata.db")
    db.DB_FILE = db._fallback_db_path
    db.CONFIG_FILE = os.path.join(tmp, "config.json")
    with open(db.CONFIG_FILE, 'w') as f:
        json.dump({"libraries": [{"name": "测试库", "path": lib}]}, f)
    db.init_db()

    import routes
    import trash
    import webapp

    trash.get_trash_dir = lambda: trash_dir  # 隔离：不写真实 .trash

    client = webapp.app.test_client()
    auth = {"headers": {"X-Auth-Token": webapp.APP_TOKEN, "Host": "localhost"}}

    def make_file(name, tags='', rating=0, color=0, flag=0, content=b"data"):
        p = os.path.join(lib, name).replace("\\", "/")
        with open(p, "wb") as f:
            f.write(content)
        conn = db._db()
        conn.execute(
            "INSERT OR REPLACE INTO file_meta (path, tags, comments, rating, color, flag) "
            "VALUES (?, ?, ?, ?, ?, ?)", (db._meta_key(p), tags, '', rating, color, flag))
        for t in [x.strip() for x in tags.split(',') if x.strip()]:
            conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (t,))
            conn.execute("""INSERT OR IGNORE INTO file_tags (file_path, tag_id)
                         SELECT ?, id FROM tags WHERE name = ?""", (db._meta_key(p), t))
        conn.commit()
        conn.close()
        return p

    def meta_row(path):
        conn = db._db()
        r = conn.execute("SELECT tags FROM file_meta WHERE path = ?",
                         (db._meta_key(path),)).fetchone()
        conn.close()
        return r

    def tag_count(tag):
        conn = db._db()
        r = conn.execute("""SELECT count(*) FROM file_tags ft JOIN tags t ON t.id = ft.tag_id
                            WHERE t.name = ?""", (tag,)).fetchone()
        conn.close()
        return r[0]

    # ==================================================================
    print("\n== 1. 智能文件夹孤儿行不再炸穿（P1：解包崩溃）==")
    orphan = make_file("orphan.jpg", tags="回忆", rating=5)
    os.remove(orphan)  # 应用外删除：meta 行残留
    r = client.post("/api/smart_folders", json={
        "name": "孤儿测试", "rules": {"min_rating": 3}}, **auth)
    folders = client.get("/api/smart_folders", **auth).get_json()
    sid = next((f["id"] for f in folders if f["name"] == "孤儿测试"), None)
    check("智能文件夹已创建", sid is not None)
    r = client.get(f"/api/smart_folders/{sid}/files", **auth)
    check("孤儿行场景智能文件夹不 500", r.status_code == 200,
          f"status={r.status_code}")
    check("孤儿 meta 行被清理（备份后）", meta_row(orphan) is None)
    backup = os.path.join(FE_DIR, ".assetdeck", "orphan_meta_backup.jsonl")
    if os.path.exists(backup):
        lines = [json.loads(x) for x in open(backup, encoding='utf-8')
                 if db._meta_key(orphan) in x]
        check("孤儿行备份 JSONL 含 color/flag 字段",
              lines and 'color' in lines[-1] and 'flag' in lines[-1])
    else:
        check("孤儿行备份 JSONL 含 color/flag 字段", False, "备份文件缺失")

    print("\n== 2. 目录重命名迁移后代 meta/file_tags（P1）==")
    sub = os.path.join(lib, "旧目录")
    os.makedirs(sub, exist_ok=True)
    f1 = os.path.join(sub, "a.jpg").replace("\\", "/")
    f2 = os.path.join(sub, "b.jpg").replace("\\", "/")
    for p, tg in ((f1, "风景"), (f2, "人像")):
        with open(p, "wb") as fh:
            fh.write(b"x")
        conn = db._db()
        conn.execute("INSERT OR REPLACE INTO file_meta (path, tags) VALUES (?, ?)",
                     (db._meta_key(p), tg))
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tg,))
        conn.execute("INSERT OR IGNORE INTO file_tags (file_path, tag_id) "
                     "SELECT ?, id FROM tags WHERE name = ?", (db._meta_key(p), tg))
        conn.commit()
        conn.close()
    r = client.post("/api/rename", json={
        "old_path": sub, "new_name": "新目录"}, **auth)
    check("目录重命名成功", r.get_json().get("success") is True)
    new_sub = os.path.join(lib, "新目录")
    nf1 = os.path.join(new_sub, "a.jpg").replace("\\", "/")
    check("后代 file_meta 路径平移", meta_row(nf1) is not None
          and meta_row(nf1)[0] == "风景", str(meta_row(nf1)))
    conn = db._db()
    n_ft = conn.execute("""SELECT count(*) FROM file_tags ft JOIN tags t ON t.id = ft.tag_id
                           WHERE ft.file_path = ? AND t.name = '人像'""",
                        (db._meta_key(os.path.join(new_sub, "b.jpg")),)).fetchone()[0]
    conn.close()
    check("后代 file_tags 路径平移", n_ft == 1)
    r = client.get("/api/search?tag=" + "风景", **auth)
    hits = [f['path'] for f in r.get_json().get('files', [])]
    check("改名后标签检索仍命中新路径", nf1 in hits, f"hits={len(hits)}")
    check("旧路径 meta 无残留", meta_row(f1) is None)

    print("\n== 3. restore (恢复) 副本唯一循环（P1：覆盖丢文件）==")
    x1 = make_file("同名片.jpg", content=b"FIRST")
    recs1, _ = trash.trash_items([x1])
    x2 = make_file("同名片.jpg", content=b"SECOND")
    recs2, _ = trash.trash_items([x2])
    # 预占 "(恢复)" 名，逼出计数循环第二档 "(恢复) (2)"
    with open(os.path.join(lib, "同名片 (恢复).jpg"), "wb") as f:
        f.write(b"OCCUPIED")
    restored, errors = trash.restore_items([recs1[0]['id'], recs2[0]['id']])
    p_first = os.path.join(lib, "同名片.jpg")
    p_occ = os.path.join(lib, "同名片 (恢复).jpg")
    p_cnt = os.path.join(lib, "同名片 (恢复) (2).jpg")
    check("两条记录全部恢复", len(restored) == 2 and not errors, f"errors={errors}")
    check("预占文件未被覆盖", open(p_occ, 'rb').read() == b"OCCUPIED")
    check("计数循环落到 (2)", os.path.exists(p_cnt) and open(p_cnt, 'rb').read() == b"SECOND"
          and open(p_first, 'rb').read() == b"FIRST")

    print("\n== 4. trash 入库失败回滚（P1：文件蒸发）==")
    y = make_file("回滚.jpg", content=b"ROLLBACK")
    orig_db = trash._db
    def boom():
        raise RuntimeError("db locked")
    trash._db = boom
    recs, errs = trash.trash_items([y])
    trash._db = orig_db
    check("入库失败报错误项", len(errs) == 1 and not recs)
    check("文件已移回原位", os.path.exists(y) and open(y, 'rb').read() == b"ROLLBACK")
    leftover = [f for f in os.listdir(trash_dir) if '回滚' in f]
    check("回收站目录无半成品残留", not leftover, str(leftover))

    print("\n== 5. purge 目录物理删除（不再进系统废纸篓）==")
    pdir = os.path.join(lib, "待清目录")
    os.makedirs(pdir, exist_ok=True)
    with open(os.path.join(pdir, "inner.jpg"), "wb") as f:
        f.write(b"i")
    recs, _ = trash.trash_items([pdir])
    check("目录删除入站", len(recs) == 1 and recs[0]['is_dir'])
    purged, errors = trash.purge_items([recs[0]['id']])
    src_in_trash = os.path.join(trash_dir, recs[0]['path'] and
                                os.path.basename(_slot_name(trash_dir, recs[0]['id'])))
    check("purge 成功", len(purged) == 1 and not errors, f"errors={errors}")
    remains = [f for f in os.listdir(trash_dir)]
    check("目录已从 .trash 物理移除（rmtree 而非系统废纸篓）", not remains, str(remains))

    print("\n== 6. 并发删除同名文件：槽位互斥（TOCTOU）==")
    d_a = os.path.join(lib, "并发A"); d_b = os.path.join(lib, "并发B")
    os.makedirs(d_a, exist_ok=True); os.makedirs(d_b, exist_ok=True)
    c1 = make_file("并发A/并发.jpg", content=b"C1")
    c2 = make_file("并发B/并发.jpg", content=b"C2")
    results = []
    def _del(p):
        results.append(trash.trash_items([p]))
    t1 = threading.Thread(target=_del, args=(c1,))
    t2 = threading.Thread(target=_del, args=(c2,))
    t1.start(); t2.start(); t1.join(); t2.join()
    n_ok = sum(1 for r, e in results if r and not e)
    files_in_trash = [f for f in os.listdir(trash_dir) if f.startswith('并发')]
    check("并发删除两条全部入站且物理共存", n_ok == 2 and len(files_in_trash) == 2,
          f"ok={n_ok} files={files_in_trash}")

    print("\n== 7. 标签改名/删除 legacy 同步 + 撞名拒绝 ==")
    tg = make_file("标签同步.jpg", tags="旧名,公共")
    conn = db._db()
    tid = conn.execute("SELECT id FROM tags WHERE name='旧名'").fetchone()[0]
    conn.close()
    r = client.put(f"/api/tags/{tid}", json={"name": "新名"}, **auth)
    check("标签改名成功", r.get_json().get("success") is True)
    check("legacy 串同步为新名", meta_row(tg)[0] in ("新名,公共", "公共,新名"),
          str(meta_row(tg)))
    conn = db._db()
    tid2 = conn.execute("SELECT id FROM tags WHERE name='公共'").fetchone()[0]
    conn.close()
    r = client.put(f"/api/tags/{tid2}", json={"name": "新名"}, **auth)
    check("改名撞已有标签被拒绝（400）", r.status_code == 400)
    r = client.delete(f"/api/tags/{tid}", **auth)
    check("标签删除成功", r.get_json().get("success") is True)
    check("legacy 串不再含已删标签", meta_row(tg)[0] == "公共", str(meta_row(tg)))

    print("\n== 8. 智能文件夹孤儿清理补 file_tags（计数不虚高）==")
    orphan2 = make_file("orphan2.jpg", tags="计数", rating=4)  # 需命中 min_rating=3 规则
    os.remove(orphan2)
    r = client.get(f"/api/smart_folders/{sid}/files", **auth)
    check("查询不 500 且清理执行", r.status_code == 200)
    check("孤儿 file_tags 已清（标签计数归零）", tag_count("计数") == 0,
          f"count={tag_count('计数')}")

    print("\n== 9. 导出 v2 + 导入值域钳制 ==")
    r = client.get("/api/meta/export", **auth)
    payload = json.loads(r.data)
    check("导出含 tags 定义与 trash 快照", 'tags' in payload and 'trash' in payload
          and payload.get('version') >= 2)
    z = make_file("导入.jpg", content=b"Z")
    r = client.post("/api/meta/import", json={
        "rows": [{"path": z, "tags": "", "rating": -3, "color": 99, "flag": 77}],
        "mode": "replace"}, **auth)
    check("导入成功", r.get_json().get("success") is True)
    conn = db._db()
    row = conn.execute("SELECT rating, color, flag FROM file_meta WHERE path = ?",
                       (db._meta_key(z),)).fetchone()
    conn.close()
    check("rating/color/flag 钳制到值域", row == (0, 5, 2), str(row))

    print("\n== 10. /api/search 截断后置（组合筛选不丢结果）==")
    bulk_dir = os.path.join(lib, "bulk")
    os.makedirs(bulk_dir, exist_ok=True)
    conn = db._db()
    for i in range(2049):
        p = os.path.join(bulk_dir, f"zzq_{i:04d}.jpg").replace("\\", "/")
        with open(p, "wb") as f:
            f.write(b"b")
        conn.execute("INSERT OR REPLACE INTO file_meta (path, tags) VALUES (?, '')",
                     (db._meta_key(p),))
    colored = os.path.join(bulk_dir, "zzq_colored.jpg").replace("\\", "/")
    with open(colored, "wb") as f:
        f.write(b"c")
    conn.execute("INSERT OR REPLACE INTO file_meta (path, tags, color) VALUES (?, '', 5)",
                 (db._meta_key(colored),))
    conn.commit()
    conn.close()
    r = client.get("/api/search?q=zzq&color=5", **auth)
    hits = [f['path'] for f in r.get_json().get('files', [])]
    check("2050 命中 + 过滤后仍含截断区外的颜色标记文件", colored in hits,
          f"hits={len(hits)}")

    print("\n== 11. 缩略图只读回退 + filmstrip 单射 ==")
    from thumb_engine import engine as tengine
    ro = os.path.join(lib, "只读")
    os.makedirs(ro, exist_ok=True)
    os.chmod(ro, 0o555)
    try:
        p = tengine.get_thumb_path(os.path.join(ro, "a.jpg").replace("\\", "/"))
        check("只读源目录回退全局缓存路径", not p.startswith(ro), p[-40:])
    finally:
        os.chmod(ro, 0o755)
    check("filmstrip 路径单射（clip.v2 vs clip_v2）",
          tengine.get_filmstrip_path("/x/clip.v2.mp4")
          != tengine.get_filmstrip_path("/x/clip_v2.mp4"))

    print("\n== 12. 前端静态断言 ==")
    fe_render = open(src_of("static/assets/js/fe-render.js"), encoding='utf-8').read()
    fe_actions = open(src_of("static/assets/js/fe-actions.js"), encoding='utf-8').read()
    check("时间线跨批次从 DOM 恢复组容器",
          "document.getElementById(`tlg-${STATE.tlLastKey}`)" in fe_render)
    check("背景粘贴虚拟视图守卫",
          # [V5 复查] 三 scheme startsWith 链升级为 isVirtualView 五 scheme 判定
          "if (path && isVirtualView(path)) return window.showToast" in fe_actions)
    check("主题选择器 CSS.escape", "CSS.escape(name)" in fe_actions)
    check("purge 仅成功才剔除撤销栈",
          "if (data.success) window._undoStack = window._undoStack.filter"
          in fe_actions)
    check("搜索历史非数组防护", "Array.isArray(v) ? v : []" in fe_actions)
    check("search:// 跳过本地二次过滤", "serverFiltered" in fe_render)
    check("对比视图屏蔽全局快捷键", "$('#compareView') && $('#compareView').style.display !== 'none'"
          in fe_actions)
    check("拖拽上传虚拟视图拦截", "虚拟视图，请先进入一个真实目录再上传" in fe_actions)
    check("strip URL 单引号转义", ".replace(/'/g, '%27')" in fe_actions)
    check("灯箱 ESC 停止幻灯片", "STATE.lightbox.active=false; window.stopSlideshow?.(); }" in fe_render)
    check("IO unobserve 助手已导出", "unobserveThumbImgs," in fe_render)

    print("\n== 13. 插件静态断言 ==")
    conv = open(src_of("plugins/format_factory/converter_gui.py"), encoding='utf-8').read()
    check("转码安全落位助手（同名不销毁源）", "def finalize_output" in conv
          and "move_to_trash(final_name)" in conv)
    check("转码失败路径清理半成品", conv.count("Utils._cleanup_tmp(tmp)") >= 4)
    check("CardLoader 纳入关停协议", "tab._loader" in conv)
    base_p = open(src_of("plugins/video_pro/base_player.py"), encoding='utf-8').read()
    check("display_size 只读缓存", "_size_cache" in base_p
          and "refresh_size_cache" in base_p)
    check("toast 真实标题还原", "_real_title" in base_p)
    libav = open(src_of("plugins/video_pro/libav_player.py"), encoding='utf-8').read()
    check("音量线程内应用（GUI 侧只记账）", "applied_vol" in libav
          and "self._audio.set_volume(self.slider_vol.value() / 100.0)" in libav)
    check("截断文件停止复用 EOF 收尾", "_on_decode_stop" in libav
          and libav.index("_on_decode_stop") < libav.index("self._eof_sig.emit()", libav.index("_on_decode_stop")))
    qt_p = open(src_of("plugins/video_pro/player_qt.py"), encoding='utf-8').read()
    check("player_qt toast 代次令牌", "_toast_gen" in qt_p)
    timer_g = open(src_of("plugins/shutdown_timer/timer_gui.py"), encoding='utf-8').read()
    check("退出前取消已排定关机", "_cancel_on_app_quit" in timer_g)
    check("到期分支取消竞态防护", "_cancel_requested" in timer_g)
    img_g = open(src_of("plugins/image_factory/image_gui.py"), encoding='utf-8').read()
    check("进程池崩溃不逃逸", "处理进程异常终止" in img_g)
    window_p = open(src_of("window.py"), encoding='utf-8').read()
    check("copy_finished 按发送者绑定", "lambda errs, w=worker: self.copy_finished(errs, w)"
          in window_p)
    routes_p = open(src_of("routes.py"), encoding='utf-8').read()
    check("智能查询/搜索共用 scan_cache", "scan_cache=scan_cache" in routes_p)
    check("strip 失败缓存专属键", "check_failure_cache(path + ':strip')" in routes_p)

    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


def _slot_name(trash_dir, rid):
    import trash as _t
    conn = _t._db()
    row = conn.execute("SELECT trashed_name FROM trash WHERE id = ?", (rid,)).fetchone()
    conn.close()
    return row[0] if row else ''


if __name__ == "__main__":
    sys.exit(main())
