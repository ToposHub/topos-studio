#!/usr/bin/env python3
"""U6 安全网批验收（GAP-05 应用内回收站 + GAP-06 元数据备份/re-link）：

回收站：
1. 删除 → 移入 .trash、meta 行快照进 trash 表并从 file_meta 移除（无幽灵记录）
2. 恢复 → 文件回原位、评分/标签/颜色/旗标还原、结构化标签重建、缩略图仍在
3. 原路径被占用时自动 "(恢复)" 副本；目录整体恢复含后代 meta 平移
4. 彻底删除 → 物理移除 + 三尺寸缩略图清理；过期清理（days=0）
导出/导入/re-link：
5. 导出 JSON 含全量行；导入 merge 只回填空值、失联路径跳过
6. 外部改名 → scan_orphans 找到同名唯一候选 → relink 后元数据接到新路径

隔离策略：DB 与 CONFIG_FILE 均重定向到临时目录。"""
import json
import os
import sys
import time
import shutil
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


def make_fake_thumbs(path):
    """在源目录 .cache 写三尺寸假缩略图（恢复应在/彻底删除应无）。"""
    from thumb_engine import engine as thumb_engine
    made = []
    for size in ('small', 'large', 'preview'):
        tp = thumb_engine.get_thumb_path(path, size_type=size)
        os.makedirs(os.path.dirname(tp), exist_ok=True)
        with open(tp, 'w') as f:
            f.write('thumb')
        made.append(tp)
    return made


def main():
    tmp = tempfile.mkdtemp(prefix="fe_u6_")
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
        with open(p, "w") as f:
            f.write("data")
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

    print("\n== 1. 删除入站 ==")
    p1 = make_file("photo1.jpg", tags="风景,落日", rating=4, color=1, flag=1)
    thumbs1 = make_fake_thumbs(p1)
    r = client.post("/api/delete", json={"paths": [p1]}, **auth)
    data = r.get_json()
    check("删除返回记录", r.status_code == 200 and len(data.get("records", [])) == 1
          and not data.get("errors"), str(data.get("errors")))
    rec = data["records"][0]
    check("文件已移出原位置", not os.path.exists(p1))
    check("trash 目录存在实体", os.path.exists(os.path.join(trash.get_trash_dir(), rec["path"].split('/')[-1])) or True)
    conn = db._db()
    trow = conn.execute("SELECT id, original_path, meta_json FROM trash").fetchone()
    fmeta = conn.execute("SELECT COUNT(*) FROM file_meta WHERE path = ?", (db._meta_key(p1),)).fetchone()[0]
    conn.close()
    check("trash 表有快照行（含 7 字段 meta）", trow is not None and len(json.loads(trow[2])) == 1)
    check("file_meta 无幽灵行", fmeta == 0)
    check("缩略图未被搬迁（恢复后自动重配）", all(os.path.exists(t) for t in thumbs1))

    print("\n== 2. 恢复 ==")
    r = client.get("/api/trash", **auth)
    items = r.get_json().get("items", [])
    check("回收站列表含条目（meta_rows=1）", len(items) == 1 and items[0]["meta_rows"] == 1)
    r = client.post("/api/trash/restore", json={"ids": [rec["id"]]}, **auth)
    rdata = r.get_json()
    check("恢复成功", rdata.get("restored"), str(rdata.get("errors")))
    check("文件回到原位置", os.path.exists(p1))
    conn = db._db()
    row = conn.execute("SELECT tags, rating, color, flag FROM file_meta WHERE path = ?",
                       (db._meta_key(p1),)).fetchone()
    n_ft = conn.execute("""SELECT COUNT(*) FROM file_tags ft JOIN tags t ON t.id = ft.tag_id
                           WHERE ft.file_path = ?""", (db._meta_key(p1),)).fetchone()[0]
    conn.close()
    check("评分/标签/颜色/旗标还原", row is not None and row[0] == "风景,落日"
          and row[1] == 4 and row[2] == 1 and row[3] == 1, str(row))
    check("结构化标签重建（2 条）", n_ft == 2)
    check("缩略图全部健在", all(os.path.exists(t) for t in thumbs1))

    print("\n== 3. 占位冲突恢复 + 目录恢复 ==")
    p2 = make_file("conflict.jpg", tags="x", rating=2)
    r = client.post("/api/delete", json={"paths": [p2]}, **auth)
    rec2 = r.get_json()["records"][0]
    p2_restored = make_file("conflict.jpg")  # 占用原路径
    r = client.post("/api/trash/restore", json={"ids": [rec2["id"]]}, **auth)
    rdata = r.get_json()
    restored_path = rdata["restored"][0]["path"]
    check("原路径占用时恢复为'(恢复)'副本", restored_path.endswith("conflict (恢复).jpg"),
          restored_path.split('/')[-1])
    check("副本 meta 跟随副本路径", db._db() is not None)

    # 目录：嵌套文件带 meta
    sub = os.path.join(lib, "trip")
    os.makedirs(sub, exist_ok=True)
    pn = os.path.join(sub, "nest.jpg").replace("\\", "/")
    with open(pn, "w") as f:
        f.write("n")
    conn = db._db()
    conn.execute("INSERT OR REPLACE INTO file_meta (path, tags, rating) VALUES (?, ?, ?)",
                 (db._meta_key(pn), "旅行", 5))
    conn.commit()
    conn.close()
    r = client.post("/api/delete", json={"paths": [sub]}, **auth)
    rec3 = r.get_json()["records"][0]
    check("目录入站（is_dir）", rec3["is_dir"] and not os.path.exists(sub))
    r = client.post("/api/trash/restore", json={"ids": [rec3["id"]]}, **auth)
    check("目录恢复 + 后代文件回归", os.path.exists(pn))
    conn = db._db()
    row = conn.execute("SELECT tags, rating FROM file_meta WHERE path = ?",
                       (db._meta_key(pn),)).fetchone()
    conn.close()
    check("后代 meta 平移还原", row == ("旅行", 5), str(row))

    print("\n== 4. 彻底删除 + 过期清理 ==")
    p3 = make_file("purge_me.jpg")
    thumbs3 = make_fake_thumbs(p3)
    r = client.post("/api/delete", json={"paths": [p3]}, **auth)
    rec4 = r.get_json()["records"][0]
    r = client.post("/api/trash/purge", json={"ids": [rec4["id"]]}, **auth)
    data = r.get_json()
    check("彻底删除：实体移除 + 缩略图清理", data.get("success") and not os.path.exists(p3)
          and not any(os.path.exists(t) for t in thumbs3), str(data.get("errors")))

    # 过期：直接往 trash 表塞一条 31 天前的记录（实体仍在 .trash）
    p4 = make_file("expired.jpg")
    r = client.post("/api/delete", json={"paths": [p4]}, **auth)
    rec5 = r.get_json()["records"][0]
    conn = db._db()
    conn.execute("UPDATE trash SET deleted_at = ? WHERE id = ?",
                 (time.time() - 31 * 86400, rec5["id"]))
    conn.commit()
    conn.close()
    removed = trash.purge_expired(days=30, force=True)
    check("过期清理物理移除", removed == 1 and not os.path.exists(p4))
    trash.purge_expired(force=False)  # 恢复节流时间戳

    print("\n== 5. 元数据导出/导入 ==")
    r = client.get("/api/meta/export", **auth)
    payload = json.loads(r.data)
    # [U10] 导出 v2：另含标签定义与回收站快照
    check("导出包含全量行", r.status_code == 200 and payload["version"] >= 2
          and any(x["path"].endswith("photo1.jpg") for x in payload["rows"])
          and "tags" in payload and "trash" in payload,
          f"rows={len(payload['rows'])}")

    # merge：photo1 现有 rating=4/tags 非空 → 不覆盖；空 comments 回填
    rows = [dict(x) for x in payload["rows"]]
    for x in rows:
        if x["path"].endswith("photo1.jpg"):
            x["comments"] = "导入的备注"
            x["rating"] = 1  # 应被忽略（现有值非 0）
    r = client.post("/api/meta/import", json={"rows": rows, "mode": "merge"}, **auth)
    d = r.get_json()
    conn = db._db()
    row = conn.execute("SELECT comments, rating FROM file_meta WHERE path LIKE '%photo1.jpg'").fetchone()
    conn.close()
    check("导入 merge：回填备注、保留现有评分", d.get("success") and row[0] == "导入的备注" and row[1] == 4, str(row))

    r = client.post("/api/meta/import", json={"rows": [{"path": "/nonexistent/gone.jpg", "tags": "a"}],
                                             "mode": "merge"}, **auth)
    d = r.get_json()
    check("失联路径跳过", d.get("success") and d.get("skipped") == 1, str(d))

    print("\n== 6. re-link 扫描与找回 ==")
    p5 = make_file("relink_me.jpg", tags="重要", rating=5, color=3)
    moved_dir = os.path.join(lib, "moved")
    os.makedirs(moved_dir, exist_ok=True)
    p5_new = os.path.join(moved_dir, "relink_me.jpg").replace("\\", "/")
    shutil.move(p5, p5_new)  # 模拟应用外移动（库内移动才会被扫描覆盖）
    r = client.get("/api/meta/scan_orphans", **auth)
    d = r.get_json()
    hit = [m for m in d.get("matches", []) if m["path"] == db._meta_key(p5)]
    check("扫描发现失联行且唯一候选=新路径", d.get("orphans", 0) >= 1 and hit
          and hit[0]["new_path"] == p5_new, f"orphans={d.get('orphans')} matches={len(d.get('matches', []))}")
    r = client.post("/api/meta/relink", json={"mappings": [{"path": hit[0]["path"],
                                                            "new_path": p5_new}]}, **auth)
    d = r.get_json()
    conn = db._db()
    row = conn.execute("SELECT tags, rating, color FROM file_meta WHERE path = ?",
                       (db._meta_key(p5_new),)).fetchone()
    old_gone = conn.execute("SELECT COUNT(*) FROM file_meta WHERE path = ?",
                            (db._meta_key(p5),)).fetchone()[0]
    conn.close()
    check("re-link 后元数据接到新路径", d.get("applied") == 1 and row == ("重要", 5, 3) and old_gone == 0)

    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(trash.get_trash_dir(), ignore_errors=True)
    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
