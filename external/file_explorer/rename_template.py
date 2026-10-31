"""[V3/GAP-10] 批量重命名模板引擎（供批量重命名与导入模块共用）。

语法：``{field}``、``{field:fmt}``，修饰符 ``:upper`` / ``:lower``。

字段：
- {name} 原名（无扩展名）  {ext} 扩展名（含点）
- {seq} / {seq:3} 序号（:N 补零 N 位）
- {date:taken:%Y%m%d} 时间（taken=拍摄时间, mtime=修改时间；缺省 taken 回退 mtime；
  strftime 语法；无时区，按 UTC 取整）
- {camera} {lens} {iso} {aperture} {shutter} {focal} {w} {h} {duration}
- {parent} 父目录名  {parent2} 上上级目录名

未知字段原样保留（用户能直接看出没被替换）。结果统一过滤文件名非法字符。
"""
import os
import re
import time as _time

_TOKEN = re.compile(r'\{([^{}]+)\}')
_ILLEGAL = re.compile(r'[\\/:*?"<>|]')


def sanitize(name: str) -> str:
    return _ILLEGAL.sub('_', name).strip()


def _fmt_duration(seconds) -> str:
    try:
        s = int(round(float(seconds)))
    except (TypeError, ValueError):
        return ''
    if s >= 3600:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    if s >= 60:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s}s"


def render(tpl: str, ctx: dict) -> str:
    """ctx: {name, ext, seq, parent, parent2, meta(dict)}。

    meta 技术列：date_taken/mtime/camera/lens/iso/aperture/shutter/focal/
    width/height/duration（缺的键渲染为空串）。"""
    meta = ctx.get('meta') or {}

    def repl(m):
        expr = m.group(1).strip()
        parts = [p.strip() for p in expr.split(':')]
        key = parts[0]
        # [V5 复查] 先摘出尾部 upper/lower 修饰符：此前只在普通字段的循环里
        # 应用，date/seq 分支提前 return 把修饰符静默吞掉
        mods = []
        while len(parts) > 1 and parts[-1] in ('upper', 'lower'):
            mods.append(parts.pop())
        if key == 'seq':
            digits = 0
            for p in parts[1:]:
                if p.isdigit():
                    digits = int(p)
            val = str(int(ctx.get('seq', 0) or 0)).zfill(digits)
        elif key == 'date':
            src = parts[1] if len(parts) > 1 and parts[1] in ('taken', 'mtime') else 'taken'
            # [V5 复查] strftime 格式按原样拼回（%H:%M 里的冒号此前被
            # split(':') 吃掉，只剩 %H）
            fmt = ':'.join(parts[2:]) if len(parts) > 2 else '%Y%m%d'
            ts = meta.get('date_taken') or meta.get('mtime') if src == 'taken' else meta.get('mtime')
            val = ''
            if ts:
                try:
                    val = _time.strftime(fmt, _time.gmtime(float(ts)))
                except (ValueError, TypeError):
                    val = ''
        elif key == 'name':
            val = str(ctx.get('name') or '')
        elif key == 'ext':
            val = str(ctx.get('ext') or '')
        elif key == 'camera':
            val = str(meta.get('camera') or '')
        elif key == 'lens':
            val = str(meta.get('lens') or '')
        elif key == 'iso':
            v = meta.get('iso')
            val = str(int(v)) if v else ''
        elif key == 'aperture':
            v = meta.get('aperture')
            val = f"f{v:g}" if v else ''
        elif key == 'shutter':
            val = str(meta.get('shutter') or '')
        elif key == 'focal':
            v = meta.get('focal')
            val = f"{v:g}mm" if v else ''
        elif key == 'w':
            v = meta.get('width')
            val = str(int(v)) if v else ''
        elif key == 'h':
            v = meta.get('height')
            val = str(int(v)) if v else ''
        elif key == 'duration':
            val = _fmt_duration(meta.get('duration'))
        elif key == 'parent':
            val = str(ctx.get('parent') or '')
        elif key == 'parent2':
            val = str(ctx.get('parent2') or '')
        else:
            return m.group(0)
        for p in mods:
            if p == 'upper':
                val = val.upper()
            elif p == 'lower':
                val = val.lower()
        return val

    return sanitize(_TOKEN.sub(repl, tpl))
