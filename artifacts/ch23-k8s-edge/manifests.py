"""Load and validate the Kubernetes manifests without a cluster.

`kubectl` is not available in this repository's test environment and no
chapter demo may require one, so the manifests ship as real files validated
by **parsing** and by the same admission checks the controller applies.
That is weaker than `kubectl apply --dry-run=server` and it is not a token
check: an `Agent` that omits its policy reference, floats its model
snapshot, or asks for anything other than deny-by-default egress is
rejected here, which is the failure a controller would otherwise discover
in production.

There is no YAML dependency in this repository, so this module carries a
parser for the subset the manifests use: multiple documents separated by
``---``, block mappings, block sequences of scalars and of mappings, flow
sequences, flow mappings, comments, and scalars. Anything else raises.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "K8S_DIR",
    "REQUIRED_SPEC_FIELDS",
    "AgentSpec",
    "ManifestError",
    "admission_problems",
    "load",
    "load_all",
    "loads",
]

K8S_DIR = Path(__file__).resolve().parent / "k8s"

#: What an ``Agent`` must carry. Every one of these answers a question
#: somebody asks during an incident, which is why none of them is optional.
REQUIRED_SPEC_FIELDS: tuple[str, ...] = (
    "version",
    "model",
    "budget",
    "tools",
    "policyRef",
    "egress",
)


class ManifestError(ValueError):
    """The manifest is not the shape this controller admits."""


# --------------------------------------------------------------- the parser


def loads(text: str) -> list[Any]:
    """Parse a possibly multi-document YAML file into a list of documents."""
    documents: list[Any] = []
    for chunk in _split_documents(text):
        lines = _significant(chunk)
        if not lines:
            continue
        value, index = _parse(lines, 0, lines[0][0])
        if index != len(lines):
            raise ManifestError(f"trailing content: {lines[index][1]!r}")
        documents.append(value)
    return documents


def _split_documents(text: str) -> list[str]:
    """Split on ``---`` at column zero."""
    chunks: list[list[str]] = [[]]
    for line in text.splitlines():
        if line.rstrip() == "---":
            chunks.append([])
        else:
            chunks[-1].append(line)
    return ["\n".join(chunk) for chunk in chunks]


def _significant(text: str) -> list[tuple[int, str]]:
    """Indentation and body for every line that carries content."""
    if "\t" in text:
        raise ManifestError("tabs are not valid YAML indentation")
    out: list[tuple[int, str]] = []
    for raw in text.splitlines():
        stripped = raw.split("#")[0].rstrip() if _comment_at_start(raw) \
            else _strip_comment(raw)
        if not stripped.strip():
            continue
        out.append((len(stripped) - len(stripped.lstrip(" ")),
                    stripped.strip()))
    return out


def _comment_at_start(line: str) -> bool:
    """Whether the line is nothing but a comment."""
    return line.lstrip().startswith("#")


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
        elif char == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(char)
    return "".join(out)


def _parse(
    lines: list[tuple[int, str]],
    index: int,
    indent: int,
) -> tuple[Any, int]:
    """Parse one block at ``indent``."""
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
    """Parse a block sequence whose items may be scalars or mappings."""
    items: list[Any] = []
    while index < len(lines):
        line_indent, body = lines[index]
        if line_indent != indent or not body.startswith("- "):
            break
        head = body[2:].strip()
        # ``- name: orders`` starts a mapping that continues on the
        # following lines at the item's own content indent.
        if ":" in head and not head.startswith(("[", "{", '"', "'")):
            item_indent = indent + 2
            inner_lines = [(item_indent, head)]
            index += 1
            # Collect until the item ends. An item ends at a shallower
            # indent, or at the next ``- `` back at the sequence's own
            # level. A ``- `` *deeper* than that belongs to a nested
            # sequence inside this item and has to be collected, which is
            # the case a naive "stop at the next dash" loop gets wrong.
            while index < len(lines):
                line_indent, line_body = lines[index]
                if line_indent < item_indent:
                    break
                if line_indent == indent and line_body.startswith("- "):
                    break
                inner_lines.append(lines[index])
                index += 1
            value, consumed = _parse_mapping(inner_lines, 0, item_indent)
            if consumed != len(inner_lines):
                raise ManifestError(f"could not parse list item {head!r}")
            items.append(value)
            continue
        items.append(_scalar(head))
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
            raise ManifestError(f"unexpected indentation: {body!r}")
        if ":" not in body:
            raise ManifestError(f"expected 'key: value', got {body!r}")
        key, _, rest = body.partition(":")
        key = key.strip()
        rest = rest.strip()
        index += 1
        if rest:
            out[key] = _scalar(rest)
            continue
        if index < len(lines) and lines[index][0] > indent:
            out[key], index = _parse(lines, index, lines[index][0])
        elif index < len(lines) and lines[index][1].startswith("- ") \
                and lines[index][0] == indent:
            out[key], index = _parse_sequence(lines, index, indent)
        else:
            out[key] = None
    return out, index


def _scalar(text: str) -> Any:
    """Parse one scalar, flow sequence, or flow mapping."""
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        return [_scalar(p) for p in _split_flow(inner)] if inner else []
    if text.startswith("{") and text.endswith("}"):
        inner = text[1:-1].strip()
        out: dict[str, Any] = {}
        for part in _split_flow(inner):
            key, _, value = part.partition(":")
            out[key.strip()] = _scalar(value.strip())
        return out
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if text in ("true", "false"):
        return text == "true"
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
    return [p.strip() for p in parts if p.strip()]


# ------------------------------------------------------------- the manifests


def load(name: str, directory: Path = K8S_DIR) -> dict[str, Any]:
    """Load one single-document manifest.

    Raises:
        ManifestError: If the file holds anything other than one document.
    """
    documents = loads((directory / name).read_text(encoding="utf-8"))
    if len(documents) != 1:
        raise ManifestError(
            f"{name} holds {len(documents)} documents; expected 1"
        )
    return documents[0]


def load_all(directory: Path = K8S_DIR) -> dict[str, dict[str, Any]]:
    """Every manifest in the directory, keyed by filename."""
    return {
        path.name: load(path.name, directory)
        for path in sorted(directory.glob("*.yaml"))
    }


@dataclass(frozen=True)
class AgentSpec:
    """One ``Agent`` custom resource, validated.

    This is the chapter's "effective configuration hash expressed as a
    resource": the agent's version, model snapshot, tool set, budget, and
    policy are one reviewable object, so "which version is running" is
    answered by ``kubectl get`` rather than by an archaeology session.
    """

    name: str
    version: str
    model: dict[str, Any]
    budget: dict[str, Any]
    tools: list[dict[str, Any]]
    policy_ref: str
    egress: str
    owner: str
    replicas: int

    @property
    def snapshot(self) -> str:
        """The pinned model snapshot. Never a floating alias."""
        return str(self.model.get("snapshot", ""))

    @property
    def tool_names(self) -> list[str]:
        """The tools, by name, in declaration order."""
        return [str(t["name"]) for t in self.tools]

    def mcp_servers(self) -> list[str]:
        """Which MCP servers the controller has to wire."""
        return [str(t["mcpServer"]) for t in self.tools if "mcpServer" in t]

    def approval_rule(self, tool: str) -> str | None:
        """The approval rule declared for one tool, if any."""
        for entry in self.tools:
            if entry["name"] == tool:
                rule = entry.get("requiresApproval")
                return str(rule) if rule else None
        return None

    def config_hash_inputs(self) -> dict[str, Any]:
        """Everything that changes behaviour, and nothing that does not."""
        return {
            "version": self.version,
            "model": dict(self.model),
            "budget": dict(self.budget),
            "tools": [dict(t) for t in self.tools],
            "policyRef": self.policy_ref,
            "egress": self.egress,
        }

    @classmethod
    def from_manifest(cls, document: dict[str, Any]) -> AgentSpec:
        """Build a spec from a parsed manifest.

        Raises:
            ManifestError: On anything admission would reject.
        """
        problems = admission_problems(document)
        if problems:
            raise ManifestError("; ".join(problems))
        spec = document["spec"]
        return cls(
            name=str(document["metadata"]["name"]),
            version=str(spec["version"]),
            model=dict(spec["model"]),
            budget=dict(spec["budget"]),
            tools=[dict(t) for t in spec["tools"]],
            policy_ref=str(spec["policyRef"]),
            egress=str(spec["egress"]),
            owner=str(spec.get("owner", "")),
            replicas=int(spec.get("replicas", 1)),
        )


def admission_problems(document: dict[str, Any]) -> list[str]:
    """Every reason a controller should refuse this ``Agent``.

    Fails closed on each one. An agent admitted without a policy reference
    is an agent whose authorization is whatever the namespace default
    happens to be, which is how a cluster ends up with an unreviewed
    permission surface nobody chose.
    """
    problems: list[str] = []
    if document.get("kind") != "Agent":
        problems.append(f"kind is {document.get('kind')!r}, not 'Agent'")
    if not str(document.get("apiVersion", "")).startswith(
        "agents.northstar.dev/"
    ):
        problems.append(f"unexpected apiVersion {document.get('apiVersion')!r}")
    if not (document.get("metadata") or {}).get("name"):
        problems.append("metadata.name is missing")

    spec = document.get("spec") or {}
    for field in REQUIRED_SPEC_FIELDS:
        if field not in spec or spec[field] in (None, "", [], {}):
            problems.append(f"spec.{field} is missing")

    egress = spec.get("egress")
    if egress is not None and egress != "deny-by-default":
        problems.append(
            f"spec.egress is {egress!r}; an agent fetches URLs a customer "
            f"supplied, so the only admissible value is 'deny-by-default'"
        )

    model = spec.get("model") or {}
    snapshot = str(model.get("snapshot", ""))
    if not snapshot:
        problems.append("spec.model.snapshot is missing")
    elif snapshot.endswith(("latest", "-v1", ":latest")):
        problems.append(
            f"spec.model.snapshot {snapshot!r} looks like a floating alias"
        )

    for entry in spec.get("tools") or []:
        if "name" not in entry:
            problems.append("a tool reference has no name")
        if "mcpServer" not in entry:
            problems.append(
                f"tool {entry.get('name')!r} has no mcpServer reference; "
                f"tools attach declaratively, not as inline definitions"
            )
    return problems
