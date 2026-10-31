#!/usr/bin/env python3
"""U9 审计修复批验收：交叉审计发现的漏网缺陷。

数据安全：
1. 同秒删除两个同名文件 → .trash 内物理共存（此前静默覆盖 = 数据丢失）
2. INSERT OR REPLACE 改评分 → FTS 索引无幽灵行（recursive_triggers）
3. trash restore 元数据回写失败 → 文件回滚进回收站、记录保留（非僵尸）
标签一致性：
4. rename/batch_move 后结构化标签（file_tags）随迁，/api/search?tag= 命中新路径
5. 彻底删除清理 file_tags（标签计数回收）
路径语义：
6. trash LIKE 前缀转义：删 my_dir 不误删 myXdir 的 meta
7. 搜索关键词含 % / _ 按字面匹配
其它：
8. 胶片条路径按扩展名区分；批量旋转 EXIF Orientation 归 1 + 缩略图失效
9. duplicates/scan 返回 truncated 字段；/api/file 目录 400
10. 前端：DOMPurify 消毒、undo 死条目不回栈、虚拟视图刷新分流（静态断言）

隔离策略：DB 与 CONFIG_FILE 均重定向到临时目录。"""
import io
import json
import os
import sys
import tempfile

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


def main():
    tmp = tempfile.mkdtemp(prefix="fe_u9_")
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
    import trash
    import webapp

    client = webapp.app.test_client()
    auth = {"headers": {"X-Auth-Token": webapp.APP_TOKEN, "Host": "localhost"}}

    def make_file(name, tags='', rating=0, color=0, flag=0):
        p = os.path.join(lib, name).replace("\\", "/")
        with open(p, "wb") as f:
            f.write(b"data")
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

    def fts_count(path):
        conn = db._db()
        n = conn.execute("SELECT count(*) FROM file_meta_fts WHERE path = ?",
                         (db._meta_key(path),)).fetchone()[0]
        conn.close()
        return n

    print("\n== 1. 回收站同秒同名覆盖（P0）==")
    d1 = os.path.join(lib, "d1")
    d2 = os.path.join(lib, "d2")
    os.makedirs(d1, exist_ok=True)
    os.makedirs(d2, exist_ok=True)
    a = os.path.join(d1, "IMG_0001.jpg").replace("\\", "/")
    b = os.path.join(d2, "IMG_0001.jpg").replace("\\", "/")
    for i, p in enumerate((a, b)):
        with open(p, "wb") as f:
            f.write(os.urandom(16 * (i + 1)) + p.encode())  # 刻意不同长度
    r = client.post("/api/delete", json={"paths": [a, b]}, **auth)
    data = r.get_json()
    check("两条删除均成功", len(data.get("records", [])) == 2 and not data.get("errors"),
          str(data.get("errors")))
    conn = db._db()
    rows = conn.execute(
        "SELECT trashed_name FROM trash WHERE item_name = 'IMG_0001.jpg' ORDER BY id"
    ).fetchall()
    conn.close()
    trashed_names = [r[0] for r in rows]
    # 注意：.trash 为 FE 固定目录、跨多次运行共享，按本轮 DB 记录断言
    check("两条 trash 记录指向不同物理文件",
          len(trashed_names) == 2 and len(set(trashed_names)) == 2 and all(trashed_names),
          str(trashed_names))
    sizes = {os.path.getsize(os.path.join(trash.get_trash_dir(), tn))
             for tn in trashed_names}
    check("两个实体内容互不覆盖", len(sizes) == 2 and 0 not in sizes, str(sizes))

    print("\n== 2. FTS 幽灵行（INSERT OR REPLACE）==")
    g = make_file("ghost.jpg", tags="风景")
    r = client.post("/api/update_meta", json={"path": g, "rating": 3}, **auth)
    check("update_meta 成功", r.status_code == 200 and r.get_json().get("success"))
    r = client.post("/api/update_meta", json={"path": g, "rating": 4}, **auth)
    client.post("/api/update_meta", json={"path": g, "comments": "备注"}, **auth)
    check("三次 REPLACE 后 FTS 无重复行", fts_count(g) == 1, f"count={fts_count(g)}")
    r = client.get(f"/api/search?q=ghost.jpg", **auth)
    hits = [f["path"] for f in r.get_json().get("files", [])]
    check("FTS 搜索仍命中该文件", hits == [g], str(hits))
    conn = db._db()
    dup_total = conn.execute(
        """SELECT count(*) FROM (SELECT path FROM file_meta_fts
           GROUP BY path HAVING count(*) > 1)""").fetchone()[0]
    conn.close()
    check("全库无同 path 幽灵行", dup_total == 0)

    print("\n== 3. restore 原子性（DB 回写失败 → 文件回滚进回收站）==")
    c = make_file("atomic.jpg", tags="重要", rating=5)
    r = client.post("/api/delete", json={"paths": [c]}, **auth)
    rid = r.get_json()["records"][0]["id"]
    check("删除成功", not os.path.exists(c))
    # 模拟元数据回写阶段 DB 故障：commit 抛异常
    # （sqlite3.Connection 不允许实例级赋值，用代理包装）
    real_db = trash._db

    class _BrokenConn:
        def __init__(self, conn):
            self._c = conn

        def cursor(self):
            return self._c.cursor()

        def execute(self, *a, **k):
            return self._c.execute(*a, **k)

        def commit(self):
            raise RuntimeError("simulated db busy")

        def rollback(self):
            self._c.rollback()

        def close(self):
            self._c.close()

    def broken_db():
        return _BrokenConn(real_db())

    trash._db = broken_db
    try:
        restored, errors = trash.restore_items([rid])
    finally:
        trash._db = real_db
    check("恢复报错而非静默", bool(errors), str(errors))
    in_trash = [fn for fn in os.listdir(trash.get_trash_dir())
                if fn.startswith("atomic__") and fn.endswith(".jpg")]
    check("文件回滚进回收站（未滞留原位置）",
          not os.path.exists(c) and len(in_trash) >= 1, str(in_trash))
    conn = db._db()
    still = conn.execute("SELECT 1 FROM trash WHERE id = ?", (rid,)).fetchone()
    conn.close()
    check("trash 记录保留（可再次恢复）", still is not None)
    restored2, errors2 = trash.restore_items([rid])
    check("故障解除后可正常恢复",
          len(restored2) == 1 and os.path.exists(c) and not errors2, str(errors2))

    print("\n== 4. rename / batch_move 迁移结构化标签 ==")
    t1 = make_file("tagged_a.jpg", tags="产品图")
    conn = db._db()
    n0 = conn.execute("""SELECT count(*) FROM file_tags ft JOIN tags t ON t.id = ft.tag_id
                         WHERE t.name = '产品图'""").fetchone()[0]
    conn.close()
    check("打标后 file_tags 有行", n0 == 1)
    new1 = os.path.join(lib, "tagged_b.jpg").replace("\\", "/")
    r = client.post("/api/rename", json={"old_path": t1, "new_name": "tagged_b.jpg"}, **auth)
    check("rename 成功", r.get_json().get("success"), str(r.get_json()))
    r = client.get("/api/search?tag=产品图", **auth)
    hits = [f["path"] for f in r.get_json().get("files", [])]
    check("rename 后按标签检索命中新路径", hits == [new1], str(hits))
    dest = os.path.join(lib, "moved").replace("\\", "/")
    os.makedirs(dest, exist_ok=True)
    r = client.post("/api/batch/move", json={"paths": [new1], "dest": dest}, **auth)
    check("batch_move 成功", r.get_json().get("success"), str(r.get_json()))
    r = client.get("/api/search?tag=产品图", **auth)
    hits = [f["path"] for f in r.get_json().get("files", [])]
    moved_path = os.path.join(dest, "tagged_b.jpg").replace("\\", "/")
    check("batch_move 后按标签检索命中新路径", hits == [moved_path], str(hits))
    conn = db._db()
    orphan_tags = conn.execute(
        "SELECT count(*) FROM file_tags WHERE file_path NOT IN (SELECT path FROM file_meta)"
    ).fetchone()[0]
    conn.close()
    check("无孤儿 file_tags 行", orphan_tags == 0)

    print("\n== 5. 彻底删除清理 file_tags ==")
    t2 = make_file("perm.jpg", tags="临时标签")
    r = client.post("/api/delete", json={"paths": [t2], "permanent": True}, **auth)
    check("彻底删除成功", r.status_code == 200 and not r.get_json().get("errors"))
    conn = db._db()
    left = conn.execute("""SELECT count(*) FROM file_tags ft JOIN tags t ON t.id = ft.tag_id
                           WHERE t.name = '临时标签'""").fetchone()[0]
    tag_left = conn.execute("SELECT count(*) FROM tags WHERE name = '临时标签'").fetchone()[0]
    conn.close()
    check("file_tags 已清理", left == 0)
    check("空标签已回收", tag_left == 0)

    print("\n== 6. trash LIKE 通配符转义 ==")
    d_under = os.path.join(lib, "my_dir")
    d_other = os.path.join(lib, "myXdir")
    os.makedirs(d_under, exist_ok=True)
    os.makedirs(d_other, exist_ok=True)
    f_in = make_file("my_dir/inner.txt", tags="目录内")
    f_out = make_file("myXdir/outer.txt", tags="目录外")
    r = client.post("/api/delete", json={"paths": [d_under]}, **auth)
    check("目录删除成功", r.status_code == 200 and not r.get_json().get("errors"))
    conn = db._db()
    out_left = conn.execute("SELECT 1 FROM file_meta WHERE path = ?",
                            (db._meta_key(f_out),)).fetchone()
    conn.close()
    check("无关目录（_ 通配误伤）meta 未被删", out_left is not None)
    # 恢复验证目录内 meta 仍在快照中
    conn = db._db()
    snap = conn.execute("SELECT meta_json FROM trash WHERE item_name = 'my_dir'").fetchone()
    conn.close()
    rows = json.loads(snap[0]) if snap else []
    check("目录快照含后代 meta（未漏）", any(r0["path"].endswith("inner.txt") for r0 in rows))

    print("\n== 7. 搜索关键词 % / _ 字面匹配 ==")
    make_file("100_percent.txt")
    make_file("100Xpercent.txt")
    r = client.get("/api/search?q=100_percent", **auth)
    hits = [f["name"] for f in r.get_json().get("files", [])]
    check("% 不作通配符", hits == ["100_percent.txt"], str(hits))

    print("\n== 8. 胶片条路径 / 批量旋转 ==")
    from thumb_engine import engine as te
    s1 = te.get_filmstrip_path(os.path.join(lib, "a.mp4"))
    s2 = te.get_filmstrip_path(os.path.join(lib, "a.mov"))
    check("同名不同扩展名胶片条路径不同", s1 != s2, f"{os.path.basename(s1)} vs {os.path.basename(s2)}")

    from PIL import Image
    rot = os.path.join(lib, "rot.jpg").replace("\\", "/")
    img = Image.new("RGB", (40, 20), (255, 0, 0))
    ex = img.getexif()
    ex[0x0112] = 6  # Orientation: 需顺时针 90° 摆正
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=ex.tobytes())
    with open(rot, "wb") as f:
        f.write(buf.getvalue())
    from thumb_engine import engine as thumb_engine
    tp = thumb_engine.get_thumb_path(rot, size_type="small")
    os.makedirs(os.path.dirname(tp), exist_ok=True)
    with open(tp, "w") as f:
        f.write("stale")
    r = client.post("/api/batch/rotate", json={"paths": [rot], "degrees": 90}, **auth)
    check("旋转成功", r.get_json().get("success") and rot in r.get_json().get("rotated", []),
          str(r.get_json()))
    with Image.open(rot) as out:
        # 原图 40x20 横存 + Orientation 6（显示为 20x40 竖图）；
        # 摆正后 20x40，再顺时针 90° → 存储像素 40x20、Orientation=1
        check("尺寸符合摆正+旋转的预期", out.size == (40, 20), str(out.size))
        o = out.getexif().get(0x0112, 1)
        check("EXIF Orientation 归 1（无双重旋转）", o == 1, f"orientation={o}")
    check("旧缩略图已删除（将按新方向重生成）", not os.path.exists(tp))

    print("\n== 9. duplicates 上限字段 / /api/file 目录 400 ==")
    ddup = os.path.join(lib, "dupscan")
    os.makedirs(ddup, exist_ok=True)
    for i in range(3):
        with open(os.path.join(ddup, f"same{i}.bin"), "wb") as f:
            f.write(b"identical-content")
    with open(os.path.join(ddup, "unique.bin"), "wb") as f:
        f.write(b"unique")
    r = client.get(f"/api/duplicates/scan?path={ddup}", **auth)
    d = r.get_json()
    check("识别 1 组重复", len(d.get("groups", [])) == 1 and len(d["groups"][0]["files"]) == 3,
          str(d.get("groups")))
    check("返回 truncated 字段", d.get("truncated") is False)
    r = client.get(f"/api/file?path={lib}", **auth)
    check("/api/file 目录请求 400", r.status_code == 400, f"status={r.status_code}")
    r = client.get("/api/search?q=角度_特殊", **auth)
    check("search 含 _ 关键词不 500", r.status_code == 200)

    print("\n== 10. 前端静态断言 ==")
    actions = open(os.path.join(FE_DIR, "static/assets/js/fe-actions.js"), encoding="utf-8").read()
    check("undo 死条目不回栈（permanent 判定）",
          "记录不存在|已不在回收站" in actions and "permanent" in actions)
    check("purge/empty 后剔除撤销栈条目",
          "_undoStack = window._undoStack.filter" in actions)
    check("openTrashView 竞态令牌",
          "window.openTrashView = async () => {" in actions
          and actions.count("const seq = (window._loadSeq") >= 4)
    check("reloadFiles 虚拟视图分流", "search://tag/" in actions and "openTrashView(); return;" in actions)
    check("重复删除后重绘分组", "window.renderDupResults = " in actions
          and "window._dupGroups = groups" in actions)
    render = open(os.path.join(FE_DIR, "static/assets/js/fe-render.js"), encoding="utf-8").read()
    check("marked 输出经 DOMPurify 消毒",
          "DOMPurify.sanitize(marked.parse" in render or
          ("DOMPurify" in render and "marked.parse(t)" in render))
    html = open(os.path.join(FE_DIR, "templates/index.html"), encoding="utf-8").read()
    check("index.html 引入 purify.min.js", "vendor/purify/purify.min.js" in html)
    check("DOMPurify 本地文件存在",
          os.path.isfile(os.path.join(FE_DIR, "static/vendor/purify/purify.min.js")))

    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
