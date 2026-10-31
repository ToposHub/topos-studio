"""
Plugin Manager for Topos GLSL Plugin System (Phase 1)

Parses .tglsl files and extracts metadata and shader code.
Supports multi-language via i18n translation keys.

P3-4: 支持 @preview_cost 标记重型效果
- high: 播放时轻度降级
- medium: 播放时中度降级
- low: 播放时重度降级
- skip: 播放时完全跳过（AI 插件）
"""

import os
import re
import logging
import threading
from pathlib import Path
from typing import Dict, List, Optional, Any
from src.shared.i18n.i18n import I18N

# P3-4: 导入效果预览质量系统
from src.shared.effects.preview_quality import (
    PreviewQualityLevel,
    EffectCostCategory,
    HeavyEffectDefinition,
    get_effect_preview_quality_manager,
)

logger = logging.getLogger(__name__)


class PluginManager:
    """
    Singleton class that manages GLSL plugins.
    Scans .tglsl files, parses metadata headers, and provides access to plugin data.
    """
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.plugins: Dict[str, Dict[str, Any]] = {}
        # AUD-0800：共享注册表跨线程读写——UI 线程 scan/reload 与渲染
        # 线程 get_plugin 并发。自身变更（scan/reload）在锁内以"解析到
        # 局部快照后一次性合并/替换"完成，读取方在锁内取引用；外部对
        # self.plugins 的单条 dict 操作（如菜单剔除 .pop）在 CPython 下
        # 原子，不破坏一致性。
        self._lock = threading.RLock()

    def scan_plugins(self, directory: str = "plugins") -> Dict[str, Dict[str, Any]]:
        """
        Scan a directory for .tglsl plugin files and parse their metadata.

        Args:
            directory: Path to the plugins directory (default: "plugins")

        Returns:
            Dictionary of parsed plugins keyed by plugin_id
        """
        scanned = self._scan_directory_into({}, directory)
        with self._lock:
            self.plugins.update(scanned)
        return self.plugins

    def _scan_directory_into(
            self, registry: Dict[str, Dict[str, Any]],
            directory: str = "plugins") -> Dict[str, Dict[str, Any]]:
        """把目录内全部插件解析进 ``registry``（不触碰 self.plugins）。

        AUD-0800：解析（文件 IO + 正则）全部发生在局部快照上，完成后由
        调用方一次性合并——渲染线程不会读到"扫描到一半"的中间态注册表。
        """
        # Convert relative path to absolute path based on project root.
        # plugin_manager.py is in src/features/color/core/. ``parents[4]``
        # is the repository root: core -> color -> features -> src -> root.
        PROJECT_ROOT = str(Path(__file__).resolve().parents[4])
        abs_directory = directory if os.path.isabs(directory) \
            else os.path.join(PROJECT_ROOT, directory)

        if not os.path.exists(abs_directory):
            os.makedirs(abs_directory, exist_ok=True)
            return registry

        for filename in os.listdir(abs_directory):
            if filename.endswith('.tglsl'):
                filepath = os.path.join(abs_directory, filename)
                plugin_id = os.path.splitext(filename)[0]

                try:
                    plugin_data = self._parse_plugin_file(filepath, plugin_id)
                    if plugin_data:
                        registry[plugin_id] = plugin_data
                except Exception as e:
                    logger.error(f"Error parsing {filename}: {e}")

        # Scan for AI plugins (.tai files)
        for filename in os.listdir(abs_directory):
            if filename.endswith('.tai'):
                filepath = os.path.join(abs_directory, filename)
                plugin_id = os.path.splitext(filename)[0]

                try:
                    plugin_data = self._parse_ai_plugin_file(filepath, plugin_id)
                    if plugin_data:
                        registry[plugin_id] = plugin_data
                except Exception as e:
                    logger.error(f"Error parsing AI plugin {filename}: {e}")

        return registry
    
    def _parse_plugin_file(self, filepath: str, plugin_id: str) -> Optional[Dict[str, Any]]:
        """
        Parse a single .tglsl file and extract metadata and shader code.

        Args:
            filepath: Path to the .tglsl file
            plugin_id: The plugin identifier (filename without extension)

        Returns:
            Parsed plugin dictionary or None if parsing fails
        """
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()

        # Extract name (with i18n support)
        name_match = re.search(r'//\s*@name\s+(.+)', content)
        name = name_match.group(1).strip() if name_match else plugin_id

        # Extract i18n key for name if provided
        i18n_name_match = re.search(r'//\s*@i18n_name\s+(.+)', content)
        if i18n_name_match:
            i18n_key = i18n_name_match.group(1).strip()
            translated_name = I18N.tr(f"plugins.{i18n_key}.name", name)
            name = translated_name

        # Extract author
        author_match = re.search(r'//\s*@author\s+(.+)', content)
        author = author_match.group(1).strip() if author_match else "Unknown"

        # P3-4: Extract preview_cost (播放时效果降级标记)
        preview_cost_match = re.search(r'//\s*@preview_cost\s+(high|medium|low|skip)', content)
        preview_cost = preview_cost_match.group(1).strip() if preview_cost_match else None

        # FX-M07: Extract version directive
        version_match = re.search(r'//\s*@version\s+(.+)', content)
        version = version_match.group(1).strip() if version_match else ""

        # P1-COLOR-15: capability manifest（色彩域/精度/确定性/导出支持声明）
        capabilities = self._parse_capabilities(content)

        # @description 单行描述（此前出现于插件文件但未被解析）
        description_match = re.search(r'//\s*@description\s+(.+)', content)
        description = description_match.group(1).strip() if description_match else ""

        # Convert preview_cost string to PreviewQualityLevel
        preview_quality_level = None
        if preview_cost:
            preview_quality_level = self._parse_preview_cost(preview_cost)
            # 注册到效果预览质量管理器
            if preview_quality_level:
                self._register_plugin_preview_quality(plugin_id, name, preview_quality_level)

        # Extract parameters (with i18n support)
        params = self._parse_params(content, plugin_id)

        # Extract shader code (everything after the metadata block)
        shader_code = self._extract_shader_code(content)

        # FX-M07: compute content hash for identity verification
        import hashlib
        content_hash = hashlib.sha256(shader_code.encode("utf-8")).hexdigest()

        return {
            "plugin_id": plugin_id,
            "name": name,
            "author": author,
            "type": "dctl",
            "params": params,
            "shader_code": shader_code,
            # P3-4: 添加 preview_cost 字段
            "preview_cost": preview_cost,
            "preview_quality_level": preview_quality_level,
            # FX-M07: stable identity metadata
            "version": version,
            "content_hash": content_hash,
            "source_uri": filepath,
            # P1-COLOR-15: capability manifest + 描述
            "capabilities": capabilities,
            "description": description,
        }

    # P1-COLOR-15: capability manifest 支持的声明键（值不设白名单，未知的
    # 原样保留并在摘要中展示——宁可显示生值也不静默丢弃声明）
    CAPABILITY_KEYS = (
        "input_space", "output_space", "alpha", "precision",
        "determinism", "export_support", "threading",
        # Phase 7 P7-07：后端声明轴（逗号分隔，如
        # ``// @graphics_backends opengl,metal``）。缺省按 OpenGL-only /
        # glsl_fragment 处理（与 EffectCapability 默认一致——旧插件行为不变）。
        "graphics_backends", "compute_backends",
        # O7（遮挡裁剪优化计划）：遮挡安全声明。``// @occlusion
        # background_independent`` 表示插件不读取/不依赖下层背景且不把
        # 可见内容扩展出画布。缺省不声明 = fail-closed（阻止高级裁剪）。
        "occlusion",
    )

    # P7-07：缺省声明值（键存在才写 default，未声明的旧插件不被"伪造"为
    # 显式声明过——但解析产物里给出确定值，消费方无需再猜）
    BACKEND_DECLARATION_DEFAULTS = {
        "graphics_backends": "opengl",
        "compute_backends": "glsl_fragment",
    }

    def _parse_capabilities(self, content: str) -> Dict[str, str]:
        """解析头部 capability 声明（如 ``// @input_space scene-linear``）。"""
        capabilities: Dict[str, str] = {}
        for key in self.CAPABILITY_KEYS:
            match = re.search(rf'//\s*@{key}\s+(\S+)', content)
            if match:
                capabilities[key] = match.group(1).strip()
            elif key in self.BACKEND_DECLARATION_DEFAULTS:
                capabilities[key] = self.BACKEND_DECLARATION_DEFAULTS[key]
        return capabilities

    def _parse_preview_cost(self, cost_str: str) -> Optional[PreviewQualityLevel]:
        """
        P3-4: 解析 preview_cost 标记。

        Args:
            cost_str: preview_cost 值（high/medium/low/skip）

        Returns:
            PreviewQualityLevel 枚举值或 None
        """
        cost_map = {
            "high": PreviewQualityLevel.HIGH,
            "medium": PreviewQualityLevel.MEDIUM,
            "low": PreviewQualityLevel.LOW,
            "skip": PreviewQualityLevel.SKIP,
        }
        return cost_map.get(cost_str.lower())

    def _register_plugin_preview_quality(
        self,
        plugin_id: str,
        plugin_name: str,
        preview_quality: PreviewQualityLevel
    ) -> None:
        """
        P3-4: 将插件注册到效果预览质量管理器。

        Args:
            plugin_id: 插件 ID
            plugin_name: 插件显示名称
            preview_quality: 预览质量级别
        """
        manager = get_effect_preview_quality_manager()

        # 确定成本类别（SKIP 为 AI 推理，其他为 Shader）
        cost_category = (
            EffectCostCategory.AI_INFERENCE
            if preview_quality == PreviewQualityLevel.SKIP
            else EffectCostCategory.SHADER_HEAVY
        )

        # 创建重型效果定义
        definition = HeavyEffectDefinition(
            effect_type=f"plugin:{plugin_id}",
            cost_category=cost_category,
            preview_quality=preview_quality,
            degradation_params={"enabled": False},
            ui_label=plugin_name,
            description=f"Plugin {plugin_name} - {preview_quality.name} preview quality",
        )

        manager.register_heavy_effect(definition)
        logger.debug(f"P3-4: Registered plugin preview quality: {plugin_id} -> {preview_quality.name}")
    
    def _parse_params(self, content: str, plugin_id: str = "") -> List[Dict[str, Any]]:
        """
        Parse @param directives from the plugin content.

        Supported formats:
        - @param slider var_name "Label" default min max
        - @param checkbox var_name "Label" default
        - @param color var_name "Label" r g b
        - @param select var_name "Label" default "option1" "option2" ...

        Args:
            content: The full content of the .tglsl file
            plugin_id: The plugin identifier for i18n lookup

        Returns:
            List of parameter dictionaries
        """
        params = []

        # Regex pattern for @param lines
        # Matches: // @param type var_name "label" value1 [value2 value3]
        param_pattern = r'//\s*@param\s+(\w+)\s+(\w+)\s+"([^"]+)"\s+(.+)'

        for match in re.finditer(param_pattern, content):
            param_type = match.group(1)
            var_name = match.group(2)
            label = match.group(3)
            values_str = match.group(4).strip()

            # Try to get translated label using i18n
            if plugin_id:
                translated_label = I18N.tr(f"plugins.{plugin_id}.params.{var_name}", label)
                label = translated_label

            param = {
                "type": param_type,
                "var_name": var_name,
                "label": label
            }
            
            if param_type == "slider":
                # slider: default min max
                value_parts = values_str.split()
                if len(value_parts) >= 3:
                    param["default"] = float(value_parts[0])
                    param["min"] = float(value_parts[1])
                    param["max"] = float(value_parts[2])
                elif len(value_parts) == 1:
                    param["default"] = float(value_parts[0])
                    param["min"] = 0.0
                    param["max"] = 1.0
                    
            elif param_type == "checkbox":
                # checkbox: default (0 or 1)
                param["default"] = int(float(values_str.split()[0]))
                
            elif param_type == "color":
                # color: r g b (values 0-1)
                value_parts = values_str.split()
                if len(value_parts) >= 3:
                    param["default"] = [
                        float(value_parts[0]),
                        float(value_parts[1]),
                        float(value_parts[2])
                    ]
                else:
                    param["default"] = [1.0, 1.0, 1.0]
                    
            elif param_type == "select":
                # select: default "option1" "option2" ...
                # Parse default value and options
                select_pattern = r'([\d.]+)\s+(.+)'
                select_match = re.match(select_pattern, values_str)
                if select_match:
                    param["default"] = int(float(select_match.group(1)))
                    # Extract quoted options
                    options = re.findall(r'"([^"]+)"', select_match.group(2))
                    param["options"] = options
            else:
                # Unknown type - store raw values
                param["default"] = values_str
            
            params.append(param)
        
        return params
    
    def _parse_ai_plugin_file(self, filepath: str, plugin_id: str) -> Optional[Dict[str, Any]]:
        """
        Parse a .tai (Topos AI) plugin file and extract metadata for ONNX inference.

        Supported directives:
        - @name, @i18n_name, @author (same as .tglsl)
        - @category: Category (upscale, denoise, deblur, enhance, style)
        - @ai_model: Path to ONNX model (relative to project root)
        - @ai_tile_size: Tile size for tiled inference (default: 512)
        - @ai_overlap: Overlap between tiles (default: 32)
        - @ai_scale: Output scale factor (default: 1.0)
        - @ai_input_format: Model input format, RGB or BGR (default: RGB)
        - @ai_output_format: Model output format, RGB or BGR (default: RGB)
        - @param: Same syntax as .tglsl

        Args:
            filepath: Path to the .tai file
            plugin_id: The plugin identifier

        Returns:
            Parsed AI plugin dictionary or None if parsing fails
        """
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()

        # Extract name (with i18n support)
        name_match = re.search(r'//\s*@name\s+(.+)', content)
        name = name_match.group(1).strip() if name_match else plugin_id

        i18n_name_match = re.search(r'//\s*@i18n_name\s+(.+)', content)
        if i18n_name_match:
            i18n_key = i18n_name_match.group(1).strip()
            name = I18N.tr(f"plugins.{i18n_key}.name", name)

        # Extract author
        author_match = re.search(r'//\s*@author\s+(.+)', content)
        author = author_match.group(1).strip() if author_match else "Unknown"

        # Extract AI-specific directives
        category_match = re.search(r'//\s*@category\s+(.+)', content)
        category = category_match.group(1).strip() if category_match else "enhance"

        model_match = re.search(r'//\s*@ai_model\s+(.+)', content)
        model_path = model_match.group(1).strip() if model_match else None

        tile_size_match = re.search(r'//\s*@ai_tile_size\s+(\d+)', content)
        tile_size = int(tile_size_match.group(1)) if tile_size_match else 512

        overlap_match = re.search(r'//\s*@ai_overlap\s+(\d+)', content)
        overlap = int(overlap_match.group(1)) if overlap_match else 32

        scale_match = re.search(r'//\s*@ai_scale\s+([\d.]+)', content)
        scale = float(scale_match.group(1)) if scale_match else 1.0

        input_fmt_match = re.search(r'//\s*@ai_input_format\s+(\w+)', content)
        input_format = input_fmt_match.group(1).strip() if input_fmt_match else "RGB"

        output_fmt_match = re.search(r'//\s*@ai_output_format\s+(\w+)', content)
        output_format = output_fmt_match.group(1).strip() if output_fmt_match else "RGB"

        # Validate required fields
        if not model_path:
            logger.info(f"AI plugin {plugin_id} missing @ai_model directive")
            return None

        # P3-4: AI 插件默认为 SKIP（播放时跳过），除非显式指定
        preview_cost_match = re.search(r'//\s*@preview_cost\s+(high|medium|low|skip)', content)
        preview_cost = preview_cost_match.group(1).strip() if preview_cost_match else "skip"  # 默认 skip
        preview_quality_level = self._parse_preview_cost(preview_cost) or PreviewQualityLevel.SKIP

        # 注册到效果预览质量管理器
        self._register_plugin_preview_quality(plugin_id, name, preview_quality_level)

        # Extract parameters (reuse existing parser)
        params = self._parse_params(content, plugin_id)

        return {
            "plugin_id": plugin_id,
            "name": name,
            "author": author,
            "type": "ai",
            "category": category,
            "model_path": model_path,
            "tile_size": tile_size,
            "overlap": overlap,
            "scale": scale,
            "input_format": input_format,
            "output_format": output_format,
            "params": params,
            "shader_code": None,  # No shader code for AI plugins
            # P3-4: AI 插件标记 preview_cost
            "preview_cost": preview_cost,
            "preview_quality_level": preview_quality_level,
        }

    def _extract_shader_code(self, content: str) -> str:
        """
        Extract the GLSL shader code from the plugin file.
        Everything after the metadata block (after the closing ---- line).
        
        Args:
            content: The full content of the .tglsl file
            
        Returns:
            The shader code as a string
        """
        # Find the end of the metadata block
        # Look for the closing comment line: // -------------
        end_pattern = r'//\s*-{5,}'
        match = re.search(end_pattern, content)
        
        if match:
            # Return everything after the metadata block
            shader_code = content[match.end():].strip()
        else:
            # Fallback: find the last @param line and return everything after
            last_param = re.search(r'//\s*@param.+\n', content)
            if last_param:
                # Find the next non-comment line
                remaining = content[last_param.end():]
                # Skip any remaining comment lines
                lines = remaining.split('\n')
                start_idx = 0
                for i, line in enumerate(lines):
                    stripped = line.strip()
                    if stripped and not stripped.startswith('//'):
                        start_idx = i
                        break
                shader_code = '\n'.join(lines[start_idx:]).strip()
            else:
                # Last resort: return the whole content
                shader_code = content
        
        return shader_code
    
    def get_plugin(self, plugin_id: str) -> Optional[Dict[str, Any]]:
        """
        Get a specific plugin by its ID.

        Args:
            plugin_id: The plugin identifier (filename without extension)

        Returns:
            Plugin dictionary or None if not found
        """
        with self._lock:
            return self.plugins.get(plugin_id)

    def get_all_plugins(self) -> Dict[str, Dict[str, Any]]:
        """
        Get all available plugins.

        Returns:
            Dictionary of all plugins keyed by plugin_id
        """
        with self._lock:
            return self.plugins

    def reload_plugins(self, directory: str = "plugins") -> Dict[str, Dict[str, Any]]:
        """
        Rebuild the plugin registry from the directory.

        Args:
            directory: Path to the plugins directory (default: "plugins")

        Returns:
            Dictionary of parsed plugins

        AUD-0800：重扫描先在局部快照上完整解析，成功后在锁内整体替换
        ——旧实现 clear 后边扫边写，渲染线程在窗口期会读到空/半成品
        注册表（插件瞬时缺失 → 静默剔除出 composition）。
        """
        scanned = self._scan_directory_into({}, directory)
        with self._lock:
            self.plugins.clear()
            self.plugins.update(scanned)
        return self.plugins


# Module-level convenience functions
_manager_instance = None

def get_plugin_manager() -> PluginManager:
    """Get the singleton PluginManager instance."""
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = PluginManager()
    return _manager_instance


# P1-COLOR-15: capability manifest 摘要标签（key → 中文说明）
_CAPABILITY_LABELS = {
    "input_space": "输入色彩空间",
    "output_space": "输出色彩空间",
    "alpha": "Alpha 语义",
    "precision": "精度",
    "determinism": "确定性",
    "export_support": "导出支持",
    "threading": "线程模型",
    # Phase 7 P7-07
    "graphics_backends": "图形后端",
    "compute_backends": "计算后端",
}


def format_capability_summary(plugin_data: Dict[str, Any]) -> str:
    """把插件元数据（版本/描述/capability manifest）合成 tooltip 文本。

    没有任何可显示信息时返回空字符串（调用方据此跳过 tooltip）。
    """
    lines: list = []
    description = (plugin_data.get("description") or "").strip()
    if description:
        lines.append(description)
    version = (plugin_data.get("version") or "").strip()
    if version:
        lines.append(f"Version {version}")
    capabilities = plugin_data.get("capabilities") or {}
    for key in PluginManager.CAPABILITY_KEYS:
        value = capabilities.get(key)
        if value:
            label = _CAPABILITY_LABELS.get(key, key)
            lines.append(f"{label}: {value}")
    return "\n".join(lines)
