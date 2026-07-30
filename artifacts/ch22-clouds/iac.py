"""Read the infrastructure overlays without applying them.

Terraform is not installed in this repository's test environment and no
chapter demo may create a cloud resource, so the overlays ship as real
files validated by **parsing**. That is weaker than `terraform validate`
and it is not a token check: it catches the drift that matters here, which
is one platform's overlay quietly enforcing a different approval threshold
from the others, or a variable declared and never used, or an overlay whose
tool endpoint is not an output anyone can read.

The parser covers the HCL subset the overlays use: ``terraform``,
``variable``, ``resource``, and ``output`` blocks, ``key = value``
assignments, heredocs, and comments. Anything else raises. This is not an
HCL implementation and should not grow into one; if the overlays need more
syntax than this, run the real tool.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "IAC_ROOT",
    "OVERLAYS",
    "Block",
    "HCLError",
    "Overlay",
    "load_overlay",
    "thresholds",
]

IAC_ROOT = Path(__file__).resolve().parent / "iac"

#: One overlay per platform. Each README-worthy fact about what it creates
#: and what it costs lives in the file's own header comment, where an
#: engineer about to apply it will actually read it.
OVERLAYS: tuple[str, ...] = ("aws", "gcp", "azure")

_BLOCK = re.compile(
    r'^(?P<type>[a-z_]+)((\s+"(?P<a>[^"]+)")(\s+"(?P<b>[^"]+)")?)?\s*\{$'
)
_ASSIGN = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.+)$")
_HEREDOC = re.compile(r"^<<-?(?P<tag>[A-Z]+)$")


class HCLError(ValueError):
    """The overlay is not the subset this parser covers."""


@dataclass
class Block:
    """One HCL block: a type, up to two labels, and its assignments."""

    type: str
    labels: tuple[str, ...]
    body: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        """``resource.aws_x.y`` style identifier."""
        return ".".join((self.type, *self.labels))


@dataclass
class Overlay:
    """One platform's overlay, parsed."""

    cloud: str
    path: Path
    blocks: list[Block]

    def of_type(self, block_type: str) -> list[Block]:
        """Every block of one type, in file order."""
        return [b for b in self.blocks if b.type == block_type]

    @property
    def variables(self) -> dict[str, Block]:
        """Declared variables, by name."""
        return {b.labels[0]: b for b in self.of_type("variable")}

    @property
    def resources(self) -> list[str]:
        """Every resource address, in file order."""
        return [b.name for b in self.of_type("resource")]

    @property
    def outputs(self) -> dict[str, Any]:
        """Declared outputs, by name."""
        return {b.labels[0]: b.body.get("value") for b in self.of_type("output")}

    def approval_threshold(self) -> int | None:
        """The threshold this overlay enforces, or ``None`` if it declares none.

        ``None`` is a finding. An overlay with no threshold deploys an
        agent that can move any amount without a human, and a scorecard row
        for that platform is a benchmark of a system you are not shipping.
        """
        variable = self.variables.get("approval_threshold_cents")
        if variable is None:
            return None
        default = variable.body.get("default")
        return int(default) if isinstance(default, int) else None

    def unused_variables(self) -> list[str]:
        """Variables declared and never referenced."""
        text = self.path.read_text(encoding="utf-8")
        return sorted(
            name
            for name in self.variables
            if f"var.{name}" not in text
        )


def load_overlay(cloud: str, root: Path = IAC_ROOT) -> Overlay:
    """Parse one platform's ``main.tf``.

    Raises:
        HCLError: If the file is missing or uses syntax outside the subset.
    """
    path = root / cloud / "main.tf"
    if not path.exists():
        raise HCLError(f"no overlay for {cloud!r} at {path}")
    return Overlay(cloud, path, _parse(path.read_text(encoding="utf-8")))


def _parse(text: str) -> list[Block]:
    """Parse the HCL subset the overlays use."""
    blocks: list[Block] = []
    current: Block | None = None
    heredoc: tuple[str, str, list[str]] | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if heredoc is not None:
            key, tag, buffer = heredoc
            if line == tag:
                if current is not None:
                    current.body[key] = "\n".join(buffer)
                heredoc = None
            else:
                buffer.append(raw)
            continue
        if not line or line.startswith("#"):
            continue
        if line == "}":
            if current is None:
                raise HCLError("unbalanced closing brace")
            blocks.append(current)
            current = None
            continue

        match = _BLOCK.match(line)
        if match:
            if current is not None:
                raise HCLError(f"nested blocks are not supported: {line}")
            labels = tuple(
                label
                for label in (match.group("a"), match.group("b"))
                if label
            )
            current = Block(match.group("type"), labels)
            continue

        assignment = _ASSIGN.match(line)
        if assignment and current is not None:
            key = assignment.group("key")
            value = assignment.group("value").strip()
            doc = _HEREDOC.match(value)
            if doc:
                heredoc = (key, doc.group("tag"), [])
                continue
            current.body[key] = _value(value)
            continue

        raise HCLError(f"unsupported line: {line}")

    if current is not None:
        raise HCLError(f"unclosed block {current.type}")
    return blocks


def _value(text: str) -> Any:
    """Parse one HCL scalar."""
    if text.startswith('"') and text.endswith('"'):
        return text[1:-1]
    if text in ("true", "false"):
        return text == "true"
    if text.lstrip("-").isdigit():
        return int(text)
    return text


def thresholds(root: Path = IAC_ROOT) -> dict[str, int | None]:
    """The approval threshold each overlay enforces.

    They must be identical. An overlay that gates at a different number is
    a different system, and comparing the three is then a comparison of
    three policies wearing one agent's name.
    """
    return {
        cloud: load_overlay(cloud, root).approval_threshold()
        for cloud in OVERLAYS
    }
