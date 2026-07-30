"""The client half: negotiate, discover, pin.

Three modules, one job each:

* ``session``   -- the connection, and the two transports as a client sees
  them, including the walk from a 401 to an audience-bound token
* ``negotiate`` -- the two-part admission check, revision *and* capability
* ``pins``      -- the tool surface as a supply-chain artifact
"""
