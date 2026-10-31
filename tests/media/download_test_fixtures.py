#!/usr/bin/env python3
"""
Test Fixtures Downloader for P5-2 Standard Material Matrix

从公开测试素材源下载或本地生成测试素材。

## 功能
- 从公开源下载专业编码格式素材（ProRes, DNxHR 等）
- 使用 FFmpeg 生成合成素材（作为 fallback）
- 支持增量下载（只下载缺失的素材）
- 支持素材校验（MD5/SHA256）

## 使用方法
    python tests/media/download_test_fixtures.py --all
    python tests/media/download_test_fixtures.py --fixtures prores_422_hq_1080p,dnxhr_hqx_4k
    python tests/media/download_test_fixtures.py --group p52_intra
    python tests/media/download_test_fixtures.py --verify

## Fallback 策略
1. 优先从公开源下载
2. 如果下载失败，使用 FFmpeg 生成合成素材
3. 如果生成失败，使用现有素材
4. 如果所有都失败，跳过测试并发出警告
"""

from __future__ import annotations

import os
import sys
import json
import subprocess
import argparse
import logging
import time
import hashlib
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class DownloadStatus(Enum):
    """下载状态"""
    SUCCESS = 'success'
    FAILED = 'failed'
    SKIPPED = 'skipped'
    GENERATED = 'generated'  # FFmpeg 生成的
    EXISTING = 'existing'


@dataclass
class DownloadResult:
    """下载结果"""
    fixture_id: str
    status: DownloadStatus
    path: Optional[Path] = None
    error: Optional[str] = None
    elapsed_time: float = 0.0
    file_size_mb: float = 0.0
    source: str = ''


# 公开测试素材源定义
PUBLIC_DOWNLOAD_SOURCES: Dict[str, Dict[str, Any]] = {
    # 注意：这些 URL 是示例，实际使用时需要替换为真实可用的源
    'xiph_test_media': {
        'name': 'Xiph.org Test Media',
        'base_url': 'https://media.xiph.org/video/derf/',
        'description': '标准测试视频序列',
        'available_formats': ['h264', 'y4m', 'raw'],
        'fixtures': {
            # Xiph.org 有很多标准测试序列，如 bus, foreman, etc.
            # 这些是 Y4M 或 H.264 格式，不含 ProRes/DNxHR
            'bus_1080p': {
                'url': 'https://media.xiph.org/video/derf/y4m/bus_cif.y4m',
                'note': '需要转换格式',
            },
        },
    },
    'ffmpeg_generated': {
        'name': 'FFmpeg Generated Synthetic',
        'base_url': 'local://synthetic',
        'description': '本地 FFmpeg 生成合成素材',
        'available_formats': ['h264', 'h265', 'prores', 'dnxhr', 'nv12', 'p010'],
        'is_local': True,
    },
}


class FixtureDownloader:
    """素材下载器"""

    def __init__(self, output_dir: Optional[str] = None, use_proxy: bool = False):
        """
        初始化下载器。

        Args:
            output_dir: 输出目录，默认为 tests/media/fixtures/
            use_proxy: 是否使用代理下载
        """
        if output_dir:
            self.output_dir = Path(output_dir)
        else:
            project_root = Path(__file__).parent.parent.parent
            self.output_dir = project_root / 'tests' / 'media' / 'fixtures'

        self.output_dir.mkdir(parents=True, exist_ok=True)

        # 加载 manifest
        self.manifest: Dict[str, Any] = {}
        self._load_manifest()

        # 初始化生成器（作为 fallback）
        self.generator = None  # Will be initialized if needed

        # 下载配置
        self.config = {
            'timeout': 30,
            'chunk_size': 8192,
            'max_retries': 3,
            'retry_delay': 5,
            'use_proxy': use_proxy,
        }

    def _load_manifest(self) -> None:
        """加载 fixtures_manifest.json"""
        manifest_path = Path(__file__).parent / 'fixtures_manifest.json'
        if manifest_path.exists():
            try:
                with open(manifest_path, 'r', encoding='utf-8') as f:
                    self.manifest = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Failed to load manifest: {e}")

    def _init_generator(self) -> None:
        """初始化 FFmpeg 生成器"""
        if self.generator is None:
            from generate_test_fixtures import FixtureGenerator
            self.generator = FixtureGenerator(str(self.output_dir))

    def check_connection(self, url: str) -> bool:
        """检查网络连接"""
        try:
            request = urllib.request.Request(url, method='HEAD')
            urllib.request.urlopen(request, timeout=self.config['timeout'])
            return True
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            return False

    def download_file(self, url: str, output_path: Path) -> Tuple[bool, Optional[str]]:
        """
        下载文件。

        Args:
            url: 下载 URL
            output_path: 输出路径

        Returns:
            (success, error_message)
        """
        logger.info(f"下载: {url}")

        try:
            request = urllib.request.Request(url)
            response = urllib.request.urlopen(request, timeout=self.config['timeout'])

            total_size = response.headers.get('Content-Length')
            if total_size:
                total_size = int(total_size)
                logger.info(f"  文件大小: {total_size / 1024 / 1024:.1f} MB")

            output_path.parent.mkdir(parents=True, exist_ok=True)

            downloaded = 0
            with open(output_path, 'wb') as f:
                while True:
                    chunk = response.read(self.config['chunk_size'])
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)

                    # 进度显示
                    if total_size:
                        percent = (downloaded / total_size) * 100
                        logger.debug(f"  进度: {percent:.1f}%")

            logger.info(f"  下载完成: {output_path}")
            return True, None

        except urllib.error.URLError as e:
            return False, f"URL 错误: {e}"
        except urllib.error.HTTPError as e:
            return False, f"HTTP 错误: {e.code}"
        except TimeoutError:
            return False, "下载超时"
        except Exception as e:
            return False, f"下载失败: {e}"

    def verify_checksum(self, path: Path, expected_md5: Optional[str] = None,
                         expected_sha256: Optional[str] = None) -> bool:
        """
        验证文件 checksum。

        Args:
            path: 文件路径
            expected_md5: 预期的 MD5
            expected_sha256: 预期的 SHA256

        Returns:
            是否验证通过
        """
        if expected_md5:
            logger.info(f"验证 MD5: {path}")
            actual_md5 = self._compute_md5(path)
            if actual_md5 != expected_md5:
                logger.error(f"  MD5 不匹配: 期望 {expected_md5}, 实际 {actual_md5}")
                return False
            logger.info(f"  MD5 验证通过")

        if expected_sha256:
            logger.info(f"验证 SHA256: {path}")
            actual_sha256 = self._compute_sha256(path)
            if actual_sha256 != expected_sha256:
                logger.error(f"  SHA256 不匹配: 期望 {expected_sha256}, 实际 {actual_sha256}")
                return False
            logger.info(f"  SHA256 验证通过")

        return True

    def _compute_md5(self, path: Path) -> str:
        """计算文件 MD5"""
        hash_md5 = hashlib.md5()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()

    def _compute_sha256(self, path: Path) -> str:
        """计算文件 SHA256"""
        hash_sha256 = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                hash_sha256.update(chunk)
        return hash_sha256.hexdigest()

    def download_fixture(self, fixture_id: str, force: bool = False) -> DownloadResult:
        """
        下载或生成素材。

        Args:
            fixture_id: 素材 ID
            force: 是否强制重新下载/生成

        Returns:
            DownloadResult
        """
        start_time = time.time()

        # 查找 fixture 定义
        fixture_spec = self._find_fixture_spec(fixture_id)
        if not fixture_spec:
            return DownloadResult(
                fixture_id=fixture_id,
                status=DownloadStatus.SKIPPED,
                error="Unknown fixture ID",
            )

        output_path = self.output_dir / fixture_spec.get('file_path', '')

        # 检查是否已存在
        if output_path.exists() and not force:
            # 验证 checksum（如果有）
            checksum = fixture_spec.get('checksum', {})
            expected_md5 = checksum.get('md5')
            expected_sha256 = checksum.get('sha256')

            if expected_md5 or expected_sha256:
                if self.verify_checksum(output_path, expected_md5, expected_sha256):
                    elapsed = time.time() - start_time
                    return DownloadResult(
                        fixture_id=fixture_id,
                        status=DownloadStatus.EXISTING,
                        path=output_path,
                        elapsed_time=elapsed,
                        file_size_mb=output_path.stat().st_size / 1024 / 1024,
                        source='existing',
                    )
                else:
                    # checksum 不匹配，重新下载
                    output_path.unlink(missing_ok=True)
            else:
                elapsed = time.time() - start_time
                return DownloadResult(
                    fixture_id=fixture_id,
                    status=DownloadStatus.EXISTING,
                    path=output_path,
                    elapsed_time=elapsed,
                    file_size_mb=output_path.stat().st_size / 1024 / 1024,
                    source='existing',
                )

        # 尝试从公开源下载
        download_url = self._find_download_url(fixture_id, fixture_spec)
        if download_url and not download_url.startswith('local://'):
            success, error = self.download_file(download_url, output_path)
            if success:
                elapsed = time.time() - start_time
                return DownloadResult(
                    fixture_id=fixture_id,
                    status=DownloadStatus.SUCCESS,
                    path=output_path,
                    elapsed_time=elapsed,
                    file_size_mb=output_path.stat().st_size / 1024 / 1024,
                    source='download',
                )
            else:
                logger.warning(f"下载失败，尝试 FFmpeg 生成: {error}")

        # Fallback: 使用 FFmpeg 生成
        logger.info(f"使用 FFmpeg 生成: {fixture_id}")
        self._init_generator()

        generated_path = None
        if fixture_id in ['nv12_sample_1080p', 'p010_sample_1080p']:
            # 特殊处理原始样本
            if fixture_id == 'nv12_sample_1080p':
                generated_path = self.generator.generate_nv12_sample(fixture_spec)
            else:
                generated_path = self.generator.generate_p010_sample(fixture_spec)
        else:
            # 从 generate_test_fixtures.py 导入
            from generate_test_fixtures import P52_FIXTURES
            if fixture_id in P52_FIXTURES:
                generated_path = self.generator.generate_fixture(
                    P52_FIXTURES[fixture_id], force=True
                )

        elapsed = time.time() - start_time

        if generated_path:
            return DownloadResult(
                fixture_id=fixture_id,
                status=DownloadStatus.GENERATED,
                path=generated_path,
                elapsed_time=elapsed,
                file_size_mb=generated_path.stat().st_size / 1024 / 1024,
                source='ffmpeg',
            )
        else:
            return DownloadResult(
                fixture_id=fixture_id,
                status=DownloadStatus.FAILED,
                error="Failed to download or generate",
                elapsed_time=elapsed,
            )

    def _find_fixture_spec(self, fixture_id: str) -> Optional[Dict[str, Any]]:
        """在 manifest 中查找 fixture 定义"""
        # 首先查找 P5-2 素材矩阵
        p52_fixtures = self.manifest.get('p52_standard_material_matrix', {}).get('fixtures', {})
        if fixture_id in p52_fixtures:
            return p52_fixtures[fixture_id]

        # 查找其他 fixtures
        fixtures = self.manifest.get('fixtures', {})
        if fixture_id in fixtures:
            return fixtures[fixture_id]

        # 在嵌套组中查找
        for key, value in fixtures.items():
            if isinstance(value, dict) and 'fixtures' in value:
                nested = value.get('fixtures', {})
                if fixture_id in nested:
                    return nested[fixture_id]

        # 查找 p10a_required_fixtures
        p10a_fixtures = self.manifest.get('p10a_required_fixtures', {})
        if fixture_id in p10a_fixtures:
            return p10a_fixtures[fixture_id]

        # 查找 davinci_level_validation
        davinci_fixtures = self.manifest.get('davinci_level_validation', {}).get('minimum_test_fixtures', {})
        if fixture_id in davinci_fixtures:
            return davinci_fixtures[fixture_id]

        return None

    def _find_download_url(self, fixture_id: str, fixture_spec: Dict[str, Any]) -> Optional[str]:
        """查找下载 URL"""
        # 目前大多数专业格式素材没有公开下载源
        # 这里返回 None，触发 FFmpeg 生成 fallback

        # 只有公开源支持的素材才有 URL
        # ProRes, DNxHR 等专业格式通常没有公开免费源

        # 对于 H.264/H.265 等常见格式，可能有公开源
        # 但需要根据实际 fixture 定义来匹配

        codec = fixture_spec.get('codec', {})
        codec_name = codec.get('name', '')

        # 目前主要使用 FFmpeg 生成
        # 如果有实际可用的下载源，可以在这里添加映射

        return None

    def download_all(self, fixtures: Optional[List[str]] = None,
                      force: bool = False) -> Dict[str, DownloadResult]:
        """
        下载所有素材或指定素材。

        Args:
            fixtures: 要下载的素材 ID 列表
            force: 是否强制重新下载/生成

        Returns:
            fixture_id -> DownloadResult 的字典
        """
        if fixtures is None:
            # 获取所有 P5-2 素材
            p52_fixtures = self.manifest.get('p52_standard_material_matrix', {}).get('fixtures', {})
            fixtures = list(p52_fixtures.keys())

        results = {}
        for fixture_id in fixtures:
            result = self.download_fixture(fixture_id, force)
            results[fixture_id] = result

        return results

    def verify_all(self) -> Dict[str, bool]:
        """
        验证所有现有素材。

        Returns:
            fixture_id -> 验证结果的字典
        """
        p52_fixtures = self.manifest.get('p52_standard_material_matrix', {}).get('fixtures', {})
        results = {}

        for fixture_id, fixture_spec in p52_fixtures.items():
            output_path = self.output_dir / fixture_spec.get('file_path', '')

            if not output_path.exists():
                results[fixture_id] = False
                logger.warning(f"素材缺失: {fixture_id}")
                continue

            checksum = fixture_spec.get('checksum', {})
            expected_md5 = checksum.get('md5')
            expected_sha256 = checksum.get('sha256')

            # 如果没有 checksum 定义，使用 FFprobe 验证基本属性
            if not expected_md5 and not expected_sha256:
                results[fixture_id] = self._verify_with_ffprobe(output_path, fixture_spec)
            else:
                results[fixture_id] = self.verify_checksum(output_path, expected_md5, expected_sha256)

        return results

    def _verify_with_ffprobe(self, path: Path, fixture_spec: Dict[str, Any]) -> bool:
        """使用 FFprobe 验证素材基本属性"""
        try:
            result = subprocess.run(
                ['ffprobe', '-v', 'error', '-show_entries',
                 'stream=width,height,pix_fmt,r_frame_rate,codec_name',
                 '-of', 'json', str(path)],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode != 0:
                return False

            data = json.loads(result.stdout)
            streams = data.get('streams', [])

            if not streams:
                return False

            video_stream = streams[0]

            # 检查分辨率
            expected_resolution = fixture_spec.get('resolution', {})
            if int(video_stream.get('width', 0)) != expected_resolution.get('width', 0):
                logger.warning(f"宽度不匹配: {fixture_spec.get('id')}")
                return False

            if int(video_stream.get('height', 0)) != expected_resolution.get('height', 0):
                logger.warning(f"高度不匹配: {fixture_spec.get('id')}")
                return False

            return True

        except Exception as e:
            logger.error(f"FFprobe 验证失败: {e}")
            return False

    def print_status_report(self, results: Dict[str, DownloadResult]) -> None:
        """打印下载状态报告"""
        print("\n" + "=" * 70)
        print("P5-2 Standard Material Matrix Download Report")
        print("=" * 70)

        total = len(results)
        success = sum(1 for r in results.values() if r.status in [DownloadStatus.SUCCESS, DownloadStatus.GENERATED, DownloadStatus.EXISTING])
        failed = sum(1 for r in results.values() if r.status == DownloadStatus.FAILED)
        skipped = sum(1 for r in results.values() if r.status == DownloadStatus.SKIPPED)

        print(f"\nSummary: {success}/{total} fixtures available, {failed} failed, {skipped} skipped")

        total_size = sum(r.file_size_mb for r in results.values() if r.path)
        total_time = sum(r.elapsed_time for r in results.values())
        print(f"Total size: {total_size:.1f} MB")
        print(f"Total time: {total_time:.1f} s")

        print("\n--- Detailed Results ---")
        for fixture_id, result in results.items():
            status_str = result.status.value.upper()
            size_str = f"{result.file_size_mb:.1f} MB" if result.path else "N/A"

            print(f"\n[{status_str}] {fixture_id}")
            if result.path:
                print(f"  Path: {result.path}")
                print(f"  Size: {size_str}")
                print(f"  Source: {result.source}")
            if result.error:
                print(f"  Error: {result.error}")

        print("=" * 70)


def get_fixture_groups() -> Dict[str, List[str]]:
    """获取素材组定义"""
    return {
        'p52_standard_matrix': [
            'h264_8bit_420_1080p',
            'h264_8bit_420_4k',
            'h265_10bit_420_4k',
            'prores_422_hq_1080p',
            'prores_422_hq_4k',
            'prores_4444_1080p',
            'prores_4444_4k',
            'dnxhr_hqx_4k',
            'nv12_sample_1080p',
            'p010_sample_1080p',
        ],
        'p52_hw_accel': ['h264_8bit_420_1080p', 'h264_8bit_420_4k', 'h265_10bit_420_4k'],
        'p52_proxy': ['h264_8bit_420_1080p', 'h264_8bit_420_4k', 'prores_422_hq_1080p', 'prores_422_hq_4k'],
        'p52_alpha': ['prores_4444_1080p', 'prores_4444_4k'],
        'p52_intra': ['prores_422_hq_1080p', 'prores_422_hq_4k', 'dnxhr_hqx_4k'],
        'p52_shader_test': ['nv12_sample_1080p', 'p010_sample_1080p'],
    }


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description='Download or generate test fixtures for P5-2 standard material matrix'
    )

    parser.add_argument(
        '--output-dir', '-o',
        type=str,
        help='Output directory for fixtures'
    )

    parser.add_argument(
        '--fixtures', '-f',
        type=str,
        help='Comma-separated list of fixture IDs to download'
    )

    parser.add_argument(
        '--group', '-g',
        type=str,
        help='Download fixtures from a named group'
    )

    parser.add_argument(
        '--all', '-a',
        action='store_true',
        help='Download all fixtures'
    )

    parser.add_argument(
        '--force',
        action='store_true',
        help='Force re-download/re-generate of existing fixtures'
    )

    parser.add_argument(
        '--verify',
        action='store_true',
        help='Verify existing fixtures only'
    )

    parser.add_argument(
        '--list', '-l',
        action='store_true',
        help='List available fixture groups'
    )

    parser.add_argument(
        '--quiet', '-q',
        action='store_true',
        help='Quiet mode'
    )

    args = parser.parse_args()

    if args.quiet:
        logging.basicConfig(level=logging.WARNING)
    else:
        logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

    if args.list:
        print("Available fixture groups:")
        for group, fixtures in get_fixture_groups().items():
            print(f"  {group}: {', '.join(fixtures)}")

        # 显示 fixture manifest 中的组
        downloader = FixtureDownloader(args.output_dir)
        manifest_groups = downloader.manifest.get('fixture_groups', {})
        print("\nManifest groups:")
        for group, fixtures in manifest_groups.items():
            print(f"  {group}: {len(fixtures)} fixtures")

        return

    downloader = FixtureDownloader(args.output_dir)

    if args.verify:
        results = downloader.verify_all()
        print("\nVerification results:")
        for fixture_id, passed in results.items():
            status = "PASS" if passed else "FAIL"
            print(f"  [{status}] {fixture_id}")
        return

    # 定定要下载的素材
    fixtures = None
    if args.fixtures:
        fixtures = [f.strip() for f in args.fixtures.split(',')]
    elif args.group:
        groups = get_fixture_groups()
        fixtures = groups.get(args.group)
        if not fixtures:
            print(f"Error: Unknown group '{args.group}'")
            sys.exit(1)
    elif args.all:
        fixtures = None  # Download all

    results = downloader.download_all(fixtures, args.force)
    downloader.print_status_report(results)


if __name__ == '__main__':
    main()