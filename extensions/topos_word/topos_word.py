import io
import os
import re
import sys
import shutil
import tempfile
import subprocess
from pathlib import Path

from PyQt6.QtCore import (
    Qt, QSize, QRect, QRectF, QSizeF, QUrl, QByteArray, QBuffer, QIODevice,
    QMarginsF, QTimer, pyqtSignal, QSettings
)
from PyQt6.QtGui import (
    QIcon, QPixmap, QPainter, QColor, QFont, QFontMetricsF, QAction, QKeySequence,
    QShortcut, QTransform, QDesktopServices, QTextCursor, QTextCharFormat,
    QTextBlockFormat, QTextListFormat, QTextDocument, QTextFormat, QTextImageFormat,
    QTextTable, QTextTableFormat, QTextLength, QImage, QPageSize, QPageLayout,
    QTextDocumentFragment
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QTextEdit, QToolBar, QToolButton, QFontComboBox, QComboBox, QSpinBox,
    QFileDialog, QColorDialog, QMessageBox, QDialog, QDialogButtonBox,
    QInputDialog, QMenu, QScrollArea, QFrame, QLabel, QLineEdit, QPushButton,
    QStatusBar, QSizePolicy, QListWidget, QListWidgetItem, QSplitter, QCheckBox
)
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtPrintSupport import QPrinter, QPrintDialog

try:
    import docx
    from docx.shared import Pt, RGBColor, Inches, Mm
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.text.paragraph import CT_P
    from docx.oxml.table import CT_Tbl
    from docx.text.paragraph import Paragraph
    from docx.text.run import Run
    from docx.table import Table
    from docx.oxml import OxmlElement
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    HAS_DOCX = True
except ImportError:
    HAS_DOCX = False


# ============================================================================
# 1. 内置矢量 SVG 图标集（纯代码生成，无外部文件依赖）
# ============================================================================
SVG_ICONS = {
    "new": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="12" y1="18" x2="12" y2="12"/><line x1="9" y1="15" x2="15" y2="15"/>',
    "open": '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>',
    "save": '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/>',
    "pdf": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><path d="M12 18v-6"/><polyline points="9 15 12 18 15 15"/>',
    "long_img": '<rect x="3" y="2" width="18" height="20" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/>',
    "print": '<polyline points="6 9 6 2 18 2 18 9"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect x="6" y="14" width="12" height="8"/>',
    "undo": '<polyline points="9 14 4 9 9 4"/><path d="M20 20v-7a4 4 0 0 0-4-4H4"/>',
    "redo": '<polyline points="15 14 20 9 15 4"/><path d="M4 20v-7a4 4 0 0 1 4-4h12"/>',
    "bold": '<path d="M6 4h8a4 4 0 0 1 4 4 4 4 0 0 1-4 4H6z"/><path d="M6 12h9a4 4 0 0 1 4 4 4 4 0 0 1-4 4H6z"/>',
    "italic": '<line x1="19" y1="4" x2="10" y2="4"/><line x1="14" y1="20" x2="5" y2="20"/><line x1="15" y1="4" x2="9" y2="20"/>',
    "underline": '<path d="M6 3v7a6 6 0 0 0 6 6 6 6 0 0 0 6-6V3"/><line x1="4" y1="21" x2="20" y2="21"/>',
    "strike": '<path d="M16 4H9a3 3 0 0 0-2.83 4"/><path d="M14 12a4 4 0 0 1 0 8H6"/><line x1="4" y1="12" x2="20" y2="12"/>',
    "text_color": '<path d="M5 18h14"/><path d="M8 14l4-10 4 10"/><path d="M9.5 11h5"/>',
    "highlight": '<path d="m9 11-6 6v3h9l3-3"/><path d="m22 12-4.6 4.6a2 2 0 0 1-2.8 0l-5.2-5.2a2 2 0 0 1 0-2.8L14 4"/>',
    "align_left": '<line x1="17" y1="10" x2="3" y2="10"/><line x1="21" y1="6" x2="3" y2="6"/><line x1="21" y1="14" x2="3" y2="14"/><line x1="17" y1="18" x2="3" y2="18"/>',
    "align_center": '<line x1="18" y1="10" x2="6" y2="10"/><line x1="21" y1="6" x2="3" y2="6"/><line x1="21" y1="14" x2="3" y2="14"/><line x1="18" y1="18" x2="6" y2="18"/>',
    "align_right": '<line x1="21" y1="10" x2="7" y2="10"/><line x1="21" y1="6" x2="3" y2="6"/><line x1="21" y1="14" x2="3" y2="14"/><line x1="21" y1="18" x2="7" y2="18"/>',
    "align_justify": '<line x1="21" y1="10" x2="3" y2="10"/><line x1="21" y1="6" x2="3" y2="6"/><line x1="21" y1="14" x2="3" y2="14"/><line x1="21" y1="18" x2="3" y2="18"/>',
    "bullet_list": '<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><circle cx="4" cy="6" r="1"/><circle cx="4" cy="12" r="1"/><circle cx="4" cy="18" r="1"/>',
    "number_list": '<line x1="10" y1="6" x2="21" y2="6"/><line x1="10" y1="12" x2="21" y2="12"/><line x1="10" y1="18" x2="21" y2="18"/><path d="M4 6h1v4"/><path d="M4 10h2"/><path d="M6 18H4c0-1 2-2 2-3s-1-1.5-2-1"/>',
    "image": '<rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/>',
    "search": '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
    "theme": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/>',
    "painter": '<path d="M19 3H5a2 2 0 0 0-2 2v4a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2z"/><path d="M12 11v4a2 2 0 0 1-2 2H9v4h4v-4h-1"/>',
    "clear_fmt": '<path d="M4 7h16"/><path d="M10 7L7 20"/><path d="M15 15l5 5"/><path d="M20 15l-5 5"/>',
    "auto_format": '<path d="m15 4-1 1 4 4 1-1a2.83 2.83 0 1 0-4-4Z"/><path d="m13 6-8.5 8.5a2.12 2.12 0 1 0 3 3L16 9"/><path d="M5 3v4"/><path d="M3 5h4"/><path d="M19 15v4"/><path d="M17 17h4"/>',
    "line_height": '<line x1="10" y1="6" x2="21" y2="6"/><line x1="10" y1="12" x2="21" y2="12"/><line x1="10" y1="18" x2="21" y2="18"/><polyline points="3 8 5 6 7 8"/><polyline points="3 16 5 18 7 16"/><line x1="5" y1="6" x2="5" y2="18"/>',
    "table": '<rect x="3" y="3" width="18" height="18" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="3" y1="15" x2="21" y2="15"/><line x1="9" y1="3" x2="9" y2="21"/><line x1="15" y1="3" x2="15" y2="21"/>',
    "outline": '<rect x="3" y="3" width="18" height="18" rx="2"/><line x1="9" y1="3" x2="9" y2="21"/><line x1="13" y1="8" x2="18" y2="8"/><line x1="13" y1="12" x2="18" y2="12"/><line x1="13" y1="16" x2="16" y2="16"/>',
    "link": '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
    "page_break": '<path d="M4 7V4h16v3"/><path d="M4 17v3h16v-3"/><line x1="2" y1="12" x2="6" y2="12"/><line x1="10" y1="12" x2="14" y2="12"/><line x1="18" y1="12" x2="22" y2="12"/>',
    "page_setup": '<rect x="4" y="2" width="16" height="20" rx="2"/><line x1="8" y1="2" x2="8" y2="22"/><line x1="4" y1="6" x2="20" y2="6"/>',
    "indent_first": '<line x1="9" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/><polyline points="3 5 6 7 3 9"/>',
}


def make_svg_icon(name: str, color: str = "#e5e5ea", active_color: str = "#ffffff", size: int = 18) -> QIcon:
    """将内置 SVG 路径动态渲染为高分屏 QIcon"""
    body = SVG_ICONS.get(name, "")

    def render_pixmap(hex_color: str) -> QPixmap:
        svg_str = f'''
        <svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}"
             viewBox="0 0 24 24" fill="none" stroke="{hex_color}"
             stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            {body}
        </svg>
        '''
        renderer = QSvgRenderer(QByteArray(svg_str.encode("utf-8")))
        dpr = 2.0
        pixmap = QPixmap(int(size * dpr), int(size * dpr))
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # 必须显式给出逻辑尺寸矩形，否则 QSvgRenderer 按 SVG 的固有尺寸
        # 画在左上角，高分屏 DPR 下只显示左上 1/4
        renderer.render(painter, QRectF(0, 0, size, size))
        painter.end()
        return pixmap

    icon = QIcon()
    icon.addPixmap(render_pixmap(color), QIcon.Mode.Normal, QIcon.State.Off)
    icon.addPixmap(render_pixmap(active_color), QIcon.Mode.Normal, QIcon.State.On)
    icon.addPixmap(render_pixmap("#ffffff"), QIcon.Mode.Active, QIcon.State.Off)
    # 禁用态：动作不可用时（如无法撤销 / 重做）图标自动变灰
    icon.addPixmap(render_pixmap("#52525b"), QIcon.Mode.Disabled, QIcon.State.Off)
    icon.addPixmap(render_pixmap("#52525b"), QIcon.Mode.Disabled, QIcon.State.On)
    return icon


def make_color_bar_icon(name: str, bar_color: QColor | None, size: int = 18) -> QIcon:
    """在字体颜色 / 高亮图标底部叠一条当前颜色指示条。
    None 表示当前没有显式颜色（跟随纸张主题），此时返回原始图标。"""
    base_icon = make_svg_icon(name, size=size)
    if bar_color is None or not bar_color.isValid() or bar_color.alpha() == 0:
        return base_icon
    # QIcon.pixmap 返回的是带 DPR 的物理像素图，QPainter 坐标仍是逻辑像素
    pixmap = base_icon.pixmap(QSize(size, size))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(bar_color)
    painter.drawRoundedRect(QRectF(2, size - 3.5, size - 4, 2.5), 1.2, 1.2)
    painter.end()
    icon = QIcon(pixmap)
    icon.addPixmap(pixmap, QIcon.Mode.Normal, QIcon.State.On)
    icon.addPixmap(pixmap, QIcon.Mode.Active, QIcon.State.Off)
    icon.addPixmap(pixmap, QIcon.Mode.Active, QIcon.State.On)
    return icon


def is_near_black(color: QColor, threshold: int = 40) -> bool:
    """判断是否为近黑色（Word 的默认字色，含 #1F1F1F 一类的“软黑”），用于交还纸张主题"""
    return (
        color.isValid()
        and color.alpha() > 0
        and color.red() <= threshold
        and color.green() <= threshold
        and color.blue() <= threshold
    )


# 自定义块属性：Markdown 引用块 / 分割线。
# QTextFormat.Property.UserProperty 是用户自定义属性的起点，+1 / +2 不会与 Qt 内置属性冲突
PROP_QUOTE_BLOCK = QTextFormat.Property.UserProperty + 1
PROP_HR_BLOCK = QTextFormat.Property.UserProperty + 2


def _clear_special_block_props(bfmt: QTextBlockFormat) -> QTextBlockFormat:
    """Qt 的 setBlockFormat 会保留未显式写过的 UserProperty，
    所以新建 / 重置段落时必须把这两个标记显式写成 False，
    不能依赖“全新 QTextBlockFormat 会自动清空”。"""
    bfmt.setProperty(PROP_QUOTE_BLOCK, False)
    bfmt.setProperty(PROP_HR_BLOCK, False)
    return bfmt

# WordprocessingML 命名空间（读写 pBdr / 域代码时用）
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def doc_logical_dpi(qdoc: QTextDocument, fallback: float = 96.0) -> float:
    """取文档排版实际使用的 DPI（macOS 逻辑 DPI 是 72，Windows 通常是 96）

    QTextDocument 的块格式（缩进、边距）以“文档逻辑像素”为单位；
    导出 Word / 打印 PDF 时必须按这个 DPI 换算成磅，否则会差 25%。
    """
    try:
        device = qdoc.documentLayout().paintDevice()
        if device is not None:
            dpi = float(device.logicalDpiX())
            if dpi > 1:
                return dpi
    except Exception:
        pass
    return float(fallback)


# ============================================================================
# 2. Word 文档双向转换引擎（支持 .docx / .doc / .rtf / .md / .html / .txt）
# ============================================================================
class WordDocumentBridge:
    """实现 Word 格式与 QTextDocument 之间的富文本、列表、表格及图片双向转换"""

    ALIGN_MAP_TO_QT = {
        WD_ALIGN_PARAGRAPH.LEFT if HAS_DOCX else 0: Qt.AlignmentFlag.AlignLeft,
        WD_ALIGN_PARAGRAPH.CENTER if HAS_DOCX else 1: Qt.AlignmentFlag.AlignCenter,
        WD_ALIGN_PARAGRAPH.RIGHT if HAS_DOCX else 2: Qt.AlignmentFlag.AlignRight,
        WD_ALIGN_PARAGRAPH.JUSTIFY if HAS_DOCX else 3: Qt.AlignmentFlag.AlignJustify,
    }

    # Word 内置高亮色（w:highlight）→ HEX，用于导入时还原
    HIGHLIGHT_COLOR_MAP = {
        "YELLOW": "#ffff00", "BRIGHT_GREEN": "#00ff00", "TURQUOISE": "#00ffff",
        "PINK": "#ff00ff", "BLUE": "#0000ff", "RED": "#ff0000",
        "DARK_BLUE": "#000080", "DARK_RED": "#800000", "VIOLET": "#800080",
        "TEAL": "#008080", "GREEN": "#008000", "GRAY_25": "#c0c0c0",
        "GRAY_50": "#808080", "DARK_YELLOW": "#808000", "WHITE": "#ffffff",
        "BLACK": "#000000",
    }

    # OOXML 中子元素必须排在 w:shd 之后的标签（保证 Word 打开不报修复）
    SHD_AFTER_IN_RPR = (
        "fitText", "vertAlign", "rtl", "cs", "em", "lang",
        "eastAsianLayout", "specVanish", "oMath",
    )
    SHD_AFTER_IN_TCPR = (
        "noWrap", "tcMar", "textDirection", "tcFitText", "vAlign", "hideMark",
        "headers", "cellIns", "cellDel", "cellMerge", "tcPrChange",
    )

    @staticmethod
    def _set_xml_shading(element_pr, hex_color: str, successors: tuple[str, ...] = ()):
        """为 w:rPr / w:tcPr 写入精确 RGB 底纹（支持任意 HEX，不受 16 色高亮限制）"""
        w_ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        # 先清掉已有底纹，避免重复写入或与旧颜色叠加
        for old in element_pr.findall(f"{w_ns}shd"):
            element_pr.remove(old)
        shd = OxmlElement("w:shd")
        shd.set(f"{w_ns}val", "clear")
        shd.set(f"{w_ns}color", "auto")
        shd.set(f"{w_ns}fill", hex_color.lstrip("#").upper())
        # 按 schema 顺序插入，而不是无脑 append
        insert_at = len(element_pr)
        for idx, child in enumerate(element_pr):
            tag = child.tag.rsplit("}", 1)[-1]
            if tag in successors:
                insert_at = idx
                break
        element_pr.insert(insert_at, shd)

    @staticmethod
    def convert_legacy_to_docx(file_path: str) -> str:
        """将旧版 .doc 或 .rtf 借助系统工具转换为临时 .docx 以便完整预览"""
        tmp_dir = tempfile.mkdtemp(prefix="liteword_")
        out_docx = os.path.join(tmp_dir, Path(file_path).stem + ".docx")
        errors: list[str] = []

        # macOS 原生自带 textutil，转换 .doc / .rtf 极快
        if sys.platform == "darwin" and shutil.which("textutil"):
            try:
                res = subprocess.run(
                    ["textutil", "-convert", "docx", file_path, "-output", out_docx],
                    capture_output=True, text=True
                )
                if res.returncode == 0 and os.path.exists(out_docx):
                    return out_docx
                errors.append("textutil: " + (res.stderr.strip() or "转换失败"))
            except Exception as exc:
                errors.append(f"textutil: {exc}")

        # 跨平台 LibreOffice 命令行备选（textutil 失败时仍有机会回退）
        soffice = shutil.which("soffice") or shutil.which("libreoffice")
        if soffice:
            try:
                res = subprocess.run(
                    [soffice, "--headless", "--convert-to", "docx", "--outdir", tmp_dir, file_path],
                    capture_output=True, text=True
                )
                if res.returncode == 0 and os.path.exists(out_docx):
                    return out_docx
                errors.append("LibreOffice: " + (res.stderr.strip() or "转换失败"))
            except Exception as exc:
                errors.append(f"LibreOffice: {exc}")

        shutil.rmtree(tmp_dir, ignore_errors=True)
        if errors:
            raise RuntimeError("旧版文档转换失败：" + "；".join(errors))
        raise RuntimeError(
            "当前系统未检测到 .doc/.rtf 转换组件（macOS 自带 textutil，Windows/Linux 请安装 LibreOffice 或先另存为 .docx）。"
        )

    @classmethod
    def load_docx_into_qdoc(cls, file_path: str, qdoc: QTextDocument, max_img_width: float = 600.0):
        """解析 .docx 的段落、样式、列表、表格与内嵌图片到 QTextDocument"""
        if not HAS_DOCX:
            raise RuntimeError("缺少 python-docx 库，请运行: pip install python-docx")

        doc = docx.Document(file_path)
        qdoc.clear()
        cursor = QTextCursor(qdoc)
        cursor.beginEditBlock()

        img_counter = 0
        first_block = True

        def _iter_para_runs(p):
            """按文档顺序展开段落内的 run，包含 w:hyperlink 里的超链接文字"""
            try:
                items = list(p.iter_inner_content())
            except AttributeError:
                return [(r, False, None) for r in p.runs]
            result = []
            for item in items:
                if isinstance(item, Run):
                    result.append((item, False, None))
                else:
                    # 解析 w:hyperlink 的外部地址，保证导入后仍可 Ctrl/Cmd 点击打开
                    href = None
                    try:
                        r_id = item._element.get(
                            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
                        )
                        rel = p.part.rels.get(r_id) if r_id else None
                        if rel is not None and rel.is_external:
                            href = rel.target_ref
                    except Exception:
                        href = None
                    for sub_run in getattr(item, "runs", []):
                        result.append((sub_run, True, href))
            return result

        try:
            for child in doc.element.body.iterchildren():
                if isinstance(child, CT_P):
                    para = Paragraph(child, doc)
                    if not first_block:
                        cursor.insertBlock()
                    first_block = False

                    # 1. 段落对齐与样式
                    block_fmt = _clear_special_block_props(QTextBlockFormat())
                    if para.alignment in cls.ALIGN_MAP_TO_QT:
                        block_fmt.setAlignment(cls.ALIGN_MAP_TO_QT[para.alignment])
                    block_fmt.setBottomMargin(6.0)

                    # 首行缩进：Word 中存的是磅值，按文档 DPI 换回逻辑像素
                    try:
                        first_indent = para.paragraph_format.first_line_indent
                        if first_indent is not None:
                            indent_px = float(first_indent.pt) * doc_logical_dpi(qdoc) / 72.0
                            if indent_px > 1.0:
                                block_fmt.setTextIndent(indent_px)
                    except Exception:
                        pass

                    # 分割线 / 引用块：识别本程序导出的段落边框
                    try:
                        p_bdr = child.find(f".//{{{W_NS}}}pBdr")
                        if p_bdr is not None:
                            has_bottom = p_bdr.find(f"{{{W_NS}}}bottom") is not None
                            has_left = p_bdr.find(f"{{{W_NS}}}left") is not None
                            if has_bottom and not has_left and not para.text.strip():
                                block_fmt.setProperty(PROP_HR_BLOCK, True)
                                block_fmt.setTopMargin(10.0)
                                block_fmt.setBottomMargin(10.0)
                            elif has_left and not has_bottom:
                                block_fmt.setProperty(PROP_QUOTE_BLOCK, True)
                    except Exception:
                        pass

                    # 还原段落行距（python-docx 的 Length 是 int 子类，
                    # 固定值会落在 EMU 量级，用 0.8~3.5 的倍数区间自然排除）
                    line_spacing = para.paragraph_format.line_spacing
                    if isinstance(line_spacing, (int, float)) and 0.8 <= line_spacing <= 3.5:
                        block_fmt.setLineHeight(
                            float(line_spacing) * 100.0,
                            QTextBlockFormat.LineHeightTypes.ProportionalHeight.value,
                        )
                    cursor.setBlockFormat(block_fmt)

                    style_name = (para.style.name if para.style else "").lower()
                    heading_size = None
                    heading_bold = False
                    heading_level = 0
                    if "heading 1" in style_name or "标题 1" in style_name:
                        heading_size, heading_bold, heading_level = 22, True, 1
                    elif "heading 2" in style_name or "标题 2" in style_name:
                        heading_size, heading_bold, heading_level = 17, True, 2
                    elif "heading 3" in style_name or "标题 3" in style_name:
                        heading_size, heading_bold, heading_level = 14, True, 3
                    elif "title" in style_name or "标题" == style_name:
                        heading_size, heading_bold, heading_level = 26, True, 1

                    # 同步标题层级，让 Word 文档导入后自动生成大纲
                    if heading_level > 0:
                        block_fmt.setHeadingLevel(heading_level)
                        cursor.setBlockFormat(block_fmt)

                    # 还原分页符：Word 用 pageBreakBefore 表示“段前分页”
                    try:
                        if para.paragraph_format.page_break_before:
                            block_fmt.setPageBreakPolicy(
                                QTextFormat.PageBreakFlag.PageBreak_AlwaysBefore
                            )
                            cursor.setBlockFormat(block_fmt)
                    except Exception:
                        pass

                    # 2. 列表样式检测
                    num_pr = child.find(
                        ".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}numPr"
                    )
                    if num_pr is not None or "list" in style_name:
                        list_fmt = QTextListFormat()
                        is_ordered = "number" in style_name or "decimal" in style_name
                        list_fmt.setStyle(
                            QTextListFormat.Style.ListDecimal if is_ordered else QTextListFormat.Style.ListDisc
                        )
                        cursor.createList(list_fmt)
                    else:
                        # 普通段落的左缩进（引用块 / Tab 缩进）：同样按文档 DPI 换算
                        try:
                            left_indent = para.paragraph_format.left_indent
                            if left_indent is not None:
                                left_px = float(left_indent.pt) * doc_logical_dpi(qdoc) / 72.0
                                if left_px > 1.0:
                                    block_fmt.setLeftMargin(left_px)
                                    cursor.setBlockFormat(block_fmt)
                        except Exception:
                            pass

                    # 引用块至少留出竖条的位置：Word 里有些引用只有边框、
                    # 没有左缩进，导入后若左边距为 0，竖条会压住第一个字。
                    if block_fmt.boolProperty(PROP_QUOTE_BLOCK) and block_fmt.leftMargin() < 8.0:
                        block_fmt.setLeftMargin(24.0)
                        cursor.setBlockFormat(block_fmt)

                    # 3. 遍历 Run（文本格式与内嵌图片）
                    for run, is_link, href in _iter_para_runs(para):
                        # 检查 Run 内是否有内嵌图片 (w:drawing -> a:blip)
                        blips = run._element.xpath(".//a:blip")
                        if blips:
                            for blip in blips:
                                r_id = blip.get(
                                    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
                                )
                                # 安全取关系：外部链接的 rel 没有 target_part，
                                # 用 related_parts 直接索引会抛 ValueError/KeyError
                                rel = doc.part.rels.get(r_id) if r_id else None
                                if rel is not None and not rel.is_external:
                                    img_bytes = rel.target_part.blob
                                    qimg = QImage.fromData(img_bytes)
                                    if not qimg.isNull():
                                        img_counter += 1
                                        res_name = f"docx_img_{img_counter}.png"
                                        qdoc.addResource(
                                            QTextDocument.ResourceType.ImageResource,
                                            QUrl(res_name),
                                            qimg
                                        )
                                        img_fmt = QTextImageFormat()
                                        img_fmt.setName(res_name)
                                        # 自适应限制最大宽度以契合当前纸张版面
                                        max_w = max(100.0, float(max_img_width))
                                        if qimg.width() > max_w:
                                            ratio = max_w / qimg.width()
                                            img_fmt.setWidth(max_w)
                                            img_fmt.setHeight(qimg.height() * ratio)
                                        cursor.insertImage(img_fmt)

                        if run.text:
                            char_fmt = QTextCharFormat()
                            font_name = run.font.name or (
                                "PingFang SC" if sys.platform == "darwin" else "Microsoft YaHei"
                            )
                            char_fmt.setFontFamilies([font_name])

                            if run.font.size:
                                char_fmt.setFontPointSize(run.font.size.pt)
                            elif heading_size:
                                char_fmt.setFontPointSize(heading_size)
                            else:
                                char_fmt.setFontPointSize(12)

                            is_bold = run.bold if run.bold is not None else heading_bold
                            char_fmt.setFontWeight(QFont.Weight.Bold if is_bold else QFont.Weight.Normal)
                            char_fmt.setFontItalic(bool(run.italic))
                            char_fmt.setFontUnderline(bool(run.underline))
                            char_fmt.setFontStrikeOut(bool(run.font.strike))

                            try:
                                if run.font.color and run.font.color.rgb:
                                    rgb_hex = str(run.font.color.rgb)
                                    # 纯黑 / 软黑是 Word 的默认字色，不写成显式颜色，
                                    # 让它跟随纸张主题，避免暗色护眼模式下黑字黑底
                                    if len(rgb_hex) == 6 and not is_near_black(QColor(f"#{rgb_hex}")):
                                        char_fmt.setForeground(QColor(f"#{rgb_hex}"))
                            except Exception:
                                pass

                            # 还原底纹：优先本程序导出的 w:shd（任意 HEX），
                            # 其次 Word 内置荧光笔 w:highlight（16 色）
                            try:
                                shd_fill = run._element.xpath("./w:rPr/w:shd/@w:fill")
                                if shd_fill:
                                    fill = str(shd_fill[0]).strip()
                                    if len(fill) == 6 and fill.lower() != "auto":
                                        char_fmt.setBackground(QColor(f"#{fill}"))
                                elif run.font.highlight_color is not None:
                                    hl_hex = cls.HIGHLIGHT_COLOR_MAP.get(
                                        getattr(run.font.highlight_color, "name", "")
                                    )
                                    if hl_hex:
                                        char_fmt.setBackground(QColor(hl_hex))
                            except Exception:
                                pass

                            if is_link:
                                # 超链接文字用蓝色下划线示意，避免与正文混在一起
                                char_fmt.setForeground(QColor("#0a84ff"))
                                char_fmt.setFontUnderline(True)
                                if href:
                                    char_fmt.setAnchor(True)
                                    char_fmt.setAnchorHref(href)

                            cursor.insertText(run.text, char_fmt)

                elif isinstance(child, CT_Tbl):
                    # 4. 表格转换：直接建成可编辑 QTextTable，实现导入→编辑→导出闭环
                    table = Table(child, doc)
                    rows_cnt = len(table.rows)
                    cols_cnt = len(table.columns)
                    if rows_cnt > 0 and cols_cnt > 0:
                        if not first_block:
                            cursor.insertBlock()
                        first_block = False

                        tbl_fmt = QTextTableFormat()
                        tbl_fmt.setBorder(1.0)
                        tbl_fmt.setBorderBrush(QColor("#8e8e93"))
                        tbl_fmt.setCellPadding(6.0)
                        tbl_fmt.setCellSpacing(0.0)
                        tbl_fmt.setWidth(QTextLength(QTextLength.Type.PercentageLength, 100.0))
                        qtable = cursor.insertTable(rows_cnt, cols_cnt, tbl_fmt)
                        for r_idx, row in enumerate(table.rows):
                            for c_idx in range(min(cols_cnt, len(row.cells))):
                                cell_cursor = qtable.cellAt(r_idx, c_idx).firstCursorPosition()

                                # 还原单元格底色（w:tcPr/w:shd）
                                try:
                                    fills = row.cells[c_idx]._tc.xpath("./w:tcPr/w:shd/@w:fill")
                                    if fills:
                                        fill = str(fills[0]).strip()
                                        if len(fill) == 6 and fill.lower() != "auto":
                                            cell_fmt = qtable.cellAt(r_idx, c_idx).format()
                                            cell_fmt.setBackground(QColor(f"#{fill}"))
                                            qtable.cellAt(r_idx, c_idx).setFormat(cell_fmt)
                                except Exception:
                                    pass

                                cell_txt = row.cells[c_idx].text.strip()
                                if cell_txt:
                                    c_fmt = QTextCharFormat()
                                    if r_idx == 0:
                                        c_fmt.setFontWeight(QFont.Weight.Bold)
                                    cell_cursor.insertText(cell_txt, c_fmt)
                        # 后续段落落到表格之后
                        cursor = QTextCursor(qdoc)
                        cursor.movePosition(QTextCursor.MoveOperation.End)
                        first_block = True
        finally:
            # 即使解析中途抛出异常也闭合编辑块，避免文档停留在未结束的编辑状态
            cursor.endEditBlock()

    @staticmethod
    def _set_paragraph_border(p, edge: str, color_hex: str, size: int = 12, space: int = 8):
        """给 Word 段落加一条边框（用于引用块左竖线 / 分割线底边）"""
        pPr = p._p.get_or_add_pPr()
        pBdr = OxmlElement("w:pBdr")
        el = OxmlElement(f"w:{edge}")
        el.set(f"{{{W_NS}}}val", "single")
        el.set(f"{{{W_NS}}}sz", str(size))
        el.set(f"{{{W_NS}}}space", str(space))
        el.set(f"{{{W_NS}}}color", color_hex)
        pBdr.append(el)
        pPr.append(pBdr)

    @classmethod
    def _write_block_to_word_paragraph(cls, block, p, qdoc: QTextDocument, max_img_in: float = 6.2):
        """把一个 QTextBlock 的字符格式、内嵌图片写入 python-docx 段落"""
        bf = block.blockFormat()
        dpi = doc_logical_dpi(qdoc)
        is_quote = bf.boolProperty(PROP_QUOTE_BLOCK)

        # 引用块：先写 pBdr，保证 OOXML 子元素顺序合法（pBdr 必须排在 spacing/ind 前面）
        if bf.boolProperty(PROP_QUOTE_BLOCK):
            cls._set_paragraph_border(p, "left", "0A84FF", size=18, space=10)

        align = block.blockFormat().alignment()
        if align & Qt.AlignmentFlag.AlignCenter:
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        elif align & Qt.AlignmentFlag.AlignRight:
            p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        elif align & Qt.AlignmentFlag.AlignJustify:
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        else:
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT

        # 分页符：Word 用“段前分页”实现，保证导出后仍从新页开始
        if block.blockFormat().pageBreakPolicy() == QTextFormat.PageBreakFlag.PageBreak_AlwaysBefore:
            p.paragraph_format.page_break_before = True

        # 行距：仅导出按比例设置的行距，固定值 / 单倍行距保持 Word 默认
        if (bf.lineHeightType() == QTextBlockFormat.LineHeightTypes.ProportionalHeight.value
                and bf.lineHeight() > 0):
            p.paragraph_format.line_spacing = round(bf.lineHeight() / 100.0, 2)

        # 左缩进（Tab / 引用块）与首行缩进：逻辑像素 → 磅
        if block.textList() is None and bf.leftMargin() > 1.0:
            p.paragraph_format.left_indent = Pt(round(bf.leftMargin() * 72.0 / dpi, 1))
        if bf.textIndent() > 1.0:
            p.paragraph_format.first_line_indent = Pt(round(bf.textIndent() * 72.0 / dpi, 1))

        def _style_run(run, cfmt):
            run.bold = cfmt.fontWeight() >= QFont.Weight.Bold
            run.italic = cfmt.fontItalic()
            run.underline = cfmt.fontUnderline()
            run.font.strike = cfmt.fontStrikeOut()

            families = cfmt.fontFamilies()
            if families:
                run.font.name = families[0]
            pt_size = cfmt.fontPointSize()
            if pt_size > 0:
                run.font.size = Pt(pt_size)

            # 仅当用户显式设置过前景色时才写入 Word，避免把
            # 白纸/暗色纸张的主题字色误当成正文颜色写进文档
            if cfmt.hasProperty(QTextCharFormat.Property.ForegroundBrush):
                fg = cfmt.foreground().color()
                if fg.isValid():
                    # 暗色纸张下引用文字是浅灰，导出到白纸 Word 会几乎看不见；
                    # 只在引用块内把过浅的灰色收回到可读的中灰。
                    if is_quote and fg.lightness() > 150:
                        fg = QColor("#6e6e73")
                    run.font.color.rgb = RGBColor(fg.red(), fg.green(), fg.blue())

            # 文本高亮底色：用 w:shd 写入任意 HEX，避免 16 色高亮限制
            if cfmt.hasProperty(QTextCharFormat.Property.BackgroundBrush):
                bg = cfmt.background().color()
                if bg.isValid() and bg.alpha() > 0:
                    cls._set_xml_shading(
                        run._element.get_or_add_rPr(), bg.name(), cls.SHD_AFTER_IN_RPR
                    )

        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            if frag.isValid():
                cfmt = frag.charFormat()
                if cfmt.isImageFormat():
                    img_fmt = cfmt.toImageFormat()
                    res_data = qdoc.resource(
                        QTextDocument.ResourceType.ImageResource,
                        QUrl(img_fmt.name())
                    )
                    qimg = None
                    if isinstance(res_data, QImage):
                        qimg = res_data
                    elif isinstance(res_data, QPixmap):
                        qimg = res_data.toImage()
                    elif os.path.exists(img_fmt.name()):
                        qimg = QImage(img_fmt.name())

                    if qimg and not qimg.isNull():
                        buf = QByteArray()
                        qbuf = QBuffer(buf)
                        qbuf.open(QIODevice.OpenModeFlag.WriteOnly)
                        qimg.save(qbuf, "PNG")
                        qbuf.close()
                        img_stream = io.BytesIO(bytes(buf))
                        width_px = img_fmt.width() if img_fmt.width() > 0 else qimg.width()
                        width_in = min(max_img_in, max(0.5, width_px / 96.0))
                        p.add_run().add_picture(img_stream, width=Inches(width_in))
                else:
                    txt = frag.text().replace("\u2028", "\n")
                    if txt:
                        href = cfmt.anchorHref() if cfmt.isAnchor() else ""
                        if href:
                            # 写成真正的 w:hyperlink，Word 里可点击、可编辑
                            rel_id = p.part.relate_to(href, RT.HYPERLINK, is_external=True)
                            hyperlink = OxmlElement("w:hyperlink")
                            hyperlink.set(
                                "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id",
                                rel_id,
                            )
                            run = Run(OxmlElement("w:r"), p)
                            run.text = txt
                            _style_run(run, cfmt)
                            hyperlink.append(run._element)
                            p._p.append(hyperlink)
                        else:
                            run = p.add_run(txt)
                            _style_run(run, cfmt)
            it += 1

    @classmethod
    def save_qdoc_to_docx(cls, qdoc: QTextDocument, file_path: str, page_cfg: dict | None = None):
        """将 QTextDocument 的标题、段落、列表、原生表格及图片导出为标准 .docx"""
        if not HAS_DOCX:
            raise RuntimeError("缺少 python-docx 库，请运行: pip install python-docx")

        doc = docx.Document()
        # 设置默认正文字体（按平台选择中文字体）
        normal_style = doc.styles["Normal"]
        normal_style.font.name = "PingFang SC" if sys.platform == "darwin" else "Microsoft YaHei"
        normal_style.font.size = Pt(12)

        # 应用页面设置：纸张尺寸、方向与页边距
        max_img_in = 6.2
        if page_cfg:
            sec = doc.sections[0]
            dims = {"A4": (210, 297), "Letter": (216, 279), "A5": (148, 210)}
            w_mm, h_mm = dims.get(page_cfg.get("paper", "A4"), (210, 297))
            if page_cfg.get("landscape"):
                w_mm, h_mm = h_mm, w_mm
            sec.page_width = Mm(w_mm)
            sec.page_height = Mm(h_mm)
            top, right, bottom, left = page_cfg.get("margins_mm", (20, 22, 20, 22))
            sec.top_margin = Mm(top)
            sec.right_margin = Mm(right)
            sec.bottom_margin = Mm(bottom)
            sec.left_margin = Mm(left)
            # 图片最大宽度不能超过正文栏宽，否则会在 Word 中溢出页边距
            max_img_in = max(1.0, (float(w_mm) - left - right) / 25.4)

            # 页眉 / 自动页码：默认与页边距保持 10mm 间距，和 PDF 导出的位置一致
            sec.header_distance = Mm(10)
            sec.footer_distance = Mm(10)

            header_txt = str(page_cfg.get("header_text", "")).strip()
            if header_txt:
                hp = sec.header.paragraphs[0]
                hp.text = header_txt
                hp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                if hp.runs:
                    h_run = hp.runs[0]
                    h_run.font.size = Pt(9)
                    h_run.font.color.rgb = RGBColor(142, 142, 147)
                    h_run.font.name = normal_style.font.name

            if page_cfg.get("page_number"):
                fp = sec.footer.paragraphs[0]
                fp.alignment = WD_ALIGN_PARAGRAPH.CENTER

                def _meta_run(text: str):
                    run = fp.add_run(text)
                    run.font.size = Pt(9)
                    run.font.color.rgb = RGBColor(142, 142, 147)
                    return run

                def _add_word_field(paragraph, field_name: str, placeholder: str):
                    """w:fldSimple 动态域；带一个占位结果，Word 未刷新时也不会显示空白"""
                    fld = OxmlElement("w:fldSimple")
                    fld.set(f"{{{W_NS}}}instr", field_name)
                    fld.set(f"{{{W_NS}}}dirty", "true")
                    r = OxmlElement("w:r")
                    rPr = OxmlElement("w:rPr")
                    color = OxmlElement("w:color")
                    color.set(f"{{{W_NS}}}val", "8E8E93")
                    rPr.append(color)
                    sz = OxmlElement("w:sz")
                    sz.set(f"{{{W_NS}}}val", "18")  # 9pt = 18 half-points
                    rPr.append(sz)
                    r.append(rPr)
                    t = OxmlElement("w:t")
                    t.text = placeholder
                    r.append(t)
                    fld.append(r)
                    paragraph._p.append(fld)

                _meta_run("第 ")
                _add_word_field(fp, " PAGE ", "1")
                _meta_run(" 页 / 共 ")
                _add_word_field(fp, " NUMPAGES ", "1")
                _meta_run(" 页")

        # 用 rootFrame 迭代器按文档顺序输出：普通段落/标题/列表 + 表格
        it = qdoc.rootFrame().begin()
        while not it.atEnd():
            frame = it.currentFrame()
            if isinstance(frame, QTextTable):
                rows, cols = frame.rows(), frame.columns()
                word_tbl = doc.add_table(rows=rows, cols=cols)
                try:
                    word_tbl.style = "Table Grid"
                except Exception:
                    pass
                for r in range(rows):
                    for c in range(cols):
                        cell = frame.cellAt(r, c)
                        w_cell = word_tbl.cell(r, c)

                        # 导出单元格底色（任意 HEX → w:tcPr/w:shd）
                        try:
                            cell_fmt = cell.format()
                            if cell_fmt.hasProperty(QTextFormat.Property.BackgroundBrush):
                                cell_bg = cell_fmt.background().color()
                                if cell_bg.isValid() and cell_bg.alpha() > 0:
                                    cls._set_xml_shading(
                                        w_cell._tc.get_or_add_tcPr(),
                                        cell_bg.name(),
                                        cls.SHD_AFTER_IN_TCPR,
                                    )
                        except Exception:
                            pass

                        # QTextTableCell 没有 begin()，用光标在单元格内逐段移动
                        start_pos = cell.firstCursorPosition().position()
                        end_pos = cell.lastCursorPosition().position()
                        cur = QTextCursor(qdoc)
                        cur.setPosition(start_pos)
                        first_p = True
                        while True:
                            wp = w_cell.paragraphs[0] if first_p else w_cell.add_paragraph()
                            first_p = False
                            cls._write_block_to_word_paragraph(cur.block(), wp, qdoc, max_img_in)
                            if not cur.movePosition(QTextCursor.MoveOperation.NextBlock):
                                break
                            if cur.position() > end_pos:
                                break
            else:
                block = it.currentBlock()
                if block.isValid():
                    # 分割线：导出成“空段落 + 底边框”，Word 打开后是一条真正的横线
                    # 只有空段落才是分割线；万一有文字（例如光标点进去误输入），
                    # 按普通正文导出，绝不能把文字丢掉。
                    if (block.blockFormat().boolProperty(PROP_HR_BLOCK)
                            and not block.text().strip()):
                        hr_p = doc.add_paragraph()
                        cls._set_paragraph_border(hr_p, "bottom", "C7C7CC", size=6, space=1)
                        hr_p.paragraph_format.space_before = Pt(6)
                        hr_p.paragraph_format.space_after = Pt(6)
                        it += 1
                        continue

                    h_level = block.blockFormat().headingLevel()
                    text_list = block.textList()
                    try:
                        if h_level in (1, 2, 3):
                            p = doc.add_heading(level=h_level)
                        elif text_list is not None:
                            is_num = text_list.format().style() in (
                                QTextListFormat.Style.ListDecimal,
                                QTextListFormat.Style.ListLowerAlpha,
                                QTextListFormat.Style.ListUpperAlpha,
                            )
                            p = doc.add_paragraph(style="List Number" if is_num else "List Bullet")
                        else:
                            p = doc.add_paragraph()
                    except KeyError:
                        p = doc.add_paragraph()
                    cls._write_block_to_word_paragraph(block, p, qdoc, max_img_in)
            it += 1

        doc.save(file_path)


# ============================================================================
# 3. 悬浮式查找与替换条（macOS 紧凑平面风格）
# ============================================================================
class FindReplaceBar(QFrame):
    closed = pyqtSignal()

    def __init__(self, editor: QTextEdit, parent=None):
        super().__init__(parent)
        self.editor = editor
        self.setObjectName("FindReplaceBar")
        self.setFixedHeight(44)
        self.hide()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 6, 14, 6)
        layout.setSpacing(8)

        self.find_input = QLineEdit()
        self.find_input.setPlaceholderText("查找文本...")
        self.find_input.setFixedWidth(200)
        self.find_input.returnPressed.connect(self.find_next)
        # 输入即统计匹配总数与当前序号，新手不用盲目点“下一个”
        self.find_input.textChanged.connect(self._update_match_stats)
        # 编辑正文时，查找框若开着，匹配数量与黄色高亮同步刷新
        self.editor.textChanged.connect(self._refresh_highlights_if_visible)

        self.replace_input = QLineEdit()
        self.replace_input.setPlaceholderText("替换为...")
        self.replace_input.setFixedWidth(180)
        self.replace_input.returnPressed.connect(self.replace_one)

        btn_prev = QPushButton("上一个")
        btn_prev.clicked.connect(self.find_prev)

        btn_next = QPushButton("下一个")
        btn_next.clicked.connect(self.find_next)

        btn_replace = QPushButton("替换")
        btn_replace.clicked.connect(self.replace_one)

        btn_replace_all = QPushButton("全部替换")
        btn_replace_all.clicked.connect(self.replace_all)

        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet("color: #98989d; font-size: 12px;")

        btn_close = QPushButton("✕")
        btn_close.setFixedSize(26, 26)
        btn_close.setStyleSheet("border: none; color: #98989d; font-weight: bold;")
        btn_close.clicked.connect(self.close_bar)

        layout.addWidget(self.find_input)
        layout.addWidget(btn_prev)
        layout.addWidget(btn_next)
        layout.addWidget(self.replace_input)
        layout.addWidget(btn_replace)
        layout.addWidget(btn_replace_all)
        layout.addWidget(self.status_lbl)
        layout.addStretch()
        layout.addWidget(btn_close)

        # 栏内有焦点时 Esc 也能关闭（窗口级快捷键，栏隐藏时自动失效）
        self._esc_shortcut = QShortcut(QKeySequence("Esc"), self)
        self._esc_shortcut.activated.connect(self.close_bar)

    def open_bar(self):
        self.show()
        selected = self.editor.textCursor().selectedText()
        if selected:
            self.find_input.setText(selected)
        self.find_input.setFocus()
        self.find_input.selectAll()
        # 没有改动输入框内容时（例如再次打开），也要恢复黄色全量高亮
        if self.find_input.text():
            self._update_match_stats()

    def close_bar(self):
        self.hide()
        # 关闭查找栏必须撤掉所有临时黄色高亮，避免正文残留黄块
        self.editor.setExtraSelections([])
        self.editor.setFocus()
        self.closed.emit()

    def _refresh_highlights_if_visible(self):
        if self.isVisible():
            self._update_match_stats()

    def _highlight_all_matches(self, query: str):
        """把正文所有匹配项标成半透明黄色（ExtraSelection，不改文档内容）"""
        selections = []
        if query and self.isVisible():
            doc = self.editor.document()
            hl_fmt = QTextCharFormat()
            hl_fmt.setBackground(QColor(255, 214, 10, 95))
            probe = QTextCursor(doc)
            while True:
                probe = doc.find(query, probe)
                if probe.isNull():
                    break
                extra = QTextEdit.ExtraSelection()
                extra.cursor = probe
                extra.format = hl_fmt
                selections.append(extra)
        self.editor.setExtraSelections(selections)

    def find_next(self):
        query = self.find_input.text()
        if not query:
            return
        found = self.editor.find(query)
        if not found:
            # 回到文档开头循环查找
            cursor = self.editor.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            self.editor.setTextCursor(cursor)
            found = self.editor.find(query)
        if not found:
            self.status_lbl.setText("未找到匹配项")
            self._highlight_all_matches(query)
        else:
            self._update_match_stats()

    def find_prev(self):
        query = self.find_input.text()
        if not query:
            return
        found = self.editor.find(query, QTextDocument.FindFlag.FindBackward)
        if not found:
            cursor = self.editor.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.editor.setTextCursor(cursor)
            found = self.editor.find(query, QTextDocument.FindFlag.FindBackward)
        if not found:
            self.status_lbl.setText("未找到匹配项")
            self._highlight_all_matches(query)
        else:
            self._update_match_stats()

    def _update_match_stats(self):
        """统计全文匹配数与当前光标所处序号，显示为“第 X / Y 项”"""
        query = self.find_input.text()
        if not query:
            self.status_lbl.setText("")
            self.editor.setExtraSelections([])
            return
        doc = self.editor.document()
        cur_pos = self.editor.textCursor().selectionStart()
        total = 0
        current_idx = 0
        probe = QTextCursor(doc)
        while True:
            probe = doc.find(query, probe)
            if probe.isNull():
                break
            total += 1
            if probe.selectionStart() <= cur_pos:
                current_idx = total
        if total == 0:
            self.status_lbl.setText("未找到匹配项")
        elif current_idx == 0:
            # 光标位于首个匹配之前，尚未定位到具体项
            self.status_lbl.setText(f"共 {total} 项")
        else:
            self.status_lbl.setText(f"第 {current_idx} / {total} 项")
        self._highlight_all_matches(query)

    def replace_one(self):
        query = self.find_input.text()
        if not query:
            return
        cursor = self.editor.textCursor()
        if cursor.hasSelection() and cursor.selectedText().lower() == query.lower():
            cursor.insertText(self.replace_input.text())
        self.find_next()

    def replace_all(self):
        query = self.find_input.text()
        replacement = self.replace_input.text()
        if not query:
            return
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        self.editor.setTextCursor(cursor)
        count = 0
        while self.editor.find(query):
            self.editor.textCursor().insertText(replacement)
            count += 1
        cursor.endEditBlock()
        self.status_lbl.setText(f"已替换 {count} 处")
        self._highlight_all_matches(query)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close_bar()
        else:
            super().keyPressEvent(event)


# ============================================================================
# 3.1 插入表格对话框（macOS 平面暗色风格）
# ============================================================================
class InsertTableDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("插入表格")
        self.setFixedWidth(280)
        self.setStyleSheet("""
            QDialog { background-color: #27272b; color: #f5f5f7; }
            QLabel { color: #e5e5ea; font-size: 13px; }
            QSpinBox {
                background-color: #323238; color: #f5f5f7;
                border: 1px solid #3f3f46; border-radius: 6px; padding: 4px 8px;
            }
            QPushButton {
                background-color: #323238; color: #f5f5f7;
                border: 1px solid #3f3f46; border-radius: 6px; padding: 5px 14px;
            }
            QPushButton:hover { background-color: #3e3e46; }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(14)

        form = QFormLayout()
        form.setSpacing(10)

        self.spin_rows = QSpinBox()
        self.spin_rows.setRange(1, 50)
        self.spin_rows.setValue(4)

        self.spin_cols = QSpinBox()
        self.spin_cols.setRange(1, 15)
        self.spin_cols.setValue(4)

        form.addRow("行数：", self.spin_rows)
        form.addRow("列数：", self.spin_cols)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("插入")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def get_dimensions(self) -> tuple[int, int]:
        return self.spin_rows.value(), self.spin_cols.value()


# ============================================================================
# 3.2 文档大纲导航侧边栏（自动提取 H1~H3 标题并支持点击跳转）
# ============================================================================
class OutlineSidebar(QFrame):
    def __init__(self, editor: QTextEdit, scroll_area: QScrollArea, parent=None):
        super().__init__(parent)
        self.editor = editor
        self.scroll_area = scroll_area
        self.setObjectName("OutlineSidebar")
        self.setFixedWidth(220)
        self.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 12, 10, 12)
        layout.setSpacing(8)

        title = QLabel("文档大纲")
        title.setStyleSheet("color: #98989d; font-size: 12px; font-weight: 600; padding-left: 4px;")
        layout.addWidget(title)

        self.list_widget = QListWidget()
        self.list_widget.setObjectName("OutlineList")
        self.list_widget.itemClicked.connect(self._jump_to_block)
        layout.addWidget(self.list_widget)

        self.setStyleSheet("""
            QFrame#OutlineSidebar {
                background-color: #27272b;
                border-right: 1px solid #38383f;
            }
            QListWidget#OutlineList {
                background-color: transparent;
                border: none;
                color: #e5e5ea;
                font-size: 12px;
                outline: 0;
            }
            QListWidget#OutlineList::item {
                padding: 6px 8px;
                border-radius: 6px;
                margin-bottom: 2px;
            }
            QListWidget#OutlineList::item:hover {
                background-color: #323238;
            }
            QListWidget#OutlineList::item:selected {
                background-color: #0a84ff;
                color: #ffffff;
            }
        """)

    def refresh(self):
        if not self.isVisible():
            return
        self.list_widget.clear()
        block = self.editor.document().begin()
        while block.isValid():
            level = block.blockFormat().headingLevel()
            text = block.text().strip()
            if level > 0 and text:
                indent = "    " * (level - 1)
                prefix = "H1 " if level == 1 else ("H2 " if level == 2 else "H3 ")
                item = QListWidgetItem(f"{indent}{prefix}{text}")
                item.setData(Qt.ItemDataRole.UserRole, block.position())
                self.list_widget.addItem(item)
            block = block.next()

        if self.list_widget.count() == 0:
            empty_item = QListWidgetItem("暂无标题（用样式栏或 # 快捷输入添加）")
            empty_item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list_widget.addItem(empty_item)

    def _jump_to_block(self, item: QListWidgetItem):
        pos = item.data(Qt.ItemDataRole.UserRole)
        if pos is None:
            return
        cursor = self.editor.textCursor()
        cursor.setPosition(pos)
        self.editor.setTextCursor(cursor)
        self.editor.setFocus()
        crect = self.editor.cursorRect()
        target = self.editor.mapTo(self.scroll_area.widget(), crect.center())
        self.scroll_area.ensureVisible(target.x(), target.y(), 50, 120)

    def highlight_for_position(self, cursor_pos: int):
        """正文光标移动时，高亮光标所在章节对应的标题项（不触发跳转）"""
        if not self.isVisible() or self.list_widget.count() == 0:
            return
        target_row = -1
        for i in range(self.list_widget.count()):
            pos = self.list_widget.item(i).data(Qt.ItemDataRole.UserRole)
            if pos is not None and int(pos) <= cursor_pos:
                target_row = i
            elif pos is not None:
                break
        self.list_widget.blockSignals(True)
        self.list_widget.setCurrentRow(target_row)
        self.list_widget.blockSignals(False)


# ============================================================================
# 3.3 页面设置对话框（纸张尺寸、方向、页边距）
# ============================================================================
class PageSetupDialog(QDialog):
    def __init__(self, current_cfg: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("页面设置")
        self.setFixedWidth(392)
        self.setStyleSheet("""
            QDialog { background-color: #27272b; color: #f5f5f7; }
            QLabel { color: #e5e5ea; font-size: 13px; }
            QComboBox, QSpinBox {
                background-color: #323238; color: #f5f5f7;
                border: 1px solid #3f3f46; border-radius: 6px; padding: 4px 8px;
            }
            QLineEdit {
                background-color: #323238; color: #f5f5f7;
                border: 1px solid #3f3f46; border-radius: 6px; padding: 4px 8px;
            }
            QLineEdit:focus { border: 1px solid #0a84ff; }
            QCheckBox { color: #e5e5ea; font-size: 13px; spacing: 8px; }
            QCheckBox::indicator {
                width: 15px; height: 15px; border-radius: 4px;
                border: 1px solid #5a5a64; background: #323238;
            }
            QCheckBox::indicator:checked {
                background: #0a84ff; border: 1px solid #0a84ff;
            }
            QPushButton {
                background-color: #323238; color: #f5f5f7;
                border: 1px solid #3f3f46; border-radius: 6px; padding: 5px 14px;
            }
            QPushButton:hover { background-color: #3e3e46; }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(14)

        form = QFormLayout()
        form.setSpacing(10)

        self.size_combo = QComboBox()
        self.size_combo.addItems(["A4 (210 × 297 mm)", "Letter (216 × 279 mm)", "A5 (148 × 210 mm)"])
        size_map = {"A4": 0, "Letter": 1, "A5": 2}
        self.size_combo.setCurrentIndex(size_map.get(current_cfg["paper"], 0))

        self.orient_combo = QComboBox()
        self.orient_combo.addItems(["纵向 (Portrait)", "横向 (Landscape)"])
        self.orient_combo.setCurrentIndex(1 if current_cfg["landscape"] else 0)

        self.margin_combo = QComboBox()
        self.margin_combo.addItems([
            "常规 (上下 20mm / 左右 22mm)",
            "窄边距 (上下 12mm / 左右 12mm)",
            "宽边距 (上下 25mm / 左右 32mm)",
        ])
        margin_map = {"normal": 0, "narrow": 1, "wide": 2}
        self.margin_combo.setCurrentIndex(margin_map.get(current_cfg["margin_preset"], 0))
        for combo in (self.size_combo, self.orient_combo, self.margin_combo):
            combo.setMinimumWidth(238)

        form.addRow("纸张大小：", self.size_combo)
        form.addRow("纸张方向：", self.orient_combo)
        form.addRow("页边距：", self.margin_combo)

        # 页眉与页码：不占工具栏，直接并入页面设置
        self.header_input = QLineEdit(str(current_cfg.get("header_text", "")))
        self.header_input.setPlaceholderText("留空则不显示页眉")
        self.header_input.setMinimumWidth(238)
        form.addRow("页眉文字：", self.header_input)

        self.chk_page_num = QCheckBox("在页脚底部居中显示页码 (第 X 页 / 共 Y 页)")
        self.chk_page_num.setChecked(bool(current_cfg.get("page_number", False)))
        form.addRow(self.chk_page_num)

        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("应用")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def get_config(self) -> dict:
        papers = ["A4", "Letter", "A5"]
        presets = ["normal", "narrow", "wide"]
        margins_mm = {
            "normal": (20, 22, 20, 22),
            "narrow": (12, 12, 12, 12),
            "wide": (25, 32, 25, 32),
        }
        preset = presets[self.margin_combo.currentIndex()]
        return {
            "paper": papers[self.size_combo.currentIndex()],
            "landscape": self.orient_combo.currentIndex() == 1,
            "margin_preset": preset,
            "margins_mm": margins_mm[preset],
            "header_text": self.header_input.text().strip(),
            "page_number": self.chk_page_num.isChecked(),
        }


# ============================================================================
# 3.4 智能富文本画布（Markdown 快捷排版、Tab 缩进、分页符绘制、表格右键菜单）
# ============================================================================
class SmartPageEditor(QTextEdit):
    request_insert_link = pyqtSignal()
    request_insert_page_break = pyqtSignal()
    request_zoom = pyqtSignal(int)

    # 选中文字后键入成对符号，自动把选区包在中间（保留选区原有粗体/颜色等格式）
    WRAP_PAIRS = {
        "(": ")", "（": "）", "[": "]", "【": "】",
        "{": "}", "《": "》", '"': '"', "“": "”",
        "'": "'", "‘": "’",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        # 纸张内边距（上、右、下、左，px），由主窗口在应用纸张主题时写入，
        # 用于把粘贴 / 拖入 / 双击缩放的图片压进正文栏宽
        self.page_padding_px = (0, 0, 0, 0)
        # 当前“单页”在长卷画布上的像素高度，由主窗口随缩放 / 纸张尺寸同步
        self.single_page_height: int = 1123

    def wheelEvent(self, event):
        # 接管 Ctrl/Cmd + 滚轮：QTextEdit 默认只会放大内部字号，
        # 纸张宽度、页边距和缩放标签都不会跟着变；统一交给主窗口 _change_zoom。
        if event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier):
            delta = event.angleDelta().y()
            if delta == 0:
                delta = event.pixelDelta().y()
            if delta != 0:
                self.request_zoom.emit(1 if delta > 0 else -1)
            event.accept()
            return
        super().wheelEvent(event)

    def keyPressEvent(self, event):
        key = event.key()
        mods = event.modifiers()
        cursor = self.textCursor()
        typed = event.text()

        # 0) 选中文字后按成对标点：直接包裹选区，不替换、不破坏原有行内格式
        if (cursor.hasSelection() and typed in self.WRAP_PAIRS
                and not (mods & (Qt.KeyboardModifier.ControlModifier
                                 | Qt.KeyboardModifier.MetaModifier))):
            sel = cursor.selectedText()
            start = cursor.selectionStart()
            end = cursor.selectionEnd()
            left_ch, right_ch = typed, self.WRAP_PAIRS[typed]
            cursor.beginEditBlock()
            # 只在选区两端补符号，不替换中间内容：跨段选区里的 U+2029 段落分隔
            # 也能原样保留，不会被压成一段纯文本。
            cursor.setPosition(end)
            cursor.insertText(right_ch)
            cursor.setPosition(start)
            cursor.insertText(left_ch)
            cursor.setPosition(start + 1)
            cursor.setPosition(end + 1, QTextCursor.MoveMode.KeepAnchor)
            self.setTextCursor(cursor)
            cursor.endEditBlock()
            return

        in_table = cursor.currentTable() is not None

        # 1) Ctrl/Cmd + Enter 插入分页符
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and (
            mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier)
        ):
            self.request_insert_page_break.emit()
            return

        # 1.1) 普通回车：空列表项自动退出列表；标题行末回车回落到正文
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and mods == Qt.KeyboardModifier.NoModifier:
            # 输入 --- 后回车：转成一条分割线（比要求用户再敲空格更符合 Markdown 习惯）
            if self._try_hr_from_dashes():
                return
            if self._handle_plain_enter():
                return

        # 2) Tab / Shift+Tab 缩进（表格内保留 Qt 默认行为：跳到上/下一个单元格）
        if not in_table:
            if key == Qt.Key.Key_Tab and not (mods & Qt.KeyboardModifier.ControlModifier):
                self._adjust_indent(1)
                return
            if key == Qt.Key.Key_Backtab:
                self._adjust_indent(-1)
                return

        # 3) 空格触发 Markdown 快捷转换（# 标题、- 无序列表、1. 有序列表）
        if key == Qt.Key.Key_Space and mods == Qt.KeyboardModifier.NoModifier:
            if self._try_markdown_shortcut() or self._try_inline_markdown():
                return

        super().keyPressEvent(event)

    def _todo_hit(self, pos: int, point) -> tuple[int, str] | None:
        """鼠标点是否真正落在 ☐ / ☑ 字符的绘制矩形内，避免点在空格也误切换"""
        for p in (pos - 1, pos):
            if p < 0 or p >= self.document().characterCount() - 1:
                continue
            probe = QTextCursor(self.document())
            probe.setPosition(p)
            caret_rect = self.cursorRect(probe)
            char_cur = QTextCursor(self.document())
            char_cur.setPosition(p)
            char_cur.movePosition(
                QTextCursor.MoveOperation.Right,
                QTextCursor.MoveMode.KeepAnchor,
                1,
            )
            ch = char_cur.selectedText()
            if ch not in ("☐", "☑"):
                continue
            fmt = char_cur.charFormat()
            font = fmt.font()
            if font.pointSizeF() <= 0:
                font = self.font()
            width = QFontMetricsF(font).horizontalAdvance(ch)
            if (caret_rect.left() - 1 <= point.x() <= caret_rect.left() + width + 1
                    and caret_rect.top() - 2 <= point.y() <= caret_rect.bottom() + 2):
                return p, ch
        return None

    def _toggle_todo_at(self, pos: int, ch: str):
        """点击 ☐ / ☑：切换字符、颜色，并同步整行删除线与置灰"""
        to_done = ch == "☐"
        doc = self.document()
        cursor = QTextCursor(doc)
        cursor.setPosition(pos)
        cursor.beginEditBlock()

        box_fmt = QTextCharFormat()
        box_fmt.setForeground(QColor("#30d158" if to_done else "#0a84ff"))
        cursor.setPosition(pos)
        cursor.setPosition(pos + 1, QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText("☑" if to_done else "☐", box_fmt)

        # 只处理方框之后的整行文字，保留方框本身的绿/蓝色
        line_cur = QTextCursor(cursor)
        line_cur.movePosition(
            QTextCursor.MoveOperation.EndOfBlock,
            QTextCursor.MoveMode.KeepAnchor,
        )
        txt_fmt = QTextCharFormat()
        txt_fmt.setFontStrikeOut(to_done)
        if to_done:
            txt_fmt.setForeground(QColor("#8e8e93"))
        else:
            # clearForeground() 清不掉已写入的 ForegroundBrush，必须显式写 None
            txt_fmt.setProperty(QTextFormat.Property.ForegroundBrush, None)
        line_cur.mergeCharFormat(txt_fmt)
        cursor.endEditBlock()

    def mousePressEvent(self, event):
        if (event.button() == Qt.MouseButton.LeftButton
                and event.modifiers() == Qt.KeyboardModifier.NoModifier):
            click_cursor = self.cursorForPosition(event.pos())
            hit = self._todo_hit(click_cursor.position(), event.pos())
            if hit is not None:
                self._toggle_todo_at(hit[0], hit[1])
                return
        super().mousePressEvent(event)

    def _handle_plain_enter(self) -> bool:
        """普通回车的新手友好处理，返回 True 表示已自行处理、不再交给 Qt"""
        cursor = self.textCursor()
        if cursor.hasSelection():
            return False
        block = cursor.block()

        # 1) 空引用块按回车：退出引用，恢复普通正文段落
        bfmt = cursor.blockFormat()
        if bfmt.boolProperty(PROP_QUOTE_BLOCK) and not block.text().strip():
            cursor.beginEditBlock()
            # 空引用若同时落在列表里，一并退出列表，否则会卡在“有项目符号的引用”
            cur_list = cursor.currentList()
            if cur_list is not None:
                cur_list.remove(block)
            new_bfmt = _clear_special_block_props(QTextBlockFormat())
            new_bfmt.setTopMargin(0.0)
            new_bfmt.setBottomMargin(6.0)
            cursor.setBlockFormat(new_bfmt)
            cfmt = QTextCharFormat()
            cfmt.setFont(self.document().defaultFont())
            cursor.setCharFormat(cfmt)
            self.setCurrentCharFormat(cfmt)
            cursor.endEditBlock()
            return True

        # 3) 分割线空段落按回车：在下方新建普通正文段，避免继承 HR 标记
        if bfmt.boolProperty(PROP_HR_BLOCK) and not block.text().strip():
            cursor.beginEditBlock()
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
            cursor.insertBlock()
            new_bfmt = _clear_special_block_props(QTextBlockFormat())
            new_bfmt.setTopMargin(0.0)
            new_bfmt.setBottomMargin(6.0)
            cursor.setBlockFormat(new_bfmt)
            cfmt = QTextCharFormat()
            cfmt.setFont(self.document().defaultFont())
            cursor.setCharFormat(cfmt)
            self.setCurrentCharFormat(cfmt)
            cursor.endEditBlock()
            return True

        # 4) 空列表项按回车：退出列表，恢复普通段落（与 Word 行为一致）
        cur_list = cursor.currentList()
        if cur_list is not None and not block.text().strip():
            cursor.beginEditBlock()
            cur_list.remove(block)
            bfmt = cursor.blockFormat()
            bfmt.setIndent(0)
            bfmt.setLeftMargin(0.0)
            cursor.setBlockFormat(bfmt)
            cfmt = QTextCharFormat()
            cfmt.setFont(self.document().defaultFont())
            cfmt.setFontWeight(QFont.Weight.Normal)
            self.setCurrentCharFormat(cfmt)
            cursor.endEditBlock()
            return True

        # 5) 标题行末按回车：新段落自动回落为“正文”样式
        if cursor.blockFormat().headingLevel() > 0 and cursor.atBlockEnd():
            cursor.beginEditBlock()
            cursor.insertBlock()
            bfmt = _clear_special_block_props(QTextBlockFormat())
            bfmt.setHeadingLevel(0)
            bfmt.setTopMargin(0.0)
            bfmt.setBottomMargin(6.0)
            cursor.setBlockFormat(bfmt)
            cfmt = QTextCharFormat()
            cfmt.setFont(self.document().defaultFont())
            cfmt.setFontWeight(QFont.Weight.Normal)
            cursor.setCharFormat(cfmt)
            self.setCurrentCharFormat(cfmt)
            cursor.endEditBlock()
            return True

        return False

    # ---- Markdown 引用块 / 分割线 --------------------------------------------
    def _try_hr_from_dashes(self) -> bool:
        """在空行输入 --- 后按回车，把当前段变成一条分割线"""
        cursor = self.textCursor()
        if cursor.hasSelection() or cursor.currentTable() is not None:
            return False
        block = cursor.block()
        if block.text().strip() != "---":
            return False
        if cursor.positionInBlock() != len(block.text()):
            return False

        cursor.beginEditBlock()
        cursor.movePosition(
            QTextCursor.MoveOperation.StartOfBlock, QTextCursor.MoveMode.KeepAnchor
        )
        cursor.removeSelectedText()
        self._convert_block_to_hr(cursor)
        cursor.endEditBlock()
        self.setFocus()
        return True

    def _convert_block_to_hr(self, cursor: QTextCursor):
        """把 cursor 所在空段落标记为分割线，并在下方新建一个可继续输入的正文段"""
        # 分割线是满栏横线：清掉可能从引用/列表继承来的左缩进，否则 PDF 里会变短
        cur_list = cursor.currentList()
        if cur_list is not None:
            cur_list.remove(cursor.block())
        bfmt = cursor.blockFormat()
        bfmt.setLeftMargin(0.0)
        bfmt.setTextIndent(0.0)
        bfmt.setProperty(PROP_QUOTE_BLOCK, False)
        bfmt.setProperty(PROP_HR_BLOCK, True)
        bfmt.setTopMargin(10.0)
        bfmt.setBottomMargin(10.0)
        cursor.setBlockFormat(bfmt)

        # 分割线本身不占文字，光标落到下一段，符合“画完线继续写”的直觉
        cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
        cursor.insertBlock()
        new_bfmt = _clear_special_block_props(QTextBlockFormat())
        new_bfmt.setTopMargin(0.0)
        new_bfmt.setBottomMargin(6.0)
        cursor.setBlockFormat(new_bfmt)
        cfmt = QTextCharFormat()
        cfmt.setFont(self.document().defaultFont())
        cursor.setCharFormat(cfmt)
        self.setCurrentCharFormat(cfmt)

    def _adjust_indent(self, delta: int):
        cursor = self.textCursor()
        cursor.beginEditBlock()
        block_fmt = cursor.blockFormat()
        if cursor.currentList() is not None:
            # 列表内 Tab 调整层级
            level = max(0, min(8, block_fmt.indent() + delta))
            block_fmt.setIndent(level)
        else:
            # 普通段落按 24px 步进调整左缩进
            margin = max(0.0, block_fmt.leftMargin() + delta * 24.0)
            block_fmt.setLeftMargin(margin)
        cursor.setBlockFormat(block_fmt)
        cursor.endEditBlock()

    def _try_markdown_shortcut(self) -> bool:
        cursor = self.textCursor()
        if cursor.hasSelection() or cursor.currentTable() is not None:
            return False
        block_text = cursor.block().text()
        pos_in_block = cursor.positionInBlock()
        prefix = block_text[:pos_in_block]

        # `- ` 会先被下面的无序列表规则转换成列表，导致用户还没机会输入 [ ]。
        # 所以在列表项里把 `[ ]` / `[]` + 空格识别为待办，并退出列表。
        stripped = block_text.strip()
        if cursor.currentList() is not None and stripped in ("[ ]", "[]"):
            cursor.beginEditBlock()
            cursor.movePosition(
                QTextCursor.MoveOperation.StartOfBlock,
                QTextCursor.MoveMode.KeepAnchor,
            )
            cursor.removeSelectedText()
            cur_list = cursor.currentList()
            if cur_list is not None:
                cur_list.remove(cursor.block())
            bfmt = _clear_special_block_props(cursor.blockFormat())
            bfmt.setIndent(0)
            bfmt.setTextIndent(0.0)
            bfmt.setHeadingLevel(0)
            bfmt.setTopMargin(0.0)
            bfmt.setBottomMargin(6.0)
            cursor.setBlockFormat(bfmt)
            self._insert_todo_box(cursor)
            cursor.endEditBlock()
            return True

        rules = {
            "#": ("heading", 1, 22.0),
            "##": ("heading", 2, 17.0),
            "###": ("heading", 3, 14.0),
            "-": ("list", QTextListFormat.Style.ListDisc, 0),
            "*": ("list", QTextListFormat.Style.ListDisc, 0),
            "1.": ("list", QTextListFormat.Style.ListDecimal, 0),
            ">": ("quote", 0, 0),
            "---": ("hr", 0, 0),
            "[]": ("todo", 0, 0),
            "[ ]": ("todo", 0, 0),
            "- [ ]": ("todo", 0, 0),
        }
        if prefix not in rules:
            return False

        cursor.beginEditBlock()
        cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()

        kind, val, pt = rules[prefix]
        if kind == "todo":
            cur_list = cursor.currentList()
            if cur_list is not None:
                cur_list.remove(cursor.block())
            bfmt = _clear_special_block_props(cursor.blockFormat())
            bfmt.setIndent(0)
            bfmt.setTextIndent(0.0)
            bfmt.setHeadingLevel(0)
            bfmt.setTopMargin(0.0)
            bfmt.setBottomMargin(6.0)
            cursor.setBlockFormat(bfmt)
            self._insert_todo_box(cursor)
        elif kind == "heading":
            bfmt = cursor.blockFormat()
            bfmt.setProperty(PROP_HR_BLOCK, False)
            bfmt.setHeadingLevel(val)
            bfmt.setTopMargin(12.0)
            bfmt.setBottomMargin(6.0)
            cursor.setBlockFormat(bfmt)
            cfmt = QTextCharFormat()
            cfmt.setFontPointSize(pt)
            cfmt.setFontWeight(QFont.Weight.Bold)
            # 用 merge 保留字体族等既有属性，避免标题回落到默认字体
            cursor.mergeCharFormat(cfmt)
            self.mergeCurrentCharFormat(cfmt)
        elif kind == "quote":
            # 引用块：左侧留出 24px 放竖条，上下各留 6px 呼吸空间
            cur_list = cursor.currentList()
            if cur_list is not None:
                cur_list.remove(cursor.block())
            bfmt = cursor.blockFormat()
            bfmt.setLeftMargin(24.0)
            bfmt.setTopMargin(6.0)
            bfmt.setBottomMargin(6.0)
            bfmt.setProperty(PROP_HR_BLOCK, False)
            bfmt.setProperty(PROP_QUOTE_BLOCK, True)
            cursor.setBlockFormat(bfmt)
            # 引用文字用主题灰；暗色纸张用更亮的灰，保证可读
            base = self.palette().base().color()
            quote_color = QColor("#98989d") if base.lightness() < 128 else QColor("#6e6e73")
            cfmt = QTextCharFormat()
            cfmt.setForeground(quote_color)
            cursor.mergeCharFormat(cfmt)
            self.mergeCurrentCharFormat(cfmt)
        elif kind == "hr":
            self._convert_block_to_hr(cursor)
        else:
            # 列表：如果当前块残留分割线标记，先清掉，否则横线会压在列表文字上
            bfmt = cursor.blockFormat()
            bfmt.setProperty(PROP_HR_BLOCK, False)
            cursor.setBlockFormat(bfmt)
            lfmt = QTextListFormat()
            lfmt.setStyle(val)
            cursor.createList(lfmt)

        cursor.endEditBlock()
        return True

    def _insert_todo_box(self, cursor: QTextCursor):
        """在当前块开头插入蓝色 ☐ + 一个空格，并把后续输入格式恢复为正文"""
        box_fmt = QTextCharFormat()
        box_fmt.setForeground(QColor("#0a84ff"))
        cursor.insertText("☐", box_fmt)
        txt_fmt = QTextCharFormat()
        txt_fmt.setFont(self.document().defaultFont())
        cursor.insertText(" ", txt_fmt)
        self.setCurrentCharFormat(txt_fmt)

    def _try_inline_markdown(self) -> bool:
        """空格触发行内 Markdown：**粗体** / *斜体* / `行内代码`"""
        cursor = self.textCursor()
        if cursor.hasSelection() or cursor.currentTable() is not None:
            return False
        block = cursor.block()
        prefix = block.text()[:cursor.positionInBlock()]
        patterns = [
            (r"\*\*(.+?)\*\*$", "bold"),
            (r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)$", "italic"),
            (r"`(.+?)`$", "code"),
        ]
        for pat, style in patterns:
            m = re.search(pat, prefix)
            if not m:
                continue
            inner = m.group(1)
            start = block.position() + m.start()
            end = block.position() + m.end()

            # 取段首字符格式作为基底，保留当前段落的字体族 / 字号；
            # 否则在标题或自定义字号段落里敲 **粗体** 会退回 12pt。
            base_cur = QTextCursor(block)
            base_cur.setPosition(block.position())
            if block.length() > 0:
                base_cur.movePosition(
                    QTextCursor.MoveOperation.NextCharacter,
                    QTextCursor.MoveMode.KeepAnchor,
                )
                base_fmt = base_cur.charFormat()
            else:
                base_fmt = QTextCharFormat(self.document().defaultFont())

            fmt = QTextCharFormat(base_fmt)
            if style == "bold":
                fmt.setFontWeight(QFont.Weight.Bold)
            elif style == "italic":
                fmt.setFontItalic(True)
            elif style == "code":
                fmt.setFontFamilies(["Menlo" if sys.platform == "darwin" else "Consolas"])
                fmt.setForeground(QColor("#ff9f0a"))

            cursor.beginEditBlock()
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()
            cursor.insertText(inner, fmt)

            # 空格后恢复正常格式，避免后续输入一直粘着粗体 / 斜体 / 代码色
            reset_fmt = QTextCharFormat(base_fmt)
            reset_fmt.setFontWeight(QFont.Weight.Normal)
            reset_fmt.setFontItalic(False)
            reset_fmt.setFontStrikeOut(False)
            cursor.insertText(" ", reset_fmt)
            self.setCurrentCharFormat(reset_fmt)
            self.setTextCursor(cursor)
            cursor.endEditBlock()
            return True
        return False

    # ---- 图片自适应与剪贴板净化 ---------------------------------------------
    def get_printable_width(self) -> float:
        """当前纸张真正的可排版宽度（控件宽度 - 左右纸张内边距 - 边框余量）"""
        margins = self.contentsMargins()
        pad_left, pad_right = margins.left(), margins.right()
        # 样式表 padding 尚未生效时（极早期）用主窗口同步过来的纸张内边距兜底；
        # 注意正常情况下 contentsMargins() 已经把 padding + 边框算进去了，不能再叠加
        if pad_left + pad_right <= 0:
            _top, right, _bottom, left = self.page_padding_px
            pad_left, pad_right = left, right
        usable = self.width() - pad_left - pad_right - 2
        return max(200.0, float(usable))

    def insert_scaled_image(self, qimg: QImage, prefix: str = "img"):
        """把图片注册为文档资源，并按当前纸张可用宽度等比缩放后插入"""
        if qimg is None or qimg.isNull():
            return
        res_name = f"{prefix}_{os.urandom(8).hex()}"
        self.document().addResource(
            QTextDocument.ResourceType.ImageResource, QUrl(res_name), qimg
        )
        img_fmt = QTextImageFormat()
        img_fmt.setName(res_name)
        max_w = self.get_printable_width()
        if qimg.width() > max_w:
            img_fmt.setWidth(max_w)
            img_fmt.setHeight(qimg.height() * (max_w / qimg.width()))
        self.textCursor().insertImage(img_fmt)

    def insertFromMimeData(self, source):
        # 0) 编辑器内部复制/剪切：交给 Qt 原生实现，完整保留列表、表格与图片资源
        if source.hasFormat("application/x-qrichtext"):
            super().insertFromMimeData(source)
            return

        # 1) 剪贴板本身就是截图 / 图片：按纸张可用宽度等比缩放
        if source.hasImage():
            img_data = source.imageData()
            if isinstance(img_data, QImage):
                qimg = img_data
            elif isinstance(img_data, QPixmap):
                qimg = img_data.toImage()
            else:
                qimg = QImage(img_data) if img_data is not None else QImage()
            if not qimg.isNull():
                self.insert_scaled_image(qimg, "paste_img")
                return

        # 2) 从 Finder 复制的图片文件（text/uri-list）
        if source.hasUrls():
            for url in source.urls():
                path = url.toLocalFile()
                if path and Path(path).suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
                    qimg = QImage(path)
                    if not qimg.isNull():
                        self.insert_scaled_image(qimg, "paste_img")
                        return

        # 3) 富文本粘贴：清掉网页带来的纯白 / 极暗底块，并限制内嵌图片宽度
        if source.hasHtml():
            tmp_doc = QTextDocument()
            tmp_doc.setHtml(source.html())
            max_w = self.get_printable_width()
            blk = tmp_doc.begin()
            while blk.isValid():
                it = blk.begin()
                while not it.atEnd():
                    frag = it.fragment()
                    if frag.isValid():
                        cur = QTextCursor(tmp_doc)
                        cur.setPosition(frag.position())
                        cur.setPosition(
                            frag.position() + frag.length(),
                            QTextCursor.MoveMode.KeepAnchor,
                        )
                        cfmt = frag.charFormat()
                        if cfmt.isImageFormat():
                            ifmt = cfmt.toImageFormat()
                            # width 为 0 表示按原图尺寸渲染，需要查资源拿真实宽度
                            cur_w = ifmt.width()
                            if cur_w <= 0:
                                res = tmp_doc.resource(
                                    QTextDocument.ResourceType.ImageResource,
                                    QUrl(ifmt.name()),
                                )
                                if isinstance(res, QImage):
                                    cur_w = float(res.width())
                                elif isinstance(res, QPixmap):
                                    cur_w = float(res.width())
                            if cur_w > max_w:
                                ratio = max_w / cur_w
                                ifmt.setWidth(max_w)
                                if ifmt.height() > 0:
                                    ifmt.setHeight(ifmt.height() * ratio)
                                cur.setCharFormat(ifmt)
                        else:
                            # 近黑色字同样交还主题，避免暗色纸张下看不见
                            if (cfmt.hasProperty(QTextCharFormat.Property.ForegroundBrush)
                                    and is_near_black(cfmt.foreground().color())):
                                cfmt.clearForeground()
                            # 只清除“纯白 / 极暗”的网页背景块，保留用户主动设置的高亮色
                            bg = cfmt.background()
                            if bg.style() != Qt.BrushStyle.NoBrush:
                                bg_col = bg.color()
                                if bg_col.isValid():
                                    lum = (bg_col.red() + bg_col.green() + bg_col.blue()) / 3.0
                                    if lum >= 235 or lum <= 30:
                                        cfmt.clearBackground()
                            cur.setCharFormat(cfmt)
                    it += 1
                blk = blk.next()
            self.textCursor().insertFragment(QTextDocumentFragment(tmp_doc))
            return

        super().insertFromMimeData(source)

    def normalize_document_colors(self):
        """导入文档时把近黑字色与纯白/极暗底块交还主题，避免暗色纸张下“黑字黑底/白框”"""
        doc = self.document()
        # 先收集目标片段再统一改格式，避免边遍历边修改导致迭代器失效
        targets = []
        block = doc.begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                if frag.isValid() and frag.length() > 0:
                    cfmt = frag.charFormat()
                    if cfmt.isImageFormat():
                        it += 1
                        continue
                    drop_fg = (
                        cfmt.hasProperty(QTextCharFormat.Property.ForegroundBrush)
                        and is_near_black(cfmt.foreground().color())
                    )
                    drop_bg = False
                    bg = cfmt.background()
                    if bg.style() != Qt.BrushStyle.NoBrush:
                        bg_col = bg.color()
                        if bg_col.isValid():
                            lum = (bg_col.red() + bg_col.green() + bg_col.blue()) / 3.0
                            drop_bg = lum >= 235 or lum <= 30
                    if drop_fg or drop_bg:
                        targets.append(
                            (frag.position(), frag.length(),
                             QTextCharFormat(cfmt), drop_fg, drop_bg)
                        )
                it += 1
            block = block.next()
        if not targets:
            return
        cursor = QTextCursor(doc)
        cursor.beginEditBlock()
        for pos, length, cfmt, drop_fg, drop_bg in targets:
            cursor.setPosition(pos)
            cursor.setPosition(pos + length, QTextCursor.MoveMode.KeepAnchor)
            if drop_fg:
                cfmt.clearForeground()
            if drop_bg:
                cfmt.clearBackground()
            cursor.setCharFormat(cfmt)
        cursor.endEditBlock()

    def mouseDoubleClickEvent(self, event):
        # 双击图片：按版面宽度百分比一键缩放（QTextEdit 原生不支持拖角缩放）
        cursor = self.cursorForPosition(event.pos())
        img_fmt = None
        for move_op in (QTextCursor.MoveOperation.Right, QTextCursor.MoveOperation.Left):
            probe = QTextCursor(cursor)
            if probe.movePosition(move_op, QTextCursor.MoveMode.KeepAnchor, 1):
                if probe.charFormat().isImageFormat():
                    candidate = probe.charFormat().toImageFormat()
                    if candidate.name():
                        img_fmt = candidate
                        cursor = probe
                        break

        if img_fmt is not None:
            max_w = self.get_printable_width()
            res = self.document().resource(
                QTextDocument.ResourceType.ImageResource, QUrl(img_fmt.name())
            )
            if isinstance(res, QPixmap):
                res = res.toImage()
            orig_w = res.width() if isinstance(res, QImage) and not res.isNull() else 0
            orig_h = res.height() if isinstance(res, QImage) and not res.isNull() else 0
            cur_w = img_fmt.width() if img_fmt.width() > 0 else float(orig_w or max_w)
            cur_pct = max(10, min(100, int(round(cur_w / max_w * 100))))
            pct, ok = QInputDialog.getInt(
                self, "调整图片大小", "图片宽度占版面比例 (10% - 100%)：",
                value=cur_pct, min=10, max=100, step=5,
            )
            if ok:
                new_w = max_w * (pct / 100.0)
                img_fmt.setWidth(new_w)
                if orig_w > 0 and orig_h > 0:
                    img_fmt.setHeight(new_w * (orig_h / orig_w))
                cursor.setCharFormat(img_fmt)
                self.setTextCursor(cursor)
                self.viewport().update()
            return

        super().mouseDoubleClickEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        # 按住 Ctrl / Cmd 点击超链接在浏览器中打开
        if event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier):
            href = self.anchorAt(event.pos())
            if href:
                QDesktopServices.openUrl(QUrl(href))

    def paintEvent(self, event):
        super().paintEvent(event)
        # 绘制可视化分页符指示线（用 cursorRect 换算视口坐标，滚动时位置正确）
        painter = QPainter(self.viewport())
        pen = painter.pen()
        pen.setColor(QColor("#0a84ff"))
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        font = painter.font()
        font.setPointSize(8)
        painter.setFont(font)
        fm = painter.fontMetrics()

        block = self.document().begin()
        while block.isValid():
            bf = block.blockFormat()
            # pageBreakPolicy 在 PyQt6 里是单值枚举，不能用 & / int() 比较
            if bf.pageBreakPolicy() == QTextFormat.PageBreakFlag.PageBreak_AlwaysBefore:
                cursor = QTextCursor(block)
                y = self.cursorRect(cursor).top()
                if 0 <= y <= self.viewport().height():
                    painter.drawLine(24, y, self.viewport().width() - 24, y)
                    # 标签放在右侧页边距，避免压住正文首行
                    label = "分页符"
                    painter.drawText(
                        self.viewport().width() - 24 - fm.horizontalAdvance(label),
                        max(12, y - 3),
                        label,
                    )

            # Markdown 引用块：左侧 3px 蓝色竖条
            if bf.boolProperty(PROP_QUOTE_BLOCK):
                start_cursor = QTextCursor(block)
                end_cursor = QTextCursor(block)
                end_cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
                y1 = self.cursorRect(start_cursor).top()
                y2 = self.cursorRect(end_cursor).bottom()
                if y2 >= 0 and y1 <= self.viewport().height():
                    # 注意：paintEvent 的坐标系是 viewport，不是整块 QTextEdit，
                    # 不能再加 contentsMargins().left()，否则竖条会跑到正文里面。
                    text_left = self.cursorRect(start_cursor).left()
                    bar_x = max(2, int(text_left) - 18)
                    painter.fillRect(
                        QRectF(bar_x, y1, 3, max(16.0, y2 - y1)),
                        QColor("#0a84ff"),
                    )

            # Markdown 分割线 ---：绘制一条 1px 灰色横线（只有空段落才算分割线）
            if bf.boolProperty(PROP_HR_BLOCK) and not block.text().strip():
                r = self.cursorRect(QTextCursor(block))
                if r.bottom() >= 0 and r.top() <= self.viewport().height():
                    base = self.palette().base().color()
                    hr_color = QColor("#48484a") if base.lightness() < 128 else QColor("#c7c7cc")
                    painter.setPen(hr_color)
                    # 同样使用 viewport 坐标：横向铺满可视正文栏，两侧各留 8px。
                    x1 = 8
                    x2 = self.viewport().width() - 8
                    y = int(r.center().y())
                    painter.drawLine(int(x1), y, int(x2), y)

            block = block.next()

        # 长卷模式虚拟分页参考线：连续画布没有真实分页，用浅色虚线提示
        # 当前内容大约写到第几页，导出 PDF 前心里有数。
        page_h = getattr(self, "single_page_height", 0)
        if page_h > 100:
            guide_pen = painter.pen()
            guide_pen.setColor(QColor(209, 209, 214, 150))
            guide_pen.setStyle(Qt.PenStyle.DotLine)
            painter.setPen(guide_pen)
            page_idx = 1
            while page_idx * page_h < self.viewport().height() - 40:
                y = page_idx * page_h
                painter.drawLine(16, y, self.viewport().width() - 16, y)
                hint = f"第 {page_idx} 页结束"
                painter.setPen(QColor("#8e8e93"))
                painter.drawText(
                    self.viewport().width() - 24 - fm.horizontalAdvance(hint),
                    y - 4,
                    hint,
                )
                painter.setPen(guide_pen)
                page_idx += 1
        painter.end()

    def contextMenuEvent(self, event):
        menu = self.createStandardContextMenu()
        menu.setStyleSheet("""
            QMenu {
                background-color: #2c2c31; color: #f5f5f7;
                border: 1px solid #42424a; border-radius: 6px; padding: 4px;
            }
            QMenu::item { padding: 5px 18px; border-radius: 4px; }
            QMenu::item:selected { background-color: #0a84ff; color: #ffffff; }
            QMenu::separator { height: 1px; background: #3f3f46; margin: 4px 8px; }
        """)
        menu.addSeparator()

        act_plain = menu.addAction("粘贴为纯文本")
        act_plain.triggered.connect(self._paste_plain_text)

        act_link = menu.addAction("插入 / 编辑超链接 (Ctrl+K)")
        act_link.triggered.connect(self.request_insert_link.emit)

        act_pb = menu.addAction("插入分页符 (Ctrl+Enter)")
        act_pb.triggered.connect(self.request_insert_page_break.emit)

        # 如果光标在表格内，动态挂载完整的表格编辑子菜单
        cursor = self.textCursor()
        table = cursor.currentTable()
        if table is not None:
            menu.addSeparator()
            tbl_menu = menu.addMenu("表格操作")
            cell = table.cellAt(cursor)
            r, c = cell.row(), cell.column()

            tbl_menu.addAction("在上方插入行").triggered.connect(lambda: table.insertRows(r, 1))
            tbl_menu.addAction("在下方插入行").triggered.connect(lambda: table.insertRows(r + 1, 1))
            tbl_menu.addAction("在左侧插入列").triggered.connect(lambda: table.insertColumns(c, 1))
            tbl_menu.addAction("在右侧插入列").triggered.connect(lambda: table.insertColumns(c + 1, 1))
            tbl_menu.addSeparator()
            tbl_menu.addAction("删除当前行").triggered.connect(lambda: table.removeRows(r, 1))
            tbl_menu.addAction("删除当前列").triggered.connect(lambda: table.removeColumns(c, 1))
            tbl_menu.addSeparator()

            act_merge = tbl_menu.addAction("合并选中单元格")
            act_merge.setEnabled(cursor.hasSelection())
            act_merge.triggered.connect(lambda: self._merge_selected_cells(table, cursor))

            tbl_menu.addAction("设置单元格背景色...").triggered.connect(
                lambda: self._set_table_cell_bg(table, cursor)
            )

        menu.exec(event.globalPos())

    def _merge_selected_cells(self, table: QTextTable, cursor: QTextCursor):
        try:
            table.mergeCells(cursor)
        except Exception:
            pass

    def _paste_plain_text(self):
        text = QApplication.clipboard().text()
        if text:
            self.insertPlainText(text)

    def _set_table_cell_bg(self, table: QTextTable, cursor: QTextCursor):
        color = QColorDialog.getColor(QColor("#323238"), self, "选择单元格底色")
        if not color.isValid():
            return
        cell = table.cellAt(cursor)
        fmt = cell.format()
        fmt.setBackground(color)
        cell.setFormat(fmt)


# ============================================================================
# 4. 主窗口：Topos Word（macOS 平面化极简 Word 编辑器，主色 #27272b）
# ============================================================================
class ToposWordWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        # 页面像素尺寸（A4 @ 96 DPI），页面设置会就地覆写这两个实例属性
        self.base_page_width = 794
        self.base_page_height = 1123
        self.current_file: str | None = None
        self.is_dark_paper: bool = False  # 默认白底真实纸张预览，支持一键切换暗色护眼纸张
        self.zoom_steps: int = 0          # 缩放步进 (-3 ~ +6)
        # 防重入标记：避免 documentSizeChanged / 格式同步回流造成布局死循环
        self._adjusting_height: bool = False
        self._syncing_format: bool = False
        # 最近打开文件 / 颜色图标缓存（不落磁盘，只用于当前窗口减少重复渲染）
        self.settings = QSettings("Topos", "ToposWord")
        self._color_icon_cache: dict[tuple[str, int | None], QIcon] = {}

        # 页面配置（纸张 / 方向 / 页边距），同时作用于画布、PDF 与 .docx 导出
        self.page_cfg = {
            "paper": "A4",
            "landscape": False,
            "margin_preset": "normal",
            "margins_mm": (20, 22, 20, 22),
            "header_text": "",
            "page_number": False,
        }
        self._copied_char_format: QTextCharFormat | None = None
        self._applying_painter: bool = False
        # 专注写作模式状态：进入前记住大纲是否开着，退出时只恢复原本可见的面板
        self._focus_mode: bool = False
        self._outline_was_visible: bool = False
        # 全文统计缓存（按 QTextDocument.revision 失效），避免划选时反复扫描全文
        self._stats_revision: int = -1
        self._stats_words: int = 0
        self._stats_chars: int = 0
        # 崩溃恢复改用临时 .docx：toHtml() 无法携带内存中的内嵌图片
        self._autosave_path = os.path.join(tempfile.gettempdir(), "topos_word_recovery.docx")

        self.setWindowTitle("Topos Word — 未命名文档.docx")
        self.resize(1140, 820)
        self.setMinimumSize(820, 560)
        self.setAcceptDrops(True)

        # 注：不在 macOS 上启用 setUnifiedTitleAndToolBarOnMac，
        # 自定义深色工具栏与原生标题栏合并会触发 Cocoa 窗口状态异常
        self._init_ui()
        self._apply_macos_stylesheet()
        self._apply_paper_theme()

        # 专注写作模式：F11 进入 / 退出；Esc 作为兜底出口，避免 macOS 把 F11 抢去显示桌面
        self._focus_shortcut = QShortcut(QKeySequence("F11"), self)
        self._focus_shortcut.activated.connect(self._toggle_focus_mode)
        # macOS 有时会把 F11 抢去“显示桌面”，留一个键盘兜底组合
        self._focus_shortcut_alt = QShortcut(QKeySequence("Ctrl+Shift+F"), self)
        self._focus_shortcut_alt.activated.connect(self._toggle_focus_mode)
        self._focus_escape = QShortcut(QKeySequence("Esc"), self)
        self._focus_escape.setEnabled(False)
        self._focus_escape.activated.connect(self._exit_focus_mode)

        self._update_word_count()
        self._load_welcome_template()

        # 30 秒静默自动备份 + 启动时检查上次异常退出的恢复文件
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(30_000)
        self._autosave_timer.timeout.connect(self._run_autosave)
        self._autosave_timer.start()
        QTimer.singleShot(250, self._check_autosave_recovery)

    # ------------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------------
    def _init_ui(self):
        # 顶部主工具栏（macOS 平面化双组整合）
        self.toolbar = QToolBar("格式工具栏")
        self.toolbar.setMovable(False)
        self.toolbar.setIconSize(QSize(18, 18))
        self.toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.toolbar)

        # 第二行：插入与视图（避免功能过多被折叠进 >> 溢出菜单）
        self.insert_toolbar = QToolBar("插入与视图")
        self.insert_toolbar.setMovable(False)
        self.insert_toolbar.setIconSize(QSize(18, 18))
        self.insert_toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.addToolBarBreak(Qt.ToolBarArea.TopToolBarArea)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.insert_toolbar)

        # 1) 文件操作组
        self.act_new = self._add_action("new", "新建文档 (Ctrl+N)；右侧箭头可选模板", QKeySequence.StandardKey.New, self.new_document)
        self.act_open = self._add_action("open", "打开 Word / 富文本 (Ctrl+O)", QKeySequence.StandardKey.Open, self.open_document)
        self._add_action("save", "保存文档 (Ctrl+S)", QKeySequence.StandardKey.Save, self.save_document)
        self._add_action("pdf", "导出为 PDF", None, self.export_pdf)
        self._add_action("long_img", "导出为高清长图 (PNG)", None, self.export_long_image)
        self._add_action("print", "打印 (Ctrl+P)", QKeySequence.StandardKey.Print, self.print_document)
        self._setup_recent_menu()
        self._setup_new_menu()

        self._add_separator()

        # 2) 历史操作组
        self.act_undo = self._add_action("undo", "撤销 (Ctrl+Z)", QKeySequence.StandardKey.Undo, lambda: self.editor.undo())
        self.act_redo = self._add_action("redo", "重做 (Ctrl+Y)", QKeySequence.StandardKey.Redo, lambda: self.editor.redo())
        # 初始无可撤销内容，先置灰；真正的状态在下方绑定 undoAvailable / redoAvailable
        self.act_undo.setEnabled(False)
        self.act_redo.setEnabled(False)

        self._add_separator()

        # 2.1) 段落样式（正文 / 标题 1-3，写入 headingLevel 驱动大纲）
        self.style_combo = QComboBox()
        self.style_combo.setFixedWidth(90)
        self.style_combo.addItems(["正文", "标题 1", "标题 2", "标题 3"])
        self.style_combo.currentIndexChanged.connect(self._on_paragraph_style_changed)
        self.toolbar.addWidget(self.style_combo)

        # 3) 字体与字号选择
        # 只加载可缩放矢量字体并限制下拉可见项，避免 macOS 遍历特殊字库时崩溃
        self.font_combo = QFontComboBox()
        self.font_combo.setFontFilters(QFontComboBox.FontFilter.ScalableFonts)
        self.font_combo.setMaxVisibleItems(16)
        self.font_combo.setFixedWidth(148)
        self.font_combo.setCurrentFont(QFont("PingFang SC" if sys.platform == "darwin" else "Microsoft YaHei"))
        self.font_combo.currentFontChanged.connect(self._on_font_family_changed)
        self.toolbar.addWidget(self.font_combo)

        self.size_combo = QComboBox()
        self.size_combo.setFixedWidth(68)
        self.size_combo.setEditable(True)
        for sz in ["9", "10", "11", "12", "14", "16", "18", "20", "24", "28", "36", "48"]:
            self.size_combo.addItem(sz)
        self.size_combo.setCurrentText("12")
        self.size_combo.currentTextChanged.connect(self._on_font_size_changed)
        self.toolbar.addWidget(self.size_combo)

        self._add_separator()

        # 4) 字符样式组 (B / I / U / S / 颜色 / 高亮)
        self.act_bold = self._add_action("bold", "加粗 (Ctrl+B)", QKeySequence.StandardKey.Bold, self._toggle_bold, checkable=True)
        self.act_italic = self._add_action("italic", "斜体 (Ctrl+I)", QKeySequence.StandardKey.Italic, self._toggle_italic, checkable=True)
        self.act_underline = self._add_action("underline", "下划线 (Ctrl+U)", QKeySequence.StandardKey.Underline, self._toggle_underline, checkable=True)
        self.act_strike = self._add_action("strike", "删除线", None, self._toggle_strike, checkable=True)

        self._add_separator()

        # 5) 段落对齐组
        self.act_align_left = self._add_action("align_left", "左对齐", None, lambda: self._set_alignment(Qt.AlignmentFlag.AlignLeft), checkable=True)
        self.act_align_center = self._add_action("align_center", "居中对齐", None, lambda: self._set_alignment(Qt.AlignmentFlag.AlignCenter), checkable=True)
        self.act_align_right = self._add_action("align_right", "右对齐", None, lambda: self._set_alignment(Qt.AlignmentFlag.AlignRight), checkable=True)
        self.act_align_justify = self._add_action("align_justify", "两端对齐", None, lambda: self._set_alignment(Qt.AlignmentFlag.AlignJustify), checkable=True)
        self.act_align_left.setChecked(True)

        # 中文排版刚需：一键首行缩进 2 字符（按当前字号动态计算，随导出同步到 Word）
        self.act_first_indent = self._add_action(
            "indent_first", "首行缩进 2 字符", None,
            self._toggle_first_line_indent, checkable=True
        )

        self._add_separator()

        # 6) 列表、插图、行距与表格
        self._add_action("bullet_list", "无序项目符号列表", None, lambda: self._toggle_list(QTextListFormat.Style.ListDisc))
        self._add_action("number_list", "有序编号列表", None, lambda: self._toggle_list(QTextListFormat.Style.ListDecimal))
        self._add_action("image", "插入本地图片", None, self.insert_image)

        self.line_spacing_combo = QComboBox()
        self.line_spacing_combo.setFixedWidth(74)
        self.line_spacing_combo.setToolTip("段落行距")
        self.line_spacing_combo.addItems(["1.0x", "1.15x", "1.5x", "2.0x"])
        self.line_spacing_combo.setCurrentText("1.0x")
        self.line_spacing_combo.currentTextChanged.connect(self._on_line_spacing_changed)
        self.toolbar.addWidget(self.line_spacing_combo)

        # 第二行：文字效果 / 表格 / 超链接 / 分页符 / 页面设置 + 查找 / 大纲 / 纸张
        self.act_text_color = self._add_action(
            "text_color", "字体颜色（点右侧箭头选快捷色）", None,
            self._pick_text_color, toolbar=self.insert_toolbar
        )
        self.act_highlight = self._add_action(
            "highlight", "文本高亮底色（点右侧箭头选快捷色）", None,
            self._pick_highlight_color, toolbar=self.insert_toolbar
        )
        self._setup_color_menus()
        self.act_painter = self._add_action(
            "painter", "格式刷（选中源文本后点击，再划选目标文本）", None,
            self._activate_format_painter, checkable=True, toolbar=self.insert_toolbar
        )
        self._add_action("clear_fmt", "清除选中文本格式", None, self._clear_formatting, toolbar=self.insert_toolbar)

        self._add_separator(self.insert_toolbar)

        self._add_action("table", "插入表格", None, self.insert_table, toolbar=self.insert_toolbar)
        self._add_action("link", "插入 / 编辑超链接 (Ctrl+K)", QKeySequence("Ctrl+K"), self.insert_hyperlink, toolbar=self.insert_toolbar)
        self._add_action("page_break", "插入分页符 (Ctrl+Enter)", None, self.insert_page_break, toolbar=self.insert_toolbar)
        self._add_action("page_setup", "页面设置（纸张 / 方向 / 页边距）", None, self.open_page_setup, toolbar=self.insert_toolbar)
        self._add_action(
            "auto_format", "一键规范化排版（清理空行 / 首行缩进 / 1.5x 行距）",
            None, self.auto_format_document, toolbar=self.insert_toolbar
        )

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.insert_toolbar.addWidget(spacer)

        self._add_action("search", "查找与替换 (Ctrl+F)", QKeySequence.StandardKey.Find, self._toggle_find_bar, toolbar=self.insert_toolbar)
        self.act_outline = self._add_action(
            "outline", "显示/隐藏文档大纲", None, self._toggle_outline,
            checkable=True, toolbar=self.insert_toolbar
        )
        self._add_action("theme", "切换纸张背景（白底 / 暗色护眼）", None, self._toggle_paper_theme, toolbar=self.insert_toolbar)

        # 中央主容器
        central = QWidget()
        central.setObjectName("CentralRoot")
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # 悬浮查找栏
        self.editor = SmartPageEditor()
        self.editor.setObjectName("PaperCanvas")
        self.editor.setAcceptRichText(True)
        self.editor.setAcceptDrops(False)  # 文件拖拽交给主窗口统一处理
        self.editor.viewport().setAcceptDrops(False)
        self.editor.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.editor.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.editor.setFixedWidth(self.base_page_width)
        self.editor.setMinimumHeight(self.base_page_height)
        self.editor.request_insert_link.connect(self.insert_hyperlink)
        self.editor.request_insert_page_break.connect(self.insert_page_break)
        self.editor.request_zoom.connect(self._change_zoom)

        default_font = QFont("PingFang SC" if sys.platform == "darwin" else "Microsoft YaHei", 12)
        self.editor.setFont(default_font)
        self.editor.document().setDefaultFont(default_font)

        self.find_bar = FindReplaceBar(self.editor, self)
        root_layout.addWidget(self.find_bar)

        # 外层平滑滚动工作区
        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("WorkspaceScroll")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        page_holder = QWidget()
        page_holder.setObjectName("PageHolder")
        holder_layout = QVBoxLayout(page_holder)
        holder_layout.setContentsMargins(32, 28, 32, 48)
        holder_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        holder_layout.addWidget(self.editor)

        self.scroll_area.setWidget(page_holder)
        self.scroll_area.setAcceptDrops(False)

        # 大纲侧边栏 + 滚动编辑区（水平分割，默认收起）
        self.outline_sidebar = OutlineSidebar(self.editor, self.scroll_area, self)
        workspace_split = QSplitter(Qt.Orientation.Horizontal)
        workspace_split.setObjectName("WorkspaceSplit")
        workspace_split.setHandleWidth(1)
        workspace_split.addWidget(self.outline_sidebar)
        workspace_split.addWidget(self.scroll_area)
        workspace_split.setStretchFactor(0, 0)
        workspace_split.setStretchFactor(1, 1)
        workspace_split.setCollapsible(0, True)
        root_layout.addWidget(workspace_split)
        self.workspace_split = workspace_split

        # 防抖定时器：输入停止 300ms 后刷新大纲
        self._outline_timer = QTimer(self)
        self._outline_timer.setSingleShot(True)
        self._outline_timer.setInterval(300)
        self._outline_timer.timeout.connect(self.outline_sidebar.refresh)

        self.setCentralWidget(central)

        # 底部状态栏（#27272b 统一视觉）
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

        self.lbl_format = QLabel("格式: DOCX")
        self.lbl_stats = QLabel("字数: 0  |  字符: 0")
        self.lbl_paper_mode = QLabel("纸张: 经典白底")

        btn_zoom_out = QPushButton("−")
        btn_zoom_out.setObjectName("ZoomBtn")
        btn_zoom_out.setFixedSize(22, 20)
        btn_zoom_out.clicked.connect(lambda: self._change_zoom(-1))

        self.lbl_zoom = QLabel("100%")
        self.lbl_zoom.setFixedWidth(42)
        self.lbl_zoom.setAlignment(Qt.AlignmentFlag.AlignCenter)

        btn_zoom_in = QPushButton("+")
        btn_zoom_in.setObjectName("ZoomBtn")
        btn_zoom_in.setFixedSize(22, 20)
        btn_zoom_in.clicked.connect(lambda: self._change_zoom(1))

        self.status_bar.addWidget(self.lbl_format)
        self.status_bar.addWidget(self._make_status_sep())
        self.status_bar.addWidget(self.lbl_stats)
        self.status_bar.addPermanentWidget(self.lbl_paper_mode)
        self.status_bar.addPermanentWidget(self._make_status_sep())
        self.status_bar.addPermanentWidget(btn_zoom_out)
        self.status_bar.addPermanentWidget(self.lbl_zoom)
        self.status_bar.addPermanentWidget(btn_zoom_in)

        # 信号绑定
        self.editor.textChanged.connect(self._update_word_count)
        self.editor.selectionChanged.connect(self._update_word_count)
        self.editor.textChanged.connect(self._outline_timer.start)
        self.editor.undoAvailable.connect(self.act_undo.setEnabled)
        self.editor.redoAvailable.connect(self.act_redo.setEnabled)
        self.editor.currentCharFormatChanged.connect(self._sync_toolbar_char_format)
        self.editor.cursorPositionChanged.connect(self._sync_toolbar_block_format)
        self.editor.cursorPositionChanged.connect(self._ensure_cursor_visible)
        self.editor.selectionChanged.connect(self._apply_format_painter_if_active)
        self.editor.document().documentLayout().documentSizeChanged.connect(self._adjust_page_height)
        self.editor.document().modificationChanged.connect(self._update_window_title)
        self._sync_color_buttons()

    def _add_action(self, icon_name: str, tooltip: str, shortcut, callback,
                    checkable: bool = False, toolbar: QToolBar | None = None) -> QAction:
        target_toolbar = toolbar or self.toolbar
        action = QAction(make_svg_icon(icon_name), tooltip, self)
        action.setToolTip(tooltip)
        action.setCheckable(checkable)
        if shortcut:
            action.setShortcut(shortcut)
        action.triggered.connect(callback)
        target_toolbar.addAction(action)
        return action

    def _apply_menu_style(self, menu: QMenu):
        menu.setStyleSheet("""
            QMenu {
                background-color: #2c2c31; color: #f5f5f7;
                border: 1px solid #42424a; border-radius: 6px; padding: 4px;
            }
            QMenu::item { padding: 5px 14px; border-radius: 4px; }
            QMenu::item:selected { background-color: #0a84ff; }
            QMenu::separator { height: 1px; background: #3a3a42; margin: 4px 8px; }
        """)

    # ---------------------- 最近打开文件 ----------------------
    def _setup_recent_menu(self):
        self.recent_menu = QMenu(self)
        self._apply_menu_style(self.recent_menu)
        self.act_open.setMenu(self.recent_menu)
        btn = self.toolbar.widgetForAction(self.act_open)
        if isinstance(btn, QToolButton):
            btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self._refresh_recent_menu()

    def _setup_new_menu(self):
        """新建按钮主区域仍新建空白文档，右侧箭头打开精选模板库"""
        self.new_menu = QMenu(self)
        self._apply_menu_style(self.new_menu)
        self.new_menu.addAction("空白文档").triggered.connect(self.new_document)
        self.new_menu.addSeparator()
        self.new_menu.addAction("会议纪要模板").triggered.connect(
            lambda _checked=False: self._load_builtin_template("meeting")
        )
        self.new_menu.addAction("工作周报模板").triggered.connect(
            lambda _checked=False: self._load_builtin_template("weekly")
        )
        self.new_menu.addAction("正式通知模板").triggered.connect(
            lambda _checked=False: self._load_builtin_template("notice")
        )
        self.act_new.setMenu(self.new_menu)
        btn = self.toolbar.widgetForAction(self.act_new)
        if isinstance(btn, QToolButton):
            btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)

    def _recent_files(self) -> list[str]:
        raw = self.settings.value("recent_files", [])
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            return []
        files = []
        for p in raw:
            if not p:
                continue
            abs_path = os.path.abspath(str(p))
            if os.path.exists(abs_path) and abs_path not in files:
                files.append(abs_path)
        return files[:8]

    def _push_recent_file(self, path: str):
        if not path:
            return
        abs_path = os.path.abspath(path)
        files = [p for p in self._recent_files() if p != abs_path]
        files.insert(0, abs_path)
        self.settings.setValue("recent_files", files[:8])
        if hasattr(self, "recent_menu"):
            self._refresh_recent_menu()

    def _refresh_recent_menu(self):
        if not hasattr(self, "recent_menu"):
            return
        self.recent_menu.clear()
        files = self._recent_files()
        if not files:
            empty = self.recent_menu.addAction("暂无最近打开的文档")
            empty.setEnabled(False)
            return
        for p in files:
            act = self.recent_menu.addAction(os.path.basename(p))
            act.setToolTip(p)
            act.triggered.connect(lambda _checked=False, fp=p: self._open_recent(fp))
        self.recent_menu.addSeparator()
        clear_act = self.recent_menu.addAction("清空历史记录")
        clear_act.triggered.connect(self._clear_recent_files)

    def _clear_recent_files(self):
        self.settings.setValue("recent_files", [])
        self._refresh_recent_menu()

    def _open_recent(self, path: str):
        if not os.path.exists(path):
            self._refresh_recent_menu()
            self.status_bar.showMessage("文件已不存在，已从最近列表移除", 3000)
            return
        if self._maybe_save_changes():
            self._load_file_from_path(path)

    # ---------------------- 颜色按钮色条 / 快捷色板 ----------------------
    def _color_icon_cached(self, name: str, color: QColor | None) -> QIcon:
        key = (name, color.rgba() if color is not None and color.isValid() else None)
        icon = self._color_icon_cache.get(key)
        if icon is None:
            icon = make_color_bar_icon(name, color)
            self._color_icon_cache[key] = icon
        return icon

    def _build_color_menu(self, is_highlight: bool) -> QMenu:
        presets = [
            ("默认 / 清除颜色", None),
            ("经典黑 (#1d1d1f)", "#1d1d1f"),
            ("高级灰 (#6e6e73)", "#6e6e73"),
            ("苹果蓝 (#0a84ff)", "#0a84ff"),
            ("翡翠绿 (#30d158)", "#30d158"),
            ("警示红 (#ff453a)", "#ff453a"),
            ("暖阳橙 (#ff9f0a)", "#ff9f0a"),
            ("荧光黄 (#fde047)", "#fde047"),
        ]
        menu = QMenu(self)
        self._apply_menu_style(menu)
        for label, hex_val in presets:
            if hex_val is None and is_highlight:
                label = "默认 / 清除高亮"
            act = menu.addAction(label)
            if hex_val:
                sw = QPixmap(12, 12)
                sw.fill(QColor(hex_val))
                act.setIcon(QIcon(sw))
            act.triggered.connect(
                lambda _checked=False, h=hex_val, hl=is_highlight: self._apply_preset_color(h, hl)
            )
        menu.addSeparator()
        custom = menu.addAction("更多自定义颜色...")
        custom.triggered.connect(self._pick_highlight_color if is_highlight else self._pick_text_color)
        return menu

    def _setup_color_menus(self):
        self.act_text_color.setMenu(self._build_color_menu(False))
        self.act_highlight.setMenu(self._build_color_menu(True))
        for act in (self.act_text_color, self.act_highlight):
            btn = self.insert_toolbar.widgetForAction(act)
            if isinstance(btn, QToolButton):
                btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self._sync_color_buttons()

    def _apply_preset_color(self, hex_val: str | None, is_highlight: bool):
        fmt = QTextCharFormat()
        color = QColor(hex_val) if hex_val else None
        if is_highlight:
            if color is not None:
                fmt.setBackground(color)
            else:
                # clearBackground() 无法清掉已有底色，必须显式把 brush 属性写成 None
                fmt.setProperty(QTextFormat.Property.BackgroundBrush, None)
        else:
            if color is not None:
                fmt.setForeground(color)
            else:
                fmt.setProperty(QTextFormat.Property.ForegroundBrush, None)
        self._merge_char_format(fmt)
        self._sync_color_buttons()

    def _sync_color_buttons(self, fmt: QTextCharFormat | None = None):
        if not hasattr(self, "editor") or not hasattr(self, "act_text_color"):
            return
        cf = fmt if fmt is not None else self.editor.textCursor().charFormat()
        fg_brush = cf.foreground()
        fg = fg_brush.color() if fg_brush.style() != Qt.BrushStyle.NoBrush else None
        bg_brush = cf.background()
        bg = bg_brush.color() if bg_brush.style() != Qt.BrushStyle.NoBrush else None
        self.act_text_color.setIcon(self._color_icon_cached("text_color", fg))
        self.act_highlight.setIcon(self._color_icon_cached("highlight", bg))


    def _add_separator(self, toolbar: QToolBar | None = None):
        target_toolbar = toolbar or self.toolbar
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setObjectName("ToolbarDivider")
        sep.setFixedSize(1, 20)
        container = QWidget()
        lay = QHBoxLayout(container)
        lay.setContentsMargins(6, 0, 6, 0)
        lay.addWidget(sep)
        target_toolbar.addWidget(container)

    def _make_status_sep(self) -> QLabel:
        lbl = QLabel("•")
        lbl.setStyleSheet("color: #52525b; padding: 0 4px;")
        return lbl

    # ------------------------------------------------------------------------
    # macOS 平面化样式表（主色 #27272b）
    # ------------------------------------------------------------------------
    def _apply_macos_stylesheet(self):
        self.setStyleSheet("""
            QMainWindow, QWidget#CentralRoot {
                background-color: #27272b;
                color: #f5f5f7;
                font-family: -apple-system, "PingFang SC", "SF Pro Text", "Microsoft YaHei", sans-serif;
            }
            QToolBar {
                background-color: #27272b;
                border: none;
                border-bottom: 1px solid #38383f;
                padding: 4px 8px;
                spacing: 2px;
            }
            QToolButton {
                background-color: transparent;
                border: 1px solid transparent;
                border-radius: 6px;
                padding: 4px;
                margin: 0px;
            }
            QToolButton:hover {
                background-color: #34343a;
            }
            QToolButton:pressed {
                background-color: #3f3f46;
            }
            QToolButton:checked {
                background-color: #0a84ff;
                border: 1px solid #0a84ff;
            }
            QFrame#ToolbarDivider {
                background-color: #3a3a42;
                border: none;
            }
            QFontComboBox, QComboBox {
                background-color: #323238;
                color: #f5f5f7;
                border: 1px solid #3f3f46;
                border-radius: 6px;
                padding: 3px 8px;
                font-size: 12px;
                min-height: 22px;
            }
            QFontComboBox:hover, QComboBox:hover {
                border-color: #52525b;
                background-color: #383840;
            }
            QFontComboBox:focus, QComboBox:focus {
                border-color: #0a84ff;
            }
            QComboBox::drop-down {
                border: none;
                width: 18px;
            }
            QComboBox QAbstractItemView {
                background-color: #2c2c31;
                color: #f5f5f7;
                border: 1px solid #42424a;
                selection-background-color: #0a84ff;
                selection-color: #ffffff;
                outline: 0;
            }
            QSplitter#WorkspaceSplit {
                background-color: #1f1f23;
            }
            QSplitter#WorkspaceSplit::handle {
                background-color: #38383f;
            }
            QScrollArea#WorkspaceScroll, QWidget#PageHolder {
                background-color: #1f1f23;
                border: none;
            }
            QFrame#FindReplaceBar {
                background-color: #27272b;
                border-bottom: 1px solid #38383f;
            }
            QFrame#FindReplaceBar QLineEdit {
                background-color: #323238;
                color: #f5f5f7;
                border: 1px solid #3f3f46;
                border-radius: 6px;
                padding: 4px 8px;
                font-size: 12px;
            }
            QFrame#FindReplaceBar QLineEdit:focus {
                border-color: #0a84ff;
            }
            QFrame#FindReplaceBar QPushButton {
                background-color: #323238;
                color: #e5e5ea;
                border: 1px solid #3f3f46;
                border-radius: 6px;
                padding: 4px 10px;
                font-size: 12px;
            }
            QFrame#FindReplaceBar QPushButton:hover {
                background-color: #3e3e46;
            }
            QStatusBar {
                background-color: #27272b;
                color: #98989d;
                border-top: 1px solid #38383f;
                font-size: 12px;
                padding: 2px 12px;
            }
            QStatusBar::item {
                border: none;
            }
            QPushButton#ZoomBtn {
                background-color: #323238;
                color: #e5e5ea;
                border: 1px solid #3f3f46;
                border-radius: 4px;
                font-weight: bold;
            }
            QPushButton#ZoomBtn:hover {
                background-color: #404048;
            }
            QScrollBar:vertical {
                background: transparent;
                width: 9px;
                margin: 2px;
            }
            QScrollBar::handle:vertical {
                background: #46464f;
                border-radius: 4px;
                min-height: 28px;
            }
            QScrollBar::handle:vertical:hover {
                background: #5c5c66;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: none;
                height: 0px;
            }
        """)

    def _apply_paper_theme(self):
        """切换纸张视觉模式：真实白底纸张 vs 深夜暗色护眼纸张，页边距跟随页面设置"""
        t, r, b, l = self.page_cfg["margins_mm"]
        scale = 1.0 + self.zoom_steps * 0.1
        pt, pr, pb, pl = (int(t * 3.2 * scale), int(r * 3.2 * scale),
                          int(b * 3.2 * scale), int(l * 3.2 * scale))
        # 同步给画布，粘贴 / 拖入 / 双击缩放图片时据此计算正文栏宽
        self.editor.page_padding_px = (pt, pr, pb, pl)
        if self.is_dark_paper:
            self.editor.setStyleSheet(f"""
                QTextEdit#PaperCanvas {{
                    background-color: #27272b;
                    color: #e5e5ea;
                    border: 1px solid #3a3a42;
                    border-radius: 4px;
                    padding: {pt}px {pr}px {pb}px {pl}px;
                    selection-background-color: #0a84ff;
                    selection-color: #ffffff;
                }}
            """)
            self.lbl_paper_mode.setText("纸张: 暗色护眼")
        else:
            self.editor.setStyleSheet(f"""
                QTextEdit#PaperCanvas {{
                    background-color: #ffffff;
                    color: #1d1d1f;
                    border: 1px solid #34343a;
                    border-radius: 3px;
                    padding: {pt}px {pr}px {pb}px {pl}px;
                    selection-background-color: #0a84ff;
                    selection-color: #ffffff;
                }}
            """)
            self.lbl_paper_mode.setText("纸张: 经典白底")

        # 长卷虚拟分页参考线：单页像素高度随缩放 / 纸张尺寸同步
        self.editor.single_page_height = int(self.base_page_height * scale)
        self.editor.viewport().update()

    def _toggle_paper_theme(self):
        self.is_dark_paper = not self.is_dark_paper
        self._apply_paper_theme()

    # ------------------------------------------------------------------------
    # 文档尺寸自适应与滚动跟踪
    # ------------------------------------------------------------------------
    def _adjust_page_height(self, doc_size):
        if self._adjusting_height:
            return
        self._adjusting_height = True
        try:
            scale = 1.0 + self.zoom_steps * 0.1
            min_h = int(self.base_page_height * scale)
            doc_h = int(doc_size.height()) if doc_size and doc_size.height() > 0 else 0
            target_h = max(min_h, doc_h + 140)
            # 差值小于阈值时不动布局，避免反复触发 documentSizeChanged 造成死循环
            if abs(self.editor.height() - target_h) > 4:
                self.editor.setFixedHeight(target_h)
        finally:
            self._adjusting_height = False

    def _ensure_cursor_visible(self):
        crect = self.editor.cursorRect()
        pos_in_holder = self.editor.mapTo(self.scroll_area.widget(), crect.center())
        self.scroll_area.ensureVisible(pos_in_holder.x(), pos_in_holder.y(), 50, 80)

    def _change_zoom(self, delta: int):
        new_steps = max(-3, min(6, self.zoom_steps + delta))
        actual_delta = new_steps - self.zoom_steps
        if actual_delta == 0:
            return
        self.zoom_steps = new_steps
        if actual_delta > 0:
            self.editor.zoomIn(actual_delta)
        else:
            self.editor.zoomOut(-actual_delta)

        scale = 1.0 + self.zoom_steps * 0.1
        self.editor.setFixedWidth(int(self.base_page_width * scale))
        self.lbl_zoom.setText(f"{int(round(scale * 100))}%")
        # 页边距 padding 也按缩放同步，避免放大后版面比例失真
        self._apply_paper_theme()
        self._adjust_page_height(self.editor.document().documentLayout().documentSize())

    # ------------------------------------------------------------------------
    # 富文本格式控制
    # ------------------------------------------------------------------------
    def _merge_char_format(self, fmt: QTextCharFormat):
        if self._syncing_format:
            return
        cursor = self.editor.textCursor()
        if cursor.hasSelection():
            cursor.mergeCharFormat(fmt)
        # 未选中文本时只作用于“后续输入”，不再强选光标下的单词，
        # 避免在图片/特殊字符旁触发格式与信号回流
        self.editor.mergeCurrentCharFormat(fmt)
        # 点击工具栏后把焦点还给正文，方便继续输入；
        # 但正在字号框里键入数字时不抢焦点，否则只能输入一位
        size_edit = self.size_combo.lineEdit()
        if not (self.size_combo.hasFocus() or (size_edit is not None and size_edit.hasFocus())):
            self.editor.setFocus()

    def _on_font_family_changed(self, font: QFont):
        fmt = QTextCharFormat()
        fmt.setFontFamilies([font.family()])
        self._merge_char_format(fmt)

    def _on_font_size_changed(self, size_str: str):
        try:
            pt = float(size_str)
            if 5 <= pt <= 144:
                fmt = QTextCharFormat()
                fmt.setFontPointSize(pt)
                self._merge_char_format(fmt)
        except ValueError:
            pass

    def _toggle_bold(self, checked: bool):
        fmt = QTextCharFormat()
        fmt.setFontWeight(QFont.Weight.Bold if checked else QFont.Weight.Normal)
        self._merge_char_format(fmt)

    def _toggle_italic(self, checked: bool):
        fmt = QTextCharFormat()
        fmt.setFontItalic(checked)
        self._merge_char_format(fmt)

    def _toggle_underline(self, checked: bool):
        fmt = QTextCharFormat()
        fmt.setFontUnderline(checked)
        self._merge_char_format(fmt)

    def _toggle_strike(self, checked: bool):
        fmt = QTextCharFormat()
        fmt.setFontStrikeOut(checked)
        self._merge_char_format(fmt)

    def _pick_text_color(self):
        color = QColorDialog.getColor(self.editor.textColor(), self, "选择文字颜色")
        if color.isValid():
            fmt = QTextCharFormat()
            fmt.setForeground(color)
            self._merge_char_format(fmt)
            self._sync_color_buttons()

    def _pick_highlight_color(self):
        color = QColorDialog.getColor(QColor("#fde047"), self, "选择高亮底色")
        if color.isValid():
            fmt = QTextCharFormat()
            fmt.setBackground(color)
            self._merge_char_format(fmt)
            self._sync_color_buttons()

    def _set_alignment(self, alignment: Qt.AlignmentFlag):
        self.editor.setAlignment(alignment)
        self._sync_toolbar_block_format()

    def _toggle_list(self, style: QTextListFormat.Style):
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        current_list = cursor.currentList()
        if current_list and current_list.format().style() == style:
            # 取消列表
            block_fmt = QTextBlockFormat()
            block_fmt.setIndent(0)
            cursor.setBlockFormat(block_fmt)
            current_list.remove(cursor.block())
        else:
            list_fmt = QTextListFormat()
            list_fmt.setStyle(style)
            cursor.createList(list_fmt)
        cursor.endEditBlock()

    def _toggle_first_line_indent(self, checked: bool):
        """按当前字号把选中段落（或光标所在段）首行缩进 2 个汉字宽度"""
        cursor = self.editor.textCursor()
        pt = cursor.charFormat().fontPointSize()
        if pt <= 0:
            try:
                pt = float(self.size_combo.currentText())
            except Exception:
                pt = self.editor.document().defaultFont().pointSizeF()
        if pt <= 0:
            pt = 12.0

        family = self.font_combo.currentFont().family()
        if not family:
            family = self.editor.document().defaultFont().family()

        dpi = doc_logical_dpi(self.editor.document())
        default_pt = self.editor.document().defaultFont().pointSizeF()
        if default_pt <= 0:
            default_pt = pt
        indent_cache = {}

        def _two_chars_for(cf: QTextCharFormat) -> float:
            """按这一段自己的字号算 2 个汉字宽；选区混排标题/正文时不会互相带偏"""
            block_pt = cf.fontPointSize()
            if block_pt <= 0:
                block_pt = default_pt
            families = cf.fontFamilies()
            block_family = families[0] if families else family
            weight = cf.fontWeight()
            key = (block_family, round(float(block_pt), 1), weight)
            if key not in indent_cache:
                font = QFont(block_family)
                font.setPointSizeF(float(block_pt))
                font.setWeight(weight)
                width = QFontMetricsF(font).horizontalAdvance("汉") * 2.0
                if width <= 0:
                    width = 2.0 * float(block_pt) * dpi / 72.0
                indent_cache[key] = width
            return indent_cache[key]

        doc = self.editor.document()
        start = cursor.selectionStart()
        end = cursor.selectionEnd()
        first_block = doc.findBlock(start)
        last_block = doc.findBlock(end)
        # 选区正好停在下一段开头时，不算选中了下一段
        if end > start and last_block.position() == end:
            prev_block = last_block.previous()
            if prev_block.isValid():
                last_block = prev_block

        cursor.beginEditBlock()
        block = first_block
        while block.isValid():
            bfmt = block.blockFormat()
            if checked:
                block_cursor = QTextCursor(block)
                block_cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
                if block.text():
                    block_cursor.movePosition(
                        QTextCursor.MoveOperation.NextCharacter,
                        QTextCursor.MoveMode.KeepAnchor,
                    )
                two_chars_px = _two_chars_for(block_cursor.charFormat())
                bfmt.setTextIndent(two_chars_px)
            else:
                bfmt.setTextIndent(0.0)
            block_cursor = QTextCursor(block)
            block_cursor.setBlockFormat(bfmt)
            if block == last_block:
                break
            block = block.next()
        cursor.endEditBlock()

        self._sync_toolbar_block_format()
        self.editor.setFocus()

    def insert_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择图片", "", "图片文件 (*.png *.jpg *.jpeg *.webp *.bmp)"
        )
        if not path:
            return
        qimg = QImage(path)
        if qimg.isNull():
            QMessageBox.warning(self, "插图失败", "无法读取所选图片文件。")
            return

        # 统一走画布的自适应插图，自动适配 A4 / A5 / 横向纸张与当前缩放
        self.editor.insert_scaled_image(qimg, "local_img")

    # ---- 段落样式 / 行距 / 格式刷 / 清除格式 ---------------------------------
    def _on_paragraph_style_changed(self, index: int):
        if self._syncing_format:
            return
        presets = {
            0: (0, 12.0, False),  # 正文
            1: (1, 22.0, True),   # 标题 1
            2: (2, 17.0, True),   # 标题 2
            3: (3, 14.0, True),   # 标题 3
        }
        level, pt, is_bold = presets.get(index, (0, 12.0, False))
        cursor = self.editor.textCursor()
        has_selection = cursor.hasSelection()
        if not has_selection:
            cursor.select(QTextCursor.SelectionType.BlockUnderCursor)

        cursor.beginEditBlock()
        block_fmt = cursor.blockFormat()
        block_fmt.setHeadingLevel(level)
        block_fmt.setTopMargin(12.0 if level > 0 else 0.0)
        block_fmt.setBottomMargin(6.0)
        cursor.setBlockFormat(block_fmt)

        char_fmt = QTextCharFormat()
        char_fmt.setFontPointSize(pt)
        char_fmt.setFontWeight(QFont.Weight.Bold if is_bold else QFont.Weight.Normal)
        cursor.mergeCharFormat(char_fmt)
        cursor.endEditBlock()

        if not has_selection:
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
            self.editor.setTextCursor(cursor)
        self.editor.setFocus()
        self.outline_sidebar.refresh()

    def _on_line_spacing_changed(self, text: str):
        try:
            factor = float(text.rstrip("x")) * 100.0
        except ValueError:
            return
        cursor = self.editor.textCursor()
        block_fmt = cursor.blockFormat()
        block_fmt.setLineHeight(factor, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
        cursor.mergeBlockFormat(block_fmt)
        self.editor.setFocus()

    def auto_format_document(self):
        """一键中文排版：清空行 / 去段首段尾空格 / 首行缩进 2 字符 / 1.5 倍行距。
        所有修改包在同一个 editBlock 里，Ctrl+Z 一次即可整体撤销。"""
        doc = self.editor.document()
        trim_chars = " \t\u3000\u00a0"
        dpi = doc_logical_dpi(doc)
        default_pt = doc.defaultFont().pointSizeF()
        if default_pt <= 0:
            default_pt = 12.0
        default_family = doc.defaultFont().family()

        cursor = QTextCursor(doc)
        cursor.beginEditBlock()

        # 第一步：清理正文块首尾的手敲空格；用删字符而不是整段替换，保留粗体/颜色/链接等行内格式
        block = doc.begin()
        while block.isValid():
            cur = QTextCursor(block)
            if cur.currentTable() is None and block.textList() is None:
                bf = block.blockFormat()
                if not bf.boolProperty(PROP_QUOTE_BLOCK) and not bf.boolProperty(PROP_HR_BLOCK):
                    raw = block.text()
                    if raw:
                        trail = len(raw.rstrip(trim_chars))
                        if trail < len(raw):
                            cur.setPosition(block.position() + trail)
                            cur.setPosition(
                                block.position() + len(raw),
                                QTextCursor.MoveMode.KeepAnchor,
                            )
                            cur.removeSelectedText()
                        lead = len(raw) - len(raw.lstrip(trim_chars))
                        if lead > 0:
                            cur.setPosition(block.position())
                            cur.setPosition(
                                block.position() + lead,
                                QTextCursor.MoveMode.KeepAnchor,
                            )
                            cur.removeSelectedText()
            block = block.next()

        # 第二步：合并连续空行；保留每段空行中的“最后一个”，删除更靠前的。
        # 这样删除时只会和相邻空块合并，不会把后面的标题/表格并进空段而丢掉格式。
        # 分割线 / 引用 / 分页符 / 列表 / 表格中的空块都有结构意义，不参与合并。
        empty_positions = []
        empty_run = []

        def _is_plain_empty(blk) -> bool:
            cur = QTextCursor(blk)
            bf = blk.blockFormat()
            return (
                not blk.text().strip()
                and blk.textList() is None
                and cur.currentTable() is None
                and not bf.boolProperty(PROP_QUOTE_BLOCK)
                and not bf.boolProperty(PROP_HR_BLOCK)
                and bf.pageBreakPolicy() != QTextFormat.PageBreakFlag.PageBreak_AlwaysBefore
            )

        block = doc.begin()
        while block.isValid():
            if _is_plain_empty(block):
                empty_run.append(block.position())
            else:
                if len(empty_run) > 1:
                    empty_positions.extend(empty_run[:-1])
                empty_run = []
            block = block.next()
        if len(empty_run) > 1:
            empty_positions.extend(empty_run[:-1])

        for pos in reversed(empty_positions):
            target = doc.findBlock(pos)
            if not target.isValid():
                continue
            cur = QTextCursor(target)
            cur.movePosition(
                QTextCursor.MoveOperation.NextBlock,
                QTextCursor.MoveMode.KeepAnchor,
            )
            cur.removeSelectedText()

        # 第三步：正文段落统一首行缩进 2 字符、1.5 倍行距。
        # 跳过表格、列表、图片块、引用、分割线与空段；标题只清首行缩进。
        # 待办行按正文处理，让清单和普通段落保持同一套缩进 / 行距。
        block = doc.begin()
        while block.isValid():
            cur = QTextCursor(block)
            raw = block.text()
            bf = block.blockFormat()
            if (cur.currentTable() is not None
                    or block.textList() is not None
                    or not raw.strip()
                    or "\ufffc" in raw
                    or bf.boolProperty(PROP_QUOTE_BLOCK)
                    or bf.boolProperty(PROP_HR_BLOCK)):
                block = block.next()
                continue

            if bf.headingLevel() > 0:
                bf.setTextIndent(0.0)
                cur.setBlockFormat(bf)
            else:
                # 按这一段自己的字号算 2 个汉字宽，而不是统一按 12pt
                first_cur = QTextCursor(block)
                first_cur.setPosition(block.position())
                if block.length() > 0:
                    first_cur.movePosition(
                        QTextCursor.MoveOperation.NextCharacter,
                        QTextCursor.MoveMode.KeepAnchor,
                    )
                    cf = first_cur.charFormat()
                else:
                    cf = QTextCharFormat()
                pt = cf.fontPointSize()
                if pt <= 0:
                    pt = default_pt
                families = cf.fontFamilies()
                family = families[0] if families else default_family
                font = QFont(family)
                font.setPointSizeF(float(pt))
                font.setWeight(cf.fontWeight())
                indent_px = QFontMetricsF(font).horizontalAdvance("汉") * 2.0
                if indent_px <= 0:
                    indent_px = 2.0 * float(pt) * dpi / 72.0
                bf.setTextIndent(indent_px)
                bf.setLineHeight(
                    150.0,
                    QTextBlockFormat.LineHeightTypes.ProportionalHeight.value,
                )
                bf.setBottomMargin(6.0)
                cur.setBlockFormat(bf)
            block = block.next()

        cursor.endEditBlock()
        self._sync_toolbar_block_format()
        self.status_bar.showMessage("已完成一键智能排版（按 Ctrl+Z 可撤销）", 3000)
        self.editor.setFocus()

    def _activate_format_painter(self, checked: bool):
        if checked:
            cursor = self.editor.textCursor()
            if cursor.hasSelection():
                # 取选区首字符的格式（charFormat 默认返回光标前一字符）
                probe = QTextCursor(cursor)
                probe.setPosition(cursor.selectionStart() + 1)
                self._copied_char_format = probe.charFormat()
            else:
                self._copied_char_format = self.editor.currentCharFormat()
            self.editor.viewport().setCursor(Qt.CursorShape.CrossCursor)
            self.status_bar.showMessage("格式刷已激活：请划选需要应用格式的文字", 4000)
        else:
            self._copied_char_format = None
            self.editor.viewport().setCursor(Qt.CursorShape.IBeamCursor)

    def _apply_format_painter_if_active(self):
        if self._copied_char_format is None or self._applying_painter:
            return
        self._applying_painter = True
        try:
            cursor = self.editor.textCursor()
            if cursor.hasSelection():
                fmt = QTextCharFormat(self._copied_char_format)
                # 先清空，避免 setCharFormat 触发的 selectionChanged 递归回到这里
                self._copied_char_format = None
                cursor.setCharFormat(fmt)
                self.act_painter.setChecked(False)
                self.editor.viewport().setCursor(Qt.CursorShape.IBeamCursor)
                self.status_bar.showMessage("格式已应用", 2000)
        finally:
            self._applying_painter = False

    def _clear_formatting(self):
        cursor = self.editor.textCursor()
        default_fmt = QTextCharFormat()
        default_fmt.setFont(self.editor.document().defaultFont())

        cursor.beginEditBlock()

        # 1) 字符格式：选区与后续输入都回到文档默认字体
        if cursor.hasSelection():
            cursor.setCharFormat(default_fmt)
        self.editor.setCurrentCharFormat(default_fmt)

        # 2) 列表：选区覆盖的列表段落全部退出列表
        doc = self.editor.document()
        block = doc.findBlock(cursor.selectionStart())
        end_pos = cursor.selectionEnd()
        while block.isValid() and block.position() <= end_pos:
            text_list = block.textList()
            if text_list is not None:
                text_list.remove(block)
            block = block.next()

        # 3) 段落格式：用全新的段落格式彻底清掉标题层级 / 缩进 / 行距 / 页边距，
        #    否则大纲里仍残留旧标题
        bfmt = _clear_special_block_props(QTextBlockFormat())
        bfmt.setBottomMargin(6.0)
        bfmt.setLineHeight(100.0, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
        cursor.setBlockFormat(bfmt)
        cursor.endEditBlock()

        self.editor.setFocus()
        self.outline_sidebar.refresh()
        self._sync_toolbar_block_format()

    # ---- 表格 / 超链接 / 分页符 / 页面设置 / 大纲 ----------------------------
    def insert_table(self):
        dlg = InsertTableDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        rows, cols = dlg.get_dimensions()
        tbl_fmt = QTextTableFormat()
        tbl_fmt.setBorder(1.0)
        tbl_fmt.setBorderBrush(QColor("#8e8e93"))
        tbl_fmt.setCellPadding(8.0)
        tbl_fmt.setCellSpacing(0.0)
        tbl_fmt.setWidth(QTextLength(QTextLength.Type.PercentageLength, 100.0))
        table = self.editor.textCursor().insertTable(rows, cols, tbl_fmt)
        self.editor.setTextCursor(table.cellAt(0, 0).firstCursorPosition())
        self.editor.setFocus()

    def insert_hyperlink(self):
        cursor = self.editor.textCursor()
        selected_text = cursor.selectedText()
        current_href = cursor.charFormat().anchorHref()

        url, ok = QInputDialog.getText(
            self, "插入超链接", "请输入目标网址 (URL)：", text=current_href or "https://"
        )
        if not ok or not url.strip():
            return
        url = url.strip()

        link_fmt = QTextCharFormat()
        link_fmt.setAnchor(True)
        link_fmt.setAnchorHref(url)
        link_fmt.setForeground(QColor("#0a84ff"))
        link_fmt.setFontUnderline(True)

        if selected_text:
            cursor.mergeCharFormat(link_fmt)
        elif current_href and cursor.charFormat().isAnchor():
            # 光标落在已有链接里且没有选区：扩选整段链接再改地址，
            # 避免又插入一条重复链接
            doc = cursor.document()
            start = cursor.selectionStart()
            end = cursor.selectionEnd()

            def _is_same_link(pos: int) -> bool:
                probe = QTextCursor(doc)
                probe.setPosition(pos)
                fmt = probe.charFormat()
                return fmt.isAnchor() and fmt.anchorHref() == current_href

            while start > 0 and _is_same_link(start):
                start -= 1
            while end < doc.characterCount() - 1 and _is_same_link(end + 1):
                end += 1
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            cursor.mergeCharFormat(link_fmt)
        else:
            cursor.insertText(url, link_fmt)
        self.editor.setFocus()

    def insert_page_break(self):
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        cursor.insertBlock()
        bfmt = cursor.blockFormat()
        bfmt.setPageBreakPolicy(QTextFormat.PageBreakFlag.PageBreak_AlwaysBefore)
        cursor.setBlockFormat(bfmt)
        cursor.endEditBlock()
        self.editor.viewport().update()
        self.editor.setFocus()

    def open_page_setup(self):
        dlg = PageSetupDialog(self.page_cfg, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self.page_cfg = dlg.get_config()

        # 按 96 DPI 换算纸张像素尺寸
        dims_px = {"A4": (794, 1123), "Letter": (816, 1056), "A5": (559, 794)}
        w, h = dims_px.get(self.page_cfg["paper"], (794, 1123))
        if self.page_cfg["landscape"]:
            w, h = h, w
        self.base_page_width = w
        self.base_page_height = h

        scale = 1.0 + self.zoom_steps * 0.1
        self.editor.setFixedWidth(int(self.base_page_width * scale))
        self._apply_paper_theme()
        self._adjust_page_height(self.editor.document().documentLayout().documentSize())
        orient_txt = "横向" if self.page_cfg["landscape"] else "纵向"
        self.status_bar.showMessage(
            f"页面已更新：{self.page_cfg['paper']} · {orient_txt}", 3000
        )

    def _toggle_outline(self, checked: bool):
        self.outline_sidebar.setVisible(checked)
        if checked:
            self.outline_sidebar.refresh()

    # ------------------------------------------------------------------------
    # 专注写作模式（F11 / Esc）
    # ------------------------------------------------------------------------
    def _toggle_focus_mode(self):
        if self._focus_mode:
            self._exit_focus_mode()
        else:
            self._enter_focus_mode()

    def _enter_focus_mode(self):
        self._focus_mode = True
        # 用动作的勾选状态记录“用户原本想不想看大纲”，比 isVisible() 更可靠
        self._outline_was_visible = self.act_outline.isChecked()
        self.toolbar.hide()
        self.insert_toolbar.hide()
        self.status_bar.hide()
        self.outline_sidebar.hide()
        self.find_bar.hide()
        self._focus_escape.setEnabled(True)
        self.editor.setFocus()

    def _exit_focus_mode(self):
        if not self._focus_mode:
            return
        self._focus_mode = False
        self.toolbar.show()
        self.insert_toolbar.show()
        self.status_bar.show()
        self.act_outline.setChecked(self._outline_was_visible)
        if self._outline_was_visible:
            self.outline_sidebar.show()
            total = max(400, self.workspace_split.width())
            self.workspace_split.setSizes([220, max(1, total - 220)])
            self.outline_sidebar.refresh()
        else:
            self.outline_sidebar.hide()
        self._focus_escape.setEnabled(False)
        self.status_bar.showMessage("已退出专注模式 (按 F11 可随时切换)", 2500)
        self.editor.setFocus()

    def _sync_toolbar_char_format(self, fmt: QTextCharFormat):
        self._syncing_format = True
        try:
            self.act_bold.setChecked(fmt.fontWeight() >= QFont.Weight.Bold)
            self.act_italic.setChecked(fmt.fontItalic())
            self.act_underline.setChecked(fmt.fontUnderline())
            self.act_strike.setChecked(fmt.fontStrikeOut())

            families = fmt.fontFamilies()
            # 光标回到未显式设置字体的段落时回落到文档默认字体，避免下拉框停在上一处字体
            family_name = families[0] if families else self.editor.document().defaultFont().family()
            self.font_combo.blockSignals(True)
            self.font_combo.setCurrentFont(QFont(family_name))
            self.font_combo.blockSignals(False)

            pt = fmt.fontPointSize()
            if pt <= 0:
                pt = self.editor.document().defaultFont().pointSizeF()
            if pt > 0:
                self.size_combo.blockSignals(True)
                self.size_combo.setCurrentText(str(int(pt) if float(pt).is_integer() else pt))
                self.size_combo.blockSignals(False)
            self._sync_color_buttons(fmt)
        finally:
            self._syncing_format = False

    def _sync_toolbar_block_format(self):
        align = self.editor.alignment()
        self.act_align_left.setChecked(bool(align & Qt.AlignmentFlag.AlignLeft))
        self.act_align_center.setChecked(bool(align & Qt.AlignmentFlag.AlignCenter))
        self.act_align_right.setChecked(bool(align & Qt.AlignmentFlag.AlignRight))
        self.act_align_justify.setChecked(bool(align & Qt.AlignmentFlag.AlignJustify))

        level = self.editor.textCursor().blockFormat().headingLevel()
        self.style_combo.blockSignals(True)
        self.style_combo.setCurrentIndex(level if 0 <= level <= 3 else 0)
        self.style_combo.blockSignals(False)

        # 行距回显（仅处理按比例设置的行距，固定行高 / 最小行高不强行映射）
        bf = self.editor.textCursor().blockFormat()
        pct = 100
        if bf.lineHeightType() == QTextBlockFormat.LineHeightTypes.ProportionalHeight.value:
            pct = int(round(bf.lineHeight()))
        text = {100: "1.0x", 115: "1.15x", 150: "1.5x", 200: "2.0x"}.get(pct, "1.0x")
        self.line_spacing_combo.blockSignals(True)
        self.line_spacing_combo.setCurrentText(text)
        self.line_spacing_combo.blockSignals(False)

        # 首行缩进回显（textIndent 以文档逻辑像素为单位，>1 即视为开启）
        self.act_first_indent.setChecked(bf.textIndent() > 1.0)

        # 光标所在章节 → 大纲自动高亮跟随
        if hasattr(self, "outline_sidebar"):
            self.outline_sidebar.highlight_for_position(self.editor.textCursor().position())

    def _toggle_find_bar(self):
        if self.find_bar.isVisible():
            self.find_bar.close_bar()
        else:
            self.find_bar.open_bar()

    # ------------------------------------------------------------------------
    # 文件打开、保存与导出
    # ------------------------------------------------------------------------
    def _maybe_save_changes(self) -> bool:
        if not self.editor.document().isModified():
            return True
        ret = QMessageBox.question(
            self,
            "未保存的更改",
            "当前文档内容已修改，是否在继续前保存？",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
        )
        if ret == QMessageBox.StandardButton.Save:
            return self.save_document()
        return ret == QMessageBox.StandardButton.Discard

    def new_document(self):
        if not self._maybe_save_changes():
            return
        self.editor.clear()
        self.current_file = None
        # 清空后把“后续输入”的字符格式恢复到文档默认，避免继承上一文档的标题样式
        default_fmt = QTextCharFormat()
        default_fmt.setFont(self.editor.document().defaultFont())
        self.editor.setCurrentCharFormat(default_fmt)
        self.editor.document().setModified(False)
        # 新文档不应还能撤销回上一份文档
        self.editor.document().clearUndoRedoStacks()
        self.lbl_format.setText("格式: DOCX")
        self._update_window_title()
        self._update_word_count()
        self._sync_toolbar_block_format()
        self.find_bar.close_bar()
        # 大纲侧栏如果正开着，必须刷新，否则残留上一文档的标题列表
        self.outline_sidebar.refresh()

    def _load_builtin_template(self, kind: str):
        """从“新建”下拉套用内置模板；仍然走未保存确认与全新文档状态。"""
        if not self._maybe_save_changes():
            return
        templates = {
            "meeting": (
                '<h1 style="font-size:22pt;">项目推进会议纪要</h1>'
                '<p><b>时间：</b>2026 年 __ 月 __ 日 &nbsp;&nbsp;|&nbsp;&nbsp; '
                '<b>地点：</b>线上 / 会议室</p>'
                '<p><b>参会人员：</b>张三、李四、王五</p>'
                '<h2 style="font-size:17pt;">一、核心决议事项</h2>'
                '<ul><li>确定第一阶段交付节点与验收标准；</li>'
                '<li>明确各模块负责人及联调时间。</li></ul>'
                '<h2 style="font-size:17pt;">二、待办任务跟踪</h2>'
                '<p><span style="color:#0a84ff;">☐</span> 完成需求评审定稿（负责人：张三）</p>'
                '<p><span style="color:#0a84ff;">☐</span> 输出技术方案与排期表（负责人：李四）</p>'
            ),
            "weekly": (
                '<h1 style="font-size:22pt;">工作周报（第 __ 周）</h1>'
                '<h2 style="font-size:17pt;">一、本周完成工作</h2>'
                '<ol><li>完成核心功能模块开发与自测；</li>'
                '<li>修复已知体验问题并更新使用文档。</li></ol>'
                '<h2 style="font-size:17pt;">二、下周工作计划</h2>'
                '<ol><li>推进联调测试与性能优化；</li>'
                '<li>整理阶段性汇报材料。</li></ol>'
                '<h2 style="font-size:17pt;">三、风险与需协调事项</h2>'
                '<p>暂无阻塞性问题。</p>'
            ),
            "notice": (
                '<h1 align="center" style="font-size:22pt;">关于 ________ 的通知</h1>'
                '<p>各部门、全体同事：</p>'
                '<p style="text-indent: 24pt;">为进一步规范工作流程、提升协作效率，'
                '现将有关事项通知如下：</p>'
                '<p style="text-indent: 24pt;">一、具体安排：请于本周五前完成相关资料提交。</p>'
                '<p style="text-indent: 24pt;">二、注意事项：如有疑问请及时与综合管理部联系。</p>'
                '<p align="right">行政管理部<br/>2026 年 __ 月 __ 日</p>'
            ),
        }
        html = templates.get(kind)
        if not html:
            return
        self.editor.setHtml(html)
        self.current_file = None
        default_fmt = QTextCharFormat()
        default_fmt.setFont(self.editor.document().defaultFont())
        self.editor.setCurrentCharFormat(default_fmt)
        self.editor.document().setModified(False)
        self.editor.document().clearUndoRedoStacks()
        cur = self.editor.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        self.editor.setTextCursor(cur)
        self.lbl_format.setText("格式: DOCX")
        self._update_window_title()
        self._update_word_count()
        self.find_bar.close_bar()
        self.outline_sidebar.refresh()
        self._sync_toolbar_block_format()
        self.status_bar.showMessage("已载入内置模板，可直接修改占位内容", 3000)

    def open_document(self):
        if not self._maybe_save_changes():
            return
        file_filter = (
            "所有支持文档 (*.docx *.doc *.rtf *.md *.html *.htm *.txt);;"
            "Word 文档 (*.docx *.doc);;"
            "富文本与网页 (*.rtf *.html *.htm);;"
            "Markdown 与纯文本 (*.md *.txt)"
        )
        path, _ = QFileDialog.getOpenFileName(self, "打开文档", "", file_filter)
        if not path:
            return
        self._load_file_from_path(path)

    def _load_file_from_path(self, path: str) -> bool:
        """把指定路径的文档载入编辑器（供打开对话框与文件拖拽复用）"""
        ext = Path(path).suffix.lower()
        tmp_docx = None
        try:
            if ext == ".docx":
                WordDocumentBridge.load_docx_into_qdoc(
                    path, self.editor.document(), self.editor.get_printable_width()
                )
            elif ext in (".doc", ".rtf"):
                tmp_docx = WordDocumentBridge.convert_legacy_to_docx(path)
                WordDocumentBridge.load_docx_into_qdoc(
                    tmp_docx, self.editor.document(), self.editor.get_printable_width()
                )
            elif ext == ".md":
                with open(path, "r", encoding="utf-8") as f:
                    self.editor.setMarkdown(f.read())
                self.editor.normalize_document_colors()
            elif ext in (".html", ".htm"):
                with open(path, "r", encoding="utf-8") as f:
                    self.editor.setHtml(f.read())
                self.editor.normalize_document_colors()
            else:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    self.editor.setPlainText(f.read())

            self.current_file = path
            self.editor.document().setModified(False)
            # 打开文档后清空撤销栈，避免 Ctrl+Z 把整篇新载入的内容撤成空白
            self.editor.document().clearUndoRedoStacks()
            self.lbl_format.setText(f"格式: {ext.lstrip('.').upper()}")
            self._update_window_title()
            self._update_word_count()
            self.outline_sidebar.refresh()
            self._push_recent_file(path)
            return True
        except Exception as exc:
            QMessageBox.critical(self, "打开文档失败", f"无法解析该文件：\n{exc}")
            return False
        finally:
            # 清理 .doc/.rtf 转换产生的临时目录，避免临时文件堆积
            if tmp_docx and os.path.exists(tmp_docx):
                shutil.rmtree(os.path.dirname(tmp_docx), ignore_errors=True)

    # ---- 拖拽打开 / 插图 -----------------------------------------------------
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if not urls:
            return
        path = urls[0].toLocalFile()
        if not path:
            return
        ext = Path(path).suffix.lower()
        if ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
            qimg = QImage(path)
            if qimg.isNull():
                return
            self.editor.insert_scaled_image(qimg, "drop_img")
        elif ext in (".docx", ".doc", ".rtf", ".md", ".html", ".htm", ".txt"):
            if self._maybe_save_changes():
                self._load_file_from_path(path)

    def save_document(self) -> bool:
        if not self.current_file or Path(self.current_file).suffix.lower() in (".doc", ".rtf"):
            return self.save_document_as()
        return self._write_to_path(self.current_file)

    def save_document_as(self) -> bool:
        default_name = Path(self.current_file).stem + ".docx" if self.current_file else "未命名文档.docx"
        file_filter = (
            "Word 文档 (*.docx);;"
            "HTML 富文本 (*.html);;"
            "Markdown 文档 (*.md);;"
            "纯文本 (*.txt)"
        )
        path, _ = QFileDialog.getSaveFileName(self, "保存文档", default_name, file_filter)
        if not path:
            return False
        if not Path(path).suffix:
            path += ".docx"
        return self._write_to_path(path)

    def _write_to_path(self, path: str) -> bool:
        ext = Path(path).suffix.lower()
        try:
            if ext == ".docx":
                WordDocumentBridge.save_qdoc_to_docx(self.editor.document(), path, self.page_cfg)
            elif ext in (".html", ".htm"):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self.editor.toHtml())
            elif ext == ".md":
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self.editor.toMarkdown())
            else:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self.editor.toPlainText())

            self.current_file = path
            self.editor.document().setModified(False)
            self.lbl_format.setText(f"格式: {ext.lstrip('.').upper()}")
            self._update_window_title()
            self.status_bar.showMessage(f"已保存至: {path}", 3500)
            self._clear_autosave()
            self._push_recent_file(path)
            return True
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", f"写入文件时发生错误：\n{exc}")
            return False

    def _normalize_print_clone(self, clone: QTextDocument, content_width: float,
                               default_text_color: str = "#000000",
                               map_light_quote: bool = True):
        """导出前整理克隆文档：默认字色转目标色、超宽图片压进正文栏宽。
        长图导出复用这个方法，但暗色纸默认字色要保留浅色、引用浅灰不要转中灰。"""
        cursor = QTextCursor(clone)
        cursor.beginEditBlock()
        block = clone.begin()
        while block.isValid():
            is_quote = block.blockFormat().boolProperty(PROP_QUOTE_BLOCK)
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                if frag.isValid():
                    fmt = frag.charFormat()
                    if fmt.isImageFormat():
                        # 图片宽度是在屏幕画布上按 96dpi 纸张算的；
                        # 打印页更窄时必须等比缩小，否则会溢出页边距
                        img_fmt = fmt.toImageFormat()
                        if img_fmt.width() > content_width > 0:
                            ratio = content_width / img_fmt.width()
                            img_fmt.setWidth(content_width)
                            img_fmt.setHeight(img_fmt.height() * ratio)
                            cursor.setPosition(frag.position())
                            cursor.setPosition(
                                frag.position() + frag.length(),
                                QTextCursor.MoveMode.KeepAnchor,
                            )
                            cursor.setCharFormat(img_fmt)
                    elif not fmt.hasProperty(QTextCharFormat.Property.ForegroundBrush):
                        # 未显式设色的正文跟随纸张主题（暗色纸是浅色字），
                        # 打印到白纸时必须强制成黑色，否则整页空白
                        cursor.setPosition(frag.position())
                        cursor.setPosition(
                            frag.position() + frag.length(),
                            QTextCursor.MoveMode.KeepAnchor,
                        )
                        default_fmt = QTextCharFormat()
                        target_color = QColor(default_text_color)
                        if not target_color.isValid():
                            target_color = QColor("#000000")
                        default_fmt.setForeground(target_color)
                        cursor.mergeCharFormat(default_fmt)
                    else:
                        # 暗色纸上的浅灰引用文字，打印到白纸时转成可读的中灰
                        fg = fmt.foreground().color()
                        if map_light_quote and is_quote and fg.isValid() and fg.lightness() > 150:
                            cursor.setPosition(frag.position())
                            cursor.setPosition(
                                frag.position() + frag.length(),
                                QTextCursor.MoveMode.KeepAnchor,
                            )
                            quote_fmt = QTextCharFormat()
                            quote_fmt.setForeground(QColor("#6e6e73"))
                            cursor.mergeCharFormat(quote_fmt)
                it += 1
            block = block.next()
        cursor.endEditBlock()

    def _render_document_to_printer(self, printer: QPrinter):
        """把编辑器文档画到打印机 / PDF；有页眉或页码时手动分页叠加"""
        header_text = str(self.page_cfg.get("header_text", "")).strip()
        show_page_num = bool(self.page_cfg.get("page_number", False))

        # 始终走手动分页：Qt 原生 print() 会自动在右下角画一个页码，
        # 导致“取消勾选页码”后仍然出现数字；手动路径才能让开关语义一致。
        doc = self.editor.document()
        doc_dpi = doc_logical_dpi(doc)
        resolution = float(printer.resolution())
        if resolution <= 1:
            resolution = 1200.0
        scale = resolution / doc_dpi

        page_px = printer.pageRect(QPrinter.Unit.DevicePixel)
        paper_px = printer.paperRect(QPrinter.Unit.DevicePixel)
        page_units = QSizeF(page_px.width() / scale, page_px.height() / scale)
        if page_units.width() < 2 or page_units.height() < 2:
            raise RuntimeError("打印机返回的页面尺寸异常")

        clone = doc.clone()
        # 关键：克隆文档必须绑定一个排版设备。否则在 macOS 下 drawContents 会输出空白
        clone.documentLayout().setPaintDevice(self.editor.viewport())
        clone.setPageSize(page_units)
        self._normalize_print_clone(clone, page_units.width())
        total_pages = clone.pageCount()

        painter = QPainter(printer)
        if not painter.isActive():
            raise RuntimeError("无法在打印机上创建绘图设备")
        try:
            world = QTransform().scale(scale, scale)
            meta_font = QFont(
                "PingFang SC" if sys.platform == "darwin" else "Microsoft YaHei", 9
            )
            fm = QFontMetricsF(meta_font)
            top_margin = page_px.top() - paper_px.top()
            bottom_margin = paper_px.bottom() - page_px.bottom()

            for page_index in range(total_pages):
                if page_index > 0:
                    printer.newPage()
                # newPage() 会重置 painter 变换，每页都必须重新应用
                painter.setWorldTransform(world)
                painter.save()
                painter.translate(0, -page_index * page_units.height())
                clone.drawContents(
                    painter,
                    QRectF(
                        0,
                        page_index * page_units.height(),
                        page_units.width(),
                        page_units.height(),
                    ),
                )
                painter.restore()

                # 叠加引用竖条 / 分割线：drawContents 只画文档内容，
                # 编辑器里自绘的标记不会自动出现在 PDF / 打印里。
                painter.save()
                painter.translate(0, -page_index * page_units.height())
                painter.setClipRect(QRectF(0, 0, page_units.width(), page_units.height()))
                block = clone.begin()
                while block.isValid():
                    bf = block.blockFormat()
                    r = clone.documentLayout().blockBoundingRect(block)
                    if bf.boolProperty(PROP_QUOTE_BLOCK):
                        bar_x = max(2, r.left() + bf.leftMargin() - 18)
                        painter.fillRect(
                            QRectF(bar_x, r.top(), 3, max(16.0, r.height())),
                            QColor("#0a84ff"),
                        )
                    if bf.boolProperty(PROP_HR_BLOCK) and not block.text().strip():
                        painter.setPen(QColor("#c7c7cc"))
                        painter.drawLine(
                            int(r.left() + 4),
                            int(r.center().y()),
                            int(r.right() - 4),
                            int(r.center().y()),
                        )
                    block = block.next()
                painter.restore()

                # 页眉页脚用未缩放坐标（设备像素）单独绘制，避免被 scale 放大
                painter.setWorldTransform(QTransform())
                painter.setFont(meta_font)
                painter.setPen(QColor("#8e8e93"))
                if header_text:
                    painter.drawText(
                        QRectF(
                            0,
                            -top_margin / 2 - fm.height() / 2,
                            page_px.width(),
                            fm.height(),
                        ),
                        int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                        header_text,
                    )
                if show_page_num:
                    painter.drawText(
                        QRectF(
                            0,
                            page_px.height() + bottom_margin / 2 - fm.height() / 2,
                            page_px.width(),
                            fm.height(),
                        ),
                        int(Qt.AlignmentFlag.AlignCenter),
                        f"第 {page_index + 1} 页 / 共 {total_pages} 页",
                    )
        finally:
            painter.end()

    def export_pdf(self):
        default_name = (Path(self.current_file).stem if self.current_file else "文档导出") + ".pdf"
        path, _ = QFileDialog.getSaveFileName(self, "导出为 PDF", default_name, "PDF 文件 (*.pdf)")
        if not path:
            return
        try:
            printer = QPrinter(QPrinter.PrinterMode.HighResolution)
            printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
            printer.setOutputFileName(path)
            size_enum = {
                "A4": QPageSize.PageSizeId.A4,
                "Letter": QPageSize.PageSizeId.Letter,
                "A5": QPageSize.PageSizeId.A5,
            }
            printer.setPageSize(QPageSize(size_enum.get(self.page_cfg["paper"], QPageSize.PageSizeId.A4)))
            printer.setPageOrientation(
                QPageLayout.Orientation.Landscape if self.page_cfg["landscape"]
                else QPageLayout.Orientation.Portrait
            )
            t, r, b, l = self.page_cfg["margins_mm"]
            printer.setPageMargins(QMarginsF(l, t, r, b), QPageLayout.Unit.Millimeter)
            self._render_document_to_printer(printer)
            self.status_bar.showMessage(f"PDF 导出成功: {path}", 3500)
            if hasattr(self, "_notify_export"):
                self._notify_export(path)
        except Exception as exc:
            QMessageBox.critical(self, "导出 PDF 失败", f"导出过程中发生错误：\n{exc}")

    def export_long_image(self):
        """把当前文档渲染成一张高清长图（2x 像素），适合微信 / 飞书直接分享。
        版面按当前纸张、页边距和缩放比例排版，避免 150% 视图导出的字号和页宽对不上。"""
        default_name = (Path(self.current_file).stem if self.current_file else "文档长图") + ".png"
        path, _ = QFileDialog.getSaveFileName(self, "导出为高清长图", default_name, "PNG 图片 (*.png)")
        if not path:
            return
        if not Path(path).suffix:
            path += ".png"
        try:
            t, r, b, l = self.page_cfg["margins_mm"]
            scale = 1.0 + self.zoom_steps * 0.1
            pt, pr, pb, pl = (int(t * 3.2 * scale), int(r * 3.2 * scale),
                              int(b * 3.2 * scale), int(l * 3.2 * scale))
            page_w = int(self.base_page_width * scale)
            page_h = int(self.base_page_height * scale)
            content_w = max(80, page_w - pl - pr - 2)

            doc_clone = self.editor.document().clone()
            # 保留屏幕文档的 72 / 96 dpi 版面，避免 clone 换 dpi 后字体和首行缩进比例变化
            doc_clone.documentLayout().setPaintDevice(self.editor.viewport())
            doc_clone.setTextWidth(content_w)
            self._normalize_print_clone(
                doc_clone,
                content_w,
                default_text_color="#e5e5ea" if self.is_dark_paper else "#1d1d1f",
                map_light_quote=not self.is_dark_paper,
            )
            content_h = int(doc_clone.size().height())
            total_h = max(page_h, content_h + pt + pb)

            if total_h > 30000:
                QMessageBox.warning(
                    self, "长图过长",
                    "文档超过 30000 像素高，无法生成单张 PNG。建议先导出 PDF。",
                )
                return
            dpr = 2.0 if total_h * 2 <= 30000 else 1.0
            img = QImage(int(page_w * dpr), int(total_h * dpr), QImage.Format.Format_ARGB32)
            img.setDevicePixelRatio(dpr)
            img.fill(QColor("#27272b" if self.is_dark_paper else "#ffffff"))

            painter = QPainter(img)
            try:
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
                painter.save()
                painter.translate(pl, pt)
                doc_clone.drawContents(painter)
                painter.restore()

                # drawContents 不包含编辑器 paintEvent 里的引用竖条 / 分割线，
                # 长图里手动补一遍，保证分享出去和屏幕看到的一致。
                # 这里坐标是整张图片的页面坐标，不能再叠加一次 pl / pt。
                quote_color = QColor("#0a84ff")
                hr_color = QColor("#48484a") if self.is_dark_paper else QColor("#c7c7cc")
                block = doc_clone.begin()
                while block.isValid():
                    bf = block.blockFormat()
                    rect = doc_clone.documentLayout().blockBoundingRect(block)
                    if bf.boolProperty(PROP_QUOTE_BLOCK):
                        text_left_doc = doc_clone.documentMargin() + bf.leftMargin()
                        bar_x = max(2.0, pl + text_left_doc - 18)
                        painter.fillRect(
                            QRectF(bar_x, pt + rect.top(), 3, max(16.0, rect.height())),
                            quote_color,
                        )
                    if bf.boolProperty(PROP_HR_BLOCK) and not block.text().strip():
                        y = int(pt + rect.center().y())
                        painter.setPen(hr_color)
                        painter.drawLine(8, y, max(8, page_w - 8), y)
                    block = block.next()
            finally:
                painter.end()

            if img.save(path, "PNG"):
                self.status_bar.showMessage(f"高清长图已导出: {path}", 3500)
                if hasattr(self, "_notify_export"):
                    self._notify_export(path)
            else:
                QMessageBox.warning(self, "导出失败", "无法保存长图文件。")
        except Exception as exc:
            QMessageBox.critical(self, "导出长图失败", f"导出过程中发生错误：\n{exc}")

    def print_document(self):
        """Ctrl/Cmd + P：调起系统打印对话框"""
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        if self.current_file:
            printer.setDocName(Path(self.current_file).stem)
        # 打印默认沿用文档页面设置，用户仍可在系统对话框中改纸张
        size_enum = {
            "A4": QPageSize.PageSizeId.A4,
            "Letter": QPageSize.PageSizeId.Letter,
            "A5": QPageSize.PageSizeId.A5,
        }
        printer.setPageSize(QPageSize(size_enum.get(self.page_cfg["paper"], QPageSize.PageSizeId.A4)))
        printer.setPageOrientation(
            QPageLayout.Orientation.Landscape if self.page_cfg["landscape"]
            else QPageLayout.Orientation.Portrait
        )
        t, r, b, l = self.page_cfg["margins_mm"]
        printer.setPageMargins(QMarginsF(l, t, r, b), QPageLayout.Unit.Millimeter)
        dialog = QPrintDialog(printer, self)
        dialog.setWindowTitle("打印文档")
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self._render_document_to_printer(printer)
            self.status_bar.showMessage("已发送到打印机", 3000)
        except Exception as exc:
            QMessageBox.critical(self, "打印失败", f"打印过程中发生错误：\n{exc}")

    # ------------------------------------------------------------------------
    # 状态栏统计与初始示例文档
    # ------------------------------------------------------------------------
    @staticmethod
    def _count_raw_text(raw: str) -> tuple[int, int]:
        """返回（字数, 字符数）：中文字符 + 英文/数字单词算字数，去掉换行算字符"""
        clean = raw.replace("\u2029", "\n")  # QTextCursor.selectedText() 用 U+2029 表示段落分隔
        chars = len(clean.replace("\n", ""))
        cjk = len(re.findall(r"[\u4e00-\u9fff]", clean))
        en = len(re.findall(r"[A-Za-z0-9_]+", clean))
        return cjk + en, chars

    def _update_word_count(self):
        document = self.editor.document()
        revision = document.revision()
        # 全文统计只在文档真正改动时重算；划选文字时复用缓存，长文档也不卡
        if revision != self._stats_revision:
            self._stats_words, self._stats_chars = self._count_raw_text(self.editor.toPlainText())
            self._stats_revision = revision

        words, chars = self._stats_words, self._stats_chars
        cursor = self.editor.textCursor()
        if cursor.hasSelection():
            sel_words, sel_chars = self._count_raw_text(cursor.selectedText())
            self.lbl_stats.setText(
                f"已选: {sel_words} 字 ({sel_chars} 字符)  |  总字数: {words}"
            )
            return

        read_mins = max(1, int(round(words / 400.0))) if words > 0 else 0
        time_hint = f"  |  阅读约 {read_mins} 分钟" if words > 0 else ""
        self.lbl_stats.setText(f"字数: {words}  |  字符: {chars}{time_hint}")

    def _update_window_title(self):
        name = os.path.basename(self.current_file) if self.current_file else "未命名文档.docx"
        star = " *" if self.editor.document().isModified() else ""
        self.setWindowTitle(f"Topos Word — {name}{star}")

    # ------------------------------------------------------------------------
    # 定时自动保存与崩溃恢复
    # ------------------------------------------------------------------------
    def _run_autosave(self):
        if not self.editor.document().isModified():
            return
        # 用桥接层存成临时 .docx：内嵌图片、表格、页面设置都能无损保留，
        # 而 toHtml() 无法携带注册在内存资源表里的图片
        # 先写临时文件再原子替换：避免程序在写入途中崩溃留下损坏的备份
        tmp_path = self._autosave_path + ".tmp"
        try:
            WordDocumentBridge.save_qdoc_to_docx(
                self.editor.document(), tmp_path, self.page_cfg
            )
            os.replace(tmp_path, self._autosave_path)
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    def _clear_autosave(self):
        for path in (
            self._autosave_path,
            self._autosave_path + ".tmp",
            os.path.join(tempfile.gettempdir(), "topos_word_recovery.html"),  # 旧版遗留
        ):
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass

    def _check_autosave_recovery(self):
        # 先清掉旧版本遗留的 .html 备份，避免误判
        legacy = os.path.join(tempfile.gettempdir(), "topos_word_recovery.html")
        if os.path.exists(legacy):
            try:
                os.remove(legacy)
            except Exception:
                pass

        if not os.path.exists(self._autosave_path):
            return
        ret = QMessageBox.question(
            self,
            "恢复未保存文档",
            "检测到上次异常退出前的自动备份内容，是否立即恢复？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret == QMessageBox.StandardButton.Yes:
            try:
                WordDocumentBridge.load_docx_into_qdoc(
                    self._autosave_path, self.editor.document(), self.editor.get_printable_width()
                )
                self.editor.normalize_document_colors()
                self.editor.document().clearUndoRedoStacks()
                self.editor.document().setModified(True)
                self._update_window_title()
                self._update_word_count()
                self.outline_sidebar.refresh()
                cur = self.editor.textCursor()
                cur.movePosition(QTextCursor.MoveOperation.End)
                self.editor.setTextCursor(cur)
            except Exception:
                pass
        self._clear_autosave()

    def _load_welcome_template(self):
        self.editor.setHtml("""
            <h1 style="font-size: 22pt; margin-bottom: 10px;">Topos Word</h1>
            <p style="font-size: 12pt; line-height: 1.6;">
                欢迎使用基于 <b>PyQt6</b> 构建的 macOS 平面化专业 Word 编辑器。你可以直接点击工具栏左上角的
                <span style="color: #0a84ff; font-weight: bold;">打开按钮</span> 预览并编辑本地
                <code>.docx</code> / <code>.doc</code> / <code>.rtf</code> / <code>.md</code> 文件，
                也可以把文件直接拖进窗口。
            </p>
            <h3 style="font-size: 14pt; margin-top: 14px;">专业排版能力：</h3>
            <ul>
                <li><b>标题样式与大纲</b>：样式栏可选正文 / 标题 1-3，右侧大纲按钮可打开标题导航；</li>
                <li><b>原生表格闭环</b>：插入表格，或在表格上右键增删行列、合并单元格、设置底色；</li>
                <li><b>格式刷与清除格式</b>：选中源文本后点格式刷，再划选目标文本即可套用；</li>
                <li>支持行距 1.0 / 1.15 / 1.5 / 2.0 倍、Tab / Shift+Tab 缩进，<code>#</code>、<code>-</code>、<code>1.</code> 加空格快捷转标题或列表；</li>
                <li><b>超链接</b>：<b>Ctrl/Cmd + K</b> 插入，按住 Ctrl/Cmd 点击可在浏览器打开；</li>
                <li><b>页面设置</b>：纸张 A4 / Letter / A5、横纵向、三档页边距，同步作用于 PDF 与 .docx；</li>
                <li><b>Ctrl/Cmd + Enter</b> 插入分页符；每 30 秒自动备份，异常退出后可一键恢复；</li>
                <li>按下 <b>Ctrl/Cmd + F</b> 呼出查找与批量替换，最右侧太阳图标切换白底 / 暗色护眼纸张。</li>
            </ul>
        """)
        self.editor.document().setModified(False)
        # 光标落到文末的普通段落，避免用户一开始输入就继承欢迎页的标题样式
        cur = self.editor.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        self.editor.setTextCursor(cur)
        # 欢迎内容不应出现在撤销历史里（否则一按 Ctrl+Z 整页消失）
        self.editor.document().clearUndoRedoStacks()
        self._update_window_title()

    def closeEvent(self, event):
        if self._maybe_save_changes():
            self._clear_autosave()
            event.accept()
        else:
            event.ignore()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setApplicationName("Topos Word")
    app.setApplicationDisplayName("Topos Word")
    window = ToposWordWindow()
    window.show()
    sys.exit(app.exec())
