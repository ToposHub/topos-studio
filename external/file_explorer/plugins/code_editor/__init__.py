from flask import Blueprint, request, jsonify, current_app
import os

# 插件元数据
NAME = 'Code Editor'
VERSION = '1.0.0'
AUTHOR = 'AssetDeck Team'
DESCRIPTION = '基于 Monaco Editor 的代码编辑器'

# 定义蓝图
bp = Blueprint('code_editor', __name__, static_folder='static')

def setup(context):
    """
    插件初始化
    :param context: PluginContext
    """
    # 1. 注册前端 JS
    # [修改] 路径更新为 /api/plugin/...
    context.register_js('/api/plugin/code_editor/static/main.js')
    
    # 2. 注册文件关联
    supported_exts = [
        '.txt', '.md', '.py', '.json', '.xml', '.html', 
        '.css', '.js', '.log', '.ini', '.yaml', '.sql', '.sh'
    ]
    context.register_file_handler(supported_exts, 'CodeEditorPlugin.open')

    # 3. 注册到右键菜单 (context_menu)
    # 这里的 onclick 调用了前端 JS 函数
    new_file_btn = """
    <div class="menu-item" onclick="CodeEditorPlugin.newFile()">
        <i class="bi bi-file-earmark-plus me-2 text-success"></i>新建代码文件
    </div>
    """
    context.register_ui_component('context_menu', new_file_btn)

    # [新增] 批量打开按钮 (默认隐藏，由 JS 控制显示)
    batch_open_btn = """
    <div id="ce-open-batch-btn" class="menu-item" onclick="CodeEditorPlugin.openSelected()" style="display:none;">
        <i class="bi bi-file-earmark-code me-2 text-primary"></i>在编辑器中打开
    </div>
    """
    context.register_ui_component('context_menu', batch_open_btn)

    # 4. Register Top Menu Entry (Launch empty editor)
    context.register_ui_component('top_menu', """
        <div class="dropdown-item" onclick="CodeEditorPlugin.newFile()">
            <i class="bi bi-file-code me-2 text-primary"></i>VS Code 编辑器
        </div>
    """)

# --- 插件 API ---

@bp.route('/read', methods=['POST'])
def read_file():
    path = request.json.get('path')
    if not path or not os.path.exists(path):
        return jsonify({'error': '文件不存在'}), 404
    
    try:
        # 读取文件内容
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
        return jsonify({'content': content})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@bp.route('/save', methods=['POST'])
def save_file():
    path = request.json.get('path')
    content = request.json.get('content')
    
    if not path:
        return jsonify({'error': '路径缺失'}), 400
        
    try:
        # 写入文件
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
            
        # [新增] 使主应用的列表缓存失效（先进行标准化确保匹配）
        import unicodedata
        
        # 统一路径格式
        clean_path = unicodedata.normalize('NFC', path.replace('\\', '/'))
        parent_dir = os.path.dirname(clean_path)
        
        if hasattr(current_app, 'invalidate_cache'):
            current_app.invalidate_cache(parent_dir)
        
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
