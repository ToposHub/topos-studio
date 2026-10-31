"""[ARCH-1] Flask 应用外壳：app 实例 + SEC-1 鉴权 + PERF-3 统一 JSON 错误处理。

本模块零内部依赖（不 import db/thumbs/bridge），任何导入顺序下
app/APP_TOKEN/FLASK_PORT 均唯一；routes 在此挂载路由，bridge.ensure_runtime
写 FLASK_PORT，main.py 作组合根再导出（兼容旧 ``from file_explorer.main import ...``）。"""
import logging
import os
import secrets
import hmac
import json

from flask import Flask, jsonify, request
from urllib.parse import urlsplit

import const

log = logging.getLogger('werkzeug')
logger = logging.getLogger(__name__)
log.setLevel(logging.WARNING)

app = Flask(__name__, static_folder='static', template_folder='templates')
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
# [BUG-P2-15] 上传体积上限：不设限时超大上传可填满磁盘（超限由 Flask 返回 413）
app.config['MAX_CONTENT_LENGTH'] = const.UPLOAD_MAX_CONTENT_BYTES


# [SEC-1] API 鉴权：绑定 127.0.0.1 不等于安全。浏览器里任意恶意网页无需 CORS 预检
# 即可用"简单请求"直接打本服务的 GET/POST（drive-by），因此所有 /api/* 都要求携带
# 启动时随机生成的 token（HttpOnly Cookie 为主，header/query 兜底供非浏览器调用方），
# 并校验 Host 主机名防 DNS rebinding。Cookie 用 SameSite=Lax：跨站 fetch/子资源请求
# 不携带 cookie，同站（含 <img>/<audio> 与新标签页导航）自动携带，前端零改动。
APP_TOKEN = secrets.token_urlsafe(32)
_ALLOWED_API_HOSTNAMES = {'127.0.0.1', 'localhost', '::1'}
_COOKIE_NAME = 'fx_token'


@app.before_request
def _guard_api():
    if not request.path.startswith('/api/'):
        return None
    hostname = (urlsplit(f'//{request.host or ""}').hostname or '').lower()
    if hostname not in _ALLOWED_API_HOSTNAMES:
        return jsonify({'error': 'forbidden host'}), 403
    # [ARCH-7] 健康检查免 token：响应只含无敏感信息的服务状态布尔值，
    # 供独立模式/嵌入模式的就绪探测使用（Host 校验仍然生效）
    if request.path == '/api/health':
        return None
    supplied = (request.cookies.get(_COOKIE_NAME)
                or request.headers.get('X-Auth-Token')
                or request.args.get('token') or '')
    if not hmac.compare_digest(supplied.encode('utf-8', 'replace'), APP_TOKEN.encode()):
        return jsonify({'error': 'unauthorized'}), 401
    return None


@app.after_request
def _api_no_store(resp):
    """[BUG-P3-36] JSON/204 响应统一 no-store：QWebEngineView 磁盘缓存
    与配置写竞争会让 /api/list 等拿到陈旧响应。已显式带 Cache-Control 的
    媒体端点（thumb/file 的 max-age）不受影响。"""
    if (request.path.startswith('/api/') and request.method == 'GET'
            and (resp.mimetype == 'application/json' or resp.status_code == 204)):
        resp.headers['Cache-Control'] = 'no-store'
    return resp


# [PERF-3] /api/* 未捕获异常统一返回 JSON，而非 werkzeug 的 HTML 错误页
# （前端 res.json() 解析失败会掩盖真实错误）
@app.errorhandler(Exception)
def _api_json_error(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        if request.path.startswith('/api/'):
            return jsonify({'error': e.description or e.name}), e.code
        return e
    if request.path.startswith('/api/'):
        return jsonify({'error': str(e)}), 500
    raise e


ROOT_PATH = os.path.expanduser("~")


def find_available_port(start_port=const.DEFAULT_PORT, max_attempts=const.FIND_PORT_MAX_ATTEMPTS):
    """自动找可用端口，避免端口冲突"""
    import socket
    for attempt in range(max_attempts):
        port = start_port + attempt
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            try:
                result = s.connect_ex(('127.0.0.1', port))
                if result != 0:  # 端口可用
                    return port
            except:
                return port
    # 如果连续端口都不可用，返回随机高位端口
    import random
    return random.randint(49152, 65535)


# 自动选择可用端口
# [ARCH-6] 端口探测延迟到 ensure_runtime()：import 本模块不应做 socket
# 探测、写端口文件等副作用（宿主测试进程 import 一次即背上全部后台资源）
FLASK_PORT = None

_port_config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.port_config.json')
def _save_port_config():
    try:
        with open(_port_config_path, 'w') as f:
            json.dump({'port': FLASK_PORT, 'host': '127.0.0.1'}, f)
    except:
        pass
