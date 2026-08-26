"""Тесты плагина. Кладёт scripts/ в sys.path, чтобы пакет импортировался."""
import pathlib
import sys

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
