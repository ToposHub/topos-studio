/**
 * AI 素材库浏览器 —— 前端逻辑
 *
 * 挂在 file_explorer 的全局 window 上，复用其渲染管线：
 *   STATE.allFiles = 搜索结果  →  applyFilterAndSort()  →  自动瀑布流 + 缩略图
 *
 * 不改 file_explorer 核心 JS，所有逻辑封装在本文件。
 */
(function () {
    "use strict";

    // [R1-14] HTML 转义：错误信息等外来源字符串进 innerHTML 前必须转义
    function escapeHtml(s) {
        return String(s).replace(/[&<>"']/g, function (c) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
        });
    }

    // 防抖：用户连续输入时只在停顿后查询
    let searchTimer = null;
    let currentQuery = "";
    let currentFilter = "all";
    let lastResults = []; // 缓存最近一次搜索的全部结果，用于本地类型过滤

    /**
     * 主搜索函数：调插件 /search 端点 → 塞进 STATE.allFiles → 重渲染
     */
    window.aiLibrarySearch = async function (query) {
        const statusEl = document.getElementById("aiSearchStatus");
        const clearBtn = document.getElementById("aiSearchClear");

        currentQuery = (query || "").trim();
        clearBtn.style.display = currentQuery ? "block" : "none";

        if (!currentQuery) {
            // 清空搜索：恢复提示，不主动切回目录浏览（让用户自己点目录）
            if (statusEl) statusEl.textContent = "";
            lastResults = [];
            return;
        }

        if (statusEl) statusEl.textContent = "搜索中...";

        try {
            const url = `/api/plugin/ai_library_browser/search?q=${encodeURIComponent(currentQuery)}&type=${currentFilter}&limit=200`;
            const res = await fetch(url);
            const data = await res.json();

            // vault 服务不可用
            if (data.error) {
                if (statusEl) {
                    statusEl.innerHTML = `<span style="color:#dc3545;">⚠️ ${escapeHtml(data.error)}</span>`;
                }
                return;
            }

            const files = data.files || [];
            lastResults = files;

            if (files.length === 0) {
                if (statusEl) {
                    statusEl.textContent = `未找到 "${currentQuery}" 相关素材`;
                }
                // 清空当前显示
                if (window.STATE) {
                    STATE.allFiles = [];
                    if (typeof applyFilterAndSort === "function") applyFilterAndSort();
                }
                return;
            }

            // ★ 核心：塞进 STATE.allFiles + 调 applyFilterAndSort
            // 复用 file_explorer 的全部渲染管线（瀑布流/缩略图/类型过滤）
            if (window.STATE && typeof applyFilterAndSort === "function") {
                // 用伪 currentPath 标记"这是搜索结果视图"
                // （面包屑会显示这个，但右键"打开目录"需要防护——见下方 hook）
                STATE.currentPath = `ai://search/${currentQuery}`;
                STATE.allFiles = files;
                STATE.renderIdx = 0;
                STATE.selected.clear();
                STATE.visuallySelected.clear();
                const container = document.getElementById("fileContainer");
                if (container) container.innerHTML = "";
                applyFilterAndSort();
            }

            if (statusEl) {
                statusEl.textContent = `找到 ${files.length} 个 "${currentQuery}" 相关素材`;
            }
        } catch (e) {
            if (statusEl) {
                statusEl.innerHTML = `<span style="color:#dc3545;">⚠️ 搜索失败：${escapeHtml(e.message)}</span>`;
            }
        }
    };

    /**
     * 类型过滤切换（全部/视频/图片/音乐）
     * 重新查询（让后端过滤）+ 同步按钮 active 状态
     */
    window.aiLibraryFilter = function (type, btnEl) {
        currentFilter = type;

        // 更新按钮 active 样式
        document.querySelectorAll(".ai-filter-btn").forEach((b) => {
            b.classList.remove("active");
            b.style.background = "var(--btn-bg, #2a2a2a)";
            b.style.color = "var(--text-color, #eee)";
        });
        if (btnEl) {
            btnEl.classList.add("active");
            btnEl.style.background = "var(--btn-active-bg, #0d6efd)";
            btnEl.style.color = "#fff";
        }

        // 有查询时重新搜（带类型过滤）
        if (currentQuery) {
            window.aiLibrarySearch(currentQuery);
        }
    };

    /**
     * 输入框事件绑定（防抖 300ms）
     */
    function initSearchInput() {
        const input = document.getElementById("aiSearchInput");
        if (!input) return;

        input.addEventListener("input", (e) => {
            const val = e.target.value;
            if (searchTimer) clearTimeout(searchTimer);
            searchTimer = setTimeout(() => {
                window.aiLibrarySearch(val);
            }, 300);
        });

        // 回车立即搜
        input.addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
                if (searchTimer) clearTimeout(searchTimer);
                window.aiLibrarySearch(input.value);
            }
        });
    }

    /**
     * 启动时检查 vault 服务是否在线
     */
    async function checkVaultStatus() {
        const statusEl = document.getElementById("aiSearchStatus");
        if (!statusEl) return;

        try {
            const res = await fetch("/api/plugin/ai_library_browser/status");
            const data = await res.json();
            if (data.online) {
                // 拉 stats 显示素材数
                try {
                    const statsRes = await fetch("/api/plugin/ai_library_browser/stats");
                    const stats = await statsRes.json();
                    if (stats.total !== undefined) {
                        statusEl.innerHTML = `📚 库中有 <b>${stats.total}</b> 个素材` +
                            (stats.missing > 0 ? ` <span style="color:#ffc107;">(${stats.missing} 个路径丢失)</span>` : "");
                    }
                } catch (e) {
                    statusEl.textContent = "✅ vault 服务在线";
                }
            } else {
                statusEl.innerHTML = `<span style="color:#dc3545;">⚠️ vault 未启动。终端运行：<code>topos-vault serve</code></span>`;
            }
        } catch (e) {
            statusEl.innerHTML = '<span style="color:#dc3545;">⚠️ 无法连接 vault</span>';
        }
    }

    // DOM 就绪后初始化
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", () => {
            initSearchInput();
            checkVaultStatus();
        });
    } else {
        initSearchInput();
        checkVaultStatus();
    }

    // 防护：当 currentPath 是 ai:// 时，拦截会出问题的操作
    // （右键"在系统中打开"等对伪路径无意义）
    const origOpenInSystem = window.openCurrentFolderInSystem;
    if (origOpenInSystem) {
        window.openCurrentFolderInSystem = function () {
            if (STATE.currentPath && STATE.currentPath.startsWith("ai://")) {
                return; // 搜索结果视图下不执行
            }
            return origOpenInSystem.apply(this, arguments);
        };
    }

    console.log("[AI Library Browser] 插件已加载");
})();
