"""Portable Jupyter and Databricks source notebook interchange."""
from __future__ import annotations

import json
import re
from pathlib import Path
from uuid import uuid4


def new_cell(language="python", source=""):
    return {"id": uuid4().hex, "language": language, "source": source}


def parse_notebook(filename: str, content: bytes):
    if len(content) > 10 * 1024 * 1024:
        raise ValueError("Notebook must be smaller than 10 MB")
    text = content.decode("utf-8-sig")
    suffix = Path(filename).suffix.lower()
    if suffix == ".ipynb":
        document = json.loads(text)
        if not isinstance(document, dict) or not isinstance(document.get("cells"), list):
            raise ValueError("Not a Jupyter notebook")
        cells = []
        for original in document["cells"]:
            if not isinstance(original, dict):
                raise ValueError("Notebook cells must be objects")
            source = original.get("source", "")
            if isinstance(source, list):
                if not all(isinstance(line, str) for line in source):
                    raise ValueError("Notebook cell source must contain text")
                source = "".join(source)
            if not isinstance(source, str):
                raise ValueError("Notebook cell source must contain text")
            language = "markdown" if original.get("cell_type") == "markdown" else "python"
            if source.lstrip().startswith("%%sql"):
                language, source = "sql", source.lstrip()[5:].lstrip("\r\n")
            cells.append(new_cell(language, source))
    elif suffix in (".py", ".sql"):
        language = "sql" if suffix == ".sql" else "python"
        text = re.sub(r"^# Databricks notebook source\r?\n", "", text)
        cells = []
        for chunk in re.split(r"(?m)^\s*# COMMAND ----------\s*$", text):
            lines = chunk.strip().splitlines()
            if not lines:
                continue
            lines = [re.sub(r"^# MAGIC ?", "", line) for line in lines]
            chunk = "\n".join(lines)
            cell_language = language
            for magic, magic_language in (("%sql", "sql"), ("%python", "python"), ("%md", "markdown")):
                if chunk.startswith(magic):
                    chunk = chunk[len(magic):].lstrip("\r\n ")
                    cell_language = magic_language
                    break
            cells.append(new_cell(cell_language, chunk))
    else:
        raise ValueError("Supported notebook formats: .ipynb, .py and .sql")
    if not cells:
        cells = [new_cell()]
    if len(cells) > 500:
        raise ValueError("Notebook may contain at most 500 cells")
    return {"name": Path(filename).stem.replace("_", " "), "language": "python", "description": "", "cells": cells}


def export_notebook(notebook):
    cells = []
    for cell in notebook["cells"]:
        source = cell["source"]
        if cell["language"] == "sql":
            source = "%%sql\n" + source
        kind = "markdown" if cell["language"] == "markdown" else "code"
        entry = {"cell_type": kind, "metadata": {}, "source": source.splitlines(keepends=True)}
        if kind == "code":
            entry.update(execution_count=None, outputs=[])
        cells.append(entry)
    return {"nbformat": 4, "nbformat_minor": 5, "metadata": {"kernelspec": {"display_name": "OpenBricks Python + Spark SQL", "language": "python", "name": "python3"}}, "cells": cells}
