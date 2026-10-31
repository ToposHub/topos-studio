from __future__ import annotations
from src.features.scripting.engine import ScriptEngine


class ScriptingModule:
    def is_available(self) -> bool:
        return True

    def create_engine(self, image_engine):
        return ScriptEngine(image_engine)


def create_script_engine(image_engine):
    return ScriptingModule().create_engine(image_engine)
