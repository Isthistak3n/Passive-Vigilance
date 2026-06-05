"""ScoringEngine interface conformance tests."""

import inspect

import pytest

from modules.scoring_engine import ScoringEngine
from modules.persistence import PersistenceEngine, MobileScoring
from modules.fixed_scoring import FixedScoring


def test_abc_cannot_be_instantiated():
    with pytest.raises(TypeError):
        ScoringEngine()


def test_both_engines_are_scoring_engines():
    assert issubclass(PersistenceEngine, ScoringEngine)
    assert issubclass(FixedScoring, ScoringEngine)


def test_mobile_scoring_is_persistence_engine_alias():
    assert MobileScoring is PersistenceEngine


def test_update_signature_matches_across_engines():
    """update() must accept devices positionally and gps_fix by keyword.

    This is the orchestrator call site contract (orchestrator.py:305):
    ``engine.update(devices, gps_fix=...)``. PersistenceEngine keeps its existing
    positional-or-keyword gps_fix (unchanged); FixedScoring uses keyword-only.
    Both are callable the same way.
    """
    for cls in (PersistenceEngine, FixedScoring):
        sig = inspect.signature(cls.update)
        params = list(sig.parameters)
        assert params[1] == "devices", cls
        gps = sig.parameters.get("gps_fix")
        assert gps is not None, cls
        assert gps.default is None, cls
        assert gps.kind in (
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ), cls


def test_mobile_status_returns_dict():
    status = PersistenceEngine().status()
    assert isinstance(status, dict)
    assert status["mode"] == "mobile"


def test_fixed_status_returns_dict():
    fs = FixedScoring(db_path=":memory:", baseline_hours=72)
    status = fs.status()
    assert isinstance(status, dict)
    assert status["mode"] == "fixed"
