/* plugins/audio_pro/static/audio_player.js - 终极修复版 */

// [FUNC-18] 波形中键平移的 window 级监听只绑一次（旧实现每次打开音频
// 都向 window 挂一对 mousemove/mouseup 并闭包引用旧 DOM，持续泄漏）
(function () {
    window.addEventListener('mousemove', (e) => {
        const P = window.AudioProPlugin;
        if (!P || !P._panning || !P._panWrapper) return;
        P._panWrapper.scrollLeft = P._panStartLeft - (e.clientX - P._panStartX) * 1.5;
    });
    window.addEventListener('mouseup', () => {
        const P = window.AudioProPlugin;
        if (!P || !P._panning) return;
        P._panning = false;
        if (P._panWrapper) P._panWrapper.style.cursor = 'crosshair';
    });
})();

window.AudioProPlugin = {
    // 插件状态：包含 WaveSurfer 实例、区域、频谱、播放状态、缩放和循环模式
    state: { ws: null, regions: null, spectrogram: null, isPlaying: false, zoomLevel: 20, currentPath: '', loopMode: 'single' },

    openProAudio: function(filePath) {
        const file = typeof filePath === 'string' ? { path: filePath, name: filePath.split('/').pop() } : filePath;
        this.state.currentPath = file.path;
        document.getElementById('proAudioPlayer').style.display = 'flex';
        document.getElementById('proAudioTitle').innerText = file.name;
        this.initWaveSurfer(); this.bindKeys(); 
    },

    initWaveSurfer: function() {
        if (this.state.ws) this.state.ws.destroy();
        const Regions = window.RegionsPlugin || WaveSurfer.Regions;
        const Timeline = window.TimelinePlugin || WaveSurfer.Timeline;
        this.state.ws = WaveSurfer.create({
            container: '#wave-main', waveColor: '#444', progressColor: '#007aff', cursorColor: '#fff',
            height: 200, barWidth: 2, minPxPerSec: this.state.zoomLevel,
            plugins: [ (this.state.regions = Regions.create()), Timeline.create({ container: '#wave-timeline' }) ]
        });
        const wrapper = this.state.ws.getWrapper();
        // 中键平移波形
        // [FUNC-18] mousemove/mouseup 已提升为 window 级单次绑定（见文件末尾），
        // 这里只记录当前 wrapper —— 旧实现每次 init 都挂一对新监听且引用旧 wrapper
        wrapper.addEventListener('mousedown', (e) => {
            if (e.button === 1) {
                e.preventDefault(); this._panning = true; this._panWrapper = wrapper;
                this._panStartX = e.clientX; this._panStartLeft = wrapper.scrollLeft;
                wrapper.style.cursor = 'grabbing';
            }
        });
        // 右键清除选区
        wrapper.addEventListener('contextmenu', (e) => {
            e.preventDefault();
            if (this.state.regions.getRegions().length > 0) { 
                this.clearSelection(); 
                if (window.showToast) {
                    window.showToast("选区已清除");
                } else {
                    Toastify({text: "选区已清除", duration: 1000, style: {background: "#444"}}).showToast(); 
                }
            }
        });
        // 播放结束处理（支持单曲/列表循环）
        this.state.ws.on('finish', () => {
            if (this.state.loopMode === 'single') { this.state.ws.setTime(0); this.state.ws.play(); }
            else if (this.state.loopMode === 'list') { this.navigate(1); }
            else { this.state.isPlaying = false; this.updateUI(); }
        });

        // ️ 滚轮缩放
        wrapper.addEventListener('wheel', (e) => {
            e.preventDefault();
            this.state.zoomLevel = Math.max(10, Math.min(1000, this.state.zoomLevel + (e.deltaY > 0 ? -10 : 10)));
            this.state.ws.zoom(this.state.zoomLevel);
        });

        this.state.regions.enableDragSelection({ color: 'rgba(255, 165, 0, 0.3)' });
        this.state.regions.on('region-created', (r) => { 
            this.state.regions.getRegions().forEach(old => { if(old !== r) old.remove(); });
        });

        this.state.ws.load(`/api/file?path=${encodeURIComponent(this.state.currentPath)}`);
        this.state.ws.on('ready', () => { 
            this.state.ws.play(); this.state.isPlaying = true; this.updateUI(); 
            document.getElementById('proTimeTotal').innerText = this.fmtTime(this.state.ws.getDuration());
        });
        this.state.ws.on('audioprocess', (t) => {
            document.getElementById('proTimeCurrent').innerText = this.fmtTime(t);
            const r = this.state.regions.getRegions()[0];
            // 选区内循环播放优先
            if (r && this.state.isPlaying && t >= r.end) this.state.ws.setTime(r.start);
        });
    },

    toggleLoop: function() {
        const modes = ['none', 'single', 'list'];
        let idx = modes.indexOf(this.state.loopMode);
        this.state.loopMode = modes[(idx + 1) % modes.length];
        const labels = { 'none': '关闭循环', 'single': '单曲循环', 'list': '列表循环' };
        if (window.showToast) {
            window.showToast(labels[this.state.loopMode]);
        } else {
            Toastify({ text: labels[this.state.loopMode], duration: 1500, style: { background: "#444" } }).showToast();
        }
        this.updateUI();
    },

    // 🔴 5. 合并后的单一 updateUI 函数
    updateUI: function() {
        // 播放按钮图标更新
        const playIcon = document.getElementById('proPlayIcon');
        if (playIcon) playIcon.className = this.state.isPlaying ? 'bi bi-pause-circle-fill' : 'bi bi-play-circle-fill';

        // 循环按钮图标与颜色更新
        const loopBtn = document.getElementById('proLoopBtn');
        const loopIcon = document.getElementById('proLoopIcon');
        if (loopBtn && loopIcon) {
            if (this.state.loopMode === 'none') {
                loopBtn.className = "btn p-0 text-white-50";
                loopIcon.className = "bi bi-repeat";
            } else if (this.state.loopMode === 'single') {
                loopBtn.className = "btn p-0 text-primary"; 
                loopIcon.className = "bi bi-repeat-1";      
            } else if (this.state.loopMode === 'list') {
                loopBtn.className = "btn p-0 text-primary"; 
                loopIcon.className = "bi bi-repeat";        
            }
        }
    },

    bindKeys: function() {
        const handler = (e) => {
            const player = document.getElementById('proAudioPlayer');
            // 1. 如果播放器未显示，不拦截
            if (!player || player.style.display !== 'flex') return;

            const isCtrl = e.ctrlKey || e.metaKey;

            // 2. 拦截 Space (播放/暂停)
            if (e.code === 'Space') {
                e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
                this.togglePlay();
            }
            // 3. 拦截 Ctrl+C
            else if (isCtrl && e.code === 'KeyC') {
                // 这一步阻止了事件传给主界面，所以不会再复制文件了
                e.preventDefault(); 
                e.stopPropagation(); 
                e.stopImmediatePropagation();
                
                // 🔴 【修改点】将下面这行代码删除或注释掉，就不会再有弹窗了
                // if (window.showToast) window.showToast("暂不支持复制波形数据", "info");
                
                // 保持为空即可，这样按下 Ctrl+C 时界面没有任何反应（静默失败）
            }
            // 4. 拦截方向键 (快进/快退)
            else if (e.code === 'ArrowLeft') { 
                e.preventDefault(); e.stopPropagation();
                this.state.ws.setTime(Math.max(0, this.state.ws.getCurrentTime() - 5));
            }
            else if (e.code === 'ArrowRight') { 
                e.preventDefault(); e.stopPropagation();
                this.state.ws.setTime(Math.min(this.state.ws.getDuration(), this.state.ws.getCurrentTime() + 5));
            }
            // 5. 拦截 ESC (取消选区)
            else if (e.code === 'Escape') {
                e.preventDefault(); e.stopPropagation();
                this.clearSelection();
            }
        };

        if (this._boundKeyHandler) {
            window.removeEventListener('keydown', this._boundKeyHandler, true);
        }
        this._boundKeyHandler = handler;
        // 务必保持 true (捕获模式)，确保比全局监听器先执行
        window.addEventListener('keydown', this._boundKeyHandler, true);
    },

    toggleSpectrogram: function(checked) {
        const container = document.getElementById('wave-spectrogram');
        if (checked) {
            container.style.display = 'block';
            if (!this.state.spectrogram) {
                const Spec = window.SpectrogramPlugin || WaveSurfer.Spectrogram;
                this.state.spectrogram = this.state.ws.registerPlugin(Spec.create({ container: container, labels: true, height: 120 }));
            }
        } else { container.style.display = 'none'; }
    },

    navigate: function(dir) {
        let idx = STATE.currentIndex;
        const exts = ['.mp3','.wav','.ogg','.flac','.m4a','.aac'];
        while (true) {
            idx += dir;
            if (idx < 0 || idx >= STATE.visibleFiles.length) break;
            const f = STATE.visibleFiles[idx];
            if (exts.some(e => f.name.toLowerCase().endsWith(e))) {
                STATE.currentIndex = idx; this.openProAudio(f); return;
            }
        }
    },

    togglePlay: function() { this.state.ws.playPause(); this.state.isPlaying = !this.state.isPlaying; this.updateUI(); },
    clearSelection: function() { this.state.regions.clearRegions(); },
    setVolume: function(v) { this.state.ws.setVolume(v/100); },
    fmtTime: (s) => `${Math.floor(s/60)}:${Math.floor(s%60).toString().padStart(2,'0')}.${Math.floor((s%1)*10)}`,
    
    saveTrim: async function() {
        const regions = this.state.regions.getRegions();
        if (regions.length === 0) {
            if (window.showToast) {
                window.showToast("请先在波形上拖拽出要截取的区域", "warning");
            } else {
                Toastify({ text: "请先在波形上拖拽出要截取的区域", duration: 2000, style: { background: "#ff4444" } }).showToast();
            }
            return;
        }

        // [BUG-P0-04] ARCH-13 已把按钮改为 data-action（无 onclick 属性），
        // 旧 onclick*= 选择器恒 null → 截取功能整体静默失效。改 id 定位。
        const btn = document.getElementById('proTrimBtn');
        if (!btn) return;
        const originalHtml = btn.innerHTML;
        const region = regions[0];
        
        btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>处理中...';
        btn.disabled = true;

        try {
            const response = await fetch('/api/audio/trim', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    path: this.state.currentPath,
                    start: region.start.toFixed(3),
                    end: region.end.toFixed(3)
                })
            });

            const result = await response.json();
            if (result.success) {
                if (window.showToast) {
                    window.showToast("截取成功！新文件已生成", "success");
                } else {
                    Toastify({ text: "截取成功！新文件已生成", duration: 3000, style: { background: "#198754" } }).showToast();
                }
                if (window.reloadFiles) window.reloadFiles(); 
            } else {
                if (window.showToast) {
                    window.showToast("截取失败: " + result.message, "error");
                } else {
                    Toastify({ text: "截取失败: " + result.message, duration: 4000, style: { background: "#dc3545" } }).showToast();
                }
            }
        } catch (err) {
            if (window.showToast) {
                window.showToast("系统错误", "error");
            } else {
                Toastify({ text: "系统错误", duration: 3000, style: { background: "#dc3545" } }).showToast();
            }
        } finally {
            btn.innerHTML = originalHtml;
            btn.disabled = false;
        }
    }
};

window.openProAudio = (f) => AudioProPlugin.openProAudio(f);
window.closeProAudio = () => { AudioProPlugin.state.ws?.pause(); document.getElementById('proAudioPlayer').style.display = 'none'; };
