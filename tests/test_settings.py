"""Dashboard settings: checked, saved to settings.yaml, applied in place, loaded on start."""

import pytest
import yaml

from guardian import settings
from guardian.config import Config, StorageConfig


def make(tmp_path):
    return Config(storage=StorageConfig(data_dir=tmp_path / "data"))


def test_save_applies_in_place_and_persists(tmp_path):
    cfg = make(tmp_path)
    held = cfg.autopause  # a part of the app keeps this object
    changed = settings.save(cfg, {"autopause.grace_s": 20, "camera.keepalive": "always",
                                  "autopause.triggers": ["bed_not_clear"], "toolchanger.timeout_s": None})
    assert set(changed) == {"autopause.grace_s", "camera.keepalive", "autopause.triggers"}
    assert held.grace_s == 20 and held.triggers == ["bed_not_clear"]
    data = yaml.safe_load(settings.settings_path(cfg).read_text(encoding="utf-8"))
    assert data["autopause"]["grace_s"] == 20 and data["camera"]["keepalive"] == "always"
    fresh = settings.load_overrides(make(tmp_path))
    assert fresh.autopause.grace_s == 20 and fresh.camera.keepalive == "always"
    assert fresh.autopause.triggers == ["bed_not_clear"]


def test_bad_values_are_refused_and_nothing_changes(tmp_path):
    cfg = make(tmp_path)
    for bad in ({"camera.keepalive": "sometimes"}, {"autopause.grace_s": -1},
                {"autopause.triggers": ["klipper_error_response"]},
                {"blackbox.pre_trigger_s": 400},  # pre + post would pass max_incident_s
                {"notifications.email.enabled": True},  # no address
                {"notifications.email.to_addrs": ["not an address"]}):
        with pytest.raises(settings.SettingsError):
            settings.save(cfg, bad)
    with pytest.raises(settings.SettingsError, match="cannot be changed"):
        settings.save(cfg, {"printer.host": "1.2.3.4"})
    with pytest.raises(settings.SettingsError):
        settings.save(cfg, {"notifications.email.api_key_env": "X"})
    assert cfg.camera.keepalive == "printing" and not settings.settings_path(cfg).exists()


def test_email_on_with_address(tmp_path):
    cfg = make(tmp_path)
    settings.save(cfg, {"notifications.email.enabled": True, "notifications.email.to_addrs": ["me@example.com"]})
    assert cfg.notifications.email.enabled and cfg.notifications.email.to_addrs == ["me@example.com"]


def test_broken_file_is_ignored(tmp_path):
    cfg = make(tmp_path)
    path = settings.settings_path(cfg)
    path.parent.mkdir(parents=True)
    path.write_text("camera: {keepalive: sometimes}\n", encoding="utf-8")
    assert settings.load_overrides(cfg).camera.keepalive == "printing"
    path.write_text(": : not yaml [", encoding="utf-8")
    assert settings.load_overrides(cfg).camera.keepalive == "printing"


def test_unknown_keys_in_file_are_ignored(tmp_path):
    cfg = make(tmp_path)
    path = settings.settings_path(cfg)
    path.parent.mkdir(parents=True)
    path.write_text("printer: {host: 6.6.6.6}\nautopause: {enabled: false}\n", encoding="utf-8")
    cfg = settings.load_overrides(cfg)
    assert cfg.printer.host != "6.6.6.6" and cfg.autopause.enabled is False


def test_current_lists_every_field(tmp_path):
    values = settings.current(make(tmp_path))
    assert set(values) == settings.KEYS
