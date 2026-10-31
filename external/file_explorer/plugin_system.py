import os
import json
import importlib
import logging

# [ARCH-11] 不在此处 basicConfig——那是 import 副作用，会抢占宿主的 root
# logger 配置；日志格式由宿主/独立入口统一设置
logger = logging.getLogger("PluginSystem")

class PluginContext:
    """插件上下文：为主程序提供安全的 API 接口供插件调用"""
    def __init__(self, manager, plugin_name, app, socket=None, config_methods=None):
        self._manager, self.plugin_name, self.app, self.socket = manager, plugin_name, app, socket
        self._config_methods = config_methods or {}
        self.logger = logging.getLogger(f"Plugin.{plugin_name}")

    def register_js(self, js_path): self._manager.register_js(js_path)
    def register_file_handler(self, extensions, handler_js_func): self._manager.register_file_handler(extensions, handler_js_func)
    def register_ui_component(self, slot_name, html_content): self._manager.register_ui_component(slot_name, html_content)
    def register_hook(self, event_name, callback_func): self._manager.register_hook(event_name, callback_func)
    def get_config(self, key=None, default=None):
        # [BUG-P3-56] key 缺省时此前把宿主完整 config 交给任意插件（无
        # 命名空间隔离，配置中的敏感字段全量暴露）。现在无 key 一律拒绝。
        if not key:
            self.logger.warning(
                "插件 %s 无 key 调用 get_config() 已被拒绝（命名空间隔离）",
                self.plugin_name)
            return default
        if 'get' in self._config_methods:
            conf = self._config_methods['get']()
            return conf.get(key, default)
        return default
    def set_config(self, data):
        if 'set' in self._config_methods: self._config_methods['set'](data)

class PluginManager:
    """插件管理器：处理插件加载、钩子注册及 UI 注入"""
    def __init__(self, app=None):
        self.app, self.socket = app, None
        self.config_methods, self.plugins, self.js_extensions, self.file_handlers = {}, [], [], {}
        self.hooks, self.ui_slots, self.failed_plugins = {}, {}, []

    def register_hook(self, event_name, callback_func):
        """注册插件事件钩子"""
        if event_name not in self.hooks: self.hooks[event_name] = []
        self.hooks[event_name].append(callback_func)

    def fire_event(self, event_name, **kwargs):
        """触发已注册的插件事件"""
        results = []
        if event_name in self.hooks:
            for func in self.hooks[event_name]:
                try:
                    res = func(**kwargs)
                    if res: results.append(res)
                except Exception as e: logger.error(f"Hook Error {event_name}: {e}")
        return results
    
    def register_ui_component(self, slot_name, html_content):
        """注入前端 UI 组件槽位"""
        if slot_name not in self.ui_slots: self.ui_slots[slot_name] = []
        self.ui_slots[slot_name].append(html_content)

    def get_ui_components(self, slot_name): return "".join(self.ui_slots.get(slot_name, []))

    def init_app(self, app, socket=None, config_methods=None):
        self.app = app
        self.socket = socket
        self.config_methods = config_methods or {}
        self.load_plugins()

    def load_plugins(self):
        plugins_dir = os.path.join(os.path.dirname(__file__), 'plugins')
        if not os.path.exists(plugins_dir):
            os.makedirs(plugins_dir)

        # [ARCH-11] 插件启停：宿主 config 的 disabled_plugins 列表 + 插件
        # 自身 config.json 的 "disabled": true，两种方式都可以停用插件
        disabled_by_host = []
        if 'get' in self.config_methods:
            try:
                conf = self.config_methods['get']() or {}
                disabled_by_host = conf.get('disabled_plugins', []) or []
            except Exception:
                pass

        # 遍历 plugins 目录下的所有子文件夹
        # [BUG-P3-31/36] 排序遍历：os.listdir 顺序随平台不定，此前同名
        # 扩展名的注册归属随加载顺序漂移
        for folder_name in sorted(os.listdir(plugins_dir)):
            plugin_path = os.path.join(plugins_dir, folder_name)
            init_file = os.path.join(plugin_path, '__init__.py')
            config_file = os.path.join(plugin_path, 'config.json')

            if os.path.isdir(plugin_path) and os.path.exists(init_file):
                try:
                    # [新增] 安全加载机制：try-except 块
                    # 1. 加载配置
                    config = {}
                    if os.path.exists(config_file):
                        with open(config_file, 'r', encoding='utf-8') as f:
                            config = json.load(f)

                    if config.get('disabled') or folder_name in disabled_by_host:
                        logger.info(f"插件已停用（跳过加载）: {folder_name}")
                        continue
                    
                    # 2. 动态导入 Python 模块
                    module_name = f"plugins.{folder_name}"
                    module = importlib.import_module(module_name)

                    # [新增] 插件元数据读取
                    plugin_info = {
                        'name': getattr(module, 'NAME', config.get('name', folder_name)),
                        'version': getattr(module, 'VERSION', config.get('version', '0.0.1')),
                        'author': getattr(module, 'AUTHOR', config.get('author', 'Unknown')),
                        'description': getattr(module, 'DESCRIPTION', config.get('description', '')),
                        'id': folder_name
                    }

                    # [BUG-P3-55] 宿主 API 版本兼容校验：此前收集了 VERSION
                    # 却从不校验。插件可声明 API_VERSION；未声明的按 1 通过
                    # （全部现有插件无需改动）。
                    plugin_api = int(getattr(module, 'API_VERSION', 1) or 1)
                    if plugin_api > HOST_API_VERSION:
                        raise RuntimeError(
                            f"插件要求宿主 API v{plugin_api}，"
                            f"当前宿主仅支持 v{HOST_API_VERSION}")
                    
                    # 3. 注册 Flask Blueprint (如果有)
                    # [增强] API 路由增强：确保不冲突，使用 /api/plugin/<name>
                    if hasattr(module, 'bp'):
                        # 自动修正 blueprint 的 template/static 路径
                        self.app.register_blueprint(module.bp, url_prefix=f"/api/plugin/{folder_name}")

                    # 4. 创建 PluginContext
                    # [增强] 插件上下文
                    context = PluginContext(
                        manager=self,
                        plugin_name=folder_name,
                        app=self.app,
                        socket=self.socket,
                        config_methods=self.config_methods
                    )

                    # 5. 执行插件的 setup 函数
                    if hasattr(module, 'setup'):
                        module.setup(context)

                    logger.info(f"✅ 插件已加载: {plugin_info['name']} ({plugin_info['version']})")
                    self.plugins.append({'name': folder_name, 'config': config, 'info': plugin_info})

                except Exception as e:
                    error_msg = f"❌ 插件加载失败 {folder_name}: {str(e)}"
                    logger.error(error_msg, exc_info=True)
                    self.failed_plugins.append({'name': folder_name, 'error': str(e)})

    # --- 供插件调用的 API ---

    def register_js(self, js_path):
        """插件注册自己的前端脚本"""
        self.js_extensions.append(js_path)

    def register_file_handler(self, extensions, handler_js_func):
        """
        插件注册文件处理器
        extensions: ['.md', '.markdown']
        handler_js_func: 前端调用的 JS 函数名，例如 'MarkdownPlugin.open'
        """
        # 支持单个扩展名或列表
        if isinstance(extensions, str):
            extensions = [extensions]
            
        for ext in extensions:
            self.file_handlers[ext.lower()] = handler_js_func

# 宿主插件 API 版本：不兼容的破坏性变更时递增；插件声明更大的
# API_VERSION 将拒绝加载（进入 failed_plugins 而非半工作状态）
HOST_API_VERSION = 1

# 全局单例
plugin_manager = PluginManager()
