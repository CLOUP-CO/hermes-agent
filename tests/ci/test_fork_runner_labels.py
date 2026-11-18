"""The CLOUP fork only has GitHub-hosted runners.

Upstream workflows ask for ``blacksmith-*`` and ``windows-latest-32-*-core``
labels that exist only in the upstream org; on this fork no runner picks them up
and the jobs wait in the queue until they are cancelled. Every upstream sync brings
those labels back, so this guard fails as soon as a workflow names a runner label
outside the hosted set.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"

HOSTED = {
    "ubuntu-24.04",
    "ubuntu-24.04-arm",
    "ubuntu-latest",
    "windows-2025",
    "windows-11-arm",
    "macos-latest",
    "macos-15",
    "macos-15-intel",
}

_EXPR = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
_LITERAL = re.compile(r"'((?:[^']|'')*)'")


def _labels(value) -> list[str]:
    """Runner labels a ``runs-on``/``runner`` value can resolve to.

    Literal strings count as they are; inside a ``${{ }}`` expression only its quoted
    literals count (``a == 'x64' && 'macos-15-intel' || 'macos-15'``), minus the ones that
    are compared against, which are inputs and not labels. A list or a ``labels`` mapping
    contributes every entry.
    """
    if isinstance(value, list):
        return [label for item in value for label in _labels(item)]
    if isinstance(value, dict):
        return _labels(value.get("labels", []))
    if not isinstance(value, str):
        return []
    exprs = _EXPR.findall(value)
    if not exprs:
        return [value.strip()] if value.strip() else []
    out = []
    for expr in exprs:
        compared = set(re.findall(r"[=!]=\s*'((?:[^']|'')*)'", expr))
        out += [lit for lit in _LITERAL.findall(expr) if lit not in compared]
    return out


def _walk(node, path=()):
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("runs-on", "runner"):
                yield path + (key,), value
            yield from _walk(value, path + (str(key),))
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from _walk(item, path + (str(i),))


def runner_labels(text: str) -> list[tuple[str, str]]:
    doc = yaml.safe_load(text) or {}
    return [(".".join(path), label) for path, value in _walk(doc.get("jobs") or {})
            for label in _labels(value)]


def _workflow_files() -> list[Path]:
    return sorted(p for p in WORKFLOWS.iterdir() if p.suffix in (".yml", ".yaml"))


@pytest.mark.parametrize("workflow", _workflow_files(), ids=lambda p: p.name)
def test_workflow_only_names_hosted_runners(workflow: Path):
    unknown = [(where, label) for where, label in runner_labels(workflow.read_text(encoding="utf-8"))
               if label not in HOSTED]
    assert not unknown, f"{workflow.name}: runner labels this fork cannot serve: {unknown}"


def test_guard_catches_upstream_labels():
    upstream = """
jobs:
  plain:
    runs-on: blacksmith-4vcpu-ubuntu-2404
  pick:
    runs-on: ${{ inputs.arch == 'arm64' && 'windows-latest-32-arm-core' || 'windows-2025' }}
  grid:
    strategy:
      matrix:
        target:
          - runner: blacksmith-32vcpu-ubuntu-2404-arm
    runs-on: ${{ matrix.target.runner }}
"""
    found = {label for _, label in runner_labels(upstream)}
    assert found == {"blacksmith-4vcpu-ubuntu-2404", "windows-latest-32-arm-core", "windows-2025",
                     "blacksmith-32vcpu-ubuntu-2404-arm"}
    assert found - HOSTED == {"blacksmith-4vcpu-ubuntu-2404", "windows-latest-32-arm-core",
                              "blacksmith-32vcpu-ubuntu-2404-arm"}
