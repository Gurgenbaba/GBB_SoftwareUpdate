"""MultiStageUninstallEngine._ulog formatting (must not raise)."""

from unittest.mock import MagicMock

from app.config import SOFTWARE_BY_KEY
from app.uninstall_engine import MultiStageUninstallEngine


def _engine() -> MultiStageUninstallEngine:
    return MultiStageUninstallEngine(MagicMock(), MagicMock(), MagicMock(), software_providers={})


def test_ulog_plain_message() -> None:
    eng = _engine()
    sw = SOFTWARE_BY_KEY["firefox"]
    eng._ulog(sw, "plain")
    eng.logger.info.assert_called_once_with("[UNINSTALL] %s: %s", sw.display_name, "plain")


def test_ulog_percent_args() -> None:
    eng = _engine()
    sw = SOFTWARE_BY_KEY["firefox"]
    eng._ulog(sw, "x=%s y=%s", 1, "two")
    eng.logger.info.assert_called_once_with("[UNINSTALL] %s: %s", sw.display_name, "x=1 y=two")


def test_ulog_bad_format_does_not_raise() -> None:
    eng = _engine()
    sw = SOFTWARE_BY_KEY["firefox"]
    eng._ulog(sw, "only %s", 1, 2, 3)
    call = eng.logger.info.call_args[0]
    assert call[0] == "[UNINSTALL] %s: %s"
    assert call[1] == sw.display_name
    assert "only %s" in call[2]
    assert "unformatted_args" in call[2]
