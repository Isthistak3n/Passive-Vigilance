"""Tests for modules/config.py — startup validation of the environment surface."""

import logging
import re
from pathlib import Path

from modules import config
from modules.config import ERROR, WARNING, validate_environment


def _errors(findings):
    return [f for f in findings if f.severity == ERROR]


def _warnings(findings):
    return [f for f in findings if f.severity == WARNING]


# A minimal healthy environment (NODE_MODE is the one required variable).
_CLEAN = {"NODE_MODE": "fixed"}


def _validate(extra=None, dotenv_path="does-not-exist"):
    """Validate a controlled environment; the typo check is off unless a
    dotenv_path is supplied (never the node's real .env)."""
    env = dict(_CLEAN)
    if extra:
        env.update(extra)
    return validate_environment(env=env, dotenv_path=dotenv_path)


# ---------------------------------------------------------------------------
# The drift guard: the registry must cover every variable the code reads
# ---------------------------------------------------------------------------


def test_registry_covers_every_env_var_the_code_reads():
    """Every os.getenv/os.environ.get name in the codebase must be declared in
    the registry — otherwise a new knob ships unvalidated and the typo checker
    would flag its legitimate use."""
    root = Path(__file__).resolve().parent.parent
    pattern = re.compile(
        r"os\.(?:getenv|environ\.get)\(\s*[\"']([A-Z][A-Z0-9_]+)[\"']")
    read_vars = set()
    for path in (list((root / "modules").glob("*.py")) + [root / "main.py"]
                 + list((root / "gui").glob("*.py"))
                 + list((root / "scripts").glob("*.py"))):
        read_vars |= set(pattern.findall(path.read_text()))
    missing = sorted(read_vars - set(config.REGISTRY))
    assert not missing, f"env vars read in code but absent from REGISTRY: {missing}"


def test_registry_covers_every_documented_env_key():
    """Every key shipped in .env.example must be recognised, or a fresh install
    would emit typo warnings for the defaults it was given."""
    root = Path(__file__).resolve().parent.parent
    documented = set(re.findall(r"^([A-Z][A-Z0-9_]+)=",
                                (root / ".env.example").read_text(), re.M))
    unknown = sorted(k for k in documented - set(config.REGISTRY)
                     if not k.startswith(config._FOREIGN_KEY_PREFIXES))
    assert not unknown, f".env.example keys absent from REGISTRY: {unknown}"


# ---------------------------------------------------------------------------
# Per-variable checks
# ---------------------------------------------------------------------------


class TestValueChecks:
    def test_clean_environment_has_no_findings(self):
        assert _validate() == []

    def test_absent_and_empty_values_are_skipped(self):
        assert _validate({"NTFY_TOPIC": ""}) == []

    def test_garbage_integer_is_an_error(self):
        findings = _validate({"KISMET_POLL_INTERVAL_SECONDS": "banana"})
        assert any("not an integer" in f.message for f in _errors(findings))

    def test_garbage_float_is_an_error(self):
        findings = _validate({"WIFI_ALERT_MIN_SCORE": "high"})
        assert any("not a number" in f.message for f in _errors(findings))

    def test_out_of_range_score_is_an_error(self):
        findings = _validate({"WIFI_ALERT_MIN_SCORE": "1.5"})
        assert any("above maximum" in f.message for f in _errors(findings))

    def test_port_zero_is_an_error(self):
        findings = _validate({"GUI_PORT": "0"})
        assert any(f.var == "GUI_PORT" for f in _errors(findings))

    def test_rollup_hour_24_is_an_error(self):
        findings = _validate({"ENTITY_ROLLUP_HOUR_UTC": "24"})
        assert any(f.var == "ENTITY_ROLLUP_HOUR_UTC" for f in _errors(findings))

    def test_bad_choice_is_an_error(self):
        findings = _validate({"NODE_MODE": "hybrid"})
        assert any("fixed" in f.message for f in _errors(findings))

    def test_choices_are_case_insensitive(self):
        assert _validate({"NODE_MODE": "Fixed", "LOG_LEVEL": "debug"}) == []

    def test_unrecognised_boolean_warns_it_means_false(self):
        findings = _validate({"GUI_ENABLED": "ture"})
        warns = _warnings(findings)
        assert any("treated as FALSE" in f.message for f in warns)

    def test_positive_dbm_threshold_is_an_error(self):
        findings = _validate({"EGREGIOUS_SIGNAL_DBM": "40"})
        assert any("negative dBm" in f.message for f in _errors(findings))

    def test_negative_dbm_threshold_is_fine(self):
        assert _validate({"EGREGIOUS_SIGNAL_DBM": "-40"}) == []


class TestSpecialFormats:
    def test_valid_sdr_cycle_slices(self):
        assert _validate({"SDR_CYCLE_SLICES": "adsb:840,ais:60"}) == []

    def test_unknown_band_in_cycle_is_an_error(self):
        findings = _validate({"SDR_CYCLE_SLICES": "adsb:840,fm:30"})
        assert any("unknown band" in f.message for f in _errors(findings))

    def test_malformed_slice_is_an_error(self):
        findings = _validate({"SDR_CYCLE_SLICES": "adsb"})
        assert any("name:seconds" in f.message for f in _errors(findings))

    def test_night_hours_wrap_is_valid(self):
        assert _validate({"SURVEY_NIGHT_HOURS": "22-06"}) == []

    def test_night_hours_25_is_an_error(self):
        findings = _validate({"SURVEY_NIGHT_HOURS": "25-06"})
        assert any(f.var == "SURVEY_NIGHT_HOURS" for f in _errors(findings))

    def test_non_url_webhook_is_an_error(self):
        findings = _validate({"DISCORD_WEBHOOK_URL": "discord.com/api/webhooks/x"})
        assert any("http" in f.message for f in _errors(findings))


# ---------------------------------------------------------------------------
# Cross-field checks
# ---------------------------------------------------------------------------


class TestCrossFieldChecks:
    def test_missing_node_mode_is_an_error(self):
        findings = validate_environment(env={}, dotenv_path="does-not-exist")
        assert any(f.var == "NODE_MODE" for f in _errors(findings))

    def test_backend_without_credentials_is_an_error_naming_the_slots(self):
        findings = _validate({"ALERT_BACKEND": "telegram"})
        vars_flagged = {f.var for f in _errors(findings)}
        assert {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"} <= vars_flagged

    def test_configured_backend_is_clean(self):
        findings = _validate({
            "ALERT_BACKEND": "telegram",
            "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c",
        })
        assert findings == []

    def test_console_backend_needs_no_credentials(self):
        assert _validate({"ALERT_BACKEND": "console"}) == []

    def test_active_window_zero_warns_about_the_smearing_footgun(self):
        findings = _validate({"KISMET_ACTIVE_WINDOW_SECONDS": "0"})
        assert any("#232" in f.message for f in _warnings(findings))


# ---------------------------------------------------------------------------
# Typo detection against .env
# ---------------------------------------------------------------------------


class TestTypoCheck:
    def test_typoed_key_warns_with_a_suggestion(self, tmp_path):
        envfile = tmp_path / ".env"
        envfile.write_text("NODE_MODE=fixed\nWIFI_ALERT_MIN_SCROE=0.8\n")
        findings = validate_environment(env=_CLEAN, dotenv_path=str(envfile))
        warns = _warnings(findings)
        assert any(f.var == "WIFI_ALERT_MIN_SCROE" for f in warns)
        assert any("WIFI_ALERT_MIN_SCORE" in f.message for f in warns)

    def test_installer_keys_are_not_flagged(self, tmp_path):
        envfile = tmp_path / ".env"
        envfile.write_text("NODE_MODE=fixed\nINSTALL_AIS=true\n")
        findings = validate_environment(env=_CLEAN, dotenv_path=str(envfile))
        assert findings == []

    def test_no_dotenv_file_skips_the_check(self):
        assert _validate(dotenv_path="definitely/not/here") == []


# ---------------------------------------------------------------------------
# report()
# ---------------------------------------------------------------------------


class TestReport:
    def test_clean_report_is_one_info_line(self, caplog):
        with caplog.at_level(logging.INFO, logger="modules.config"):
            config.report([])
        assert "no findings" in caplog.text

    def test_errors_are_logged_at_error_level(self, caplog):
        findings = [config.Finding(ERROR, "GUI_PORT", "0 is below minimum 1")]
        with caplog.at_level(logging.INFO, logger="modules.config"):
            config.report(findings)
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("GUI_PORT" in r.getMessage() for r in errors)
        assert "Startup continues" in caplog.text

    def test_warnings_are_logged_at_warning_level(self, caplog):
        findings = [config.Finding(WARNING, "GUI_ENABLED", "odd boolean")]
        with caplog.at_level(logging.INFO, logger="modules.config"):
            config.report(findings)
        warns = [r for r in caplog.records
                 if logging.WARNING <= r.levelno < logging.ERROR]
        assert any("GUI_ENABLED" in r.getMessage() for r in warns)
