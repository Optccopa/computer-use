"""Configuration parsing and validation."""

from __future__ import annotations

import pytest

from cufast.config import Config


class TestEnvParsing:
    def test_defaults_when_unset(self, monkeypatch):
        for name in (
            "CUFAST_DISPLAY", "CUFAST_MAX_WIDTH", "CUFAST_MAX_HEIGHT",
            "CUFAST_JPEG_QUALITY", "CUFAST_DRAW_CURSOR", "CUFAST_CAPTURE_TIMEOUT_MS",
            "CUFAST_SETTLE_MS",
        ):
            monkeypatch.delenv(name, raising=False)
        cfg = Config.from_env()
        assert (cfg.max_width, cfg.max_height) == (1024, 768)
        assert cfg.display_index == 0
        assert cfg.draw_cursor is True

    def test_empty_string_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("CUFAST_MAX_WIDTH", "   ")
        assert Config.from_env().max_width == 1024

    def test_reads_values(self, monkeypatch):
        monkeypatch.setenv("CUFAST_DISPLAY", "1")
        monkeypatch.setenv("CUFAST_MAX_WIDTH", "1280")
        monkeypatch.setenv("CUFAST_MAX_HEIGHT", "720")
        monkeypatch.setenv("CUFAST_JPEG_QUALITY", "0.6")
        cfg = Config.from_env()
        assert (cfg.display_index, cfg.max_width, cfg.max_height) == (1, 1280, 720)
        assert cfg.jpeg_quality == pytest.approx(0.6)

    def test_integer_typo_is_reported(self, monkeypatch):
        monkeypatch.setenv("CUFAST_MAX_WIDTH", "wide")
        with pytest.raises(ValueError, match="must be an integer"):
            Config.from_env()

    def test_float_typo_is_reported(self, monkeypatch):
        monkeypatch.setenv("CUFAST_JPEG_QUALITY", "high")
        with pytest.raises(ValueError, match="must be a number"):
            Config.from_env()

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy_booleans(self, monkeypatch, value):
        monkeypatch.setenv("CUFAST_DRAW_CURSOR", value)
        assert Config.from_env().draw_cursor is True

    @pytest.mark.parametrize("value", ["0", "false", "No", "off"])
    def test_falsy_booleans(self, monkeypatch, value):
        monkeypatch.setenv("CUFAST_DRAW_CURSOR", value)
        assert Config.from_env().draw_cursor is False

    @pytest.mark.parametrize("value", ["tru", "enable", "banana"])
    def test_boolean_typo_is_reported(self, monkeypatch, value):
        # Every other variable reports a typo; silently reading "tru" as false would
        # quietly disable the cursor overlay with no way to notice.
        monkeypatch.setenv("CUFAST_DRAW_CURSOR", value)
        with pytest.raises(ValueError, match="must be true or false"):
            Config.from_env()


class TestValidation:
    def test_valid_config_constructs(self):
        Config(max_width=1280, max_height=720, jpeg_quality=0.8)

    def test_rejects_non_positive_box(self):
        with pytest.raises(ValueError, match="must be positive"):
            Config(max_width=0)
        with pytest.raises(ValueError):
            Config(max_height=-10)

    def test_long_edge_ceiling(self):
        # Above 2576 the API rescales the image itself and the coordinates we hand
        # back stop matching the ones the model sees.
        Config(max_width=2576, max_height=2576)
        with pytest.raises(ValueError, match="2576"):
            Config(max_width=2577)
        with pytest.raises(ValueError, match="2576"):
            Config(max_height=4000)

    def test_rejects_bad_quality(self):
        with pytest.raises(ValueError, match="0.01 and 1.0"):
            Config(jpeg_quality=99.0)
        with pytest.raises(ValueError):
            Config(jpeg_quality=0.0)

    def test_rejects_negative_numbers(self):
        with pytest.raises(ValueError):
            Config(display_index=-5)
        with pytest.raises(ValueError):
            Config(settle_ms=-1000)
        with pytest.raises(ValueError):
            Config(capture_timeout_ms=-1)

    def test_validation_runs_on_direct_construction(self):
        # build_server() accepts a caller-supplied Config; an unvalidated one would
        # reach the native layer and desynchronise coordinates with no error.
        with pytest.raises(ValueError):
            Config(max_width=4000, max_height=3000)
