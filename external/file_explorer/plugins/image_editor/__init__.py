import os
import base64
from flask import Blueprint, request, jsonify

# Plugin Metadata
NAME = 'Darkroom'

# Define Blueprint
bp = Blueprint('image_editor', __name__, static_folder='static')

def setup(context):
    # 1. Register JS Bridge
    context.register_js('/api/plugin/image_editor/static/bridge.js')

    # 2. Register Context Menu (Right Click on Images)
    # Only show for images
    menu_html = """
    <div class="menu-item" onclick="DarkroomPlugin.open(window.STATE.contextTarget ? window.STATE.contextTarget.path : null)">
        <i class="bi bi-palette2 me-2 text-warning"></i>编辑 / 调色 (Darkroom)
    </div>
    """
    context.register_ui_component('context_menu', menu_html)

    # 3. Register Supported Files
    context.register_file_handler(['.jpg', '.jpeg', '.png', '.webp', '.bmp'], 'DarkroomPlugin.open')

# --- APIs ---

@bp.route('/read', methods=['POST'])
def read_image():
    """Read image and return as Base64 to prevent Canvas Tainting"""
    path = request.json.get('path')
    if not path or not os.path.exists(path):
        return jsonify({'error': 'File not found'}), 404

    # [R1-14] 大小上限：base64 整读会随文件尺寸放大内存（编码后 ×4/3），
    # 编辑器场景 128MB 足够，超出直接拒绝
    max_bytes = 128 * 1024 * 1024
    try:
        if os.path.getsize(path) > max_bytes:
            return jsonify({'error': 'File too large'}), 413
    except OSError:
        return jsonify({'error': 'File not accessible'}), 404

    try:
        with open(path, "rb") as image_file:
            encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
            ext = os.path.splitext(path)[1].lower().replace('.', '')
            if ext == 'jpg': ext = 'jpeg'
            mime = f"data:image/{ext};base64,{encoded_string}"
            return jsonify({'data': mime})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@bp.route('/save', methods=['POST'])
def save_image():
    """Save Base64 data back to file"""
    data = request.json
    path = data.get('path')
    image_data = data.get('image') # data:image/png;base64,....
    
    if not path or not image_data:
        return jsonify({'error': 'Missing data'}), 400
        
    try:
        # Strip header
        if ',' in image_data:
            header, encoded = image_data.split(',', 1)
        else:
            encoded = image_data
            
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(encoded))
            
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
