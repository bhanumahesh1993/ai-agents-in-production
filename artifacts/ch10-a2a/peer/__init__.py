"""The fraud review agent: a peer on a different runtime.

Nothing in ``client/`` imports anything from here, and nothing here imports
anything from ``client/``. That is the point of the chapter. The two halves
share ``wire.py`` and a mounted url, and if either half could reach into
the other the artifact would be demonstrating an in-process call with extra
steps.
"""
