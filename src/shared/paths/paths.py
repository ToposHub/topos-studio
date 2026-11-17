"""
统一路径解析工具

提供不依赖 os.getcwd() 的路径解析函数，确保从不同工作目录启动应用时，
资源路径（LUT、模型等）始终指向正确的位置。
"""

import logging
import os
import sys

logger = logging.getLogger(__name__)


def get_project_root() -> str:
    """
    获取项目根目录的绝对路径。

    无论从哪个工作目录启动应用，都能正确定位项目根目录。

    AUD-36601：本文件位于 ``<root>/src/shared/paths/paths.py``，必须回溯
    **4 层**到项目根。旧实现只回溯 3 层得到 ``<root>/src``，导致
    luts/models/cache 全部解析进源码树（``src/luts``、``src/cache``
    即其历史产物）。

    Returns:
        项目根目录的绝对路径
    """
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)

    return os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))


def get_lut_dir() -> str:
    """
    获取 LUT 目录的绝对路径。

    优先级：
    1. 用户配置的 LUT 目录（未来扩展）
    2. 项目根目录下的 luts/ 目录

    Returns:
        LUT 目录的绝对路径
    """
    project_root = get_project_root()
    lut_dir = os.path.join(project_root, "luts")

    if not os.path.exists(lut_dir):
        try:
            os.makedirs(lut_dir, exist_ok=True)
        except OSError as exc:
            # AUD-36602：目录创建失败只降级（仍返回路径交由调用方按
            # 缺失处理），但必须留痕——不得宽泛 except 静默吞掉
            # 权限/磁盘满等领域错误。
            logger.warning("创建 LUT 目录失败 %s: %s", lut_dir, exc)

    return lut_dir


def get_hdri_dir() -> str:
    """
    获取预置 HDR 环境贴图目录的绝对路径。

    用户把 .hdr/.exr 全景图放进此目录，Scene3D/Light3D 的 hdri_preset 下拉
    会自动列出。无素材时 Scene3D 用程序生成的天空渐变作默认环境光。

    注：AUD-36601 修复后 ``get_project_root()`` 已回溯到真正的项目根，
    此处直接复用（不再保留旧的 4 层手工回溯变体）。

    Returns:
        hdri 目录的绝对路径（不存在则自动创建）
    """
    project_root = get_project_root()
    hdri_dir = os.path.join(project_root, "hdri")

    if not os.path.exists(hdri_dir):
        try:
            os.makedirs(hdri_dir, exist_ok=True)
        except OSError as exc:
            # AUD-36602：同 get_lut_dir——留痕降级，不静默吞错。
            logger.warning("创建 hdri 目录失败 %s: %s", hdri_dir, exc)

    return hdri_dir


def get_models_dir(subdir: str = "") -> str:
    """
    获取模型目录的绝对路径。

    Args:
        subdir: 子目录名称（如 "denoise", "deblur"）

    Returns:
        模型目录的绝对路径
    """
    project_root = get_project_root()
    models_dir = os.path.join(project_root, "models")

    if subdir:
        models_dir = os.path.join(models_dir, subdir)

    if not os.path.exists(models_dir):
        try:
            os.makedirs(models_dir, exist_ok=True)
        except OSError as exc:
            # AUD-36602：同 get_lut_dir——留痕降级，不静默吞错。
            logger.warning("创建模型目录失败 %s: %s", models_dir, exc)

    return models_dir


def get_cache_dir() -> str:
    """
    获取缓存目录的绝对路径。

    Returns:
        缓存目录的绝对路径
    """
    project_root = get_project_root()
    cache_dir = os.path.join(project_root, "cache")

    if not os.path.exists(cache_dir):
        try:
            os.makedirs(cache_dir, exist_ok=True)
        except OSError as exc:
            # AUD-36602：同 get_lut_dir——留痕降级，不静默吞错。
            logger.warning("创建缓存目录失败 %s: %s", cache_dir, exc)

    return cache_dir


def get_config_root() -> str:
    """
    配置根目录（配置文件所在目录），不依赖 os.getcwd()。

    与 ``config_manager.ConfigManager.get_default_config_path`` 同源：
    ``TOPOS_CONFIG_PATH`` 指向配置**文件**（开发形态 src/main.py 注入
    ``<root>/.topos/config.json``），取其父目录；未设置时为平台用户级
    数据目录（打包形态）。B 阶段起持久化路径（宿主库、files-data/）
    一律由本函数派生，消除按 CWD 解析的分叉库。
    """
    override = os.environ.get("TOPOS_CONFIG_PATH")
    if override:
        return os.path.dirname(os.path.abspath(os.path.expanduser(override)))
    from src.shared.config.config_manager import ConfigManager

    return ConfigManager.get_platform_data_dir()


def get_host_db_path() -> str:
    """
    宿主 settings/layouts 库的显式绝对路径（``<config_root>/host.db``）。

    [数据边界 §6] 生产代码禁止再用 ``Database()`` 无参默认（按 CWD 解析
    topos.db，历史上分叉出 src/topos.db）。旧 repo 根 topos.db 仍归旧
    file_explorer 使用，不做迁移。
    """
    return os.path.join(get_config_root(), "host.db")
