"""The support agent's half of the A2A connection.

``resolve.py`` is the trust policy, ``escalate.py`` is the delegation, and
``follow.py`` is the client-side state machine. Nothing here imports
``peer/``: everything this side knows about the fraud review agent, it
learned from a pinned card.
"""
