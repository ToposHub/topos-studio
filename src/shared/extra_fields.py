"""DESK-04A · 未知字段保留的通用机制（跨端前向兼容的核心）。

契约（docs/adr/0005 §2）：高版本写入者的新键必须能穿过低版本读取者
load→save 不丢。实现模式（与 ``ClipInstance._extra_fields``/R28 同构，
本模块把它推广到其余各层）：

1. 数据类声明 ``_extra_fields: dict = field(default_factory=dict,
   repr=False, compare=False)``；
2. ``from_dict`` 用 ``split_extras(data, _KNOWN_KEYS)`` 捕获未知键；
3. ``to_dict`` 末尾 ``**self._extra_fields`` 原样回写；
4. 类挂 ``@preserves_extras``——``with_*``/``copy`` 类更新方法自动
   携带 extras（改已知值不丢未知键）。

``_KNOWN_KEYS`` 必须涵盖 to_dict 写出的全部键 + from_dict 消费的
legacy 迁移键（legacy 键已迁移，不算未知载荷）。
"""

from __future__ import annotations

from typing import Any, Iterable

__all__ = ["split_extras", "preserves_extras"]

#: 挂上本装饰器的类，其 "派生新实例" 的方法名约定。
_DERIVE_PREFIXES = ("with_",)
_DERIVE_NAMES = ("copy", "clone", "duplicate")


def split_extras(data: dict[str, Any], known_keys: Iterable[str]) -> dict[str, Any]:
    """取 *data* 中不在 *known_keys* 内的键值（深拷贝，防共享可变载荷）。"""
    import copy

    known = known_keys if isinstance(known_keys, (set, frozenset)) else \
        frozenset(known_keys)
    return {
        key: copy.deepcopy(value)
        for key, value in data.items()
        if key not in known
    }


def preserves_extras(cls):
    """类装饰器：派生方法（``with_*``/``copy`` 等）自动携带 ``_extra_fields``。

    仅当结果同为该类、其 extras 为空、而源 extras 非空时补拷——
    显式构造的新值（from_dict 等工厂）不受影响。frozen 数据类经
    ``object.__setattr__`` 补写。
    """
    for name in list(vars(cls)):
        if not (name.startswith(_DERIVE_PREFIXES) or name in _DERIVE_NAMES):
            continue
        fn = vars(cls)[name]
        if isinstance(fn, (staticmethod, classmethod)) or not callable(fn):
            # 静态/类工厂不持有 self 的 extras 语义，跳过
            continue

        def wrapper(self, *args, _fn=fn, **kwargs):
            result = _fn(self, *args, **kwargs)
            mine = getattr(self, "_extra_fields", None)
            if (
                isinstance(result, cls)
                and isinstance(mine, dict) and mine
                and getattr(result, "_extra_fields", None) == {}
            ):
                import copy as _copy
                try:
                    result._extra_fields = _copy.deepcopy(mine)
                except AttributeError:
                    # frozen dataclass
                    object.__setattr__(
                        result, "_extra_fields", _copy.deepcopy(mine))
            return result

        wrapper.__name__ = getattr(fn, "__name__", name)
        wrapper.__doc__ = fn.__doc__
        setattr(cls, name, wrapper)
    return cls
