from __future__ import annotations

from .base import LanguageAdapter
from .python import PythonAdapter
from .rust import RustAdapter

_ADAPTERS: list[LanguageAdapter] = [
    RustAdapter(),
    PythonAdapter(),
]

_BY_NAME: dict[str, LanguageAdapter] = {a.language_name: a for a in _ADAPTERS}


def get_language_adapter(lang_type: str) -> LanguageAdapter | None:
    """言語名から対応する LanguageAdapter を返す。未対応言語は None。"""
    return _BY_NAME.get(lang_type.lower())