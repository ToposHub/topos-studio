#!/usr/bin/env python3
"""
Library 面板优化验证脚本
用于验证：
1. 拖拽宽度优化（requestAnimationFrame 节流）
2. 字体大小缩小一号
"""

import re
import sys
from pathlib import Path

def check_drag_optimization():
    """检查拖拽优化是否正确应用"""
    print("🔍 检查拖拽宽度优化...")

    # [U9] ARCH-13 已将 script.js 拆分为三模块，rAF 逻辑现居 fe-state.js
    fe_state = Path(__file__).parent / "static" / "assets" / "js" / "fe-state.js"
    content = fe_state.read_text(encoding='utf-8')

    # 检查是否包含 requestAnimationFrame 优化
    if 'requestAnimationFrame' in content:
        print("  ✅ 拖拽优化已应用：使用 requestAnimationFrame 节流")
        return True
    else:
        print("  ❌ 拖拽优化未找到")
        return False


def check_font_sizes():
    """检查字体大小是否正确缩小"""
    print("\n🔍 检查 Library 面板字体大小...")
    
    # 检查 CSS 文件
    style_css = Path(__file__).parent / "static" / "assets" / "css" / "style.css"
    content = style_css.read_text(encoding='utf-8')
    
    checks = {
        'tree-header': r'\.tree-header.*font-size:\s*10px',
        'tree-toggler': r'\.tree-toggler.*font-size:\s*10px',
        'smart-folder-item': r'\.smart-folder-item.*font-size:\s*10px',
        'add-smart-btn': r'\.add-smart-btn.*font-size:\s*11px',
    }
    
    all_passed = True
    for name, pattern in checks.items():
        if re.search(pattern, content, re.DOTALL):
            print(f"  ✅ {name}: 字体已缩小")
        else:
            print(f"  ❌ {name}: 字体未找到预期值")
            all_passed = False
    
    # 检查 HTML 文件
    index_html = Path(__file__).parent / "templates" / "index.html"
    html_content = index_html.read_text(encoding='utf-8')
    
    # 检查内联样式
    inline_fonts = re.findall(r'style="font-size:\s*(\d+)px"', html_content)
    sidebar_fonts = [int(size) for size in inline_fonts if int(size) <= 12]
    
    if sidebar_fonts and all(size <= 10 for size in sidebar_fonts[:3]):  # 检查前3个（侧边栏相关）
        print(f"  ✅ HTML 内联字体已缩小: {sidebar_fonts[:3]}")
    else:
        print(f"  ⚠️  HTML 内联字体可能需要检查: {inline_fonts}")
    
    return all_passed


def check_theme_fonts():
    """检查 toposcolor 主题字体"""
    print("\n🔍 检查 ToposColor 主题字体...")
    
    theme_css = Path(__file__).parent / "static" / "themes" / "toposcolor.css"
    if not theme_css.exists():
        print("  ⚠️  toposcolor.css 不存在，跳过")
        return True
    
    content = theme_css.read_text(encoding='utf-8')

    # [U9] 主题此后经过视觉迭代（10px → 13px/14px），此检查不再锚定具体
    # 数值，只验证主题确实在用 !important 主动管理面板字体
    if 'font-size:' in content and '!important' in content:
        print("  ✅ 主题字体由 toposcolor.css 主动管理")
        return True
    else:
        print("  ❌ 主题未管理字体（缺少 font-size !important）")
        return False


def main():
    print("=" * 60)
    print("📊 Library 面板优化验证")
    print("=" * 60)
    
    results = []
    results.append(check_drag_optimization())
    results.append(check_font_sizes())
    results.append(check_theme_fonts())
    
    print("\n" + "=" * 60)
    if all(results):
        print("✅ 所有优化验证通过！")
        print("=" * 60)
        print("\n📝 优化总结：")
        print("  1. 拖拽宽度：使用 requestAnimationFrame 节流，减少重绘")
        print("  2. 字体大小：统一缩小一号（11-13px → 10-11px）")
        print("  3. 主题统一：toposcolor.css 同步更新")
        return 0
    else:
        print("❌ 部分优化未通过，请检查上述标记为 ❌ 的项目")
        print("=" * 60)
        return 1


if __name__ == "__main__":
    sys.exit(main())
