/** fe-state.js — [ARCH-13] state 模块：全局状态、常量与共享工具（IIFE）。
 *
 * 载入顺序：state → render → actions（见 templates/index.html）。
 * state 先行求值，把共享工具挂到 window.__FE 供后续模块解构；
 * render 的导出同样进 __FE，actions 启动时一次性解构。 */
(() => {
const ds = document.body.dataset;
// 支持预览的专业文件格式列表
const PREVIEW_FORMATS = ['psd', 'psb', 'ai', 'eps', 'pdf', 'sketch', 'xd', 'fig', 'cdr', 'indd', 'cr2', 'cr3', 'nef', 'orf', 'raf', 'sr2', 'arw', 'rw2', 'dng', 'pef', 'mos', 'mef', 'fff', '3fr', 'kdc', 'mrw', 'erf', 'srw', 'exr', 'hdr', 'tga', 'tiff', 'tif', 'dpx', 'cin', 'r3d', 'toos'];

// 文件类型图标映射 (Bootstrap Icons)
const FILE_ICON_MAP = {
    // 代码文件
    '.py': 'bi-filetype-py', '.js': 'bi-filetype-js', '.ts': 'bi-filetype-ts',
    '.html': 'bi-filetype-html', '.htm': 'bi-filetype-html', '.css': 'bi-filetype-css',
    '.json': 'bi-filetype-json', '.xml': 'bi-filetype-xml',
    '.java': 'bi-filetype-java', '.kt': 'bi-filetype-java',
    '.c': 'bi-filetype-c', '.cpp': 'bi-filetype-cpp', '.cc': 'bi-filetype-cpp', '.cxx': 'bi-filetype-cpp',
    '.h': 'bi-filetype-c', '.hpp': 'bi-filetype-cpp',
    '.cs': 'bi-filetype-cs', '.go': 'bi-filetype-go', '.rs': 'bi-filetype-rs',
    '.php': 'bi-filetype-php', '.rb': 'bi-gem', '.swift': 'bi-filetype-swift',
    '.sql': 'bi-database', '.sh': 'bi-terminal', '.bash': 'bi-terminal',
    '.yaml': 'bi-filetype-yml', '.yml': 'bi-filetype-yml',
    '.toml': 'bi-gear', '.ini': 'bi-gear', '.cfg': 'bi-gear', '.conf': 'bi-gear',
    '.md': 'bi-markdown', '.markdown': 'bi-markdown',
    '.vue': 'bi-filetype-js', '.jsx': 'bi-filetype-js', '.tsx': 'bi-filetype-ts',
    '.scss': 'bi-filetype-css', '.sass': 'bi-filetype-css', '.less': 'bi-filetype-css',
    // 文档文件
    '.pdf': 'bi-file-earmark-pdf', '.doc': 'bi-file-earmark-word', '.docx': 'bi-file-earmark-word',
    '.xls': 'bi-file-earmark-excel', '.xlsx': 'bi-file-earmark-excel',
    '.ppt': 'bi-file-earmark-ppt', '.pptx': 'bi-file-earmark-ppt',
    '.txt': 'bi-file-earmark-text', '.log': 'bi-file-earmark-text', '.rtf': 'bi-file-earmark-text',
    '.csv': 'bi-file-earmark-spreadsheet',
    // 压缩包
    '.zip': 'bi-file-earmark-zip', '.rar': 'bi-file-earmark-zip', '.7z': 'bi-file-earmark-zip',
    '.tar': 'bi-file-earmark-zip', '.gz': 'bi-file-earmark-zip', '.bz2': 'bi-file-earmark-zip',
    '.xz': 'bi-file-earmark-zip', '.iso': 'bi-disc', '.dmg': 'bi-disc',
    // 音频文件
    '.mp3': 'bi-file-earmark-music', '.wav': 'bi-file-earmark-music', '.flac': 'bi-file-earmark-music',
    '.aac': 'bi-file-earmark-music', '.ogg': 'bi-file-earmark-music', '.wma': 'bi-file-earmark-music',
    '.m4a': 'bi-file-earmark-music', '.aiff': 'bi-file-earmark-music',
    // 视频文件
    '.mp4': 'bi-file-earmark-play', '.mov': 'bi-file-earmark-play', '.avi': 'bi-file-earmark-play',
    '.mkv': 'bi-file-earmark-play', '.wmv': 'bi-file-earmark-play', '.flv': 'bi-file-earmark-play',
    '.webm': 'bi-file-earmark-play', '.m4v': 'bi-file-earmark-play',
    // 图片文件
    '.jpg': 'bi-file-earmark-image', '.jpeg': 'bi-file-earmark-image', '.png': 'bi-file-earmark-image',
    '.gif': 'bi-file-earmark-image', '.webp': 'bi-file-earmark-image', '.bmp': 'bi-file-earmark-image',
    '.svg': 'bi-file-earmark-image', '.ico': 'bi-file-earmark-image',
    '.heic': 'bi-file-earmark-image', '.heif': 'bi-file-earmark-image',
    '.tiff': 'bi-file-earmark-image', '.tif': 'bi-file-earmark-image',
    // 专业格式
    '.psd': 'bi-file-earmark-richtext', '.psb': 'bi-file-earmark-richtext',
    '.ai': 'bi-file-earmark-richtext', '.eps': 'bi-file-earmark-richtext',
    '.sketch': 'bi-file-earmark-richtext', '.fig': 'bi-file-earmark-richtext', '.xd': 'bi-file-earmark-richtext',
    // RAW 格式
    '.raw': 'bi-camera', '.cr2': 'bi-camera', '.cr3': 'bi-camera', '.nef': 'bi-camera',
    '.orf': 'bi-camera', '.raf': 'bi-camera', '.arw': 'bi-camera', '.dng': 'bi-camera',
    // 字体文件
    '.ttf': 'bi-fonts', '.otf': 'bi-fonts', '.woff': 'bi-fonts', '.woff2': 'bi-fonts',
    // 其他
    '.exe': 'bi-windows', '.app': 'bi-apple', '.dmg': 'bi-disc',
    '.apk': 'bi-android2', '.ipa': 'bi-apple'
};

// 根据文件扩展名获取对应的图标类
function getFileIcon(filename) {
    const ext = '.' + filename.split('.').pop().toLowerCase();
    return FILE_ICON_MAP[ext] || 'bi-file-earmark';
}
// 全局状态管理对象
const STATE = {
    currentPath: "", rootPath: "", allFiles: [], visibleFiles: [], renderIdx: 0, BATCH_SIZE: 100,
    viewMode: ds.viewMode || 'grid',
    filter: { minRating: parseInt(ds.filterRating) || 0, type: ds.filterType || 'all',
              color: 0, flag: 'all',  // [U5/GAP-03] 颜色/旗标筛选（会话级）
              dateFrom: '', dateTo: '', minDuration: 0,  // [V1/GAP-02] 拍摄日期/时长（会话级）
              domBucket: '' },  // [V3/GAP-09] 自动主色桶筛选（''=不过滤，-1=灰阶）
    recursive: false,  // [V3/GAP-11] 子文件夹内容聚合显示（localStorage 持久）
    timelineBase: 'mtime',  // [V1/GAP-02] 时间线分组基准：mtime | taken
    expandedStacks: new Set(),  // [V2/GAP-07] 已展开的堆叠 id（会话级）
    collectionCache: [],  // [V2/GAP-05] 集合缓存（侧栏 + 视图标题共用）
    allTags: [],  // [U5/GAP-01] 标签缓存（侧栏 + 属性面板补全共用）
    sort: { field: ds.sortField || 'name', order: ds.sortOrder || 'asc' },
    smartExpanded: ds.smartExpanded === 'true', libraryExpanded: ds.libraryExpanded === 'true', sidebarCollapsed: ds.sidebarCollapsed === 'true',
    selected: new Set(), visuallySelected: new Set(), currentIndex: -1, lastIndex: -1, history: [], historyIdx: -1,
    lightbox: { active: false, index: -1, zoom: 1, transX: 0, transY: 0 },
    audioInstance: null, audioTargetId: null, audioHoverIndex: -1, isScrubbing: false,
    pluginPanelActive: false, useLargeThumbs: false, thumbLoading: { total: 0, loaded: 0, active: false }
};
window.STATE = STATE;
const $ = s => document.querySelector(s), $$ = s => document.querySelectorAll(s);

// --- Global Toast Debouncer (Anti-Spam) ---
const TOAST_STATE = { lastMsg: "", lastTime: 0 };

window.showToast = (text, type = 'info') => {
    const now = Date.now();
    // Anti-Spam Rule: If same message appears within 1.5 seconds, ignore it.
    if (text === TOAST_STATE.lastMsg && (now - TOAST_STATE.lastTime < 1500)) {
        console.warn("[Toast Ignored] Duplicate message suppression:", text);
        return;
    }

    // Update State
    TOAST_STATE.lastMsg = text;
    TOAST_STATE.lastTime = now;

    // Style Config
    let bg = "#252526"; // Default/Info
    if (type === 'success') bg = "#198754";
    if (type === 'error') bg = "#dc3545";
    if (type === 'warning') bg = "#fd7e14";

    Toastify({
        text: text,
        duration: 2000,
        gravity: "top", 
        position: "center", 
        style: { 
            background: bg,
            marginTop: "60px", // Push down a bit
            borderRadius: "6px",
            border: `1px solid ${type === 'error' ? '#dc3545' : '#444'}`,
            boxShadow: "0 4px 12px rgba(0,0,0,0.5)"
        },
        stopOnFocus: true
    }).showToast();
};

/** 选择逻辑 */
function escapeHtml(unsafe) { // 转义 HTML 防止 XSS
    if (typeof unsafe !== 'string') return unsafe;
    return unsafe.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;");
}
// [FUNC-16] 路径入 CSS 属性选择器（[data-path="..."]）的转义：
// 此前只转义单引号（双引号内本就无需），路径含双引号即打破选择器
function cssPathSel(path) {
    return '"' + String(path).replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';
}

/** 性能优化：批量更新 DOM */
window.visualUpdateQueue = new Set(); window.isVisualLoopRunning = false; window.propertyDebounceTimer = null;
const processVisualUpdates = () => {
    if (window.visualUpdateQueue.size > 0) {
        window.visualUpdateQueue.forEach(idx => {
            const card = document.getElementById(`card-${idx}`);
            if (card && STATE.visibleFiles[idx]) {
                const isSel = STATE.selected.has(STATE.visibleFiles[idx].path);
                card.classList.toggle('selected', isSel);
                const cb = card.firstElementChild; if(cb && cb.classList.contains('card-checkbox')) cb.checked = isSel;
            }
        });
        window.visualUpdateQueue.clear();
    }
    if (window.visualUpdateQueue.size > 0) requestAnimationFrame(processVisualUpdates);
    else window.isVisualLoopRunning = false;
};
const updateCardVisual = (idx, immediate = false) => {
    if (immediate) {
        const card = document.getElementById(`card-${idx}`);
        if (card && STATE.visibleFiles[idx]) {
            const isSel = STATE.selected.has(STATE.visibleFiles[idx].path);
            card.classList.toggle('selected', isSel);
            const cb = card.firstElementChild; if(cb && cb.classList.contains('card-checkbox')) cb.checked = isSel;
        }
        window.visualUpdateQueue.delete(idx); return;
    }
    window.visualUpdateQueue.add(idx);
    if (!window.isVisualLoopRunning) { window.isVisualLoopRunning = true; requestAnimationFrame(processVisualUpdates); }
};

const getMimeType = (fn) => {
    const ext = fn.split('.').pop().toLowerCase();
    const map = { 'jpg':'image/jpeg','jpeg':'image/jpeg','png':'image/png','gif':'image/gif','webp':'image/webp','mp4':'video/mp4','mov':'video/quicktime','mp3':'audio/mpeg','wav':'audio/wav','pdf':'application/pdf' };
    return map[ext] || 'application/octet-stream';
};
const parseSize = (s) => { // 解析文件大小字符串
    if (!s) return 0;
    const u = { 'B': 1, 'KB': 1024, 'MB': 1024*1024, 'GB': 1024*1024*1024, 'TB': 1024*1024*1024*1024 }, m = s.match(/^([\d.]+)\s*([A-Z]+)$/i);
    return m ? parseFloat(m[1]) * (u[m[2].toUpperCase()] || 1) : parseFloat(s) || 0;
};

/** 排序与筛选逻辑 */
const persistConfig = (data) => fetch('/api/config', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data) }).catch(console.error);

// [U3/BUG-P2-20] 统一 fetch 层：超时中断 / res.ok 检查 / JSON 解析 / 失败 toast。
// 返回解析后的 JSON；网络错误或非 2xx 抛 Error（message 已就绪可展示）。
// silent=true 时不自动 toast（调用方自行决定呈现）。
window.feFetch = async (url, opts = {}, { timeout = 20000, silent = false, context = '操作' } = {}) => {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeout);
    try {
        const res = await fetch(url, { ...opts, signal: ctrl.signal });
        let data = null;
        try { data = await res.json(); } catch (_) { /* 空响应体/非 JSON */ }
        if (!res.ok) {
            const msg = (data && (data.error || data.message)) || `HTTP ${res.status}`;
            throw new Error(msg);
        }
        return data;
    } catch (e) {
        if (!silent) {
            const reason = (e && e.name === 'AbortError') ? '超时' : (e.message || '网络错误');
            window.showToast(`⚠️ ${context}失败: ${reason}`, 'error');
        }
        throw e;
    } finally { clearTimeout(timer); }
};

const _resolveAction = (name) => name.split('.').reduce((o, k) => (o == null ? undefined : o[k]), window);
const _dispatchAction = (el, e) => {
    const fn = _resolveAction(el.dataset.action);
    if (typeof fn !== 'function') return;
    let args = [];
    if (el.dataset.args) {
        try { args = JSON.parse(el.dataset.args).map(a => a === '$el' ? el : a); }
        catch (err) { console.error('[data-action] data-args JSON 解析失败:', err); return; }
    }
    // 便捷数据属性：存在即追加为尾参（data-path → 路径串，data-id → 数值）
    if (el.dataset.path !== undefined) args.push(el.dataset.path);
    if (el.dataset.id !== undefined) args.push(/^\d+$/.test(el.dataset.id) ? parseInt(el.dataset.id, 10) : el.dataset.id);
    const result = fn.apply(el, args);
    if (result === false && e) { e.preventDefault(); e.stopPropagation(); }
};
// 捕获相位：菜单条目上的 stopPropagation 冒泡监听（如 .app-menubar）此前
// 能拦截到 inline onclick（target 相位）之后的冒泡链，但会拦截 document
// 冒泡委托——inline 语义以捕获相位复刻最接近。
document.addEventListener('click', (e) => {
    const el = e.target.closest('[data-action]');
    if (!el) return;
    if (el.dataset.stop !== undefined) {
        e.stopPropagation();
        // [BUG-P2-16] Bootstrap 下拉唯一依赖 document 冒泡监听收起——
        // 上面的 stopPropagation 会把事件截断在半路，"设置"菜单内所有
        // 带 data-stop 的条目点击后下拉悬留不关。这里手动收起补偿。
        const menu = el.closest('.dropdown-menu');
        if (menu && window.bootstrap?.Dropdown) {
            const toggle = menu.previousElementSibling;
            if (toggle?.dataset?.bsToggle === 'dropdown') {
                bootstrap.Dropdown.getOrCreateInstance(toggle).hide();
            }
        }
    }
    if (el.dataset.prevent !== undefined) e.preventDefault();
    _dispatchAction(el, e);
}, true);
// [ARCH-13] change/input 委托：表单控件动态生成处不再拼 onchange/oninput 字符串。
// 约定：处理函数首参收到触发变更的控件（e.target）
document.addEventListener('change', (e) => {
    const el = e.target.closest('[data-change-action]');
    if (!el) return;
    const fn = _resolveAction(el.dataset.changeAction);
    if (typeof fn !== 'function') return;
    fn.call(el, e.target);
});
document.addEventListener('input', (e) => {
    const el = e.target.closest('[data-input-action]');
    if (!el) return;
    const fn = _resolveAction(el.dataset.inputAction);
    if (typeof fn !== 'function') return;
    fn.call(el, e.target);
});


const FE = window.__FE = window.__FE || {};
Object.assign(FE, { ds, PREVIEW_FORMATS, FILE_ICON_MAP, getFileIcon, STATE, $, $$,
    escapeHtml, cssPathSel, updateCardVisual, getMimeType, parseSize, persistConfig,
    showToast: window.showToast });
})();
