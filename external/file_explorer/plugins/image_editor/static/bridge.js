window.DarkroomPlugin = {
    currentPath: null,
    
    initUI: function() {
        if (document.getElementById('darkroom-modal')) return;
        
        const modalHtml = `
        <div id="darkroom-modal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.9); z-index:9999; flex-direction:column;">
            <div style="height:50px; background:#252526; display:flex; align-items:center; padding:0 20px; justify-content:space-between; border-bottom:1px solid #333;">
                <div class="d-flex align-items-center gap-2">
                    <i class="bi bi-palette2 text-warning"></i>
                    <span class="fw-bold text-white">Darkroom Editor</span>
                    <span id="dr-filename" class="text-muted ms-2 small"></span>
                </div>
                <div class="d-flex gap-2">
                    <button class="btn btn-sm btn-outline-secondary" onclick="DarkroomPlugin.close()">取消</button>
                </div>
            </div>
            
            <div style="flex:1; width:100%; position:relative;">
                <iframe id="darkroom-frame" src="/api/plugin/image_editor/static/editor.html" 
                    style="width:100%; height:100%; border:none; display:block;"></iframe>
            </div>
        </div>
        `;
        document.body.insertAdjacentHTML('beforeend', modalHtml);

        // Listen for Save Response from Iframe
        window.addEventListener('message', (e) => {
            if (e.data.type === 'SAVE_DATA') {
                this.handleSave(e.data.payload);
            }
        });
    },

    open: function(path) {
        if (!path) return;
        
        // 3. 确保在 open 时清除之前的 currentPath 状态，防止旧图残留干扰
        this.currentPath = null;
        
        this.initUI();
        this.currentPath = path;
        
        const modal = document.getElementById('darkroom-modal');
        const frame = document.getElementById('darkroom-frame');
        const title = document.getElementById('dr-filename');
        
        modal.style.display = 'flex';
        title.innerText = path.split(/[/\\]/).pop();

        // Load Image Base64
        Toastify({text: "正在加载图片...", duration: 1000, style: {background: "#252526"}}).showToast();
        
        fetch('/api/plugin/image_editor/read', {
            method: 'POST', 
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({path: path})
        })
        .then(res => res.json())
        .then(data => {
            if(data.error) {
                alert("加载失败: " + data.error);
                this.close();
                return;
            }
            
            // 1. 增加对 iframe onload 状态的判断
            const sendLoadMessage = () => {
                console.log("Darkroom: Sending LOAD_IMAGE to iframe");
                frame.contentWindow.postMessage({ type: 'LOAD_IMAGE', payload: data.data }, '*');
            };

            // 检查 iframe 是否已经加载完成
            // 使用自定义 dataset 标记或 contentWindow 是否可用
            if (frame.contentDocument && frame.contentDocument.readyState === 'complete') {
                sendLoadMessage();
            } else {
                // 2. 如果是首次打开（或尚未加载），给 iframe 一个显式的初始加载完成信号
                frame.onload = () => {
                    console.log("Darkroom: Iframe loaded, sending signal");
                    sendLoadMessage();
                    // 加载完成后清除一次性监听器
                    frame.onload = null;
                };
            }
        });
    },

    save: function() {
        // Request data from Iframe
        const frame = document.getElementById('darkroom-frame');
        frame.contentWindow.postMessage({ type: 'GET_DATA' }, '*');
    },

    handleSave: function(base64Data) {
        Toastify({text: "正在保存...", duration: 2000, style: {background: "#0d6efd"}}).showToast();
        
        fetch('/api/plugin/image_editor/save', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({path: this.currentPath, image: base64Data})
        })
        .then(res => res.json())
        .then(data => {
            if (data.status === 'ok') {
                Toastify({text: "保存成功!", duration: 2000, style: {background: "#198754"}}).showToast();
                this.close();
                if (window.reloadFiles) window.reloadFiles();
            } else {
                alert("保存失败: " + data.error);
            }
        });
    },

    close: function() {
        document.getElementById('darkroom-modal').style.display = 'none';
        // Reset iframe source to clear memory or just leave it
    }
};
