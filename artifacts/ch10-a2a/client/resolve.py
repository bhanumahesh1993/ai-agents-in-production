"""Resolving a peer, which is where the trust decision actually lives.

``resolve_peer`` is nine lines and three of them are refusals. That ratio is
the point: fetching a card is trivial, and everything that makes a card
worth acting on happens after the fetch.

Each refusal covers a failure the others do not.

**An invalid signature** means the claim is not from the party you reviewed.
TLS cannot cover this, because TLS authenticates the host you connected to
and says nothing about the document it served. A misconfigured static host,
a CDN with a poisoned cache, a redirect chain landing somewhere unexpected:
in every case the card is well-formed, correctly served, and wrong, and
there is no cryptographic difference between it and the real one.

**A hash mismatch** means the peer's advertised capabilities changed since
that review. That is a decision for a human, not for a runtime. The useful
question during an incident is not "what does the card say now" but "is what
I am calling the same thing security approved in March", and only a pinned
hash can answer it.

**An unsupported protocol version** fails loudly here instead of quietly
later. A silent version mismatch across an agent boundary does not produce a
clean error; it produces a peer returning a state name your state machine
does not have.

Resolution is a read. Retrying it is free.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from northstar_contracts import canonical_json
from transport import WELL_KNOWN_PATH, MockTransport, origin_of
from wire import PROTOCOL_VERSION, SUPPORTED_BINDINGS, AgentCard

__all__ = [
    "PEER_ID",
    "PINS_PATH",
    "PREFERRED_BINDING",
    "SUPPORTED_A2A_VERSIONS",
    "PeerRegistry",
    "Pin",
    "UntrustedPeer",
    "resolve_peer",
    "sha256_of",
    "verify_signature",
]

HERE = Path(__file__).resolve().parent

#: Deployed configuration. Not fetched, not discovered, not inferred.
PINS_PATH = HERE / "pins.json"

#: The peer this chapter delegates to.
PEER_ID = "northstar-fraud-review"

#: A2A versions this client has been tested against. A version outside this
#: set is refused rather than attempted, which is the difference between a
#: clear error and a mystery.
SUPPORTED_A2A_VERSIONS: frozenset[str] = frozenset({PROTOCOL_VERSION})

#: The binding to prefer when the peer offers several. JSON-RPC over HTTP is
#: the one you can read in a proxy log, which is why it is the right default
#: for a first integration and for any boundary crossing an organization.
PREFERRED_BINDING = "JSONRPC"


class UntrustedPeer(RuntimeError):
    """A peer this client will not talk to.

    One exception for all three refusals, because the caller's response to
    all three is the same: do not delegate, and page somebody. The message
    says which check failed.
    """


@dataclass(frozen=True)
class Pin:
    """What a security review fixed about one peer.

    Args:
        peer_id: The name the card must carry.
        url: The interface url this client will call. Deployed, not
            discovered: a card that points somewhere else is drift.
        public_key: The key the card's signature is verified against. Named
            for the asymmetric key a real deployment pins; this artifact
            verifies an HMAC stand-in with it so nothing needs a crypto
            library.
        card_hash: sha256 over the canonical JSON of the approved card body.
        required_scope: The scope the delegation must carry.
        skill: The skill this client invokes.
        reviewed_on: When the pin was set, for the audit trail.
        reviewed_by: Who set it.
    """

    peer_id: str
    url: str
    public_key: str
    card_hash: str
    required_scope: str
    skill: str
    reviewed_on: str = ""
    reviewed_by: str = ""


def load_pins(path: Path | None = None) -> dict[str, Pin]:
    """Read the deployed pin file.

    Args:
        path: Override, for tests. Defaults to :data:`PINS_PATH`.

    Returns:
        Peer id to :class:`Pin`.
    """
    data = json.loads((path or PINS_PATH).read_text())
    return {
        peer_id: Pin(
            peer_id=peer_id,
            url=str(entry["url"]),
            public_key=str(entry["public_key"]),
            card_hash=str(entry["card_hash"]),
            required_scope=str(entry["required_scope"]),
            skill=str(entry["skill"]),
            reviewed_on=str(entry.get("reviewed_on", "")),
            reviewed_by=str(entry.get("reviewed_by", "")),
        )
        for peer_id, entry in data["peers"].items()
    }


def sha256_of(card: AgentCard) -> str:
    """Hash a card's body, canonically.

    The signature is not part of the body and therefore not part of the
    hash. Otherwise re-signing an unchanged card would read as drift.
    """
    return hashlib.sha256(
        canonical_json(card.to_dict()).encode("utf-8")
    ).hexdigest()


def verify_signature(card: AgentCard, public_key: str) -> bool:
    """Whether the card's detached signature covers the body it arrived with.

    Args:
        card: The fetched card, carrying its signature.
        public_key: The pinned key.

    Returns:
        ``True`` if the signature verifies. Compared with
        :func:`hmac.compare_digest`, because a signature check that leaks
        timing is a signature check with a caveat.
    """
    if not card.signature:
        return False
    expected = hmac.new(
        public_key.encode("utf-8"),
        canonical_json(card.to_dict()).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(card.signature, expected)


class PeerRegistry:
    """The pinned peers, and the transport used to fetch their cards.

    Internally, cards are resolved through a registry rather than by probing
    hosts, which is also where allowlisting and review belong. A peer absent
    from :attr:`pinned` is not an unknown peer to be looked up; it is a peer
    nobody reviewed.

    Args:
        transport: How cards are fetched. In-process here.
        pinned: Peer id to pin. Defaults to the deployed pin file.
    """

    def __init__(
        self,
        transport: MockTransport,
        pinned: dict[str, Pin] | None = None,
    ) -> None:
        self.transport = transport
        self.pinned: dict[str, Pin] = (
            dict(pinned) if pinned is not None else load_pins()
        )

    def fetch(self, url: str) -> AgentCard:
        """GET the card published at ``url``'s well-known path."""
        return self.transport.fetch_card(origin_of(url) + WELL_KNOWN_PATH)

    def card_url(self, url: str) -> str:
        """Where the card for an interface url is published."""
        return origin_of(url) + WELL_KNOWN_PATH


def resolve_peer(peer_id: str, registry: PeerRegistry) -> AgentCard:
    """Resolve a pinned, signed peer card. Read-only; safe to retry.

    Args:
        peer_id: A peer in the registry's pin file.
        registry: The pinned allowlist and the transport.

    Returns:
        The card, once it is known to be from the reviewed party, unchanged
        since that review, and speaking a version this client handles.

    Raises:
        UntrustedPeer: On an unreviewed peer, an invalid signature, a hash
            that drifted from the pin, a card naming a different agent, a
            url that is not the pinned one, or a card offering no interface
            at a supported version.
    """
    pin = registry.pinned.get(peer_id)      # deployed config, not runtime
    if pin is None:
        known = ", ".join(sorted(registry.pinned)) or "(none pinned)"
        raise UntrustedPeer(
            f"{peer_id}: no pin. Reviewed peers: {known}."
        )
    card = registry.fetch(pin.url)          # mock transport in demo mode
    if not verify_signature(card, pin.public_key):
        raise UntrustedPeer(f"{peer_id}: card signature invalid")
    if sha256_of(card) != pin.card_hash:
        raise UntrustedPeer(f"{peer_id}: card drifted from pin")
    if card.protocol_version not in SUPPORTED_A2A_VERSIONS:
        raise UntrustedPeer(f"{peer_id}: unsupported A2A version")
    _check_pin_details(pin, card)
    return card


def _check_pin_details(pin: Pin, card: AgentCard) -> None:
    """The rest of the pin: identity, endpoint, binding, and skill.

    Separated from :func:`resolve_peer` so the three refusals the chapter
    excerpts stay readable, not because these matter less. A verified
    signature over a card that names a different agent, points at a
    different host, or no longer advertises the skill you call is a verified
    signature over something you should not act on.

    Raises:
        UntrustedPeer: On any mismatch.
    """
    if card.name != pin.peer_id:
        raise UntrustedPeer(
            f"{pin.peer_id}: card names {card.name!r}"
        )
    interface = card.interface_for(
        binding=PREFERRED_BINDING, versions=SUPPORTED_A2A_VERSIONS
    )
    if interface is None:
        offered = ", ".join(
            f"{i.protocol_binding}/{i.protocol_version}"
            for i in card.supported_interfaces
        )
        raise UntrustedPeer(
            f"{pin.peer_id}: no {PREFERRED_BINDING} interface at a "
            f"supported version. Offered: {offered}. "
            f"Bindings this client knows: {', '.join(SUPPORTED_BINDINGS)}."
        )
    if interface.url != pin.url:
        raise UntrustedPeer(
            f"{pin.peer_id}: card points at {interface.url!r}, pinned "
            f"{pin.url!r}"
        )
    if card.skill_by_id(pin.skill) is None:
        advertised = ", ".join(str(s.get("id")) for s in card.skills)
        raise UntrustedPeer(
            f"{pin.peer_id}: card no longer advertises {pin.skill!r}. "
            f"Advertised: {advertised or '(none)'}."
        )
    scopes = _declared_scopes(card)
    if pin.required_scope not in scopes:
        raise UntrustedPeer(
            f"{pin.peer_id}: card declares scopes {sorted(scopes)}, which "
            f"do not include the pinned {pin.required_scope!r}"
        )


def _declared_scopes(card: AgentCard) -> set[str]:
    """Every scope any of the card's security schemes asks for."""
    found: set[str] = set()
    for scheme in card.security_schemes.values():
        if isinstance(scheme, dict):
            found.update(str(s) for s in scheme.get("scopes") or ())
    return found


def skill_description(card: AgentCard, skill_id: str) -> str:
    """The text a delegating model would read, isolated so it is visible.

    A remote card's skill descriptions go into your agent's context as text.
    That is third-party content in a privileged position in your prompt, and
    it belongs in the injection surface Chapter 18 maps. This function
    exists so that the one place untrusted text enters the client is a
    function call somebody can grep for.
    """
    skill: dict[str, Any] | None = card.skill_by_id(skill_id)
    return str((skill or {}).get("description", ""))
