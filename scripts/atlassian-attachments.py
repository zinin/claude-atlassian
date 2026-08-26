#!/usr/bin/env python3
"""Точка входа: чинит sys.path и передаёт управление пакету."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from atlassian_attachments.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
