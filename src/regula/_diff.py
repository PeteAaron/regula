"""Output-directory diff helper.

Shared between the ``regula diff`` CLI command and the reproducibility
test. Compares two ``output/<doc_id>/`` directories file-by-file, parsing
JSON / JSONL artefacts so timestamp fields that legitimately vary can be
stripped recursively before comparison.

Used by:
- :func:`regula.cli.diff`
- ``tests/test_reproducibility_skeleton.py``
- ``tests/test_cli_commands.py``
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Fields that will always differ between runs (timestamps, durations, git
# SHAs from dirty trees), stripped recursively before comparison.
IGNORE_KEYS: frozenset[str] = frozenset(
    {
        "generated_at",
        "git_sha",
        "started_at",
        "finished_at",
        "duration_seconds",
    }
)

# Files whose content is always run-specific (log output) and is skipped
# entirely.
IGNORE_FILES: frozenset[str] = frozenset({"run.log", "preview.html"})


@dataclass(frozen=True)
class Diff:
    """One difference between two output directories."""

    path: str  # relative path under the output dir
    message: str


def _strip(node: Any) -> Any:
    """Recursively drop IGNORE_KEYS from any nested dict."""
    if isinstance(node, dict):
        return {k: _strip(v) for k, v in node.items() if k not in IGNORE_KEYS}
    if isinstance(node, list):
        return [_strip(item) for item in node]
    return node


def _files_in(root: Path) -> set[Path]:
    return {p.relative_to(root) for p in root.rglob("*") if p.is_file()}


def _compare_json(a: Path, b: Path, rel: str) -> list[Diff]:
    try:
        a_data = _strip(json.loads(a.read_text(encoding="utf-8")))
        b_data = _strip(json.loads(b.read_text(encoding="utf-8")))
    except json.JSONDecodeError as e:
        return [Diff(rel, f"invalid JSON: {e}")]
    if a_data != b_data:
        return [Diff(rel, "json content differs after stripping IGNORE_KEYS")]
    return []


def _compare_jsonl(a: Path, b: Path, rel: str) -> list[Diff]:
    a_lines = [ln for ln in a.read_text(encoding="utf-8").splitlines() if ln]
    b_lines = [ln for ln in b.read_text(encoding="utf-8").splitlines() if ln]
    if len(a_lines) != len(b_lines):
        return [Diff(rel, f"jsonl line count differs: {len(a_lines)} vs {len(b_lines)}")]
    for i, (la, lb) in enumerate(zip(a_lines, b_lines, strict=True)):
        try:
            sa = _strip(json.loads(la))
            sb = _strip(json.loads(lb))
        except json.JSONDecodeError as e:
            return [Diff(rel, f"invalid JSONL at line {i + 1}: {e}")]
        if sa != sb:
            return [Diff(rel, f"jsonl line {i + 1} differs after stripping IGNORE_KEYS")]
    return []


def _compare_bytes(a: Path, b: Path, rel: str) -> list[Diff]:
    if a.read_bytes() != b.read_bytes():
        return [Diff(rel, "binary content differs")]
    return []


def compare_outputs(a: Path, b: Path) -> list[Diff]:
    """Return the list of meaningful differences between two output dirs.

    Empty list means the runs are identical for reproducibility purposes
    (timestamps and similar metadata aside).
    """
    diffs: list[Diff] = []
    files_a = _files_in(a)
    files_b = _files_in(b)

    only_a = files_a - files_b
    only_b = files_b - files_a
    for rel in sorted(only_a):
        diffs.append(Diff(str(rel), "present in A only"))
    for rel in sorted(only_b):
        diffs.append(Diff(str(rel), "present in B only"))

    for rel in sorted(files_a & files_b):
        if rel.name in IGNORE_FILES:
            continue
        fa, fb = a / rel, b / rel
        if rel.suffix == ".json":
            diffs.extend(_compare_json(fa, fb, str(rel)))
        elif rel.suffix == ".jsonl":
            diffs.extend(_compare_jsonl(fa, fb, str(rel)))
        else:
            diffs.extend(_compare_bytes(fa, fb, str(rel)))

    return diffs
