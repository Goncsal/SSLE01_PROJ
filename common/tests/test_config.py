import pytest

from evcommon import config


def test_typed_getters():
    env = {"A": "5", "B": "true", "C": " x, y ,,z ", "F": "1.5", "E": ""}
    assert config.get_int("A", 1, env) == 5
    assert config.get_int("E", 7, env) == 7
    assert config.get_int("MISSING", 3, env) == 3
    assert config.get_bool("B", False, env) is True
    assert config.get_bool("E", True, env) is False
    assert config.get_list("C", env=env) == ["x", "y", "z"]
    assert config.get_float("F", 0.0, env) == 1.5
    with pytest.raises(config.ConfigError):
        config.get_int("C", 0, env)
    with pytest.raises(config.ConfigError):
        config.get_bool("C", False, env)


def test_require_and_tokens():
    with pytest.raises(config.ConfigError):
        config.require("X", {})
    with pytest.raises(config.ConfigError):
        config.require_token("T", {"T": "short"})
    assert config.require_token("T", {"T": "a" * 16}) == "a" * 16


def test_prefixed():
    env = {"REGISTRY_TOKEN_PORTO": "p" * 20, "REGISTRY_TOKEN_VILA_REAL": "v" * 20,
           "REGISTRY_TOKEN_": "x", "REGISTRY_ADMIN_TOKEN": "a" * 20, "REGISTRY_TOKEN_EMPTY": ""}
    assert config.prefixed("REGISTRY_TOKEN_", env) == {"PORTO": "p" * 20, "VILA_REAL": "v" * 20}


def test_database_url(tmp_path):
    url = config.database_url(f"sqlite:///{tmp_path}/sub/registry.db", env={})
    assert url == f"sqlite:///{tmp_path}/sub/registry.db"
    assert (tmp_path / "sub").is_dir()
    assert config.normalise_database_url("postgresql://u:p@postgres:5432/csms") == \
        "postgresql+psycopg://u:p@postgres:5432/csms"
    assert config.normalise_database_url("postgres://u:p@h/db").startswith("postgresql+psycopg://")
    assert config.normalise_database_url("sqlite:///:memory:") == "sqlite:///:memory:"
    with pytest.raises(config.ConfigError):
        config.normalise_database_url("mysql://x")


def test_instance_id():
    assert config.instance_id({"INSTANCE_ID": "csms-7f9c-abcde"}) == "csms-7f9c-abcde"
    assert config.instance_id({})
