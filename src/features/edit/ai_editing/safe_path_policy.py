"""TAEP Safe Path Policy（M0.4 安全边界）。

集中所有工程 payload / audit / 导出路径的"授权根内"校验，取代散落各处的
``Path`` 拼接与每处一写的正则（审查 M0.4）。

核心问题：

- 旧 ``AuditStore`` 只有每处一写的 ``[A-Za-z0-9._-]`` 正则 + sha256 兜底；其它
  路径（payload、export、replay）各自拼接 ``Path``，没有统一的"目标仍在授权根
  内"校验，存在 symlink 跳转 / 相对路径逃逸风险。

不变量：

1. 任何写文件前，目标 ``resolve()`` 后必须仍在 ``authorized_root`` 之内；否则拒绝。
2. 拒绝 symlink 跳转：``resolve()`` 已展开 symlink；再断言结果路径以授权根为前缀。
3. 拒绝相对路径逃逸：``..`` 在 ``resolve()`` 后被消除；若 resolve 失败或跨根，拒绝。
4. 文件名（用于落盘的 basename）一律走 ``safe_filename``：合法 ID 保留，其余用
   稳定 sha256，杜绝 ``../`` 等路径语义混入文件名。

本模块纯逻辑、无 I/O；``resolve_within`` 不创建文件、不跟随不存在的 symlink
（用 ``strict=False`` 容忍目标尚未创建，但仍展开已存在的 symlink 前缀）。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path


class PathPolicyError(Exception):
    """路径越权/逃逸的统一错误。携带稳定诊断码。"""


# ---------------------------------------------------------------------- #
# 文件名安全化
# ---------------------------------------------------------------------- #


_SAFE_FILENAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def safe_filename(token: str) -> str:
    """把任意 token 转为不含路径语义的文件名（不含扩展名）。

    合法、可读的 ID（``[A-Za-z0-9][A-Za-z0-9._-]{0,127}``）保留原名；其余用
    sha256 hex（稳定、不可逆、无路径分隔符）。原始 token 仍可保留在 JSON 内容里
    供查询，但**绝不**直接进入文件系统路径。
    """
    if not isinstance(token, str) or not token:
        return hashlib.sha256(b"<empty>").hexdigest()
    if _SAFE_FILENAME_RE.fullmatch(token):
        return token
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------- #
# 授权根内校验
# ---------------------------------------------------------------------- #


class SafePathPolicy:
    """授权根路径策略：解析目标并断言仍在根内。

    用法::

        policy = SafePathPolicy(authorized_root=project_dir / "ai")
        safe = policy.resolve_within("audit/../../etc/passwd")  # 抛 PathPolicyError
        ok = policy.resolve_within("audit/abc.json")            # 返回绝对 Path
    """

    def __init__(self, authorized_root: str | Path) -> None:
        # authorized_root 自身 resolve（不要求已存在），取绝对 + 展开 symlink 前缀。
        root = Path(authorized_root).expanduser()
        try:
            self._root = self._best_effort_resolve(root)
        except (OSError, RuntimeError):
            # resolve 失败（如不存在的根）：退回 absolute，后续仍按前缀字符串校验。
            self._root = root.absolute()
        # W07 Windows 修复：旧实现给根字符串硬补 "/"——Windows 根串以
        # "\" 结尾（或无结尾分隔符），补 "/" 产生混合分隔符前缀，导致
        # _is_within 对**所有**根内路径判负（授权根策略在 Windows 整体
        # 不可用）。成员判定改用 Path.relative_to（天然分隔符/大小写语
        # 义正确）；_root_str 仅保留诊断输出，不再参与判定。
        self._root_str = str(self._root)

    @property
    def root(self) -> Path:
        return self._root

    def resolve_within(self, target: str | Path, *, must_exist: bool = False) -> Path:
        """解析 ``target`` 并断言仍在授权根内。

        - 相对路径相对于 ``authorized_root`` 解析。
        - ``resolve()`` 展开 symlink；再断言结果以授权根为前缀。
        - ``must_exist=True`` 时，目标必须已存在（否则抛错）。

        Raises:
            PathPolicyError: 目标逃出授权根、resolve 失败、或必须存在却不存在。
        """
        p = Path(target).expanduser()
        if not p.is_absolute():
            p = self._root / p
        try:
            resolved = self._best_effort_resolve(p, strict=must_exist)
        except FileNotFoundError as exc:
            raise PathPolicyError(
                f"path {target!r} does not exist (must_exist=True)"
            ) from exc
        except (OSError, RuntimeError) as exc:
            raise PathPolicyError(f"cannot resolve path {target!r}: {exc}") from exc

        if not self._is_within(resolved):
            raise PathPolicyError(
                f"path {target!r} resolves outside authorized root "
                f"{self._root!s} (resolved={resolved!s})"
            )
        return resolved

    def join_within(self, *parts: str) -> Path:
        """拼接多段相对路径并断言仍在根内（便捷方法）。

        每段先经 ``safe_filename`` 规范化，杜绝任一段带 ``/`` 或 ``..``。
        """
        if not parts:
            return self._root
        safe_parts = [safe_filename(p) for p in parts]
        candidate = self._root.joinpath(*safe_parts)
        return self.resolve_within(candidate)

    def _is_within(self, resolved: Path) -> bool:
        # W07：Path.relative_to 判定（Windows 大小写不敏感/分隔符语义由
        # PurePath 处理；等值根自身也通过）。
        try:
            resolved.relative_to(self._root)
            return True
        except ValueError:
            return resolved == self._root

    @staticmethod
    def _best_effort_resolve(p: Path, *, strict: bool = False) -> Path:
        """尽量用 ``resolve(strict=...)`` 展开 symlink；不存在时不抛（除非 strict）。"""
        # strict=True 要求全部存在；strict=False 容忍尾部不存在但仍展开已存在前缀。
        return p.resolve(strict=strict)


def assert_within_root(
    target: str | Path, authorized_root: str | Path
) -> Path:
    """无状态便捷函数：断言 target 在 authorized_root 内，返回解析后的绝对路径。"""
    return SafePathPolicy(authorized_root).resolve_within(target)


def is_path_safe(target: str | Path, authorized_root: str | Path) -> bool:
    """无状态便捷函数：非抛出式校验（True=安全，False=越权）。"""
    try:
        SafePathPolicy(authorized_root).resolve_within(target)
        return True
    except PathPolicyError:
        return False
