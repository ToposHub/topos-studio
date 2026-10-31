"""AI 素材库浏览器插件（file_explorer 插件）。

在 file_explorer dock 面板的侧边栏注入一个"AI 素材库"搜索框，
用户输入关键词（如"微笑"）后，调用 vault 查询服务（进程 C），
结果复用 file_explorer 现有的瀑布流 + 缩略图渲染。

架构：
    本插件 ──HTTP──► topos-vault serve（进程 C，端口 8288）
                 └─► 只读 vault.db

    本插件**不直接读 vault.db**——它只做 HTTP 转发，避免 file_explorer
    的 Flask 进程和 vault 打标进程争抢 DB。

vault 查询服务的地址可通过环境变量 TOPOS_VAULT_URL 覆盖（默认 http://127.0.0.1:8288）。
"""

from __future__ import annotations

import os

from flask import Blueprint, jsonify, request

# vault 查询服务地址（file_explorer 插件转发请求到这里）
VAULT_URL = os.environ.get("TOPOS_VAULT_URL", "http://127.0.0.1:8288").rstrip("/")

# 插件元数据（plugin_system.py 会读取）
NAME = "AI 素材库"
VERSION = "0.1.0"
AUTHOR = "Topos Color"
DESCRIPTION = "在 vault 中语义搜索视频/音乐/图片素材"


def setup(context):
    """插件注册入口（由 PluginManager.load_plugins 调用）。"""
    # 1. 注入侧边栏搜索框 HTML（sidebar_bottom 槽位）
    context.register_ui_component("sidebar_bottom", _SIDEBAR_HTML)

    # 2. 注册前端 JS
    context.register_js("/static_plugin/ai_library_browser/main.js")


# ---------------------------------------------------------------------- #
# Blueprint：转发搜索请求到 vault 查询服务
# ---------------------------------------------------------------------- #

bp = Blueprint("ai_library_browser", __name__)


@bp.route("/search", methods=["GET"])
def search():
    """转发搜索请求到 vault 查询服务。

    前端调 /api/plugin/ai_library_browser/search?q=微笑&type=all
    本端点转发到 {VAULT_URL}/api/search?q=微笑&type=all
    返回 vault 的原始 JSON（{files: [...]} 格式，对齐 file_explorer）。

    用 Blueprint 转发而非前端直连 vault，是为了：
      1. 避免跨域（file_explorer 在 8188，vault 在 8288）
      2. 前端只需知道一个 base URL（file_explorer 自己的）
      3. vault 不可用时这里能返回友好的错误，而不是前端报 CORS 错
    """
    import urllib.request
    import urllib.error

    q = request.args.get("q", "").strip()
    media_type = request.args.get("type", "all").strip()
    limit = request.args.get("limit", "100").strip()
    # [R1-14] limit 校验：非法值回退默认，越界钳制，防止直通 vault URL
    try:
        limit = max(1, min(int(limit), 500))
    except ValueError:
        limit = 100

    if not q:
        return jsonify({"files": [], "query": "", "total": 0, "error": None})

    url = f"{VAULT_URL}/api/search?q={urllib.parse.quote(q)}&type={media_type}&limit={limit}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            import json
            data = json.loads(resp.read().decode("utf-8"))
            return jsonify(data)
    except urllib.error.URLError as e:
        # vault 查询服务没启动
        return jsonify({
            "files": [],
            "query": q,
            "total": 0,
            "error": f"vault 查询服务未启动（{VAULT_URL}）。请运行：topos-vault serve",
            "error_detail": str(e.reason) if hasattr(e, "reason") else str(e),
        }), 200  # 返回 200 让前端能解析 JSON 并显示提示
    except Exception as e:
        return jsonify({
            "files": [],
            "query": q,
            "total": 0,
            "error": f"查询失败：{e}",
        }), 200


@bp.route("/status", methods=["GET"])
def status():
    """检查 vault 查询服务是否在线。"""
    import urllib.request
    try:
        req = urllib.request.Request(f"{VAULT_URL}/api/health", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            import json
            return jsonify({"online": True, "vault_url": VAULT_URL, **json.loads(resp.read())})
    except Exception:
        return jsonify({"online": False, "vault_url": VAULT_URL})


@bp.route("/stats", methods=["GET"])
def stats():
    """转发库统计。"""
    import urllib.request
    try:
        req = urllib.request.Request(f"{VAULT_URL}/api/stats", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            import json
            return jsonify(json.loads(resp.read()))
    except Exception as e:
        return jsonify({"error": str(e), "vault_url": VAULT_URL}), 200


# ---------------------------------------------------------------------- #
# 侧边栏 HTML（注入到 sidebar_bottom 槽位）
# ---------------------------------------------------------------------- #

_SIDEBAR_HTML = """
<!-- AI 素材库搜索框 -->
<div class="ai-library-panel" style="padding: 10px; border-top: 1px solid var(--border-color, #333); margin-top: 8px;">
    <div style="font-size: 11px; font-weight: 600; color: var(--text-muted, #888); margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.5px;">
        🤖 AI 素材库
    </div>
    <div style="position: relative; margin-bottom: 6px;">
        <input type="text" id="aiSearchInput"
               placeholder="搜索：微笑、夜景、happy..."
               autocomplete="off"
               style="width: 100%; padding: 6px 28px 6px 8px; font-size: 13px;
                      background: var(--input-bg, #2a2a2a); color: var(--text-color, #eee);
                      border: 1px solid var(--border-color, #444); border-radius: 4px;">
        <span id="aiSearchClear" onclick="document.getElementById('aiSearchInput').value=''; window.aiLibrarySearch('');"
              style="position: absolute; right: 6px; top: 50%; transform: translateY(-50%);
                    cursor: pointer; color: var(--text-muted, #888); display: none; font-size: 14px;">×</span>
    </div>
    <div style="display: flex; gap: 4px; margin-bottom: 6px;">
        <button class="ai-filter-btn active" data-type="all" onclick="window.aiLibraryFilter('all', this)"
                style="flex:1; padding: 3px 6px; font-size: 11px; border: 1px solid var(--border-color, #444);
                       background: var(--btn-active-bg, #0d6efd); color: #fff; border-radius: 3px; cursor: pointer;">全部</button>
        <button class="ai-filter-btn" data-type="video" onclick="window.aiLibraryFilter('video', this)"
                style="flex:1; padding: 3px 6px; font-size: 11px; border: 1px solid var(--border-color, #444);
                       background: var(--btn-bg, #2a2a2a); color: var(--text-color, #eee); border-radius: 3px; cursor: pointer;">视频</button>
        <button class="ai-filter-btn" data-type="image" onclick="window.aiLibraryFilter('image', this)"
                style="flex:1; padding: 3px 6px; font-size: 11px; border: 1px solid var(--border-color, #444);
                       background: var(--btn-bg, #2a2a2a); color: var(--text-color, #eee); border-radius: 3px; cursor: pointer;">图片</button>
        <button class="ai-filter-btn" data-type="audio" onclick="window.aiLibraryFilter('audio', this)"
                style="flex:1; padding: 3px 6px; font-size: 11px; border: 1px solid var(--border-color, #444);
                       background: var(--btn-bg, #2a2a2a); color: var(--text-color, #eee); border-radius: 3px; cursor: pointer;">音乐</button>
    </div>
    <div id="aiSearchStatus" style="font-size: 11px; color: var(--text-muted, #888); min-height: 16px;"></div>
</div>
"""
