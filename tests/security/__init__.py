"""Security tests for the schema layer: what a schema may read, and what it may not.

The core rule these tests exist to pin: a schema is compiled with the policy stated
explicitly (``allow='sandbox'``, ``defuse='always'``), so an include that escapes the
schema's own directory is refused **before** the file is opened, remote resources are
never fetched, and XML entities are not resolved.

Each file covers one boundary:

* ``test_xsd_sandbox.py`` -- escaping the schema's directory
* ``test_xsd_include.py``  -- the harmless case, which must keep working
* ``test_xsd_filesystem.py`` -- absolute paths, drive letters, and other escape routes
* ``test_xsd_network.py``  -- remote resources, blocked before any connection
* ``test_xsd_entities.py`` -- DOCTYPE entities, and the behaviour change that refutes
   the expectation that a schema which worked still works
"""
