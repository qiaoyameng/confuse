import os
import unittest
from unittest import mock

import pytest

import confuse
from confuse.migrations import ConverterRegistry

from . import TempDir


def _migrator(**kwargs):
    """A migrator for the example upgrade: `server.port` moved to
    `http.listen.port` and integer timeouts became unit strings.
    """
    migrator = confuse.Migrator(**kwargs)
    migrator.register_converter("duration", lambda secs: f"{secs}s")
    migrator.add_migration(
        2,
        [
            confuse.Move("server.port", "http.listen.port"),
            confuse.Convert("timeout", "duration"),
            confuse.Remove("legacy_option"),
        ],
    )
    return migrator


class PlanTest(unittest.TestCase):
    def test_move_remove_and_convert(self):
        source = {
            "server": {"port": 8080},
            "timeout": 30,
            "legacy_option": True,
        }
        plan = _migrator().plan(source)
        assert plan.from_version == 1
        assert plan.to_version == 2
        assert plan.data == {
            "http": {"listen": {"port": 8080}},
            "timeout": "30s",
            "config_version": 2,
        }

    def test_plan_records_old_and_new_values(self):
        source = {"server": {"port": 8080}, "timeout": 30, "legacy_option": 1}
        plan = _migrator().plan(source)
        by_kind = {op.kind: op for op in plan.operations}
        move = by_kind["move"]
        assert move.old_path == ("server", "port")
        assert move.new_path == ("http", "listen", "port")
        assert move.old_value == 8080
        assert move.new_value == 8080
        convert = by_kind["convert"]
        assert convert.old_value == 30
        assert convert.new_value == "30s"
        remove = by_kind["remove"]
        assert remove.old_path == ("legacy_option",)

    def test_planning_does_not_modify_source(self):
        source = {"server": {"port": 8080}}
        _migrator().plan(source)
        assert source == {"server": {"port": 8080}}

    def test_missing_keys_are_skipped(self):
        plan = _migrator().plan({"unrelated": 1})
        assert plan.operations == []
        assert plan.data == {"unrelated": 1, "config_version": 2}

    def test_conflict_raises_and_names_both_values(self):
        source = {
            "server": {"port": 8080},
            "http": {"listen": {"port": 9090}},
        }
        with pytest.raises(confuse.MigrationConflictError, match="8080.*9090"):
            _migrator().plan(source)

    def test_conflict_leaves_source_untouched(self):
        source = {
            "server": {"port": 8080},
            "http": {"listen": {"port": 9090}},
        }
        with pytest.raises(confuse.MigrationConflictError):
            _migrator().plan(source)
        assert source["server"]["port"] == 8080
        assert source["http"]["listen"]["port"] == 9090

    def test_unknown_converter_raises(self):
        migrator = confuse.Migrator()
        migrator.add_migration(2, [confuse.Convert("timeout", "eval")])
        with pytest.raises(confuse.MigrationError, match="eval"):
            migrator.plan({"timeout": "30"})

    def test_failing_converter_raises_migration_error(self):
        migrator = confuse.Migrator()
        migrator.register_converter(
            "boom", mock.Mock(side_effect=ValueError("bad value"))
        )
        migrator.add_migration(2, [confuse.Convert("timeout", "boom")])
        with pytest.raises(confuse.MigrationError, match="bad value"):
            migrator.plan({"timeout": 30})

    def test_multi_step_migration(self):
        migrator = _migrator()
        migrator.add_migration(3, [confuse.Move("http.listen.port", "net.port")])
        plan = migrator.plan({"server": {"port": 8080}})
        assert plan.from_version == 1
        assert plan.to_version == 3
        assert plan.data["net"]["port"] == 8080
        assert "http" not in plan.data

    def test_missing_intermediate_version_points_out_gap(self):
        migrator = _migrator()
        migrator.add_migration(4, [confuse.Remove("old")])
        with pytest.raises(confuse.MigrationGapError, match="version 3"):
            migrator.plan({})

    def test_newer_source_version_raises(self):
        with pytest.raises(confuse.MigrationError, match="newer"):
            _migrator().plan({"config_version": 5})

    def test_non_integer_version_marker_raises(self):
        with pytest.raises(confuse.MigrationError, match="integer"):
            _migrator().plan({"config_version": "two"})

    def test_explicit_target_version(self):
        migrator = _migrator(target_version=2)
        migrator.add_migration(3, [confuse.Remove("old")])
        plan = migrator.plan({})
        assert plan.to_version == 2

    def test_custom_version_key(self):
        migrator = _migrator(version_key="meta.version")
        plan = migrator.plan({"meta": {"version": 2}})
        assert not plan
        assert plan.operations == []


class ApplyTest(unittest.TestCase):
    def test_apply_updates_source_and_version(self):
        source = confuse.ConfigSource({"server": {"port": 8080}, "timeout": 30})
        data = _migrator().plan(source).apply()
        assert data["config_version"] == 2
        assert source["http"]["listen"]["port"] == 8080
        assert "server" not in source
        assert source["timeout"] == "30s"

    def test_migrate_convenience(self):
        source = {"server": {"port": 8080}}
        _migrator().migrate(source)
        assert source == {
            "http": {"listen": {"port": 8080}},
            "config_version": 2,
        }

    def test_same_version_is_idempotent(self):
        source = {"http": {"listen": {"port": 8080}}, "config_version": 2}
        plan = _migrator().plan(source)
        assert not plan
        plan.apply()
        assert source == {"http": {"listen": {"port": 8080}}, "config_version": 2}

    def test_stale_plan_is_rejected(self):
        source = {"server": {"port": 8080}}
        plan = _migrator().plan(source)
        source["config_version"] = 2
        with pytest.raises(confuse.MigrationError, match="re-plan"):
            plan.apply()

    def test_only_specified_source_is_migrated(self):
        user = confuse.ConfigSource({"server": {"port": 8080}})
        defaults = confuse.ConfigSource(
            {"server": {"port": 1}, "timeout": 5}, default=True
        )
        root = confuse.RootView([user, defaults])
        _migrator().migrate(user)
        assert "server" not in user
        assert defaults == {"server": {"port": 1}, "timeout": 5}
        # The untouched layer still shows through the merged view.
        assert root["timeout"].get() == 5


class FileMigrationTest(unittest.TestCase):
    CONTENTS = b"server:\n    port: 8080\ntimeout: 30\n"

    def _source(self, temp):
        path = temp.sub("config.yaml", self.CONTENTS)
        return confuse.YamlSource(path), path

    def test_file_is_migrated(self):
        with TempDir() as temp:
            source, path = self._source(temp)
            _migrator().migrate(source)
            with open(path, "rb") as f:
                contents = f.read().decode("utf-8")
            assert "http:" in contents
            assert "port: 8080" in contents
            assert "server" not in contents
            assert "timeout: 30s" in contents
            assert "config_version: 2" in contents
            # The in-memory source matches the file.
            assert source["http"]["listen"]["port"] == 8080

    def test_idempotent_reload_does_not_touch_file(self):
        with TempDir() as temp:
            source, path = self._source(temp)
            _migrator().migrate(source)
            with open(path, "rb") as f:
                migrated = f.read()
            mtime = os.stat(path).st_mtime_ns
            # Reloading the migrated file yields an empty, no-op plan.
            reloaded = confuse.YamlSource(path)
            plan = _migrator().plan(reloaded)
            assert not plan
            plan.apply()
            with open(path, "rb") as f:
                assert f.read() == migrated
            assert os.stat(path).st_mtime_ns == mtime

    def test_failed_write_leaves_no_partial_file(self):
        with TempDir() as temp:
            source, path = self._source(temp)
            plan = _migrator().plan(source)
            with mock.patch(
                "confuse.migrations.os.replace",
                side_effect=OSError("disk full"),
            ):
                with pytest.raises(confuse.MigrationError, match="disk full"):
                    plan.apply()
            # The original file is intact and no temp file is left behind.
            with open(path, "rb") as f:
                assert f.read() == self.CONTENTS
            assert os.listdir(temp.path) == ["config.yaml"]
            assert dict(source) == confuse.load_yaml(path)


class ConverterRegistryTest(unittest.TestCase):
    def test_register_as_decorator(self):
        registry = ConverterRegistry()

        @registry.register("shout")
        def shout(value):
            return str(value).upper()

        assert registry.convert("shout", "hi") == "HI"

    def test_duplicate_registration_raises(self):
        registry = ConverterRegistry()
        registry.register("x", str)
        with pytest.raises(confuse.MigrationError, match="already"):
            registry.register("x", str)

    def test_non_callable_rejected(self):
        registry = ConverterRegistry()
        with pytest.raises(TypeError):
            registry.register("x", "not-a-function")
