"""Unit tests for core.settings — load_settings and data-classes."""

from __future__ import annotations

import tempfile
from pathlib import Path

import yaml

from core.settings import Settings, load_settings


class TestLoadSettings:
    def test_loads_minimal_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "app.yaml"
            config_path.write_text(
                yaml.dump({
                    "app": {"timezone": "UTC"},
                    "tickflow": {},
                    "logging": {},
                }),
                encoding="utf-8",
            )
            settings = load_settings(config_path)
            assert isinstance(settings, Settings)
            assert settings.app.timezone == "UTC"

    def test_default_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "app.yaml"
            config_path.write_text(
                yaml.dump({"app": {}, "tickflow": {}, "logging": {}}),
                encoding="utf-8",
            )
            settings = load_settings(config_path)
            assert settings.app.update_time_after_close == "16:30"
            assert settings.app.timezone == "Asia/Shanghai"
            assert settings.logging.level == "INFO"

    def test_app_settings_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "app.yaml"
            config_path.write_text(
                yaml.dump({"app": {}, "tickflow": {}, "logging": {}}),
                encoding="utf-8",
            )
            settings = load_settings(config_path)
            assert settings.app.daily_update_max_retries == 2
            assert settings.app.data_provider_priority == ["tickflow"]
