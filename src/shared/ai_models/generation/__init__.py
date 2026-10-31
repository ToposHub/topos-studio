"""Shared media-generation provider layer (C1 migration).

Physically hosts the Volcengine Ark provider and its contract modules so
composite (and any module) can use generation WITHOUT importing edit —
the module-boundary rule (docs/module_boundary_rules.md).  The edit side
keeps thin re-export shims at the historical paths, so every existing
consumer keeps working unchanged.
"""
