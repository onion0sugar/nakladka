"""Testy konfiguracji (.env → load_config)."""

import pytest

from config import ConfigError, load_config

REQUIRED = {
    "MSSQL_SERVER": "192.168.24.22\\SERWISKOPB2B",
    "MSSQL_DATABASE": "SerwisKop_Magazyn",
    "MSSQL_USERNAME": "serwiskop-ro",
    "MSSQL_PASSWORD": "haslo",
    "NTFY_TOPIC": "test-topic",
    "SUPERVISOR_TOPIC": "supervisor-topic",
    "RESPONSE_TOPIC": "response-topic",
}


def _set_env(monkeypatch, values: dict[str, str]) -> None:
    for key in REQUIRED:
        monkeypatch.delenv(key, raising=False)
    for key in values:
        monkeypatch.setenv(key, values[key])


def test_load_config_reads_required_vars(monkeypatch):
    _set_env(monkeypatch, REQUIRED)
    cfg = load_config()
    assert cfg.mssql_server == "192.168.24.22\\SERWISKOPB2B"
    assert cfg.mssql_database == "SerwisKop_Magazyn"
    assert cfg.mssql_username == "serwiskop-ro"
    assert cfg.ntfy_topic == "test-topic"


def test_missing_required_var_raises(monkeypatch):
    values = dict(REQUIRED)
    del values["MSSQL_SERVER"]
    _set_env(monkeypatch, values)
    with pytest.raises(ConfigError, match="MSSQL_SERVER"):
        load_config()


def test_defaults(monkeypatch):
    _set_env(monkeypatch, REQUIRED)
    cfg = load_config()
    assert cfg.mssql_port == 1433
    assert cfg.mssql_encrypt == "yes"
    assert cfg.mssql_trust_server_certificate == "yes"
    assert cfg.poll_interval == 10
    assert cfg.announce_interval == 16
    assert cfg.send_text is True
    assert cfg.ntfy_server == "https://ntfy.sh"
    assert cfg.ntfy_token == ""
    assert cfg.ntfy_priority == "default"


def test_flags_parsed(monkeypatch):
    values = dict(REQUIRED)
    values.update(
        {
            "SEND_TEXT": "false",
            "POLL_INTERVAL": "10",
            "ANNOUNCE_INTERVAL": "45",
        }
    )
    _set_env(monkeypatch, values)
    cfg = load_config()
    assert cfg.send_text is False
    assert cfg.poll_interval == 10
    assert cfg.announce_interval == 45


def test_announce_interval_zero_allowed(monkeypatch):
    values = dict(REQUIRED)
    values["ANNOUNCE_INTERVAL"] = "0"  # 0 = wysyłaj raz na poll
    _set_env(monkeypatch, values)
    cfg = load_config()
    assert cfg.announce_interval == 0


def test_ntfy_options_loaded(monkeypatch):
    values = dict(REQUIRED)
    values["NTFY_SERVER"] = "https://ntfy.example.com"
    values["NTFY_TOKEN"] = "tk_test"
    values["NTFY_TITLE"] = "Test"
    _set_env(monkeypatch, values)
    cfg = load_config()
    assert cfg.ntfy_server == "https://ntfy.example.com"
    assert cfg.ntfy_token == "tk_test"
    assert cfg.ntfy_title == "Test"
