#!/usr/bin/env python3
"""[U1] 前端三模块一致性自动对照检查。

背景：ARCH-13 拆分后遗留两处"迁移了一半"的 P0——
  BUG-P0-03  updateSelectionUI 未列入 fe-render.js 导出清单，
             fe-actions.js 解构得到 undefined，重命名主流程崩溃；
  BUG-P0-04  audio_player.js 用 onclick*= 选择器定位已改为 data-action 的按钮，
             恒 null，截取功能静默失效。
本脚本静态扫描前端源码，把这两类断裂变成可回归的断言：

1. FE 导出 ↔ 解构：fe-state/render 两个 Object.assign(FE, {...}) 导出名，
   必须覆盖 fe-actions.js 头部解构的全部名字。
2. data-action ↔ handler：index.html + 前端 JS 生成的每个 data-action 值，
   必须能解析到 window 上已定义的处理函数（支持 "obj.method" 点路径）。

用法：python3 test_fe_consistency.py（无外部依赖，CI 可直接跑）
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
JS_DIR = os.path.join(HERE, 'static', 'assets', 'js')
TPL = os.path.join(HERE, 'templates', 'index.html')


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


def fe_exported_names() -> set:
    """fe-state.js / fe-render.js 里 Object.assign(FE, {...}) 的导出名。"""
    names = set()
    for mod in ('fe-state.js', 'fe-render.js'):
        src = _read(os.path.join(JS_DIR, mod))
        for m in re.finditer(r'Object\.assign\((?:FE|window\.__FE)\s*,\s*\{', src):
            # 从 { 起做括号配平，截取完整对象字面量
            depth, i, start = 1, m.end(), m.end()
            while i < len(src) and depth:
                if src[i] == '{':
                    depth += 1
                elif src[i] == '}':
                    depth -= 1
                i += 1
            body = src[start:i - 1]
            body = re.sub(r'//[^\n]*', '', body)  # 去行注释，防止注释文本被当名字
            for item in body.split(','):
                item = item.strip()
                if not item:
                    continue
                # 形式：name / name: expr / 'name': expr
                km = re.match(r"^(?:(['\"])(?P<q>.+?)\1|(?P<b>[A-Za-z_$][\w$]*))\s*:", item)
                if km:
                    names.add(km.group('q') or km.group('b'))
                else:
                    names.add(item.split(':')[0].strip())
    return names


def fe_destructured_names() -> set:
    """fe-actions.js 头部 const {...} = window.__FE 解构的名字。"""
    src = _read(os.path.join(JS_DIR, 'fe-actions.js'))
    m = re.search(r'const\s*\{([^}]+)\}\s*=\s*window\.__FE\s*;', src)
    if not m:
        return set()
    return {p.strip() for p in m.group(1).split(',') if p.strip()}


def _window_defined_roots(js_sources: list) -> set:
    """收集 window.X = / window.X.Y = / const|let|var X / function X 的顶层名字，
    以及所有 "obj.method" 点路径的首段对象名（openProAudio 等动态挂载）。"""
    roots = set()
    for src in js_sources:
        for m in re.finditer(r'\bwindow\.([A-Za-z_$][\w$]*)\s*=', src):
            roots.add(m.group(1))
        for m in re.finditer(r'\b(?:const|let|var|function|class)\s+([A-Za-z_$][\w$]*)', src):
            roots.add(m.group(1))
        # window.Foo = {...} / window.Foo.bar = ... 的 Foo
        for m in re.finditer(r'window\.(?P<obj>[A-Za-z_$][\w$]*)\.(?:[A-Za-z_$][\w$]*)\s*=', src):
            roots.add(m.group('obj'))
    return roots


def data_action_refs() -> list:
    """(value, 出处) 列表：index.html 静态 + 三个 fe 模块字符串模板里生成的。"""
    refs = []
    html = _read(TPL)
    refs += [(v, 'index.html') for v in re.findall(r'data-action="([^"]+)"', html)]
    refs += [(v, 'index.html') for v in re.findall(r"data-action='([^']+)'", html)]
    for mod in ('fe-state.js', 'fe-render.js', 'fe-actions.js'):
        src = _read(os.path.join(JS_DIR, mod))
        refs += [(v, mod) for v in re.findall(r'data-action="([^"]+)"', src)]
        refs += [(v, mod) for v in re.findall(r"data-action=\`([^\`]+)\`", src)]
        # 模板字符串内插值：data-action="${...}" 属于动态，跳过
    return refs


def main() -> int:
    failures = []

    exported = fe_exported_names()
    destructured = fe_destructured_names()
    missing = sorted(destructured - exported)
    if missing:
        failures.append(
            f"[FE 导出↔解构] fe-actions.js 解构了但未导出的名字（undefined 崩溃）: {missing}")
    print(f"[1] FE 导出 {len(exported)} 项 / actions 解构 {len(destructured)} 项 → "
          + ("OK" if not missing else f"缺失 {missing}"))

    js_sources = [_read(os.path.join(JS_DIR, m))
                  for m in ('fe-state.js', 'fe-render.js', 'fe-actions.js')]
    js_sources.append(_read(os.path.join(HERE, 'plugins', 'audio_pro', 'static', 'audio_player.js')))
    roots = _window_defined_roots(js_sources)
    # index.html 内联脚本也定义处理函数
    html = _read(TPL)
    roots |= _window_defined_roots([html])
    # 插件 JS（AudioProPlugin 等点路径的目标）
    plugin_static = os.path.join(HERE, 'plugins')
    for dirpath, _, files in os.walk(plugin_static):
        for fn in files:
            if fn.endswith('.js') and 'vendor' not in dirpath and '/vs/' not in dirpath.replace(os.sep, '/'):
                try:
                    roots |= _window_defined_roots([_read(os.path.join(dirpath, fn))])
                except (OSError, UnicodeDecodeError):
                    pass

    bad_refs = []
    for value, where in data_action_refs():
        if '$' in value:  # 动态模板插值，静态检查不覆盖
            continue
        ref = value[len('window.'):] if value.startswith('window.') else value
        head = ref.split('.')[0]
        if head not in roots:
            bad_refs.append((value, where))
    if bad_refs:
        failures.append(
            f"[data-action↔handler] 找不到处理函数的引用: {bad_refs}")
    print(f"[2] data-action 引用 "
          f"{len([1 for v, _ in data_action_refs() if '$' not in v])} 处 → "
          + ("OK" if not bad_refs else f"未解析 {bad_refs}"))

    if failures:
        print("\n".join(failures))
        return 1
    print("✅ 前端一致性检查全部通过")
    return 0


if __name__ == '__main__':
    sys.exit(main())
