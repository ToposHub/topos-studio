/** fe-actions.js — [ARCH-13] actions 模块：用户行为、API 调用、事件初始化（IIFE）。
 *
 * render 已先行求值，这里一次性解构其导出；本模块再把 loadPath/openItem
 * 回填 __FE（render 内 navigateFile/onTreeClick 晚期绑定用）。 */
(() => {
const { ds, STATE, $, $$, escapeHtml, persistConfig, updateCardVisual, parseSize, getFileIcon,
        PREVIEW_FORMATS, renderNextBatch, applyFilterAndSort, updateSortMenuState, updateFilterMenuState,
        updateTimeBadge, updateThumbProgress, setupContextMenu, setupLightboxEvents, initFolderTree,
        revealTreePath, createTreeNodeHtml, findInsertPosition, updateSelectionUI, updateNavButtons,
        updateToolbar, togglePropertiesPanel, showFileProperties, showBatchProperties } = window.__FE;
window.clearSelection = () => { // 清除所有选择
    STATE.visuallySelected.forEach(i => {
        const c = document.getElementById(`card-${i}`);
        if(c){ c.classList.remove('selected'); const cb=c.querySelector('.card-checkbox'); if(cb) cb.checked=false; }
    });
    STATE.selected.clear(); STATE.visuallySelected.clear(); updateToolbar(); togglePropertiesPanel(false);
};
window.selectAll = () => { // 全选/反全选
    // [FUNC-21] 按内容判断而非数量：筛选后数量相等会误判为"全选"而清空选区
    if (STATE.visibleFiles.length > 0 && STATE.visibleFiles.every(f => STATE.selected.has(f.path))) return window.clearSelection();
    STATE.selected.clear(); STATE.visuallySelected.clear();
    STATE.visibleFiles.forEach((f, i) => { STATE.selected.add(f.path); STATE.visuallySelected.add(i); updateCardVisual(i); });
    if (STATE.selected.size > 1) showBatchProperties();
    updateToolbar();
};
window.invertSelection = () => { // 反选当前视图中的文件
    const old = new Set(STATE.selected); STATE.selected.clear(); STATE.visuallySelected.clear();
    STATE.visibleFiles.forEach((f, i) => {
        if (!old.has(f.path)) { STATE.selected.add(f.path); STATE.visuallySelected.add(i); }
        updateCardVisual(i);
    });
    updateToolbar();
    if (!STATE.selected.size) togglePropertiesPanel(false);
    else if (STATE.selected.size === 1) { const p=[...STATE.selected][0], f=STATE.visibleFiles.find(x=>x.path===p); if(f) showFileProperties(f); }
    else showBatchProperties();
};

window.showAboutModal = () => { const el = $('#aboutModal'); if (el) { const modal = new bootstrap.Modal(el); modal.show(); } };

/** 工具函数 */
window.setSort = (field, el) => { STATE.sort.field = field; persistConfig({ sort_field: field }); if (el) { const menu = el.closest('.dropdown-menu'); menu.querySelectorAll('.dropdown-item').forEach(i => i.classList.remove('active')); el.classList.add('active'); } applyFilterAndSort(); };
window.toggleSortOrder = () => { STATE.sort.order = STATE.sort.order === 'asc' ? 'desc' : 'asc'; persistConfig({ sort_order: STATE.sort.order }); applyFilterAndSort(); };
window.setFilterType = (type, el) => { STATE.filter.type = type; persistConfig({ filter_type: type }); if (el) { const menu = el.closest('.dropdown-menu'); menu.querySelectorAll('[data-filter-type]').forEach(i => i.classList.remove('active')); el.classList.add('active'); } applyFilterAndSort(); };
window.setFilterRating = (rating, el) => { STATE.filter.minRating = rating; persistConfig({ filter_rating: rating }); if (el) { const menu = el.closest('.dropdown-menu'); menu.querySelectorAll('[data-filter-rating]').forEach(i => i.classList.remove('active')); el.classList.add('active'); } applyFilterAndSort(); };
// [U5/GAP-03] 颜色/旗标筛选（会话级，不持久化）
window.setFilterColor = (v, el) => { STATE.filter.color = v; if (el) { const menu = el.closest('.dropdown-menu'); menu.querySelectorAll('[data-filter-color]').forEach(i => i.classList.remove('active')); el.classList.add('active'); } applyFilterAndSort(); };
window.setFilterFlag = (v, el) => { STATE.filter.flag = v; if (el) { const menu = el.closest('.dropdown-menu'); menu.querySelectorAll('[data-filter-flag]').forEach(i => i.classList.remove('active')); el.classList.add('active'); } applyFilterAndSort(); };
// [V1/GAP-02] 拍摄日期/最短时长筛选（会话级；data-change-action 传入控件本身）
window.setFilterDateFrom = (el) => { STATE.filter.dateFrom = (el?.value || '').trim(); applyFilterAndSort(); };
window.setFilterDateTo = (el) => { STATE.filter.dateTo = (el?.value || '').trim(); applyFilterAndSort(); };
window.setFilterDuration = (v, el) => { STATE.filter.minDuration = parseFloat(v) || 0; if (el) { const menu = el.closest('.dropdown-menu'); menu.querySelectorAll('[data-filter-duration]').forEach(i => i.classList.remove('active')); el.classList.add('active'); } applyFilterAndSort(); };
// [V1/GAP-02] 时间线分组基准切换
window.setTimelineBase = (el) => { STATE.timelineBase = el?.value === 'taken' ? 'taken' : 'mtime'; applyFilterAndSort(); };

// [V1/GAP-01] 全库索引：侧栏状态轮询 + 手动重建
window.startLibraryScan = async () => {
    try {
        const res = await fetch('/api/scan/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
        const data = await res.json().catch(() => ({}));
        if (res.status === 409) window.showToast('索引扫描已在进行中', 'info');
        else if (!res.ok) window.showToast((data && data.message) || '触发扫描失败', 'error');
        else window.showToast('已开始后台索引扫描（新文件将进入全库搜索）', 'success');
        window.refreshScanStatus();
    } catch (e) { window.showToast('触发扫描失败', 'error'); }
};
window.refreshScanStatus = async () => {
    const el = document.getElementById('scanStatusText'); if (!el) return;
    try {
        const st = await (await fetch('/api/scan/status')).json();
        if (st.running) {
            el.innerHTML = `<span style="color:#7bd88f;">● ${escapeHtml(st.phase || '扫描中')}</span>` + (st.total ? ` ${st.scanned || 0}/${st.total}` : '');
            window._scanWasRunning = true;
        } else {
            const last = st.last_result || {};
            el.innerHTML = '索引就绪' + (last.finished_at
                ? ` <span style="font-size:10px;opacity:0.6;">· 上次 +${last.added ?? 0}/~${last.updated ?? 0}${last.elapsed != null ? ` (${last.elapsed}s)` : ''}</span>` : '');
            // 扫描结束：刷新当前真实目录视图（新文件即刻出现）
            if (window._scanWasRunning) {
                window._scanWasRunning = false;
                const cur = STATE.currentPath || '';
                if (!cur.startsWith('smart://') && !cur.startsWith('trash://') && !cur.startsWith('search://')) window.reloadFiles();
            }
        }
    } catch (e) { /* 状态轮询失败静默 */ }
};

/** 文件列表渲染 */
// [UX-7] 卡片角标：星级/标签（此前 DB 里已有 tags/rating 但卡片无任何展示）
window.startAudioPreview = (idx, path) => { // 鼠标悬停开始音频预览
    if (STATE.audioTargetId === idx && STATE.audioInstance) return;
    stopAudioPreview(); STATE.audioTargetId = idx; const container = $(`#waveform-${idx}`); if (!container || typeof WaveSurfer === 'undefined') return;
    try {
        STATE.audioInstance = WaveSurfer.create({ container, waveColor:'#4facfe', progressColor:'#00f2fe', cursorColor:'#fff', barWidth:2, barGap:1, barRadius:2, height:140, normalize:true, interact:false, cursorWidth:0 });
        STATE.audioInstance.load(`/api/file?path=${encodeURIComponent(path)}`);
        STATE.audioInstance.on('ready', () => { STATE.audioInstance.play().catch(()=>{}); updateTimeBadge(idx, 0, STATE.audioInstance.getDuration()); });
        STATE.audioInstance.on('audioprocess', (t) => updateTimeBadge(idx, t, STATE.audioInstance.getDuration()));
    } catch (e) { console.error("WS Error:", e); }
};
window.stopAudioPreview = () => { // 停止预览并销毁实例
    if (STATE.audioInstance) { try { STATE.audioInstance.destroy(); } catch(e){} STATE.audioInstance = null; }
    if (STATE.audioTargetId !== null) { const b = $(`#audio-time-${STATE.audioTargetId}`); if(b) b.innerText = "00:00"; STATE.audioTargetId = null; }
};
window.scrubAudioPreview = (e) => { // 音频进度拖动
    if (!STATE.audioInstance || STATE.audioTargetId === null) return;
    const dur = STATE.audioInstance.getDuration(); 
    if (!dur || dur === Infinity) return;

    // 1. 获取当前正在预览的特定波形图容器 DOM
    const waveContainer = document.getElementById(`waveform-${STATE.audioTargetId}`);
    if (!waveContainer) return;

    // 2. 获取该特定容器的尺寸和位置
    const rect = waveContainer.getBoundingClientRect();
    
    // 3. 计算相对于该容器的点击位置
    // 注意：CSS中波形图可能有 padding 或定位偏移，直接计算 rect.left 最准确
    const progress = (e.clientX - rect.left) / rect.width;

    STATE.audioInstance.seekTo(Math.max(0, Math.min(1, progress)));
};
window._skipBridgeNotification = false;

async function loadPath(path, isHist = false, fromTree = false, skipBridge = false) { // 加载指定目录的文件列表
    if (!path) return; path = path.replace(/\\/g, '/'); const isRenaming = window._isRenamingPath === path;
    // [FUNC-15] 目录切换竞态：快速切换时旧响应后到会覆盖新列表（用户随后
    // 在旧目录画面上删除/重命名 = 误操作真实文件）。递增令牌丢弃过期响应。
    const seq = (window._loadSeq = (window._loadSeq || 0) + 1);
    STATE.thumbLoading.total = 0; STATE.thumbLoading.loaded = 0; updateThumbProgress();
    if (!isHist && STATE.history[STATE.historyIdx] !== path) { STATE.history = STATE.history.slice(0, STATE.historyIdx + 1); STATE.history.push(path); STATE.historyIdx++; }
    updateNavButtons(); STATE.currentPath = path; STATE.selected.clear(); STATE.visuallySelected.clear(); updateToolbar(); togglePropertiesPanel(false);
    if($('#pathDisplay')) $('#pathDisplay').value = path;
    window.renderBreadcrumb?.(path);  // [U7/GAP-13] 面包屑随路径联动
    if($('#searchInput')){ $('#searchInput').value = ''; $('#searchClear').style.display='none'; }
    // [U5 收尾] 离开搜索视图时收起"保存为智能文件夹"入口
    { const sb = $('#saveSearchBtn'); if (sb) sb.style.display = 'none'; }
    fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({last_path: path})});
    if (!fromTree) revealTreePath(path).catch(()=>{});
    // [UX-3] 不再预清空列表：加载失败时保留旧内容，成功后由 applyFilterAndSort 重绘
    try {
        // [V3/GAP-11] 子文件夹聚合：真实目录且开关打开时携带 recursive 参数
        const isRealDir = !/^(smart|search|trash|collection|similar):\/\//.test(path);
        const recQ = (STATE.recursive && isRealDir) ? '&recursive=1&depth=2' : '';
        const res = await fetch(`/api/list?path=${encodeURIComponent(path)}${recQ}`), data = await res.json();
        // [FUNC-15] 已切换到别的目录：丢弃这次过期响应
        if (seq !== window._loadSeq) return;
        if (data.error) {
            // [V4/GAP-22] 软锁目录：弹解锁流程，成功后自动重载
            if (data.locked) {
                $('#emptyState').innerText = "🔒 目录已锁定";
                $('#emptyState').classList.remove('d-none');
                const ok = await window.promptUnlock(data.lock_path, data.hint);
                if (ok && seq === window._loadSeq) { loadPath(path, true); return; }
                if (!ok) return;
            }
            if (!isRenaming) window.showToast("⚠️ 无法访问文件夹", 'warning');
            // [BUG-P1-08] 失败必须清空旧卡片：路径栏已切到新路径，残留旧目录
            // 的卡片会让右键删除/重命名实际作用在旧目录的真实文件上
            STATE.allFiles = []; STATE.visibleFiles = []; STATE.renderIdx = 0;
            STATE.visuallySelected.clear(); window.__FE.unobserveThumbImgs($('#fileContainer')); $('#fileContainer').innerHTML = '';
            $('#emptyState').innerText = "⚠️ 文件夹无法访问";
            $('#emptyState').classList.remove('d-none');
        }
        else $('#emptyState').innerText = "空文件夹 — 将文件拖入此处可上传"; // [BUG-P3-40] 空态给引导
        if(data.files) { STATE.allFiles = data.files; applyFilterAndSort(); if (STATE.pendingSelection) { const idx = STATE.visibleFiles.findIndex(f => f.path === STATE.pendingSelection); if (idx !== -1) { STATE.selected.add(STATE.pendingSelection); STATE.lastIndex = idx; updateSelectionUI(); showFileProperties(STATE.visibleFiles[idx]); } STATE.pendingSelection = null; } }

        // --- SyncBridge Hook ---
        // skipBridge 为 true 时表示由 Python 端触发，跳过通知避免循环
        if (window.pyBridge && !skipBridge && !window._skipBridgeNotification) {
            window.pyBridge.onDirectoryChanged(path);
        }
        // 重置标记
        window._skipBridgeNotification = false;
    } catch(e) {
        console.error("List Error:", e);
        // [BUG-P1-08] 网络失败同样清空（导航已提交：历史/路径栏均已是新路径）
        if (seq === window._loadSeq) {
            STATE.allFiles = []; STATE.visibleFiles = []; STATE.renderIdx = 0;
            STATE.visuallySelected.clear(); window.__FE.unobserveThumbImgs($('#fileContainer')); $('#fileContainer').innerHTML = '';
            $('#emptyState').innerText = "⚠️ 加载失败，点击刷新重试";
            $('#emptyState').classList.remove('d-none');
        }
        window.showToast("目录加载失败", 'error');
    }
}

window.goBack = () => { if (STATE.historyIdx > 0) { STATE.historyIdx--; loadPath(STATE.history[STATE.historyIdx], true); } };
window.goForward = () => { if (STATE.historyIdx < STATE.history.length - 1) { STATE.historyIdx++; loadPath(STATE.history[STATE.historyIdx], true); } };
window.goUp = () => { let p = STATE.currentPath; if (!p) return; // [FUNC-21] 智能文件夹是虚拟视图，无"上级"可言
    if (p.startsWith('smart://')) return window.showToast("智能文件夹是虚拟视图", 'info');
    if (p.startsWith('search://')) return loadPath(STATE.rootPath); // [U5] 搜索视图上级 = 回主目录
    if (p.startsWith('trash://')) return loadPath(STATE.rootPath); // [U6] 回收站视图上级 = 回主目录
    if (p.startsWith('collection://')) return loadPath(STATE.rootPath); // [V2] 集合视图上级 = 回主目录
    if (p.startsWith('similar://')) return loadPath(STATE.rootPath); // [V3] 相似视图上级 = 回主目录
    if (p.length > 1 && p.endsWith('/')) p = p.slice(0, -1); const i = p.lastIndexOf('/'); if (i === -1) return; let up = p.substring(0, i); if (up === '' && p.startsWith('/')) up = '/'; if (up.endsWith(':')) up += '/'; if (up !== STATE.currentPath) loadPath(up); };
// [U9] 虚拟视图（trash:// smart:// search://）下刷新按钮此前会 loadPath 报
// "无法访问文件夹"并清空视图——按视图类型分流
window.reloadFiles = () => {
    const cur = STATE.currentPath || '';
    if (cur.startsWith('trash://')) { openTrashView(); return; }
    if (cur.startsWith('collection://')) { openCollectionView(cur.slice('collection://'.length)); return; }
    if (cur.startsWith('similar://')) { runSimilarSearch(decodeURIComponent(cur.slice('similar://'.length))); return; }
    if (cur.startsWith('smart://')) {
        const id = cur.slice('smart://'.length);
        if (STATE.smartFoldersCache?.some(f => String(f.id) === String(id))) { openSmartFolder(id); return; }
        // [V5 复查] 缓存未命中（列表接口失败/尚未加载）时先拉一次列表再重试，
        // 此前直接回退 loadPath('smart://id') 必报"无法访问文件夹"
        loadSmartFolders?.().then(() => { if (STATE.smartFoldersCache?.some(f => String(f.id) === String(id))) openSmartFolder(id); }).catch(() => {});
        return;
    }
    if (cur.startsWith('search://q/')) { runLibrarySearch(decodeURIComponent(cur.slice('search://q/'.length))); return; }
    if (cur.startsWith('search://tag/')) { runLibrarySearch('', { tag: decodeURIComponent(cur.slice('search://tag/'.length)) }); return; }
    loadPath(cur, true);
};

/** 交互操作 */
function openItem(i) { // 双击或按键打开项目
    // [BUG-P1-10] 筛选/搜索缩短列表后按 Space/Enter 会拿到 undefined
    const f = STATE.visibleFiles[i]; if (!f) return;
    const ext = f.name.split('.').pop().toLowerCase();
    // [UX-6] 文件夹：进入目录（此前落到底部 openSystem 会误开 Finder）
    if (f.type === 'dir') return loadPath(f.path);
    // 图片始终使用灯箱预览，不再导入到项目中
    if (['jpg','jpeg','png','gif','webp','bmp','svg','ico'].includes(ext) || PREVIEW_FORMATS.includes(ext)) return openLightbox(i);
    // 视频一律走内置播放器（video_pro），失败只提示不转交；系统播放器仅可右键手动选择
    if (f.type === 'video' || ['mp4','mov','webm','ogg','mkv','m4v','avi','flv','wmv','topos','vob','ogv','qt','divx','3gp','mxf','ts','mts','m2ts'].includes(ext)) {
        // [BUG-P2-20] 播放请求失败此前完全静默
        return feFetch('/api/play_native', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({path: f.path}) }, { context: '打开视频', silent: true }).catch(() => window.showToast("播放请求失败", 'error'));
    }
    if (f.type === 'audio' || ['mp3','wav','ogg','flac','m4a','aac'].includes(ext)) return openProAudio(f);
    if (window.FILE_HANDLERS && window.FILE_HANDLERS['.'+ext]) { const [obj, meth] = window.FILE_HANDLERS['.'+ext].split('.'); if (window[obj]?.[meth]) return window[obj][meth](f.path); }
    openSystem(f.path);
}
function onCardClick(e, i, isCheckbox) { // 卡片点击选择逻辑
    const menu = $('#contextMenu'); if(menu) menu.style.display = 'none';
    // [BUG-P1-10] 渲染批次/筛选切换后索引可能越界
    const target = STATE.visibleFiles[i]; if (!target) return;
    const p = target.path;
    if(e.ctrlKey || e.metaKey || isCheckbox) { if (STATE.selected.has(p)) { STATE.selected.delete(p); STATE.visuallySelected.delete(i); } else { STATE.selected.add(p); STATE.visuallySelected.add(i); } updateCardVisual(i, true); STATE.lastIndex = i; }
    else if(e.shiftKey && STATE.lastIndex !== -1) { STATE.visuallySelected.forEach(idx => updateCardVisual(idx)); STATE.visuallySelected.clear(); STATE.selected.clear(); const [s, end] = [Math.min(STATE.lastIndex, i), Math.max(STATE.lastIndex, i)]; for(let k=s; k<=end; k++) { STATE.selected.add(STATE.visibleFiles[k].path); STATE.visuallySelected.add(k); updateCardVisual(k); } } 
    else { STATE.visuallySelected.forEach(idx => { const card = document.getElementById(`card-${idx}`); if (card) { card.classList.remove('selected'); const cb = card.querySelector('.card-checkbox'); if (cb) cb.checked = false; } }); STATE.selected.clear(); STATE.selected.add(p); STATE.visuallySelected.clear(); STATE.visuallySelected.add(i); STATE.lastIndex = i; updateCardVisual(i, true); }
    updateToolbar();
    if (window.propertyDebounceTimer) clearTimeout(window.propertyDebounceTimer);
    if (STATE.selected.size === 0) togglePropertiesPanel(false);
    else { const panel = document.getElementById('propertiesPanel'); if (panel && panel.classList.contains('active')) { window.propertyDebounceTimer = setTimeout(() => { if (STATE.selected.size === 0) { togglePropertiesPanel(false); return; } if (STATE.selected.size > 1) showBatchProperties(); else if (STATE.selected.size === 1) { STATE.pluginPanelActive = false; const path = STATE.selected.values().next().value, file = STATE.visibleFiles.find(f => f.path === path); if (file) showFileProperties(file); } }, 200); } }
}
window.openWithApp = (appPath) => {
    // [FUNC-16] 此前读取从未赋值的 STATE.contextMenuIndex（子菜单点击必抛
    // TypeError）；统一走右键菜单的 contextTarget
    const target = STATE.contextTarget;
    if (!target || !appPath) return;
    // [BUG-P2-20] 失败（文件不存在/应用不在白名单 4xx）此前静默
    feFetch('/api/open_with', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({path: target.path, app_path: appPath}) }, { context: '打开文件' })
        .then(data => { if (data && data.status === 'error') window.showToast(data.message || '打开失败', 'error'); })
        .catch(() => {});
    $('#contextMenu').style.display = 'none';
};

/** [ARCH-13] 全局事件委托：data-action 驱动（替代 HTML onclick 字符串拼接）。
 *
 * 约定：
 * - data-action       处理函数名，支持点路径（"AudioProPlugin.togglePlay"）
 * - data-args         JSON 数组参数；"$el" 哨兵替换为元素本身
 * - data-stop         点击先 stopPropagation
 * - data-prevent      点击先 preventDefault
 * - 处理函数返回 false 时按 inline onclick 语义 prevent + stop
 */
// [BUG-P2-20] 以下系统动作失败此前一律静默，统一走 feFetch（失败 toast）
window.openSystem = (path) => feFetch('/api/open_system', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path})}, { context: '打开文件', silent: true }).catch(()=>{});
// [V5 复查] 五种虚拟视图统一判定：多处守卫此前只拦 smart/search/trash 三种，
// collection:// 与 similar:// 视图下"新建文件夹/粘贴/拖拽上传/系统打开/
// 重复查找/路径回车"会把虚拟路径当真实目录发给 API
const isVirtualView = (p) => /^(smart|search|trash|collection|similar):\/\//.test(String(p || ''));
window.isVirtualView = isVirtualView;
window.openCurrentFolderInSystem = () => { if (isVirtualView(STATE.currentPath)) return window.showToast("虚拟视图，无法在系统中打开", 'warning'); openSystem(STATE.currentPath); };
window.batchDelete = async () => { if(!STATE.selected.size) return; if (!(await window.showConfirmDialog(`确定要删除 ${STATE.selected.size} 个项目吗？（可在回收站恢复，Ctrl+Z 撤销）`))) return;
    // [BUG-P2-20] 删除失败（含部分失败）此前无任何提示
    try {
        const data = await feFetch('/api/delete', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paths:Array.from(STATE.selected)})}, { context: '删除' });
        const failCount = (data.errors || []).length;
        if (failCount) window.showToast(`删除完成，${failCount} 项失败`, 'warning');
        // [U6/GAP-05] 入站记录进撤销栈（Ctrl+Z 恢复）。
        // [U10] trashIds 为空（全部回退系统废纸篓）不入栈——空 ids 会被
        // 后端 400，撤销时 feFetch 抛错把条目原样压回 = 永久死条目
        const trashIds = (data.records || []).filter(r => !r.in_os_trash).map(r => r.id);
        if (trashIds.length) window.pushUndo({ type: 'delete', trashIds, dir: STATE.currentPath });
    } catch (e) {}
    reloadFiles();
};
window.copySelected = () => {
    if(!STATE.selected.size) return;
    // [BUG-P2-20] 网络失败此前无提示
    feFetch('/api/action/copy', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({paths: Array.from(STATE.selected)})
    }, { context: '复制', silent: true }).then(data => {
        if (data && data.status === 'ok') window.showToast(`已复制 ${STATE.selected.size} 个文件`, 'success');
    }).catch(() => window.showToast('复制请求失败', 'error'));
};
window.toggleSidebar = () => { const panel = $('#sidebarPanel'), miniBtn = $('#sidebarToggleMini'); if (!panel || !miniBtn) return; STATE.sidebarCollapsed = !STATE.sidebarCollapsed; panel.classList.toggle('collapsed', STATE.sidebarCollapsed); miniBtn.classList.toggle('show', STATE.sidebarCollapsed); persistConfig({ sidebar_collapsed: STATE.sidebarCollapsed }); };
window.pasteFiles = () => { // [U6] 虚拟视图不可粘贴（smart:// 原有防护一并补齐 trash://）
    // [U10] 补 search://；[V5 复查] 统一五 scheme（含 collection://、similar://）
    if (isVirtualView(STATE.currentPath)) return window.showToast("虚拟视图，无法粘贴", 'info');
    return feFetch('/api/action/paste',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:STATE.currentPath})}, { context: '粘贴', silent: true }).catch(() => window.showToast('粘贴请求失败', 'error'));
};
// [BUG-P2-06] 取消进行中的粘贴（进度条"取消"按钮）
window.cancelPaste = () => fetch('/api/action/paste_cancel', { method: 'POST' }).catch(console.error);
window.switchLayout = (mode) => { STATE.viewMode = mode; persistConfig({ view_mode: mode }); const grp = $('#btn-view-adaptive')?.closest('.btn-group'); grp?.querySelectorAll('button').forEach(b => b.classList.remove('active','btn-primary')); const btn = $(`#btn-view-${mode}`); if(btn) btn.classList.add('active','btn-primary'); $('#fileContainer').className = `layout-${mode}`;
    // [V1/GAP-02] 时间线基准切换仅时间线视图显示
    const tsel = $('#timelineBaseSel'); if (tsel) tsel.style.display = mode === 'timeline' ? '' : 'none';
    // [U7/GAP-07] 时间线分组结构需重建 DOM（组头/分组容器），重渲染当前列表
    applyFilterAndSort(); };
window.setCardSize = (size) => { // 调整缩略图大小
    const val = { small: 140, medium: 180, large: 280 }[size] || 180, slider = $('#sizeSlider');
    if(slider) { slider.value = val; slider.dispatchEvent(new Event('input')); } else persistConfig({ card_size: val });
    document.documentElement.style.setProperty('--card-width', val + 'px'); document.documentElement.style.setProperty('--card-height', val + 'px');
    document.body.classList.remove('size-large', 'size-small'); $$('.app-menubar .menu-item').forEach(m => m.classList.remove('active'));
    if ((val > 180) !== STATE.useLargeThumbs) { STATE.useLargeThumbs = val > 180; refreshVisibleThumbs(); }
};
window.refreshVisibleThumbs = () => { const sizeParam = STATE.useLargeThumbs ? 'large' : 'small'; $$('.file-thumb').forEach(img => { try { const url = new URL(img.src); url.searchParams.set('size', sizeParam); img.src = url.toString(); } catch(e) {} }); };
window.switchTheme = (name) => { $('#theme-stylesheet').href = name==='Default' ? '' : `/static/themes/${name}.css`; $$('[id^="icon-theme-"]').forEach(el => { el.classList.remove('text-primary'); el.classList.add('text-muted'); }); const icon = $(`#icon-theme-${CSS.escape(name)}`); if(icon){ icon.classList.remove('text-muted'); icon.classList.add('text-primary'); } fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({theme:name})}).catch(()=>{}); };

/** 智能文件夹与库管理 */
window.toggleSmartFolders = () => { const list = $('#smartFolderList'), toggler = $('#smartFolderToggler'); if (!list || !toggler) return; const isHidden = list.style.display === 'none'; list.style.display = isHidden ? 'block' : 'none'; toggler.classList.toggle('expanded', isHidden); persistConfig({ smart_expanded: isHidden }); };
window.toggleTagList = () => { const list = $('#tagList'), toggler = $('#tagListToggler'); if (!list || !toggler) return; const isHidden = list.style.display === 'none'; list.style.display = isHidden ? 'block' : 'none'; toggler.classList.toggle('expanded', isHidden); };
window.loadSmartFolders = async () => {
    const list = $('#smartFolderList'); if (!list) return;
    try { const res = await fetch('/api/smart_folders'), folders = await res.json(); STATE.smartFoldersCache = folders; list.innerHTML = folders.map(f => `<div class="smart-folder-item" data-id="${f.id}" data-name="${escapeHtml(f.name)}"><i class="bi bi-gear-wide-connected text-primary"></i><span class="text-truncate" style="flex:1;">${escapeHtml(f.name)}</span><i class="bi bi-three-dots-vertical small text-muted ms-auto" style="font-size:10px; opacity:0.5;"></i></div>`).join(''); } 
    catch (e) { console.error("Load smart folders error:", e); }
};
// [BUG-P2-20] 添加库触发失败此前静默
window.addLibrary = () => feFetch('/api/action/add_library', { method: 'POST' }, { context: '添加库' }).catch(() => {});
window.togglePinnedLibraries = () => { const list = $('#pinnedLibraryList'), toggler = $('#libraryListToggler'); if (!list || !toggler) return; const isHidden = list.style.display === 'none'; list.style.display = isHidden ? 'block' : 'none'; toggler.classList.toggle('expanded', isHidden); persistConfig({ library_expanded: isHidden }); };
window.loadPinnedLibraries = async () => {
    const list = $('#pinnedLibraryList'); if (!list) return;
    // [ARCH-13] 库条目：路径经 data-lib-path + 容器委托取用（不再拼进 onclick JS 串）
    try { const res = await fetch('/api/library/list'), libs = await res.json(); list.innerHTML = libs.map(lib => { return `<div class="smart-folder-item" data-lib-path="${escapeHtml(lib.path)}"><i class="bi bi-folder-symlink text-info"></i><span class="text-truncate" style="flex:1;" title="${escapeHtml(lib.path)}">${escapeHtml(lib.name)}</span><i class="bi bi-x-lg unpin-btn ms-auto" title="取消固定"></i></div>`; }).join(''); } 
    catch (e) { console.error("Load libraries error:", e); }
};
window.unpinLibrary = async (path) => { if (!(await window.showConfirmDialog("确定要从素材库中移除此文件夹吗？", "移除"))) return;
    // [BUG-P2-20] 取消固定失败此前无提示
    try { await feFetch('/api/library/unpin', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ path }) }, { context: '移除库' }); loadPinnedLibraries(); } catch (e) {} };
window.openSmartFolder = async (id, el) => { const name = el?.dataset?.name || '智能文件夹'; $$('.smart-folder-item, .tree-header').forEach(i => i.classList.remove('active')); if (el) el.classList.add('active'); STATE.currentPath = `smart://${id}`; STATE.pendingSelection = null; const pInp = $('#pathDisplay'); if(pInp) pInp.value = `智能文件夹: ${name}`; window.renderBreadcrumb?.(STATE.currentPath); STATE.allFiles = []; STATE.visibleFiles = []; STATE.renderIdx = 0; STATE.selected.clear(); STATE.visuallySelected.clear(); window.__FE.unobserveThumbImgs($('#fileContainer')); $('#fileContainer').innerHTML = ''; $('#emptyState').classList.add('d-none');
    // [U10] 对齐 loadPath/openTrashView 的清理：残留的搜索词会被
    // applyFilterAndSort 二次过滤智能文件夹结果、缩略图计数跨视图串台
    STATE.thumbLoading.total = 0; STATE.thumbLoading.loaded = 0; updateThumbProgress();
    if($('#searchInput')){ $('#searchInput').value = ''; $('#searchClear').style.display='none'; }
    { const sb = $('#saveSearchBtn'); if (sb) sb.style.display = 'none'; }
    // [BUG-P1-05] 竞态令牌与 loadPath 共用 _loadSeq：快速连点两个智能文件夹、
    // 或点完智能文件夹立刻点目录树时，旧响应后到会覆盖新列表（后续删除/
    // 重命名作用在错误目录上）
    const seq = (window._loadSeq = (window._loadSeq || 0) + 1);
    try { const res = await fetch(`/api/smart_folders/${id}/files`), data = await res.json(); if (seq !== window._loadSeq) return; if (data.files) { STATE.allFiles = data.files; applyFilterAndSort(); } else if (data.error) { $('#emptyState').innerText = "⚠️ 查询失败"; $('#emptyState').classList.remove('d-none'); } } catch (e) { if (seq !== window._loadSeq) return; console.error("Smart folder open error:", e); $('#emptyState').innerText = "⚠️ 查询失败"; $('#emptyState').classList.remove('d-none'); } };
window.showSmartContextMenu = (e, id) => { e.preventDefault(); e.stopPropagation(); const menu = $('#smartContextMenu'), folder = STATE.smartFoldersCache.find(f => f.id === id); if (!menu || !folder) return; let x = e.clientX, y = e.clientY; if (x + 180 > window.innerWidth) x -= 180; if (y + 100 > window.innerHeight) y -= 100; menu.style.cssText = `left:${x}px; top:${y}px; display:block;`; STATE.smartContextData = { id: folder.id, name: folder.name, rules: folder.rules }; const mainCtx = $('#contextMenu'); if(mainCtx) mainCtx.style.display = 'none'; };
window.handleSmartEdit = () => { const data = STATE.smartContextData; if(!data) return; openSmartBuilder(data); $('#smartContextMenu').style.display = 'none'; };
window.handleSmartDelete = () => { const data = STATE.smartContextData; if(!data) return; deleteSmartFolder(data.id, {stopPropagation:()=>{}}); $('#smartContextMenu').style.display = 'none'; };
window.deleteSmartFolder = async (id, e) => { if(e?.stopPropagation) e.stopPropagation(); if (!(await window.showConfirmDialog("确定删除此智能文件夹吗？", "删除"))) return;
    // [BUG-P2-20] 服务端 500 时此前仍 toast"已删除"（反向误导）
    try { await feFetch(`/api/smart_folders/${id}`, { method: 'DELETE' }, { context: '删除智能文件夹' }); loadSmartFolders(); if (STATE.currentPath === `smart://${id}`) loadPath(STATE.rootPath); window.showToast("已删除", 'success'); } catch (e) {} };

/** 智能文件夹构建器（presetRules：从"保存为智能文件夹"入口预填规则） */
window.openSmartBuilder = (editData = null, presetRules = null) => {
    const modalEl = $('#smartBuilderModal'); if(!modalEl) return;
    const modal = new bootstrap.Modal(modalEl), nameInp = $('#smartFolderName'), container = $('#smartRulesContainer'), title = modalEl.querySelector('.modal-title');
    container.innerHTML = '';
    if (editData) { title.innerText = "编辑智能文件夹"; nameInp.value = editData.name; modalEl.dataset.editId = editData.id; const rules = editData.rules || {}; if (Object.keys(rules).length > 0) { for (const [key, val] of Object.entries(rules)) addSmartRule(key, val); } else addSmartRule('tags_include'); }
    else { title.innerText = "新建智能文件夹"; nameInp.value = ''; delete modalEl.dataset.editId;
        if (presetRules && Object.keys(presetRules).length > 0) { for (const [key, val] of Object.entries(presetRules)) addSmartRule(key, val || null); }
        else addSmartRule('tags_include'); }
    modal.show();
};
window.addSmartRule = (defaultType = 'tags_include', defaultValue = null) => {
    const container = $('#smartRulesContainer'); if(!container) return;
    const div = document.createElement('div'); div.className = 'smart-rule-row';
    // [ARCH-13] 规则行：change/click 均走 #smartRulesContainer 容器委托
    // [U5 收尾] 新增 颜色/旗标/精确标签/关键词 四类规则（与 /api/search、筛选菜单对齐）
    div.innerHTML = `<select class="form-select form-select-sm bg-dark text-white border-secondary rule-prop" data-change-action="updateRuleInput"><option value="q" ${defaultType==='q'?'selected':''}>关键词</option><option value="tag" ${defaultType==='tag'?'selected':''}>标签（精确）</option><option value="type" ${defaultType==='type'?'selected':''}>文件类型</option><option value="min_rating" ${defaultType==='min_rating'?'selected':''}>最低评分</option><option value="color" ${defaultType==='color'?'selected':''}>颜色标记</option><option value="flag" ${defaultType==='flag'?'selected':''}>旗标</option><option value="tags_include" ${defaultType==='tags_include'?'selected':''}>标签包含（旧）</option></select><div class="rule-value-container flex-grow-1"></div><i class="bi bi-dash-circle rule-del" title="移除条件"></i>`;
    container.appendChild(div); const sel = div.querySelector('.rule-prop'); if(sel) updateRuleInput(sel, defaultValue);
};
window.updateRuleInput = (select, valueToSet = null) => {
    const container = select.nextElementSibling; if(!container) return;
    const type = select.value; let html = '';
    if(type === 'type') html = `<select class="form-select form-select-sm bg-dark text-white border-secondary rule-val"><option value="video">视频 (Video)</option><option value="image">图片 (Image)</option><option value="audio">音频 (Audio)</option><option value="doc">文档 (Doc)</option></select>`;
    else if(type === 'min_rating') html = `<select class="form-select form-select-sm bg-dark text-white border-secondary rule-val"><option value="1">★☆☆☆☆ (1星+)</option><option value="2">★★☆☆☆ (2星+)</option><option value="3">★★★☆☆ (3星+)</option><option value="4">★★★★☆ (4星+)</option><option value="5">★★★★★ (5星)</option></select>`;
    // [U5 收尾] 色板与旗标取值和筛选菜单/卡片角标一致（1红 2黄 3绿 4蓝 5紫；pick 旗标 / noreject 排除拒收 / reject 仅拒收）
    else if(type === 'color') html = `<select class="form-select form-select-sm bg-dark text-white border-secondary rule-val"><option value="1">🔴 红</option><option value="2">🟡 黄</option><option value="3">🟢 绿</option><option value="4">🔵 蓝</option><option value="5">🟣 紫</option></select>`;
    else if(type === 'flag') html = `<select class="form-select form-select-sm bg-dark text-white border-secondary rule-val"><option value="pick">旗标（选用）</option><option value="noreject">排除拒收</option><option value="reject">仅拒收</option></select>`;
    else if(type === 'tag') html = `<input type="text" class="form-control form-control-sm bg-dark text-white border-secondary rule-val" placeholder="输入确切标签名..." list="tag-suggestions">`;
    else if(type === 'q') html = `<input type="text" class="form-control form-control-sm bg-dark text-white border-secondary rule-val" placeholder="名称/标签/备注关键词...">`;
    else html = `<input type="text" class="form-control form-control-sm bg-dark text-white border-secondary rule-val" placeholder="输入标签或备注关键词...">`;
    container.innerHTML = html; if (valueToSet !== null) { const input = container.querySelector('.rule-val'); if(input) input.value = valueToSet; }
};
window.saveSmartFolder = async () => {
    const nameInp = $('#smartFolderName'), name = nameInp?.value.trim(); if(!name) return window.showToast("请输入文件夹名称", 'error');
    const rules = {}, rows = $$('.smart-rule-row'); if(!rows.length) return window.showToast("请至少添加一个规则", 'error');
    rows.forEach(row => { const propEl = row.querySelector('.rule-prop'), valEl = row.querySelector('.rule-val'); if(propEl && valEl && valEl.value) rules[propEl.value] = valEl.value; });
    const editId = $('#smartBuilderModal').dataset.editId, method = editId ? 'PUT' : 'POST', url = editId ? `/api/smart_folders/${editId}` : '/api/smart_folders';
    // [BUG-P2-20] 服务端 500 时此前仍 toast"已创建/已更新"（反向误导）
    try {
        const data = await feFetch(url, { method: method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ name, rules }) }, { context: '保存智能文件夹' });
        if (data && data.error) throw new Error(data.error);
        const modalEl = $('#smartBuilderModal'); if(modalEl) { const modal = bootstrap.Modal.getInstance(modalEl); modal?.hide(); }
        loadSmartFolders(); window.showToast(editId ? "已更新" : "已创建", 'success');
    }
    catch(e) { /* feFetch 已 toast；错误信息含服务端 error 字段 */ }
};

/** 初始化 DOM 事件 */
document.addEventListener('DOMContentLoaded', async () => {
    // --- Setup QWebChannel ---
    if (typeof qt !== 'undefined' && qt.webChannelTransport) {
        new QWebChannel(qt.webChannelTransport, function(channel) {
            window.pyBridge = channel.objects.pyBridge;
        });
    }

    STATE.rootPath = document.body.dataset.root || "/"; const startPath = document.body.dataset.start || STATE.rootPath;
    $('#sidebarPanel').style.width = (parseInt(document.body.dataset.sidebarWidth)||260) + 'px';
    const resizer = $('#sidebarResizer'), panel = $('#sidebarPanel');
    resizer.onmousedown = (e) => {
        const startX = e.clientX, startW = panel.offsetWidth;
        document.body.style.cursor = 'col-resize';
        document.body.style.userSelect = 'none';
        
        const mv = ev => {
            panel.style.width = Math.max(150, startW + ev.clientX - startX) + 'px';
        };
        const up = () => {
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
            document.removeEventListener('mousemove', mv);
            document.removeEventListener('mouseup', up);
            persistConfig({ sidebar_width: panel.offsetWidth });
        };
        document.addEventListener('mousemove', mv);
        document.addEventListener('mouseup', up);
    };
    $('#pathDisplay').onkeydown = e => { if(e.key!=='Enter') return; const v = $('#pathDisplay').value.trim(); $('#pathDisplay').blur();
        // [BUG-P3-45] 虚拟视图的展示文案不是路径，回车此前必然报"无法访问"
        // [V5 复查] 五 scheme 全拦（含 collection://、similar://）
        if (/^(智能文件夹:|全库搜索:|标签:|相似图片:|集合:|回收站)/.test(v) || isVirtualView(v)) return window.showToast("虚拟视图无路径可跳转", 'info');
        loadPath(v); };
    $('#pathDisplay').onclick = () => $('#pathDisplay').select();
    let searchTimer = null;
    $('#searchInput').oninput = () => { if(searchTimer) clearTimeout(searchTimer); searchTimer = setTimeout(() => { searchTimer = null;
        const q = $('#searchInput').value.trim(), scope = $('#searchScope')?.value || 'dir';
        // [U5/GAP-02] 全库范围走 /api/search 虚拟视图；目录范围维持本地筛选
        if (scope === 'library' && q) runLibrarySearch(q); else applyFilterAndSort();
    }, 300); };
    // [U5 收尾] 回车立即执行全库搜索（runLibrarySearch 内记录历史）
    $('#searchInput').onkeydown = (e) => { if (e.key !== 'Enter') return;
        const q = $('#searchInput').value.trim(), scope = $('#searchScope')?.value || 'dir';
        if (scope === 'library' && q) runLibrarySearch(q); };
    // [U5 收尾/GAP-02] 搜索历史下拉：聚焦展示、失焦收起、条目回放
    $('#searchInput').addEventListener('focus', () => window.toggleSearchHistory(true));
    $('#searchInput').addEventListener('blur', () => setTimeout(() => window.toggleSearchHistory(false), 150));
    $('#searchHistoryPanel')?.addEventListener('mousedown', (e) => {
        const del = e.target.closest('[data-sh-del]');
        if (del) { e.preventDefault(); e.stopPropagation(); window.removeSearchHistory(parseInt(del.dataset.shDel, 10)); return; }
        if (e.target.closest('[data-sh-clear]')) { e.preventDefault(); e.stopPropagation(); window.clearSearchHistory(); return; }
        const item = e.target.closest('.sh-item');
        if (item) {
            e.preventDefault();
            const it = window.loadSearchHistory()[parseInt(item.dataset.shIdx, 10)]; if (!it) return;
            $('#searchInput').value = it.type === 'q' ? it.value : '';
            window.runLibrarySearch(it.value, it.type === 'tag' ? { tag: it.value } : {});
            $('#searchInput').blur();
        }
    });
    window.clearSearch = () => { if(searchTimer) clearTimeout(searchTimer); $('#searchInput').value='';
        { const sb = $('#saveSearchBtn'); if (sb) sb.style.display = 'none'; }
        if (STATE.currentPath.startsWith('search://')) { loadPath(STATE.rootPath); } else { applyFilterAndSort(); }
        $('#searchInput').focus(); };
    const dz = $('.main-panel'); ['dragenter','dragover','dragleave','drop'].forEach(ev => dz.addEventListener(ev, e=>{e.preventDefault(); e.stopPropagation();}));
    dz.ondragenter = dz.ondragover = () => dz.classList.add('drag-over');
    dz.ondragleave = (e) => { if(!dz.contains(e.relatedTarget)) dz.classList.remove('drag-over'); };
    dz.ondrop = async (e) => {
        dz.classList.remove('drag-over'); if(!e.dataTransfer.files.length) return;
        // [U10] 虚拟视图拦截：此前直接把 smart:// 等虚拟串发给 /api/upload，
        // 用户只能收到"目标目录不在允许范围内"的困惑报错
        // [V5 复查] 五 scheme 全拦（含 collection://、similar://）
        const cur = STATE.currentPath || '';
        if (isVirtualView(cur))
            return window.showToast("虚拟视图，请先进入一个真实目录再上传", 'info');
        const fd = new FormData(); fd.append('path', cur); Array.from(e.dataTransfer.files).forEach(f=>fd.append('files',f));
        window.showToast(`上传中...`, 'info');
        // [BUG-P2-20] 上传失败/部分失败此前静默（同名冲突被后端拒绝也装作成功）
        try {
            const res = await fetch('/api/upload', { method: 'POST', body: fd });
            const data = await res.json().catch(() => null);
            if (!res.ok) { window.showToast(`上传失败: ${(data && data.error) || res.status}`, 'error'); return; }
            if (data?.errors?.length) window.showToast(data.errors[0] + (data.errors.length > 1 ? ` 等 ${data.errors.length} 项问题` : ''), 'warning');
        } catch (err) { window.showToast('上传失败: 网络错误', 'error'); }
        reloadFiles();
    };
    const container = $('#fileContainer'), scrollContainer = $('#scrollContainer');
    if (scrollContainer) scrollContainer.addEventListener('click', (e) => { if (e.target === scrollContainer || e.target === container) window.clearSelection(); });
    container.addEventListener('click', (e) => {
        const card = e.target.closest('.file-card'); if (!card) return;
        const index = parseInt(card.dataset.index);
        // [ARCH-13] 缩略图重试：原 inline onclick 随每张卡片复制
        const retry = e.target.closest('.thumb-retry');
        if (retry) { e.stopPropagation(); window.retryThumb(retry, index); return; }
        if (e.target.classList.contains('card-checkbox')) onCardClick(e, index, true); else onCardClick(e, index, false);
    });
    container.addEventListener('dblclick', (e) => { const card = e.target.closest('.file-card'); if (!card) return; const index = parseInt(card.dataset.index), f = STATE.visibleFiles[index]; if (f.type === 'dir') loadPath(f.path); else openItem(index); });
    container.addEventListener('mouseover', (e) => { const card = e.target.closest('.file-card'); if (!card || card.dataset.isAudio !== 'true') return; const index = parseInt(card.dataset.index); if (STATE.audioHoverIndex === index) return; STATE.audioHoverIndex = index; if(STATE.audioTimer) clearTimeout(STATE.audioTimer); STATE.audioTimer = setTimeout(() => startAudioPreview(index, card.dataset.audioPath), 50); });
    container.addEventListener('mouseout', (e) => { const card = e.target.closest('.file-card'); if (!card || card.contains(e.relatedTarget)) return; if (card.dataset.isAudio === 'true') { STATE.audioHoverIndex = -1; if(STATE.audioTimer) clearTimeout(STATE.audioTimer); STATE.isScrubbing = false; stopAudioPreview(); } });
    // [U7/GAP-10] 视频卡片悬停 scrub：胶片条懒加载（首次生成数秒，此后
    // 直读缓存），按悬停比例切帧——纯样式更新，<100ms
    window._stripCache = {};
    window.ensureVideoStrip = (path) => new Promise((resolve) => {
        const hit = window._stripCache[path];
        if (hit !== undefined) return resolve(hit === 'pending' ? null : hit);
        window._stripCache[path] = 'pending';
        const im = new Image();
        im.onload = () => { const entry = { w: im.naturalWidth, h: im.naturalHeight }; window._stripCache[path] = entry; resolve(entry); };
        im.onerror = () => { window._stripCache[path] = 'failed'; resolve(null); };
        im.src = `/api/thumb/strip?path=${encodeURIComponent(path)}`;
    });
    container.addEventListener('mouseover', (e) => {
        const wrap = e.target.closest('.file-thumb-wrapper[data-video-scrub]');
        if (!wrap || wrap.dataset.stripReady) return;
        window.ensureVideoStrip(wrap.dataset.videoScrub).then(entry => {
            if (!entry || !wrap.isConnected || wrap.dataset.stripReady) return;
            wrap.dataset.stripReady = '1';
            const ov = document.createElement('div');
            ov.className = 'strip-overlay';
            // [U10] encodeURIComponent 不转义单引号：文件名含 ' 会越出 CSS
            // url('...') 字符串——补 %27 转义
            ov.style.backgroundImage = `url('/api/thumb/strip?path=${encodeURIComponent(wrap.dataset.videoScrub).replace(/'/g, '%27')}')`;
            wrap.appendChild(ov);
        });
    });
    container.addEventListener('mousemove', (e) => {
        const wrap = e.target.closest('.file-thumb-wrapper[data-video-scrub]');
        if (!wrap) return;
        const ov = wrap.querySelector('.strip-overlay'); if (!ov) return;
        const r = wrap.getBoundingClientRect();
        const ratio = Math.max(0, Math.min(0.999, (e.clientX - r.left) / r.width));
        ov.style.backgroundPosition = `${Math.floor(ratio * 6) / 5 * 100}% 50%`;
    });
    container.addEventListener('mouseout', (e) => {
        const wrap = e.target.closest('.file-thumb-wrapper[data-video-scrub]');
        if (wrap && !wrap.contains(e.relatedTarget)) wrap.querySelector('.strip-overlay')?.remove();
    });
    // [U7/GAP-07] 时间线组头点击收起/展开
    container.addEventListener('click', (e) => {
        const header = e.target.closest('.tl-header'); if (!header) return;
        const group = document.getElementById(`tlg-${header.dataset.tlGroup}`);
        if (group) { const hidden = group.style.display === 'none'; group.style.display = hidden ? '' : 'none'; header.querySelector('i')?.classList.toggle('collapsed', !hidden); }
    });
    container.addEventListener('mousedown', (e) => { const card = e.target.closest('.file-card'); if (card?.dataset.isAudio === 'true' && STATE.audioInstance) { STATE.isScrubbing = true; STATE.audioInstance.pause(); scrubAudioPreview(e); } });
    container.addEventListener('mousemove', (e) => { if(STATE.isScrubbing) scrubAudioPreview(e); });
    container.addEventListener('mouseup', (e) => { if(STATE.isScrubbing){ STATE.isScrubbing = false; STATE.audioInstance?.play(); } });
    const slider = $('#sizeSlider');
    if(slider) {
        slider.oninput = () => { const val = parseInt(slider.value); document.documentElement.style.setProperty('--card-width', val + 'px'); document.documentElement.style.setProperty('--card-height', val + 'px'); persistConfig({ card_size: val }); if ((val > 180) !== STATE.useLargeThumbs) { STATE.useLargeThumbs = val > 180; refreshVisibleThumbs(); } };
        slider.value = ds.cardSize || 140; STATE.useLargeThumbs = parseInt(slider.value) > 180; slider.dispatchEvent(new Event('input'));
    }
    switchLayout(STATE.viewMode); updateSortMenuState(); updateFilterMenuState();
    // [V3/GAP-11] 恢复子文件夹聚合开关状态
    try { STATE.recursive = localStorage.getItem('fe_recursive') === '1'; } catch (e) { STATE.recursive = false; }
    { const rb = $('#btnRecursive'); if (rb) { rb.classList.toggle('btn-primary', STATE.recursive); rb.classList.toggle('btn-outline-secondary', !STATE.recursive); } }
    if (STATE.smartExpanded) { const list = $('#smartFolderList'), toggler = $('#smartFolderToggler'); if(list) { list.style.display = 'block'; toggler?.classList.add('expanded'); } }
    if (STATE.libraryExpanded) { const list = $('#pinnedLibraryList'), toggler = $('#libraryListToggler'); if(list) { list.style.display = 'block'; toggler?.classList.add('expanded'); } }
    if (STATE.sidebarCollapsed) { const panel = $('#sidebarPanel'), miniBtn = $('#sidebarToggleMini'); panel?.classList.add('collapsed'); miniBtn?.classList.add('show'); }
    await initFolderTree(); await loadPath(startPath, false); loadSmartFolders(); loadPinnedLibraries(); loadTags(); loadCollections();
    // [V2/GAP-05] 集合侧栏交互：点击打开 / 右键菜单 / 拖拽文件添加成员
    $('#collectionList')?.addEventListener('click', (e) => {
        const item = e.target.closest('[data-coll-id]');
        if (item) window.openCollectionView(item.dataset.collId, item);
    });
    $('#collectionList')?.addEventListener('contextmenu', (e) => {
        const item = e.target.closest('[data-coll-id]');
        if (item) window.showCollContextMenu(e, item.dataset.collId);
    });
    $('#collectionList')?.addEventListener('dragover', (e) => { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; });
    $('#collectionList')?.addEventListener('drop', (e) => {
        const item = e.target.closest('[data-coll-id]');
        if (!item) return;
        e.preventDefault();
        const raw = e.dataTransfer.getData('text/plain') || '';
        const dropped = decodeURIComponent(raw.replace(/^file:\/\//, '')).replace(/\\/g, '/');
        if (!dropped) return;
        // 拖动的卡片在选区内 → 整个选区入集；否则仅该文件
        const paths = STATE.selected.has(dropped) ? Array.from(STATE.selected) : [dropped];
        window.addPathsToCollection(item.dataset.collId, paths);
    });
    // [U5/GAP-01] 侧栏标签：点击 = 按标签全库检索；右键 = 删除标签
    $('#tagList')?.addEventListener('click', (e) => {
        const item = e.target.closest('.smart-folder-item');
        if (item?.dataset.tagName) {
            $$('.smart-folder-item, .tree-header').forEach(i => i.classList.remove('active'));
            item.classList.add('active');
            window.runLibrarySearch('', { tag: item.dataset.tagName });
        }
    });
    $('#tagList')?.addEventListener('contextmenu', async (e) => {
        const item = e.target.closest('.smart-folder-item');
        if (!item?.dataset.tagName) return;
        e.preventDefault();
        const name = item.dataset.tagName;
        if (!(await window.showConfirmDialog(`删除标签 "${name}"？（将从所有文件移除）`, '删除'))) return;
        const t = (STATE.allTags || []).find(x => x.name === name);
        if (t) { try { await feFetch(`/api/tags/${t.id}`, { method: 'DELETE' }, { context: '删除标签' }); loadTags(); } catch (e2) {} }
    });
    // [V2/GAP-06] 标签拖拽改父：拖 A 到 B 上 = A 变成 B 的子标签（后端防环）
    $('#tagList')?.addEventListener('dragstart', (e) => {
        const item = e.target.closest('[data-tag-id]');
        if (!item) return;
        e.dataTransfer.setData('application/x-fe-tag', item.dataset.tagId);
        e.dataTransfer.effectAllowed = 'move';
    });
    $('#tagList')?.addEventListener('dragover', (e) => {
        if (e.dataTransfer.types.includes('application/x-fe-tag')) { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; }
    });
    $('#tagList')?.addEventListener('drop', async (e) => {
        const target = e.target.closest('[data-tag-id]');
        const tid = e.dataTransfer.getData('application/x-fe-tag');
        if (!target || !tid || target.dataset.tagId === tid) return;
        e.preventDefault();
        try {
            await feFetch(`/api/tags/${tid}`, { method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ parent_id: parseInt(target.dataset.tagId, 10) }) }, { context: '调整标签层级' });
            loadTags();
        } catch (e2) {}
    });
    new IntersectionObserver(e => { if(e[0].isIntersecting) renderNextBatch(); }, {rootMargin: '200px'}).observe($('#scrollSentinel'));
    
    // --- Thumbnail Polling for Embedded Mode ---
    // 在嵌入模式下，通过轮询 /api/thumb/poll 检测缩略图更新（支持批量刷新）
    let _poll_confirmed_paths = [];  // 已确认处理的路径
    async function _pollThumbnailUpdates() {
        try {
            // 修复：使用正确的多参数格式 ?confirmed=path1&confirmed=path2
            let url = '/api/thumb/poll?';
            if (_poll_confirmed_paths.length > 0) {
                url += _poll_confirmed_paths.map(p => 'confirmed=' + encodeURIComponent(p)).join('&');
            }
            const res = await fetch(url);
            const data = await res.json();
            const paths = data.paths || [];
            
            if (paths.length > 0) {
                // 批量刷新所有待更新的缩略图
                const pathsToConfirm = [];
                for (const item of paths) {
                    if (window.refreshThumbnail) {
                        window.refreshThumbnail(item.path);
                    }
                    // [FUNC-20] 只 ACK 当前视图内实际刷新过的路径；后台目录
                    // 的条目保留在队列，等用户真正进入时再消费
                    const target = item.path.replace(/\\/g, '/').toLowerCase();
                    if (STATE.visibleFiles.some(f => f.path.replace(/\\/g, '/').toLowerCase() === target)) {
                        pathsToConfirm.push(item.path);
                    }
                }
                // 记录本次已确认的路径，下次请求时发送给后端
                _poll_confirmed_paths = pathsToConfirm;
            } else {
                // 没有待处理的项目，清空确认列表
                _poll_confirmed_paths = [];
            }
        } catch (e) {
            // [FUNC-20] 轮询失败保留 confirmed，恢复后重发（幂等），
            // 不清空以免与 60s 目录缓存叠加造成重复刷新循环
        }
    }
    // [FUNC-7] 消费后端评分广播：跨应用（主项目/独立模式）改评分后同步到当前列表
    async function _pollRatingUpdates() {
        try {
            const res = await fetch('/api/rating/poll');
            const data = await res.json();
            const updates = data.updates || [];
            if (updates.length > 0) {
                for (const item of updates) {
                    if (window.updateFileRating) window.updateFileRating(item.path, item.rating);
                }
                // ACK：让后端清掉已消费的条目
                // [BUG-P1-07] 每个参数都要自带 confirmed= 前缀（旧写法仅第一条
                // 生效，其余被后端反复推送、重复刷新）
                fetch('/api/rating/poll?' + updates.map(u => 'confirmed=' + encodeURIComponent(u.path)).join('&')).catch(() => {});
            }
        } catch (e) { /* Ignore polling errors */ }
    }
    // Start polling every 1.5 seconds（[UX-2] 页面不可见时暂停，省电省 IO）
    setInterval(() => { if (document.hidden) return; _pollThumbnailUpdates(); _pollRatingUpdates(); }, 1500);
    // [V4/GAP-17] OCR 开关状态初始化
    fetch('/api/ocr/status').then(r => r.json()).then(d => {
        window._ocrEnabled = !!d.enabled;
        if (typeof updateOcrLabel === 'function') updateOcrLabel();
    }).catch(() => {});
    // [V1/GAP-01] 索引器状态轮询（3s，独立于缩略图/评分轮询）
    window.refreshScanStatus();
    setInterval(() => { if (document.hidden) return; window.refreshScanStatus(); }, 3000);
    setupLightboxEvents(); setupContextMenu();
    // [ARCH-13] 动态列表的容器级委托：节点/条目 HTML 不再自带 inline 事件
    $('#folderTree')?.addEventListener('click', (e) => {
        const header = e.target.closest('.tree-header');
        if (header) window.onTreeClick(e, header);
    });
    $('#smartFolderList')?.addEventListener('click', (e) => {
        const item = e.target.closest('.smart-folder-item');
        if (item?.dataset.id !== undefined) window.openSmartFolder(parseInt(item.dataset.id, 10), item);
    });
    $('#smartFolderList')?.addEventListener('contextmenu', (e) => {
        const item = e.target.closest('.smart-folder-item');
        if (item?.dataset.id !== undefined) window.showSmartContextMenu(e, parseInt(item.dataset.id, 10));
    });
    $('#pinnedLibraryList')?.addEventListener('click', (e) => {
        const unpin = e.target.closest('.unpin-btn');
        if (unpin) { e.stopPropagation(); const it = unpin.closest('.smart-folder-item'); if (it?.dataset.libPath) window.unpinLibrary(it.dataset.libPath); return; }
        const item = e.target.closest('.smart-folder-item');
        if (item?.dataset.libPath) loadPath(item.dataset.libPath);
    });
    $('#smartRulesContainer')?.addEventListener('click', (e) => {
        const del = e.target.closest('.rule-del');
        if (del) del.closest('.smart-rule-row')?.remove();
    });
    const propPanel = $('#propertiesPanel');
    if (propPanel) propPanel.addEventListener('click', (e) => {
        if (e.target.classList.contains('bi-star-fill') && e.target.parentElement.classList.contains('rating-stars')) {
            const newRating = parseInt(e.target.dataset.rating), path = STATE.lastRenderedPath, file = STATE.visibleFiles.find(f => f.path === path);
            if (path && newRating) {
                if (file) file.rating = newRating;
                e.target.parentElement.querySelectorAll('.bi-star-fill').forEach((star, index) => star.classList.toggle('active', index < newRating));
                fetch('/api/update_meta', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ path: path, rating: newRating }) }).catch(console.error);
                // 通知 Python 端星级变更
                if (window.pyBridge) {
                    window.pyBridge.onRatingChanged(path, newRating);
                }
            }
            return;
        }
        // [U5/GAP-03] 颜色色板点击
        const swatch = e.target.closest('.color-swatch[data-color]');
        if (swatch && swatch.dataset.path) {
            const v = parseInt(swatch.dataset.color);
            swatch.parentElement.querySelectorAll('.color-swatch').forEach(s => s.classList.toggle('active', s === swatch));
            window.setFileColor(swatch.dataset.path, v);
            return;
        }
        // [U5/GAP-03] 旗标按钮点击（再点同一态 = 清除）
        const flagBtn = e.target.closest('.flag-btn[data-flag]');
        if (flagBtn && flagBtn.dataset.path) {
            let v = parseInt(flagBtn.dataset.flag);
            if (flagBtn.classList.contains('active')) v = 0;
            flagBtn.parentElement.querySelectorAll('.flag-btn').forEach(b => b.classList.remove('active'));
            if (v !== 0) flagBtn.classList.add('active');
            window.setFileFlag(flagBtn.dataset.path, v);
            return;
        }
        // [U5/GAP-01] 标签 chip ✕ 移除
        const chipX = e.target.closest('.chip-x[data-tag]');
        if (chipX && chipX.dataset.path) {
            window.removeFileTag(chipX.dataset.path, chipX.dataset.tag);
        }
    });
    // [UX-6] F2 重命名入口（宿主 Qt 快捷键在 Files 面板聚焦时亦转发到这里）
    window.__fxRenameSelected = () => {
        if (STATE.selected.size !== 1 || STATE.lastIndex < 0) return;
        const file = STATE.visibleFiles[STATE.lastIndex]; if (!file) return;
        togglePropertiesPanel(true);
        showFileProperties(file);
        const inp = $('#prop-filename');
        if (inp) { inp.focus(); inp.select(); }
    };
    // [UX-6] 网格列数：取第一行可见卡片数（list 布局自然得到 1）
    const gridColumns = () => {        const first = document.getElementById('card-0'); if (!first) return 1;
        const top = first.getBoundingClientRect().top; let cols = 0;
        for (let i = 0; i < Math.min(STATE.visibleFiles.length, 60); i++) {
            const c = document.getElementById(`card-${i}`); if (!c) break;
            if (Math.abs(c.getBoundingClientRect().top - top) < 4) cols++; else break;
        }
        return Math.max(1, cols);
    };
    const moveSelectionTo = (next) => {
        if (next < 0 || next >= STATE.visibleFiles.length || next === STATE.lastIndex) return;
        STATE.visuallySelected.forEach(idx => updateCardVisual(idx));
        STATE.selected.clear(); STATE.visuallySelected.clear();
        STATE.selected.add(STATE.visibleFiles[next].path); STATE.visuallySelected.add(next);
        STATE.lastIndex = next; updateCardVisual(next, true); updateToolbar();
        // [BUG-P1-09] 键盘快速导航与鼠标点击共用防抖：每步方向键此前都直接
        // 弹属性面板触发 loadDeepInfo，旧文件响应后到会覆盖新面板内容
        if (window.propertyDebounceTimer) clearTimeout(window.propertyDebounceTimer);
        window.propertyDebounceTimer = setTimeout(() => { if (STATE.lastIndex === next) showFileProperties(STATE.visibleFiles[next]); }, 200);
        document.getElementById(`card-${next}`)?.scrollIntoView({ block: 'nearest' });
    };
    document.addEventListener('keydown', e => { // 全局快捷键
        if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
        // [U10] 对比视图打开时让位（此前方向键仍在底层移动选中、Delete
        // 直接删除选中文件——非模态遮罩下主视图快捷键必须屏蔽）
        if (STATE.lightbox.active || document.querySelector('.modal.show')
            || ($('#compareView') && $('#compareView').style.display !== 'none')) return;
        const isCtrl = e.ctrlKey || e.metaKey;
        if (isCtrl && e.key.toLowerCase() === 'a') { e.preventDefault(); selectAll(); }
        else if (isCtrl && e.shiftKey && e.key.toLowerCase() === 'i') { e.preventDefault(); invertSelection(); }
        else if (isCtrl && e.key.toLowerCase() === 'c') {
            e.preventDefault();
            e.stopPropagation(); // 防止事件穿透/冒泡
            copySelected();
        }
        else if (isCtrl && e.key.toLowerCase() === 'v') { e.preventDefault(); pasteFiles(); }
        // [U6/GAP-05] 会话内撤销：重命名/删除（回收站恢复）
        else if (isCtrl && e.key.toLowerCase() === 'z') { e.preventDefault(); window.undoLast(); }
        else if (e.code === 'Space' && STATE.lastIndex !== -1 && !STATE.lightbox.active) { e.preventDefault(); openItem(STATE.lastIndex); }
        // [UX-6] 方向键网格导航 / Enter 打开 / Delete 删除 / F2 重命名 / Backspace 上级
        else if (['ArrowRight','ArrowLeft','ArrowUp','ArrowDown'].includes(e.key)) {
            e.preventDefault();
            const cols = gridColumns(), n = STATE.visibleFiles.length;
            if (!n) return;
            const cur = STATE.lastIndex;
            let next;
            if (cur < 0) next = 0;
            else if (e.key === 'ArrowRight') next = Math.min(cur + 1, n - 1);
            else if (e.key === 'ArrowLeft') next = Math.max(cur - 1, 0);
            else if (e.key === 'ArrowUp') next = Math.max(cur - cols, 0);
            else next = Math.min(cur + cols, n - 1);
            moveSelectionTo(next);
        }
        else if (e.key === 'Enter') { if (STATE.selected.size === 1 && STATE.lastIndex >= 0) { e.preventDefault(); openItem(STATE.lastIndex); } }
        else if (e.key === 'Delete') { if (STATE.selected.size) { e.preventDefault(); window.batchDelete(); } }
        else if (e.key === 'Backspace') { e.preventDefault(); window.goUp(); }
        else if (e.key === 'F2') {
            if (STATE.selected.size === 1 && STATE.lastIndex >= 0) { e.preventDefault(); window.__fxRenameSelected(); }
        }
    });
    document.addEventListener('click', e => { if(!e.target.closest('.app-menubar')) $$('.app-menubar .menu-item').forEach(m=>m.classList.remove('active')); });
    $$('.app-menubar .dropdown-item').forEach(el => el.addEventListener('click', e => { e.stopPropagation(); $$('.app-menubar .menu-item').forEach(m=>m.classList.remove('active')); }));
});

/** 插件管理逻辑 */
window.openPluginManager = async () => {
    const modalEl = document.getElementById('pluginManagerModal');
    const container = document.getElementById('pluginManagerList');
    const label = document.getElementById('pluginCountLabel');
    if(!modalEl || !container) return;

    // Show loading state
    container.innerHTML = '<div class="text-center text-muted py-4"><div class="spinner-border text-primary" role="status"></div><div class="mt-2 small">正在读取插件状态...</div></div>';
    const modal = new bootstrap.Modal(modalEl);
    modal.show();

    try {
        const res = await fetch('/api/manager/plugins');
        const data = await res.json();
        
        let html = '';
        
        // 1. Render Active Plugins
        if (data.active && data.active.length > 0) {
            data.active.forEach(p => {
                const info = p.info || {};
                const config = p.config || {};
                // Check if it's the Format Factory to add a specific Launch button
                let actionBtn = '';
                if (p.name === 'format_factory') {
                     actionBtn = `<button class="btn btn-sm btn-outline-primary ms-3" data-action="launchFormatFactory" data-stop="1"><i class="bi bi-play-fill"></i> 启动</button>`;
                }
                // [FUNC-16] 插件自报的 name/version/description/author 均转义
                html += `
                <div class="d-flex align-items-start bg-black bg-opacity-25 border border-secondary rounded p-3">
                    <div class="me-3 pt-1"><i class="bi bi-puzzle-fill text-success" style="font-size: 1.5rem;"></i></div>
                    <div class="flex-grow-1">
                        <div class="d-flex align-items-center mb-1">
                            <span class="fw-bold text-white me-2">${escapeHtml(info.name || p.name)}</span>
                            <span class="badge bg-secondary bg-opacity-50 border border-secondary text-white-50" style="font-size: 0.7rem;">${info.version ? `v${escapeHtml(String(info.version))}` : ""}</span>
                            ${actionBtn}
                        </div>
                        <div class="text-white-50 small mb-2">${escapeHtml(info.description || '暂无描述')}</div>
                        <div class="d-flex gap-3 text-muted" style="font-size: 0.75rem;">
                            <span><i class="bi bi-person me-1"></i>${escapeHtml(info.author)}</span>
                            <span><i class="bi bi-box-seam me-1"></i>${escapeHtml(p.name)}</span>
                        </div>
                    </div>
                    <div class="form-check form-switch">
                        <input class="form-check-input" type="checkbox" checked disabled title="核心插件暂不可禁用">
                    </div>
                </div>`;
            });
        } else {
            html += `<div class="text-center text-muted py-3">没有已加载的插件</div>`;
        }

        // 2. Render Failed Plugins
        if (data.failed && data.failed.length > 0) {
            html += `<div class="text-danger small fw-bold mt-4 mb-2 ps-1"><i class="bi bi-exclamation-triangle-fill me-2"></i>加载失败 (${data.failed.length})</div>`;
            data.failed.forEach(p => {
                // [FUNC-16] 失败插件的 name/error（含 Python traceback）转义
                html += `
                <div class="d-flex align-items-center bg-danger bg-opacity-10 border border-danger rounded p-3">
                    <i class="bi bi-x-circle-fill text-danger me-3" style="font-size: 1.2rem;"></i>
                    <div class="flex-grow-1">
                        <div class="fw-bold text-white mb-1">${escapeHtml(p.name)}</div>
                        <div class="text-danger small text-break">${escapeHtml(p.error)}</div>
                    </div>
                </div>`;
            });
        }

        container.innerHTML = html;
        // [BUG-P2-21] 响应结构异常时 data.active.length 此前直接 TypeError
        const activeCount = data.active?.length ?? 0, failedCount = data.failed?.length ?? 0;
        if(label) label.innerText = `运行中: ${activeCount} | 异常: ${failedCount}`;

    } catch (e) {
        // [FUNC-16] 错误消息可能回显服务端响应体，转义后再入 DOM
        container.innerHTML = `<div class="text-danger text-center py-3">获取数据失败: ${escapeHtml(e.message)}</div>`;
    }
};

// [ARCH-13] 插件管理器的"启动"按钮：data-action 委托（原 inline onclick 多语句拼接）
window.launchFormatFactory = () => {
    bootstrap.Modal.getInstance(document.getElementById('pluginManagerModal'))?.hide();
    if (window.FormatFactoryPlugin) window.FormatFactoryPlugin.open(null);
};
// [ARCH-13] "设置"菜单项：原 inline onclick 多语句 getElementById().click()
window.proxyClick = (sel) => document.querySelector(sel)?.click();

/** 菜单处理 */
function getContextPath() { return STATE.contextTarget?.path; }
window.handleMenuOpen = () => { const target = STATE.contextTarget; if (!target) return; if (target.type === 'file') return openItem(target.index); if (target.type === 'tree') return loadPath(target.path); };
window.handleMenuNewFolder = async () => { // 新建文件夹逻辑
    // [BUG-P3-46] 虚拟视图下执行必失败且无反馈，直接拦截
    // [V5 复查] 五 scheme 全拦（含 collection://、similar://）
    if (isVirtualView(STATE.currentPath)) return window.showToast("虚拟视图中无法新建文件夹", 'info');
    const parentPath = getContextPath(); if (!parentPath) return;
    // [UX-5] 原生 prompt → 应用内 Modal
    const name = await window.showInputDialog("请输入新文件夹名称:", "新建文件夹"); if (!name) return;
    try {
        const res = await fetch('/api/mkdir', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ path: parentPath, name: name }) }), data = await res.json();
        if (data.success) {
            if (parentPath === STATE.currentPath) reloadFiles();
            const newPath = (parentPath.endsWith('/') ? parentPath : parentPath + '/') + name, parentNode = $(`.tree-node-container[data-path="${cssPathSel(parentPath)}"]`);
            let container = null, lvl = 0;
            if (parentNode) { const toggler = parentNode.querySelector('.tree-toggler'), childrenContainer = parentNode.querySelector('.tree-children'); if (toggler?.classList.contains('expanded') && childrenContainer) { container = childrenContainer; lvl = (parseInt(parentNode.querySelector('.tree-header').style.paddingLeft || 10) - 10) / 15 + 1; } } 
            else if (parentPath === STATE.rootPath || parentPath === '') { container = $('#folderTree'); lvl = 0; }
            if (container) { const tempDiv = document.createElement('div'); tempDiv.innerHTML = createTreeNodeHtml(name, newPath, lvl); const newNode = tempDiv.firstElementChild, referenceNode = findInsertPosition(container, name); container.insertBefore(newNode, referenceNode); }
        } else window.showToast("⚠️ " + (data.message || "创建失败"), 'error');
    } catch (e) { console.error(e); }
};
window.handleMenuRename = async () => { // 重命名逻辑
    const path = getContextPath(); if (!path) return;
    const oldName = path.split(/[/\\]/).pop();
    // [UX-5] 原生 prompt → 应用内 Modal
    const newName = await window.showInputDialog("重命名:", oldName);
    if (newName && newName !== oldName) {
        // [BUG-P1-06] 服务端拒绝（重名/非法字符）时不得改写树节点——
        // 此前无条件 .then 导致树与磁盘不一致
        window.doRename(path, newName).then((ok) => {
            if (!ok) return;
            const node = $(`.tree-node-container[data-path="${cssPathSel(path)}"]`);
            if (node) {
                const parentPath = path.substring(0, path.lastIndexOf('/') + 1), newPath = parentPath + newName;
                const updateNodePath = (targetNode, oldP, newP) => { targetNode.dataset.path = newP; targetNode.querySelectorAll('.tree-node-container').forEach(child => { const childOldPath = child.dataset.path; if (childOldPath.startsWith(oldP + '/')) child.dataset.path = newP + childOldPath.substring(oldP.length); }); };
                updateNodePath(node, path, newPath); const nameSpan = node.querySelector('.tree-header span:not(.tree-toggler)'); if (nameSpan) nameSpan.innerText = newName;
                const container = node.parentNode; if (container) { node.remove(); container.insertBefore(node, findInsertPosition(container, newName)); }
            }
        });
    }
};
window.handleMenuDelete = async () => { // 删除逻辑
    const target = STATE.contextTarget; if (!target) return;
    let paths = target.type === 'file' ? Array.from(STATE.selected) : [target.path];
    // [UX-5] 原生 confirm → 应用内 Modal
    if (!paths.length || !(await window.showConfirmDialog(`确定要将选中的 ${paths.length} 个项目移至回收站吗？（30 天内可恢复，Ctrl+Z 撤销）`, '移至回收站'))) return;
    try {
        const res = await fetch('/api/delete', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ paths }) }), data = await res.json();
        if (data.deleted?.length) {
            // [U6/GAP-05] 入站记录进撤销栈（Ctrl+Z 恢复）
            if (data.records?.length) window.pushUndo({ type: 'delete', trashIds: data.records.filter(r => !r.in_os_trash).map(r => r.id), dir: STATE.currentPath });
            let needJump = false, jumpTarget = STATE.rootPath; const normCurrent = STATE.currentPath.replace(/\\/g, '/');
            for (const p of data.deleted) {
                // [BUG-P3-50] needJump/jumpTarget 计算此前整段重复两遍（复制粘贴残留）
                const normP = p.replace(/\\/g, '/').replace(/\/$/, ''), node = $(`.tree-node-container[data-path="${cssPathSel(normP)}"]`);
                if (normCurrent === normP || normCurrent.startsWith(normP + '/')) { needJump = true; const lastSep = normP.lastIndexOf('/'); jumpTarget = (lastSep !== -1 ? normP.substring(0, lastSep) : '') || STATE.rootPath; }
                if (node) node.remove();
            }
            if (needJump) { await loadPath(jumpTarget); $$('.tree-header').forEach(h => h.classList.remove('active')); const parentNode = $(`.tree-node-container[data-path="${cssPathSel(jumpTarget)}"]`); if (parentNode) parentNode.querySelector('.tree-header')?.classList.add('active'); } 
            else reloadFiles();
            if (target.type === 'file') { STATE.selected.clear(); STATE.visuallySelected.clear(); updateToolbar(); togglePropertiesPanel(false); }
        }
    } catch (e) { console.error(e); }
};
window.handleMenuSysOpen = () => { const path = getContextPath(); if (path) openSystem(path); };
window.handleMenuReveal = () => { const path = getContextPath(); if (path) feFetch('/api/reveal', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ path }) }, { context: '定位文件' }).catch(() => {}); };
window.handleMenuCopy = () => {
    const target = STATE.contextTarget;
    if (!target) return;
    let paths = target.type === 'file' ? Array.from(STATE.selected) : [target.path];
    if (paths.length) {
        // [BUG-P2-20] 网络失败此前无提示
        feFetch('/api/action/copy', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ paths })
        }, { context: '复制', silent: true }).then(data => {
            if (data && data.status === 'ok') window.showToast(`已复制 ${paths.length} 个文件`, 'success');
        }).catch(() => window.showToast('复制请求失败', 'error'));
    }
};
window.handleMenuPaste = () => { const path = getContextPath();
    // [U10] 背景右键菜单在任何视图都显示粘贴项——虚拟路径直接发给
    // CopyWorker 会按进程 CWD 解析出意外目标。复用 pasteFiles 的守卫
    // [V5 复查] 五 scheme 全拦（含 collection://、similar://）
    if (path && isVirtualView(path)) return window.showToast("虚拟视图，无法粘贴", 'info');
    if (path) feFetch('/api/action/paste', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ path }) }, { context: '粘贴', silent: true }).catch(() => window.showToast('粘贴请求失败', 'error')); };
window.toggleMenu = (el) => { const act = el.classList.contains('active'); $$('.app-menubar .menu-item').forEach(m=>m.classList.remove('active')); if(!act) el.classList.add('active'); };
window.openFolderDialog = () => feFetch('/api/action/open_folder_dialog',{method:'POST'}, { context: '打开目录选择框' }).catch(() => {});
// [BUG-P2-20] 截图触发失败此前静默
window.triggerPlayerSnapshot = () => feFetch('/api/action/snapshot', { method: 'POST' }, { context: '截图', silent: true }).catch(() => window.showToast('截图请求失败（播放器未激活？）', 'error'));
// [UX-3] 缩略图手动重试：清除后端失败缓存并重新排队
window.retryThumb = async (el, idx) => {
    const f = STATE.visibleFiles[idx]; if (!f) return;
    try { await feFetch('/api/thumb/retry', { method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ path: f.path, size: STATE.useLargeThumbs ? 'large' : 'small' }) }, { context: '重试缩略图' }); }
    catch (e) {}
    // 隐藏失败占位、重新请求（新 v 防缓存）；未生成完前显示占位 logo，
    // 生成完成后由轮询 refreshThumbnail 刷新
    const fb = el.closest('.fallback-icon'); if (fb) fb.style.display = 'none';
    const img = $(`#card-${idx} .file-thumb`);
    if (img) {
        img.src = `/api/thumb?path=${encodeURIComponent(f.path)}&v=${Date.now()}${STATE.useLargeThumbs ? '&size=large' : '&size=small'}`;
        img.style.display = 'block';
    }
};
window.refreshThumbnail = (path) => {    const target = path.replace(/\\/g,'/').toLowerCase(), idx = STATE.visibleFiles.findIndex(f => f.path.replace(/\\/g,'/').toLowerCase() === target);
    if (idx === -1) return; STATE.visibleFiles[idx].t_ver = Date.now(); const img = $(`#card-${idx} .file-thumb`);
    if (img) { const sizeParam = STATE.useLargeThumbs ? 'large' : 'small'; img.src = `/api/thumb?path=${encodeURIComponent(STATE.visibleFiles[idx].path)}&v=${STATE.visibleFiles[idx].t_ver}&size=${sizeParam}`; const fallback = img.nextElementSibling; if (fallback?.classList.contains('fallback-icon')) fallback.style.display = 'none'; img.style.display = 'block'; }
};
window.showPasteProgress = () => { $('#pasteProgressPanel').classList.add('show'); $('#pasteProgressBar').style.width='0%'; };
window.updatePasteProgress = (p, s) => { $('#pasteProgressBar').style.width = p+'%'; $('#pasteSpeed').innerText = s; };
window.hidePasteProgress = () => { $('#pasteProgressBar').style.width='100%'; $('#pasteProgressBar').classList.add('bg-success'); setTimeout(()=>{ $('#pasteProgressPanel').classList.remove('show'); $('#pasteProgressBar').classList.remove('bg-success'); }, 1000); };
// [FUNC-7] 后端评分广播落点：更新内存数据，并在属性面板展示该文件时同步星标
// （triggerPlayerSnapshot 已移至 openFolderDialog 旁，统一 feFetch）

/** 文件操作逻辑实现 */
window.doRename = async (oldPath, newName) => { // 执行重命名 API 调用；[BUG-P1-06] 返回是否成功，调用方可据此决定后续 UI 更新
    window._isRenamingPath = oldPath;
    try {
        const res = await fetch('/api/rename', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ old_path: oldPath, new_name: newName }) }), data = await res.json();
        if (data.success) {
            // [FUNC-21] 保留分隔符计算父目录：旧实现根目录文件重命名丢 '/'
            const slash = oldPath.lastIndexOf('/');
            const newPath = slash === -1 ? newName : oldPath.substring(0, slash + 1) + newName;
            // [U6/GAP-05] 重命名入撤销栈（Ctrl+Z 还原，树同步走 doRename 常规路径）
            window.pushUndo({ type: 'rename', from: oldPath, to: newPath });
            const nC = STATE.currentPath.replace(/\\/g, '/'), nO = oldPath.replace(/\\/g, '/'), nN = newPath.replace(/\\/g, '/');
            if (nC === nO || nC.startsWith(nO + '/')) { STATE.currentPath = nN + nC.substring(nO.length); const pInp = $('#pathDisplay'); if(pInp) pInp.value = STATE.currentPath; }
            window.showToast("重命名成功", 'success'); STATE.pendingSelection = newPath; reloadFiles();
            return true;
        } else { window.showToast(data.message || "重命名失败", 'error'); const inp = $('#prop-filename'); if(inp) inp.value = oldPath.split(/[/\\]/).pop(); return false; }
    } catch (e) { console.error(e); window.showToast("请求失败", 'error'); return false; }
    finally { window._isRenamingPath = null; }
};
window.doUpdateMeta = async (path, comments) => { // 更新备注 API 调用
    const f = STATE.visibleFiles.find(f => f.path === path); if(f) f.comments = comments;
    try { await fetch('/api/update_meta', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ path: path, comments: comments }) }); } 
    catch (e) { console.error(e); window.showToast("保存备注失败", 'error'); }
};

/** 批量操作逻辑 */
window.applyBatchRename = async () => {
    const files = Array.from(STATE.selected); if (!files.length) return; const mode = $('#renameModeSelect').value, params = {};
    if (mode === 'replace') { params.find = $('#renameFind')?.value || ''; params.replace = $('#renameReplace')?.value || ''; if (!params.find) return window.showToast("请输入查找内容", 'error'); }
    else if (mode === 'template') { params.template = $('#renameTemplate')?.value || ''; if (!params.template) return window.showToast("请输入重命名模板", 'error'); params.start = $('#renameStart')?.value || 1; }
    else { params.prefix = $('#renamePrefix')?.value || ''; params.start = $('#renameStart')?.value || 1; params.digits = $('#renameDigits')?.value || 2; }
    const btn = $('#batch-tab-rename button'), oldText = btn.innerHTML; btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 处理中...';
    try { const res = await fetch('/api/batch/rename', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ files, mode, params }) }), data = await res.json(); if (data.success) { window.showToast(`成功重命名 ${data.processed} 个文件`, 'success'); if (data.errors?.length) { console.error(data.errors); window.showToast(`发生 ${data.errors.length} 个错误`, 'warning'); } reloadFiles(); } else window.showToast(data.message || "操作失败", 'error'); } 
    catch (e) { window.showToast("请求错误: " + e, 'error'); } finally { btn.disabled = false; btn.innerHTML = oldText; }
};
window.applyBatchMetadata = async () => {
    const files = Array.from(STATE.selected); if (!files.length) return;
    const tagsInp = $('#batch-tab-metadata input'), tags = tagsInp ? tagsInp.value.trim() : '', rating = window.BATCH_RATING || 0;
    const color = window.BATCH_COLOR, flag = window.BATCH_FLAG;
    if (rating === 0 && !tags && color === null && flag === null) return window.showToast("请至少设置评分/标签/颜色/旗标", 'warning');
    const btn = $('#batch-tab-metadata button'), oldText = btn.innerHTML; btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 处理中...';
    try { const res = await fetch('/api/batch/meta', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ files, rating, tags, color, flag }) }), data = await res.json(); if (data.success) { window.showToast("元数据已更新", 'success'); reloadFiles(); } else window.showToast(data.message || "更新失败", 'error'); }
    catch (e) { window.showToast("请求错误: " + e, 'error'); } finally { btn.disabled = false; btn.innerHTML = oldText; if(tagsInp) tagsInp.value = ''; window.setBatchRating(0); window.BATCH_COLOR = null; window.BATCH_FLAG = null; $$('#batchColorRow .color-swatch, #batch-tab-metadata .flag-btn').forEach(el => el.classList.remove('active')); }
};

// ============================================================================
// [U5] 组织能力：标签体系 2.0 + 全库搜索 + 颜色/旗标
// ============================================================================

window.loadTags = async () => { // 标签列表 → STATE.allTags + 侧栏渲染（[V2/GAP-06] 层级树）
    const list = $('#tagList'); if (!list) return;
    try {
        const tags = await feFetch('/api/tags', {}, { context: '加载标签', silent: true });
        STATE.allTags = tags || [];
        if (!STATE.allTags.length) { list.innerHTML = '<div class="menu-item text-muted small px-3">暂无标签 — 选中文件后在属性面板添加</div>'; return; }
        // 层级排序：根按名称，子孙跟在父后（DFS），缩进 = 深度
        const childrenOf = {};
        const byId = {};
        STATE.allTags.forEach(t => { byId[t.id] = t; (childrenOf[t.parent_id || 0] = childrenOf[t.parent_id || 0] || []).push(t); });
        Object.values(childrenOf).forEach(arr => arr.sort((a, b) => a.name.localeCompare(b.name)));
        const ordered = [];
        const visit = (node, depth, chain) => {
            ordered.push({ tag: node, depth, path: chain });
            (childrenOf[node.id] || []).forEach(ch => visit(ch, depth + 1, chain + '/' + node.name));
        };
        (childrenOf[0] || []).forEach(root => visit(root, 0, ''));
        // 防环兜底：未输出的（孤儿/环）按平级追加
        const seen = new Set(ordered.map(o => o.tag.id));
        STATE.allTags.forEach(t => { if (!seen.has(t.id)) ordered.push({ tag: t, depth: 0, path: '' }); });
        list.innerHTML = ordered.map(({ tag: t, depth, path: chain }) => {
            const display = chain ? `${chain}/${t.name}` : t.name;
            return `<div class="smart-folder-item" data-tag-name="${escapeHtml(t.name)}" data-tag-id="${t.id}" draggable="true" title="${escapeHtml(display)}（拖到其他标签上可设为子标签）" style="padding-left:${8 + depth * 14}px;"><span class="tag-color-dot" style="background:${escapeHtml(t.color || '#4DACFF')}"></span><i class="bi bi-caret-right ${depth > 0 ? 'd-none' : ''}" style="font-size:9px;color:#666;"></i><span class="text-truncate" style="flex:1;">${escapeHtml(t.name)}</span><span class="tag-count">${t.count || 0}</span></div>`;
        }).join('');
    } catch (e) { /* feFetch 已 toast（silent 除外） */ }
};

window.runLibrarySearch = async (q, opts = {}) => { // 全库搜索虚拟视图（search://）
    const seq = (window._loadSeq = (window._loadSeq || 0) + 1);
    const display = opts.tag ? `标签: ${opts.tag}` : `全库搜索: ${q}`;
    STATE.currentPath = opts.tag ? `search://tag/${encodeURIComponent(opts.tag)}` : `search://q/${encodeURIComponent(q)}`;
    const pInp = $('#pathDisplay'); if (pInp) pInp.value = display;
    window.renderBreadcrumb?.(STATE.currentPath);
    // [U5 收尾/GAP-02] 搜索历史 + "保存为智能文件夹"入口
    window.recordSearchHistory(q, opts.tag || '');
    { const sb = $('#saveSearchBtn'); if (sb) sb.style.display = 'inline'; }
    STATE.selected.clear(); STATE.visuallySelected.clear(); updateToolbar(); togglePropertiesPanel(false);
    window.__FE.unobserveThumbImgs($('#fileContainer')); $('#fileContainer').innerHTML = ''; $('#emptyState').classList.add('d-none');
    STATE.thumbLoading.total = 0; STATE.thumbLoading.loaded = 0; updateThumbProgress();
    try {
        const params = new URLSearchParams();
        if (opts.tag) params.set('tag', opts.tag); else params.set('q', q);
        const data = await feFetch(`/api/search?${params}`, {}, { context: '全库搜索', silent: true });
        if (seq !== window._loadSeq) return;
        // [U10] 后端异常分支是 HTTP 200 + {error}（feFetch 只查 res.ok）：
        // 丢弃 error 会把查询失败显示成"0 结果"
        if (data.error) throw new Error(data.error);
        STATE.allFiles = data.files || [];
        applyFilterAndSort();
        const cnt = $('#searchCount');
        if (cnt) { cnt.style.display = 'inline'; cnt.textContent = `全库 ${STATE.allFiles.length} 项`; }
    } catch (e) {
        if (seq !== window._loadSeq) return;
        $('#emptyState').innerText = '⚠️ 搜索失败';
        $('#emptyState').classList.remove('d-none');
    }
};

// [U5 收尾/GAP-02] 搜索历史（localStorage，最多 12 条，标签搜索同样入史）
const SEARCH_HISTORY_KEY = 'fe_search_history';
// [U10] 合法 JSON 但非数组（如 "5"/{"a":1}）时返回 []：filter 前的
// TypeError 会把 runLibrarySearch 整个炸掉（全库搜索瘫痪且无自愈）
window.loadSearchHistory = () => { try { const v = JSON.parse(localStorage.getItem(SEARCH_HISTORY_KEY)); return Array.isArray(v) ? v : []; } catch (e) { return []; } };
const saveSearchHistory = (list) => { try { localStorage.setItem(SEARCH_HISTORY_KEY, JSON.stringify(list.slice(0, 12))); } catch (e) {} };
window.recordSearchHistory = (q, tag = '') => {
    const entry = tag ? { type: 'tag', value: tag } : { type: 'q', value: (q || '').trim() };
    if (!entry.value) return;
    const list = window.loadSearchHistory().filter(it => !(it.type === entry.type && it.value === entry.value));
    list.unshift(entry); saveSearchHistory(list); renderSearchHistory();
};
window.removeSearchHistory = (idx) => { const list = window.loadSearchHistory(); list.splice(idx, 1); saveSearchHistory(list); renderSearchHistory(); };
window.clearSearchHistory = () => { saveSearchHistory([]); renderSearchHistory(); };
window.renderSearchHistory = () => {
    const panel = $('#searchHistoryPanel'); if (!panel) return;
    const list = window.loadSearchHistory();
    if (!list.length) { panel.innerHTML = '<div class="sh-empty">暂无搜索历史</div>'; return; }
    panel.innerHTML = list.map((it, i) => `<div class="sh-item" data-sh-idx="${i}"><i class="bi ${it.type === 'tag' ? 'bi-tag' : 'bi-clock-history'}"></i><span class="sh-text text-truncate">${escapeHtml(it.value)}</span><i class="bi bi-x-lg sh-del" data-sh-del="${i}" title="删除此条"></i></div>`).join('')
        + '<div class="sh-footer" data-sh-clear>清空历史</div>';
};
window.toggleSearchHistory = (show) => {
    const panel = $('#searchHistoryPanel'); if (!panel) return;
    if (show) { renderSearchHistory(); panel.style.display = 'block'; } else panel.style.display = 'none';
};
// [U5 收尾/GAP-02] "保存为智能文件夹"：把 search://q/<q> 或 search://tag/<tag>
// 转成同语义规则（后端 execute_smart_query 的 q/tag 规则）预填构建器
window.saveSearchAsSmartFolder = () => {
    const p = STATE.currentPath || '';
    let rules = null;
    if (p.startsWith('search://tag/')) rules = { tag: decodeURIComponent(p.slice('search://tag/'.length)) };
    else if (p.startsWith('search://q/')) rules = { q: decodeURIComponent(p.slice('search://q/'.length)) };
    if (!rules) return window.showToast("仅全库搜索结果可保存为智能文件夹", 'info');
    window.openSmartBuilder(null, rules);
};

// [U5/GAP-01] 标签增删（后端自动维护 legacy 逗号列与 tags 表双向一致）
window.addFileTag = async (path, name) => {
    try {
        const data = await feFetch('/api/meta/tags', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ path, add: [name] }) }, { context: '添加标签' });
        if (data?.success) { window._applyTagUpdate(path, data.tags); loadTags(); }
    } catch (e) {}
};
window.removeFileTag = async (path, name) => {
    try {
        const data = await feFetch('/api/meta/tags', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ path, remove: [name] }) }, { context: '移除标签' });
        if (data?.success) { window._applyTagUpdate(path, data.tags); loadTags(); }
    } catch (e) {}
};
window._applyTagUpdate = (path, tags) => {
    const target = path.replace(/\\/g, '/').toLowerCase();
    const hit = f => f.path.replace(/\\/g, '/').toLowerCase() === target;
    const tagStr = (tags || []).join(',');
    const all = STATE.allFiles.find(hit); if (all) all.tags = tagStr;
    const vis = STATE.visibleFiles.find(hit); if (vis) vis.tags = tagStr;
    if (STATE.lastRenderedPath && STATE.lastRenderedPath.replace(/\\/g, '/').toLowerCase() === target) {
        const chips = $('#prop-tag-chips');
        if (chips) chips.innerHTML = (tags || []).map(t => `<span class="tag-chip">${escapeHtml(t)}<i class="chip-x" data-tag="${escapeHtml(t)}" data-path="${escapeHtml(path)}" title="移除标签">✕</i></span>`).join('');
    }
};

// ============================================================================
// [V2/GAP-05] 静态集合：侧栏管理 + collection:// 虚拟视图 + 拖拽添加成员
// ============================================================================

window.toggleCollectionList = () => { const list = $('#collectionList'), toggler = $('#collectionListToggler'); if (!list || !toggler) return; const isHidden = list.style.display === 'none'; list.style.display = isHidden ? 'block' : 'none'; toggler.classList.toggle('expanded', isHidden); };
window.loadCollections = async () => {
    const list = $('#collectionList'); if (!list) return;
    try {
        const cols = await feFetch('/api/collections', {}, { context: '加载集合', silent: true });
        STATE.collectionCache = cols || [];
        if (!STATE.collectionCache.length) { list.innerHTML = '<div class="menu-item text-muted small px-3">暂无集合 — 点右上 + 新建，把文件拖进来</div>'; return; }
        list.innerHTML = STATE.collectionCache.map(c => `<div class="smart-folder-item" data-coll-id="${c.id}" data-coll-name="${escapeHtml(c.name)}"><i class="bi bi-collection text-warning"></i><span class="text-truncate" style="flex:1;" title="${escapeHtml(c.name)}">${escapeHtml(c.name)}</span><span class="tag-count">${c.count || 0}</span></div>`).join('');
    } catch (e) { /* silent */ }
};
window.createCollection = async () => {
    const name = await window.showInputDialog('新建集合', '新建集合');
    if (!name) return;
    try {
        await feFetch('/api/collections', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ name }) }, { context: '新建集合' });
        loadCollections();
        window.showToast(`集合「${name}」已创建，把文件拖到它上面即可添加`, 'success');
    } catch (e) {}
};
window.openCollectionView = async (id, el) => {
    const coll = (STATE.collectionCache || []).find(c => String(c.id) === String(id));
    const name = (el?.dataset?.collName) || coll?.name || `集合 #${id}`;
    $$('.smart-folder-item, .tree-header').forEach(i => i.classList.remove('active'));
    if (el) el.classList.add('active');
    STATE.currentPath = `collection://${id}`;
    // [V5 复查] 进入虚拟视图清掉滞留的 pendingSelection：此前重命名后切到
    // 集合视图不消费它，之后进真实目录会被误选中并弹属性面板
    STATE.pendingSelection = null;
    const pInp = $('#pathDisplay'); if (pInp) pInp.value = `集合: ${name}`;
    window.renderBreadcrumb?.(STATE.currentPath);
    STATE.selected.clear(); STATE.visuallySelected.clear(); updateToolbar(); togglePropertiesPanel(false);
    { const sb = $('#saveSearchBtn'); if (sb) sb.style.display = 'none'; }
    if ($('#searchInput')) { $('#searchInput').value = ''; $('#searchClear').style.display = 'none'; }
    { const cnt = $('#searchCount'); if (cnt) cnt.style.display = 'none'; }
    STATE.allFiles = []; STATE.visibleFiles = []; STATE.renderIdx = 0;
    STATE.thumbLoading.total = 0; STATE.thumbLoading.loaded = 0; updateThumbProgress();
    window.__FE.unobserveThumbImgs($('#fileContainer')); $('#fileContainer').innerHTML = '';
    $('#emptyState').classList.add('d-none');
    const seq = (window._loadSeq = (window._loadSeq || 0) + 1);
    try {
        const data = await feFetch(`/api/collections/${id}/items`, {}, { context: '集合' });
        if (seq !== window._loadSeq) return;
        STATE.allFiles = data.files || [];
        applyFilterAndSort();
        if (!STATE.allFiles.length) { $('#emptyState').innerText = '集合为空 — 回到目录视图，把文件拖到侧栏集合上'; $('#emptyState').classList.remove('d-none'); }
    } catch (e) {
        if (seq !== window._loadSeq) return;
        $('#emptyState').innerText = '⚠️ 集合加载失败'; $('#emptyState').classList.remove('d-none');
    }
};
window.showCollContextMenu = (e, id) => {
    e.preventDefault(); e.stopPropagation();
    const menu = $('#collectionContextMenu'); if (!menu) return;
    const coll = (STATE.collectionCache || []).find(c => String(c.id) === String(id));
    if (!coll) return;
    let x = e.clientX, y = e.clientY;
    if (x + 160 > window.innerWidth) x -= 160; if (y + 90 > window.innerHeight) y -= 90;
    menu.style.cssText = `left:${x}px; top:${y}px; display:block;`;
    STATE.collContextData = { id: coll.id, name: coll.name };
    const sm = $('#smartContextMenu'); if (sm) sm.style.display = 'none';
};
window.handleCollRename = async () => {
    const d = STATE.collContextData; const menu = $('#collectionContextMenu'); if (menu) menu.style.display = 'none';
    if (!d) return;
    const name = await window.showInputDialog('重命名集合', d.name);
    if (!name || name === d.name) return;
    try { await feFetch(`/api/collections/${d.id}`, { method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ name }) }, { context: '重命名集合' }); loadCollections(); if (STATE.currentPath === `collection://${d.id}`) window.reloadFiles(); } catch (e) {}
};
window.handleCollDelete = async () => {
    const d = STATE.collContextData; const menu = $('#collectionContextMenu'); if (menu) menu.style.display = 'none';
    if (!d) return;
    if (!(await window.showConfirmDialog(`删除集合「${d.name}」？（不删除文件本身）`, '删除集合'))) return;
    try {
        await feFetch(`/api/collections/${d.id}`, { method: 'DELETE' }, { context: '删除集合' });
        loadCollections();
        if (STATE.currentPath === `collection://${d.id}`) loadPath(STATE.rootPath);
        window.showToast('集合已删除', 'success');
    } catch (e) {}
};
window.addPathsToCollection = async (collId, paths) => {
    if (!collId || !paths?.length) return;
    try {
        const data = await feFetch(`/api/collections/${collId}/items`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ action: 'add', paths }) }, { context: '添加到集合' });
        window.showToast(`已加入集合（共 ${data.count} 项）`, 'success');
        loadCollections();
    } catch (e) {}
};
window.removeFromCollection = async () => {
    const menu = $('#contextMenu'); if (menu) menu.style.display = 'none';
    const cur = STATE.currentPath || '';
    if (!cur.startsWith('collection://')) return;
    const collId = cur.slice('collection://'.length);
    const target = STATE.contextTarget;
    const paths = target?.type === 'file' ? Array.from(STATE.selected) : (target ? [target.path] : []);
    if (!paths.length) return;
    try {
        await feFetch(`/api/collections/${collId}/items`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ action: 'remove', paths }) }, { context: '从集合移除' });
        window.showToast('已从集合移除（文件保留原位）', 'success');
        window.reloadFiles();
    } catch (e) {}
};

// ============================================================================
// [V2/GAP-07] 堆叠：右键堆叠/取消 + 封面 ×N 角标展开
// ============================================================================

window.stackSelected = async () => {
    const menu = $('#contextMenu'); if (menu) menu.style.display = 'none';
    const paths = Array.from(STATE.selected);
    if (paths.length < 2) return window.showToast('请至少选择 2 个文件', 'warning');
    const cover = STATE.lastIndex >= 0 ? STATE.visibleFiles[STATE.lastIndex]?.path : null;
    try {
        const data = await feFetch('/api/stacks', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ paths, cover }) }, { context: '堆叠' });
        if (data.success) { window.showToast(`已堆叠 ${paths.length} 个文件（网格只显示封面）`, 'success'); window.reloadFiles(); }
    } catch (e) {}
};
window.unstackSelected = async () => {
    const menu = $('#contextMenu'); if (menu) menu.style.display = 'none';
    const ids = new Set();
    for (const p of STATE.selected) { const f = STATE.visibleFiles.find(x => x.path === p); if (f?.stack_id) ids.add(f.stack_id); }
    const target = STATE.contextTarget;
    if (target?.type === 'file') { const f = STATE.visibleFiles[target.index]; if (f?.stack_id) ids.add(f.stack_id); }
    if (!ids.size) return window.showToast('所选文件不在堆叠中', 'info');
    try {
        for (const sid of ids) await feFetch(`/api/stacks/${sid}/unstack`, { method: 'POST' }, { context: '取消堆叠' });
        ids.forEach(sid => STATE.expandedStacks.delete(sid));
        window.showToast('已取消堆叠', 'success');
        window.reloadFiles();
    } catch (e) {}
};
window.toggleStackExpand = (sid) => {
    // [V5 复查] 委托层把 data-args 数组按位置传参——此前签名收 el 读
    // dataset.stackId，而委托根本不注入 data-stack-id，点击永远静默无效
    sid = parseInt(sid, 10);
    if (!sid) return;
    if (STATE.expandedStacks.has(sid)) STATE.expandedStacks.delete(sid); else STATE.expandedStacks.add(sid);
    applyFilterAndSort();
    return false;
};

// ============================================================================
// [V2/GAP-04] XMP 元数据写回设置 + 反向导入
// ============================================================================

window.setMetaEmbedMode = async (mode) => {
    if (!['sidecar', 'inplace', 'off'].includes(mode)) return;
    try {
        await fetch('/api/config', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ meta_embed: mode }) });
        window.showToast(mode === 'sidecar' ? '已设为旁车 .xmp 模式' : mode === 'inplace' ? '已设为直接写入文件模式' : '已关闭元数据写回', 'success');
    } catch (e) { window.showToast('设置保存失败', 'error'); }
};
window.runXmpImport = async () => {
    if (!(await window.showConfirmDialog('扫描全部素材库，从文件的 XMP（或 .xmp 旁车）读取评分/标签。仅当数据库中该文件还没有标记时才采用，不会覆盖现有数据。', '开始导入'))) return;
    window.showToast('正在扫描 XMP 标记（大库可能需要一点时间）...', 'info');
    try {
        const data = await feFetch('/api/meta/import_xmp', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}' }, { timeout: 300000, context: 'XMP 导入' });
        window.showToast(`XMP 导入完成：采纳 ${data.adopted ?? 0} 项，已有标记保留 ${data.conflicts ?? 0} 项`, 'success');
        loadTags(); window.reloadFiles();
    } catch (e) {}
};

// ============================================================================
// [V3/GAP-08] 查找相似（pHash）+ [GAP-09] 自动颜色筛选 + [GAP-11] 递归聚合
// ============================================================================

window.runSimilarFromContext = () => {
    const menu = $('#contextMenu'); if (menu) menu.style.display = 'none';
    const path = getContextPath();
    if (path) window.runSimilarSearch(path);
};
window.runSimilarSearch = async (path) => {
    const seq = (window._loadSeq = (window._loadSeq || 0) + 1);
    STATE.currentPath = `similar://${encodeURIComponent(path)}`;
    STATE.pendingSelection = null; // [V5 复查] 虚拟视图入口统一清滞留选中
    const pInp = $('#pathDisplay'); if (pInp) pInp.value = `相似图片: ${path.split('/').pop()}`;
    window.renderBreadcrumb?.(STATE.currentPath);
    STATE.selected.clear(); STATE.visuallySelected.clear(); updateToolbar(); togglePropertiesPanel(false);
    { const sb = $('#saveSearchBtn'); if (sb) sb.style.display = 'none'; }
    window.__FE.unobserveThumbImgs($('#fileContainer')); $('#fileContainer').innerHTML = ''; $('#emptyState').classList.add('d-none');
    STATE.thumbLoading.total = 0; STATE.thumbLoading.loaded = 0; updateThumbProgress();
    try {
        const data = await feFetch(`/api/similar?path=${encodeURIComponent(path)}`, {}, { timeout: 60000, context: '查找相似', silent: true });
        if (seq !== window._loadSeq) return;
        if (data.error) throw new Error(data.error);
        STATE.allFiles = data.files || [];
        applyFilterAndSort();
        const cnt = $('#searchCount');
        if (cnt) { cnt.style.display = 'inline'; cnt.textContent = `相似 ${STATE.allFiles.length} 项（距离 ≤ ${data.threshold}）`; }
        if (!STATE.allFiles.length) { $('#emptyState').innerText = '没有找到相似的文件（先生成缩略图可提升覆盖）'; $('#emptyState').classList.remove('d-none'); }
    } catch (e) {
        if (seq !== window._loadSeq) return;
        $('#emptyState').innerText = `⚠️ ${e.message || '相似搜索失败'}`;
        $('#emptyState').classList.remove('d-none');
    }
};
window.setFilterDomBucket = (v, el) => {
    STATE.filter.domBucket = (v === '' || v === null) ? '' : parseInt(v, 10);
    $$('#domBucketRow .dom-dot').forEach(i => {
        const raw = (i.dataset.args || '').replace(/[\["\]]/g, '');
        i.classList.toggle('active', raw === String(STATE.filter.domBucket));
    });
    applyFilterAndSort();
};
window.toggleRecursiveList = () => {
    STATE.recursive = !STATE.recursive;
    try { localStorage.setItem('fe_recursive', STATE.recursive ? '1' : '0'); } catch (e) {}
    const btn = $('#btnRecursive'); if (btn) { btn.classList.toggle('btn-primary', STATE.recursive); btn.classList.toggle('btn-outline-secondary', !STATE.recursive); }
    const cur = STATE.currentPath || '';
    if (cur.startsWith('smart://') || cur.startsWith('trash://') || cur.startsWith('search://') || cur.startsWith('collection://') || cur.startsWith('similar://'))
        return window.showToast('虚拟视图不适用子文件夹聚合', 'info');
    loadPath(cur, true);
};

// ============================================================================
// [V3/GAP-12] 导入素材 + [GAP-13] 库统计 + [GAP-14] 缓存管理
// ============================================================================

window.openImportModal = () => {
    const inp = $('#importDest');
    if (inp && !inp.value && !/^(smart|search|trash|collection|similar):\/\//.test(STATE.currentPath || '')) inp.value = STATE.currentPath;
    const el = $('#importModal'); if (el) new bootstrap.Modal(el).show();
};
window.runImport = async () => {
    const source = $('#importSource')?.value.trim(), dest = $('#importDest')?.value.trim();
    if (!source || !dest) return window.showToast('请填写源目录与目标目录', 'warning');
    const btn = $('#importModal .btn-primary'), old = btn.innerHTML;
    btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 导入中...';
    $('#importResult').textContent = '导入中，请稍候...';
    try {
        const data = await feFetch('/api/import/run', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
            source, dest,
            subdir: $('#importSubdir')?.value.trim() || '',
            name_template: $('#importTemplate')?.value.trim() || '',
            date_folders: $('#importDateFolders')?.checked ?? true,
            skip_dup: $('#importSkipDup')?.checked ?? true,
            delete_source: $('#importDelSource')?.checked ?? false,
        }) }, { timeout: 600000, context: '导入素材' });
        $('#importResult').textContent = `完成：导入 ${data.imported}，跳过重复 ${data.skipped}，失败 ${data.failed}`;
        window.showToast(`导入完成：新增 ${data.imported} 项`, data.failed ? 'warning' : 'success');
        if (data.errors?.length) console.warn('导入错误:', data.errors);
        window.startLibraryScan?.();
    } catch (e) { $('#importResult').textContent = '导入失败：' + (e.message || '网络错误'); }
    finally { btn.disabled = false; btn.innerHTML = old; }
};
window.openStatsModal = () => {
    const el = $('#statsModal'); if (!el) return;
    new bootstrap.Modal(el).show();
    loadStats();
};
window.loadStats = async () => {
    const box = $('#statsContent'); if (!box) return;
    try {
        const s = await feFetch('/api/stats', {}, { context: '库统计' });
        const fmtB = (b) => b > 1073741824 ? (b / 1073741824).toFixed(2) + ' GB' : b > 1048546 ? (b / 1048576).toFixed(1) + ' MB' : (b / 1024).toFixed(0) + ' KB';
        const fmtD = (s2) => { s2 = Math.round(s2 || 0); const h = Math.floor(s2 / 3600); return h ? `${h} 小时 ${Math.round((s2 % 3600) / 60)} 分` : `${Math.round(s2 / 60)} 分钟`; };
        const maxBytes = Math.max(1, ...(s.by_ext || []).map(x => x.bytes));
        const star = (n) => '★'.repeat(n);
        box.innerHTML = `
            <div class="row g-2 mb-3">
                <div class="col-md-3"><div class="border border-secondary rounded p-2 text-center"><div class="fw-bold fs-5">${s.total_files}</div><div class="text-white-50 small">已编目文件</div></div></div>
                <div class="col-md-3"><div class="border border-secondary rounded p-2 text-center"><div class="fw-bold fs-5">${fmtB(s.total_bytes)}</div><div class="text-white-50 small">总体积</div></div></div>
                <div class="col-md-3"><div class="border border-secondary rounded p-2 text-center"><div class="fw-bold fs-5">${fmtD(s.total_duration)}</div><div class="text-white-50 small">视频/音频时长</div></div></div>
                <div class="col-md-3"><div class="border border-secondary rounded p-2 text-center"><div class="fw-bold fs-5 ${s.missing ? 'text-warning' : ''}">${s.missing}</div><div class="text-white-50 small">失联（可重连）</div></div></div>
            </div>
            <h6 class="small text-white-50">类型分布（按体积）</h6>
            ${(s.by_ext || []).map(x => `<div class="d-flex align-items-center gap-2 mb-1"><span class="text-truncate" style="width:70px;font-size:11px;">${escapeHtml(x.ext)}</span><div class="progress flex-grow-1" style="height:8px;"><div class="progress-bar" style="width:${Math.max(2, x.bytes / maxBytes * 100)}%"></div></div><span class="small text-white-50" style="width:110px;text-align:right;">${x.count} 项 · ${fmtB(x.bytes)}</span></div>`).join('') || '<div class="text-white-50 small">暂无数据 — 先运行一次索引</div>'}
            <div class="d-flex gap-4 mt-3">
                <div><h6 class="small text-white-50">评分分布</h6>${[1,2,3,4,5].map(i => `<div class="small">${star(i)} <span class="text-white-50">${s.ratings?.[String(i)] || 0}</span></div>`).join('')}</div>
                <div><h6 class="small text-white-50">标签 TOP</h6>${(s.top_tags || []).slice(0, 5).map(t => `<div class="small text-truncate">${escapeHtml(t.name)} <span class="text-white-50">${t.count}</span></div>`).join('') || '<div class="small text-white-50">暂无标签</div>'}</div>
                <div><h6 class="small text-white-50">近 7 天新增</h6><div class="fs-5">${s.recent_7d}</div></div>
            </div>`;
    } catch (e) { if (box) box.innerHTML = '<div class="text-danger text-center py-4">统计加载失败</div>'; }
};
window.openCacheModal = () => { const el = $('#cacheModal'); if (el) { new bootstrap.Modal(el).show(); refreshCacheSize(); } };
window.refreshCacheSize = async () => {
    const box = $('#cacheSizeInfo'); if (!box) return;
    try {
        const d = await feFetch('/api/cache/size', {}, { context: '缓存统计' });
        const fmtB = (b) => b > 1073741824 ? (b / 1073741824).toFixed(2) + ' GB' : b > 1048546 ? (b / 1048576).toFixed(1) + ' MB' : (b / 1024).toFixed(0) + ' KB';
        box.innerHTML = `全局缓存：${fmtB(d.global?.bytes || 0)}（${escapeHtml(d.global?.path || '')}）<br>本地 .cache 目录：${(d.local || []).length} 个，共 ${fmtB((d.local || []).reduce((a, x) => a + x.bytes, 0))}<br><b>合计 ${fmtB(d.total_bytes || 0)}</b>`;
    } catch (e) { if (box) box.textContent = '统计失败'; }
};
window.runCacheCleanup = async (scope, days) => {
    if (parseInt(days, 10) === 0 && !(await window.showConfirmDialog('清空全部缩略图缓存？浏览时会自动重新生成（首次较慢）。', '清空缓存'))) return;
    try {
        const d = await feFetch('/api/cache/cleanup', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ scope, older_days: parseInt(days, 10) || 0 }) }, { timeout: 300000, context: '缓存清理' });
        window.showToast(`已清理 ${d.cleaned_files} 个缩略图文件`, 'success');
        refreshCacheSize();
    } catch (e) {}
};

// ============================================================================
// [V4/GAP-16] 地图视图 + [GAP-17] OCR + [GAP-18] 导出 + [GAP-22] 软锁
// ============================================================================

// ---- [GAP-16] 地图（Leaflet CDN，离线优雅降级） ----
window._leafletState = { loading: false, map: null, ready: false };
window.openMapModal = async () => {
    const el = $('#mapModal'); if (!el) return;
    new bootstrap.Modal(el).show();
    if (!window._ensureLeaflet()) return;
    await window._waitLeaflet();
    renderMapView();
};
window._ensureLeaflet = () => {
    if (window.L || window._leafletState.loading) return true;
    window._leafletState.loading = true;
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css';
    document.head.appendChild(link);
    const script = document.createElement('script');
    script.src = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js';
    script.onload = () => { window._leafletState.ready = true; };
    script.onerror = () => { window._leafletState.loading = false; window.showToast('地图组件加载失败（需要网络）', 'error'); };
    document.head.appendChild(script);
    return true;
};
window._waitLeaflet = () => new Promise(resolve => {
    const t0 = Date.now();
    const timer = setInterval(() => {
        if (window.L || Date.now() - t0 > 8000) { clearInterval(timer); resolve(); }
    }, 200);
});
window.renderMapView = async () => {
    const list = $('#mapList'); if (!list) return;
    list.innerHTML = '加载有 GPS 的素材...';
    // [V5 复查] 渲染令牌：快速重开弹窗时旧响应不覆盖新视图
    const token = (window._mapSeq = (window._mapSeq || 0) + 1);
    try {
        const data = await feFetch('/api/map/files', {}, { context: '地图' });
        if (token !== window._mapSeq) return;
        const files = data.files || [];
        if (!window.L) { list.textContent = '地图组件不可用（离线）。'; return; }
        if (!files.length) { list.textContent = '没有带 GPS 信息的素材（拍摄时开启定位，索引后即可显示）。'; return; }
        if (!window._leafletState.map) {
            window._leafletState.map = L.map('mapDiv');
            L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 18, attribution: '© OpenStreetMap' }).addTo(window._leafletState.map);
            // [V5 复查] 标记统一收进 layerGroup：每次渲染前 clearLayers，
            // 反复开关弹窗不再层层叠加旧 marker
            window._leafletState.markers = L.layerGroup().addTo(window._leafletState.map);
        }
        const map = window._leafletState.map;
        const markers = window._leafletState.markers;
        markers.clearLayers();
        setTimeout(() => map.invalidateSize(), 250);
        const bounds = [];
        list.innerHTML = files.slice(0, 100).map(f => `<div class="d-flex gap-2 align-items-center py-1 border-bottom border-secondary" data-map-path="${escapeHtml(f.path)}" style="cursor:pointer;"><i class="bi bi-geo-alt-fill text-danger"></i><span class="text-truncate flex-grow-1">${escapeHtml(f.name)}</span><span style="font-size:10px;">${f.lat.toFixed(4)}, ${f.lon.toFixed(4)}</span></div>`).join('')
            + (files.length > 100 ? `<div class="text-white-50 small pt-1">…其余 ${files.length - 100} 项在地图上</div>` : '');
        files.forEach((f, i) => {
            const m = L.circleMarker([f.lat, f.lon], { radius: 6, color: '#e53935', fillOpacity: 0.85 }).addTo(markers)
                .bindTooltip(escapeHtml(f.name));
            m.on('click', () => {
                const row = list.querySelector(`[data-map-path="${cssPathSel(f.path)}"]`);
                if (row) { list.querySelectorAll('.bg-primary.bg-opacity-25').forEach(x => x.classList.remove('bg-primary', 'bg-opacity-25')); row.classList.add('bg-primary', 'bg-opacity-25'); row.scrollIntoView({ block: 'nearest' }); }
            });
            bounds.push([f.lat, f.lon]);
        });
        if (bounds.length) map.fitBounds(bounds, { padding: [30, 30], maxZoom: 14 });
    } catch (e) { if (token === window._mapSeq) list.textContent = '地图数据加载失败'; }
};

// ---- [GAP-17] OCR ----
window.toggleOcrEnabled = async () => {
    const cur = window._ocrEnabled;
    const next = !cur;
    try {
        await fetch('/api/config', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ ocr_enabled: next }) });
        window._ocrEnabled = next;
        updateOcrLabel();
        window.showToast(next ? '已启用 OCR（索引时自动识别图片文字，全库搜索可命中）' : '已关闭 OCR', 'success');
    } catch (e) { window.showToast('设置保存失败', 'error'); }
};
function updateOcrLabel() {
    const el = document.getElementById('ocrToggleLabel');
    if (el) el.textContent = window._ocrEnabled ? '关闭图内文字识别（OCR）' : '启用图内文字识别（OCR）';
}
window.runOcrOnFile = async (path) => {
    try {
        const d = await feFetch('/api/ocr/run', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ path }) }, { timeout: 60000, context: 'OCR' });
        return d.text || '';
    } catch (e) { return ''; }
};

// ---- [GAP-18] 导出 ----
window.openExportModal = async () => {
    const paths = Array.from(STATE.selected).filter(p => /\.(jpe?g|png|webp|bmp|tiff?)$/i.test(p));
    if (!paths.length) return window.showToast('请选择要导出的图片', 'warning');
    const cnt = $('#exportCount'); if (cnt) cnt.textContent = paths.length;
    window._exportPaths = paths;
    window._exportCtx = STATE.currentPath; // [V5 复查] 记录快照来源视图，运行时按需刷新
    const inp = $('#exportDest'); if (inp && !inp.value) inp.value = (STATE.currentPath && !/^(smart|search|trash|collection|similar):\/\//.test(STATE.currentPath)) ? STATE.currentPath + '/导出' : '';
    const el = $('#exportModal'); if (el) new bootstrap.Modal(el).show();
    try {
        const presets = await feFetch('/api/export/presets', {}, { context: '预设', silent: true });
        window._exportPresets = presets || [];
        const sel = $('#exportPresetSel');
        if (sel) sel.innerHTML = '<option value="">不使用预设</option>' + window._exportPresets.map(p => `<option value="${p.id}">${escapeHtml(p.name)}</option>`).join('');
    } catch (e) {}
};
window.applyExportPreset = (sel) => {
    const p = (window._exportPresets || []).find(x => String(x.id) === sel?.value);
    if (!p) return;
    const cfg = p.config || {};
    $('#exportFormat').value = cfg.format || 'jpeg';
    $('#exportQuality').value = cfg.quality || 90;
    $('#exportMaxW').value = cfg.max_w || '';
    $('#exportMaxH').value = cfg.max_h || '';
    $('#exportWatermarkText').value = (cfg.watermark && cfg.watermark.text) || '';
    $('#exportWmPos').value = (cfg.watermark && cfg.watermark.position) || 'br';
};
window.saveExportPreset = async () => {
    const name = await window.showInputDialog('预设名称', 'Web 1280px 水印');
    if (!name) return;
    const cfg = {
        format: $('#exportFormat').value, quality: parseInt($('#exportQuality').value) || 90,
        max_w: parseInt($('#exportMaxW').value) || 0, max_h: parseInt($('#exportMaxH').value) || 0,
        watermark: $('#exportWatermarkText').value ? { text: $('#exportWatermarkText').value, position: $('#exportWmPos').value, opacity: 0.5 } : null,
    };
    try {
        await feFetch('/api/export/presets', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ name, config: cfg }) }, { context: '保存预设' });
        window.showToast('预设已保存', 'success');
        window.openExportModal.refresh = true;
        const presets = await feFetch('/api/export/presets', {}, { context: '预设', silent: true });
        window._exportPresets = presets || [];
        const sel = $('#exportPresetSel');
        if (sel) sel.innerHTML = '<option value="">不使用预设</option>' + window._exportPresets.map(p => `<option value="${p.id}">${escapeHtml(p.name)}</option>`).join('');
    } catch (e) {}
};
window.runExport = async () => {
    const dest = $('#exportDest').value.trim();
    if (!dest) return window.showToast('请填写目标目录', 'warning');
    // [V5 复查] 弹窗打开后选区可能已变化——仍在原目录且选区非空时用实时选区，
    // 避免导出"打开弹窗那一刻"的旧快照
    if (window._exportCtx === STATE.currentPath && STATE.selected.size) {
        window._exportPaths = Array.from(STATE.selected).filter(p => /\.(jpe?g|png|webp|bmp|tiff?)$/i.test(p));
    }
    const wmText = $('#exportWatermarkText').value.trim();
    const body = {
        paths: window._exportPaths || [], dest,
        format: $('#exportFormat').value, quality: parseInt($('#exportQuality').value) || 90,
        max_w: parseInt($('#exportMaxW').value) || 0, max_h: parseInt($('#exportMaxH').value) || 0,
        watermark: wmText ? { text: wmText, position: $('#exportWmPos').value, opacity: 0.5 } : null,
    };
    const btn = $('#exportModal .btn-primary'), old = btn.innerHTML;
    btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 导出中...';
    try {
        const d = await feFetch('/api/export/run', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) }, { timeout: 600000, context: '导出' });
        $('#exportResult').textContent = `已导出 ${(d.exported || []).length} 张${d.errors?.length ? `，失败 ${d.errors.length}` : ''}`;
        window.showToast(`导出完成：${(d.exported || []).length} 张`, 'success');
    } catch (e) { $('#exportResult').textContent = '导出失败'; }
    finally { btn.disabled = false; btn.innerHTML = old; }
};

// ---- [GAP-22] 文件夹软锁 ----
window.lockFolderFromContext = async () => {
    const menu = $('#contextMenu'); if (menu) menu.style.display = 'none';
    const target = STATE.contextTarget;
    const path = target?.type === 'tree' ? target.path : null;
    if (!path) return;
    const pwd = await window.showInputDialog(`为「${path.split('/').pop()}」设置保护密码（UI 级软锁，非加密）`);
    if (!pwd) return;
    try {
        await feFetch('/api/locks', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ path, password: pwd }) }, { context: '设置保护' });
        window.showToast('已上锁（解锁：进入该目录时输入密码）', 'success');
    } catch (e) {}
};
window.promptUnlock = async (lockPath, hint) => {
    for (let i = 0; i < 3; i++) {
        const pwd = await window.showInputDialog(`此目录已受密码保护${hint ? `（提示：${hint}）` : ''}`, '');
        if (pwd === null) return false;  // 取消
        try {
            const d = await feFetch('/api/locks/unlock', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ path: lockPath, password: pwd }) }, { context: '解锁', silent: true });
            if (d.success) { window.showToast('已解锁', 'success'); return true; }
        } catch (e) {}
        window.showToast('密码错误，请重试', 'error');
    }
    return false;
};

// ============================================================================
// [U6/GAP-05] 会话内撤销栈（Ctrl+Z）：重命名 / 删除（回收站恢复）
// ============================================================================

window._undoStack = [];
window.pushUndo = (entry) => {
    window._undoStack.push(entry);
    if (window._undoStack.length > 20) window._undoStack.shift();
};
window.undoLast = async () => {
    const entry = window._undoStack.pop();
    if (!entry) return window.showToast("没有可撤销的操作", 'info');
    if (entry.type === 'rename') {
        const oldName = entry.from.substring(entry.from.lastIndexOf('/') + 1);
        const ok = await window.doRename(entry.to, oldName);
        // doRename 已把还原记录入撤销栈——撤销是"取消上次操作"，不应可被再次撤销
        if (ok) { window._undoStack.pop(); window.showToast(`已撤销重命名：${oldName}`, 'success'); }
        else window._undoStack.push(entry);  // 失败把记录放回去
    } else if (entry.type === 'delete') {
        try {
            const data = await feFetch('/api/trash/restore', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ ids: entry.trashIds }) }, { context: '撤销删除' });
            if (data.restored?.length) {
                window.showToast(`已撤销删除：恢复 ${data.restored.length} 项`, 'success');
                loadTags();
                const cur = STATE.currentPath.replace(/\\/g, '/');
                if (cur.startsWith('trash://')) openTrashView();
                else if (entry.dir && (cur === entry.dir.replace(/\\/g, '/') || cur.startsWith(entry.dir.replace(/\\/g, '/') + '/'))) reloadFiles();
                if (data.errors?.length) window.showToast(data.errors[0], 'warning');
                // 部分成功也出栈：把已恢复的 id 留在栈里会反复重试报错
            } else if (data.errors?.length) {
                // [U9] 死条目（记录已不存在/文件已不在回收站）不再回栈——
                // 此前原样压回，此后每次 Ctrl+Z 都先撞上它，永远轮不到
                // 更早的撤销记录
                const permanent = data.errors.every(msg => /记录不存在|已不在回收站/.test(msg));
                if (permanent) window.showToast('无法撤销：' + data.errors[0], 'warning');
                else window._undoStack.push(entry);
            }
        } catch (e) { window._undoStack.push(entry); }  // 网络/服务异常可重试
    }
};

// ============================================================================
// [U6/GAP-05] 回收站视图（trash:// 虚拟视图）
// ============================================================================

window.openTrashView = async () => {
    $$('.smart-folder-item, .tree-header').forEach(i => i.classList.remove('active'));
    STATE.currentPath = 'trash://';
    STATE.pendingSelection = null; // [V5 复查] 虚拟视图入口统一清滞留选中
    // [U9] 竞态令牌与 loadPath/openSmartFolder/runLibrarySearch 共用 _loadSeq：
    // 点目录后立刻点回收站时，目录的旧响应后到会把回收站视图覆盖掉
    const seq = (window._loadSeq = (window._loadSeq || 0) + 1);
    const pInp = $('#pathDisplay'); if (pInp) pInp.value = '回收站（保留 30 天）';
    window.renderBreadcrumb?.(STATE.currentPath);
    STATE.selected.clear(); STATE.visuallySelected.clear(); updateToolbar(); togglePropertiesPanel(false);
    { const sb = $('#saveSearchBtn'); if (sb) sb.style.display = 'none'; }
    if ($('#searchInput')) { $('#searchInput').value = ''; $('#searchClear').style.display = 'none'; }
    { const cnt = $('#searchCount'); if (cnt) cnt.style.display = 'none'; }
    STATE.allFiles = []; STATE.visibleFiles = []; STATE.renderIdx = 0;
    STATE.thumbLoading.total = 0; STATE.thumbLoading.loaded = 0; updateThumbProgress();
    const container = $('#fileContainer'), empty = $('#emptyState');
    empty?.classList.add('d-none');
    container.innerHTML = '<div class="text-center text-muted py-4"><div class="spinner-border text-primary" role="status"></div></div>';
    try {
        const data = await feFetch('/api/trash', {}, { context: '回收站' });
        if (seq !== window._loadSeq) return;
        renderTrashList(data.items || []);
    } catch (e) {
        if (seq !== window._loadSeq) return;
        container.innerHTML = '<div class="text-muted text-center py-5">⚠️ 回收站加载失败</div>';
    }
};
window.renderTrashList = (items) => {
    const container = $('#fileContainer'), empty = $('#emptyState');
    if (!items.length) {
        container.innerHTML = '';
        if (empty) { empty.innerText = '回收站是空的 — 删除的文件会先在这里保留 30 天，可随时恢复（Ctrl+Z 立即撤销删除）'; empty.classList.remove('d-none'); }
        return;
    }
    empty?.classList.add('d-none');
    const fmt = (ts) => { const d = new Date(ts * 1000); const p2 = n => String(n).padStart(2, '0'); return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${p2(d.getHours())}:${p2(d.getMinutes())}`; };
    container.innerHTML = `<div class="trash-toolbar"><span class="text-white-50 small">共 ${items.length} 项 · 到期自动物理清理 · 文件夹/文件名被占用时恢复为"(恢复)"副本</span><button class="btn btn-sm btn-outline-danger" data-action="emptyTrash" data-stop><i class="bi bi-trash3"></i> 清空回收站</button></div>`
        + items.map(it => `<div class="trash-item">
            <i class="bi ${it.is_dir ? 'bi-folder-fill text-warning' : 'bi-file-earmark'} me-2"></i>
            <div class="flex-grow-1 min-width-0">
                <div class="text-truncate">${escapeHtml(it.name)}${it.meta_rows ? ` <span class="badge bg-primary bg-opacity-25 border border-primary text-white-50" style="font-size:10px;vertical-align:middle;">元数据 ${escapeHtml(String(it.meta_rows))} 条</span>` : ''}</div>
                <div class="text-truncate text-white-50" style="font-size:11px;">原位置: ${escapeHtml(it.original_path)}</div>
            </div>
            <span class="text-white-50 me-2" style="font-size:11px;white-space:nowrap;">${fmt(it.deleted_at)}</span>
            <button class="btn btn-sm btn-outline-success me-1" data-action="restoreTrash" data-args='[${it.id}]' title="恢复到原位置"><i class="bi bi-arrow-counterclockwise"></i> 恢复</button>
            <button class="btn btn-sm btn-outline-danger" data-action="purgeTrash" data-args='[${it.id}]' title="彻底删除（不可恢复）"><i class="bi bi-x-lg"></i></button>
        </div>`).join('');
};
window.restoreTrash = async (id) => {
    try {
        const data = await feFetch('/api/trash/restore', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ ids: [id] }) }, { context: '恢复' });
        if (data.restored?.length) { window.showToast(`已恢复「${data.restored[0].name}」到原位置`, 'success'); loadTags(); openTrashView(); }
        if (data.errors?.length) window.showToast(data.errors[0], 'warning');
    } catch (e) {}
};
window.purgeTrash = async (id) => {
    if (!(await window.showConfirmDialog('彻底删除后无法恢复，确定吗？', '彻底删除'))) return;
    try {
        const data = await feFetch('/api/trash/purge', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ ids: [id] }) }, { context: '彻底删除' });
        if (data.success) { window.showToast('已彻底删除', 'success'); openTrashView(); }
        // [U9] 有失败项时此前完全静默
        else if (data.errors?.length) window.showToast(data.errors[0], 'warning');
        // [U9→U10 修正] 仅在删除确实成功时才从撤销栈剔除——purge 部分失败时
        // 该 id 仍在回收站可恢复，此前无条件剔除会静默剥夺 Ctrl+Z 能力
        if (data.success) window._undoStack = window._undoStack.filter(e => !(e.type === 'delete' && e.trashIds?.includes(id)));
    } catch (e) {}
};
window.emptyTrash = async () => {
    if (!(await window.showConfirmDialog('清空回收站？所有项目将被彻底删除，无法恢复。', '清空回收站'))) return;
    try {
        const data = await feFetch('/api/trash/purge', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ all: true }) }, { context: '清空回收站' });
        if (data.success) { window.showToast('回收站已清空', 'success'); openTrashView(); }
        else if (data.errors?.length) window.showToast(data.errors[0], 'warning');
        // [U9→U10 修正] 清空失败（success=false）时删除类撤销记录仍有效，
        // 此前无条件全删；完全成功才整体失效
        if (data.success) window._undoStack = window._undoStack.filter(e => e.type !== 'delete');
    } catch (e) {}
};

// ============================================================================
// [U6/GAP-06] 元数据备份（导出/导入）与失联元数据找回（re-link）
// ============================================================================

window.exportMetaBackup = () => {
    window.showToast('正在生成元数据备份...', 'info');
    // 同站导航自动携带 HttpOnly token cookie，触发下载
    window.location.href = '/api/meta/export';
};
window.importMetaBackup = () => $('#metaImportInput')?.click();
window.handleMetaImportFile = async (input) => {
    const file = input.files?.[0]; if (!file) return;
    input.value = '';
    let rows;
    try { rows = JSON.parse(await file.text()).rows; } catch (e) { return window.showToast('备份文件解析失败', 'error'); }
    if (!Array.isArray(rows) || !rows.length) return window.showToast('备份文件为空或格式不对', 'error');
    if (!(await window.showConfirmDialog(`导入 ${rows.length} 条元数据？合并模式：只回填空值，不覆盖现有数据。`, '导入元数据'))) return;
    try {
        const res = await feFetch('/api/meta/import', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ rows, mode: 'merge' }) }, { context: '导入元数据', timeout: 60000 });
        if (res.success) {
            window.showToast(`已导入 ${res.applied} 条${res.skipped ? `（跳过 ${res.skipped} 条失联路径，可用"失联元数据找回"扫描）` : ''}`, 'success');
            loadTags();
        }
    } catch (e) {}
};
window.openRelinkScan = async () => {
    const modalEl = $('#relinkModal'); if (!modalEl) return;
    const list = $('#relinkList'), cnt = $('#relinkCount');
    const modal = new bootstrap.Modal(modalEl);
    list.innerHTML = '<div class="text-center text-muted py-4"><div class="spinner-border text-primary" role="status"></div><div class="mt-2 small">正在扫描失联元数据（大库可能需要一点时间）...</div></div>';
    if (cnt) cnt.textContent = '';
    modal.show();
    try {
        const data = await feFetch('/api/meta/scan_orphans', {}, { timeout: 120000, context: 're-link 扫描' });
        if (!data.orphans) { list.innerHTML = '<div class="text-muted text-center py-4">没有失联的元数据 🎉</div>'; return; }
        if (cnt) cnt.textContent = `失联 ${data.orphans} 条，可自动找回 ${data.matches.length} 条`;
        if (!data.matches.length) {
            list.innerHTML = `<div class="text-muted small p-3">失联 ${data.orphans} 条，但素材库内没有找到同名的唯一候选文件（可能已改名多次、重名歧义或移出库外）。</div>`;
            return;
        }
        window._relinkMatches = data.matches;
        list.innerHTML = `<div class="relink-actions mb-2"><a href="#" data-action="toggleRelinkChecks" data-args='[true]' data-prevent>全选</a> · <a href="#" data-action="toggleRelinkChecks" data-args='[false]' data-prevent>全不选</a></div>`
            + data.matches.map((m, i) => `<label class="relink-row"><input type="checkbox" class="relink-check form-check-input" checked data-idx="${i}">
                <div class="flex-grow-1 min-width-0">
                    <div class="text-truncate small">${escapeHtml(m.name)}</div>
                    <div class="text-truncate text-white-50" style="font-size:11px;">${escapeHtml(m.path)} → <span class="text-success">${escapeHtml(m.new_path)}</span></div>
                </div></label>`).join('');
    } catch (e) { list.innerHTML = '<div class="text-danger text-center py-4">扫描失败</div>'; }
};
window.toggleRelinkChecks = (on) => { $$('#relinkList .relink-check').forEach(cb => cb.checked = on); return false; };
window.applyRelink = async () => {
    const matches = window._relinkMatches || [];
    const sel = Array.from($$('#relinkList .relink-check:checked'));
    if (!sel.length) return window.showToast('请先勾选要找回的条目', 'warning');
    const mappings = sel.map(cb => { const m = matches[parseInt(cb.dataset.idx, 10)]; return { path: m.path, new_path: m.new_path }; });
    try {
        const data = await feFetch('/api/meta/relink', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ mappings }) }, { context: '应用 re-link' });
        if (data.applied) window.showToast(`已找回 ${data.applied} 条元数据`, 'success');
        if (data.errors?.length) window.showToast(`${data.errors.length} 条失败：${data.errors[0]}`, 'warning');
        const modalEl = $('#relinkModal'); if (modalEl) bootstrap.Modal.getInstance(modalEl)?.hide();
    } catch (e) {}
};

// [U5/GAP-03] 单文件颜色/旗标（卡片角标即时刷新）
window.setFileColor = async (path, color) => {
    const target = path.replace(/\\/g, '/').toLowerCase();
    const upd = f => { if (f.path.replace(/\\/g, '/').toLowerCase() === target) f.color = color; };
    STATE.allFiles.forEach(upd); STATE.visibleFiles.forEach(upd);
    window.__FE.cardBadgesHtml && window._refreshBadges(path);
    try { await feFetch('/api/update_meta', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ path, color }) }, { context: '颜色标记' }); } catch (e) {}
};
window.setFileFlag = async (path, flag) => {
    const target = path.replace(/\\/g, '/').toLowerCase();
    const upd = f => { if (f.path.replace(/\\/g, '/').toLowerCase() === target) f.flag = flag; };
    STATE.allFiles.forEach(upd); STATE.visibleFiles.forEach(upd);
    window.__FE.cardBadgesHtml && window._refreshBadges(path);
    try { await feFetch('/api/update_meta', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ path, flag }) }, { context: '旗标' }); } catch (e) {}
};
window._refreshBadges = (path) => {
    const target = path.replace(/\\/g, '/').toLowerCase();
    const vis = STATE.visibleFiles.find(f => f.path.replace(/\\/g, '/').toLowerCase() === target);
    if (!vis) return;
    const idx = STATE.visibleFiles.indexOf(vis);
    let host = $(`#card-${idx} .file-thumb-wrapper`) || $(`#card-${idx} .audio-card-wrapper`) || $(`#card-${idx} .non-img-wrapper`);
    if (host && host.classList.contains('non-img-wrapper')) host.style.position = 'relative';
    if (host) { const old = host.querySelector('.card-badges'); if (old) old.remove(); host.insertAdjacentHTML('beforeend', window.__FE.cardBadgesHtml(vis)); }
};

// [ARCH-13] 晚期绑定回填：render 模块内 navigateFile/onTreeClick 经此取用
Object.assign(window.__FE, { loadPath, openItem, isVirtualView });
})();

// ============================================================================
// [U7/GAP-13] 面包屑交互 + [U7/GAP-12] 幻灯片 + [U7/GAP-08] 对比视图
// + [U7/GAP-09] 重复查找 + [U7/GAP-14] 批量旋转/移动
// ============================================================================
(() => {
const { $, $$, STATE, escapeHtml, loadPath, isVirtualView, openLightbox } = window.__FE;

// ---- 面包屑 ----
window.copyPath = () => {
    const p = STATE.currentPath || '';
    const done = () => window.showToast('路径已复制', 'success');
    if (navigator.clipboard?.writeText) { navigator.clipboard.writeText(p).then(done).catch(() => {}); return; }
    const ta = document.createElement('textarea');
    ta.value = p; document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); done(); } catch (e) { window.showToast('复制失败', 'error'); }
    ta.remove();
};
window.togglePathEdit = () => {
    const inp = $('#pathDisplay'), bar = $('#breadcrumbBar');
    if (!inp || !bar) return;
    bar.style.display = 'none';
    inp.style.display = ''; inp.focus(); inp.select();
};
window._hideCrumbMenu = () => { const m = $('#crumbMenu'); if (m) m.remove(); };
document.addEventListener('click', (e) => {
    const seg = e.target.closest('.crumb-seg[data-crumb-path]');
    if (seg) { window._hideCrumbMenu(); loadPath(seg.dataset.crumbPath); return; }
    const arrow = e.target.closest('.crumb-arrow[data-crumb-parent]');
    if (arrow) {
        e.stopPropagation();
        const parent = arrow.dataset.crumbParent;
        const exist = $('#crumbMenu');
        if (exist) { exist.remove(); if (exist.dataset.parent === parent) return; }
        const menu = document.createElement('div');
        menu.id = 'crumbMenu'; menu.className = 'crumb-menu';
        menu.dataset.parent = parent;
        menu.innerHTML = '<div class="text-muted small px-2 py-1">加载中...</div>';
        document.body.appendChild(menu);
        const rect = arrow.getBoundingClientRect();
        menu.style.left = rect.left + 'px';
        menu.style.top = rect.bottom + 2 + 'px';
        fetch(`/api/tree?path=${encodeURIComponent(parent)}`).then(r => r.json()).then(folders => {
            menu.innerHTML = folders.length
                ? folders.map(f => `<div class="crumb-menu-item" data-crumb-path="${escapeHtml(f.path)}"><i class="bi bi-folder me-2 text-warning"></i>${escapeHtml(f.name)}</div>`).join('')
                : '<div class="text-muted small px-2 py-1">无同级目录</div>';
        }).catch(() => { menu.innerHTML = '<div class="text-danger small px-2 py-1">加载失败</div>'; });
        return;
    }
    if (!e.target.closest('#crumbMenu')) window._hideCrumbMenu();
});
document.addEventListener('click', (e) => {
    const item = e.target.closest('.crumb-menu-item[data-crumb-path]');
    if (item) { window._hideCrumbMenu(); loadPath(item.dataset.crumbPath); }
});
window.addEventListener('scroll', () => window._hideCrumbMenu(), true);
window.addEventListener('resize', () => window._hideCrumbMenu());

// ---- 幻灯片放映（灯箱）----
window.toggleSlideshow = () => {
    STATE.slideshow = STATE.slideshow || { on: false, timer: null, interval: 2000 };
    STATE.slideshow.on = !STATE.slideshow.on;
    const btn = $('#slideshowBtn');
    if (btn) btn.innerHTML = STATE.slideshow.on ? '<i class="bi bi-pause-fill"></i>' : '<i class="bi bi-play-fill"></i>';
    // [V4/GAP-20] 背景音乐：取当前视图第一个音频，循环播放（随放映启停）
    if (STATE.slideshow.on) {
        const audio = STATE.visibleFiles.find(f => f.type === 'audio');
        if (audio) {
            try {
                STATE.slideshow.bgm = new Audio(`/api/file?path=${encodeURIComponent(audio.path)}`);
                STATE.slideshow.bgm.loop = true;
                STATE.slideshow.bgm.volume = 0.6;
                STATE.slideshow.bgm.play().catch(() => {});
                window.showToast(`放映开始（BGM：${audio.name}）`, 'info');
            } catch (e) { window.showToast(`放映开始（每 ${STATE.slideshow.interval / 1000}s）`, 'info'); }
        } else window.showToast(`放映开始（每 ${STATE.slideshow.interval / 1000}s）`, 'info');
        restartSlideshowTimer();
    } else { stopSlideshowTimer(); if (STATE.slideshow.bgm) { try { STATE.slideshow.bgm.pause(); } catch (e) {} } }
};
window.runOcrPanel = async (path) => {
    // [V5 复查] 委托层把 data-path 字符串作为首参传入——此前签名收 el 读
    // dataset.path，path 恒为 undefined，点击识别按钮永远无反应
    const box = $('#prop-ocr');
    if (!path || !box) return;
    box.innerHTML = '<div class="text-muted small">识别中...</div>';
    const text = await window.runOcrOnFile(path);
    box.innerHTML = text
        ? `<div class="small bg-black bg-opacity-25 border border-secondary rounded p-2" style="white-space:pre-wrap;max-height:180px;overflow:auto;">${escapeHtml(text)}</div>`
        : '<div class="text-muted small">未识别到文字（或 OCR 引擎不可用，可在设置中开启自动识别）</div>';
};
window.setSlideshowInterval = (sel) => {
    STATE.slideshow = STATE.slideshow || { on: false, timer: null, interval: 2000 };
    STATE.slideshow.interval = parseInt(sel.value, 10) || 2000;
    if (STATE.slideshow.on) restartSlideshowTimer();
};
function stopSlideshowTimer() { if (STATE.slideshow?.timer) { clearTimeout(STATE.slideshow.timer); STATE.slideshow.timer = null; } }
window.restartSlideshowTimer = restartSlideshowTimer;
window.stopSlideshow = () => { stopSlideshowTimer(); STATE.slideshow && (STATE.slideshow.on = false); if (STATE.slideshow?.bgm) { try { STATE.slideshow.bgm.pause(); } catch (e) {} } const btn = $('#slideshowBtn'); if (btn) btn.innerHTML = '<i class="bi bi-play-fill"></i>'; };
function restartSlideshowTimer() {
    stopSlideshowTimer();
    if (!STATE.slideshow?.on) return;
    STATE.slideshow.timer = setTimeout(() => {
        if (!STATE.lightbox.active || !STATE.slideshow.on) return;
        // 仅在图片间轮播（视频交给播放器），找不到下一张图片则从头再来
        const n = STATE.visibleFiles.length;
        for (let step = 1; step <= n; step++) {
            const idx = (STATE.currentIndex + step) % n;
            const f = STATE.visibleFiles[idx];
            if (f && (f.type === 'image' || ['jpg','jpeg','png','gif','webp','bmp','svg'].includes((f.name.split('.').pop() || '').toLowerCase()))) {
                openLightbox(idx); return;
            }
        }
    }, STATE.slideshow.interval);
}

// ---- 对比视图（2-4 张并排，锁定缩放/平移）----
window.openCompareView = () => {
    const items = Array.from(STATE.selected);
    if (items.length < 2 || items.length > 4) return window.showToast('请选择 2-4 张图片进行对比', 'warning');
    // [U10] 校验筛选后的实际可渲染集合：选区含被筛选隐藏的文件时
    // items.length 过检但 files.length 可为 1（"单张对比"全屏展示）
    const files = items.map(p => STATE.visibleFiles.find(f => f.path === p)).filter(Boolean);
    if (files.length < 2) return window.showToast('选中的可见文件不足 2 张（部分已被筛选隐藏）', 'warning');
    const view = $('#compareView'), panes = $('#cmpPanes');
    if (!view || !panes) return;
    panes.innerHTML = files.map(f => `<div class="cmp-pane"><img src="/api/file?path=${encodeURIComponent(f.path)}" draggable="false"><div class="cmp-name text-truncate">${escapeHtml(f.name)}</div></div>`).join('');
    $('#cmpTitle').textContent = `对比 ${files.length} 张 · 滚轮缩放（锁定）、拖拽平移（同步）`;
    view.style.display = 'flex';
    window._cmpState = { zoom: 1, tx: 0, ty: 0 };
    applyCompareTransform();
};
window.closeCompareView = () => { const v = $('#compareView'); if (v) v.style.display = 'none'; };
function applyCompareTransform() {
    const s = window._cmpState || { zoom: 1, tx: 0, ty: 0 };
    $$('#cmpPanes .cmp-pane img').forEach(img => {
        img.style.transform = `translate(${s.tx}px, ${s.ty}px) scale(${s.zoom})`;
    });
}
window.compareZoom = (factor) => {
    const s = window._cmpState = window._cmpState || { zoom: 1, tx: 0, ty: 0 };
    s.zoom = factor === 0 ? 1 : Math.max(0.1, Math.min(10, s.zoom * factor));
    if (factor === 0) { s.tx = 0; s.ty = 0; }
    applyCompareTransform();
};
document.addEventListener('DOMContentLoaded', () => {
    const view = $('#compareView');
    if (!view) return;
    view.addEventListener('wheel', (e) => {
        e.preventDefault();
        window.compareZoom(e.deltaY > 0 ? 0.9 : 1.1);
    }, { passive: false });
    let dragging = false, sx = 0, sy = 0;
    view.addEventListener('mousedown', (e) => { dragging = true; sx = e.clientX - (window._cmpState?.tx || 0); sy = e.clientY - (window._cmpState?.ty || 0); e.preventDefault(); });
    window.addEventListener('mousemove', (e) => { if (!dragging || view.style.display === 'none') return; window._cmpState.tx = e.clientX - sx; window._cmpState.ty = e.clientY - sy; applyCompareTransform(); });
    window.addEventListener('mouseup', () => dragging = false);
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && view.style.display !== 'none') window.closeCompareView(); });
});

// ---- 重复文件查找 ----
window.openDuplicatesModal = () => {
    // [V5 复查] 虚拟视图一律不预填（此前漏了 collection://、similar://，
    // 会把 "collection://3" 填进扫描目录，一点"扫描"必失败）
    const inp = $('#dupScanPath');
    if (inp && !inp.value) inp.value = isVirtualView(STATE.currentPath) ? '' : STATE.currentPath;
    const modalEl = $('#duplicatesModal'); if (modalEl) new bootstrap.Modal(modalEl).show();
};
window.runDuplicateScan = async () => {
    const path = $('#dupScanPath')?.value.trim();
    if (!path) return window.showToast('请输入扫描目录', 'warning');
    const results = $('#dupResults'), summary = $('#dupSummary');
    results.innerHTML = '<div class="text-center text-muted py-4"><div class="spinner-border text-primary" role="status"></div><div class="mt-2 small">正在扫描（按体积预分组，仅对疑似重复做哈希）...</div></div>';
    summary.textContent = '';
    try {
        const data = await feFetch(`/api/duplicates/scan?path=${encodeURIComponent(path)}`, {}, { timeout: 300000, context: '重复扫描' });
        window._dupGroups = data.groups || [];
        window._dupTruncated = !!data.truncated;
        window.renderDupResults();
    } catch (e) { results.innerHTML = '<div class="text-danger text-center py-4">扫描失败</div>'; }
};
// [U9] 渲染抽成独立函数：删除重复项后按剩余分组重绘（此前 DOM 不更新，
// 再次点击会对已入回收站的路径重复提交）
window.renderDupResults = () => {
    const results = $('#dupResults'), summary = $('#dupSummary');
    const groups = window._dupGroups || [];
    if (summary) {
        let txt = `发现 ${groups.length} 组重复`;
        if (window._dupTruncated) txt += '（已达到扫描/哈希上限，结果可能不完整）';
        summary.textContent = txt;
    }
    if (!groups.length) { results.innerHTML = '<div class="text-muted text-center py-4">没有发现重复文件 🎉</div>'; return; }
    const fmtSize = (b) => b > 1073741824 ? (b / 1073741824).toFixed(2) + ' GB' : b > 1048576 ? (b / 1048576).toFixed(1) + ' MB' : (b / 1024).toFixed(0) + ' KB';
    results.innerHTML = groups.map((g, gi) => `
        <div class="dup-group">
            <div class="dup-group-head text-white-50 small">${fmtSize(g.size)} × ${g.files.length} 份</div>
            ${g.files.map((fp, fi) => `
                <label class="dup-row"><input type="radio" name="dup-keep-${gi}" value="${fi}" ${fi === 0 ? 'checked' : ''}>
                <span class="text-truncate" title="${escapeHtml(fp)}">${escapeHtml(fp)}</span></label>`).join('')}
        </div>`).join('');
};
window.deleteDuplicateExtras = async () => {
    const groups = window._dupGroups || [];
    if (!groups.length) return window.showToast('请先扫描', 'warning');
    const toDelete = [];
    groups.forEach((g, gi) => {
        const keepRaw = $(`input[name="dup-keep-${gi}"]:checked`)?.value || '0';
        const keep = parseInt(keepRaw, 10);
        g.files.forEach((fp, fi) => { if (fi !== keep) toDelete.push(fp); });
    });
    if (!toDelete.length) return window.showToast('没有可删除的项', 'info');
    if (!(await window.showConfirmDialog(`将把 ${toDelete.length} 个重复文件移入回收站（可恢复），确定吗？`, '移入回收站'))) return;
    try {
        const data = await feFetch('/api/delete', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ paths: toDelete }) }, { context: '删除重复项' });
        if (data.records?.length) window.pushUndo({ type: 'delete', trashIds: data.records.filter(r => !r.in_os_trash).map(r => r.id), dir: STATE.currentPath });
        const deletedSet = new Set(data.deleted || []);
        window.showToast(`已移入回收站 ${deletedSet.size} 个重复文件`, 'success');
        // [U9] 从分组中剔除已删路径（仅剩 1 份的组不再算重复）并重绘
        window._dupGroups = groups
            .map(g => ({ ...g, files: g.files.filter(fp => !deletedSet.has(fp)) }))
            .filter(g => g.files.length >= 2);
        window.renderDupResults();
        reloadFiles();
    } catch (e) {}
};

// ---- 批量旋转 / 批量移动 ----
window.batchRotate = async () => {
    const paths = Array.from(STATE.selected);
    if (!paths.length) return;
    if (!(await window.showConfirmDialog(`将选中的 ${paths.length} 个图片顺时针旋转 90°？`, '批量旋转'))) return;
    try {
        const data = await feFetch('/api/batch/rotate', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ paths, degrees: 90 }) }, { timeout: 120000, context: '批量旋转' });
        if (data.rotated?.length) {
            window.showToast(`已旋转 ${data.rotated.length} 个文件`, 'success');
            if (data.errors?.length) window.showToast(`${data.errors.length} 个失败：${data.errors[0]}`, 'warning');
            // 缩略图是旧方向：清缓存重载（后端失败缓存 1 小时内不会重试，直接改 v 参数刷）
            reloadFiles();
        }
    } catch (e) {}
};
window.batchMove = async () => {
    const paths = Array.from(STATE.selected);
    if (!paths.length) return;
    const dest = await window.showInputDialog('批量移动 — 输入目标目录', STATE.currentPath);
    if (!dest) return;
    try {
        const data = await feFetch('/api/batch/move', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ paths, dest }) }, { timeout: 120000, context: '批量移动' });
        if (data.moved?.length) {
            window.showToast(`已移动 ${data.moved.length} 个文件`, 'success');
            if (data.errors?.length) window.showToast(`${data.errors.length} 个失败：${data.errors[0]}`, 'warning');
            reloadFiles();
        }
    } catch (e) {}
};
})();
