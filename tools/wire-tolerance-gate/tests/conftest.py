"""Keep pytest from collecting the fixture tree as real tests.

``fixtures/repo/apps/fake/tests/test_something.py`` exists specifically to
prove the gate exempts test-shaped paths; it is not a real test (its
``Widget`` stand-in has no actual ``model_validate``) and must never be
imported or executed by pytest itself.
"""

from __future__ import annotations

collect_ignore_glob = ["fixtures/**"]
