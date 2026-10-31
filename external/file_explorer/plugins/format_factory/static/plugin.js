const FormatFactoryPlugin = {
    open: function(path) {
        // If path is missing, use current selection or nothing
        if (!path && window.STATE && window.STATE.selected.size > 0) {
            path = Array.from(window.STATE.selected)[0];
        }
        
        fetch('/api/plugin/format_factory/launch', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({path: path || ''})
        }).then(() => {
            if (window.showToast) {
                window.showToast("正在启动格式工厂...", "info");
            } else {
                Toastify({
                    text: "正在启动格式工厂...",
                    duration: 2000,
                    style: {background: "#252526", border: "1px solid #0d6efd"}
                }).showToast();
            }
        });
        
        const menu = document.getElementById('contextMenu');
        if(menu) menu.style.display = 'none';
    }
};
// Expose to global scope for HTML onclick
window.FormatFactoryPlugin = FormatFactoryPlugin;
