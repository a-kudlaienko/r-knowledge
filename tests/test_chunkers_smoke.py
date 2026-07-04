"""Smoke tests for the 4 tree-sitter-backed chunkers.

Written alongside the tree-sitter-language-pack migration (Item G, see
tasks/todo.md and ``knowledge decide`` topic "tree-sitter-language-pack-
migration"): the repo previously had NO chunker tests at all, so a broken
parser binding (wrong package, wrong grammar name, ABI mismatch) would only
surface at runtime against a real repo. These are deliberately smoke-level,
not grammar-exactness tests: they prove each chunker (a) produces chunks,
(b) emits the kind names its own docstring promises, (c) reports sane
start/end lines within the source file, and (d) that the stored chunk text
is a real, unmodified (or, for JSON, deliberately sanitized) slice of the
source rather than something mangled by a parser/version mismatch.
"""
from __future__ import annotations

from pathlib import Path

from knowledge.chunkers.javascript_chunker import JavaScriptChunker
from knowledge.chunkers.json_chunker import JsonChunker
from knowledge.chunkers.python_chunker import PythonChunker
from knowledge.chunkers.shell_chunker import ShellChunker


def _line_count(source: str) -> int:
    return source.count("\n") + 1


def _assert_sane_lines(chunk, total_lines: int) -> None:
    assert 1 <= chunk.start_line <= chunk.end_line <= total_lines


PYTHON_SOURCE = '''"""Module docstring."""

import os


class Greeter:
    """Greets people."""

    def greet(self, name):
        """Return a greeting."""
        return f"hello {name}"


def standalone(x):
    """Double x."""
    return x * 2
'''


def test_python_chunker_smoke():
    chunker = PythonChunker()
    chunks = chunker.chunk(PYTHON_SOURCE.encode("utf-8"), Path("mod.py"))

    assert chunks, "expected at least one chunk"
    kinds = {c.kind for c in chunks}
    assert "class" in kinds
    assert "function" in kinds
    assert "module_level" in kinds

    total_lines = _line_count(PYTHON_SOURCE)
    for c in chunks:
        _assert_sane_lines(c, total_lines)
        assert c.text in PYTHON_SOURCE

    class_chunk = next(c for c in chunks if c.kind == "class")
    assert class_chunk.name == "Greeter"
    assert "def greet" in class_chunk.text  # methods stay embedded (M2 flat)

    fn_chunk = next(c for c in chunks if c.kind == "function")
    assert fn_chunk.name == "standalone"

    module_chunk = next(c for c in chunks if c.kind == "module_level")
    assert "import os" in module_chunk.text


JSON_SOURCE = """{
  "name": "example",
  "version": "1.0.0",
  "password": "S3cr3t!"
}
"""


def test_json_chunker_smoke():
    chunker = JsonChunker()
    chunks = chunker.chunk(JSON_SOURCE.encode("utf-8"), Path("pkg.json"))

    assert chunks, "expected at least one chunk per top-level key"
    kinds = {c.kind for c in chunks}
    assert kinds == {"json_object"}

    total_lines = _line_count(JSON_SOURCE)
    names = {c.name for c in chunks}
    assert {"name", "version", "password"} <= names

    for c in chunks:
        _assert_sane_lines(c, total_lines)

    name_chunk = next(c for c in chunks if c.name == "name")
    assert '"example"' in name_chunk.text

    # Sanitizer layer 2: sensitive key's string value is scrubbed, not left raw.
    password_chunk = next(c for c in chunks if c.name == "password")
    assert "S3cr3t!" not in password_chunk.text
    assert "CHANGE_ME" in password_chunk.text


SHELL_SOURCE = """#!/usr/bin/env bash
set -euo pipefail

greet() {
  local name="$1"
  echo "hello ${name}"
}

greet "world"
"""


def test_shell_chunker_smoke():
    chunker = ShellChunker()
    chunks = chunker.chunk(SHELL_SOURCE.encode("utf-8"), Path("script.sh"))

    assert chunks, "expected at least one chunk"
    kinds = {c.kind for c in chunks}
    assert "shell_function" in kinds
    assert "module_level" in kinds

    total_lines = _line_count(SHELL_SOURCE)
    for c in chunks:
        _assert_sane_lines(c, total_lines)
        assert c.text in SHELL_SOURCE

    fn_chunk = next(c for c in chunks if c.kind == "shell_function")
    assert fn_chunk.name == "greet"
    assert "echo" in fn_chunk.text

    module_chunk = next(c for c in chunks if c.kind == "module_level")
    assert "set -euo pipefail" in module_chunk.text


JS_SOURCE = """import { readFile } from 'fs';

export function add(a, b) {
  return a + b;
}

class Widget {
  render() {
    return "<div></div>";
  }
}

const square = (x) => x * x;
"""


def test_javascript_chunker_smoke():
    chunker = JavaScriptChunker()
    chunks = chunker.chunk(JS_SOURCE.encode("utf-8"), Path("mod.js"))

    assert chunks, "expected at least one chunk"
    kinds = {c.kind for c in chunks}
    assert "function" in kinds
    assert "class" in kinds
    assert "module_level" in kinds

    total_lines = _line_count(JS_SOURCE)
    for c in chunks:
        _assert_sane_lines(c, total_lines)
        assert c.text in JS_SOURCE

    names = {c.name for c in chunks if c.kind == "function"}
    assert "add" in names
    assert "square" in names  # arrow-function-assigned-to-const picked up

    class_chunk = next(c for c in chunks if c.kind == "class")
    assert class_chunk.name == "Widget"

    module_chunk = next(c for c in chunks if c.kind == "module_level")
    assert "import { readFile }" in module_chunk.text
