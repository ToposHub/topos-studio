"""[V4/GAP-21·第一步] AI 自动打标：把图片送宿主 topos-vault 语义服务，
返回标签以 `ai:` 前缀写入标签体系（独立命名空间，不污染人工标签）。

- 零新模型依赖：vault 是宿主已有服务（ai_library_browser 插件对接的
  :8288 语义检索服务），config `ai_vault_url` 配置后启用；缺省关闭。
- 接口约定（topos-vault）：POST {url}/tags  body {path} → {tags: [...]}。
  服务未就绪/超时一律静默跳过，不影响扫描主流程。
- 人脸识别不在本期（第二步本地模型，远期）。
"""
import logging

import const
from db import _db, _meta_key, load_config

logger = logging.getLogger(__name__)

TAG_PREFIX = 'ai:'


def vault_url():
    return str(load_config().get('ai_vault_url') or '').strip().rstrip('/')


def fetch_ai_tags(path, timeout=6):
    """vault → 标签列表；未配置/失败返回 None（调用方跳过）。"""
    url = vault_url()
    if not url:
        return None
    try:
        import json
        import urllib.request
        req = urllib.request.Request(
            f'{url}/tags',
            data=json.dumps({'path': path}).encode('utf-8'),
            headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8', 'ignore'))
        tags = data.get('tags') or []
        out = []
        for t in tags:
            # [V5 复查] 去前缀必须整串匹配：lstrip('ai:') 按字符集剥离会把
            # 'animal' 砍成 'nimal'、'iso' 砍成空串
            t = str(t).strip()
            if t.startswith(TAG_PREFIX):
                t = t[len(TAG_PREFIX):].strip()
            if t:
                out.append(f'{TAG_PREFIX}{t}')
        return out[:12]
    except Exception as e:
        logger.debug(f"[AI] vault 打标失败 {path}: {e}")
        return None


def apply_ai_tags(path, ai_tags):
    """ai:* 标签写入 tags/file_tags（幂等，不触碰人工标签）。"""
    if not ai_tags:
        return False
    key = _meta_key(path)
    conn = _db()
    try:
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO file_meta (path, tags, comments, rating) VALUES (?, '', '', 0)",
                  (key,))
        for t in ai_tags:
            c.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (t,))
            c.execute("""INSERT OR IGNORE INTO file_tags (file_path, tag_id)
                         SELECT ?, id FROM tags WHERE name = ?""", (key, t))
        # 重算 legacy 串（保留人工标签）
        names = [r[0] for r in c.execute(
            """SELECT t.name FROM file_tags ft JOIN tags t ON t.id = ft.tag_id
               WHERE ft.file_path = ? ORDER BY t.name""", (key,)).fetchall()]
        c.execute("UPDATE file_meta SET tags = ? WHERE path = ?", (",".join(names), key))
        conn.commit()
        return True
    finally:
        conn.close()


def tag_file(path):
    """单文件 AI 打标入口（扫描器/手动触发）。

    返回写入的标签数；0=无标签；-1=vault 不可达/失败（扫描器据此熔断，
    不对剩余文件逐个空等 6s 超时）。"""
    if not vault_url():
        return 0
    tags = fetch_ai_tags(path)
    if tags is None:
        return -1
    if not tags:
        return 0
    apply_ai_tags(path, tags)
    return len(tags)
