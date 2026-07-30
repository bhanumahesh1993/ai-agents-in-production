"""The Northstar read server: one registry, two transports, one auth model.

Everything here is a package under ``artifacts/ch09-mcp/``, which the
repository's root ``conftest.py`` puts on ``sys.path``. That is what makes
plain ``import server.auth`` resolve both from ``python
artifacts/ch09-mcp/demo.py`` at the repository root and from one ``pytest``
run over the whole ``artifacts/`` tree.

Layout:

* ``expose``    -- render a ``ToolRegistry`` as MCP descriptors, refuse writes
* ``auth``      -- the four fail-closed token checks
* ``authserver``-- a mock OAuth 2.1 authorization server and its discovery
* ``readserver``-- the transport-free MCP server object (JSON-RPC in, out)
* ``transports``-- the stdio pipe and the Streamable HTTP endpoint, mocked
* ``drift``     -- a third-party server whose descriptions change under you
"""
