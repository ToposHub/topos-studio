window.CodeEditorPlugin = {
    tabs: [], // Array of { path: string, model: monacoModel, viewState: object }
    activePath: null,
    editor: null,

    // --- 1. UI Initialization with Menu Bar & Tabs ---
    initUI: function() {
        if (document.getElementById('code-editor-modal')) return;

        // 1. Inject CSS for Tabs and Prompts
        const style = document.createElement('style');
        style.innerHTML = `
            .ce-tab-bar { display: flex; background: #252526; overflow-x: auto; border-bottom: 1px solid #1e1e1e; }
            .ce-tab { 
                display: flex; align-items: center; padding: 8px 12px; background: #2d2d2d; color: #969696; 
                border-right: 1px solid #1e1e1e; cursor: pointer; font-size: 13px; user-select: none; min-width: 120px; max-width: 200px;
                transition: background 0.2s, color 0.2s;
            }
            .ce-tab:hover { background: #2a2d2e; color: #ccc; }
            .ce-tab.active { background: #1e1e1e; color: #fff; border-top: 1px solid #007acc; }
            .ce-tab-close { margin-left: auto; padding: 2px; border-radius: 3px; font-size: 14px; opacity: 0.5; }
            .ce-tab-close:hover { background: #4b4b4b; color: white; opacity: 1; }
            .ce-tab-unsaved { width:8px; height:8px; background:#fff; border-radius:50%; margin-left:8px; display:none; }
            
            .ce-tab-bar::-webkit-scrollbar { height: 3px; }
            .ce-tab-bar::-webkit-scrollbar-thumb { background: #444; }

            /* Custom Prompt Styling */
            #ce-prompt-input:focus { border-color: #007acc !important; box-shadow: 0 0 0 2px rgba(0, 122, 204, 0.25) !important; }
        `;
        document.head.appendChild(style);

        // Standard Desktop App Menu Structure (Minimalist style)
        const menuBarHtml = `
        <div class="d-flex gap-1">
            <div class="dropdown">
                <button class="btn btn-sm btn-link text-decoration-none dropdown-toggle py-0" data-bs-toggle="dropdown" style="color: #aaa; font-size: 13px;">文件 (File)</button>
                <ul class="dropdown-menu dropdown-menu-dark shadow" style="font-size:13px;">
                    <li><a class="dropdown-item" href="javascript:void(0)" onclick="CodeEditorPlugin.newFile()"><i class="bi bi-file-earmark-plus me-2"></i>新建 (New)</a></li>
                    <li><a class="dropdown-item" href="javascript:void(0)" onclick="CodeEditorPlugin.openFileDialog()"><i class="bi bi-folder2-open me-2"></i>打开 (Open)</a></li>
                    <li><hr class="dropdown-divider"></li>
                    <li><a class="dropdown-item" href="javascript:void(0)" onclick="CodeEditorPlugin.save()"><i class="bi bi-save me-2"></i>保存 (Save) <span class="text-muted ms-2 small">Ctrl+S</span></a></li>
                    <li><a class="dropdown-item" href="javascript:void(0)" onclick="CodeEditorPlugin.saveAs()"><i class="bi bi-save2 me-2"></i>另存为 (Save As...)</a></li>
                    <li><hr class="dropdown-divider"></li>
                    <li><a class="dropdown-item" href="javascript:void(0)" onclick="CodeEditorPlugin.renameCurrentFile()"><i class="bi bi-pencil-square me-2"></i>重命名 (Rename)</a></li>
                    <li><hr class="dropdown-divider"></li>
                    <li><a class="dropdown-item text-danger" href="javascript:void(0)" onclick="CodeEditorPlugin.closeAll()"><i class="bi bi-x-lg me-2"></i>全部关闭 (Close All)</a></li>
                </ul>
            </div>
            <div class="dropdown">
                <button class="btn btn-sm btn-link text-decoration-none dropdown-toggle py-0" data-bs-toggle="dropdown" style="color: #aaa; font-size: 13px;">编辑 (Edit)</button>
                <ul class="dropdown-menu dropdown-menu-dark shadow" style="font-size:13px;">
                    <li><a class="dropdown-item" href="javascript:void(0)" onclick="CodeEditorPlugin.triggerAction('undo')"><i class="bi bi-arrow-counterclockwise me-2"></i>撤销 (Undo) <span class="text-muted ms-2 small">Ctrl+Z</span></a></li>
                    <li><a class="dropdown-item" href="javascript:void(0)" onclick="CodeEditorPlugin.triggerAction('redo')"><i class="bi bi-arrow-clockwise me-2"></i>重做 (Redo) <span class="text-muted ms-2 small">Ctrl+Y</span></a></li>
                    <li><hr class="dropdown-divider"></li>
                    <li><a class="dropdown-item" href="javascript:void(0)" onclick="CodeEditorPlugin.triggerAction('find')"><i class="bi bi-search me-2"></i>查找 (Find) <span class="text-muted ms-2 small">Ctrl+F</span></a></li>
                    <li><a class="dropdown-item" href="javascript:void(0)" onclick="CodeEditorPlugin.triggerAction('replace')"><i class="bi bi-input-cursor-text me-2"></i>替换 (Replace) <span class="text-muted ms-2 small">Ctrl+H</span></a></li>
                </ul>
            </div>
            <div class="dropdown">
                <button class="btn btn-sm btn-link text-decoration-none dropdown-toggle py-0" data-bs-toggle="dropdown" style="color: #aaa; font-size: 13px;">视图 (View)</button>
                <ul class="dropdown-menu dropdown-menu-dark shadow" style="font-size:13px;">
                    <li><a class="dropdown-item" href="javascript:void(0)" onclick="CodeEditorPlugin.toggleWordWrap()"><i class="bi bi-text-wrap me-2"></i>自动换行 (Word Wrap)</a></li>
                    <li><hr class="dropdown-divider"></li>
                    <li><a class="dropdown-item" href="javascript:void(0)" onclick="CodeEditorPlugin.zoom(1)"><i class="bi bi-zoom-in me-2"></i>放大 (Zoom In)</a></li>
                    <li><a class="dropdown-item" href="javascript:void(0)" onclick="CodeEditorPlugin.zoom(-1)"><i class="bi bi-zoom-out me-2"></i>缩小 (Zoom Out)</a></li>
                    <li><a class="dropdown-item" href="javascript:void(0)" onclick="CodeEditorPlugin.zoom(0)"><i class="bi bi-arrow-counterclockwise me-2"></i>重置缩放 (Reset Zoom)</a></li>
                </ul>
            </div>
        </div>`;

        const modalHtml = `
        <div id="code-editor-modal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:#1e1e1e; z-index:9999; flex-direction:column;">
            <!-- Header -->
            <div style="height:40px; background:#333; display:flex; align-items:center; padding:0 10px; justify-content:space-between; flex-shrink:0;">
                ${menuBarHtml}
                
                <div style="position:absolute; left:50%; transform:translateX(-50%); display:flex; align-items:center; gap:8px;">
                    <i class="bi bi-file-code text-primary"></i>
                    <span id="ce-filename" onclick="CodeEditorPlugin.renameCurrentFile()" style="color:#aaa; font-size:13px; font-weight:500; cursor:pointer;" title="点击重命名">Untitled</span>
                    <span id="ce-status" style="font-size:11px; color:#666; display:none; pointer-events:none;">Saved</span>
                </div>

                <div style="display:flex; align-items:center; gap:4px;">
                    <button class="btn btn-sm btn-link text-decoration-none py-0" style="font-size:12px; color: #aaa;" onclick="CodeEditorPlugin.save()" title="保存 (Ctrl+S)">
                        <i class="bi bi-save me-1"></i>保存
                    </button>
                    <button class="btn btn-sm btn-link text-decoration-none py-0" style="font-size:12px; color: #aaa;" onclick="CodeEditorPlugin.saveAs()" title="另存为">
                        <i class="bi bi-save2 me-1"></i>另存为
                    </button>
                    <div style="width: 1px; height: 16px; background: #444; margin: 0 8px;"></div>
                    <button class="btn btn-sm btn-link text-decoration-none py-0 px-2" onclick="CodeEditorPlugin.closeAll()" title="关闭" style="color: #aaa; font-size: 1.1rem;">
                        <i class="bi bi-x-lg"></i>
                    </button>
                </div>
            </div>

            <!-- Tab Bar -->
            <div id="ce-tabs-container" class="ce-tab-bar" style="height:35px; flex-shrink:0;"></div>
            
            <!-- Editor Container -->
            <div id="monaco-container" style="flex:1; width:100%; height:100%; overflow:hidden;"></div>

            <!-- Status Bar -->
            <div style="height:22px; background:#333; color:#aaa; border-top: 1px solid #444; display:flex; align-items:center; padding:0 10px; justify-content:space-between; font-size:11px; user-select:none; flex-shrink:0;">
                <div id="ce-cursor-info"><i class="bi bi-cursor me-1"></i>Ln 1, Col 1</div>
                <div style="display:flex; gap:15px;">
                    <span>UTF-8</span>
                    <span id="ce-lang-info">PLAIN TEXT</span>
                </div>
            </div>
        </div>
        `;
        document.body.insertAdjacentHTML('beforeend', modalHtml);

        // Prevent global shortcuts from reaching the main app
        const modal = document.getElementById('code-editor-modal');
        modal.addEventListener('keydown', (e) => {
            const isCtrl = e.ctrlKey || e.metaKey;
            if (isCtrl) {
                const key = e.key.toLowerCase();
                if (['c', 'v', 'a', 's', 'z', 'y', 'f', 'h'].includes(key)) {
                    e.stopPropagation();
                }
            }
        });
    },

    // --- 2. Custom Prompt Implementation (Fixes focus/input issues in PyQt) ---
    showInputPrompt: function(title, defaultValue, callback) {
        let overlay = document.getElementById('ce-prompt-overlay');
        if (!overlay) {
            const html = `
            <div id="ce-prompt-overlay" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.75); z-index:10001; align-items:center; justify-content:center; backdrop-filter:blur(4px);">
                <div id="ce-prompt-box" style="background:#252526; border:1px solid #444; border-radius:8px; padding:20px; width:380px; box-shadow:0 20px 50px rgba(0,0,0,0.6); pointer-events:auto;">
                    <div id="ce-prompt-title" style="color:#eee; font-size:14px; margin-bottom:15px; font-weight:500; user-select:none;">Prompt</div>
                    <input id="ce-prompt-input" type="text" autocomplete="off" spellcheck="false" class="form-control form-control-sm bg-dark text-white border-secondary mb-4" style="font-size:13px; height:35px; color:#fff !important;">
                    <div class="d-flex justify-content-end gap-2">
                        <button id="ce-prompt-cancel" class="btn btn-sm btn-outline-secondary" style="font-size:12px; padding:5px 15px;">取消</button>
                        <button id="ce-prompt-ok" class="btn btn-sm btn-primary" style="font-size:12px; padding:5px 15px;">确定</button>
                    </div>
                </div>
            </div>`;
            document.body.insertAdjacentHTML('beforeend', html);
            overlay = document.getElementById('ce-prompt-overlay');
        }

        const inputEl = document.getElementById('ce-prompt-input');
        const okBtn = document.getElementById('ce-prompt-ok');
        const cancelBtn = document.getElementById('ce-prompt-cancel');
        
        document.getElementById('ce-prompt-title').innerText = title;
        inputEl.value = defaultValue;
        overlay.style.display = 'flex';
        
        // Block interaction with editor while prompt is visible
        if (this.editor) this.editor.updateOptions({ readOnly: true });

        const closePrompt = () => { 
            overlay.style.display = 'none'; 
            if (this.editor) {
                this.editor.updateOptions({ readOnly: false });
                this.editor.focus();
            }
        };

        // Aggressive focus strategy
        const focusInput = () => { inputEl.focus(); inputEl.select(); };
        setTimeout(focusInput, 100);

        okBtn.onclick = (e) => { 
            if(e) e.preventDefault();
            const val = inputEl.value.trim(); 
            if (val) { closePrompt(); callback(val); } 
        };
        cancelBtn.onclick = (e) => { if(e) e.preventDefault(); closePrompt(); };
        
        inputEl.onkeydown = (e) => {
            e.stopPropagation(); // Stop Monaco or App shortcuts
            if (e.key === 'Enter') { e.preventDefault(); okBtn.onclick(); }
            if (e.key === 'Escape') { e.preventDefault(); cancelBtn.onclick(); }
        };
        
        // Prevent clicking background from losing focus
        overlay.onclick = (e) => { if(e.target === overlay) focusInput(); };
    },

    // --- 3. Multi-Tab Core Logic ---
    open: function(filePath) {
        this.initUI();
        document.getElementById('code-editor-modal').style.display = 'flex';
        
        const existing = this.tabs.find(t => t.path === filePath);
        if (existing) {
            this.switchTab(filePath);
            return;
        }

        if (!window.monaco) {
            this.loadMonacoLib(() => this.loadFileAndCreateTab(filePath));
        } else {
            this.loadFileAndCreateTab(filePath);
        }
    },

    loadFileAndCreateTab: function(filePath) {
        if (filePath.startsWith('Untitled-')) {
            this.addTab(filePath, "");
            return;
        }

        fetch('/api/plugin/code_editor/read', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({path: filePath}) })
        .then(res => res.json())
        .then(data => {
            if (data.error) return this.showToast(data.error, "error");
            this.addTab(filePath, data.content);
        });
    },

    addTab: function(path, content) {
        const ext = path.split('.').pop().toLowerCase();
        const langMap = { 'js': 'javascript', 'py': 'python', 'html': 'html', 'css': 'css', 'json': 'json', 'md': 'markdown', 'xml': 'xml', 'sql': 'sql', 'sh': 'shell', 'txt': 'plaintext', 'ini': 'ini' };
        const model = monaco.editor.createModel(content, langMap[ext] || 'plaintext', monaco.Uri.parse("file://" + path));

        this.tabs.push({ path: path, model: model, viewState: null });
        if (!this.editor) this.createMonacoInstance();
        
        this.renderTabs();
        this.switchTab(path);
    },

    createMonacoInstance: function() {
        const container = document.getElementById('monaco-container');
        this.editor = monaco.editor.create(container, {
            model: null, theme: 'vs-dark', automaticLayout: true, fontSize: 14, minimap: { enabled: true }, scrollBeyondLastLine: false
        });
        this.editor.onDidChangeCursorPosition(() => this.updateStatusBar());
        this.editor.onDidChangeModelLanguage(() => this.updateStatusBar());
        this.editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => this.save());
    },

    switchTab: function(path) {
        const target = this.tabs.find(t => t.path === path);
        if (!target || !this.editor) return;

        if (this.activePath) {
            const current = this.tabs.find(t => t.path === this.activePath);
            if (current) current.viewState = this.editor.saveViewState();
        }

        this.editor.setModel(target.model);
        if (target.viewState) this.editor.restoreViewState(target.viewState);
        this.editor.focus();
        this.activePath = path;
        
        document.getElementById('ce-filename').innerText = path.split(/[/\\]/).pop();
        this.renderTabs();
        this.updateStatusBar();
    },

    closeTab: function(path, event) {
        if (event) event.stopPropagation();
        const idx = this.tabs.findIndex(t => t.path === path);
        if (idx === -1) return;
        const tab = this.tabs[idx];
        if (tab.model) tab.model.dispose();
        this.tabs.splice(idx, 1);
        
        if (this.tabs.length === 0) {
            this.closeAll();
        } else if (this.activePath === path) {
            const newTarget = this.tabs[idx] || this.tabs[idx - 1];
            this.switchTab(newTarget.path);
        } else {
            this.renderTabs();
        }
    },

    renderTabs: function() {
        const container = document.getElementById('ce-tabs-container');
        if (!container) return;
        container.innerHTML = '';
        this.tabs.forEach(tab => {
            const name = tab.path.split(/[/\\]/).pop();
            const isActive = tab.path === this.activePath ? 'active' : '';
            const el = document.createElement('div');
            el.className = `ce-tab ${isActive}`;
            el.innerHTML = `
                <i class="bi bi-file-earmark-code me-2" style="font-size:12px;"></i>
                <span class="text-truncate" style="max-width:140px;">${name}</span>
                <span class="ce-tab-unsaved"></span>
                <i class="bi bi-x ce-tab-close" onclick="CodeEditorPlugin.closeTab('${tab.path.replace(/\\/g, '\\\\')}', event)"></i>
            `;
            el.onclick = () => this.switchTab(tab.path);
            container.appendChild(el);
        });
    },

    // --- 4. Logic & Actions ---
    newFile: function() {
        const name = `Untitled-${this.tabs.length + 1}.txt`;
        this.open(name);
    },

    renameCurrentFile: function() {
        if (!this.activePath || this.activePath.startsWith('Untitled-')) {
            this.showToast("新建文件请先保存", "info");
            return;
        }
        const oldPath = this.activePath;
        const oldName = oldPath.split(/[/\\]/).pop();
        
        this.showInputPrompt("重命名文件:", oldName, (newName) => {
            if (newName === oldName) return;
            fetch('/api/rename', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ old_path: oldPath, new_name: newName })
            })
            .then(res => res.json())
            .then(data => {
                if (data.success) {
                    const sep = oldPath.includes('/') ? '/' : '\\';
                    const parent = oldPath.substring(0, oldPath.lastIndexOf(sep));
                    const newPath = parent + sep + newName;
                    const tab = this.tabs.find(t => t.path === oldPath);
                    if (tab) tab.path = newPath;
                    this.activePath = newPath;
                    document.getElementById('ce-filename').innerText = newName;
                    this.renderTabs();
                    this.showToast("重命名成功", "success");
                    if (window.reloadFiles) window.reloadFiles();
                } else {
                    this.showToast("重命名失败: " + (data.message || "Unknown error"), "error");
                }
            });
        });
    },

    saveAs: function() {
        if (!this.editor || !this.activePath) return;
        const oldName = this.activePath.split(/[/\\]/).pop();
        const defaultName = oldName.startsWith('Untitled') ? "new_file.txt" : "copy_" + oldName;

        this.showInputPrompt("另存为 (输入文件名):", defaultName, (newName) => {
            let parentDir = "";
            if (this.activePath.includes('/') || this.activePath.includes('\\')) {
                const sep = this.activePath.includes('/') ? '/' : '\\';
                parentDir = this.activePath.substring(0, this.activePath.lastIndexOf(sep)) + sep;
            } else {
                let currentPath = (window.STATE && window.STATE.currentPath) ? window.STATE.currentPath : "";
                if (!currentPath || currentPath.startsWith("smart://")) {
                    currentPath = (window.STATE && window.STATE.rootPath) ? window.STATE.rootPath : "";
                }
                if (currentPath) {
                    const sep = currentPath.includes('\\') ? '\\' : '/';
                    parentDir = currentPath.endsWith(sep) ? currentPath : currentPath + sep;
                }
            }
            this._doSave(parentDir + newName, true);
        });
    },

    save: function() {
        if(!this.activePath) return;
        if(this.activePath.startsWith('Untitled-')) {
            this.saveAs(); 
            return;
        }
        this._doSave(this.activePath);
    },

    _doSave: function(targetPath, isSaveAs = false) {
        if (!this.editor) return;
        const content = this.editor.getValue();
        fetch('/api/plugin/code_editor/save', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({path: targetPath, content: content}) })
        .then(res => res.json())
        .then(data => {
            if (data.status === 'ok') {
                const status = document.getElementById('ce-status'); 
                status.style.display = 'inline'; 
                status.innerText = "已保存 " + new Date().toLocaleTimeString(); 
                setTimeout(() => { if(status) status.style.display='none'; }, 3000);
                this.showToast(isSaveAs ? "另存为成功！" : "保存成功", "success");
                if (isSaveAs) { 
                    const tab = this.tabs.find(t => t.path === this.activePath);
                    if (tab) tab.path = targetPath;
                    this.activePath = targetPath;
                    document.getElementById('ce-filename').innerText = targetPath.split(/[/\\]/).pop();
                    this.renderTabs();
                }
                if (window.reloadFiles) window.reloadFiles();
            } else this.showToast("保存失败: " + data.error, "error");
        });
    },

    triggerAction: function(cmd) {
        if (!this.editor) return;
        switch(cmd) {
            case 'undo': this.editor.trigger('source', 'undo'); break;
            case 'redo': this.editor.trigger('source', 'redo'); break;
            case 'find': this.editor.trigger('source', 'actions.find'); break;
            case 'replace': this.editor.trigger('source', 'editor.action.startFindReplaceAction'); break;
        }
    },

    toggleWordWrap: function() {
        if (!this.editor) return;
        const current = this.editor.getOption(monaco.editor.EditorOption.wordWrap);
        const newState = current === 'off' ? 'on' : 'off';
        this.editor.updateOptions({ wordWrap: newState });
        this.showToast(`自动换行: ${newState === 'on' ? '开启' : '关闭'}`);
    },

    zoom: function(delta) {
        if (!this.editor) return;
        if (delta === 0) {
            this.editor.updateOptions({ fontSize: 14 });
        } else {
            const currentSize = this.editor.getOption(monaco.editor.EditorOption.fontSize);
            this.editor.updateOptions({ fontSize: Math.max(8, Math.min(40, currentSize + delta)) });
        }
    },

    updateStatusBar: function() {
        if (!this.editor) return;
        const position = this.editor.getPosition();
        const model = this.editor.getModel();
        const infoEl = document.getElementById('ce-cursor-info');
        if (infoEl && position) infoEl.innerText = `Ln ${position.lineNumber}, Col ${position.column}`;
        const langEl = document.getElementById('ce-lang-info');
        if (langEl && model) langEl.innerText = model.getLanguageId().toUpperCase();
    },

    openFileDialog: function() { this.showToast("请在主界面选择文件打开", "info"); },

    showToast: function(text, type='info') {
        if (window.showToast) window.showToast(text, type);
        else {
            let bg = "#0d6efd"; if(type==='success') bg="#198754"; if(type==='error') bg="#dc3545";
            Toastify({ text: text, duration: 2000, gravity: "top", position: "center", style: { background: bg, marginTop: "50px", borderRadius: "6px" } }).showToast();
        }
    },

    loadMonacoLib: function(callback) {
        const BASE_URL = '/api/plugin/code_editor/static/vs';
        if (!window.require) {
            const script = document.createElement('script'); script.src = `${BASE_URL}/loader.js`;
            script.onload = () => { window.require.config({ paths: { 'vs': BASE_URL } }); window.require(['vs/editor/editor.main'], callback); };
            document.head.appendChild(script);
        } else { window.require.config({ paths: { 'vs': BASE_URL } }); window.require(['vs/editor/editor.main'], callback); }
    },

    closeAll: function() {
        document.getElementById('code-editor-modal').style.display = 'none';
        this.tabs.forEach(t => t.model.dispose());
        this.tabs = [];
        this.activePath = null;
        if (this.editor) { this.editor.dispose(); this.editor = null; }
        if (window.reloadFiles) window.reloadFiles();
    },

    openSelected: function() {
        const supportedExts = ['.txt', '.md', '.py', '.json', '.xml', '.html', '.css', '.js', '.log', '.ini', '.yaml', '.sql', '.sh'];
        const selected = window.STATE ? window.STATE.selected : new Set();
        let openedCount = 0;
        
        selected.forEach(path => {
            const ext = '.' + path.split('.').pop().toLowerCase();
            if (supportedExts.includes(ext)) {
                this.open(path);
                openedCount++;
            }
        });
        
        if (openedCount === 0) {
            this.showToast("没有选支持的文档文件", "warning");
        } else {
            // Close context menu
            const ctxMenu = document.getElementById('contextMenu');
            if (ctxMenu) ctxMenu.style.display = 'none';
        }
    }
};

// Initialize Context Menu Listener for Batch Open
document.addEventListener('contextmenu', (e) => {
    const btn = document.getElementById('ce-open-batch-btn');
    if (!btn) return;
    
    // Default hide
    btn.style.display = 'none';
    
    const selected = window.STATE ? window.STATE.selected : new Set();
    // Only show if more than 1 item selected
    if (selected.size <= 1) return;
    
    const supportedExts = ['.txt', '.md', '.py', '.json', '.xml', '.html', '.css', '.js', '.log', '.ini', '.yaml', '.sql', '.sh'];
    let hasSupported = false;
    
    // Check if at least one selected file is supported
    for (const path of selected) {
        const ext = '.' + path.split('.').pop().toLowerCase();
        if (supportedExts.includes(ext)) {
            hasSupported = true;
            break;
        }
    }
    
    if (hasSupported) {
        btn.style.display = 'flex'; // Use flex to match other menu items
    }
});
