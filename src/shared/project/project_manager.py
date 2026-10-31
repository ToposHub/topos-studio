import json
import os
import threading
import zipfile
import tempfile
import shutil
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)
from src.shared.io.file_io import FileIO  # AI Baking: Import FileIO for BAKED_CACHE_DIR

class ProjectManager:
    # AUD-36911: debounce window for scheduled autosaves.
    AUTOSAVE_DEBOUNCE_SECONDS = 2.0

    def __init__(self) -> None:
        self.autosave_timer = None
        self._autosave_qtimer = None  # main-thread QTimer debounce (Qt hosts)
        self.pending_save = False
        self.current_image_path = None
        self.engine_ref = None
        # Callbacks for retrieving composite state (set by MainWindow)
        self.composite_data_callback = None          # () -> dict or None
        self.composite_viewer_state_callback = None   # () -> dict or None

    def get_project_path(self, image_path: str) -> str:
        """Returns the expected path for the project package (.topos)."""
        return f"{os.path.splitext(image_path)[0]}.topos"
    
    def _is_zip_file(self, filepath: str) -> bool:
        """
        Detect if a file is a ZIP archive by checking the magic bytes.
        ZIP files start with 'PK' (0x50 0x4B).
        
        Args:
            filepath: Path to the file to check.
            
        Returns:
            True if the file is a ZIP archive, False otherwise.
        """
        try:
            with open(filepath, 'rb') as f:
                magic = f.read(2)
            return magic == b'PK'
        except Exception:
            return False

    def load_project(self, image_path: str) -> Optional[Dict[str, Any]]:
        """
        Attempts to load a project file associated with the image.
        Supports both new ZIP archive format (v2.0) and legacy plain JSON format (v1.0).
        
        Args:
            image_path: Path to the image file.
            
        Returns:
            A dict with 'layers' and 'raw_params' if successful, or None.
        """
        project_path = self.get_project_path(image_path)
        if not os.path.exists(project_path):
            return None
        
        try:
            # Detect file format
            if self._is_zip_file(project_path):
                # New ZIP archive format (v2.0)
                return self._load_zip_project(project_path)
            else:
                # Legacy plain JSON format (v1.0) - backward compatibility
                return self._load_json_project(project_path)
        except Exception as e:
            logger.error(f"to load project: {e}")
        
        return None
    
    def _load_zip_project(self, project_path: str) -> Optional[Dict[str, Any]]:
        """
        Load a project from a ZIP archive format (v2.0+).

        Supports multiple serialization formats:
        - project.json: JSON format (backward compatible)
        - project.json.gz: gzip compressed JSON
        - project.mp: MessagePack binary format
        - project.mp.gz: gzip compressed MessagePack

        Auto-detects format based on file presence.

        Args:
            project_path: Path to the .topos ZIP file.

        Returns:
            A dict with 'layers' and 'raw_params' if successful, or None.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            # Extract ZIP contents
            with zipfile.ZipFile(project_path, 'r') as zf:
                zf.extractall(temp_dir)

            # Detect project file format (priority: msgpack.gz > msgpack > json.gz > json)
            project_file_path = None
            project_file_name = None

            # Try MessagePack with gzip compression
            mp_gz_path = os.path.join(temp_dir, "project.mp.gz")
            if os.path.exists(mp_gz_path):
                project_file_path = mp_gz_path
                project_file_name = "project.mp.gz"

            # Try MessagePack without compression
            mp_path = os.path.join(temp_dir, "project.mp")
            if project_file_path is None and os.path.exists(mp_path):
                project_file_path = mp_path
                project_file_name = "project.mp"

            # Try JSON with gzip compression
            json_gz_path = os.path.join(temp_dir, "project.json.gz")
            if project_file_path is None and os.path.exists(json_gz_path):
                project_file_path = json_gz_path
                project_file_name = "project.json.gz"

            # Try JSON (default, backward compatible)
            json_path = os.path.join(temp_dir, "project.json")
            if project_file_path is None and os.path.exists(json_path):
                project_file_path = json_path
                project_file_name = "project.json"

            if project_file_path is None:
                logger.info(".topos file: no project data file found")
                return None

            # Load project data based on format
            data = self._load_project_file(project_file_path, project_file_name)

            if data is None:
                return None

            # Get assets directory path
            assets_dir = os.path.join(temp_dir, "assets")
            if not os.path.exists(assets_dir):
                assets_dir = None
            
            # Load layers with assets_dir for bitmap/AI matte loading
            if "layers" in data:
                from .layer_adapter import layer_from_dict
                layers = [layer_from_dict(l, assets_dir=assets_dir) for l in data["layers"]]

                # AI Baking: Copy baked EXR files from assets to local cache
                # and schedule GPU texture upload
                if assets_dir:
                    FileIO._ensure_baked_cache_dir()
                    for layer in layers:
                        if getattr(layer, 'is_baked', False) and getattr(layer, 'baked_file_name', None):
                            baked_src_path = os.path.join(assets_dir, layer.baked_file_name)
                            if os.path.exists(baked_src_path):
                                baked_dst_path = os.path.join(FileIO.BAKED_CACHE_DIR, layer.baked_file_name)
                                if not (os.path.exists(baked_dst_path) and os.path.getsize(baked_dst_path) == os.path.getsize(baked_src_path)):
                                    shutil.copy2(baked_src_path, baked_dst_path)

                # Source bake: Copy denoise/deblur EXR from assets to local cache
                source_bake = data.get("source_bake")
                if source_bake and source_bake.get("source_bake_file_name") and assets_dir:
                    src_path = os.path.join(assets_dir, source_bake["source_bake_file_name"])
                    if os.path.exists(src_path):
                        dst_path = os.path.join(FileIO.BAKED_CACHE_DIR, source_bake["source_bake_file_name"])
                        if not (os.path.exists(dst_path) and os.path.getsize(dst_path) == os.path.getsize(src_path)):
                            shutil.copy2(src_path, dst_path)

                # AI Plugin Results: Copy AI plugin EXR files from assets to local cache
                if assets_dir:
                    for layer in layers:
                        for plugin in getattr(layer, 'plugins', []):
                            ai_result_file = plugin.get("ai_result_file")
                            if ai_result_file:
                                src_path = os.path.join(assets_dir, ai_result_file)
                                if os.path.exists(src_path):
                                    dst_path = os.path.join(FileIO.BAKED_CACHE_DIR, ai_result_file)
                                    if not (os.path.exists(dst_path) and os.path.getsize(dst_path) == os.path.getsize(src_path)):
                                        shutil.copy2(src_path, dst_path)

                return {
                    "layers": layers,
                    "raw_params": data.get("raw_params", None),
                    "source_bake": source_bake,
                    "flip_state": data.get("flip_state", None),
                    "composite_graph": data.get("composite_graph", None),
                    "composite_state": data.get("composite_state", None),
                    "video_denoise": data.get("video_denoise", None),
                }

        return None

    def _load_project_file(self, project_file_path: str, project_file_name: str) -> Optional[Dict[str, Any]]:
        """Load project data from a file with format auto-detection.

        Supports:
        - project.json: JSON format
        - project.json.gz: gzip compressed JSON
        - project.mp: MessagePack binary format
        - project.mp.gz: gzip compressed MessagePack

        Args:
            project_file_path: Path to the project file.
            project_file_name: Name of the project file (used for format detection).

        Returns:
            Project data dict or None if loading failed.
        """
        import gzip

        try:
            # Detect format from file name
            if project_file_name.endswith('.gz'):
                # gzip compressed file
                with gzip.open(project_file_path, 'rb') as f:
                    raw_data = f.read()

                # Determine inner format
                if project_file_name.startswith('project.mp'):
                    # MessagePack format
                    try:
                        import msgpack
                        return msgpack.unpackb(raw_data, raw=False)
                    except ImportError:
                        logger.warning("msgpack not available, cannot load MessagePack format")
                        return None
                else:
                    # JSON format
                    return json.loads(raw_data.decode('utf-8'))

            elif project_file_name.endswith('.mp'):
                # MessagePack without compression
                try:
                    import msgpack
                    with open(project_file_path, 'rb') as f:
                        return msgpack.unpackb(f.read(), raw=False)
                except ImportError:
                    logger.warning("msgpack not available, cannot load MessagePack format")
                    return None

            else:
                # JSON format (default)
                with open(project_file_path, 'r', encoding='utf-8') as f:
                    return json.load(f)

        except Exception as e:
            logger.error(f"to load project file {project_file_name}: {e}")
            return None

    def _load_json_project(self, project_path: str) -> Optional[Dict[str, Any]]:
        """Load a project from legacy plain JSON format (v1.0)."""
        with open(project_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        if "layers" in data:
            # No assets_dir for legacy format (bitmaps were not supported)
            from .layer_adapter import layer_from_dict
            layers = [layer_from_dict(l) for l in data["layers"]]
            return {
                "layers": layers,
                "raw_params": data.get("raw_params", None),
                "source_bake": data.get("source_bake", None),
                "flip_state": data.get("flip_state", None),
                "composite_graph": data.get("composite_graph", None),
                "composite_state": data.get("composite_state", None),
                "video_denoise": data.get("video_denoise", None),
            }

        return None

    def _invalidate_project_thumbnail(self, image_path) -> None:
        """保存后失效缩略图缓存，促使下次按新内容重建。

        AUD-36915：历史实现同时把缩略图 JPEG 写进 ``<cwd>/.cache``——
        该目录随启动工作目录漂移、全库无任何读取方（真缩略图由
        ThumbnailProvider 自管），且数据格式无约定，属只写孤儿，已移除；
        保留唯一被消费的副作用（ThumbnailProvider 缓存失效）。
        """
        try:
            from src.shared.media.thumbnail_provider import ThumbnailProvider
            ThumbnailProvider.instance().cache.remove(image_path)
        except Exception as e:
            logger.error(f"failed to invalidate project thumbnail: {e}")

    def save_project(self, image_path: str, engine: Any, save_preview: bool = True,
                     composite_graph_data: dict = None,
                     composite_viewer_state: dict = None,
                     serialization_format: str = "json",
                     compress_project: bool = False) -> bool:
        """
        Saves the current engine state to disk as a ZIP archive (.topos).

        The ZIP archive contains:
        - project.json or project.mp: Project metadata and layer configurations
        - assets/: Directory containing binary data (bitmaps, AI mattes) as PNG files

        Serialization formats:
        - "json": Human-readable, backward compatible (default)
        - "msgpack": Binary format, 50-70% size reduction for large projects

        Args:
            image_path: Path to the image file.
            engine: The image engine containing layer data.
            save_preview: Whether to save a preview thumbnail (skipped for autosave).
            composite_graph_data: Optional serialized composite node graph.
            composite_viewer_state: Optional composite viewer zoom/pan state.
            serialization_format: "json" (default) or "msgpack" for binary format.
            compress_project: Apply gzip compression to project file (reduces size further).

        Returns:
            True if the archive was written (and atomically swapped in),
            False on I/O or serialization failure (AUD-36910).
        """
        if not image_path or not engine:
            return False

        project_path = self.get_project_path(image_path)

        try:
            # Create a temporary directory for building the archive
            with tempfile.TemporaryDirectory() as temp_dir:
                # Create assets directory
                assets_dir = os.path.join(temp_dir, "assets")
                os.makedirs(assets_dir, exist_ok=True)

                # Build project data with assets_dir for bitmap saving
                data = {
                    "version": "2.0",
                    "_format_version": 1 if serialization_format == "json" else 2,
                    "image_path": image_path,
                    "layers": [l.to_dict(assets_dir=assets_dir) for l in engine.layers],
                    "active_layer_index": engine.active_layer_index,
                    "raw_params": getattr(engine, 'current_raw_params', None),
                    "source_bake": engine.get_source_bake_state(),
                    "flip_state": engine.get_flip_state()
                }

                # M5: 视频降噪参数（独立字段，不与图片降噪 source_bake 共用）
                video_denoise = getattr(engine, 'get_video_denoise_state', lambda: None)()
                if video_denoise:
                    data["video_denoise"] = video_denoise

                # Include composite graph data if provided (relativize image paths)
                if composite_graph_data:
                    project_dir = os.path.dirname(os.path.abspath(image_path))
                    data["composite_graph"] = self._relativize_composite_paths(
                        composite_graph_data, project_dir
                    )

                # Include composite viewer state if provided
                if composite_viewer_state:
                    data["composite_state"] = composite_viewer_state

                # AI Baking: Copy baked EXR files from cache to assets directory
                for layer in engine.layers:
                    if getattr(layer, 'is_baked', False) and getattr(layer, 'baked_file_name', None):
                        baked_src_path = os.path.join(FileIO.BAKED_CACHE_DIR, layer.baked_file_name)
                        if os.path.exists(baked_src_path):
                            baked_dst_path = os.path.join(assets_dir, layer.baked_file_name)
                            shutil.copy2(baked_src_path, baked_dst_path)

                # Source bake: Copy denoise/deblur EXR from cache to assets
                source_bake_state = engine.get_source_bake_state()
                if source_bake_state and source_bake_state.get("source_bake_file_name"):
                    src_path = os.path.join(FileIO.BAKED_CACHE_DIR, source_bake_state["source_bake_file_name"])
                    if os.path.exists(src_path):
                        dst_path = os.path.join(assets_dir, source_bake_state["source_bake_file_name"])
                        shutil.copy2(src_path, dst_path)

                # AI Plugin Results: Save AI plugin results to EXR and copy to assets
                ai_plugin_result_cache = getattr(engine, 'ai_plugin_result_cache', {})
                for layer in engine.layers:
                    for plugin in layer.plugins:
                        if plugin.get("type") == "ai":
                            instance_id = plugin["instance_id"]
                            result = ai_plugin_result_cache.get((layer.id, instance_id))
                            if result is not None:
                                filename = f"ai_plugin_{instance_id}.exr"
                                FileIO.save_baked_layer(filename, result)
                                src_path = os.path.join(FileIO.BAKED_CACHE_DIR, filename)
                                if os.path.exists(src_path):
                                    dst_path = os.path.join(assets_dir, filename)
                                    shutil.copy2(src_path, dst_path)
                                plugin["ai_result_file"] = filename

                # Write project file with selected serialization format
                if serialization_format == "msgpack":
                    # Use MessagePack binary format
                    try:
                        import msgpack
                        project_file_name = "project.mp"
                        project_file_path = os.path.join(temp_dir, project_file_name)
                        packed_data = msgpack.packb(data, use_bin_type=True)

                        # Apply gzip compression if requested
                        if compress_project:
                            import gzip
                            project_file_name = "project.mp.gz"
                            project_file_path = os.path.join(temp_dir, project_file_name)
                            with gzip.open(project_file_path, 'wb', compresslevel=6) as f:
                                f.write(packed_data)
                        else:
                            with open(project_file_path, 'wb') as f:
                                f.write(packed_data)
                    except ImportError:
                        # Fallback to JSON if msgpack not available
                        logger.warning("msgpack not available, falling back to JSON format")
                        serialization_format = "json"

                if serialization_format == "json":
                    project_file_name = "project.json"
                    project_file_path = os.path.join(temp_dir, project_file_name)

                    # Apply gzip compression if requested
                    if compress_project:
                        import gzip
                        project_file_name = "project.json.gz"
                        project_file_path = os.path.join(temp_dir, project_file_name)
                        with gzip.open(project_file_path, 'wt', encoding='utf-8', compresslevel=6) as f:
                            json.dump(data, f, indent=2, ensure_ascii=False)
                    else:
                        with open(project_file_path, 'w', encoding='utf-8') as f:
                            json.dump(data, f, indent=2, ensure_ascii=False)

                # Ensure parent directory exists
                os.makedirs(os.path.dirname(project_path), exist_ok=True)

                # AUD-36910: build the archive beside the destination and swap
                # it in atomically, so a failed/interrupted save never leaves
                # a truncated .topos behind.
                temp_fd, temporary_zip_path = tempfile.mkstemp(
                    prefix=f".{os.path.basename(project_path)}.",
                    suffix=".tmp",
                    dir=os.path.dirname(os.path.abspath(project_path)),
                )
                os.close(temp_fd)
                try:
                    with zipfile.ZipFile(temporary_zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                        # Add project file (json, msgpack, or compressed)
                        zf.write(project_file_path, project_file_name)

                        # Add all files in assets directory (if any)
                        if os.listdir(assets_dir):
                            for asset_file in os.listdir(assets_dir):
                                asset_path = os.path.join(assets_dir, asset_file)
                                zf.write(asset_path, f"assets/{asset_file}")
                    os.replace(temporary_zip_path, project_path)
                finally:
                    if os.path.exists(temporary_zip_path):
                        os.remove(temporary_zip_path)

            # Invalidate the project thumbnail only if requested (avoid
            # frequent IO on autosave); AUD-36915 后不再写孤儿 JPEG。
            if save_preview:
                self._invalidate_project_thumbnail(image_path)

            return True

        except (OSError, zipfile.BadZipFile, TypeError, ValueError) as e:
            # AUD-36910: log with traceback and report failure instead of the
            # previous blanket `except Exception` that swallowed every error.
            logger.error(f"Failed to save project {project_path}: {e}", exc_info=True)
            return False

    def save_project_to_path(self, output_path: str, engine: Any, image_path: str = "",
                             composite_graph_data: dict = None,
                             composite_viewer_state: dict = None,
                             serialization_format: str = "json",
                             compress_project: bool = False) -> bool:
        """Save current engine state to a custom .topos file path.

        Used by presets to save a project-grade snapshot to an arbitrary location.

        Serialization formats:
        - "json": Human-readable, backward compatible (default)
        - "msgpack": Binary format, 50-70% size reduction for large projects

        Args:
            output_path: Full path for the .topos file to create.
            engine: The image engine containing layer data.
            image_path: Optional original image path (stored as metadata).
            composite_graph_data: Optional serialized composite node graph.
            composite_viewer_state: Optional composite viewer zoom/pan state.
            serialization_format: "json" (default) or "msgpack" for binary format.
            compress_project: Apply gzip compression to project file.

        Returns:
            True if the archive was written (and atomically swapped in),
            False on I/O or serialization failure (AUD-36910).
        """
        if not engine:
            return False

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                assets_dir = os.path.join(temp_dir, "assets")
                os.makedirs(assets_dir, exist_ok=True)

                data = {
                    "version": "2.0",
                    "_format_version": 1 if serialization_format == "json" else 2,
                    "image_path": image_path,
                    "layers": [l.to_dict(assets_dir=assets_dir) for l in engine.layers],
                    "active_layer_index": engine.active_layer_index,
                    "raw_params": getattr(engine, 'current_raw_params', None),
                    "source_bake": engine.get_source_bake_state(),
                    "flip_state": engine.get_flip_state()
                }

                # M5: 视频降噪参数（独立字段，不与图片降噪 source_bake 共用）
                video_denoise = getattr(engine, 'get_video_denoise_state', lambda: None)()
                if video_denoise:
                    data["video_denoise"] = video_denoise

                # Include composite graph data if provided
                if composite_graph_data:
                    project_dir = os.path.dirname(os.path.abspath(output_path))
                    data["composite_graph"] = self._relativize_composite_paths(
                        composite_graph_data, project_dir
                    )

                # Include composite viewer state if provided
                if composite_viewer_state:
                    data["composite_state"] = composite_viewer_state

                # Copy baked EXR files to assets
                for layer in engine.layers:
                    if getattr(layer, 'is_baked', False) and getattr(layer, 'baked_file_name', None):
                        baked_src_path = os.path.join(FileIO.BAKED_CACHE_DIR, layer.baked_file_name)
                        if os.path.exists(baked_src_path):
                            shutil.copy2(baked_src_path, os.path.join(assets_dir, layer.baked_file_name))

                # Source bake
                source_bake_state = engine.get_source_bake_state()
                if source_bake_state and source_bake_state.get("source_bake_file_name"):
                    src_path = os.path.join(FileIO.BAKED_CACHE_DIR, source_bake_state["source_bake_file_name"])
                    if os.path.exists(src_path):
                        shutil.copy2(src_path, os.path.join(assets_dir, source_bake_state["source_bake_file_name"]))

                # AI Plugin Results
                ai_plugin_result_cache = getattr(engine, 'ai_plugin_result_cache', {})
                for layer in engine.layers:
                    for plugin in layer.plugins:
                        if plugin.get("type") == "ai":
                            instance_id = plugin["instance_id"]
                            result = ai_plugin_result_cache.get((layer.id, instance_id))
                            if result is not None:
                                filename = f"ai_plugin_{instance_id}.exr"
                                FileIO.save_baked_layer(filename, result)
                                src_path = os.path.join(FileIO.BAKED_CACHE_DIR, filename)
                                if os.path.exists(src_path):
                                    shutil.copy2(src_path, os.path.join(assets_dir, filename))
                                plugin["ai_result_file"] = filename

                # Write project file with selected serialization format
                if serialization_format == "msgpack":
                    try:
                        import msgpack
                        project_file_name = "project.mp"
                        project_file_path = os.path.join(temp_dir, project_file_name)
                        packed_data = msgpack.packb(data, use_bin_type=True)

                        if compress_project:
                            import gzip
                            project_file_name = "project.mp.gz"
                            project_file_path = os.path.join(temp_dir, project_file_name)
                            with gzip.open(project_file_path, 'wb', compresslevel=6) as f:
                                f.write(packed_data)
                        else:
                            with open(project_file_path, 'wb') as f:
                                f.write(packed_data)
                    except ImportError:
                        logger.warning("msgpack not available, falling back to JSON format")
                        serialization_format = "json"

                if serialization_format == "json":
                    project_file_name = "project.json"
                    project_file_path = os.path.join(temp_dir, project_file_name)

                    if compress_project:
                        import gzip
                        project_file_name = "project.json.gz"
                        project_file_path = os.path.join(temp_dir, project_file_name)
                        with gzip.open(project_file_path, 'wt', encoding='utf-8', compresslevel=6) as f:
                            json.dump(data, f, indent=2, ensure_ascii=False)
                    else:
                        with open(project_file_path, 'w', encoding='utf-8') as f:
                            json.dump(data, f, indent=2, ensure_ascii=False)

                os.makedirs(os.path.dirname(output_path), exist_ok=True)

                # AUD-36910: atomic write — build beside the destination,
                # then os.replace so the previous snapshot survives a
                # failed/interrupted save.
                temp_fd, temporary_zip_path = tempfile.mkstemp(
                    prefix=f".{os.path.basename(output_path)}.",
                    suffix=".tmp",
                    dir=os.path.dirname(os.path.abspath(output_path)),
                )
                os.close(temp_fd)
                try:
                    with zipfile.ZipFile(temporary_zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                        zf.write(project_file_path, project_file_name)
                        if os.listdir(assets_dir):
                            for asset_file in os.listdir(assets_dir):
                                zf.write(os.path.join(assets_dir, asset_file), f"assets/{asset_file}")
                    os.replace(temporary_zip_path, output_path)
                finally:
                    if os.path.exists(temporary_zip_path):
                        os.remove(temporary_zip_path)

            return True

        except (OSError, zipfile.BadZipFile, TypeError, ValueError) as e:
            # AUD-36910: log with traceback and report failure instead of the
            # previous blanket `except Exception` that swallowed every error.
            logger.error(f"Failed to save project to path {output_path}: {e}", exc_info=True)
            return False

    def schedule_autosave(self, image_path: str, engine: Any) -> None:
        """Schedule a debounced save on the thread that owns *engine*.

        AUD-36911: the save snapshot walks ``engine.layers`` and mutates
        plugin dicts. Running it on a ``threading.Timer`` background thread
        raced the UI/render threads. When a Qt event loop is available on
        the main thread (the production path), the debounce now uses a
        single-shot QTimer so the snapshot executes on the UI thread.
        Headless hosts without a QCoreApplication fall back to the legacy
        background timer.
        """
        self.current_image_path = image_path
        self.engine_ref = engine

        qtimer = self._main_thread_autosave_timer()
        if qtimer is not None:
            if self.autosave_timer:
                # Cancel any pending legacy fallback timer.
                self.autosave_timer.cancel()
                self.autosave_timer = None
            qtimer.start(int(self.AUTOSAVE_DEBOUNCE_SECONDS * 1000))
            return

        if self.autosave_timer:
            self.autosave_timer.cancel()

        # Fallback for non-Qt hosts only: this executes the snapshot off the
        # owning thread, so it must not be used when a Qt loop exists.
        self.autosave_timer = threading.Timer(
            self.AUTOSAVE_DEBOUNCE_SECONDS, self._perform_autosave
        )
        self.autosave_timer.daemon = True
        self.autosave_timer.start()

    def _main_thread_autosave_timer(self) -> Any:
        """Return a single-shot QTimer owned by the main thread, or None.

        Returns None when called off the main thread, when PyQt6 is
        unavailable, or when no QCoreApplication instance exists (headless
        tests, CLI tools).
        """
        if threading.current_thread() is not threading.main_thread():
            return None
        try:
            from PyQt6.QtCore import QCoreApplication, QTimer
        except ImportError:
            return None
        if QCoreApplication.instance() is None:
            return None
        if self._autosave_qtimer is None:
            self._autosave_qtimer = QTimer()
            self._autosave_qtimer.setSingleShot(True)
            self._autosave_qtimer.timeout.connect(self._perform_autosave)
        return self._autosave_qtimer

    def _perform_autosave(self) -> None:
        if self.current_image_path and self.engine_ref:
            # Retrieve composite data via callbacks
            composite_data = None
            if self.composite_data_callback:
                composite_data = self.composite_data_callback()
            viewer_state = None
            if self.composite_viewer_state_callback:
                viewer_state = self.composite_viewer_state_callback()
            # Autosave only saves the project data, not the preview thumbnail
            self.save_project(self.current_image_path, self.engine_ref, save_preview=False,
                              composite_graph_data=composite_data,
                              composite_viewer_state=viewer_state)

    def backup_project(self, image_path: str, engine, backup_dir: str = "", max_backups: int = 5) -> None:
        """Create a timestamped backup copy of the project file.

        Args:
            image_path: Path to the current image file.
            engine: The image engine containing layer data.
            backup_dir: Custom backup directory (defaults to .backups next to image).
            max_backups: Maximum number of backups to keep (oldest deleted first).
        """
        import datetime
        import glob
        import shutil

        if not image_path or not engine:
            return

        project_path = self.get_project_path(image_path)
        if not os.path.exists(project_path):
            # No project file yet — save first, then backup
            self.save_project(image_path, engine, save_preview=False)

        if not os.path.exists(project_path):
            return

        # Resolve backup directory
        if not backup_dir:
            backup_dir = os.path.join(os.path.dirname(image_path), ".backups")

        os.makedirs(backup_dir, exist_ok=True)

        # Create timestamped backup filename
        basename = os.path.splitext(os.path.basename(image_path))[0]
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = f"{basename}_{timestamp}.topos"
        backup_path = os.path.join(backup_dir, backup_filename)

        # Copy current project to backup
        try:
            shutil.copy2(project_path, backup_path)
        except Exception as e:
            logger.error(f"Failed to create backup: {e}")
            return

        # Rotate: remove oldest backups exceeding max_backups
        pattern = os.path.join(backup_dir, f"{basename}_*.topos")
        backups = sorted(glob.glob(pattern))

        while len(backups) > max_backups:
            oldest = backups.pop(0)
            try:
                os.remove(oldest)
            except OSError:
                break

    # ── Composite path helpers ──

    @staticmethod
    def _relativize_composite_paths(graph_data: dict, project_dir: str) -> dict:
        """Convert absolute image paths in ImageInputNodes to project-relative paths."""
        if not graph_data or not project_dir:
            return graph_data
        for node_data in graph_data.get("nodes", []):
            if node_data.get("type") == "image_input":
                params = node_data.get("parameters", {})
                fp = params.get("file_path", "")
                if fp and os.path.isabs(fp):
                    try:
                        params["file_path"] = os.path.relpath(fp, project_dir)
                    except ValueError:
                        pass  # Different drives on Windows, keep absolute
        return graph_data

    @staticmethod
    def _absolutize_composite_paths(graph_data: dict, project_dir: str) -> dict:
        """Convert relative image paths in ImageInputNodes back to absolute paths."""
        if not graph_data or not project_dir:
            return graph_data
        for node_data in graph_data.get("nodes", []):
            if node_data.get("type") == "image_input":
                params = node_data.get("parameters", {})
                fp = params.get("file_path", "")
                if fp and not os.path.isabs(fp):
                    abs_path = os.path.normpath(os.path.join(project_dir, fp))
                    if os.path.exists(abs_path):
                        params["file_path"] = abs_path
        return graph_data
