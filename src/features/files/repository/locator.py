"""Files 新数据目录定位器（B 阶段，[数据边界](docs/plans/files-replacement-data-boundary-20261117.md) §2）。

所有路径由本定位器唯一解析，禁止相对路径/CWD 解析。配置根沿应用既有
规则（``TOPOS_CONFIG_PATH`` → 平台用户目录，config_manager.get_default_config_path
的同源逻辑），``files-data/`` 是新 Files 子系统的唯一持久化边界。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from src.shared.paths.paths import get_config_root

FILES_DATA_DIRNAME = "files-data"
HOST_DB_FILENAME = "host.db"


@dataclass(frozen=True)
class FilesDataLocator:
    """``<config_root>/files-data/`` 下全部受管路径的唯一定位器。"""

    root: Path

    @classmethod
    def for_config_root(cls, config_root: Path | str) -> "FilesDataLocator":
        return cls(root=Path(config_root).resolve() / FILES_DATA_DIRNAME)

    @classmethod
    def default(cls) -> "FilesDataLocator":
        return cls.for_config_root(Path(get_config_root()))

    # ------------------------------------------------------------ 基础

    @property
    def catalog_db(self) -> Path:
        return self.root / "catalog.db"

    @property
    def schema_version_file(self) -> Path:
        return self.root / "schema_version"

    @property
    def libraries_file(self) -> Path:
        return self.root / "libraries.json"

    @property
    def backups_dir(self) -> Path:
        return self.root / "backups"

    # ------------------------------------------------------------ 回收站 / 缓存

    def trash_dir(self, library_id: str) -> Path:
        return self.root / "trash" / library_id

    @property
    def cache_thumbnails_dir(self) -> Path:
        return self.root / "cache" / "thumbnails"

    @property
    def cache_strips_dir(self) -> Path:
        return self.root / "cache" / "strips"

    @property
    def cache_probe_dir(self) -> Path:
        return self.root / "cache" / "probe"

    # ------------------------------------------------------------ 插件

    @property
    def plugins_install_root(self) -> Path:
        return self.root / "plugins" / "install"

    @property
    def plugins_state_file(self) -> Path:
        return self.root / "plugins" / "state.json"

    @property
    def plugins_logs_dir(self) -> Path:
        return self.root / "plugins" / "logs"

    # ------------------------------------------------------------ 宿主库

    @property
    def host_db(self) -> Path:
        """宿主 settings/layouts 库的显式路径（消除 CWD 默认 topos.db）。

        属配置根而非 files-data/（宿主数据不是 Files 受管数据；H 阶段
        重置清单将两者分开处置）。
        """
        return self.root.parent / HOST_DB_FILENAME

    def ensure_layout(self) -> None:
        """幂等创建受管目录骨架（不含库级 trash/<id>，注册库时建）。"""
        for directory in (
            self.root,
            self.root / "trash",
            self.cache_thumbnails_dir,
            self.cache_strips_dir,
            self.cache_probe_dir,
            self.plugins_install_root,
            self.plugins_logs_dir,
            self.backups_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
