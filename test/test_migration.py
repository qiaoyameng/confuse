from __future__ import annotations

import os
from pathlib import Path

import confuse
import pytest
import yaml

from . import TempDir


def migrator() -> confuse.ConfigMigrator:
    migrator = confuse.ConfigMigrator()

    @migrator.register_converter("seconds_string")
    def seconds_string(value: object) -> str:
        if not isinstance(value, int):
            raise TypeError("timeout must be an integer")
        return f"{value}s"

    migrator.register(
        1,
        move=[
            (
                ("server", "port"),
                ("http", "listen", "port"),
            )
        ],
        convert={("server", "timeout"): "seconds_string"},
    )
    migrator.register(2, delete=[("legacy",)])
    return migrator


def test_plan_moves_and_converts_values_without_mutating_source():
    source = confuse.ConfigSource(
        {
            "server": {"port": 8080, "timeout": 30},
            "legacy": None,
        }
    )
    plan = migrator().plan(source, target_version=2)

    assert source == {"server": {"port": 8080, "timeout": 30}, "legacy": None}
    operations = {change.operation: change for change in plan.changes}
    assert operations["move"].old == 8080
    assert operations["move"].new == 8080
    assert operations["convert"].old == 30
    assert operations["convert"].new == "30s"
    assert operations["delete"].old is None
    assert operations["delete"].new is confuse.MISSING
    assert operations["version"].new == 2
    assert plan.after == {
        "http": {"listen": {"port": 8080}},
        "server": {"timeout": "30s"},
        "config_version": 2,
    }


def test_apply_updates_only_selected_in_memory_source():
    migrator_ = migrator()
    selected = confuse.ConfigSource(
        {"server": {"timeout": "30s"}, "legacy": None, "config_version": 1}
    )
    other = confuse.ConfigSource({"server": {"port": 9000}})
    config = confuse.Configuration("migration-test", read=False)
    config.add(other)
    config.set(selected)

    plan = config.migration_plan(migrator_, selected, target_version=2)
    plan.apply()

    assert selected == {"server": {"timeout": "30s"}, "config_version": 2}
    assert other == {"server": {"port": 9000}}


def test_new_and_old_keys_report_conflict():
    source = confuse.ConfigSource(
        {
            "server": {"port": 8080},
            "http": {"listen": {"port": 9000}},
        }
    )
    with pytest.raises(
        confuse.MigrationConflictError,
        match="server.port.*http.listen.port",
    ):
        migrator().plan(source, target_version=1)
    assert source == {
        "server": {"port": 8080},
        "http": {"listen": {"port": 9000}},
    }


def test_missing_intermediate_version_reports_breakpoint():
    migrations = confuse.ConfigMigrator()
    migrations.register(1, delete=[("one",)])
    migrations.register(3, delete=[("three",)])

    with pytest.raises(
        confuse.MissingMigrationError,
        match=r"missing migration rule to version 2",
    ):
        migrations.plan(confuse.ConfigSource({}), target_version=3)


def test_same_version_is_idempotent():
    source = confuse.ConfigSource(
        {"server": {"port": 8080}, "config_version": 1}
    )
    migrations = migrator()

    first = migrations.plan(source, target_version=1)
    second = migrations.plan(source, target_version=1)
    assert first.changes == ()
    assert not first
    first.apply()
    assert second.source is source
    second.apply()
    assert source == {"server": {"port": 8080}, "config_version": 1}


def test_initial_version_without_rules_is_idempotent():
    source = confuse.ConfigSource({})
    plan = confuse.ConfigMigrator().plan(source)

    assert plan.changes == ()
    plan.apply()
    assert source == {}


def test_rule_cannot_use_unregistered_converter():
    migrations = confuse.ConfigMigrator()
    with pytest.raises(
        confuse.ConfigMigrationError,
        match="converter 'missing' is not registered",
    ):
        migrations.register(1, convert={("server", "timeout"): "missing"})


def test_move_can_use_registered_value_converter():
    migrations = confuse.ConfigMigrator()
    migrations.register_converter("seconds_string", lambda value: f"{value}s")
    migrations.register(
        1,
        move=[
            confuse.MoveRule(
                ("server", "timeout"),
                ("http", "timeout"),
                "seconds_string",
            )
        ],
    )
    source = confuse.ConfigSource({"server": {"timeout": 30}})

    plan = migrations.plan(source, target_version=1)

    assert plan.after["http"]["timeout"] == "30s"
    assert "server" not in plan.after


def test_apply_persists_yaml_source_atomically_and_reloads():
    with TempDir() as temp:
        filename = temp.sub(
            "config.yaml",
            b"server:\n    port: 8080\n    timeout: 30\n",
        )
        config = confuse.Configuration("migration-test", read=False)
        source = confuse.YamlSource(filename)
        config.set(source)

        plan = config.migration_plan(migrator(), source, target_version=1)
        plan.apply()

        with open(filename, encoding="utf-8") as config_file:
            written = yaml.safe_load(config_file)
        assert written == {
            "http": {"listen": {"port": 8080}},
            "server": {"timeout": "30s"},
            "config_version": 1,
        }
        assert source == written
        assert config["http"]["listen"]["port"].get() == 8080


def test_failed_temp_load_restores_source_and_original(monkeypatch):
    with TempDir() as temp:
        filename = temp.sub(
            "config.yaml",
            b"server:\n    port: 8080\n    timeout: 30\n",
        )
        source = confuse.YamlSource(filename)
        original_source = dict(source)
        real_yaml_source = confuse.migration._YamlSourceType

        def fail_yaml_source(*args: object, **kwargs: object) -> confuse.YamlSource:
            raise OSError("simulated load failure")

        monkeypatch.setattr(confuse.migration, "YamlSource", fail_yaml_source)
        plan = migrator().plan(source, target_version=1)
        with pytest.raises(OSError, match="simulated load failure"):
            plan.apply()

        assert dict(source) == original_source
        with open(filename, "rb") as config_file:
            assert config_file.read() == b"server:\n    port: 8080\n    timeout: 30\n"
        assert not plan.applied
        assert not list(Path(temp.path).glob(".*.tmp"))


def test_failed_apply_leaves_original_file_untouched(monkeypatch):
    with TempDir() as temp:
        filename = temp.sub(
            "config.yaml",
            b"server:\n    port: 8080\n    timeout: 30\n",
        )
        source = confuse.YamlSource(filename)
        original_replace = os.replace

        def fail_replace(source_name: str, destination_name: str) -> None:
            raise OSError("simulated failure")

        monkeypatch.setattr(os, "replace", fail_replace)
        plan = migrator().plan(source, target_version=1)
        with pytest.raises(OSError, match="simulated failure"):
            plan.apply()
        monkeypatch.setattr(os, "replace", original_replace)

        with open(filename, "rb") as config_file:
            assert config_file.read() == b"server:\n    port: 8080\n    timeout: 30\n"
        assert source["server"]["port"] == 8080
        assert not plan.applied
        assert not list(Path(temp.path).glob(".*.tmp"))
