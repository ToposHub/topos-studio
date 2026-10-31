"""MCP 视觉网关 —— 把智谱 Vision MCP Server（GLM-4.6V）接入 Agent 工具面。

在 ``mcp_outbound.py``（受控出站客户端骨架）+ ``mcp_stdio_transport.py``（真实
subprocess 传输）之上，提供一个面向 ``ToolFacade`` 的网关：

- 懒启动：首次调用才拉起 ``@z_ai/mcp-server`` 子进程并握手。
- 暴露 4 个高价值视觉工具（schema 来自对真实 server 的 tools/list 实测）：
  ``topos.mcp.analyze_image`` / ``analyze_video`` / ``extract_text_from_screenshot``
  / ``ui_diff_check``。
- 引用解析（红线：**模型永远看不到绝对路径**）：
  ``"latest"`` → 用户最近一次抓帧/粘贴的图片（宿主注册）；
  ``"asset:<asset_id>"`` → 工程素材（宿主解析为路径，只传给 MCP 不回传模型）；
  其它 → 视为显式文件路径（模型在用户明确给出时使用）。
- MCP server 输出只作文本数据返回（红线：不可信数据，上层经 sanitize）。
- 不可用（未装 node / 未配置 key / 子进程失败）→ 优雅降级 ``{"ok": False}``，
  绝不抛异常打断 Agent。

实测要点（2026-08-14，@z_ai/mcp-server）：
- image_source / video_source 需要**本地文件路径**（data URL 会报 file not found）。
- npx 需绝对路径启动，且其目录要前置到子进程 PATH（npx 脚本内部找 node）。
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
from collections import deque
from typing import Any, Callable, TYPE_CHECKING

from src.features.ai_harness.mcp_outbound import (
    McpConnectionProfile,
    McpOutboundClient,
    McpServerRegistry,
)
from src.features.ai_harness.mcp_stdio_transport import (
    SubprocessStdioTransport,
)

if TYPE_CHECKING:
    from src.features.ai_harness.mcp_http_transport import HttpMcpTransport

_LOG = logging.getLogger(__name__)

ZHIPU_VISION_PROFILE_ID = "zhipu-vision-mcp"
ZHIPU_SEARCH_PROFILE_ID = "zhipu-web-search-mcp"
ZHIPU_READER_PROFILE_ID = "zhipu-web-reader-mcp"
_NVM_NPX_CANDIDATES = ("~/.nvm/versions/node",)

# 远程 HTTP MCP 端点（智谱 Coding Plan；认证同智谱 API key）。
ZHIPU_SEARCH_MCP_URL = "https://open.bigmodel.cn/api/mcp/web_search_prime/mcp"
ZHIPU_READER_MCP_URL = "https://open.bigmodel.cn/api/mcp/web_reader/mcp"

# 各 MCP 工具在调用方未给 prompt 时的默认值（实测 @z_ai/mcp-server 的部分工具
# 要求 prompt 非空，如 extract_text_from_screenshot）。
_DEFAULT_PROMPTS = {
    "analyze_image": "请详细描述这张图片的内容。",
    "analyze_video": "请分析这段视频的内容与要点。",
    "extract_text_from_screenshot": "请提取图片中的所有文字。",
    "ui_diff_check": "请对比两张图片的差异。",
}

# server → 它提供的 Agent 工具（用于偏好页展示与按 server 启停门控）。
_MCP_SERVER_TOOLS: dict[str, tuple[str, ...]] = {
    ZHIPU_VISION_PROFILE_ID: (
        "topos.mcp.analyze_image", "topos.mcp.analyze_video",
        "topos.mcp.extract_text", "topos.mcp.ui_diff_check",
    ),
    ZHIPU_SEARCH_PROFILE_ID: ("topos.mcp.web_search",),
    ZHIPU_READER_PROFILE_ID: ("topos.mcp.web_read",),
}

# server 展示元信息（偏好页「MCP 服务」分区用）。
_MCP_SERVER_META: dict[str, dict[str, str]] = {
    ZHIPU_VISION_PROFILE_ID: {
        "name": "智谱 Vision MCP（GLM-4.6V 视觉）",
        "server": "@z_ai/mcp-server",
        "author": "智谱 AI（docs.bigmodel.cn）",
        "transport": "本地 stdio（npx，需 Node.js v18+）",
    },
    ZHIPU_SEARCH_PROFILE_ID: {
        "name": "智谱联网搜索 MCP",
        "server": "web_search_prime",
        "author": "智谱 AI（docs.bigmodel.cn）",
        "transport": "远程 HTTP",
    },
    ZHIPU_READER_PROFILE_ID: {
        "name": "智谱网页读取 MCP",
        "server": "webReader",
        "author": "智谱 AI（docs.bigmodel.cn）",
        "transport": "远程 HTTP",
    },
}


def _server_of_tool(tool_name: str) -> str:
    """Agent 工具名 → 所属 server 的 profile_id。"""
    for pid, tools in _MCP_SERVER_TOOLS.items():
        if tool_name in tools:
            return pid
    return ""


def default_zhipu_key_provider() -> str | None:
    """默认 key 解析：从用户配置里找智谱/bigmodel profile 的 API key。

    GLM（openai → bigmodel endpoint）的 key 即智谱账号 key（Z_AI_API_KEY）。
    解析失败返回 None（网关随之优雅降级）。
    """
    try:
        from src.features.edit.ai_editing.credential_store import HybridCredentialStore
        from src.features.edit.ai_editing.provider_profiles import ProfileRegistry

        reg = ProfileRegistry()
        if not reg.load_from_user_file():
            return None
        store = HybridCredentialStore(prefer="file")
        for p in reg.list():
            endpoint = (getattr(p, "endpoint", "") or "").lower()
            model_id = (getattr(p, "model_id", "") or "").lower()
            if "bigmodel" in endpoint or "glm" in model_id:
                ref = getattr(p, "credential_ref", "")
                if ref:
                    key = store.get_secret(ref)
                    if key:
                        return key
        return None
    except Exception:  # noqa: BLE001 — key 解析失败 → 网关不可用
        return None


# ---------------------------------------------------------------------- #
# 模块单例（mixin 注入 ToolFacade；controller 注册会话图片；退出时 shutdown）
# ---------------------------------------------------------------------- #

_GATEWAY: "McpVisionGateway | None" = None
_GATEWAY_LOCK = threading.Lock()


def get_mcp_vision_gateway() -> "McpVisionGateway":
    """返回全局 MCP 视觉网关单例（懒创建；不可用时自身优雅降级）。"""
    global _GATEWAY
    if _GATEWAY is None:
        with _GATEWAY_LOCK:
            if _GATEWAY is None:
                _GATEWAY = McpVisionGateway(
                    api_key_provider=default_zhipu_key_provider,
                )
    return _GATEWAY


def shutdown_mcp_vision_gateway() -> None:
    """App 退出时关闭 MCP 子进程。"""
    global _GATEWAY
    with _GATEWAY_LOCK:
        if _GATEWAY is not None:
            _GATEWAY.shutdown()
            _GATEWAY = None


def resolve_npx_path() -> str:
    """找 npx 的绝对路径（PATH → nvm 目录扫描）。找不到返回空串。"""
    found = shutil.which("npx")
    if found:
        return found
    import glob
    for base in _NVM_NPX_CANDIDATES:
        hits = sorted(glob.glob(os.path.join(os.path.expanduser(base), "*", "bin", "npx")))
        if hits:
            return hits[-1]  # 最新版本
    return ""


def build_zhipu_search_profile() -> McpConnectionProfile:
    """智谱联网搜索 MCP（远程 HTTP；web_search_prime）。"""
    return McpConnectionProfile(
        profile_id=ZHIPU_SEARCH_PROFILE_ID,
        server_name="web_search_prime",
        author="智谱 AI（bigmodel.cn）",
        version="0.0.1",
        homepage_domain="docs.bigmodel.cn",
        transport="cloud_api",
        base_url=ZHIPU_SEARCH_MCP_URL,
        allowed_tools=("web_search_prime",),
        data_scope="read_only",
        requires_approval_for_writes=True,
        enabled_by_default=True,
    )


def build_zhipu_reader_profile() -> McpConnectionProfile:
    """智谱网页读取 MCP（远程 HTTP；webReader）。"""
    return McpConnectionProfile(
        profile_id=ZHIPU_READER_PROFILE_ID,
        server_name="webReader",
        author="智谱 AI（bigmodel.cn）",
        version="0.0.1",
        homepage_domain="docs.bigmodel.cn",
        transport="cloud_api",
        base_url=ZHIPU_READER_MCP_URL,
        allowed_tools=("webReader",),
        data_scope="read_only",
        requires_approval_for_writes=True,
        enabled_by_default=True,
    )


def build_zhipu_vision_profile(npx_path: str) -> McpConnectionProfile:
    """智谱 Vision MCP server 的默认连接 profile（command 不含 secret）。"""
    return McpConnectionProfile(
        profile_id=ZHIPU_VISION_PROFILE_ID,
        server_name="@z_ai/mcp-server",
        author="智谱 AI（bigmodel.cn）",
        version="latest",
        homepage_domain="docs.bigmodel.cn",
        transport="local_stdio",
        command=(npx_path, "-y", "@z_ai/mcp-server"),
        # 全部 8 个工具都是只读分析；默认放行（read_only scope + 只读工具集）。
        allowed_tools=(
            "analyze_image", "analyze_video", "extract_text_from_screenshot",
            "ui_diff_check", "analyze_data_visualization",
            "understand_technical_diagram", "diagnose_error_screenshot",
            "ui_to_artifact",
        ),
        data_scope="read_only",
        requires_approval_for_writes=True,
        enabled_by_default=True,
    )


class McpVisionGateway:
    """把智谱 Vision MCP 工具桥接为 Agent 工具（注入 ToolFacade）。

    Args:
        api_key_provider: 返回 ``Z_AI_API_KEY``（宿主从 CredentialStore 解析）。
        npx_path: npx 绝对路径；空则自动解析；解析不到则网关不可用（优雅降级）。
        transport_timeout: 传输超时（视觉分析较慢，默认 240s）。
    """

    def __init__(
        self,
        *,
        api_key_provider: Callable[[], str | None],
        npx_path: str = "",
        request_timeout: float = 240.0,
        startup_timeout: float = 120.0,
        transport_factory: Any | None = None,
    ) -> None:
        self._api_key_provider = api_key_provider
        self._npx_path = npx_path or resolve_npx_path()
        self._request_timeout = request_timeout
        self._startup_timeout = startup_timeout
        # 测试注入点：覆盖默认的 npx+key transport 工厂（fake transport 用）。
        self._injected_transport_factory = transport_factory
        self._lock = threading.Lock()
        self._client: McpOutboundClient | None = None
        self._http_clients: dict[str, McpOutboundClient] = {}  # 远程 HTTP MCP（search/reader）
        self._failed_reason: str = ""     # 一旦启动失败，记住原因不再反复尝试
        self._closed = False              # shutdown() 后拒绝再建 client（AUD-13850）
        # AUD-13851：GUI 线程注册图片与 agent 线程 resolve_image_ref('latest')
        # 并发——改用 deque(maxlen=8)，append+trim 单操作原子；读取端取局部快照。
        self._session_images: "deque[str]" = deque(maxlen=8)   # 最近注册的本会话图片路径（新在后）
        # 用户在偏好页按 server 禁用的 profile_id 集合（默认全部启用）。
        # AUD-13851：读写经 _state_lock（与慢速的 _ensure_http_client/_shutdown
        # 用的 _lock 分离，避免互不相关的阻塞）。
        self._server_disabled: set[str] = set()
        self._state_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # 可用性 / 生命周期
    # ------------------------------------------------------------------ #

    @property
    def available(self) -> bool:
        """视觉（stdio）工具可用性：需要 npx + key。"""
        return bool(self._npx_path) and not self._failed_reason

    @property
    def web_available(self) -> bool:
        """联网搜索/网页读取（HTTP）工具可用性：只需智谱 key（无需 npx/node）。"""
        try:
            return bool(self._api_key_provider())
        except Exception:  # noqa: BLE001
            return False

    @property
    def failure_reason(self) -> str:
        return self._failed_reason

    # ------------------------------------------------------------------ #
    # 管理 API（偏好页「MCP 服务」分区）
    # ------------------------------------------------------------------ #

    def server_statuses(self) -> list[dict[str, Any]]:
        """列出全部内置 MCP server 的状态（供偏好页展示）。

        每项：profile_id / name / server / author / transport / available /
        reason / enabled / tools。available = 环境就绪（npx/key）；
        enabled = 用户是否启用（两者都满足工具才暴露）。
        """
        has_key = bool(self._resolve_api_key())
        # AUD-13851：锁内取启用状态快照，循环中不再直接读共享 set。
        with self._state_lock:
            disabled_snapshot = set(self._server_disabled)
        statuses: list[dict[str, Any]] = []
        for pid, meta in _MCP_SERVER_META.items():
            if pid == ZHIPU_VISION_PROFILE_ID:
                available = bool(self._npx_path) and has_key and not self._failed_reason
                if self._failed_reason:
                    reason = self._failed_reason
                elif not self._npx_path:
                    reason = "未找到 npx（需要 Node.js v18+，可经 nvm 安装）"
                elif not has_key:
                    reason = "未配置智谱 API key"
                else:
                    reason = ""
            else:
                available = has_key
                reason = "" if has_key else "未配置智谱 API key"
            statuses.append({
                "profile_id": pid,
                **meta,
                "available": available,
                "reason": reason,
                "enabled": pid not in disabled_snapshot,
                "tools": list(_MCP_SERVER_TOOLS.get(pid, ())),
            })
        return statuses

    def set_server_enabled(self, profile_id: str, enabled: bool) -> bool:
        """启用/禁用一个 MCP server（偏好页勾选）。未知 id 返回 False。"""
        if profile_id not in _MCP_SERVER_META:
            return False
        with self._state_lock:
            if enabled:
                self._server_disabled.discard(profile_id)
            else:
                self._server_disabled.add(profile_id)
        return True

    def is_server_enabled(self, profile_id: str) -> bool:
        with self._state_lock:
            return profile_id not in self._server_disabled

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:  # noqa: BLE001
                    pass
                self._client = None
            for c in self._http_clients.values():
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass
            self._http_clients.clear()

    def _resolve_api_key(self) -> str:
        try:
            return self._api_key_provider() or ""
        except Exception:  # noqa: BLE001
            return ""

    def _ensure_http_client(self, profile_id: str) -> McpOutboundClient | None:
        """懒建远程 HTTP MCP 客户端（search/reader 各一个）。失败返回 None。"""
        with self._lock:
            existing = self._http_clients.get(profile_id)
            if existing is not None:
                return existing
            api_key = self._resolve_api_key()
            if not api_key:
                return None
            profile = (
                build_zhipu_search_profile() if profile_id == ZHIPU_SEARCH_PROFILE_ID
                else build_zhipu_reader_profile()
            )

            def _http_factory(p: McpConnectionProfile) -> HttpMcpTransport:
                from src.features.ai_harness.mcp_http_transport import HttpMcpTransport
                return HttpMcpTransport(
                    p.base_url,
                    api_key=api_key,
                    request_timeout=self._request_timeout,
                    startup_timeout=self._startup_timeout,
                )

            registry = McpServerRegistry()
            registry.register(profile)
            client = McpOutboundClient(registry, transport_factory=_http_factory)
            self._http_clients[profile_id] = client
            return client

    def _ensure_client(self) -> McpOutboundClient | None:
        """懒启动 MCP 子进程 + 客户端；失败记住原因并优雅降级。

        全程持 ``self._lock``（与 ``_ensure_http_client`` / ``shutdown`` 互斥）：
        并发首调只会构建一个 client（否则输家 client 的 npx 子进程会成为
        无人回收的孤儿），且 shutdown 之后不会再写回新 client。
        """
        with self._lock:
            if self._failed_reason or self._closed:
                return None
            if self._client is not None:
                return self._client
            if not self._npx_path:
                self._failed_reason = "未找到 npx（需要 Node.js v18+，可经 nvm 安装）"
                return None
            api_key = self._resolve_api_key()
            if not api_key:
                self._failed_reason = "未配置智谱 API key（Z_AI_API_KEY）"
                return None

            def _factory(profile: McpConnectionProfile) -> SubprocessStdioTransport:
                if self._injected_transport_factory is not None:
                    return self._injected_transport_factory(profile)  # type: ignore[no-any-return]
                return SubprocessStdioTransport(
                    profile.command,
                    env={"Z_AI_API_KEY": api_key, "Z_AI_MODE": "ZHIPU"},
                    startup_timeout=self._startup_timeout,
                    request_timeout=self._request_timeout,
                )

            registry = McpServerRegistry()
            registry.register(build_zhipu_vision_profile(self._npx_path))
            client = McpOutboundClient(registry, transport_factory=_factory)
            # enabled_by_default=True 已在 register 时入会话启用；这里确保兜底。
            registry.enable_for_session(ZHIPU_VISION_PROFILE_ID)
            # 不预热：SubprocessStdioTransport.send_request 懒启动 + 握手，
            # 首次 call_tool 才拉起子进程；失败由 call_tool 的审计链归一为 ok=False。
            self._client = client
            return client

    # ------------------------------------------------------------------ #
    # 会话图片注册（宿主在抓帧/粘贴时调用）
    # ------------------------------------------------------------------ #

    def register_session_image(self, path: str) -> None:
        """注册一张本会话图片（抓帧/粘贴由宿主存盘后调用）；'latest' 解析用它。"""
        if path and os.path.isfile(path):
            # AUD-13851：deque(maxlen=8) 使 append+淘汰成为单次原子操作，
            # 不再存在 append 与 del 切片交错丢条目的窗口。
            self._session_images.append(os.path.abspath(path))

    def save_and_register_image(self, data: bytes, mime: str = "image/jpeg") -> str:
        """把图片字节存到临时文件并注册（返回路径，仅供宿主记录）。"""
        ext = ".png" if "png" in mime else ".jpg"
        fd, path = tempfile.mkstemp(prefix="topos_mcp_img_", suffix=ext)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        self.register_session_image(path)
        return path

    def save_and_register_png(self, png_bytes: bytes) -> str:
        """便捷方法：PNG 字节。"""
        return self.save_and_register_image(png_bytes, "image/png")

    # ------------------------------------------------------------------ #
    # 引用解析（红线：解析在宿主侧完成；路径只进 MCP，不回传模型）
    # ------------------------------------------------------------------ #

    def resolve_image_ref(self, ref: str, asset_resolver: Callable[[str], str | None] | None = None) -> str:
        """image_ref → 绝对路径。'latest' / 'asset:<id>' / 显式路径。"""
        ref = (ref or "").strip()
        if not ref or ref == "latest":
            # AUD-13851：局部快照读取，避免并发 append 时索引漂移。
            images = tuple(self._session_images)
            return images[-1] if images else ""
        if ref.startswith("asset:") and asset_resolver is not None:
            resolved = asset_resolver(ref[len("asset:"):])
            return resolved or ""
        return os.path.expanduser(ref)

    # ------------------------------------------------------------------ #
    # Agent 工具定义 + 调用
    # ------------------------------------------------------------------ #

    def tool_definitions(self) -> list:
        """面向 Agent 的工具定义（READ 权限）。

        视觉（stdio）工具需要 npx + key；联网搜索/网页读取（HTTP）工具只需智谱
        key。各自独立判断可用性，互不影响。
        """
        defs: list = []
        if not self.available:
            defs = []  # 视觉工具不暴露
        else:
            defs = self._vision_tool_definitions()
        if self.web_available:
            defs.extend(self._web_tool_definitions())
        # 用户在偏好页禁用的 server：其工具不暴露。
        return [d for d in defs if self.is_server_enabled(_server_of_tool(d.name))]

    def _web_tool_definitions(self) -> list:
        from src.features.edit.ai_editing.tool_facade import ToolDefinition, ToolPermission
        return [
            ToolDefinition(
                name="topos.mcp.web_search",
                permission=ToolPermission.READ,
                description=(
                    "联网搜索实时信息（经智谱 Search MCP）。适合查最新资讯、"
                    "档期、价格、行业动态等模型训练数据之外的内容。"
                    "返回标题/链接/摘要列表。"
                ),
                input_schema={
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string", "description": "搜索词（建议 ≤70 字）"},
                        "domain_filter": {"type": "string", "description": "限定域名（如 www.example.com，可选）"},
                        "recency_filter": {"type": "string", "description": "时效过滤（可选，按服务端支持）"},
                    },
                },
                output_description="搜索结果列表（标题/链接/摘要）",
            ),
            ToolDefinition(
                name="topos.mcp.web_read",
                permission=ToolPermission.READ,
                description=(
                    "读取一个网页的正文内容（经智谱 Reader MCP），返回标题、"
                    "正文（markdown）、元数据与链接。适合深入阅读搜索结果或"
                    "用户给出的网址。"
                ),
                input_schema={
                    "type": "object",
                    "required": ["url"],
                    "properties": {
                        "url": {"type": "string", "description": "要读取的网页 URL（http/https）"},
                        "return_format": {"type": "string", "enum": ["markdown", "text"],
                                          "description": "返回格式（默认 markdown）"},
                    },
                },
                output_description="网页正文（markdown/text）",
            ),
        ]

    def _vision_tool_definitions(self) -> list:
        from src.features.edit.ai_editing.tool_facade import ToolDefinition, ToolPermission
        return [
            ToolDefinition(
                name="topos.mcp.analyze_image",
                permission=ToolPermission.READ,
                description=(
                    "用 GLM-4.6V 视觉模型分析一张图片（经智谱 Vision MCP）。"
                    "image_ref 填 'latest' 表示用户最近抓取/粘贴的画面，"
                    "'asset:<素材ID>' 表示工程素材，或用户明确给出的图片路径。"
                ),
                input_schema={
                    "type": "object",
                    "required": ["image_ref"],
                    "properties": {
                        "image_ref": {"type": "string"},
                        "prompt": {"type": "string", "description": "分析关注点（可选）"},
                    },
                },
                output_description="视觉分析文本（结构化）",
            ),
            ToolDefinition(
                name="topos.mcp.analyze_video",
                permission=ToolPermission.READ,
                description=(
                    "用 GLM-4.6V 视觉模型分析视频内容（抓关键帧+要点）。"
                    "video_ref 填 'asset:<素材ID>'（工程视频素材）或用户给出的视频路径。"
                    "本地视频最大 8MB。"
                ),
                input_schema={
                    "type": "object",
                    "required": ["video_ref"],
                    "properties": {
                        "video_ref": {"type": "string"},
                        "prompt": {"type": "string", "description": "分析关注点（可选）"},
                    },
                },
                output_description="视频分析文本",
            ),
            ToolDefinition(
                name="topos.mcp.extract_text",
                permission=ToolPermission.READ,
                description=(
                    "OCR：从截图/画面中提取文字（代码、终端输出、文档均适用）。"
                    "image_ref 同 analyze_image。"
                ),
                input_schema={
                    "type": "object",
                    "required": ["image_ref"],
                    "properties": {
                        "image_ref": {"type": "string"},
                        "prompt": {"type": "string", "description": "补充说明（可选）"},
                    },
                },
                output_description="识别出的文字",
            ),
            ToolDefinition(
                name="topos.mcp.ui_diff_check",
                permission=ToolPermission.READ,
                description=(
                    "对比两张画面/截图的视觉差异（如调色前后两帧）。"
                    "expected_image_ref / actual_image_ref 同 analyze_image 的 image_ref。"
                ),
                input_schema={
                    "type": "object",
                    "required": ["expected_image_ref", "actual_image_ref"],
                    "properties": {
                        "expected_image_ref": {"type": "string", "description": "基准（如调色前）"},
                        "actual_image_ref": {"type": "string", "description": "对比（如调色后）"},
                        "prompt": {"type": "string", "description": "关注差异（可选）"},
                    },
                },
                output_description="差异分析文本",
            ),
        ]

    def invoke(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        asset_resolver: Callable[[str], str | None] | None = None,
    ) -> dict[str, Any]:
        """路由 topos.mcp.* 调用到 MCP server。永不抛异常（优雅降级）。"""
        # 用户在偏好页禁用了所属 server → 直接降级（不发起调用）。
        server_pid = _server_of_tool(tool_name)
        if server_pid and not self.is_server_enabled(server_pid):
            return {"ok": False, "error": "该 MCP 服务已在偏好设置中禁用"}

        # ---- HTTP 工具（search/reader）：不需要 npx，独立于视觉可用性 ---- #
        if tool_name in ("topos.mcp.web_search", "topos.mcp.web_read"):
            return self._invoke_web_tool(tool_name, args)

        if not self.available:
            return {"ok": False, "error": f"MCP 视觉不可用：{self._failed_reason or 'npx 未安装'}"}

        client = self._ensure_client()
        if client is None:
            return {"ok": False, "error": f"MCP 视觉不可用：{self._failed_reason}"}

        prompt = str(args.get("prompt", "") or "").strip()
        mcp_tool = ""
        mcp_args: dict[str, Any] = {}
        try:
            if tool_name == "topos.mcp.analyze_image":
                mcp_tool = "analyze_image"
                path = self.resolve_image_ref(str(args.get("image_ref", "")), asset_resolver)
                if not path or not os.path.isfile(path):
                    return {"ok": False, "error": f"图片不可用：{args.get('image_ref')!r}（可用 'latest' 或 asset:<ID>）"}
                mcp_args = {"image_source": path, "prompt": prompt}
            elif tool_name == "topos.mcp.analyze_video":
                mcp_tool = "analyze_video"
                path = self.resolve_image_ref(str(args.get("video_ref", "")), asset_resolver)
                if not path or not os.path.isfile(path):
                    return {"ok": False, "error": f"视频不可用：{args.get('video_ref')!r}（可用 asset:<ID>）"}
                mcp_args = {"video_source": path, "prompt": prompt}
            elif tool_name == "topos.mcp.extract_text":
                mcp_tool = "extract_text_from_screenshot"
                path = self.resolve_image_ref(str(args.get("image_ref", "")), asset_resolver)
                if not path or not os.path.isfile(path):
                    return {"ok": False, "error": f"图片不可用：{args.get('image_ref')!r}"}
                mcp_args = {"image_source": path, "prompt": prompt}
            elif tool_name == "topos.mcp.ui_diff_check":
                mcp_tool = "ui_diff_check"
                base = self.resolve_image_ref(str(args.get("expected_image_ref", "")), asset_resolver)
                actual = self.resolve_image_ref(str(args.get("actual_image_ref", "")), asset_resolver)
                for p, label in ((base, "expected_image_ref"), (actual, "actual_image_ref")):
                    if not p or not os.path.isfile(p):
                        return {"ok": False, "error": f"图片不可用：{label}"}
                mcp_args = {"expected_image_source": base, "actual_image_source": actual, "prompt": prompt}
            else:
                return {"ok": False, "error": f"unknown MCP tool: {tool_name}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"参数解析失败：{exc}"}

        # 部分工具要求 prompt 非空 → 未给时用工具默认值。
        if not prompt:
            prompt = _DEFAULT_PROMPTS.get(mcp_tool, "请分析内容。")
        mcp_args["prompt"] = prompt

        result = client.call_tool(ZHIPU_VISION_PROFILE_ID, mcp_tool, mcp_args)
        if not result.get("ok"):
            return {"ok": False, "error": str(result.get("reason") or result.get("decision") or "MCP 调用失败")}
        # MCP 返回只取 text 内容（不可信数据；上层 ToolFacade 会再 sanitize）。
        texts: list[str] = []
        payload = result.get("result") or {}
        content = (payload.get("result") or {}).get("content") or []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text", "")
                if t:
                    texts.append(str(t))
        if not texts:
            return {"ok": False, "error": "MCP 返回无文本内容"}
        return {"ok": True, "analysis": "\n".join(texts)}

    def _invoke_web_tool(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        """联网搜索 / 网页读取（远程 HTTP MCP）。永不抛异常。"""
        if tool_name == "topos.mcp.web_search":
            profile_id, mcp_tool = ZHIPU_SEARCH_PROFILE_ID, "web_search_prime"
            query = str(args.get("query", "") or "").strip()
            if not query:
                return {"ok": False, "error": "query 必填"}
            mcp_args: dict[str, Any] = {"search_query": query[:200]}
            if args.get("domain_filter"):
                mcp_args["search_domain_filter"] = str(args["domain_filter"])
            if args.get("recency_filter"):
                mcp_args["search_recency_filter"] = str(args["recency_filter"])
        elif tool_name == "topos.mcp.web_read":
            profile_id, mcp_tool = ZHIPU_READER_PROFILE_ID, "webReader"
            url = str(args.get("url", "") or "").strip()
            if not url.startswith(("http://", "https://")):
                return {"ok": False, "error": "url 必须是 http/https 网址"}
            mcp_args = {"url": url}
            if args.get("return_format") in ("markdown", "text"):
                mcp_args["return_format"] = str(args["return_format"])
        else:
            return {"ok": False, "error": f"unknown MCP web tool: {tool_name}"}

        client = self._ensure_http_client(profile_id)
        if client is None:
            return {"ok": False, "error": "联网 MCP 不可用（未配置智谱 API key）"}
        result = client.call_tool(profile_id, mcp_tool, mcp_args)
        if not result.get("ok"):
            return {"ok": False, "error": str(result.get("reason") or result.get("decision") or "MCP 调用失败")}
        texts: list[str] = []
        payload = result.get("result") or {}
        content = (payload.get("result") or {}).get("content") or []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text", "")
                if t:
                    texts.append(str(t))
        if not texts:
            return {"ok": False, "error": "MCP 返回无文本内容"}
        return {"ok": True, "analysis": "\n".join(texts)}
