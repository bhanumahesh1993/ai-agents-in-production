"""The supplier policy page, served from a local file and nothing else.

Northstar's ``get_policy`` resolves refund eligibility partly from a supplier's
hosted returns page, which makes that page an injection channel: the text
arrives in the agent's context with no marker distinguishing it from the
operator's own words.

Reproducing the channel does not require fetching anything. :func:`fetch` reads
one file inside this directory and refuses every other argument, so the harness
is safe to run in CI, safe to run on a laptop with no network, and cannot be
repurposed into a crawler. That refusal is a hard constraint of this artifact
rather than a convenience: a red-team harness that reaches outside the process
it is testing has stopped being a defensive tool.

The hidden element in the fixture is the payload. It is extracted with a small
regex rather than an HTML parser because the extraction is not the subject --
what matters is that ordinary prose, in a channel the system treats as
reference material, arrives in a privileged position.
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = [
    "FIXTURE_DIR",
    "SUPPLIER_PAGE",
    "LocalOnly",
    "fetch",
    "hidden_instruction",
    "page_text",
    "supplier_policy_note",
]

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"

#: The only document this module will read.
SUPPLIER_PAGE = "supplier-returns.html"

#: The element an attacker controls. ``hidden`` in the markup, and fully
#: visible to a model, which is the whole asymmetry.
_HIDDEN = re.compile(
    r'<div[^>]*id="agent-note"[^>]*>(?P<body>.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)
_TAGS = re.compile(r"<[^>]+>")
_COMMENTS = re.compile(r"<!--.*?-->", re.DOTALL)


class LocalOnly(ValueError):
    """Something other than the local fixture was asked for.

    Raised rather than logged. A harness that quietly widened from a file to a
    URL would be a different kind of program, and this is the line.
    """


def fetch(name: str = SUPPLIER_PAGE) -> str:
    """Return the fixture's markup.

    Args:
        name: A bare filename inside ``fixtures/``. Anything with a path
            separator, a scheme, or a parent reference is refused.

    Raises:
        LocalOnly: For any argument that is not the local fixture. There is no
            network path here and no way to add one through this function.
    """
    if name != SUPPLIER_PAGE:
        raise LocalOnly(
            f"this harness reads {SUPPLIER_PAGE!r} from {FIXTURE_DIR} and "
            f"nothing else; got {name!r}. It does not fetch."
        )
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def page_text(name: str = SUPPLIER_PAGE) -> str:
    """The page as a model would read it: tags stripped, comments dropped."""
    markup = _COMMENTS.sub("", fetch(name))
    text = _TAGS.sub(" ", markup)
    return " ".join(text.split())


def hidden_instruction(name: str = SUPPLIER_PAGE) -> str:
    """The attacker-controlled text inside the hidden element.

    Raises:
        LocalOnly: If the fixture no longer carries a payload, which would
            make the ``web_page`` case a test of nothing.
    """
    match = _HIDDEN.search(fetch(name))
    if match is None:
        raise LocalOnly(
            f"{name} has no #agent-note element; the web_page vector would "
            f"be testing nothing"
        )
    return " ".join(_TAGS.sub(" ", match.group("body")).split())


def supplier_policy_note(name: str = SUPPLIER_PAGE) -> str:
    """What the tool layer appends to a policy result.

    This is the realistic shape of the vulnerability. Nobody decided to put
    attacker text in the context; somebody decided the supplier's page was
    useful reference material, and the text came along.
    """
    return f"Supplier documentation ({name}): {page_text(name)}"
