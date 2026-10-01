import os
import stat
import unittest
from collections import OrderedDict
from unittest.mock import patch

import confuse
import yaml

from . import TempDir

DEFAULT_YAML = (
    "server:\n"
    "    host: localhost\n"
    "    port: 8080\n"
    "    timeout: 5\n"
)
USER_YAML = (
    "server:\n"
    "    host: myhost\n"
)


class EditSessionTest(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict("os.environ", {})
        self.env_patcher.start()
        self.tmp = TempDir()
        self.tmp.__enter__()
        self.default_path = self.tmp.sub(
            "config_default.yaml", DEFAULT_YAML.encode()
        )
        self.user_path = self.tmp.sub("config.yaml", USER_YAML.encode())
        os.environ["APP_SERVER__PORT"] = "9000"
        self.config = self._build_config()

    def tearDown(self):
        self.env_patcher.stop()
        self.tmp.__exit__(None, None, None)

    def _build_config(self):
        """Build config with the real priority order: env > user > default."""
        config = confuse.Configuration("app", read=False)
        config.add(confuse.YamlSource(self.user_path, optional=True))
        config.add(
            confuse.YamlSource(
                self.default_path, default=True, optional=True
            )
        )
        config.set_env("APP_")
        return config

    def test_targets_user_source_by_default(self):
        session = self.config.edit()
        assert session.filename == os.path.abspath(self.user_path)

    def test_select_source_by_filename(self):
        session = self.config.edit(filename=self.user_path)
        assert session.filename == os.path.abspath(self.user_path)

    def test_no_writable_source(self):
        config = confuse.Configuration("bare", read=False)
        with self.assertRaises(confuse.ConfigError):
            config.edit()

    def test_default_source_not_auto_selected(self):
        config = confuse.Configuration("bare", read=False)
        config.add(
            confuse.YamlSource(self.default_path, default=True, optional=True)
        )
        with self.assertRaises(confuse.ConfigError):
            config.edit()

    def test_explicit_default_source_allowed(self):
        sources = self.config.sources
        default_source = next(s for s in sources if s.default)
        session = self.config.edit(source=default_source)
        assert session.filename == os.path.abspath(self.default_path)

    def test_unknown_source_rejected(self):
        other = confuse.YamlSource(self.user_path, optional=True)
        with self.assertRaises(confuse.ConfigError):
            self.config.edit(source=other)

    # -- effective values and shadowing ---------------------------------

    def test_effective_merges_three_layers(self):
        server = self.config["server"].flatten()
        assert server == {"port": 9000, "host": "myhost", "timeout": 5}

    def test_set_new_path_is_effective(self):
        session = self.config.edit()
        status = session.set(("server", "retries"), 3)
        assert not status.shadowed
        assert session.preview(("server", "retries")) == 3
        with self.assertRaises(confuse.NotFoundError):
            session.effective(("server", "retries"))
        # Effective value unchanged until save.
        with self.assertRaises(confuse.NotFoundError):
            self.config["server"]["retries"].get()

    def test_set_path_masked_by_env(self):
        session = self.config.edit()
        status = session.set(("server", "port"), 7000)
        assert status.shadowed
        assert status.value == 9000
        assert session.preview(("server", "port")) == 9000
        assert session.is_shadowed(("server", "port"))

    def test_env_unset_value_shadows(self):
        os.environ["APP_SERVER__TIMEOUT"] = ""
        self.config = self._build_config()
        session = self.config.edit()
        status = session.set(("server", "timeout"), 42)
        assert status.shadowed
        assert status.present
        assert status.value is None

    # -- deletion reveals lower layers ----------------------------------

    def test_delete_reveals_default(self):
        session = self.config.edit()
        status = session.delete(("server", "host"))
        assert not status.shadowed
        assert status.present
        assert status.value == "localhost"
        result = session.save()
        assert (("server", "host"),) == tuple(
            path for path in result.revealed
        )
        assert self.config["server"]["host"].get() == "localhost"

    def test_delete_does_not_remove_from_other_sources(self):
        session = self.config.edit()
        session.delete(("server", "host"))
        session.save()
        default_data = yaml.safe_load(open(self.default_path))
        assert default_data["server"]["host"] == "localhost"
        assert "APP_SERVER__PORT" in os.environ

    def test_delete_masked_by_env_still_effective(self):
        session = self.config.edit()
        status = session.delete(("server", "port"))
        assert status.shadowed
        assert status.value == 9000
        result = session.save()
        assert (("server", "port"),) == tuple(
            path for path in result.shadowed
        )
        assert self.config["server"]["port"].get() == 9000

    def test_delete_without_lower_value(self):
        session = self.config.edit()
        session.set("standalone", 1)
        session.save()
        session = self.config.edit()
        status = session.delete("standalone")
        assert not status.present
        result = session.save()
        assert result.revealed == []
        assert not self.config["standalone"].exists()

    def test_delete_nonexistent_path_is_noop(self):
        session = self.config.edit()
        status = session.delete(("server", "nope"))
        assert not status.present
        result = session.save()
        assert result.changed == []
        assert result.revealed == []

    # -- persistence -----------------------------------------------------

    def test_save_writes_only_modified_paths(self):
        session = self.config.edit()
        session.set(("server", "retries"), 3)
        session.set(("server", "port"), 7000)
        session.delete(("server", "host"))
        result = session.save()

        written = yaml.safe_load(open(self.user_path))
        # Only the user layer; no defaults or env values copied in.
        assert written == {
            "server": {"retries": 3, "port": 7000}
        }
        assert set(result.changed) == {
            ("server", "retries"),
            ("server", "port"),
            ("server", "host"),
        }
        assert result.shadowed == [("server", "port")]
        assert result.revealed == [("server", "host")]
        assert not session.changed_paths

    def test_save_keeps_unrelated_user_keys(self):
        with open(self.user_path, "w") as fp:
            fp.write("other: value\nserver:\n    host: myhost\n")
        self.config.sources[1].load()
        session = self.config.edit()
        session.set("new", 42)
        session.save()
        written = yaml.safe_load(open(self.user_path))
        assert written["other"] == "value"
        assert written["new"] == 42

    def test_effective_after_save(self):
        session = self.config.edit()
        session.set(("server", "retries"), 3)
        session.delete(("server", "host"))
        session.save()
        assert self.config["server"]["retries"].get() == 3
        assert self.config["server"]["host"].get() == "localhost"
        assert self.config["server"]["port"].get() == 9000

    def test_save_creates_missing_file(self):
        missing = os.path.join(self.tmp.path, "subdir", "new.yaml")
        config = confuse.Configuration("newapp", read=False)
        source = confuse.YamlSource(missing, optional=True)
        config.add(source)
        session = config.edit()
        session.set("alpha", "beta")
        session.save()
        assert yaml.safe_load(open(missing)) == {"alpha": "beta"}

    def test_empty_dict_dumps_to_empty_file(self):
        session = self.config.edit()
        session.delete(("server", "host"))
        session.save()
        assert yaml.safe_load(open(self.user_path)) == {}
        # The default value now shows through.
        assert self.config["server"]["host"].get() == "localhost"

    # -- conflicts and failures ------------------------------------------

    def test_external_change_conflicts_and_preserves_file(self):
        session = self.config.edit()
        session.set("foo", "bar")
        with open(self.user_path, "w") as fp:
            fp.write("external: true\n")
        with self.assertRaises(confuse.ConfigConflictError):
            session.save()
        assert open(self.user_path).read().strip() == "external: true"
        # Nothing leaked into the running config.
        assert not self.config["foo"].exists()

    def test_deletion_then_external_change_conflicts(self):
        session = self.config.edit()
        session.delete(("server", "host"))
        with open(self.user_path, "a") as fp:
            fp.write("appended: 1\n")
        with self.assertRaises(confuse.ConfigConflictError):
            session.save()

    def test_write_failure_keeps_original(self):
        session = self.config.edit()
        session.set("foo", "bar")
        original = open(self.user_path, "rb").read()

        def boom(src, dst):
            raise OSError("simulated failure")

        with patch("confuse.edit.os.replace", boom):
            with self.assertRaises(confuse.ConfigWriteError):
                session.save()

        assert open(self.user_path, "rb").read() == original
        leftovers = [
            f for f in os.listdir(self.tmp.path)
            if f.startswith(".confuse-edit")
        ]
        assert leftovers == []
        # The failed change can still be retried.
        assert session.changed_paths == [("foo",)]
        result = session.save()
        assert result.changed == [("foo",)]

    def test_readonly_directory_raises_write_error(self):
        target_dir = os.path.join(self.tmp.path, "ro")
        os.makedirs(target_dir)
        os.chmod(target_dir, stat.S_IRUSR | stat.S_IXUSR)
        target = os.path.join(target_dir, "config.yaml")
        config = confuse.Configuration("roapp", read=False)
        config.add(confuse.YamlSource(target, optional=True))
        session = config.edit()
        session.set("a", 1)
        with self.assertRaises(confuse.ConfigWriteError):
            session.save()
        os.chmod(target_dir, stat.S_IRWXU)

    # -- session semantics -----------------------------------------------

    def test_rollback_discards_changes(self):
        session = self.config.edit()
        session.set("foo", "bar")
        session.rollback()
        assert session.changed_paths == []
        result = session.save()
        assert result.changed == []

    def test_set_parent_replaces_descendant_edits(self):
        session = self.config.edit()
        session.set(("server", "host"), "a")
        session.set("server", {"host": "b"})
        assert session.changed_paths == [("server",)]
        result = session.save()
        assert result.changed == [("server",)]
        written = yaml.safe_load(open(self.user_path))
        assert written["server"] == {"host": "b"}

    def test_repeated_set_uses_latest(self):
        session = self.config.edit()
        session.set("foo", 1)
        session.set("foo", 2)
        assert session.changed_paths == [("foo",)]
        session.save()
        assert yaml.safe_load(open(self.user_path))["foo"] == 2

    def test_status_without_pending_changes(self):
        session = self.config.edit()
        status = session.status(("server", "host"))
        assert not status.staged
        assert not status.shadowed
        assert status.value == "myhost"

    def test_refresh_detects_change(self):
        session = self.config.edit()
        with open(self.user_path, "w") as fp:
            fp.write("changed: yes\n")
        with self.assertRaises(confuse.ConfigConflictError):
            session.refresh()

    def test_refresh_adopts_externally_restored_file(self):
        session = self.config.edit()
        session.set("foo", "bar")
        original = open(self.user_path, "rb").read()
        with open(self.user_path, "wb") as fp:
            fp.write(b"temporary: yes\n")
        with self.assertRaises(confuse.ConfigConflictError):
            session.save()
        with open(self.user_path, "wb") as fp:
            fp.write(original)
        session.refresh()
        result = session.save()
        assert result.changed == [("foo",)]

    def test_context_manager_saves(self):
        with self.config.edit() as session:
            session.set("ctx", True)
        assert yaml.safe_load(open(self.user_path))["ctx"] is True

    def test_context_manager_discards_on_error(self):
        with self.assertRaises(ValueError):
            with self.config.edit() as session:
                session.set("ctx", True)
                raise ValueError("boom")
        written = yaml.safe_load(open(self.user_path))
        assert "ctx" not in written

    def test_dotless_string_path(self):
        session = self.config.edit()
        session.set("top", "value")
        assert session.preview("top") == "value"
        session.save()
        assert yaml.safe_load(open(self.user_path))["top"] == "value"

    def test_traversal_type_error(self):
        session = self.config.edit()
        with self.assertRaises(confuse.ConfigTypeError):
            session.set(("server", "host", "deep"), 1)

    def test_save_empty_session(self):
        session = self.config.edit()
        result = session.save()
        assert result.changed == []
        assert result.shadowed == []
        assert result.revealed == []

    def test_ordered_dict_tree(self):
        session = self.config.edit()
        value = OrderedDict([("a", 1), ("b", 2)])
        session.set("nested", value)
        session.save()
        written = yaml.safe_load(open(self.user_path))
        assert list(written["nested"]) == ["a", "b"]


class EditSessionExtraTest(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict("os.environ", {})
        self.env_patcher.start()
        self.tmp = TempDir()
        self.tmp.__enter__()

    def tearDown(self):
        self.env_patcher.stop()
        self.tmp.__exit__(None, None, None)

    def test_lazy_config_materialized(self):
        user_path = self.tmp.sub("config.yaml", b"foo: 1\n")
        config = confuse.LazyConfig("lazyapp", None)
        config.add(confuse.YamlSource(user_path, optional=True))
        assert not config._materialized
        with patch.dict(os.environ, {"LAZYAPPDIR": self.tmp.path}):
            session = config.edit()
            assert config._materialized
            session.set("foo", 2)
            session.save()
        assert yaml.safe_load(open(user_path))["foo"] == 2

    def test_overlay_inserted_after_session_opened(self):
        user_path = self.tmp.sub("config.yaml", b"foo: 1\n")
        config = confuse.Configuration("app", read=False)
        config.add(confuse.YamlSource(user_path, optional=True))
        session = config.edit()
        # A higher-priority overlay appears while the session is open.
        os.environ["APP_FOO"] = "9"
        config.set_env("APP_")
        status = session.set("foo", 2)
        assert status.shadowed
        assert status.value == 9
        session.save()
        assert yaml.safe_load(open(user_path))["foo"] == 2
        assert config["foo"].get() == 9

    def test_bytes_key_path(self):
        user_path = self.tmp.sub("config.yaml", b"foo: 1\n")
        config = confuse.Configuration("app", read=False)
        config.add(confuse.YamlSource(user_path, optional=True))
        session = config.edit()
        session.set((b"bar",), 3)
        session.save()
        assert yaml.safe_load(open(user_path)) == {"foo": 1, b"bar": 3}

    def test_target_source_removed_raises(self):
        user_path = self.tmp.sub("config.yaml", b"foo: 1\n")
        config = confuse.Configuration("app", read=False)
        source = confuse.YamlSource(user_path, optional=True)
        config.add(source)
        session = config.edit()
        session.set("foo", 2)
        config.clear()
        with self.assertRaises(confuse.ConfigError):
            session.save()
        assert yaml.safe_load(open(user_path))["foo"] == 1
