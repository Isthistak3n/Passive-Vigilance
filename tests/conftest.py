import sys
import os

import pytest

# Snapshot the environment at conftest import — this runs before any test module is
# collected, so before an app module's import-time ``load_dotenv()`` can inject the
# node's real ``.env`` (notably ``GUI_TOKEN``) into the process environment. Restoring
# to this baseline around every test keeps each test hermetic regardless of collection
# order. Without it, collecting a test that imports e.g. ``modules.persistence`` leaks
# the node's ``GUI_TOKEN`` into ``os.environ``, which flipped open-access GUI tests to
# 401 depending on which files ran together (a cross-file, node-only flake — CI's .env
# has no token so it never saw it).
_BASELINE_ENV = dict(os.environ)


def _restore_baseline_env():
    # Drop keys that leaked in since baseline (e.g. .env via an import-time
    # load_dotenv), but leave pytest's own per-test runtime vars (PYTEST_*) alone so we
    # don't fight its bookkeeping. Then restore any baseline value a test changed.
    for key in list(os.environ):
        if key not in _BASELINE_ENV and not key.startswith("PYTEST_"):
            del os.environ[key]
    for key, value in _BASELINE_ENV.items():
        if os.environ.get(key) != value:
            os.environ[key] = value


@pytest.fixture(autouse=True)
def _isolate_environ():
    """Reset ``os.environ`` to the pre-collection baseline before and after each test."""
    _restore_baseline_env()
    yield
    _restore_baseline_env()


# Add system gps module path for CI environments where setup-python
# creates a virtualenv that cannot see system packages.
# Only insert the path if the gps module is not already importable.
try:
    import gps
except ImportError:
    # Find and add only the gps module, not all of dist-packages
    _gps_candidates = [
        '/usr/lib/python3/dist-packages',
        '/usr/lib/python3.11/dist-packages',
        '/usr/lib/python3.12/dist-packages',
        '/usr/lib/python3.13/dist-packages',
    ]
    for _path in _gps_candidates:
        _gps_init = os.path.join(_path, 'gps', '__init__.py')
        if os.path.exists(_gps_init):
            # Append rather than insert so pip packages (numpy, etc.)
            # remain earlier on sys.path and are not shadowed by
            # system dist-packages copies.
            sys.path.append(_path)
            break
