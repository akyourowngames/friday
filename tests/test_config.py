import json

from ares.models import AppConfig


def test_load_config_returns_defaults_for_corrupt_json(tmp_path, monkeypatch):
    from ares import config as config_module

    bad_config = tmp_path / "config.json"
    bad_config.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_PATH", bad_config)

    loaded = config_module.load_config()

    assert isinstance(loaded, AppConfig)
    assert loaded.model
