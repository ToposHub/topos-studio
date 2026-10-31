/* Browser bridge for Topos Decode Speed Test. */
(function () {
    "use strict";

    function selectedPaths(path) {
        if (typeof path === "string" && path.trim()) return [path];
        const selection = window.STATE && window.STATE.selected;
        if (selection && typeof selection[Symbol.iterator] === "function") {
            return Array.from(selection).filter((item) => typeof item === "string" && item.trim());
        }
        return [];
    }

    window.DecodeBenchmarkPlugin = {
        open: function (path) {
            const paths = selectedPaths(path);
            fetch("/api/plugin/decode_benchmark/launch", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ paths: paths })
            }).catch(function (error) {
                console.error("Decode benchmark launch failed", error);
                if (window.showToast) window.showToast("解码跑分窗口启动失败", "warning");
            });
            const menu = document.getElementById("contextMenu");
            if (menu) menu.style.display = "none";
        }
    };
})();
