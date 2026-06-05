"""Tests for NODE_MODE resolution and fail-loud behavior (design 2.1)."""

import pytest

from main import resolve_node_mode


class TestResolveNodeMode:
    def test_env_valid_mobile(self):
        assert resolve_node_mode("mobile", None) == "mobile"

    def test_env_valid_fixed(self):
        assert resolve_node_mode("fixed", None) == "fixed"

    def test_env_case_and_whitespace_insensitive(self):
        assert resolve_node_mode("  FIXED  ", None) == "fixed"

    def test_env_invalid_aborts(self):
        with pytest.raises(SystemExit):
            resolve_node_mode("wardrive", None)

    def test_env_invalid_aborts_even_with_valid_flag(self):
        # A present-but-invalid NODE_MODE is a misconfiguration; do NOT silently
        # fall through to the flag — fail loud.
        with pytest.raises(SystemExit):
            resolve_node_mode("garbage", "mobile")

    def test_flag_used_when_env_unset(self):
        assert resolve_node_mode(None, "fixed") == "fixed"

    def test_flag_used_when_env_empty(self):
        assert resolve_node_mode("", "mobile") == "mobile"

    def test_env_beats_flag(self):
        # .env wins over the CLI flag.
        assert resolve_node_mode("mobile", "fixed") == "mobile"

    def test_flag_invalid_aborts(self):
        with pytest.raises(SystemExit):
            resolve_node_mode(None, "bogus")

    def test_unset_everywhere_aborts(self):
        with pytest.raises(SystemExit):
            resolve_node_mode(None, None)
