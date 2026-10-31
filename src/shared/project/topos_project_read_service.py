"""
Topos 工程包统一读取服务。

负责从 .topos ZIP 包中读取强类型 ToposProjectBundle，
包含调色栈、合成图、素材引用、时间线等完整数据。
"""

from __future__ import annotations

import logging
import os
import stat
import zipfile
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToposProjectBundle:
    """
    .topos 工程包读取结果 — 强类型替代旧 dict 返回值。

    Attributes:
        project: ToposProject 核心元数据（可能为 None）
        layers: 重建后的 Layer 对象列表
        raw_params: RAW 参数
        active_layer_index: 当前活跃图层索引
        composite_graph: 合成图数据（第一个 composition）
        composite_state: 合成视图状态
        source_bake: 烘焙数据
        flip_state: 翻转状态
        full_data: ToposProjectIO.load() 的完整 full_data（供高级调用方使用）
        assets_dir: 解压后的 assets 目录路径
    """
    project: Any | None = None  # ToposProject
    layers: list = field(default_factory=list)
    raw_params: dict | None = None
    active_layer_index: int = 0
    composite_graph: dict | None = None
    composite_state: dict | None = None
    source_bake: dict | None = None
    flip_state: dict | None = None
    full_data: dict | None = None
    assets_dir: str | None = None

    def to_legacy_dict(self) -> dict[str, Any]:
        """转换为兼容旧 ProjectManager.load_project() 返回格式的 dict。"""
        return {
            "layers": self.layers,
            "raw_params": self.raw_params,
            "source_bake": self.source_bake,
            "flip_state": self.flip_state,
            "composite_graph": self.composite_graph,
            "composite_state": self.composite_state,
            "active_layer_index": self.active_layer_index,
        }


class ToposProjectReadService:
    """
    .topos 工程包读取服务。

    优先通过 ToposProjectIO 读取新格式，fallback 到旧 ProjectManager 读取旧格式。
    返回强类型 ToposProjectBundle 而非弱类型 dict。
    """

    def load(self, topos_path: str) -> ToposProjectBundle | None:
        """
        统一读取 .topos 工程包。

        Args:
            topos_path: .topos 工程包路径

        Returns:
            ToposProjectBundle 或 None（文件不存在或格式错误）
        """
        if not topos_path or not os.path.exists(topos_path):
            return None

        # 1. 尝试新格式读取
        result = self._load_from_topos_io(topos_path)
        if result is not None:
            return result

        # 2. Fallback 到旧格式读取
        return self._load_from_project_manager(topos_path)

    def _load_from_topos_io(self, topos_path: str) -> ToposProjectBundle | None:
        """通过 ToposProjectIO 读取新格式数据。"""
        try:
            from src.shared.project.topos_project_io import ToposProjectIO, ToposProjectIOConfig

            config = ToposProjectIOConfig()
            topos_project, full_data = ToposProjectIO.load(
                filepath=topos_path,
                config=config,
                load_full_data=True,
            )

            if full_data is None:
                return None

            # 提取 color stack 数据 -> Layer 对象
            layers: list = []
            raw_params = None
            active_layer_index = 0

            color_stacks = full_data.get("color_stacks")
            if color_stacks and isinstance(color_stacks, dict):
                layers, raw_params, active_layer_index = self._color_stack_dict_to_layers(
                    color_stacks,
                    assets_dir=full_data.get("assets_dir"),
                )

            # 提取 composite 数据
            composite_graph = None
            composite_viewer_state = None

            compositions = full_data.get("compositions")
            if compositions and isinstance(compositions, dict):
                # 取第一个 composition
                for comp_id, comp_data in compositions.items():
                    if comp_data and isinstance(comp_data, dict):
                        composite_graph = comp_data
                        break

            composite_viewer_state_data = full_data.get("composite_viewer_state")
            if composite_viewer_state_data:
                composite_viewer_state = composite_viewer_state_data

            # 只有确实提取到了数据才返回新格式结果
            if layers or raw_params or composite_graph:
                return ToposProjectBundle(
                    project=topos_project,
                    layers=layers,
                    raw_params=raw_params,
                    active_layer_index=active_layer_index,
                    composite_graph=composite_graph,
                    composite_state=composite_viewer_state,
                    full_data=full_data,
                    assets_dir=full_data.get("assets_dir"),
                )

            return None

        except Exception as e:
            logger.debug(f"ToposProjectIO load failed, will fallback: {e}")
            return None

    def _load_from_project_manager(self, topos_path: str) -> ToposProjectBundle | None:
        """Fallback 到旧 ProjectManager._load_zip_project() 读取。

        AUD-37112: 旧读取器的解压路径是未加固的 ``extractall``（无成员
        数/总大小/路径防护）。不可信 ZIP 在进入该路径前必须先通过与新
        格式一致的防护校验；不安全的归档直接拒绝（返回 None），不再
        被路由进 extractall。
        """
        if zipfile.is_zipfile(topos_path):
            try:
                self._assert_legacy_archive_safe(topos_path)
            except (ValueError, zipfile.BadZipFile, OSError) as exc:
                logger.error(
                    "Refusing legacy fallback extraction of unsafe archive "
                    f"{topos_path}: {exc}"
                )
                return None
        try:
            from src.shared.project.project_manager import ProjectManager
            pm = ProjectManager()
            result = pm.load_project(topos_path)
            if result is None:
                return None
            # 旧格式返回的是 dict，包装为 ToposProjectBundle
            return ToposProjectBundle(
                layers=result.get("layers", []),
                raw_params=result.get("raw_params"),
                active_layer_index=result.get("active_layer_index", 0),
                composite_graph=result.get("composite_graph"),
                composite_state=result.get("composite_state"),
                source_bake=result.get("source_bake"),
                flip_state=result.get("flip_state"),
            )
        except Exception as e:
            logger.error(f"Failed to load project (both new and old format): {topos_path}, {e}")
            return None

    def _assert_legacy_archive_safe(self, topos_path: str) -> None:
        """AUD-37112: 旧 extractall 回退路径的防护校验。

        检查与新格式解压一致的护栏：成员数上限、声明的未压缩总大小
        上限、符号链接成员、绝对/盘符/父目录逃逸成员名。

        Raises:
            ValueError: 归档不适合进入未加固的 extractall。
            zipfile.BadZipFile / OSError: 归档无法读取。
        """
        from src.shared.project.topos_project_io import (
            MAX_ARCHIVE_ENTRIES,
            MAX_ARCHIVE_TOTAL_BYTES,
        )

        with zipfile.ZipFile(topos_path, "r") as zf:
            infos = zf.infolist()
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                raise ValueError(
                    f"archive declares {len(infos)} entries "
                    f"(limit {MAX_ARCHIVE_ENTRIES})"
                )
            declared_total = sum(info.file_size for info in infos)
            if declared_total > MAX_ARCHIVE_TOTAL_BYTES:
                raise ValueError(
                    f"archive declares {declared_total} uncompressed bytes "
                    f"(limit {MAX_ARCHIVE_TOTAL_BYTES})"
                )
            for info in infos:
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise ValueError(
                        f"archive contains a symlink member: {info.filename}"
                    )
                normalized = os.path.normpath(info.filename.replace("\\", "/"))
                parts = normalized.split("/")
                if (
                    normalized.startswith("/")
                    or ".." in parts
                    or os.path.splitdrive(normalized)[0]
                ):
                    raise ValueError(
                        f"archive member escapes extraction dir: {info.filename}"
                    )

    def _color_stack_dict_to_layers(
        self,
        color_stacks: dict,
        assets_dir: str | None = None,
    ) -> tuple[list, dict | None, int]:
        """
        从 color_stacks.json 的 dict 格式提取 layers 并重建 Layer 对象列表。

        Args:
            color_stacks: color_stacks.json 的完整数据（{stack_id: stack_data}）
            assets_dir: 解压后的 assets 目录，用于恢复 matte/baked 等外部资源

        Returns:
            (layers, raw_params, active_layer_index)
        """
        from .layer_adapter import layer_from_dict

        layers: list = []
        raw_params = None
        active_layer_index = 0

        # 取第一个 stack
        for stack_id, stack_data in color_stacks.items():
            if not isinstance(stack_data, dict):
                continue

            # 提取 layers 数组
            layers_data = stack_data.get("layers", [])
            for layer_dict in layers_data:
                if isinstance(layer_dict, dict):
                    try:
                        layer = layer_from_dict(layer_dict, assets_dir=assets_dir)
                        layers.append(layer)
                    except Exception as e:
                        logger.warning(f"Failed to deserialize layer: {e}")

            # 提取 raw_params
            raw_params = stack_data.get("raw_params")

            # 提取 active_layer_index
            active_layer_index = stack_data.get("active_layer_index", 0)

            break  # 只取第一个 stack

        return layers, raw_params, active_layer_index
