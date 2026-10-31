"""
节点 Preset/Macro 管理。

允许用户保存、加载、导入导出节点片段为预设模板。
"""

import json
import os
import re
import logging
from datetime import datetime
from typing import Any, List, Dict, Optional
from pathlib import Path

from src.shared.paths.paths import get_project_root

logger = logging.getLogger(__name__)

# AUD-11530: preset names are embedded in file names inside the presets
# directory.  Allow word characters (unicode, so CJK names keep working),
# dash, dot and space — but no path separators and no traversal sequences.
_PRESET_NAME_RE = re.compile(r"^[\w\-. ]+$")


def _is_safe_preset_name(name: Any) -> bool:
    """Return True when ``name`` is safe to embed in a preset file name.

    Rejects non-strings, empty/whitespace-only names, names with leading or
    trailing whitespace, path separators (``/`` and ``\\``) and the ``..``
    traversal sequence (AUD-11530: ``import_preset`` names come from
    external JSON payloads and must never escape the presets directory).
    """
    if not isinstance(name, str):
        return False
    if not name or name != name.strip():
        return False
    if "/" in name or "\\" in name or ".." in name:
        return False
    return bool(_PRESET_NAME_RE.match(name))


def _atomic_write_json(path: Path, data: Any) -> None:
    """原子写入 JSON 文件（AUD-11532）。

    ``open("w")`` 先截断原文件——写入中途崩溃/断电会留下半截 JSON，
    重载时该预设被当损坏文件跳过而静默丢失。这里先写同目录临时文件
    （保证与目标同一文件系统），再 ``os.replace`` 原子落盘：任一时刻
    磁盘上都是完整旧文件或完整新文件。

    Raises:
        OSError: 临时文件写入或替换失败（调用方按原 IOError 语义处理）。
    """
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except OSError:
        # 尽力清理残留临时文件，失败不影响主异常语义
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError as cleanup_exc:
            logger.warning("Preset temp file cleanup failed %s: %s",
                           tmp_path, cleanup_exc)
        raise


class PresetManager:
    """节点 Preset 管理器。

    管理 preset 文件的保存、加载、导入导出和删除。
    Preset 存储在项目根目录的 presets/ 目录中。

    Preset 数据格式：
    {
        "name": "预设名称",
        "description": "预设描述",
        "version": 1,
        "nodes": [...],
        "connections": [...],
        "created_at": "2026-05-14T10:00:00",
        "author": "用户名"  // 可选
    }
    """

    PRESET_VERSION = 1

    def __init__(self, presets_dir: Optional[str] = None):
        """初始化 Preset 管理器。

        Args:
            presets_dir: 预设存储目录，默认为项目根目录下的 presets/
        """
        if presets_dir:
            self._presets_dir = Path(presets_dir)
        else:
            # AUD-11534: 文档承诺「项目根目录下的 presets/」。旧实现取
            # os.getcwd()，随启动目录漂移（从任意 cwd 启动时预设散落到
            # 各处且与文档不符）。改用 src.shared.paths.get_project_root()
            # ——按模块位置锚定仓库根，与 luts/hdri/cache 等资源目录同一
            # 解析基准，不依赖启动目录。
            self._presets_dir = Path(get_project_root()) / "presets"

        self._presets: Dict[str, dict] = {}  # preset_name -> preset_data
        self._ensure_presets_dir()
        self._load_presets()

    def _ensure_presets_dir(self) -> None:
        """确保预设目录存在。"""
        if not self._presets_dir.exists():
            self._presets_dir.mkdir(parents=True, exist_ok=True)

    def _load_presets(self) -> None:
        """从目录加载所有预设。"""
        self._presets.clear()

        if not self._presets_dir.exists():
            return

        for file_path in self._presets_dir.glob("*.json"):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                # 验证基本结构
                if not self._validate_preset(data):
                    continue

                name = data.get("name", file_path.stem)
                self._presets[name] = data

            except (json.JSONDecodeError, IOError) as e:
                # 跳过损坏的文件
                logger.warning(f"Failed to load preset {file_path}: {e}")
                continue

    def _validate_preset(self, data: dict) -> bool:
        """验证预设数据结构。

        Args:
            data: 预设数据字典

        Returns:
            True 如果结构有效
        """
        required_keys = ["nodes", "connections"]
        for key in required_keys:
            if key not in data:
                return False

        # nodes 应该是列表
        if not isinstance(data.get("nodes"), list):
            return False

        # connections 应该是列表
        if not isinstance(data.get("connections"), list):
            return False

        return True

    def save_preset(self, name: str, nodes_data: dict, description: str = "",
                    author: str = "") -> bool:
        """保存节点片段为预设。

        Args:
            name: 预设名称（唯一标识）
            nodes_data: 节点数据（从 copy_selected_nodes 获取）
                {
                    "nodes": [{"id", "type", "position", "parameters", ...}],
                    "connections": [{"src_idx", "src_port", "dst_idx", "dst_port"}]
                }
            description: 预设描述
            author: 作者名（可选）

        Returns:
            是否保存成功
        """
        # 构建预设数据
        preset = {
            "name": name,
            "description": description,
            "version": self.PRESET_VERSION,
            "nodes": nodes_data.get("nodes", []),
            "connections": nodes_data.get("connections", []),
            "created_at": datetime.now().isoformat(),
            "author": author,
        }

        # 验证
        if not self._validate_preset(preset):
            return False

        # AUD-11530: 名称直接拼进文件路径，必须是安全文件名
        if not _is_safe_preset_name(name):
            logger.warning("Unsafe preset name rejected: %r", name)
            return False

        # 写入文件
        file_path = self._presets_dir / f"{name}.json"
        try:
            _atomic_write_json(file_path, preset)

            # 更新内存缓存
            self._presets[name] = preset
            return True

        except IOError as e:
            logger.warning(f"Error saving preset: {e}")
            return False

    def load_preset(self, name: str) -> Optional[dict]:
        """加载预设。

        Args:
            name: 预设名称

        Returns:
            预设数据字典，或 None 如果不存在
        """
        return self._presets.get(name)

    def list_presets(self) -> List[str]:
        """列出所有预设名称。

        按创建时间排序（新的在前）。Windows 时钟粒度可达 ~15ms，连续
        保存会落到同一 tick 得到完全相同的 ``created_at``；同一时间戳
        内按保存顺序倒序（后保存的更新），保证排序确定。
        """
        items = list(self._presets.items())  # 插入序
        order = sorted(
            range(len(items)),
            key=lambda i: (items[i][1].get("created_at", ""), i),
            reverse=True,
        )
        return [items[i][0] for i in order]

    def get_preset_info(self, name: str) -> Optional[Dict[str, Any]]:
        """获取预设的基本信息（不含节点数据）。

        Args:
            name: 预设名称

        Returns:
            {
                "name": str,
                "description": str,
                "created_at": str,
                "author": str,
                "node_count": int,
            }
        """
        preset = self._presets.get(name)
        if not preset:
            return None

        return {
            "name": preset.get("name", name),
            "description": preset.get("description", ""),
            "created_at": preset.get("created_at", ""),
            "author": preset.get("author", ""),
            "node_count": len(preset.get("nodes", [])),
        }

    def delete_preset(self, name: str) -> bool:
        """删除预设。

        Args:
            name: 预设名称

        Returns:
            是否删除成功
        """
        # AUD-11530: 名称直接拼进文件路径，必须是安全文件名——否则可在
        # presets 目录外 unlink 任意 .json 文件
        if not _is_safe_preset_name(name):
            logger.warning("Unsafe preset name rejected: %r", name)
            return False

        file_path = self._presets_dir / f"{name}.json"

        try:
            if file_path.exists():
                file_path.unlink()

            # 从内存缓存移除
            self._presets.pop(name, None)
            return True

        except IOError as e:
            logger.warning(f"Error deleting preset: {e}")
            return False

    def rename_preset(self, old_name: str, new_name: str) -> bool:
        """重命名预设。

        Args:
            old_name: 当前名称
            new_name: 新名称

        Returns:
            是否成功
        """
        if old_name not in self._presets:
            return False

        if new_name in self._presets:
            return False  # 新名称已存在

        # AUD-11530: 两个名称都会拼进文件路径，必须是安全文件名
        if not _is_safe_preset_name(old_name) \
                or not _is_safe_preset_name(new_name):
            logger.warning("Unsafe preset name rejected: %r -> %r",
                           old_name, new_name)
            return False

        preset = self._presets[old_name]
        original_name_field = preset.get("name", old_name)
        preset["name"] = new_name

        old_path = self._presets_dir / f"{old_name}.json"
        new_path = self._presets_dir / f"{new_name}.json"

        try:
            # AUD-11531: 先写新文件、成功后再删旧文件。写失败（磁盘满/
            # 权限/非法字符）时旧文件仍在磁盘上，预设数据不会丢失。
            # AUD-11532: 原子落盘，避免中途崩溃留下半截 JSON。
            _atomic_write_json(new_path, preset)
        except IOError as e:
            # 回滚内存中的重命名，保持缓存键与数据一致
            preset["name"] = original_name_field
            # 清掉可能写了一半的新文件（尽力而为，失败不影响结果）
            try:
                if new_path.exists():
                    new_path.unlink()
            except IOError:
                pass
            logger.warning(f"Error renaming preset: {e}")
            return False

        try:
            if old_path.exists():
                old_path.unlink()
        except IOError as e:
            # 新文件已安全落盘；旧文件删除失败只会造成重载后的重名
            # 副本，不会丢数据——告警后按成功处理
            logger.warning(f"Renamed preset but old file removal failed: {e}")

        # 更新内存缓存
        self._presets.pop(old_name, None)
        self._presets[new_name] = preset
        return True

    def import_preset(self, file_path: str) -> Optional[str]:
        """从外部文件导入预设。

        Args:
            file_path: 外部 JSON 文件路径

        Returns:
            导入后的预设名称，或 None 如果失败
        """
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Error importing preset: {e}")
            return None

        if not self._validate_preset(data):
            return None

        # AUD-11530: name 取自外部 JSON 内容，属不可信输入。非字符串或
        # 路径不安全的名称回退到导入文件的 stem，stem 仍不安全则拒绝导入。
        raw_name = data.get("name")
        name = raw_name if isinstance(raw_name, str) else Path(file_path).stem
        if not _is_safe_preset_name(name):
            name = Path(file_path).stem
        if not _is_safe_preset_name(name):
            logger.warning("Import rejected: untrusted preset name %r", raw_name)
            return None

        # 如果名称冲突，添加后缀
        if name in self._presets:
            base_name = name
            counter = 1
            while name in self._presets:
                name = f"{base_name}_{counter}"
                counter += 1
            data["name"] = name

        # 保存到预设目录
        dest_path = self._presets_dir / f"{name}.json"
        try:
            _atomic_write_json(dest_path, data)

            self._presets[name] = data
            return name

        except IOError as e:
            logger.warning(f"Error saving imported preset: {e}")
            return None

    def export_preset(self, name: str, file_path: str) -> bool:
        """导出预设到外部文件。

        Args:
            name: 预设名称
            file_path: 目标文件路径

        Returns:
            是否成功
        """
        preset = self._presets.get(name)
        if not preset:
            return False

        try:
            _atomic_write_json(Path(file_path), preset)
            return True

        except IOError as e:
            logger.warning(f"Error exporting preset: {e}")
            return False

    def update_preset_description(self, name: str, description: str) -> bool:
        """更新预设描述。

        Args:
            name: 预设名称
            description: 新描述

        Returns:
            是否成功
        """
        preset = self._presets.get(name)
        if not preset:
            return False

        # AUD-11530: 名称直接拼进文件路径，必须是安全文件名
        if not _is_safe_preset_name(name):
            logger.warning("Unsafe preset name rejected: %r", name)
            return False

        preset["description"] = description

        file_path = self._presets_dir / f"{name}.json"
        try:
            _atomic_write_json(file_path, preset)
            return True

        except IOError:
            return False

    def preset_exists(self, name: str) -> bool:
        """检查预设是否存在。

        Args:
            name: 预设名称

        Returns:
            True 如果存在
        """
        return name in self._presets

    def get_presets_dir(self) -> Path:
        """获取预设目录路径。

        Returns:
            Path 对象
        """
        return self._presets_dir

    def reload_presets(self) -> None:
        """重新从目录加载所有预设。"""
        self._load_presets()