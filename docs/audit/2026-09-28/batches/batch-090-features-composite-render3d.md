# batch-090 审查报告：src/features/composite/render3d//

- 批次：batch-090 [P1]
- 编号区间：AUD-9000–AUD-9099（实际使用 AUD-9000–AUD-9018）
- 审查日期：2026-09-28
- 文件数：5（2,826 行），全部通读；关联模块（primitives.py / scene_graph.py / skeleton.py / model3d_node.py / 相关测试）已只读核对
- 总计：Critical 0 / Major 4 / Suggestion 15

---

## src/features/composite/render3d/model_import_cache.py (行数: 390)

### 发现

- [AUD-9000] [Suggestion] `get()` 对 manifest 标量字段强转 int/float 并直接索引 `data["digest"]`，畸形 JSON 值会抛未捕获异常
  - 位置: src/features/composite/render3d/model_import_cache.py:243-264
  - 证据:
    ```python
    if int(data.get("cache_version", 0)) != IMPORT_CACHE_VERSION:
    ...
    entry = ImportCacheEntry(
        digest=data["digest"],
        ...
        created_at=float(data.get("created_at", 0.0)),
    ```
  - 影响: try/except 只包住 `json.load`（240 行）。合法 JSON 但字段类型异常（如 `"cache_version": "x"` → ValueError，缺 `digest` 键 → KeyError）会从 `get()` 逃逸。违反「外部数据须验证」铁律。当前唯一调用方 model_loader.py:949-963 用 `except Exception` 兜住，实际影响为缓存静默失效，但模块自身契约不完整。
  - 建议: 将字段解析放入 try/except (KeyError, TypeError, ValueError)，失败返回 None（视为未命中）。
  - 置信度: 高（代码路径确定，实际影响低）

- [AUD-9001] [Suggestion] 缓存目录无容量上限与淘汰策略，GLB 派生文件只增不减
  - 位置: src/features/composite/render3d/model_import_cache.py:272-306（put 无任何尺寸/条目数检查）
  - 证据:
    ```python
    if fallback_glb and os.path.isfile(fallback_glb):
        rel = self._ingest_file(fallback_glb, key.digest(), ".glb")
        entry.fallback_glb = rel
    # manifest 落盘（atomic：写临时文件再 rename）
    self._write_manifest(entry)
    ```
  - 影响: 每个 FBX 的 fallback GLB 常为数十至上百 MB，源文件每次重存（mtime_ns 变化 → 新 digest）都会追加新条目且旧条目不会被清理（只有 `get()` 命中同一 key 才 purge）。长期使用后缓存目录无限膨胀，仅能靠用户手动 `clear()`。
  - 建议: 在 put 后统计目录总大小/条目数，超阈值按 created_at LRU 淘汰；或提供定期清理钩子。
  - 置信度: 高

- [AUD-9002] [Suggestion] `_assimp_version_tag()` 每次构建 cache key 都同步跑一次 subprocess，无 memoization
  - 位置: src/features/composite/render3d/model_import_cache.py:50-63、137
  - 证据:
    ```python
    bin_path = shutil.which("assimp")
    if bin_path:
        proc = subprocess.run(
            [bin_path, "version"], check=False, capture_output=True,
            text=True, timeout=5)
    ...
    assimp_tag=assimp_tag or _assimp_version_tag(),
    ```
  - 影响: 每次 FBX fallback 加载 `build_cache_key` 都 spawn 进程（约几十 ms，慢盘更久），纯浪费——assimp 二进制版本在进程生命周期内不变；且 timeout=5 意味着最坏每次 key 构建多 5s。
  - 建议: 模块级缓存结果（如 `functools.lru_cache` 或带锁的单次探测全局变量），探测失败值同样缓存。
  - 置信度: 高

- [AUD-9003] [Suggestion] `_ingest_file` 用 `shutil.copyfile` 直写目标文件名，非原子；跨进程并发可读到半写的 GLB
  - 位置: src/features/composite/render3d/model_import_cache.py:308-318
  - 证据:
    ```python
    rel = f"{digest}{ext}"
    dst = self.file_path(rel)
    try:
        import shutil
        shutil.copyfile(src_abs, dst)
    ```
  - 影响: manifest 写入是原子的，但 GLB 本体不是。两个进程同时首次转换同一 FBX 时，A 进程 put 期间 B 进程 `get()` 命中 manifest（若 A 已写完 manifest）并 `os.path.isfile` 检查通过，读到部分写入的 GLB → trimesh 解析失败 → 本次导入报错（用户重试即好）。窗口小、仅多进程场景。
  - 建议: 复制到 `dst + ".tmp"` 后 `os.replace` 原子落位，与 manifest 写入策略保持一致。
  - 置信度: 中（机制确定，触发条件罕见）

### 通过项
- 线程安全：manifest 原子写（tmp+rename）、按 digest 命名避免多节点冲突，docstring 声明的边界成立
- GPU 资源生命周期：纯磁盘缓存模块，无 GL 对象（通过）
- GLSL/HDR 正确性：不适用（无 shader/像素数据）
- 信号防递归：不适用（无 Qt）
- 状态提交一致性：key 含 cache_version + mtime_ns + size + 导入参数，源文件覆盖后可靠失效；不用 pickle（安全）
- 异常与取消路径：所有 OSError 均降级为 warning + 返回值兜底，不阻断加载
- 输入验证：`build_cache_key` 对源文件存在性显式检查并抛 ValueError（除 AUD-9000 的 manifest 字段）
- 类型注解：完整（Optional/dataclass 字段均有注解）

---

## src/features/composite/render3d/model_import_report.py (行数: 152)

### 发现

- [AUD-9004] [Suggestion] `from_summary_dict` 对 warnings/lost_features 类型未验证：标量触发未捕获 TypeError，字符串被逐字符拆分
  - 位置: src/features/composite/render3d/model_import_report.py:142-148
  - 证据:
    ```python
    values = {key: data[key] for key in scalar_fields if key in data}
    values["warnings"] = list(data.get("warnings") or [])
    values["lost_features"] = list(data.get("lost_features") or [])
    try:
        return cls(**values)
    except (TypeError, ValueError):
        return None
    ```
  - 影响: `list()` 在 143-144 行、try 块之外。payload 若经手工构造/日志回放/版本混用出现 `"warnings": 5` → `list(5)` 抛 TypeError 直接逃逸（调用方 import_report_panel.py:416 无兜底）；`"warnings": "abc"` → `['a','b','c']` 渲染成三条假警告。另外标量字段不校验类型，dataclass 不做运行时检查，`mesh_count="x"` 会静默通过。docstring 声称「render-thread-safe summary payload」的重建边界应做验证。
  - 建议: 143-144 行移入 try 或先做 `isinstance(x, list)` 检查；标量字段逐个校验类型不匹配则丢弃/置默认。
  - 置信度: 高（路径确定）；正常管线内 `summary_dict()` 产出总是良构，实际触发需异常 payload

### 通过项
- 线程安全：纯不可变数据组装，无共享可变状态（跨线程只传 dict 的设计明确）
- GPU 资源生命周期：不适用（无 GL）
- GLSL/HDR 正确性：不适用
- 信号防递归：不适用（无 Qt）
- 状态提交一致性：`add_warning`/`add_lost_feature` 去重保序，`summary_dict`/`encode` 路径输出稳定
- 异常与取消路径：无 I/O，无异常面
- 输入验证：除 AUD-9004 外，`from_summary_dict` 对非 dict 输入返回 None、未知字段忽略（前向兼容）
- 类型注解：完整

---

## src/features/composite/render3d/model_import_service.py (行数: 426)

### 发现

- [AUD-9005] [Major] 在途导入未完成时应用退出会被非 daemon worker 线程阻塞（最长约 120s+）
  - 位置: src/features/composite/render3d/model_import_service.py:279-283、326-333；佐证 model_loader.py:987-994
  - 证据:
    ```python
    self._executor = ThreadPoolExecutor(
        max_workers=max(1, int(max_workers)),
        thread_name_prefix="topos-model-import",
    )
    ...
    def shutdown(self, wait: bool = False) -> None:
        ...
        self.cancel_all()
        self._executor.shutdown(wait=wait, cancel_futures=True)
    ```
    worker 内解析含 `subprocess.run([assimp_bin, "export", ...], timeout=120)`（model_loader.py:987-994）。
  - 影响: Python≥3.9 的 ThreadPoolExecutor worker 线程为非 daemon，解释器退出时由 threading atexit 逐个 join。`shutdown(wait=False)` + 合作式取消对「已在跑的第三方解析/assimp CLI 转换」无效，FBX fallback 转换最长 120s 才超时返回 → 用户退出应用时进程挂起最长约两分钟。
  - 建议: 退出路径上在 cancel 后做有界 `shutdown(wait=True)` 配合超时提示；或把 assimp CLI 子进程句柄登记到服务层，shutdown 时按 PIDs 杀进程；至少在 UI 层告知「后台导入进行中」。
  - 置信度: 中（线程 join 机制确定；实际是否可感知取决于应用退出路径是否有 os._exit 等强制手段，本批次文件内无证据）

### 通过项
- 线程安全：`_ProgressState` 带锁、`submit`/`active_handles` 全程持 `self._lock`、完成回调 `_forget` 释放大对象引用、`result()` 缓存 + 双检模式正确；`_shared_service` 单例有锁
- GPU 资源生命周期：worker 只做 CPU/numpy/IO，GL 上传留给 render thread，边界声明与实现一致（378 行注释成立）
- GLSL/HDR 正确性：不适用
- 信号防递归：不依赖 Qt（Service 无 PyQt import），跨边界只传 dict/字符串
- 状态提交一致性：不适用（无 undo/持久化职责）
- 异常与取消路径：排队中直接 cancel Future、运行中 token 合作式取消且交接前复查丢弃 root（388-397）；`_run` 的 `except Exception` 是显式结果化边界（转 IMPORT_FAILED），不吞错；ClosedService 二次 submit 抛 RuntimeError
- 输入验证：`from_path` 规范化绝对路径 + budget 非负钳制；`submit` 校验 request 类型；`add_done_callback` 校验 callable
- 性能模式：probe→parse 阶段化进度、句柄不驻留完成态（防 SceneGraphRoot 泄漏）、资源预估为无读取启发式且仅显式预算才阻断——均有 docstring 说明
- 类型注解：完整

---

## src/features/composite/render3d/model_loader.py (行数: 1741)

### 发现

- [AUD-9006] [Major] `_convert_trimesh_scene` 把 `transforms.local`（相对父节点）当「相对根」用，层级 glTF 场景的祖先变换丢失/错位
  - 位置: src/features/composite/render3d/model_loader.py:309-334
  - 证据:
    ```python
    local = np.eye(4, dtype=np.float32)
    try:
        # transforms.local 返回 dict {node: local matrix}
        local = np.array(transforms.local.get(geom_name, np.eye(4)),
                         dtype=np.float32).reshape(4, 4)
    ...
    # 注：trimesh 的 local 是相对 scene 根，所以挂 root 下即正确。
    ```
  - 影响: trimesh 的 `transforms.local` 是每节点相对其父的矩阵（世界矩阵应取 `node_to_matrix`）。此处把所有 mesh 平铺挂 root 却赋 local：对有层级变换的 glTF（分组/骨架节点带 TRS 的 DCC 导出极常见），祖先链变换被丢弃（或几何帧 local 恒为 identity 时全部变换丢失）→ 部件错位、模型散架。注释「local 是相对 scene 根」与 trimesh 语义不符；只有扁平导出（local==world）才正确。
  - 建议: 平铺结构应改用 `transforms.node_to_matrix[geom_name]`（世界矩阵）作为挂 root 的 local_transform；或在构建时沿 graph 边累乘到根。
  - 置信度: 中（代码事实确定；trimesh 各版本对几何帧 local 的具体取值可能为 identity 或节点 TRS，两种取值都导致层级场景错位，但简单导出不受影响，未见单测覆盖层级场景）

- [AUD-9007] [Major] `_restore_gltf_animation_and_skin` 按「节点名==trimesh 几何名」匹配重建树，未匹配时 mesh 被静默丢弃，FBX CLI fallback 可能渲染为空
  - 位置: src/features/composite/render3d/model_loader.py:1287-1339
  - 证据:
    ```python
    def _collect(node: sg.SceneNode) -> None:
        if node.mesh is not None and node.name:
            source_nodes.setdefault(node.name, node)
    ...
    source = source_nodes.get(node_name)
    ...
    return sg.SceneNode(
        name=node_name,
        mesh=source.mesh if source is not None else None,
    ```
  - 影响: `source_nodes` 的键来自 `_convert_trimesh_scene` 的 `scene.geometry` 键名（trimesh 常为 "geometry_N" 或 glTF mesh 名），而 `_build_node` 查询用的是 glTF document 的 node 名（assimp 导出为 FBX 节点名）。两套命名体系不一致时 `source=None` → 重建后的 `root.root` 全部 mesh=None——trimesh 已经成功加载的几何在重建中被整体丢弃，bbox 归零、模型不显示，且无任何 warning/lost_feature 记录。重名节点还会 `setdefault` 首个命中导致 mesh 错配。tests/composite/test_model_import_report.py 只测 accessor 解码，未覆盖此重建路径。
  - 建议: 匹配失败时保留原 trimesh 树（只补充 animations/skins，不重建 root.root）；或按 mesh 名→glTF mesh 名建立映射而非 node 名，并在零命中时记 report.add_lost_feature。
  - 置信度: 中（机制由代码直接可见；是否在常见 assimp 导出中实际触发取决于两套命名重合度，未能运行验证）

- [AUD-9008] [Major] `_flip_scene_uv_v` 用宽泛 `except Exception: pass` 掩盖 UV 翻转失败，违反 .clinerules「不得宽泛 except 掩盖领域错误」
  - 位置: src/features/composite/render3d/model_loader.py:206-217
  - 证据:
    ```python
    try:
        uvs = np.array(node.mesh.uvs, dtype=np.float32, copy=True)
        if uvs.ndim == 2 and uvs.shape[1] >= 2:
            uvs[:, 1] = 1.0 - uvs[:, 1]
            node.mesh.uvs = np.ascontiguousarray(uvs, dtype=np.float32)
    except Exception:
        pass
    ```
  - 影响: flip_uv_v 是用户显式传入的导入参数，用于修正贴图方向。任何异常（形状异常、内存错误等）被静默吞掉后模型仍「成功」导入，贴图上下颠倒且无任何 report warning，用户无从诊断。
  - 建议: except 改为记录 `logger.warning` 并累计计数，结束后写入 root.import_report.add_warning（loader 末尾有 report 兜底钩子可用）；或收窄为 (TypeError, ValueError)。
  - 置信度: 高

- [AUD-9009] [Suggestion] `_load_assimp` 用 `"load" in msg` 启发式判定 libassimp 缺失，常见解析错误会被误报为「未安装 libassimp」
  - 位置: src/features/composite/render3d/model_loader.py:917-921
  - 证据:
    ```python
    msg = str(exc).lower()
    if "assimp" in msg or "library" in msg or "load" in msg:
        raise ModelLoaderUnavailable(
            "libassimp 未安装或损坏。FBX 需要: brew install assimp（macOS）") from exc
    ```
  - 影响: "load" 出现在绝大多数 pyassimp/文件错误消息里（如 "failed to load file/texture"），损坏 FBX 的真实报错会被替换成误导性的安装指引，掩盖原始异常语义。
  - 建议: 匹配更精确的短语（如 "cannot open shared object"/"DLL"/"dlopen"/"library not found"），或直接保留原始异常消息拼上 backend 说明。
  - 置信度: 高

- [AUD-9010] [Suggestion] `_restore_gltf_animation_and_skin` 的 except 元组不含 RecursionError，深层/环形 children 会让整个导入失败而非降级
  - 位置: src/features/composite/render3d/model_loader.py:1302-1330、1393-1395
  - 证据:
    ```python
    children=[_build_node(child) for child in node_data.get("children", [])],
    ...
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        logger.warning("无法从 assimp GLB 恢复动画/蒙皮: %s", exc)
        return False, False
    ```
  - 影响: 该函数定位是 best-effort 恢复（trimesh 几何已加载成功），但超过递归深度或含环形 children 引用的畸形 GLB 会抛 RecursionError（RuntimeError 子类，不在元组内）逃逸，经 load_model 包装成整体 ModelLoaderUnavailable——几何本可见的模型加载失败。内部 `_place` 有 `except Exception` 兜底，唯独主递归没有。
  - 建议: 把 RecursionError 加入捕获（或整段 `except Exception` 后仍返回 False, False 并 warning），保证恢复器失败只损失动画/蒙皮。
  - 置信度: 中（需要畸形文件触发；逃逸路径确定）

- [AUD-9011] [Suggestion] `_extract_trimesh_skeletons` 拓扑排序 O(n²)：每节点 `unique_names.index` + 每次递归重建全集 set
  - 位置: src/features/composite/render3d/model_loader.py:848-861
  - 证据:
    ```python
    if pn is not None and pn not in placed and pn in {unique_names[i] for i in range(len(unique_names))}:
        _place(pn)  # 先放 parent
    ...
    for nm in ordered_names:
        raw_idx = unique_names.index(nm)  # 在原始 joints 数组的位置
    ```
  - 影响: 大骨架（数百骨骼）时 set 重建 + list.index 累计 O(n²)（500 骨骼约 25 万次比较），纯 Python 循环放大常数；FBX 角色骨架常见此量级，拖慢导入。
  - 建议: 先建 `unique_set = set(unique_names)` 与 `name→raw_idx` dict 再递归；`ordered_names` 循环用 dict 查找替代 index。
  - 置信度: 高

- [AUD-9012] [Suggestion] `_resample_gltf_track` 逐行 Python 循环做线性插值，长动画导入慢
  - 位置: src/features/composite/render3d/model_loader.py:1157-1170
  - 证据:
    ```python
    output = np.empty((sample_times.size, values.shape[1]), dtype=np.float32)
    for row, sample_time in enumerate(sample_times):
        if sample_time <= times[0]:
            output[row] = values[0]
            continue
        ...
        output[row] = values[left] * (1.0 - alpha) + values[right] * alpha
    ```
  - 影响: 每个 channel 每帧一次 Python 迭代；分钟级、数千关键帧 × 数十节点的动画（assimp 导出的 GLB 常见）在导入阶段产生可感知卡顿（worker 线程上，表现为进度 15% 长时间停留）。
  - 建议: 用 `np.searchsorted(times, sample_times)` 向量化 + `np.interp`（按列）或 Fancy-index 批量 lerp，两端 clamp 用布尔掩码处理。
  - 置信度: 高（复杂度事实；量级取决于动画规模）

- [AUD-9013] [Suggestion] `_normalize_color_factor` 对全 NaN 数组返回 NaN 颜色，未做有限性兜底
  - 位置: src/features/composite/render3d/model_loader.py:529-537
  - 证据:
    ```python
    arr = np.asarray(value, dtype=np.float32).reshape(-1)
    ...
    if float(np.nanmax(arr)) > 1.0:
        arr = arr / 255.0
    return np.clip(arr, 0.0, 1.0).astype(np.float32)
    ```
  - 影响: `np.nanmax` 忽略 NaN，若数组全为 NaN（损坏材质）则比较为 False，clip(NaN)=NaN 直接进入 MaterialData.base_color → shader 采样结果未定义（通常渲染黑块/闪烁）且难定位。`np.nanmax` 对全 NaN 还发 RuntimeWarning。
  - 建议: 返回前加 `arr = np.nan_to_num(arr, nan=0.8)` 或检测 `np.isfinite(arr).all()` 不满足时回退默认灰。
  - 置信度: 中（需要损坏输入；传播路径确定）

- [AUD-9014] [Suggestion] `_load_gltf_document_and_buffers` 对 buffer uri 不做路径规范化，绝对路径/`..` 可逃逸模型目录读任意本地文件
  - 位置: src/features/composite/render3d/model_loader.py:1065-1077
  - 证据:
    ```python
    uri = buffer_info.get("uri")
    ...
    else:
        with open(os.path.join(base_dir, uri), "rb") as handle:
            buffers.append(handle.read())
    ```
  - 影响: `os.path.join` 遇到以 `/` 开头的 uri 会丢弃 base_dir，`../../` 也可上溯；恶意/损坏 GLB 可让进程把任意本地文件字节读入顶点缓冲（本地单机应用，危害有限，但违反「外部数据须验证」）。
  - 建议: 解析后 `os.path.realpath` 校验仍以 base_dir 为前缀，否则抛 ValueError（已被上层捕获降级）。
  - 置信度: 高（行为确定）；实际风险低

- [AUD-9015] [Suggestion] 材质转换失败时宽泛 `except Exception: return None` 整体静默丢材质，与 docstring「失败返回标量 fallback」不符
  - 位置: src/features/composite/render3d/model_loader.py:525-526、1718-1719
  - 证据:
    ```python
    # _convert_trimesh_material 尾部：
    except Exception:
        return None
    # _convert_assimp_material 尾部同型
    ```
  - 影响: 材质任一字段转换抛错（如 props["diffuse"] 是标量导致切片失败）即丢掉整个材质（含已成功提取的纹理路径），渲染回退默认材质且 report 无记录；docstring 声称的标量 fallback 并未发生。与 AUD-9008 同属宽泛 except 掩盖领域错误，因处于「尽力提取」辅助路径降为 Suggestion。
  - 建议: 失败时返回默认标量 MaterialData 并 add_warning 到 report（可通过返回 (material, warn) 或挂载点实现），至少记 logger.warning。
  - 置信度: 高

- [AUD-9016] [Suggestion] 第三方对象参数普遍缺类型注解（Suggestion 级汇总）
  - 位置: src/features/composite/render3d/model_loader.py:288（`_convert_trimesh_scene(scene, ...)`）、358（`geom`）、457（`visual`）、588（`value`）、597（`image`）、708（`geom, scene`）、740（`scene`）、1431/1528（`scene/assimp_node`）、1573（`mesh_ref`）、1588/1620（`m`）、1695（`mat`）
  - 证据:
    ```python
    def _convert_trimesh_scene(scene, source_path: str = "") -> sg.SceneGraphRoot:
    def _convert_trimesh_geometry(geom) -> Optional[MeshData]:
    def _convert_assimp_node(assimp_node, meshes, materials) -> sg.SceneNode:
    ```
  - 影响: 阅读与静态检查收益降低；本项目其余模块类型注解纪律较好，此处因「第三方类型不定」而全部裸参。`_extract_trimesh_skeletons` 返回 `List["SkeletonData"]` 字符串注解在类型检查器下不可解析。
  - 建议: 至少统一 `Any` + docstring 说明期望的鸭子类型（vertices/faces/metadata…），返回类型保持精确。
  - 置信度: 高

### 通过项
- 线程安全：`_tls`（threading.local）skip 标志 set/reset 配 finally 保证异常路径复位（970-974）；模块无可变全局（除只读常量与惰性单例）；解析跑在 service worker、无 Qt/GL 调用
- GPU 资源生命周期：整个 loader 无 ModernGL/OpenGL 引用，纹理只落盘为 PNG 路径（TextureDescriptor 不持 GL 对象），契约正确
- GLSL/HDR 正确性：不适用（无 shader 代码）；`_normalize_color_factor` 的 clip(0,1) 是反照率颜色而非 HDR 像素数据，不违反 no-clamp 铁律
- 信号防递归：不适用（无 Qt）
- 状态提交一致性：FBX 缓存 key 将 normalize/flip_uv_v 排除在 key 外的理由成立（两者是 SceneGraph 层后处理，load_model 112-115 在缓存命中后统一应用）；morph 增量在 MeshData.__post_init__ 强校验形状
- 异常与取消路径：所有 backend 失败统一包成 ModelLoaderUnavailable 供节点层降级；CLI 转换有 returncode/文件存在双重检查与 timeout；缓存读写失败均不阻断主加载路径
- 输入验证：GLB 头长度/版本/chunk 截断显式校验（1039-1059）；accessor 形状不匹配记 warning 丢弃（1269-1273）；`_assimp_faces_to_indices` 对 n-gon 做扇形三角化且容错多种容器；JOINTS/WEIGHTS 与顶点数强校验
- 性能模式：切线 lazy 化（F5 注释与实现一致）、贴图提取按 digest 缓存命中跳过 PNG 重编码（P4-3 闭环成立）、FBX GLB 跨进程缓存（P4-2）避免重复 120s CLI 转换

---

## src/features/composite/render3d/morph.py (行数: 117)

### 发现

- [AUD-9017] [Suggestion] `MorphError` 定义后从未 raise/使用，属死代码导出
  - 位置: src/features/composite/render3d/morph.py:18-20、112-117
  - 证据:
    ```python
    class MorphError(ValueError):
        """Raised when morph data cannot be represented safely."""
    ...
    __all__ = [
        "MorphError",
        ...
    ]
    ```
  - 影响: docstring 声称「cannot be represented safely」时抛出，但全库（src + tests）无任何 raise/catch（decode 对坏输入返回 diagnostics 而非异常）。误导调用方写无谓的 except MorphError 防御。
  - 建议: 删除该类及 __all__ 条目，或在 decode 遇到不可表示输入时真正抛出并更新 docstring。
  - 置信度: 高（全库 grep 确认无使用）

- [AUD-9018] [Suggestion] `apply_morph_targets` 对传入权重不做有限性验证，NaN/Inf 权重会生成 NaN 几何直接进 VBO
  - 位置: src/features/composite/render3d/morph.py:74-78、84-87
  - 证据:
    ```python
    active = [
        (str(name), float(weight))
        for name, weight in weights.items()
        if str(name) in targets and float(weight) != 0.0
    ]
    ...
    vertices += np.asarray(targets[name], dtype=np.float32) * weight
    ```
  - 影响: `float("nan") != 0.0` 为 True，NaN/Inf 权重会照常参与累加 → 顶点全 NaN → 渲染黑块/消失。当前唯一调用方 model3d_node.py:534-536 构造签名时同样只 `float(value)` 无 isfinite 过滤；若上游参数未先过 `decode_morph_weights`（它会过滤非有限值）则防线缺口存在。公开 API 自身不设防与 decode 的严格性不一致。
  - 建议: 在 active 过滤中加 `math.isfinite(weight)` 条件（与 decode_morph_weights 对齐），非法值计入 diagnostics 或静默跳过。
  - 置信度: 中（防线缺口确定；是否可达取决于上游是否总先 decode，未在本批次文件中完全确认）

### 通过项
- 线程安全：纯函数、无共享状态，MeshData 新建而非原地改（源 mesh 不被污染，tests/composite/test_morph_targets.py 有对应断言）
- GPU 资源生命周期：不适用（无 GL；产出纯 numpy MeshData 交 render 线程）
- GLSL/HDR 正确性：不适用；法线重归一化用 `np.maximum(lengths, 1e-8)` + where 掩码，零向量安全
- 信号防递归：不适用（无 Qt）
- 状态提交一致性：`encode_morph_weights` 经 decode 规范化（键排序、JSON 紧凑分隔符），持久化输出确定性；morph→skin 顺序与 glTF 变形顺序一致（docstring 声明正确，蒙皮属性透传）
- 异常与取消路径：decode 对非法 JSON/类型/非有限值全部返回诊断码而非抛异常，容错路径完整
- 输入验证：decode 覆盖 str/None/Mapping/逐项 weight 的类型与有限性（除 AUD-9018 的 apply 侧）
- 性能模式：numpy 向量化累加 + 单次法线归一化，非激活权重短路返回原 mesh
- 类型注解：完整（含 `tuple[dict[str, float], tuple[str, ...]]` 现代注法）

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 4 | AUD-9005, AUD-9006, AUD-9007, AUD-9008 |
| Suggestion | 15 | AUD-9000–9004, AUD-9009–9018 |

Critical 为零的说明：本批次为纯 CPU 导入/数据结构层，无 GLSL、无 Qt 控件、无持久化状态提交；两个最高风险项（AUD-9006/9007 的几何错位与丢失）因触发条件依赖 trimesh/assimp 命名与层级行为且未能运行验证，按「宁缺毋滥」原则定为 Major 并标注中置信度。
