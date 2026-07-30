"""The A2A v1.0 wire contract: the only thing the two agents share.

Two agents on different runtimes cannot share code, or they are not two
agents. So this module holds the object model and nothing else: no client
policy, no peer logic, no transport. ``client/`` imports it and ``peer/``
imports it, and neither imports the other.

Three things here exist because the pre-1.0 shapes are still all over the
web and each one fails quietly rather than loudly.

**Task states are prefixed ProtoJSON enums.** ``TASK_STATE_COMPLETED`` is
the wire value; ``"completed"`` is a label for prose and diagrams. A client
that compares against the lowercase form never sees a terminal state, so it
polls finished work forever. :func:`require_wire_state` refuses the
lowercase form at the boundary, which turns that silent bug into a raise.

**An Agent Card has no top-level ``url``, ``protocolVersion``, or
``preferredTransport``.** It carries an ordered ``supportedInterfaces[]``,
each entry holding its own ``url``, ``protocolBinding``, and
``protocolVersion``. Preference is *position* in that list.
:meth:`AgentCard.from_dict` rejects a card carrying the old fields rather
than reading around them, because a card that still has a top-level ``url``
is a card written against a different protocol version.

**The transitions are a graph, not a boolean.** ``input_required`` and
``auth_required`` are not "not done yet". They are states a task lives in
while it waits for a person or for an authorization server, and they are
the two the 09:14 incident did not have.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

__all__ = [
    "APPROVAL_THRESHOLD_CENTS",
    "HANDOFF_FIELDS",
    "INITIAL_STATES",
    "LEGAL_TRANSITIONS",
    "PROTOCOL_VERSION",
    "SHORT_LABELS",
    "SUPPORTED_BINDINGS",
    "TASK_STATE_AUTH_REQUIRED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_INPUT_REQUIRED",
    "TASK_STATE_REJECTED",
    "TASK_STATE_SUBMITTED",
    "TASK_STATE_WORKING",
    "TERMINAL_STATES",
    "WIRE_STATES",
    "AgentCard",
    "IllegalTransition",
    "Interface",
    "MalformedCard",
    "Task",
    "advance",
    "is_terminal",
    "require_wire_state",
    "short_label",
    "wire_value",
]

#: The protocol version this artifact implements. v1.0.1 is a patch release
#: of the same wire contract, so the version a card declares is ``"1.0"``.
PROTOCOL_VERSION = "1.0"

#: The three bindings v1.0 defines. One object model, three encodings.
SUPPORTED_BINDINGS: tuple[str, ...] = ("JSONRPC", "GRPC", "HTTP_JSON")

#: Northstar's approval threshold, in integer cents. It travels restated in
#: every delegation, because a constraint that is not in the payload does
#: not survive a hop.
APPROVAL_THRESHOLD_CENTS = 5000

# ------------------------------------------------------------- task states

TASK_STATE_SUBMITTED = "TASK_STATE_SUBMITTED"
TASK_STATE_WORKING = "TASK_STATE_WORKING"
TASK_STATE_INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
TASK_STATE_AUTH_REQUIRED = "TASK_STATE_AUTH_REQUIRED"
TASK_STATE_COMPLETED = "TASK_STATE_COMPLETED"
TASK_STATE_FAILED = "TASK_STATE_FAILED"
TASK_STATE_CANCELED = "TASK_STATE_CANCELED"
TASK_STATE_REJECTED = "TASK_STATE_REJECTED"

#: All eight, in the order the chapter introduces them.
WIRE_STATES: tuple[str, ...] = (
    TASK_STATE_SUBMITTED,
    TASK_STATE_WORKING,
    TASK_STATE_INPUT_REQUIRED,
    TASK_STATE_AUTH_REQUIRED,
    TASK_STATE_COMPLETED,
    TASK_STATE_FAILED,
    TASK_STATE_CANCELED,
    TASK_STATE_REJECTED,
)

#: Wire value to the human label prose and diagrams use. One direction
#: only: labels are never wire values, so nothing maps back by accident.
SHORT_LABELS: dict[str, str] = {
    TASK_STATE_SUBMITTED: "submitted",
    TASK_STATE_WORKING: "working",
    TASK_STATE_INPUT_REQUIRED: "input required",
    TASK_STATE_AUTH_REQUIRED: "auth required",
    TASK_STATE_COMPLETED: "completed",
    TASK_STATE_FAILED: "failed",
    TASK_STATE_CANCELED: "canceled",
    TASK_STATE_REJECTED: "rejected",
}

#: The four states that end a task. Only the others accept new messages.
TERMINAL_STATES: frozenset[str] = frozenset(
    {
        TASK_STATE_COMPLETED,
        TASK_STATE_FAILED,
        TASK_STATE_CANCELED,
        TASK_STATE_REJECTED,
    }
)

#: The two states a task may be created in. ``rejected`` is an *admission*
#: outcome: the peer declined before doing any domain work, so the task was
#: never submitted. Modelling it as a transition out of ``submitted`` would
#: say the peer queued work it had already refused.
INITIAL_STATES: frozenset[str] = frozenset(
    {TASK_STATE_SUBMITTED, TASK_STATE_REJECTED}
)

#: Every legal move. A state absent from a value set cannot be reached from
#: that key, and the terminal states reach nothing at all.
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    TASK_STATE_SUBMITTED: frozenset(
        {
            TASK_STATE_WORKING,
            TASK_STATE_FAILED,
            TASK_STATE_CANCELED,
        }
    ),
    TASK_STATE_WORKING: frozenset(
        {
            TASK_STATE_INPUT_REQUIRED,
            TASK_STATE_AUTH_REQUIRED,
            TASK_STATE_COMPLETED,
            TASK_STATE_FAILED,
            TASK_STATE_CANCELED,
        }
    ),
    TASK_STATE_INPUT_REQUIRED: frozenset(
        {
            TASK_STATE_WORKING,
            TASK_STATE_FAILED,
            TASK_STATE_CANCELED,
        }
    ),
    TASK_STATE_AUTH_REQUIRED: frozenset(
        {
            TASK_STATE_WORKING,
            TASK_STATE_FAILED,
            TASK_STATE_CANCELED,
        }
    ),
    TASK_STATE_COMPLETED: frozenset(),
    TASK_STATE_FAILED: frozenset(),
    TASK_STATE_CANCELED: frozenset(),
    TASK_STATE_REJECTED: frozenset(),
}

#: The six-field handoff contract from Chapter 6, as it crosses a network.
#: In-process these are good practice. Across a boundary they are the whole
#: contract, so the receiver validates that all six arrived.
HANDOFF_FIELDS: tuple[str, ...] = (
    "goal",
    "constraints",
    "state_ref",
    "budget_remaining",
    "provenance",
    "return_contract",
)


class IllegalTransition(ValueError):
    """A task was asked to move somewhere the lifecycle does not go.

    Raised rather than logged. A peer that silently allows
    ``completed -> working`` has a task store whose history cannot be
    trusted, and the client polling it has no way to find out.
    """


class MalformedCard(ValueError):
    """An Agent Card this client will not read.

    Includes the pre-1.0 shapes. A card with a top-level ``url`` is not a
    v1.0 card with an extra field; it is a card whose transport preference
    and protocol version live somewhere this code does not look.
    """


def require_wire_state(value: str) -> str:
    """Return ``value`` if it is one of the eight wire values, else raise.

    The guard exists for one specific bug. ``"completed"`` is a perfectly
    good Python string, it compares equal to nothing in
    :data:`TERMINAL_STATES`, and a client using it polls successful work
    until it times out. Failing here converts that into a traceback at the
    line that wrote the wrong value.

    Args:
        value: A candidate task state.

    Returns:
        The value, unchanged.

    Raises:
        ValueError: If the value is not a prefixed ``TASK_STATE_*`` enum.
            The message names the short label when the caller passed one,
            because that is the mistake being made nine times in ten.
    """
    if value in SHORT_LABELS:
        return value
    labels = {v: k for k, v in SHORT_LABELS.items()}
    if value in labels:
        return _reject_label(value, labels[value])
    known = ", ".join(WIRE_STATES)
    raise ValueError(f"{value!r} is not an A2A task state. Expected: {known}.")


def _reject_label(value: str, wire: str) -> str:
    """Refuse a human label used where a wire value belongs."""
    raise ValueError(
        f"{value!r} is a human label, not a wire value. v1.0 carries "
        f"prefixed ProtoJSON enums: use {wire!r}."
    )


def short_label(state: str) -> str:
    """The human label for a wire state, for prose, diagrams, and consoles."""
    return SHORT_LABELS[require_wire_state(state)]


def wire_value(label: str) -> str:
    """The wire value for a human label. The only legal direction of that map.

    Args:
        label: ``"input required"``, or ``"input_required"``, or any other
            short form the chapter's prose uses.

    Returns:
        The prefixed ``TASK_STATE_*`` enum.

    Raises:
        KeyError: If no state carries that label.
    """
    wanted = label.strip().lower().replace("_", " ")
    for state, short in SHORT_LABELS.items():
        if short == wanted:
            return state
    raise KeyError(f"no A2A task state labelled {label!r}")


def is_terminal(state: str) -> bool:
    """Whether a task in this state will never move again."""
    return require_wire_state(state) in TERMINAL_STATES


def advance(state: str, to: str) -> str:
    """Return ``to`` if the lifecycle permits ``state -> to``, else raise.

    Args:
        state: The task's current wire state.
        to: The proposed next wire state.

    Returns:
        ``to``.

    Raises:
        IllegalTransition: If the move is not in :data:`LEGAL_TRANSITIONS`.
    """
    require_wire_state(state)
    require_wire_state(to)
    allowed = LEGAL_TRANSITIONS[state]
    if to not in allowed:
        legal = ", ".join(sorted(allowed)) or "(none: terminal)"
        raise IllegalTransition(
            f"{short_label(state)} -> {short_label(to)} is not a legal A2A "
            f"transition. Legal from {state}: {legal}."
        )
    return to


# ------------------------------------------------------------- agent cards


@dataclass(frozen=True)
class Interface:
    """One entry in a card's ordered ``supportedInterfaces[]``.

    Every field here was a top-level card field before v1.0, which is why
    porting breaks: one card can now offer several protocol versions and
    several bindings at once, and the client has to *pick* rather than read.

    Args:
        url: Where to send task operations for this binding.
        protocol_binding: One of :data:`SUPPORTED_BINDINGS`.
        protocol_version: The A2A version implemented at that url.
    """

    url: str
    protocol_binding: str
    protocol_version: str

    def to_dict(self) -> dict[str, Any]:
        """The ProtoJSON form, with the camelCase keys the wire uses."""
        return {
            "url": self.url,
            "protocolBinding": self.protocol_binding,
            "protocolVersion": self.protocol_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Interface:
        """Read one interface, requiring all three fields.

        Raises:
            MalformedCard: If any of the three is missing. An interface
                without its own ``protocolVersion`` is the pre-1.0 shape
                with a new name.
        """
        missing = [
            k
            for k in ("url", "protocolBinding", "protocolVersion")
            if not data.get(k)
        ]
        if missing:
            raise MalformedCard(
                "supportedInterfaces entry is missing "
                f"{', '.join(missing)}. Each entry carries its own url, "
                "protocolBinding, and protocolVersion."
            )
        return cls(
            url=str(data["url"]),
            protocol_binding=str(data["protocolBinding"]),
            protocol_version=str(data["protocolVersion"]),
        )


#: Fields that identify a pre-1.0 card. Read around them and the client
#: silently ignores the peer's real interface list.
PRE_1_0_FIELDS: tuple[str, ...] = (
    "url",
    "protocolVersion",
    "preferredTransport",
    "additionalInterfaces",
)

_PRE_1_0_ADVICE: dict[str, str] = {
    "url": "moved to supportedInterfaces[].url",
    "protocolVersion": "moved to supportedInterfaces[].protocolVersion",
    "preferredTransport": "replaced by the order of supportedInterfaces[]",
    "additionalInterfaces": "folded into supportedInterfaces[]",
}


@dataclass(frozen=True)
class AgentCard:
    """What a peer publishes at ``/.well-known/agent-card.json``.

    Args:
        name: Stable identifier a registry and an auditor both read.
        description: One line of prose. Third-party text: it lands in the
            calling model's context, so it is an injection surface.
        version: The *agent's* version, not the protocol's.
        supported_interfaces: Ordered. Position is preference.
        capabilities: ``streaming`` and ``pushNotifications`` flags.
        security_schemes: How a caller must authenticate, declared rather
            than negotiated by convention.
        skills: Named units of work, each with id, description, and modes.
        signature: The detached signature this card was fetched with. Not
            part of the card body, and therefore not part of what
            :meth:`to_dict` returns or what the pinned hash covers.
    """

    name: str
    description: str
    version: str
    supported_interfaces: tuple[Interface, ...]
    capabilities: dict[str, Any] = field(default_factory=dict)
    security_schemes: dict[str, Any] = field(default_factory=dict)
    skills: tuple[dict[str, Any], ...] = ()
    signature: str = ""

    @property
    def preferred_interface(self) -> Interface:
        """The first interface offered. Preference is position, not a field."""
        return self.supported_interfaces[0]

    @property
    def url(self) -> str:
        """The preferred interface's url.

        Derived, deliberately. There is no top-level ``url`` on the wire in
        v1.0, and a client that wants one has to say *which* interface it
        means. This property means "the one I would pick by default".
        """
        return self.preferred_interface.url

    @property
    def protocol_version(self) -> str:
        """The preferred interface's A2A version. Also derived, same reason."""
        return self.preferred_interface.protocol_version

    def interface_for(
        self,
        *,
        binding: str | None = None,
        versions: frozenset[str] | None = None,
    ) -> Interface | None:
        """First interface, in the card's own order, that the caller can use.

        Args:
            binding: Restrict to one of :data:`SUPPORTED_BINDINGS`.
            versions: Protocol versions this client has been tested against.

        Returns:
            The highest-preference match, or ``None`` when the card offers
            nothing this client speaks.
        """
        for entry in self.supported_interfaces:
            if binding is not None and entry.protocol_binding != binding:
                continue
            if versions is not None and entry.protocol_version not in versions:
                continue
            return entry
        return None

    def skill_by_id(self, skill_id: str) -> dict[str, Any] | None:
        """One advertised skill by id, or ``None`` if the card omits it."""
        for entry in self.skills:
            if entry.get("id") == skill_id:
                return dict(entry)
        return None

    def to_dict(self) -> dict[str, Any]:
        """The card body, in ProtoJSON form. The signature is not in it."""
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "supportedInterfaces": [
                i.to_dict() for i in self.supported_interfaces
            ],
            "capabilities": dict(self.capabilities),
            "securitySchemes": dict(self.security_schemes),
            "skills": [dict(s) for s in self.skills],
        }

    def with_signature(self, signature: str) -> AgentCard:
        """Return a copy carrying the detached signature it was served with."""
        return replace(self, signature=signature)

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        signature: str = "",
    ) -> AgentCard:
        """Parse a card, refusing the pre-1.0 shapes.

        Args:
            data: The fetched JSON document.
            signature: The detached signature served alongside it.

        Returns:
            The parsed card.

        Raises:
            MalformedCard: On a pre-1.0 top-level field, on an empty
                ``supportedInterfaces``, or on an interface missing any of
                its three required fields.
        """
        legacy = [k for k in PRE_1_0_FIELDS if k in data]
        if legacy:
            detail = "; ".join(f"{k}: {_PRE_1_0_ADVICE[k]}" for k in legacy)
            raise MalformedCard(
                f"card {data.get('name', '?')!r} carries pre-1.0 field(s) "
                f"{', '.join(legacy)}. In v1.0, {detail}."
            )
        entries = data.get("supportedInterfaces") or []
        if not entries:
            raise MalformedCard(
                f"card {data.get('name', '?')!r} declares no "
                "supportedInterfaces, so there is nowhere to send a task."
            )
        return cls(
            name=str(data["name"]),
            description=str(data.get("description", "")),
            version=str(data.get("version", "")),
            supported_interfaces=tuple(
                Interface.from_dict(e) for e in entries
            ),
            capabilities=dict(data.get("capabilities") or {}),
            security_schemes=dict(data.get("securitySchemes") or {}),
            skills=tuple(dict(s) for s in data.get("skills") or []),
            signature=signature,
        )


# ------------------------------------------------------------------- tasks


@dataclass
class Task:
    """An identified, stateful piece of delegated work.

    Not a request and not a message. The task is the state, which is the
    one place the "A2A is HTTP for agents" shorthand breaks.

    Args:
        id: Derived by the *caller* from its run and step, so a retry
            presents the same identity. Namespaced by tenant in the peer's
            store, never trusted as a global key.
        tenant: Resolved by the peer from the delegation, never inferred
            from the credential the call arrived with.
        skill: Which advertised skill was invoked.
        state: One of the eight wire values.
        messages: Append-only history, oldest first.
        artifacts: What the task produced.
        required_scopes: Populated only in ``auth_required``.
    """

    id: str
    tenant: str
    skill: str
    state: str = TASK_STATE_SUBMITTED
    messages: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    required_scopes: list[str] = field(default_factory=list)
    history: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        require_wire_state(self.state)
        if self.state not in INITIAL_STATES:
            raise IllegalTransition(
                f"a task cannot be created in {short_label(self.state)}. "
                f"Legal initial states: "
                f"{', '.join(sorted(INITIAL_STATES))}."
            )
        if not self.history:
            self.history = [self.state]

    @property
    def is_terminal(self) -> bool:
        """Whether this task will never move again."""
        return is_terminal(self.state)

    def move_to(self, state: str, *, note: str = "") -> Task:
        """Transition the task, refusing illegal moves. Returns ``self``.

        The note is appended *before* the state changes, which matters for
        the terminal transitions: a task that has ended accepts no new
        messages, and the peer's own closing note is part of the transition
        rather than a message sent to a finished task.

        Args:
            state: The next wire state.
            note: Optional message appended to the history for the caller.

        Raises:
            IllegalTransition: If the lifecycle does not permit the move.
        """
        advance(self.state, state)
        if note:
            self.add_message("agent", note)
        self.state = state
        self.history.append(state)
        return self

    def add_message(self, role: str, text: str) -> dict[str, Any]:
        """Append one message. Refused once the task is terminal.

        Raises:
            IllegalTransition: If the task has ended. Only the
                non-terminal states accept new messages, which is what
                makes the client a state machine too.
        """
        if self.is_terminal:
            raise IllegalTransition(
                f"task {self.id} is {short_label(self.state)}; terminal "
                "tasks do not accept new messages."
            )
        message = {"role": role, "text": text}
        self.messages.append(message)
        return message

    def add_artifact(self, name: str, content: dict[str, Any]) -> None:
        """Record one artifact the task produced."""
        self.artifacts.append({"name": name, "content": dict(content)})

    def to_wire(self) -> dict[str, Any]:
        """The ProtoJSON form a client receives. ``state`` is the enum."""
        return {
            "id": self.id,
            "tenant": self.tenant,
            "skill": self.skill,
            "state": self.state,
            "messages": [dict(m) for m in self.messages],
            "artifacts": [dict(a) for a in self.artifacts],
            "required_scopes": list(self.required_scopes),
            "history": list(self.history),
        }
