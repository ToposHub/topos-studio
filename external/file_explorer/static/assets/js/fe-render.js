/** fe-render.js — [ARCH-13] render 模块：文件卡片/目录树/灯箱/属性面板/
 * 浮层与进度等一切 DOM 构建与同步（IIFE）。
 *
 * 对 actions 的晚期绑定（openItem/loadPath，actions 后于本模块求值）
 * 经 window.__FE 取用；actions → render 一律在模块头解构。 */
(() => {
const { STATE, $, $$, escapeHtml, cssPathSel, getFileIcon, parseSize,
        updateCardVisual, PREVIEW_FORMATS } = window.__FE;
window.applyFilterAndSort = () => { // 执行筛选与排序并重绘列表
    let files = STATE.allFiles;
    const sInp = $('#searchInput'), sClr = $('#searchClear');
    let searchActive = false;
    // [U10] search:// 视图的结果已由服务端过滤（多词 AND 语义），本地若再按
    // 整句子串滤一遍，"落日 航拍"（两词分处 tags/comments）的合法命中会被丢弃
    const serverFiltered = (STATE.currentPath || '').startsWith('search://');
    if (sInp?.value.trim() && !serverFiltered) {
        // [UX-8] 匹配范围扩展到 标签/备注（此前仅文件名子串）
        searchActive = true;
        const q = sInp.value.trim().toLowerCase(); if(sClr) sClr.style.display = 'block';
        files = files.filter(f => f.name.toLowerCase().includes(q)
            || (f.tags||'').toLowerCase().includes(q)
            || (f.comments||'').toLowerCase().includes(q));
    }
    else if(sClr) sClr.style.display = 'none';
    const cntEl = $('#searchCount');
    if (cntEl) { cntEl.style.display = searchActive ? 'inline' : 'none'; if (searchActive) cntEl.textContent = files.length ? `${files.length} 项匹配` : '无匹配'; }
    if (STATE.filter.type !== 'all') files = files.filter(f => STATE.filter.type === 'doc' ? (f.type !== 'dir' && !['video','image','audio'].includes(f.type)) : (f.type === STATE.filter.type));
    if (STATE.filter.minRating > 0) files = files.filter(f => (f.rating || 0) >= STATE.filter.minRating);
    // [U5/GAP-03] 颜色/旗标前端筛选（含"隐藏拒收"评审工作流）
    if (STATE.filter.color > 0) files = files.filter(f => (f.color || 0) === STATE.filter.color);
    if (STATE.filter.flag === 'pick') files = files.filter(f => (f.flag || 0) === 1);
    else if (STATE.filter.flag === 'noreject') files = files.filter(f => (f.flag || 0) !== 2);
    // [V1/GAP-02] 拍摄日期范围 / 最短时长（索引器回填字段；无拍摄日期的文件
    // 按修改时间参与日期筛选，时长未知的文件不满足"≥N"自然被滤除）
    if (STATE.filter.dateFrom || STATE.filter.dateTo || STATE.filter.minDuration > 0) {
        const dfTs = STATE.filter.dateFrom ? Date.parse(STATE.filter.dateFrom) / 1000 : null;
        const dtTs = STATE.filter.dateTo ? Date.parse(STATE.filter.dateTo) / 1000 + 86400 : null;
        files = files.filter(f => {
            const ts = (typeof f.date_taken_ts === 'number' && f.date_taken_ts) || f.mtime_ts || 0;
            if (dfTs && ts < dfTs) return false;
            if (dtTs && ts >= dtTs) return false;
            if (STATE.filter.minDuration > 0 && !((f.duration || 0) >= STATE.filter.minDuration)) return false;
            return true;
        });
    }
    // [V3/GAP-09] 自动主色桶筛选（''=不过滤；-1=黑白灰）
    if (STATE.filter.domBucket !== '') {
        const want = parseInt(STATE.filter.domBucket, 10);
        files = files.filter(f => f.dom_bucket !== null && f.dom_bucket !== undefined && f.dom_bucket === want);
    }
    // [V2/GAP-07] 堆叠折叠：封面在场且未展开时，只显示封面一张（×N 角标展开）
    {
        const coverSeen = {};
        files.forEach(f => { if (f.stack_id && f.stack_cover) coverSeen[f.stack_id] = (coverSeen[f.stack_id] || f.path === f.stack_cover); });
        files = files.filter(f => {
            if (!f.stack_id || !f.stack_cover) return true;
            if (!coverSeen[f.stack_id]) return true;  // 封面不在列表（被筛掉/已删）→ 全员可见
            return f.path === f.stack_cover || STATE.expandedStacks.has(f.stack_id);
        });
    }
    // [BUG-P2-19] 三态比较器（旧实现相等值恒 ±1：同值文件顺序抖动）+
    // date 字段数值/字符串统一成可比数字（混合回退此前可产生 NaN）
    const cmp = (x, y) => (x < y ? -1 : x > y ? 1 : 0);
    const toDateNum = (f) => (typeof f.mtime_ts === 'number' && !isNaN(f.mtime_ts)) ? f.mtime_ts : (Date.parse(f.mtime) || 0);
    // [U7/GAP-07] 时间线按时间组织：强制日期排序，否则分组会交错
    const sortField = STATE.viewMode === 'timeline' ? 'date' : STATE.sort.field;
    files.sort((a, b) => { // 文件夹始终置顶
        if (a.type === 'dir' && b.type !== 'dir') return -1; if (a.type !== 'dir' && b.type === 'dir') return 1;
        let vA, vB;
        switch (sortField) {
            case 'name': vA = a.name.toLowerCase(); vB = b.name.toLowerCase(); break;
            case 'ext': vA = a.name.split('.').pop().toLowerCase(); vB = b.name.split('.').pop().toLowerCase(); break;
            case 'size': // [UX-4] 优先用后端数值键：不再反解显示字符串（同值误序/性能）
                vA = (typeof a.size_bytes === 'number') ? a.size_bytes : parseSize(a.size); vB = (typeof b.size_bytes === 'number') ? b.size_bytes : parseSize(b.size); break;
            case 'date': // [UX-4] 秒级时间戳（旧实现只有日期粒度，同日文件排序抖动）
                vA = toDateNum(a); vB = toDateNum(b); break;
            case 'rating': vA = a.rating || 0; vB = b.rating || 0; break;
            default: vA = a.name; vB = b.name;
        }
        const c = cmp(vA, vB);
        return STATE.sort.order === 'asc' ? c : -c;
    });
    // [U7/GAP-07] 分组计数（组头显示）+ 跨批次分组状态复位
    STATE.tlCounts = {};
    if (STATE.viewMode === 'timeline') {
        files.forEach(f => { const k = tlKeyOf(f); STATE.tlCounts[k] = (STATE.tlCounts[k] || 0) + 1; });
    }
    STATE.tlLastKey = null;
    // [BUG-P1-10/BUG-P2-17] 集中重置散落状态：lastIndex/滚动位置。
    // 只重置 renderIdx 不清 lastIndex：筛选缩短列表后 Space/Enter 按
    // 越界索引打开错误文件；不清 scrollTop：切目录后残留滚动位置。
    STATE.visibleFiles = files; STATE.renderIdx = 0; STATE.lastIndex = -1;
    STATE.visuallySelected.clear(); unobserveThumbImgs($('#fileContainer')); $('#fileContainer').innerHTML = '';
    const sc = $('#scrollContainer'); if (sc) sc.scrollTop = 0;
    if(!files.length) $('#emptyState').classList.remove('d-none'); else $('#emptyState').classList.add('d-none');
    renderNextBatch(); updateFilterButtonState(); updateSortMenuState();
};

// [U10] 清空卡片容器前先解除缩略图观察：IntersectionObserver 对已摘除
// DOM 的 img 仍持强引用（Chromium 不回收已观察目标），跨目录导航会无限
// 累积滞留节点与请求上下文
function unobserveThumbImgs(container) {
    if (!_thumbIO || !container) return;
    container.querySelectorAll('img.file-thumb').forEach(img => _thumbIO.unobserve(img));
}

const updateFilterButtonState = () => {
    const btn = $('#filterMenuBtn'); if(!btn) return;
    // [U10] 补 U5 新增维度：设置了颜色/旗标筛选时按钮也需高亮
    // [V1] 补拍摄日期/时长维度
    const isActive = STATE.filter.type !== 'all' || STATE.filter.minRating > 0
        || (STATE.filter.color || 0) > 0 || (STATE.filter.flag || 'all') !== 'all'
        || !!STATE.filter.dateFrom || !!STATE.filter.dateTo || (STATE.filter.minDuration || 0) > 0
        || (STATE.filter.domBucket !== '' && STATE.filter.domBucket !== undefined && STATE.filter.domBucket !== null);
    btn.classList.toggle('btn-primary', isActive); btn.classList.toggle('btn-outline-secondary', !isActive);
};
const updateSortMenuState = () => { // 更新排序菜单高亮状态
    // [ARCH-13] 菜单项已改 data-action 委托，高亮改读 data-args（原读 onclick 属性）
    $$('#sortMenuBtn + .dropdown-menu .dropdown-item').forEach(el => { if (el.dataset.action === 'setSort') el.classList.remove('active'); });
    $$(`#sortMenuBtn + .dropdown-menu .dropdown-item`).forEach(el => {
        if (el.dataset.action === 'setSort' && (el.dataset.args || '').includes(`"${STATE.sort.field}"`)) el.classList.add('active');
    });
    const orderLabel = $('#sortOrderLabel'); if(orderLabel) { const isAsc = STATE.sort.order === 'asc'; orderLabel.innerHTML = isAsc ? '<i class="bi bi-arrow-down"></i> 升序 (A-Z)' : '<i class="bi bi-arrow-up"></i> 降序 (Z-A)'; }
};
const updateFilterMenuState = () => {
    const menu = $('#filterMenuBtn + .dropdown-menu'); if(!menu) return;
    menu.querySelectorAll('[data-filter-type]').forEach(i => i.classList.toggle('active', i.dataset.filterType === STATE.filter.type));
    menu.querySelectorAll('[data-filter-rating]').forEach(i => i.classList.toggle('active', parseInt(i.dataset.filterRating) === STATE.filter.minRating));
    // [U5/GAP-03] 颜色/旗标高亮
    menu.querySelectorAll('[data-filter-color]').forEach(i => i.classList.toggle('active', parseInt(i.dataset.filterColor) === (STATE.filter.color || 0)));
    menu.querySelectorAll('[data-filter-flag]').forEach(i => i.classList.toggle('active', i.dataset.filterFlag === (STATE.filter.flag || 'all')));
    // [V1/GAP-02] 时长筛选高亮
    menu.querySelectorAll('[data-filter-duration]').forEach(i => i.classList.toggle('active', parseFloat(i.dataset.filterDuration) === (STATE.filter.minDuration || 0)));
    // [V3/GAP-09] 自动颜色桶高亮（data-args 携带桶值）
    menu.querySelectorAll('.dom-dot').forEach(i => {
        const raw = (i.dataset.args || '').replace(/[\["\]]/g, '');
        i.classList.toggle('active', raw === String(STATE.filter.domBucket));
    });
};

// [V1/GAP-02] 时长秒 → "m:ss" / "h:mm:ss"
const fmtDuration = (s) => {
    s = Math.max(0, Math.round(s));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    const p2 = (n) => String(n).padStart(2, '0');
    return h ? `${h}:${p2(m)}:${p2(sec)}` : `${m}:${p2(sec)}`;
};
// [V1/GAP-02] 时间线分组键：基准可切（修改时间 | 拍摄时间）
const tlKeyOf = (f) => {
    if (STATE.timelineBase === 'taken') {
        const ts = (typeof f.date_taken_ts === 'number' && f.date_taken_ts) || f.mtime_ts;
        if (ts) { const d = new Date(ts * 1000); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`; }
        return '未知';
    }
    return (f.mtime || '').substring(0, 7) || '未知';
};

const cardBadgesHtml = (f) => {
    if (!f || f.type === 'dir') return '';
    // [BUG-P2-25] rating 后端若以字符串返回即属性注入点——统一转义
    const r = (f.rating > 0) ? `<span class="badge-rating" title="${escapeHtml(String(f.rating))} 星">${'★'.repeat(f.rating)}</span>` : '';
    const t = f.tags ? `<span class="badge-tag" title="${escapeHtml(f.tags)}"><i class="bi bi-tag-fill"></i></span>` : '';
    // [U5/GAP-03] 颜色标记圆点 + 旗标/拒收角标
    const c = (f.color > 0) ? `<span class="badge-color c${f.color}" title="${escapeHtml(String(f.color))}"></span>` : '';
    const fl = (f.flag === 1) ? `<span class="badge-flag" title="旗标"><i class="bi bi-flag-fill"></i></span>`
             : (f.flag === 2) ? `<span class="badge-flag badge-reject" title="拒收"><i class="bi bi-x-circle-fill"></i></span>` : '';
    const parts = `${c}${fl}${r}${t}`;
    return parts ? `<div class="card-badges">${parts}</div>` : '';
};
// [BUG-P2-18] 缩略图计数观察器：图片进入视口前 300px 才计入 total，
// 与 loading=lazy 的实际加载时机对齐
const _thumbIO = ('IntersectionObserver' in window)
    ? new IntersectionObserver((entries) => {
        for (const en of entries) {
            if (!en.isIntersecting) continue;
            _thumbIO.unobserve(en.target);
            if (en.target.dataset.thumbTotalCounted) continue;
            en.target.dataset.thumbTotalCounted = '1';
            STATE.thumbLoading.total++; updateThumbProgress();
        }
    }, { rootMargin: '300px' }) : null;

function createCard(f, i) { // 创建文件/文件夹卡片
    const isSel = STATE.selected.has(f.path); if(isSel) STATE.visuallySelected.add(i);
    // [BUG-P2-18] 不再预增 thumbLoading.total（视口外 lazy 图永不 load，
    // 预增会让进度条永久悬挂）：有 IO 时图片接近视口才计入 total；无 IO
    // 环境（老内核）退回创建时计数，加载必然发生。
    const needThumbCount = (f.type === 'video' || f.type === 'image' || f.type === 'doc');
    const div = document.createElement('div');
    div.className = `file-card animate-fade-in ${isSel?'selected':''} ${f.type==='dir'?'type-dir':'type-file'}`;
    div.id = `card-${i}`; div.dataset.index = i;
    if (f.type !== 'dir') {
        div.setAttribute('draggable', 'true');
        div.addEventListener('dragstart', (e) => {
            const fileUrl = 'file://' + encodeURI(f.path.replace(/\\/g, '/'));
            e.dataTransfer.setData('text/uri-list', fileUrl); e.dataTransfer.setData('text/plain', fileUrl); e.dataTransfer.effectAllowed = 'copy';
        });
    }
    const ext = f.name.split('.').pop().toLowerCase();
    const isAudio = f.type === 'audio' || ['mp3','wav','ogg','flac','m4a','aac'].includes(ext);
    // [V5 复查] 提升到函数级：此前声明在 doc 分支块内，image/video 分支
    // 引用未定义变量直接 ReferenceError，整个网格渲染中断
    const sizeParam = STATE.useLargeThumbs ? '&size=large' : '&size=small';
    let inner = '';
    if(f.type === 'dir') inner = `<div class="non-img-wrapper" style="aspect-ratio:1.6/1"><i class="bi bi-folder-fill text-warning"></i></div>`;
    else if (isAudio) {
        div.dataset.isAudio = "true"; div.dataset.audioPath = f.path;
        inner = `<div class="audio-card-wrapper" id="audio-wrapper-${i}"><div class="audio-placeholder"><i class="bi bi-music-note-beamed"></i><span style="font-size:12px;margin-top:5px;">${escapeHtml(ext.toUpperCase())}</span></div><div id="waveform-${i}" class="waveform-container"></div><div class="audio-time-badge" id="audio-time-${i}">00:00</div>${cardBadgesHtml(f)}</div>`;
    } else if(f.type==='image' || f.type==='video') {
        // [FUNC-16] res 来自 ffprobe 输出，插入 DOM 前转义
        const res = (f.type==='video' && f.res) ? `<div class="res-badge">${escapeHtml(f.res.split('·')[0].trim())}</div>` : '';
        // [V1/GAP-02] 视频时长角标（索引器/ffprobe 队列回填 duration 后出现）
        const durBadge = (f.type==='video' && typeof f.duration === 'number' && f.duration > 0) ? `<div class="dur-badge">${fmtDuration(f.duration)}</div>` : '';
        // [V2/GAP-07] 堆叠封面角标（×N，点击展开/收起成员）
        const stackBadge = (f.stack_id && f.stack_size > 1 && f.path === f.stack_cover)
            ? `<div class="stack-badge" data-action="toggleStackExpand" data-stop data-args="[${parseInt(f.stack_id, 10) || 0}]" title="堆叠 ${parseInt(f.stack_size, 10) || 1} 张，点击展开"><i class="bi bi-layers-fill"></i> ${parseInt(f.stack_size, 10) || 1}</div>` : '';
        // [U7/GAP-10] 视频 wrapper 带 data-video-scrub：容器级 mousemove 懒加载胶片条并按悬停比例切帧
        const scrubAttr = f.type==='video' ? ` data-video-scrub="${escapeHtml(f.path)}"` : '';
        inner = `<div class="file-thumb-wrapper" style="background-color: #252526;"${scrubAttr}><img src="/api/thumb?path=${encodeURIComponent(f.path)}&v=${f.t_ver||0}${sizeParam}" class="file-thumb" loading="lazy" decoding="async" crossorigin="anonymous" draggable="false" onload="onThumbLoadComplete(this)" onerror="this.style.display='none';this.nextElementSibling.style.display='flex';onThumbLoadComplete(this)"><div class="fallback-icon" style="display:none;width:100%;height:100%;align-items:center;justify-content:center;"><i class="bi bi-image" style="font-size:3rem;color:#444;"></i><span class="thumb-retry" title="重新生成缩略图"><i class="bi bi-arrow-clockwise"></i> 重试</span></div>${f.type==='video'?'<i class="bi bi-play-circle-fill play-icon-center"></i>':''}${res}${durBadge}${stackBadge}${cardBadgesHtml(f)}</div>`;
    } else if(f.type==='doc') {
        // 文档类型（PDF、Word、Excel、PPT）尝试使用 QuickLook 预览
        inner = `<div class="file-thumb-wrapper" style="background-color: #252526;"><img src="/api/thumb?path=${encodeURIComponent(f.path)}&v=${f.t_ver||0}${sizeParam}" class="file-thumb" loading="lazy" decoding="async" crossorigin="anonymous" draggable="false" onload="onThumbLoadComplete(this)" onerror="this.style.display='none';this.nextElementSibling.style.display='flex';onThumbLoadComplete(this)"><div class="fallback-icon" style="display:none;width:100%;height:100%;align-items:center;justify-content:center;"><i class="bi ${getFileIcon(f.name)}" style="font-size:3rem;color:#444;"></i></div>${cardBadgesHtml(f)}</div>`;
    } else {
        // 代码、文本、压缩包等其他文件类型使用图标显示
        const fileIcon = getFileIcon(f.name);
        inner = `<div class="non-img-wrapper" style="aspect-ratio:1/1.2;position:relative;"><i class="bi ${fileIcon}"></i>${cardBadgesHtml(f)}</div>`;
    }
    // [V1/GAP-02] 视频卡 meta 行补时长（未回填 duration 时维持原样）
    const durText = (f.type==='video' && typeof f.duration === 'number' && f.duration > 0) ? ` · ${fmtDuration(f.duration)}` : '';
    // [V3/GAP-11] 递归聚合视图：来源子目录 chip
    const subText = (f.subdir && STATE.recursive) ? `[${f.subdir}] ` : '';
    const meta = (f.type==='video' && f.res) ? `${subText}${f.size} · ${f.res}${durText}` : (f.size ? `${subText}${f.size} · ${f.mtime||''}` : `${subText}${f.mtime||''}`);
    // [FUNC-16] meta 文本同样转义（res/size/mtime 均来自磁盘探测输出）
    div.innerHTML = `<input type="checkbox" class="card-checkbox" ${STATE.selected.has(f.path)?'checked':''}>${inner}<div class="file-info"><div class="file-name" title="${escapeHtml(f.name)}">${escapeHtml(f.name)}</div><div class="file-meta"><span class="text-truncate" title="${escapeHtml(meta)}">${escapeHtml(meta)}</span></div></div>`;
    if (needThumbCount) {
        const thumbImg = div.querySelector('img.file-thumb');
        if (_thumbIO && thumbImg) _thumbIO.observe(thumbImg);
        else if (!thumbImg) { /* 无图（图标卡）不计数 */ }
        else { STATE.thumbLoading.total++; }
    }
    return div;
}
function renderNextBatch() { // 瀑布流/分页渲染
    if(STATE.renderIdx >= STATE.visibleFiles.length) return;
    const frag = document.createDocumentFragment(), end = Math.min(STATE.renderIdx + STATE.BATCH_SIZE, STATE.visibleFiles.length);
    // [U7/GAP-07] 时间线布局：按月分组渲染（组头带日期与数量，点击收起）
    if (STATE.viewMode === 'timeline') {
        const container = $('#fileContainer');
        // [U10] 跨批次延续的月份组：groupEl 此前每批重置为 null，而
        // tlLastKey 跨批保留——第 101 个文件与第 100 个同月时第二批
        // 直接 appendChild(null) 抛 TypeError，renderIdx 卡死、剩余
        // 文件永不渲染。从 DOM 恢复上一批的组容器
        let groupEl = (STATE.tlLastKey ? document.getElementById(`tlg-${STATE.tlLastKey}`) : null);
        if (STATE.tlLastKey && !groupEl) STATE.tlLastKey = null; // 组容器被清空（重渲染）则从头建组
        for(let i=STATE.renderIdx; i<end; i++) {
            const f = STATE.visibleFiles[i];
            const key = tlKeyOf(f);
            if (key !== STATE.tlLastKey) {
                const cnt = STATE.tlCounts ? (STATE.tlCounts[key] || '') : '';
                const header = document.createElement('div');
                header.className = 'tl-header'; header.dataset.tlGroup = key;
                header.innerHTML = `<i class="bi bi-caret-down-fill me-1"></i>${escapeHtml(key.replace('-', ' 年 '))} 月 · ${cnt} 项`;
                groupEl = document.createElement('div');
                groupEl.className = 'tl-group'; groupEl.id = `tlg-${key}`;
                container.appendChild(header); container.appendChild(groupEl);
                STATE.tlLastKey = key;
            }
            groupEl.appendChild(createCard(f, i));
        }
        STATE.renderIdx = end;
        const sentinel = $('#scrollSentinel');
        if (sentinel && STATE.renderIdx < STATE.visibleFiles.length
                && sentinel.getBoundingClientRect().top < window.innerHeight + 200) renderNextBatch();
        return;
    }
    for(let i=STATE.renderIdx; i<end; i++) frag.appendChild(createCard(STATE.visibleFiles[i], i));
    $('#fileContainer').appendChild(frag); STATE.renderIdx = end;
    // [FUNC-22] 超高视口下 sentinel 始终可见但不会有状态跃迁 → 主动续载
    const sentinel = $('#scrollSentinel');
    if (sentinel && STATE.renderIdx < STATE.visibleFiles.length
            && sentinel.getBoundingClientRect().top < window.innerHeight + 200) renderNextBatch();
}

/** 音频预览 (WaveSurfer) */
const updateTimeBadge = (idx, cur, tot) => {
    const b = $(`#audio-time-${idx}`); if (!b) return;
    const fmt = (s) => (!s || s === Infinity) ? "0:00" : `${Math.floor(s/60)}:${Math.floor(s%60).toString().padStart(2,'0')}`;
    b.innerText = `${fmt(cur)} / ${fmt(tot)}`;
};

/** 缩略图进度追踪 */
window.updateThumbProgress = () => { // 更新顶部缩略图生成进度条
    const panel = $('#thumbProgressPanel'), bar = $('#thumbProgressBar'), text = $('#thumbProgressText'); if (!panel || !bar || !text) return;
    const { total, loaded } = STATE.thumbLoading;
    if (total > 0 && loaded < total) { if (!STATE.thumbLoading.active) { STATE.thumbLoading.active = true; panel.classList.add('show'); } const percent = (loaded / total) * 100; bar.style.width = percent + '%'; text.innerText = `${loaded}/${total}`; }
    else if (STATE.thumbLoading.active) { bar.style.width = '100%'; text.innerText = `${total}/${total}`;
        // [FUNC-19] 800ms 收尾定时器带代次令牌：期间切了新目录则不再清新目录的计数
        const gen = STATE.thumbLoading.gen = (STATE.thumbLoading.gen || 0) + 1;
        setTimeout(() => { if (gen !== STATE.thumbLoading.gen) return; panel.classList.remove('show'); STATE.thumbLoading.active = false; STATE.thumbLoading.total = 0; STATE.thumbLoading.loaded = 0; }, 800); }
};
// [FUNC-19] 按元素去重：refreshThumbnail 换 src 后 load 再触发不重复计数
// （此前 "150/100" 超过 100% 由此而来）
// [BUG-P2-18] total 只统计"进入视口即将加载"的图（_thumbIO，见 createCard），
// loaded 统计真正完成（load/error 均计）的图——视口外 lazy 图不再把进度条
// 永久卡在半程。
window.onThumbLoadComplete = (el) => {
    if (el) { if (el.dataset.thumbCounted) return; el.dataset.thumbCounted = '1'; }
    STATE.thumbLoading.loaded++; updateThumbProgress();
};

/** 路径加载与导航 */
// 标记是否是由 Python 端触发的路径切换（避免循环通知）
const updateNavButtons = () => { $('#btnBack').disabled = STATE.historyIdx <= 0; $('#btnForward').disabled = STATE.historyIdx >= STATE.history.length-1; };
const updateToolbar = () => { const c = STATE.selected.size; $('#selCount').innerText = c; $('#selectionActions').classList.toggle('d-none', c === 0); };
function updateSelectionUI() { updateToolbar(); STATE.visuallySelected.clear(); STATE.visibleFiles.forEach((f, idx) => { const isSel = STATE.selected.has(f.path); if(isSel) STATE.visuallySelected.add(idx); updateCardVisual(idx); }); }

/** 右键菜单与关联应用 */
let loadedAppsPath = null;
async function loadAssociatedApps(path) { // 加载系统关联的应用程序
    if (loadedAppsPath === path) return; const submenu = $('#ctx-sys-submenu'); if(!submenu) return;
    try {
        const res = await fetch(`/api/apps/associated?path=${encodeURIComponent(path)}`), data = await res.json();
        // [FUNC-16] app.name/app.path 来自系统枚举，不再拼进 onclick JS 字符串；
        // 经 data 属性 + 委托点击取用（setupContextMenu 内统一监听）
        let html = `<div class="menu-item" data-action="handleMenuSysOpen"><i class="bi bi-laptop me-2"></i>默认程序</div>`;
        if (data.apps?.length) { html += `<div class="dropdown-divider"></div>`; html += data.apps.map(app => `<div class="menu-item ctx-app-item" data-app-path="${escapeHtml(app.path)}"><i class="bi bi-app-indicator me-2"></i><span class="text-truncate" style="max-width:180px">${escapeHtml(app.name)}</span></div>`).join(''); }
        else html += `<div class="dropdown-divider"></div><div class="menu-item text-muted small px-3">无其他推荐</div>`;
        submenu.innerHTML = html; loadedAppsPath = path;
    } catch (e) { submenu.innerHTML = '<div class="menu-item text-danger small px-3">加载失败</div>'; }
}
function setupContextMenu() { // 初始化右键菜单事件
    const m = $('#contextMenu'); if (!m) return;
    const setV = (id, v) => { const el = $(`#${id}`); if (el) el.style.display = v ? 'flex' : 'none'; };
    if ($('#ctx-sys-open')) $('#ctx-sys-open').onmouseenter = () => { const p = getContextPath(); if(p) loadAssociatedApps(p); };
    // [FUNC-16] 应用子菜单：委托读取 data-app-path（appPath 不再进 onclick JS 串）
    m.addEventListener('click', (e) => {
        const item = e.target.closest('.ctx-app-item');
        if (item) window.openWithApp(item.dataset.appPath);
    });
    document.addEventListener('contextmenu', (e) => {
        const c = e.target.closest('.file-card'), t = e.target.closest('.tree-node-container'), s = e.target.closest('#scrollContainer');
        let show = false;
        if (c) {
            e.preventDefault(); const i = parseInt(c.dataset.index), p = STATE.visibleFiles[i].path; STATE.contextTarget = { type: 'file', index: i, path: p };
            if (!STATE.selected.has(p)) { window.clearSelection(); STATE.selected.add(p); STATE.visuallySelected.add(i); updateCardVisual(i, true); STATE.lastIndex = i; updateToolbar(); }
            const curTarget = STATE.visibleFiles[i] || {};
            ['ctx-open', 'ctx-sys-open', 'ctx-reveal', 'ctx-div-mid', 'ctx-rename', 'ctx-div-1', 'ctx-copy', 'ctx-div-2', 'ctx-del'].forEach(id => setV(id, true)); setV('ctx-new-folder', false); setV('ctx-paste', false); setV('ctx-batch', STATE.selected.size > 1);
            // [V2/GAP-07] 堆叠项 + [V2/GAP-05] 集合视图"移除成员" + [V3/GAP-08] 查找相似
            setV('ctx-stack', STATE.selected.size >= 2);
            setV('ctx-unstack', !!curTarget.stack_id);
            setV('ctx-coll-remove', (STATE.currentPath || '').startsWith('collection://'));
            setV('ctx-similar', curTarget.type !== 'dir');
            setV('ctx-lock', false);
            show = true;
        } else if (t) {
            e.preventDefault(); STATE.contextTarget = { type: 'tree', path: t.dataset.path };
            ['ctx-open', 'ctx-new-folder', 'ctx-sys-open', 'ctx-reveal', 'ctx-div-mid', 'ctx-rename', 'ctx-div-1', 'ctx-copy', 'ctx-div-2', 'ctx-del'].forEach(id => setV(id, true)); setV('ctx-paste', false); setV('ctx-batch', false);
            ['ctx-stack', 'ctx-unstack', 'ctx-coll-remove', 'ctx-similar'].forEach(id => setV(id, false));
            setV('ctx-lock', true);  // [V4/GAP-22] 目录可设软锁
            show = true;
        } else if (s) {
            e.preventDefault(); STATE.contextTarget = { type: 'background', path: STATE.currentPath };
            ['ctx-new-folder', 'ctx-paste'].forEach(id => setV(id, true)); ['ctx-open', 'ctx-sys-open', 'ctx-reveal', 'ctx-div-mid', 'ctx-rename', 'ctx-div-1', 'ctx-copy', 'ctx-batch', 'ctx-div-2', 'ctx-del', 'ctx-stack', 'ctx-unstack', 'ctx-coll-remove', 'ctx-similar', 'ctx-lock'].forEach(id => setV(id, false)); show = true;
        }
        if (show) { loadedAppsPath = null; if($('#ctx-sys-submenu')) $('#ctx-sys-submenu').innerHTML = '<div class="menu-item text-muted small px-3">加载中...</div>'; let x = e.clientX, y = e.clientY; if (x + 180 > window.innerWidth) x -= 180; if (y + 250 > window.innerHeight) y -= 250; m.style.cssText = `left:${x}px; top:${y}px; display:block;`; }
        else m.style.display = 'none';
    });
    document.addEventListener('click', () => { m.style.display = 'none'; if($('#smartContextMenu')) $('#smartContextMenu').style.display = 'none'; if($('#collectionContextMenu')) $('#collectionContextMenu').style.display = 'none'; });
    // [BUG-P2-23] 菜单 position:fixed 但此前无 scroll/resize 关闭监听——
    // 滚动列表后菜单悬空漂移到错误位置。捕获相位监听内部容器的滚动。
    const hideCtxMenus = () => { m.style.display = 'none'; const sc2 = $('#smartContextMenu'); if (sc2) sc2.style.display = 'none'; const cc2 = $('#collectionContextMenu'); if (cc2) cc2.style.display = 'none'; };
    window.addEventListener('scroll', hideCtxMenus, true);
    window.addEventListener('resize', hideCtxMenus);
}

/** 灯箱预览 */
function setupLightboxEvents() {
    const lb = $('#cmsLightbox'), img = $('#cmsImg');
    $('#cmsClose').onclick = () => { lb.style.display='none'; STATE.lightbox.active=false; window.stopSlideshow?.(); };
    img.onwheel = (e) => { e.preventDefault(); STATE.lightbox.zoom = Math.max(0.1, Math.min(STATE.lightbox.zoom * (e.deltaY>0?0.9:1.1), 10)); updateTransform(); };
    let isDrag = false, sx=0, sy=0;
    img.onmousedown = (e) => { if(STATE.lightbox.zoom <= 1) return; isDrag = true; sx = e.clientX - STATE.lightbox.transX; sy = e.clientY - STATE.lightbox.transY; img.classList.add('zoomed'); e.preventDefault(); };
    window.onmousemove = (e) => { if(!isDrag) return; STATE.lightbox.transX = e.clientX - sx; STATE.lightbox.transY = e.clientY - sy; updateTransform(); };
    window.onmouseup = () => isDrag = false;
    document.onkeydown = (e) => { if (!STATE.lightbox.active) return; if (e.key === 'ArrowLeft') navigateFile(-1); else if (e.key === 'ArrowRight') navigateFile(1); else if (e.key === 'Escape') { lb.style.display='none'; STATE.lightbox.active=false; window.stopSlideshow?.(); } };
}
function openLightbox(i) { // 打开图片灯箱
    const f = STATE.visibleFiles[i]; if(!f) return; const ext = f.name.split('.').pop().toLowerCase();
    STATE.currentIndex = i; Object.assign(STATE.lightbox, {active:true, zoom:1, transX:0, transY:0}); updateTransform(); $('#cmsLightbox').style.display='flex';
    const textPane = $('#cmsText'), img = $('#cmsImg');
    const TEXT_EXTS = ['txt','log','csv'];
    // [U7/GAP-11] 文本/Markdown 灯箱内联渲染（此前二进制按 img 加载破图）
    if (TEXT_EXTS.includes(ext) || ext === 'md' || ext === 'markdown') {
        img.style.display = 'none'; if (textPane) { textPane.style.display = 'block'; textPane.textContent = '加载中...'; }
        // [U10] 竞态令牌：大文件 A 的响应后到会覆盖已导航到 B 的面板内容
        const seq = (window._lbTextSeq = (window._lbTextSeq || 0) + 1);
        fetch(`/api/file?path=${encodeURIComponent(f.path)}`).then(r => r.text()).then(t => {
            if (seq !== window._lbTextSeq || !textPane || !STATE.lightbox.active) return;
            // [U9] marked 不消毒原始 HTML：预览一个恶意 .md 即可在本页
            // 执行脚本（页面持有 pyBridge 可打全部 /api）。DOMPurify 白名单过滤。
            if ((ext === 'md' || ext === 'markdown') && window.marked) {
                const html = marked.parse(t);
                textPane.innerHTML = window.DOMPurify ? DOMPurify.sanitize(html) : escapeHtml(html);
                textPane.classList.add('is-md');
            }
            else { textPane.textContent = t; textPane.classList.remove('is-md'); }
        }).catch(() => { if (seq === window._lbTextSeq && textPane && STATE.lightbox.active) textPane.textContent = '⚠️ 读取失败'; });
    } else {
        if (textPane) textPane.style.display = 'none';
        img.style.display = '';
        if (PREVIEW_FORMATS.includes(ext)) img.src = `/api/thumb?path=${encodeURIComponent(f.path)}&size=preview`;
        else img.src = `/api/file?path=${encodeURIComponent(f.path)}`;
    }
    // [V4/GAP-20] 幻灯片淡入过渡
    if (STATE.slideshow?.on && !TEXT_EXTS.includes(ext) && ext !== 'md' && ext !== 'markdown') {
        img.style.transition = 'none'; img.style.opacity = '0';
        requestAnimationFrame(() => { img.style.transition = 'opacity .4s'; img.style.opacity = '1'; });
    } else { img.style.transition = ''; img.style.opacity = '1'; }
    $('#cmsTitle').innerText = f.name; $('#cmsInfo').innerText = f.size || '';
    const vInfo = $('#cmsVideoInfo'); if(vInfo) { if(f.type==='video'&&f.res){ vInfo.innerText=`${f.size} · ${f.res}`; vInfo.style.display='block'; }else vInfo.style.display='none'; }
    // [U7/GAP-12] 幻灯片播放中：导航后重置计时（导航按钮/键盘共用本路径）
    if (STATE.slideshow?.on) window.restartSlideshowTimer?.();
}
const updateTransform = () => $('#cmsImg').style.transform = `translate(${STATE.lightbox.transX}px, ${STATE.lightbox.transY}px) scale(${STATE.lightbox.zoom})`;
// [FUNC-17] 灯箱"重置缩放"按钮此前调用的是全库未定义的 resetZoom()（点击报错）
window.resetZoom = () => { STATE.lightbox.zoom = 1; STATE.lightbox.transX = 0; STATE.lightbox.transY = 0; updateTransform(); };
window.navigateFile = (dir) => {
    // [BUG-P1-10] 空列表 % 0 = NaN 崩溃守卫
    const n = STATE.visibleFiles.length; if (!n) return;
    // [BUG-P3-47] 箭头导航跳过灯箱无法呈现的文件（.zip/.docx 等此前按
    // img 加载二进制 → 破图）；视频仍转交播放器
    const TEXT_LB = ['txt', 'log', 'csv', 'md', 'markdown'];
    for (let step = 1; step <= n; step++) {
        const idx = ((STATE.currentIndex + dir * step) % n + n) % n;
        const f = STATE.visibleFiles[idx];
        if (!f) continue;
        if (f.type === 'video') { STATE.lightbox.active = false; $('#cmsLightbox').style.display = 'none'; window.stopSlideshow?.(); window.__FE.openItem(idx); return; }
        const ext = (f.name.split('.').pop() || '').toLowerCase();
        if (f.type === 'image' || PREVIEW_FORMATS.includes(ext) || TEXT_LB.includes(ext)) { openLightbox(idx); return; }
    }
    window.showToast('当前列表没有可预览的文件', 'info');
};

/** 目录树逻辑 */
// [FUNC-16] P2 修复：文件名此前直插 innerHTML（<script> 即注入）；
// data-path 走 escapeHtml 属性转义，选择器侧用 cssPathSel。
// [ARCH-13] 节点点击经 #folderTree 容器委托（onclick 曾随节点复制 N 份）
const createTreeNodeHtml = (name, path, lvl) => { const p = String(path).replace(/\\/g, '/'); return `<div class="tree-node-container" data-path="${escapeHtml(p)}"><div class="tree-header" style="padding-left:${lvl*15+10}px"><span class="tree-toggler me-1"><i class="bi bi-caret-right-fill"></i></span><i class="bi bi-folder-fill text-warning me-2"></i><span>${escapeHtml(name)}</span></div><div class="tree-children"></div></div>`; };
const findInsertPosition = (container, name) => {
    const children = container.querySelectorAll(':scope > .tree-node-container'), newName = name.toLowerCase();
    for (const child of children) { if (newName < child.querySelector('.tree-header span:not(.tree-toggler)').innerText.toLowerCase()) return child; } return null;
};
const initFolderTree = async () => { // 初始化侧边栏目录树
    const container = $('#folderTree'); if (!container) return;
    const expandedPaths = Array.from($$('#folderTree .tree-toggler.expanded')).map(t => t.closest('.tree-node-container').dataset.path);
    let html = createTreeNodeHtml(STATE.rootPath.split(/[/\\]/).pop() || "Home", STATE.rootPath, 0);
    try {
        const res = await fetch('/api/drives'), drives = await res.json();
        drives.forEach(d => { const normDrive = d.path.replace(/\\/g,'/').replace(/\/$/, ''), normRoot = STATE.rootPath.replace(/\\/g,'/').replace(/\/$/, ''); if (normDrive.toLowerCase() !== normRoot.toLowerCase()) html += createTreeNodeHtml(d.name, d.path, 0); });
    } catch(e) { console.error("Load drives error:", e); }
    container.innerHTML = html;
    for (const path of expandedPaths) { const node = container.querySelector(`.tree-node-container[data-path="${cssPathSel(path)}"]`); if (node) await expandNode(node); }
};
// [BUG-P2-24] 展开并发防护：双击 toggler 会触发两个 /api/tree 竞争写同一
// innerHTML（偶发空展开）；失败也不再完全静默。
const _expandingNodes = new WeakSet();
async function expandNode(node) { // 展开目录树节点
    if (_expandingNodes.has(node)) return;
    const t = node.querySelector('.tree-toggler'), c = node.querySelector('.tree-children');
    if(t.classList.contains('expanded')) {
        _expandingNodes.add(node);
        try {
            const res = await fetch(`/api/tree?path=${encodeURIComponent(node.dataset.path)}`), data = await res.json(), pd = parseInt(node.querySelector('.tree-header').style.paddingLeft||10);
            const expandedSubPaths = Array.from(c.querySelectorAll('.tree-toggler.expanded')).map(st => st.closest('.tree-node-container').dataset.path);
            c.innerHTML = data.map(f => createTreeNodeHtml(f.name, f.path, (pd-10)/15+1)).join('');
            for (const subPath of expandedSubPaths) { const subNode = c.querySelector(`.tree-node-container[data-path="${cssPathSel(subPath)}"]`); if (subNode) await expandNode(subNode); }
        } catch(e){ console.error("Tree refresh error:", e); }
        finally { _expandingNodes.delete(node); }
        return;
    }
    t.classList.add('expanded'); c.classList.add('show');
    _expandingNodes.add(node);
    try {
        const res = await fetch(`/api/tree?path=${encodeURIComponent(node.dataset.path)}`), data = await res.json(), pd = parseInt(node.querySelector('.tree-header').style.paddingLeft||10);
        c.innerHTML = data.map(f => createTreeNodeHtml(f.name, f.path, (pd-10)/15+1)).join('');
    } catch(e){ console.error("Tree expand error:", e); }
    finally { _expandingNodes.delete(node); }
}
window.onTreeClick = (e, header) => {
    e.stopPropagation(); const node = header.parentNode, path = node.dataset.path, isToggler = e.target.closest('.tree-toggler');
    if (isToggler) { const t = node.querySelector('.tree-toggler'), c = node.querySelector('.tree-children'); if(t.classList.contains('expanded')) { t.classList.remove('expanded'); c.classList.remove('show'); } else expandNode(node); } 
    else { $$('.tree-header').forEach(h => h.classList.remove('active')); header.classList.add('active'); if (STATE.currentPath !== path) window.__FE.loadPath(path, false, true); const t = node.querySelector('.tree-toggler'); if(!t.classList.contains('expanded')) expandNode(node); }
};
async function revealTreePath(target) { // 在目录树中定位并展开当前路径
    // [BUG-P2-27] 盘符/卷名大小写不一致时此前静默不定位——统一小写比较
    const targetNorm = target.replace(/\\/g, '/').toLowerCase();
    let rootNode = null; const roots = $$('#folderTree > .tree-node-container');
    for (const r of roots) { const rootPath = r.dataset.path.replace(/\\/g, '/'); const lp = rootPath.toLowerCase(); if (targetNorm === lp || targetNorm.startsWith(lp + '/')) { rootNode = r; break; } }
    if (!rootNode) return;
    let currentPath = rootNode.dataset.path, rel = target.slice(currentPath.length).replace(/^\//, ''), parts = rel ? rel.split('/') : [], currentNode = rootNode;
    await expandNode(currentNode);
    for (const p of parts) { if (!currentNode) break; currentPath += (currentPath.endsWith('/') ? '' : '/') + p; const nextNode = currentNode.querySelector(`.tree-node-container[data-path="${cssPathSel(currentPath)}"]`); if (nextNode) { currentNode = nextNode; await expandNode(currentNode); } else break; }
    $$('.tree-header').forEach(h => h.classList.remove('active')); const header = currentNode.querySelector('.tree-header'); if (header) { header.classList.add('active'); header.scrollIntoView({behavior: 'smooth', block: 'center'}); }
}

/** 文件操作 */
// [UX-5] 统一对话框：替代原生 prompt/confirm（QWebEngine 下阻塞弹窗体验差）
const ensureDialogHost = () => {
    let host = $('#fxDialogHost');
    if (!host) {
        host = document.createElement('div'); host.id = 'fxDialogHost';
        host.innerHTML = `
        <div class="modal fade" id="fxInputDialog" tabindex="-1">
          <div class="modal-dialog modal-sm modal-dialog-centered">
            <div class="modal-content bg-dark text-white">
              <div class="modal-header py-2"><h6 class="modal-title" id="fxInputDialogTitle">输入</h6></div>
              <div class="modal-body py-2"><input type="text" class="form-control form-control-sm bg-dark text-white border-secondary" id="fxInputDialogInput" autocomplete="off"></div>
              <div class="modal-footer py-2">
                <button type="button" class="btn btn-sm btn-secondary" data-bs-dismiss="modal">取消</button>
                <button type="button" class="btn btn-sm btn-primary" id="fxInputDialogOk">确定</button>
              </div>
            </div>
          </div>
        </div>
        <div class="modal fade" id="fxConfirmDialog" tabindex="-1">
          <div class="modal-dialog modal-sm modal-dialog-centered">
            <div class="modal-content bg-dark text-white">
              <div class="modal-body py-3" id="fxConfirmDialogMsg" style="white-space:pre-wrap;">确认？</div>
              <div class="modal-footer py-2">
                <button type="button" class="btn btn-sm btn-secondary" data-bs-dismiss="modal">取消</button>
                <button type="button" class="btn btn-sm btn-danger" id="fxConfirmDialogOk">确定</button>
              </div>
            </div>
          </div>
        </div>`;
        document.body.appendChild(host);
    }
    return host;
};
window.showInputDialog = (title, defaultValue = '') => new Promise(resolve => {
    ensureDialogHost();
    const el = $('#fxInputDialog'), inp = $('#fxInputDialogInput'), okBtn = $('#fxInputDialogOk');
    $('#fxInputDialogTitle').innerText = title; inp.value = defaultValue;
    const modal = bootstrap.Modal.getOrCreateInstance(el);
    let settled = false;
    const done = (val) => { if (settled) return; settled = true; modal.hide(); resolve(val); };
    okBtn.onclick = () => { const v = inp.value.trim(); done(v || null); };
    inp.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); okBtn.click(); } };
    el.addEventListener('hidden.bs.modal', () => { if (!settled) { settled = true; resolve(null); } }, { once: true });
    modal.show();
    setTimeout(() => { inp.focus(); inp.select(); }, 150);
});
window.showConfirmDialog = (message, okText = '确定') => new Promise(resolve => {
    ensureDialogHost();
    const el = $('#fxConfirmDialog'), msg = $('#fxConfirmDialogMsg'), okBtn = $('#fxConfirmDialogOk');
    msg.textContent = message; okBtn.textContent = okText;
    const modal = bootstrap.Modal.getOrCreateInstance(el);
    let settled = false;
    const done = (val) => { if (settled) return; settled = true; modal.hide(); resolve(val); };
    okBtn.onclick = () => done(true);
    el.addEventListener('hidden.bs.modal', () => { if (!settled) { settled = true; resolve(false); } }, { once: true });
    modal.show();
});
window.updateFileRating = (path, rating) => {
    const target = path.replace(/\\/g,'/').toLowerCase();
    const hit = f => f.path.replace(/\\/g,'/').toLowerCase() === target;
    const all = STATE.allFiles.find(hit); if (all) all.rating = rating;
    const vis = STATE.visibleFiles.find(hit); if (vis) vis.rating = rating;
    // [UX-7] 同步卡片星级角标
    if (vis) {
        const idx = STATE.visibleFiles.indexOf(vis);
        // [BUG-P2-26] 音频卡（audio-card-wrapper）与图标卡（non-img-wrapper）
        // 没有 .file-thumb-wrapper，此前角标永远不出现。逐级兜底；
        // non-img-wrapper 补内联定位（.card-badges 是 absolute）。
        let host = $(`#card-${idx} .file-thumb-wrapper`)
                || $(`#card-${idx} .audio-card-wrapper`)
                || $(`#card-${idx} .non-img-wrapper`);
        if (host && host.classList.contains('non-img-wrapper')) host.style.position = 'relative';
        if (host) { const old = host.querySelector('.card-badges'); if (old) old.remove(); host.insertAdjacentHTML('beforeend', cardBadgesHtml(vis)); }
    }
    if (STATE.lastRenderedPath && STATE.lastRenderedPath.replace(/\\/g,'/').toLowerCase() === target) {
        document.querySelectorAll('#propertiesPanel .rating-stars .bi-star-fill').forEach((star, index) => star.classList.toggle('active', index < rating));
    }
};

/** 属性面板逻辑 */
window.togglePropertiesPanel = (show) => { // 切换侧边属性面板
    const panel = $('#propertiesPanel'); if (!panel) return;
    const shouldShow = show !== undefined ? show : !panel.classList.contains('active');
    if (shouldShow) { panel.classList.add('active'); STATE.lastRenderedPath = null; if (STATE.selected.size === 1) { const path = STATE.selected.values().next().value, file = STATE.visibleFiles.find(f => f.path === path); if(file) { STATE.pluginPanelActive = false; showFileProperties(file); } } else if (STATE.selected.size > 1) showBatchProperties(); } 
    else { panel.classList.remove('active'); STATE.pluginPanelActive = false; STATE.lastRenderedPath = null; }
};
window.renderProperties = (title, html) => { STATE.pluginPanelActive = true; const panel = $('#propertiesPanel'); if(panel) { panel.querySelector('.panel-title').innerText = title; $('#propContent').innerHTML = html; panel.classList.add('active'); } };
function showFileProperties(file) { // 显示单个文件的详细属性
    const p = $('#propertiesPanel'); if (!p?.classList.contains('active') || STATE.pluginPanelActive || STATE.lastRenderedPath === file.path) return;
    STATE.lastRenderedPath = file.path; p.querySelector('.panel-title').innerText = '属性';
    let sH = '<div class="rating-stars">'; for (let i=1; i<=5; i++) sH += `<i class="bi bi-star-fill ${i<=(file.rating||0)?'active':''}" data-rating="${i}"></i>`; sH += '</div>';
    // [U5/GAP-03] 颜色标记 + 旗标（四处联动之二：属性面板；点击经 propPanel 委托）
    const curColor = file.color || 0;
    let cfH = '<div class="color-row">';
    const colorNames = ['红','黄','绿','蓝','紫'];
    for (let i=0; i<=5; i++) cfH += `<span class="color-swatch c${i} ${curColor===i?'active':''}" data-color="${i}" data-path="${escapeHtml(file.path)}" title="${i===0?'无标记':colorNames[i-1]}"></span>`;
    cfH += `<span class="flag-btn ${(file.flag||0)===1?'active':''}" data-flag="1" data-path="${escapeHtml(file.path)}" title="旗标（选用）"><i class="bi bi-flag-fill"></i>旗标</span>`;
    cfH += `<span class="flag-btn flag-reject ${(file.flag||0)===2?'active':''}" data-flag="2" data-path="${escapeHtml(file.path)}" title="拒收"><i class="bi bi-x-circle"></i>拒收</span>`;
    cfH += '</div>';
    // [U5/GAP-01] 单文件标签编辑器：chips（✕ 移除）+ 输入框（datalist 补全）
    const tagNames = (file.tags || '').split(',').map(s => s.trim()).filter(Boolean);
    let tH = '<div class="tag-chips" id="prop-tag-chips">';
    for (const t of tagNames) tH += `<span class="tag-chip">${escapeHtml(t)}<i class="chip-x" data-tag="${escapeHtml(t)}" data-path="${escapeHtml(file.path)}" title="移除标签">✕</i></span>`;
    tH += '</div>';
    tH += `<input type="text" class="form-control form-control-sm bg-dark text-white border-secondary" id="prop-tag-input" list="tag-suggestions" placeholder="添加标签，回车确认" autocomplete="off">`;
    tH += `<datalist id="tag-suggestions">${(STATE.allTags || []).map(t => `<option value="${escapeHtml(t.name)}">`).join('')}</datalist>`;
    // [FUNC-16] 路径此前拼进 onclick JS 字符串（仅转义单引号）→ data-path 属性
    let aH = `<div class="actions-area"><div class="action-btn" data-action="openSystem" data-path="${escapeHtml(file.path)}"><i class="bi bi-folder2-open"></i> 打开目录</div>${file.type === 'video' ? `<div class="action-btn" data-action="triggerPlayerSnapshot"><i class="bi bi-camera"></i> 截取封面</div>` : ''}</div>`;
    // [V4/GAP-16] 有 GPS 的图片：在地图中查看 / [V4/GAP-17] 图内文字
    const gpsLink = (file.gps_lat != null && file.gps_lon != null)
        ? `<div class="prop-group mb-2"><a href="https://maps.apple.com/?ll=${file.gps_lat},${file.gps_lon}" target="_blank" class="small text-info"><i class="bi bi-geo-alt me-1"></i>${file.gps_lat.toFixed(5)}, ${file.gps_lon.toFixed(5)} — 在地图中查看</a></div>` : '';
    const ocrSection = (file.type === 'image')
        ? `<div class="prop-group mb-3"><label class="text-white-50 small mb-1">图内文字（OCR）[V4]</label><div id="prop-ocr"><button class="btn btn-sm btn-outline-secondary border-secondary w-100" data-action="runOcrPanel" data-stop data-path="${escapeHtml(file.path)}"><i class="bi bi-textarea-t me-1"></i>识别图内文字</button></div></div>` : '';
    $('#propContent').innerHTML = `<div class="prop-group mb-3 text-center">${sH}</div><div class="prop-group mb-2"><label class="text-white-50 small mb-1">颜色 / 旗标</label>${cfH}</div><div class="prop-group mb-3"><label class="text-white-50 small mb-1">标签</label>${tH}</div><div class="prop-group mb-3"><label class="text-white-50 small mb-1">文件名</label><input type="text" class="form-control form-control-sm bg-dark text-white border-secondary" value="${escapeHtml(file.name)}" id="prop-filename" autocomplete="off"></div><div class="prop-group mb-3"><label class="text-white-50 small mb-1">备注</label><textarea class="form-control form-control-sm bg-dark text-white border-secondary" id="prop-comments" rows="3">${escapeHtml(file.comments||'')}</textarea></div><table class="prop-table"><tr><td>类型</td><td>${escapeHtml(file.type==='dir'?'文件夹':file.type)}</td></tr><tr><td>大小</td><td>${escapeHtml(file.size||'-')}</td></tr><tr><td>修改</td><td>${escapeHtml(file.mtime||'-')}</td></tr><tr><td>分辨</td><td>${escapeHtml(file.res||'-')}</td></tr></table>${gpsLink}${ocrSection}${file.type === 'video' ? '<div class="prop-group mb-3"><label class="text-white-50 small mb-1">胶片条</label><div id="prop-filmstrip" class="filmstrip"></div></div>' : ''}<div id="deep-info-area"><div class="text-center text-muted small my-2"><div class="spinner-border spinner-border-sm"></div></div></div>${aH}`;
    window.loadDeepInfo(file.path); const nInp = $('#prop-filename'), cInp = $('#prop-comments');
    nInp.onkeydown = (e) => { if(e.key==='Enter') nInp.blur(); }; nInp.onblur = () => { const n = nInp.value.trim(); if(n && n!==file.name) window.doRename(file.path, n); else nInp.value=file.name; };
    cInp.onblur = () => { const c = cInp.value.trim(); if(c!==(file.comments||'')) window.doUpdateMeta(file.path, c); };
    // [U5/GAP-01] 标签输入回车添加
    const tInp = $('#prop-tag-input');
    tInp.onkeydown = (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            const v = tInp.value.trim();
            if (v) { window.addFileTag(file.path, v); tInp.value = ''; }
        }
    };
    // [U7/GAP-10] 属性面板胶片条（视频）：悬停逐帧预览
    if (file.type === 'video') {
        const fsArea = $('#prop-filmstrip');
        if (fsArea) {
            fsArea.dataset.forPath = file.path;
            fsArea.innerHTML = '<div class="text-muted small">生成帧条中（首次较慢）...</div>';
            window.ensureVideoStrip(file.path).then(entry => {
                if (!entry || entry === 'failed') { const b = $('#prop-filmstrip'); if (b) b.innerHTML = '<div class="text-muted small">胶片条不可用</div>'; return; }
                const b = $('#prop-filmstrip');
                if (!b || b.dataset.forPath !== file.path) return;
                b.innerHTML = `<div class="strip-bar" style="background-image:url('/api/thumb/strip?path=${encodeURIComponent(file.path).replace(/'/g, '%27')}')"></div><div class="strip-preview" style="background-image:url('/api/thumb/strip?path=${encodeURIComponent(file.path).replace(/'/g, '%27')}')"></div>`;
                const bar = b.querySelector('.strip-bar'), prev = b.querySelector('.strip-preview');
                bar.onmousemove = (e) => {
                    const r = bar.getBoundingClientRect();
                    const ratio = Math.max(0, Math.min(0.999, (e.clientX - r.left) / r.width));
                    prev.style.backgroundPosition = `${Math.floor(ratio * 6) / 5 * 100}% 50%`;
                };
                prev.style.backgroundPosition = '0% 50%';
            });
        }
    }
}

// [U7/GAP-13] 可点击面包屑：真实目录时替换路径输入框；分段点击跳转、
// 箭头列出同级目录；虚拟视图自动回退输入框
// [V5 复查] 补 collection:// 与 similar://：此前集合/相似视图被当成真实
// 路径渲染出伪 crumb，还亮出复制按钮（复制出 collection://3 这类串）
window.renderBreadcrumb = (path) => {
    const bar = $('#breadcrumbBar'), inp = $('#pathDisplay'), copyBtn = $('#btnCopyPath');
    if (!bar) return;
    const isReal = path && !['smart://', 'search://', 'trash://', 'collection://', 'similar://'].some(v => path.startsWith(v));
    if (!isReal) {
        bar.style.display = 'none'; if (copyBtn) copyBtn.style.display = 'none';
        if (inp) inp.style.display = '';
        window._hideCrumbMenu?.();
        return;
    }
    if (inp) inp.style.display = 'none';
    if (copyBtn) copyBtn.style.display = '';
    bar.style.display = 'flex';
    window._hideCrumbMenu?.();
    const parts = path.split('/').filter(Boolean);
    let acc = '';
    const segs = [];
    if (path.startsWith('/')) segs.push({ name: '<i class="bi bi-disc" title="根目录"></i>', p: '/', html: true });
    for (const seg of parts) {
        acc = acc ? acc.replace(/\/$/, '') + '/' + seg : (path.startsWith('/') ? '/' + seg : seg + '/');
        if (/^[A-Za-z]:$/.test(acc)) acc += '/';
        segs.push({ name: escapeHtml(seg), p: acc });
    }
    bar.innerHTML = segs.map((s, i) => {
        const last = i === segs.length - 1;
        const segHtml = s.html
            ? `<span class="crumb-seg" data-crumb-path="${escapeHtml(s.p)}">${s.name}</span>`
            : (last ? `<span class="crumb-seg current" title="${s.name}">${s.name}</span>`
                    : `<span class="crumb-seg" data-crumb-path="${escapeHtml(s.p)}" title="${s.name}">${s.name}</span><i class="bi bi-caret-down-fill crumb-arrow" data-crumb-parent="${escapeHtml(s.p)}" title="同级目录"></i>`);
        return `<span class="crumb-wrap">${segHtml}</span>${last ? '' : '<span class="crumb-sep">›</span>'}`;
    }).join('');
};
// [BUG-P1-09] 竞态令牌：快速导航时旧文件的 EXIF/ffprobe 响应后到会覆盖
// 新面板内容；令牌丢弃过期响应
let _deepInfoSeq = 0;
window.loadDeepInfo = async (path) => { // 异步加载媒体文件深度元数据
    const area = $('#deep-info-area'); if(!area) return;
    const seq = ++_deepInfoSeq;
    area.innerHTML = '<div class="text-center text-muted small my-2"><div class="spinner-border spinner-border-sm"></div></div>'; area.style.display = 'block';
    try {
        const res = await fetch(`/api/metadata/detail?path=${encodeURIComponent(path)}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (seq !== _deepInfoSeq) return; // 已切换到别的文件
        if (Object.keys(data).length === 0) { area.style.display = 'none'; return; }
        let html = ''; const isVideo = 'Codec' in data || 'Bit Depth' in data || 'Frame Rate' in data, isPhoto = 'ISO' in data || 'Aperture' in data || 'Shutter' in data;
        // [FUNC-16] 深度元数据取自文件本身（EXIF/ffprobe），值全部转义：
        // 恶意构造的元数据（如 EXIF Artist="<img onerror=...>"）此前可直接注入
        if (isVideo) { html += '<div class="meta-grid">'; [{l:'Codec',k:'Codec',h:1},{l:'Bit Depth',k:'Bit Depth'},{l:'Color',k:'Color Info'},{l:'Bitrate',k:'Bitrate'},{l:'Resolution',k:'Resolution'},{l:'Frame Rate',k:'Frame Rate'}].forEach(b=>{ if(data[b.k]) html+=`<div class="meta-badge ${b.h?'highlight':''}"><div class="meta-label">${escapeHtml(b.l)}</div><div class="meta-value" title="${escapeHtml(String(data[b.k]))}">${escapeHtml(String(data[b.k]))}</div></div>`; }); html += '</div>'; }
        else if (isPhoto) { if(data['Model']||data['Lens']) html+=`<div class="text-center small text-muted mb-2">${escapeHtml(String(data['Model']||''))} ${data['Lens']?' · '+escapeHtml(String(data['Lens'])):''}</div>`; html+='<div class="camera-info-row">'; if(data['Aperture']) html+=`<div class="camera-info-item"><i class="bi bi-aperture"></i> ${escapeHtml(String(data['Aperture']))}</div>`; if(data['Shutter']) html+=`<div class="camera-info-item"><i class="bi bi-speedometer2"></i> ${escapeHtml(String(data['Shutter']))}</div>`; if(data['ISO']) html+=`<div class="camera-info-item"><i class="bi bi-sun"></i> ${escapeHtml(String(data['ISO']))}</div>`; html+='</div>'; if(data['Resolution']||data['Date']){ html+='<div class="d-flex justify-content-between text-muted small px-1">'; if(data['Resolution']) html+=`<span>${escapeHtml(String(data['Resolution']))}</span>`; if(data['Date']) html+=`<span>${escapeHtml(String(data['Date']).split(' ')[0])}</span>`; html+='</div>'; } }
        else { html += '<div class="deep-info-container">'; for (const [k, v] of Object.entries(data)) html += `<div class="deep-info-item"><span class="deep-info-key">${escapeHtml(String(k))}</span><span class="deep-info-val">${escapeHtml(String(v))}</span></div>`; html += '</div>'; }
        if (seq !== _deepInfoSeq) return; // 渲染前再校验一次（长任务后到）
        area.innerHTML = html; area.style.display = 'block';
    } catch(e) { console.error("Deep info load failed:", e); if (seq === _deepInfoSeq) area.style.display = 'none'; }
};
window.showBatchProperties = () => { // 显示批量操作面板
    const panel = document.getElementById('propertiesPanel');
    // [BUG-P2-22] 右键"批量操作..."在面板关闭时点了无反应——先展开面板
    if (!panel?.classList.contains('active')) panel.classList.add('active');
    STATE.pluginPanelActive = true; STATE.lastRenderedPath = 'BATCH';
    const t = panel.querySelector('.panel-title'); if (t) t.innerText = `已选中 ${STATE.selected.size} 个文件`;
    $('#propContent').innerHTML = `
        <div class="batch-tabs"><div class="batch-tab-item active" data-action="switchBatchTab" data-args='["rename","$el"]'>批量重命名</div><div class="batch-tab-item" data-action="switchBatchTab" data-args='["metadata","$el"]'>批量元数据</div></div>
        <div id="batch-tab-rename" class="batch-tab-content active">
            <div class="prop-group mb-3"><label class="text-white-50 small mb-1">模式</label><select class="form-select form-select-sm bg-dark text-white border-secondary" id="renameModeSelect" data-change-action="updateRenameMode"><option value="replace">替换文本</option><option value="append">添加序号</option><option value="template">模板 [V3]</option></select></div>
            <div id="renameInputArea"></div><div class="prop-group mb-3"><label class="text-white-50 small mb-1">预览</label><div id="renamePreview" class="custom-scrollbar"><div class="text-muted text-center py-2">请输入内容以预览</div></div></div>
            <button class="btn btn-primary btn-sm w-100" data-action="applyBatchRename"><i class="bi bi-check2-circle"></i> 应用重命名</button>
        </div>
        <div id="batch-tab-metadata" class="batch-tab-content">
            <div class="prop-group mb-3 text-center"><label class="text-white-50 small mb-2 d-block">批量评分</label><div class="rating-stars" id="batchRatingStars">${[1,2,3,4,5].map(i => `<i class="bi bi-star-fill" data-action="setBatchRating" data-args="[${i}]"></i>`).join('')}</div></div>
            <div class="prop-group mb-3"><label class="text-white-50 small mb-1">添加标签 (逗号分隔)</label><input type="text" class="form-control form-control-sm bg-dark text-white border-secondary" placeholder="例如: holiday, beach..."></div>
            <div class="prop-group mb-3"><label class="text-white-50 small mb-1">颜色标记 [U5]</label><div id="batchColorRow">${[0,1,2,3,4,5].map(i => `<span class="color-swatch c${i}" data-action="setBatchColor" data-args='[${i}]' title="${i===0?'无标记':['红','黄','绿','蓝','紫'][i-1]}"></span>`).join('')}</div></div>
            <div class="prop-group mb-3"><label class="text-white-50 small mb-1">旗标 [U5]</label><div><span class="flag-btn" data-action="setBatchFlag" data-args='[1]'><i class="bi bi-flag-fill"></i>旗标</span><span class="flag-btn flag-reject" data-action="setBatchFlag" data-args='[2]'><i class="bi bi-x-circle"></i>拒收</span><span class="flag-btn" data-action="setBatchFlag" data-args='[0]'>清除</span></div></div>
            <button class="btn btn-primary btn-sm w-100" data-action="applyBatchMetadata"><i class="bi bi-tags-fill"></i> 应用元数据</button>
        </div>`;
    panel.classList.add('active'); updateRenameMode(); window.BATCH_RATING = 0;
};
window.switchBatchTab = (tabName, el) => { $$('.batch-tab-item, .batch-tab-content').forEach(i => i.classList.remove('active')); el.classList.add('active'); $(`#batch-tab-${tabName}`).classList.add('active'); };
window.updateRenameMode = () => {
    const mode = $('#renameModeSelect').value, area = $('#renameInputArea');
    if (mode === 'replace') area.innerHTML = `<div class="prop-group mb-2"><input type="text" class="form-control form-control-sm bg-dark text-white border-secondary mb-2" id="renameFind" placeholder="查找..." data-input-action="updateRenamePreview"><input type="text" class="form-control form-control-sm bg-dark text-white border-secondary" id="renameReplace" placeholder="替换为..." data-input-action="updateRenamePreview"></div>`;
    else if (mode === 'template') {
        // [V3/GAP-10] 模板模式：变量插入按钮条 + 起始序号
        const vars = ['{name}', '{seq:3}', '{date:taken:%Y%m%d}', '{camera}', '{iso}', '{w}x{h}', '{parent}'];
        area.innerHTML = `<div class="prop-group mb-2">
            <input type="text" class="form-control form-control-sm bg-dark text-white border-secondary mb-2" id="renameTemplate" placeholder="{date:taken:%Y%m%d}_{seq:3}" data-input-action="updateRenamePreview">
            <div class="d-flex flex-wrap gap-1 mb-2">${vars.map(v => `<span class="badge bg-secondary" style="cursor:pointer;" data-action="insertRenameVar" data-stop data-args='["${v}"]'>${escapeHtml(v)}</span>`).join('')}</div>
            <div class="d-flex gap-2"><input type="number" class="form-control form-control-sm bg-dark text-white border-secondary" id="renameStart" value="1" min="1" placeholder="起始序号" data-input-action="updateRenamePreview"></div>
        </div>`;
    }
    else area.innerHTML = `<div class="prop-group mb-2"><input type="text" class="form-control form-control-sm bg-dark text-white border-secondary mb-2" id="renamePrefix" placeholder="新文件名前缀 (可选)" data-input-action="updateRenamePreview"><div class="d-flex gap-2"><input type="number" class="form-control form-control-sm bg-dark text-white border-secondary" id="renameStart" value="1" min="1" placeholder="起始" data-input-action="updateRenamePreview"><input type="number" class="form-control form-control-sm bg-dark text-white border-secondary" id="renameDigits" value="2" min="1" max="5" placeholder="位数" data-input-action="updateRenamePreview"></div></div>`;
    updateRenamePreview();
};
window.insertRenameVar = (v) => { const inp = $('#renameTemplate'); if (!inp) return; const pos = inp.selectionStart ?? inp.value.length; inp.value = inp.value.slice(0, pos) + v + inp.value.slice(pos); inp.dispatchEvent(new Event('input')); };
window.updateRenamePreview = () => { // 批量重命名实时预览
    const container = $('#renamePreview'), mode = $('#renameModeSelect').value, selectedFiles = Array.from(STATE.selected).slice(0, 10); if (selectedFiles.length === 0) return;
    // [V3/GAP-10] 模板模式预览走后端 dry-run（需 DB 技术列渲染 {date:...} 等）
    if (mode === 'template') {
        const tpl = $('#renameTemplate')?.value || '';
        if (!tpl) { container.innerHTML = '<div class="text-muted text-center py-2 small">输入模板以预览</div>'; return; }
        const seq = (window._renamePrevSeq = (window._renamePrevSeq || 0) + 1);
        fetch('/api/batch/rename/preview', { method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ files: Array.from(STATE.selected), mode: 'template', params: { template: tpl, start: parseInt($('#renameStart')?.value || 1) } }) })
            .then(r => r.json()).then(data => {
                if (seq !== window._renamePrevSeq || !container.isConnected) return;
                if (!data.success) { container.innerHTML = `<div class="text-danger small py-2">${escapeHtml((data.errors || [])[0] || '预览失败')}</div>`; return; }
                const items = (data.plan || []).slice(0, 10);
                let html = items.map(it => `<div class="preview-item"><span class="preview-old text-truncate" style="max-width: 45%;" title="${escapeHtml(it.src_name)}">${escapeHtml(it.src_name)}</span><span class="preview-arrow"><i class="bi bi-arrow-right"></i></span><span class="preview-new text-truncate ${it.conflict ? 'text-warning' : ''}" style="max-width: 45%;" title="${escapeHtml(it.dst_name)}${it.conflict ? '（冲突将自动加后缀）' : ''}">${escapeHtml(it.dst_name)}${it.conflict ? ' ⚠' : ''}</span></div>`).join('');
                if (!items.length) html = '<div class="text-muted text-center py-2 small">无变化</div>';
                container.innerHTML = html;
            }).catch(() => { if (seq === window._renamePrevSeq && container.isConnected) container.innerHTML = '<div class="text-danger small py-2">预览失败</div>'; });
        return;
    }
    let html = '';
    selectedFiles.forEach((path, idx) => {
        const oldName = path.split(/[/\\]/).pop(); let newName = oldName; const ext = oldName.includes('.') ? '.' + oldName.split('.').pop() : '';
        if (mode === 'replace') { const find = $('#renameFind')?.value, rep = $('#renameReplace')?.value || ''; if (find) newName = oldName.replaceAll(find, rep); } 
        else { const prefix = $('#renamePrefix')?.value || 'File', start = parseInt($('#renameStart')?.value || 1), digits = parseInt($('#renameDigits')?.value || 2), num = (start + idx).toString().padStart(digits, '0'); newName = `${prefix}_${num}${ext}`; }
        if (newName !== oldName) html += `<div class="preview-item"><span class="preview-old text-truncate" style="max-width: 45%;" title="${escapeHtml(oldName)}">${escapeHtml(oldName)}</span><span class="preview-arrow"><i class="bi bi-arrow-right"></i></span><span class="preview-new text-truncate" style="max-width: 45%;" title="${escapeHtml(newName)}">${escapeHtml(newName)}</span></div>`;
    });
    container.innerHTML = html || '<div class="text-muted text-center py-2 small">无变化</div>';
    if(html && STATE.selected.size > 10) container.innerHTML += `<div class="text-center text-muted small mt-2">... 以及其他 ${STATE.selected.size - 10} 个文件</div>`;
};
window.BATCH_RATING = 0;
window.setBatchRating = (r) => { window.BATCH_RATING = r; $$('#batchRatingStars .bi-star-fill').forEach((el, i) => el.classList.toggle('active', i < r)); };
// [U5/GAP-03] 批量颜色/旗标选择（null=不修改；应用后由 actions 复位）
window.setBatchColor = (c) => { window.BATCH_COLOR = c; $$('#batchColorRow .color-swatch').forEach(el => { const v = parseInt((el.dataset.args || '').replace(/[^0-9]/g, '')) || 0; el.classList.toggle('active', v === c); }); };
window.setBatchFlag = (f) => { window.BATCH_FLAG = f; $$('#batch-tab-metadata .flag-btn').forEach(el => { const m = /data-args='\[(\d)\]'/.exec(el.outerHTML); el.classList.toggle('active', m && parseInt(m[1]) === f); }); };

const FE = window.__FE;
Object.assign(FE, {
    cardBadgesHtml, createCard, renderNextBatch,
    unobserveThumbImgs,
    fmtDuration, tlKeyOf,
    applyFilterAndSort: window.applyFilterAndSort,
    updateFilterButtonState, updateSortMenuState, updateFilterMenuState,
    updateTimeBadge, updateThumbProgress: window.updateThumbProgress,
    onThumbLoadComplete: window.onThumbLoadComplete,
    loadAssociatedApps, setupContextMenu, setupLightboxEvents,
    openLightbox, updateTransform, navigateFile: window.navigateFile, resetZoom: window.resetZoom,
    createTreeNodeHtml, findInsertPosition, initFolderTree, expandNode,
    onTreeClick: window.onTreeClick, revealTreePath,
    ensureDialogHost, showInputDialog: window.showInputDialog, showConfirmDialog: window.showConfirmDialog,
    togglePropertiesPanel: window.togglePropertiesPanel, renderProperties: window.renderProperties,
    showFileProperties, loadDeepInfo: window.loadDeepInfo,
    showBatchProperties: window.showBatchProperties, switchBatchTab: window.switchBatchTab,
    updateRenameMode: window.updateRenameMode, updateRenamePreview: window.updateRenamePreview,
    setBatchRating: window.setBatchRating, updateFileRating: window.updateFileRating,
    updateNavButtons, updateToolbar,
    // [BUG-P0-03] updateSelectionUI 漏出清单：fe-actions 解构得到 undefined，
    // 重命名成功分支（pendingSelection）必抛 TypeError 且被 catch 吞掉
    updateSelectionUI,
});
})();
