# batch-205 审查报告 — src/features/edit/ui

- 批次: batch-205 [P1]
- 编号区间: AUD-20500–AUD-20599
- 文件数: 2（合计 1,216 行）
- 审查日期: 2026-09-28
- 结论概要: Critical 1 / Major 2 / Suggestion 12。核心问题为 RelinkMediaDialog 全部重链接操作因 Qt 枚举误用抛 TypeError 而不可用；QuickProjectDialog 对图片素材的 Auto 分辨率恒为 1920x1080（元数据键不匹配），且预设文本在 import 期冻结导致切换语言后默认分辨率错误。

---

## src/features/edit/ui/quick_project_dialog.py (行数: 662)

### 发现

- [AUD-20500] [Major] 图片素材的 Auto 分辨率恒为 1920x1080：FileIO.get_metadata 从不返回 "width"/"height" 键
  - 位置: src/features/edit/ui/quick_project_dialog.py:166-172（交叉证据 src/shared/io/file_io.py:511-516）
  - 证据:
    ```python
    from src.shared.io.file_io import FileIO
    file_meta = FileIO.get_metadata(path)

    if file_meta:
        metadata["width"] = file_meta.get("width", 1920)
        metadata["height"] = file_meta.get("height", 1080)
    ```
    而 FileIO.get_metadata 实际构造的键为 "Filename"/"Size"/"Format"/"Resolution"（"Resolution" 是 `"6000 x 4000"` 这样的字符串），全文件无 "width"/"height" 键（已 grep 验证无匹配）。
  - 影响: 对任意尺寸图片，探测结果永远回退默认 1920x1080：媒体信息区显示错误分辨率，Auto 预设把宽高自旋框填成 1920x1080，图片 Quick Project 的 timeline 以错误分辨率创建。
  - 建议: 解析 get_metadata 返回的 "Resolution" 字符串，或改用 PIL/专用探测接口直接取像素宽高；增加对探测结果键缺失的显式告警。
  - 置信度: 高

- [AUD-20501] [Major] 类级预设文本在 import 期冻结，运行时切换语言后 Auto 预设失配，默认分辨率落到 4K DCI
  - 位置: src/features/edit/ui/quick_project_dialog.py:61-83, 390-397, 527-536
  - 证据:
    ```python
    RESOLUTION_PRESETS = {
        ...
        I18N.tr("project.auto_from_media", "Auto (from Media)"): None,
        I18N.tr("project.custom_resolution", "Custom"): None,
    }
    ```
    ```python
    auto_res_text = I18N.tr("project.auto_from_media", "Auto (from Media)")
    auto_res_index = self.resolution_combo.findText(auto_res_text)
    if auto_res_index >= 0:
        self.resolution_combo.setCurrentIndex(auto_res_index)
    ```
    语言切换菜单在重启前即调用 `I18N.load_locale(locale_code)`（src/app/controllers/menu_toolbar_controller.py:677-689），翻译立即生效；但预设字典的键在模块首次 import 时已冻结为旧语言文本。
  - 影响: 用户切换语言后选择"稍后重启"并再次打开对话框时，findText 找不到 Auto 项 → currentIndex 停留在第 0 项 "4K DCI"，宽高被填成 4096x2160 而非素材分辨率；且 `_on_resolution_changed` 中 `preset_name == auto_text` 永假，"Auto" 条目退化为 Custom 行为。以翻译显示文本作字典键/查找键的模式本身脆弱。
  - 建议: 预设改存稳定 key（如枚举/英文 key），在每次实例化时用当前 locale 生成显示文本；用 currentData 而非 findText 定位默认项。
  - 置信度: 高（机制与触发链已验证；触发窗口为"切换语言后未重启"期间）

- [AUD-20502] [Suggestion] 构造函数在 UI 线程同步探测媒体元数据，未命中缓存时可阻塞
  - 位置: src/features/edit/ui/quick_project_dialog.py:105, 146
  - 证据:
    ```python
    self._media_metadata: dict = self._probe_media_metadata(media_path)
    ...
    video_meta = MediaSourceFactory.probe_video_metadata(path)
    ```
    probe_video_metadata 走 FFmpeg/PyAV 探测（src/shared/media/factory.py:705-760），有按文件签名的缓存，但首次探测（如网络盘/新文件）为同步子进程与文件 I/O。
  - 影响: 对话框弹出前 UI 卡顿（首次未缓存时可达数百毫秒以上，网络路径更久）。
  - 建议: 探测移至后台线程/任务，对话框先显示占位再回填；或复用 Library 侧已产生的探测缓存并传入。
  - 置信度: 高（阻塞路径确定，实际耗时取决于文件与缓存状态）

- [AUD-20503] [Suggestion] 探测异常被宽泛 except Exception 吞掉后静默采用默认元数据
  - 位置: src/features/edit/ui/quick_project_dialog.py:160-161, 178-179
  - 证据:
    ```python
    except Exception as e:
        logger.warning(f"Failed to probe video metadata: {e}")
    ```
    失败后沿用 1920x1080/24fps 默认值继续创建项目。
  - 影响: 违反"不得宽泛 except 掩盖领域错误"约束：探测失败（文件损坏、解码器缺失等）仅留日志，用户无感知地以错误分辨率/帧率建项。
  - 建议: 区分 FileNotFoundError/解码错误等领域异常并在 UI 上提示"元数据探测失败，使用默认值"；至少在信息区标注"未探测"。
  - 置信度: 高

- [AUD-20504] [Suggestion] 项目名未清洗非法字符与路径分隔符
  - 位置: src/features/edit/ui/quick_project_dialog.py:573-601
  - 证据:
    ```python
    name = self.name_edit.text().strip()
    if not name:
        ...
    project_filename = f"{name}.topos"
    project_path = os.path.join(location, project_filename)
    ```
    仅校验非空；名称含 `/`、`..`、`:` 或两侧空白外的控制字符时未拦截。
  - 影响: name 含路径分隔符时 project_path 指向子目录，存在检查（os.path.exists）与实际写入路径不一致，创建可能在下游失败或写到意外位置；Windows 保留字符会保存失败。
  - 建议: 对 name 做文件名合法性校验/清洗（禁止 `/\:*?"<>|` 与纯点号），或用 QRegularExpression 限制输入。
  - 置信度: 高

- [AUD-20505] [Suggestion] quick_project_created 信号声明后从未发射（死信号）
  - 位置: src/features/edit/ui/quick_project_dialog.py:58, 625-635
  - 证据:
    ```python
    quick_project_created = pyqtSignal(str, str, dict)
    ...
    # 保存参数供 MainWindow 读取（不发射信号，避免重入）
    self._project_path = project_path
    ```
    全库 grep 无任何 connect/emit 该信号的代码（file_controller.py:4255-4266 改为 exec() 后读 getter）。
  - 影响: 与类 docstring"信号驱动"描述不符，误导后续维护者连接一个永不发射的信号。
  - 建议: 删除该信号并同步更新 docstring，或注明结果经 getter 传递。
  - 置信度: 高

- [AUD-20506] [Suggestion] 非 VIDEO 扩展名一律归类为 image，音频素材会被误判
  - 位置: src/features/edit/ui/quick_project_dialog.py:138-163
  - 证据:
    ```python
    from src.shared.media.format_registry import VIDEO_EXTENSIONS
    video_extensions = tuple(VIDEO_EXTENSIONS)
    if path.lower().endswith(video_extensions):
        metadata["media_type"] = "video"
    else:
        metadata["media_type"] = "image"
    ```
    format_registry 提供 AUDIO_EXTENSIONS（src/shared/media/format_registry.py:99）但未使用。
  - 影响: 若调用方传入音频文件，类型显示为"Image"且走图片探测失败回退默认值。当前唯一入口 library_panel 仅对视频扩展名触发本对话框（src/app/ui/library_panel.py:558-567），故属防御性缺口而非现行 bug。
  - 建议: 用 format_registry 的分类函数三分 video/image/audio，对 audio 给出明确错误或走对应探测。
  - 置信度: 中（缺陷确定，现调用链不可达）

- [AUD-20507] [Suggestion] 裸 dict 返回注解，未参数化
  - 位置: src/features/edit/ui/quick_project_dialog.py:117-124, 649
  - 证据:
    ```python
    def _probe_media_metadata(self, path: str) -> dict:
    ...
    def get_timeline_settings(self) -> dict:
    ```
  - 影响: 字典结构（width/height/fps/...）无类型约束，调用方与维护者无法从签名得知键集，违反项目强类型规范。
  - 建议: 改为 `dict[str, Any]` 或引入 TypedDict（TimelineSettings/ProbeMetadata）。
  - 置信度: 高

### 通过项
- 线程安全：纯 UI 线程组件，无跨线程共享状态或后台线程访问
- GPU 资源生命周期：不创建/持有任何 OpenGL·ModernGL 资源，无释放遗漏
- GLSL·HDR 正确性：无着色器/HDR 数据路径
- 信号防递归：combo/spin 的程序化 setValue 未连接会再触发的槽；"先 accept 关闭、由 MainWindow 创建"的模式避免了创建流程重入（file_controller.py:4255 佐证）
- 状态提交一致性：对话框不直接写工程/不触碰 undo·redo·revision，结果经 getter 一次性交给调用方，职责边界清晰
- 取消/异常路径：reject 直接关闭无副作用；同名覆盖有确认对话（残留风险见 AUD-20504）
- 输入验证：名称/位置空值、目录存在性、同名覆盖均已校验

---

## src/features/edit/ui/relink_media_dialog.py (行数: 554)

### 发现

- [AUD-20508] [Critical] item.data(Qt.ItemDataRole) 传入枚举类型而非 .UserRole 成员，四处调用全部抛 TypeError，重链接功能整体不可用
  - 位置: src/features/edit/ui/relink_media_dialog.py:347, 381, 416, 499（对照正确用法 :288）
  - 证据:
    ```python
    for row in selected_rows:
        asset_id = self._table.item(row, 0).data(Qt.ItemDataRole)   # :347
    ```
    同样写法见 `_on_relink_by_directory`(:381)、`_on_auto_search`(:416)、`_update_relink_path`(:499)。写入侧是正确的：`name_item.setData(Qt.ItemDataRole.UserRole, asset_id)`(:288)。
    在本项目 .venv（PyQt6, offscreen）实测复现：
    `TypeError: data(self, role: int): argument 1 has unexpected type 'EnumType'`
  - 影响: "Relink Selected"、"Relink by Directory"、"Auto Search" 三个入口在取 asset_id 的第一行即抛未捕获 TypeError（PyQt 打印回溯后槽中断），_relink_map 永远为空，OK 后 get_relink_map() 返回空 dict —— 离线素材重链接（P6.3）经此对话框完全失效，且表现为静默无效果。两个调用方（src/features/edit/module.py:91、src/app/.../media_pool_panel.py:9985）同样受影响。
  - 建议: 四处改为 `data(Qt.ItemDataRole.UserRole)`；为对话框补一条最小冒烟测试（实例化后调用 `_update_relink_path` 断言 relink_map）防回归。
  - 置信度: 高（已在项目自带 venv 中离屏复现）

- [AUD-20509] [Suggestion] 目录/自动搜索在 UI 线程同步扫描，且对同一目录反复 listdir 无缓存
  - 位置: src/features/edit/ui/relink_media_dialog.py:367-409, 411-463, 465-495
  - 证据:
    ```python
    for row in range(self._table.rowCount()):
        ...
        for search_dir in search_dirs:
            found_path = self._search_file_in_directory(search_dir, original_filename)
    ```
    `_search_file_in_directory` 对每行×每目录执行 os.listdir + 逐子项 os.path.isdir（一层深），同一 search_dir 被每个素材重复列目录。
  - 影响: N 个离线素材 × 6 个候选目录（含 Desktop/Documents/Downloads 等）在 UI 线程重复扫描，主目录条目多或网络 home 时明显卡顿、界面冻结。
  - 建议: 每个搜索目录只 listdir 一次并构建文件名→路径索引供所有行复用；数据量大时移到后台线程并给出进度。
  - 置信度: 高（重复扫描确定，卡顿程度取决于目录规模）

- [AUD-20510] [Suggestion] 重链接不校验所选文件与素材媒体类型/扩展名是否匹配
  - 位置: src/features/edit/ui/relink_media_dialog.py:357-365, 497-518
  - 证据:
    ```python
    new_path, _ = QFileDialog.getOpenFileName(
        self, f"Relink: {display_name}", original_dir,
        "Media files (*.mp4 *.mov ...);;All files (*)"
    )
    if new_path:
        self._update_relink_path(row, new_path)
    ```
    `_update_relink_path` 仅检查 `os.path.exists(new_path)`。
  - 影响: 可把视频素材 relink 到图片/音频（Auto Search 按同名匹配亦不校验类型），错误映射在 accept 后直接写入工程，后续解码失败才暴露。
  - 建议: 比较新旧路径扩展名/媒体类型，不匹配时警告确认；Auto Search 命中时校验同扩展名优先。
  - 置信度: 高

- [AUD-20511] [Suggestion] 大量界面文案硬编码英文，绕过 I18N
  - 位置: src/features/edit/ui/relink_media_dialog.py:209-212, 219-221, 246, 251-253, 258-260, 301, 341-343, 401-409, 455-462, 536-541
  - 证据:
    ```python
    self._table.setHorizontalHeaderLabels([
        "Asset Name", "Original Path", "New Path", "Status"
    ])
    ...
    status_item = QTableWidgetItem("Offline")
    ```
    同文件中对话框标题、三个按钮文字又用了 `I18N.tr`（:146, :245, :250, :257），两种风格混用。
  - 影响: 非英文界面下该对话框一半翻译一半英文，与 QuickProjectDialog 等全 I18N 面板不一致。
  - 建议: 文案统一走 I18N.tr（含表头、"Offline/Found/Missing" 状态与所有 QMessageBox 文本）。
  - 置信度: 高

- [AUD-20512] [Suggestion] 未使用导入与重复导入
  - 位置: src/features/edit/ui/relink_media_dialog.py:36-44, 145, 240
  - 证据:
    ```python
    QGroupBox,
    ...
    from PyQt6.QtCore import Qt, pyqtSignal
    ...
    from src.features.edit.ui.edit_style import (
        get_color, panel_style, input_style,
    )
    ```
    QGroupBox、pyqtSignal、panel_style、input_style 在文件内均无使用（grep 验证仅出现在 import 行）；`from src.shared.i18n.i18n import I18N` 在 `_setup_ui` 内重复导入两次（:145 与 :240）。
  - 影响: 冗余依赖与噪音，偏离最小导入规范。
  - 建议: 删除未用导入，I18N 提升为模块级单次导入。
  - 置信度: 高

- [AUD-20513] [Suggestion] 死状态与绕过 __init__ 的工厂实现
  - 位置: src/features/edit/ui/relink_media_dialog.py:83, 104-141
  - 证据:
    ```python
    dialog = cls.__new__(cls)
    QDialog.__init__(dialog, parent)
    dialog._assets = asset_refs
    ...
    dialog._asset_type = "MediaAssetRef"
    ```
    `_asset_type` 三处赋值（:83, :109, :136）但全文件无任何读取；`from_offline_report`（Dict 路径）全库无调用者（仅 from_asset_refs 被 module.py:91 使用）。
  - 影响: `_asset_type` 是死状态；`cls.__new__` 绕过 `__init__` 使后续在 `__init__` 中新增的初始化被工厂静默跳过，易埋隐患；Dict 路径无测试无调用。
  - 建议: 删除 `_asset_type`（或在 `_get_asset_attr` 中实际用于分发）；工厂改为调用 `cls(assets, parent)`（运行期类型注解并不阻止传入 MediaAssetRef）；无调用的 from_offline_report 补调用方或删除。
  - 置信度: 高

### 通过项
- 线程安全：纯 UI 线程组件，无跨线程状态
- GPU 资源生命周期：不涉及 OpenGL/ModernGL 资源
- GLSL·HDR 正确性：无着色器/HDR 数据路径
- 信号防递归：item.setText 更新不触发已连接的业务槽，无递归回路
- 状态提交一致性：对话框不直接改工程状态，仅产出 relink_map 交调用方提交（MediaAssetRef 侧 relink_assets 不可变更新），边界清晰
- 取消/异常路径：Cancel/reject 无副作用；OK 前对不存在的路径有二次确认
- _search_file_in_directory 的 OSError 捕获为权限类 I/O 防御且范围限定，属可接受用法

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-20508 |
| Major | 2 | AUD-20500, AUD-20501 |
| Suggestion | 12 | AUD-20502–20507, AUD-20509–20513 |
