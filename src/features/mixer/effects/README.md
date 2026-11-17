# Mixer parameter-panel UI catalog

These 24 panels were supplied by the user in `Topos test` and copied into this feature with safe ASCII module names. `source_manifest.json` records original filenames, class names and source SHA-256 values. The files under `panels/` retain the supplied implementation and naming conventions; new adapters around them use typed APIs. Their `if __name__ == '__main__'` blocks are never executed by the catalog.

All entries have UI version `1.0.0-ui`, `backend_status=not_connected`, `supports_preview=False`, and `supports_export=False`. No production audio effect, DSP, project persistence or export capability is registered. `parameter_schemas.json` describes the parameter snapshots accepted by these panel UIs; it is not a promise that a DSP backend supports those values.

The window wrapper explicitly labels its state as UI-only and stops the source panels' demonstration timers. It caches each hidden panel under `(owner_id, instance_id)`, preserving all A/B banks and clip-editor UI details across close/reopen without claiming that those UI-only details have been serialized into a Topos project. `pause_all()` hides windows when leaving the mixer page; `dispose_all()` releases them on shutdown. Removing a rack instance releases its cached window.

```python
manager = EffectWindowManager(page)
rack = EffectRackWidget(manager)
rack.set_owner('track-1', '对白 DX', [], scope='track')
instance = rack.insert_effect('eq')
manager.open(instance, '对白 DX')
# On page leave: manager.pause_all()
# On page disposal: manager.dispose_all()
```

Browser double-click and its insert button both emit `effectChosen(effect_id)`. The browser can be scoped with `set_scope('clip'|'track'|'bus'|'master')`. Audio Clip Editor is only listed for clip owners. Rack `changed(owner_id, instances)` is a local UI-state notification; the containing page must not use it to mutate production engine state during this UI-only milestone.
