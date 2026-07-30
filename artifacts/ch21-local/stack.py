"""Read ``compose.yaml`` and check the stack without starting it.

Docker is not available in this repository's test environment and is not
required by any chapter demo, so the Compose file ships as a real file that
is validated by **parsing** rather than by applying. That is a weaker check
than `docker compose up` and it is not a token one: it catches the failures
that actually bite, which are a service that disappeared from the file, an
image referenced but never pinned, a floating tag, a dependency on a
service that does not exist, and the agent quietly losing its Postgres.

There is no YAML dependency in this repository, so this module carries a
parser for the subset ``compose.yaml`` uses: block mappings, block
sequences, flow sequences, flow mappings, comments, quoted and bare
scalars, and ``${VAR}`` interpolation. Anything fancier raises rather than
guessing, because a loader that guesses is a loader that will one day
guess wrong about a port mapping.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

__all__ = [
    "COMPOSE_PATH",
    "ENV_EXAMPLE_PATH",
    "REQUIRED_SERVICES",
    "ComposeError",
    "image_variables",
    "load_compose",
    "load_env",
    "loads",
    "problems",
    "unpinned_images",
]

HERE = Path(__file__).resolve().parent
COMPOSE_PATH = HERE / "compose.yaml"
ENV_EXAMPLE_PATH = HERE / ".env.example"

#: The nine services, and what each stands in for. A service missing from
#: the file is a component the local stack silently stopped modelling.
REQUIRED_SERVICES: dict[str, str] = {
    "agent": "accepts work, runs admission, returns run identifiers",
    "worker": "executes runs off a queue, so a run outlives a request",
    "postgres": "runs, checkpoints, approvals, event log, world state",
    "redis": "the queue and the streaming pub/sub channel",
    "gateway": "the local MCP server: policy and identity live here",
    "model": "optional local inference; not the agent runtime",
    "collector": "OTLP in, fan out; identical code locally and in prod",
    "traces": "self-hosted trace store, so nothing leaves the laptop",
    "approvals": "the approval UI, exercised by a human and not a fixture",
}

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")


class ComposeError(ValueError):
    """The Compose file is not the shape this stack requires."""


# --------------------------------------------------------------- the parser


def loads(text: str) -> Any:
    """Parse the YAML subset this stack uses.

    Raises:
        ComposeError: On anything the subset does not cover, including
            tabs, anchors, aliases, multi-document files, and block
            scalars. Guessing is how a loader ends up wrong about a port.
    """
    if "\t" in text:
        raise ComposeError("tabs are not valid YAML indentation")
    for line in text.splitlines():
        stripped = _strip_comment(line).strip()
        if not stripped:
            continue
        if stripped.startswith("<<:"):
            raise ComposeError(f"merge keys are not supported: {stripped}")
        body = stripped[2:].strip() if stripped.startswith("- ") else stripped
        value = body.partition(":")[2].strip() if ":" in body else body
        if value[:1] in {"&", "*", "|", ">"}:
            raise ComposeError(
                f"anchors, aliases, and block scalars are not supported: "
                f"{stripped}"
            )
    lines = [
        (len(raw) - len(raw.lstrip(" ")), _strip_comment(raw).strip())
        for raw in text.splitlines()
    ]
    lines = [(indent, body) for indent, body in lines if body]
    value, index = _parse_block(lines, 0, lines[0][0] if lines else 0)
    if index != len(lines):
        raise ComposeError(f"trailing content at line {index}")
    return value


def _strip_comment(line: str) -> str:
    """Drop a trailing comment, respecting quotes."""
    out: list[str] = []
    quote = ""
    for i, char in enumerate(line):
        if quote:
            out.append(char)
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
            out.append(char)
            continue
        if char == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(char)
    return "".join(out)


def _parse_block(
    lines: list[tuple[int, str]],
    index: int,
    indent: int,
) -> tuple[Any, int]:
    """Parse one block at ``indent``, returning the value and next index."""
    if index >= len(lines):
        return {}, index
    if lines[index][1].startswith("- "):
        return _parse_sequence(lines, index, indent)
    return _parse_mapping(lines, index, indent)


def _parse_sequence(
    lines: list[tuple[int, str]],
    index: int,
    indent: int,
) -> tuple[list[Any], int]:
    """Parse a block sequence of scalars or inline mappings."""
    items: list[Any] = []
    while index < len(lines):
        line_indent, body = lines[index]
        if line_indent < indent or not body.startswith("- "):
            break
        items.append(_scalar(body[2:].strip()))
        index += 1
    return items, index


def _parse_mapping(
    lines: list[tuple[int, str]],
    index: int,
    indent: int,
) -> tuple[dict[str, Any], int]:
    """Parse a block mapping at ``indent``."""
    out: dict[str, Any] = {}
    while index < len(lines):
        line_indent, body = lines[index]
        if line_indent < indent:
            break
        if line_indent > indent:
            raise ComposeError(f"unexpected indentation: {body!r}")
        if ":" not in body:
            raise ComposeError(f"expected 'key: value', got {body!r}")
        key, _, rest = body.partition(":")
        key = key.strip()
        rest = rest.strip()
        index += 1
        if rest:
            out[key] = _scalar(rest)
            continue
        if index < len(lines) and lines[index][0] > indent:
            out[key], index = _parse_block(lines, index, lines[index][0])
        elif index < len(lines) and lines[index][1].startswith("- "):
            out[key], index = _parse_sequence(lines, index, indent)
        else:
            out[key] = None
    return out, index


def _scalar(text: str) -> Any:
    """Parse one scalar, flow sequence, or flow mapping."""
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        return [_scalar(p.strip()) for p in _split_flow(inner)] if inner \
            else []
    if text.startswith("{") and text.endswith("}"):
        inner = text[1:-1].strip()
        out: dict[str, Any] = {}
        for part in _split_flow(inner):
            key, _, value = part.partition(":")
            out[key.strip()] = _scalar(value.strip())
        return out
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if text.lstrip("-").isdigit():
        return int(text)
    return text


def _split_flow(text: str) -> list[str]:
    """Split a flow collection on commas outside quotes and brackets."""
    parts: list[str] = []
    depth = 0
    quote = ""
    current: list[str] = []
    for char in text:
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    if current:
        parts.append("".join(current).strip())
    return [p for p in parts if p]


# ---------------------------------------------------------------- the checks


def load_compose(path: Path = COMPOSE_PATH) -> dict[str, Any]:
    """Parse the Compose file.

    Raises:
        ComposeError: If it does not parse or has no services.
    """
    document = loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or "services" not in document:
        raise ComposeError(f"{path.name} has no services block")
    return document


def load_env(path: Path = ENV_EXAMPLE_PATH) -> dict[str, str]:
    """Parse a ``KEY=value`` file, skipping comments and blank lines."""
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def image_variables(document: dict[str, Any]) -> dict[str, str]:
    """Map each service that uses an image variable to the variable name."""
    found: dict[str, str] = {}
    for name, service in document["services"].items():
        image = (service or {}).get("image")
        if not isinstance(image, str):
            continue
        match = _VAR.search(image)
        if match:
            found[name] = match.group(1)
    return found


def unpinned_images(env: dict[str, str]) -> list[str]:
    """Image variables that are not pinned by digest.

    A tag moves. A digest does not. This is the difference between "a new
    engineer's first run and CI's thousandth run execute the same bytes"
    and a sentence in a wiki that says they should.
    """
    return sorted(
        key
        for key, value in env.items()
        if key.endswith("_IMAGE") and not _DIGEST.search(value)
    )


def problems(
    compose_path: Path = COMPOSE_PATH,
    env_path: Path = ENV_EXAMPLE_PATH,
) -> list[str]:
    """Every way this stack is not the stack the chapter describes.

    Returns:
        A list of problems, empty when the file is sound.
    """
    found: list[str] = []
    try:
        document = load_compose(compose_path)
    except ComposeError as exc:
        return [f"compose.yaml does not parse: {exc}"]

    services = document["services"]
    for name, role in REQUIRED_SERVICES.items():
        if name not in services:
            found.append(f"missing service {name!r} ({role})")

    for name, service in services.items():
        for dependency in (service or {}).get("depends_on") or []:
            if dependency not in services:
                found.append(
                    f"{name} depends on {dependency!r}, which is not defined"
                )

    agent = services.get("agent") or {}
    if "postgres" not in (agent.get("depends_on") or []):
        found.append("the agent does not depend on postgres")
    if (agent.get("environment") or {}).get("MODEL_MODE") != "mock":
        found.append("the agent's MODEL_MODE default is not mock")

    env = load_env(env_path)
    for service, variable in image_variables(document).items():
        if variable not in env:
            found.append(
                f"{service} uses ${{{variable}}}, which .env.example does "
                f"not set"
            )
    found.extend(
        f"{variable} is not pinned by digest" for variable in unpinned_images(env)
    )
    return found
