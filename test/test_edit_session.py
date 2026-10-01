import os
import stat
import textwrap
import unittest

import confuse

from . import TempDir


def _write(path, content):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(textwrap.dedent(content).lstrip("\n"))


def _build_config(tmp, env=None, user_content=None, default_content=None):
    user = tmp.sub("config.yaml")
    default = tmp.sub("config_default.yaml")
    if user_content is not None:
        _write(user, user_content)
    if default_content is not None:
        _write(default, default_content)

    env_names = list((env or {}).keys())
    for name, value in (env or {}).items():
        os.environ[name] = value

    sources = [
        confuse.EnvSource("MYAPP_"),
        confuse.YamlSource(user, optional=True),
        confuse.YamlSource(default, optional=True, default=True),
    ]
    config = confuse.RootView(sources)
    return config, user, default, env_names


def _restore_env(old_env):
    for name in old_env:
        os.environ.pop(name, None)


class EditSessionFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = TempDir()
        self.tmp.__enter__()

    def tearDown(self):
        # Make sure directories are writable before cleanup.
        os.chmod(self.tmp.path, stat.S_IRWXU)
        self.tmp.__exit__(None, None, None)

    def make(self, env=None, user=None, default=None):
        config, user_path, default_path, env_names = _build_config(
            self.tmp, env=env, user_content=user, default_content=default
        )
        self.addCleanup(_restore_env, env_names)
        return config, user_path, default_path


class TargetSelectionTest(EditSessionFixture):
    def test_default_target_is_user_file(self):
        config, user, _ = self.make(
            user="greeting: hi\n", default="greeting: d\n"
        )
        session = confuse.EditSession(config)
        self.assertEqual(session.filename, os.path.abspath(user))

    def test_target_by_path_string(self):
        config, user, _ = self.make(user="greeting: hi\n")
        session = confuse.EditSession(config, user)
        self.assertEqual(session.filename, os.path.abspath(user))

    def test_target_by_source(self):
        config, user, _ = self.make(user="greeting: hi\n")
        source = config.sources[1]
        session = confuse.EditSession(config, source)
        self.assertIs(session.target, source)

    def test_unknown_path_target_raises(self):
        config, _, _ = self.make(user="greeting: hi\n")
        with self.assertRaises(ValueError):
            confuse.EditSession(config, self.tmp.sub("other.yaml"))

    def test_no_yaml_target_raises(self):
        config = confuse.RootView([confuse.ConfigSource({})])
        with self.assertRaises(ValueError):
            confuse.EditSession(config)


class StagedSetTest(EditSessionFixture):
    def test_set_takes_effect_when_unmasked(self):
        config, _, _ = self.make(
            user="nested:\n    a: 1\n",
            default="greeting: default\nnested:\n    a: 9\n    c: 3\n",
        )
        session = confuse.EditSession(config)
        outcome = session.set("nested.a", 42)
        self.assertEqual(outcome.effective.value, 42)
        self.assertFalse(outcome.shadowed)
        self.assertEqual(session.layer_value("nested.a"), 42)

    def test_set_reports_env_shadowing(self):
        config, _, _ = self.make(
            env={"MYAPP_GREETING": "from-env"},
            user="greeting: hi\n",
            default="greeting: d\n",
        )
        session = confuse.EditSession(config)
        outcome = session.set("greeting", "howdy")
        self.assertEqual(outcome.effective.value, "from-env")
        self.assertTrue(outcome.shadowed)
        self.assertIn("shadowed", session.explain_shadow("greeting"))

    def test_set_creates_new_path(self):
        config, _, _ = self.make(user="greeting: hi\n")
        session = confuse.EditSession(config)
        outcome = session.set("brand.new.key", "v")
        self.assertEqual(outcome.effective.value, "v")
        self.assertTrue(outcome.layer_changed)

    def test_set_none_masks_lower_layers(self):
        config, _, _ = self.make(
            user="greeting: hi\n", default="greeting: d\n"
        )
        session = confuse.EditSession(config)
        outcome = session.set("greeting", None)
        self.assertIsNone(outcome.effective.value)
        self.assertFalse(outcome.shadowed)

    def test_setting_non_mapping_path_raises(self):
        config, _, _ = self.make(user="greeting: hi\n")
        session = confuse.EditSession(config)
        with self.assertRaises(confuse.ConfigTypeError):
            session.set("greeting.child", 1)

    def test_invalid_paths(self):
        config, _, _ = self.make(user="greeting: hi\n")
        session = confuse.EditSession(config)
        with self.assertRaises(ValueError):
            session.set("", 1)
        with self.assertRaises(TypeError):
            session.set((1,), 1)


class StagedDeleteTest(EditSessionFixture):
    def test_delete_reveals_default_value(self):
        config, _, _ = self.make(
            user="greeting: hi\nnested:\n    a: 1\n",
            default="greeting: d\nnested:\n    a: 99\n    c: 3\n",
        )
        session = confuse.EditSession(config)
        outcome = session.delete("nested.a")
        self.assertEqual(outcome.revealed.value, 99)
        self.assertTrue(outcome.revealed.exists)
        self.assertEqual(outcome.effective.value, 99)

    def test_delete_leaves_path_missing_without_lower_value(self):
        config, _, _ = self.make(user="only: here\n", default="other: x\n")
        session = confuse.EditSession(config)
        outcome = session.delete("only")
        self.assertIs(outcome.revealed.value, confuse.MISSING)
        self.assertFalse(outcome.revealed.exists)
        self.assertFalse(outcome.effective.exists)

    def test_delete_only_affects_target_layer(self):
        config, _, _ = self.make(
            env={"MYAPP_GREETING": "envval"},
            user="greeting: hi\n",
            default="greeting: d\n",
        )
        session = confuse.EditSession(config)
        outcome = session.delete("greeting")
        # Env layer still wins; user deletion does not touch env/defaults.
        self.assertEqual(outcome.effective.value, "envval")

    def test_delete_missing_raises_not_found(self):
        config, _, _ = self.make(user="greeting: hi\n")
        session = confuse.EditSession(config)
        with self.assertRaises(confuse.NotFoundError):
            session.delete("absent")

    def test_delete_removes_emptied_containers(self):
        config, _, _ = self.make(user="a:\n    b:\n        c: 1\n", default="x: 1\n")
        session = confuse.EditSession(config)
        session.delete("a.b.c")
        self.assertIs(session.layer_value("a"), confuse.MISSING)


class SaveTest(EditSessionFixture):
    def test_save_writes_only_user_paths(self):
        config, user, _ = self.make(
            env={"MYAPP_GREETING": "envval"},
            user="greeting: hi\nnested:\n    a: 1\n    b: 2\n",
            default="greeting: d\nonly_default: keep\nnested:\n    c: 3\n",
        )
        session = confuse.EditSession(config)
        session.set("nested.a", 42)
        session.set("only_default", "user-value")
        session.delete("nested.b")
        report = session.save()
        self.assertTrue(report.changed)

        content = open(user, encoding="utf-8").read()
        self.assertNotIn("envval", content)
        self.assertNotIn("keep", content)
        self.assertIn("a: 42", content)
        self.assertIn("only_default: user-value", content)
        self.assertNotIn("b:", content)

    def test_saved_effective_values_after_reload(self):
        config, _, _ = self.make(
            env={"MYAPP_GREETING": "envval"},
            user="greeting: hi\nnested:\n    a: 1\n",
            default="greeting: d\nnested:\n    a: 99\n    c: 3\n",
        )
        session = confuse.EditSession(config)
        report = session.save()
        statuses = {entry.path: entry.status for entry in report.paths}
        self.assertEqual(statuses, {})

        session = confuse.EditSession(config)
        session.delete("nested.a")
        report = session.save()
        statuses = {entry.path: entry.status for entry in report.paths}
        self.assertEqual(statuses["nested.a"], "deleted-revealed")

        reloaded, _, _ = self.make(
            env={"MYAPP_GREETING": "envval"},
            user=open(config.sources[1].filename, encoding="utf-8").read(),
            default="greeting: d\nnested:\n    a: 99\n    c: 3\n",
        )
        self.assertEqual(reloaded["greeting"].get(), "envval")
        self.assertEqual(reloaded["nested"]["a"].get(), 99)
        self.assertEqual(reloaded["nested"]["c"].get(), 3)

    def test_save_shadowed_set_status(self):
        config, _, _ = self.make(
            env={"MYAPP_GREETING": "envval"},
            user="greeting: hi\n",
            default="greeting: d\n",
        )
        session = confuse.EditSession(config)
        session.set("greeting", "written-but-hidden")
        report = session.save()
        self.assertEqual(report.paths[0].status, "set-shadowed")
        self.assertTrue(report.paths[0].shadowed)

    def test_save_noop_when_content_identical(self):
        config, user, _ = self.make(user="greeting: hi\n")
        original = open(user, "rb").read()
        session = confuse.EditSession(config)
        session.set("greeting", "hi")
        report = session.save()
        self.assertFalse(report.changed)
        self.assertEqual(report.paths[0].status, "noop")
        self.assertEqual(open(user, "rb").read(), original)

    def test_save_creates_missing_file(self):
        config, user, _ = self.make(default="greeting: d\n")
        self.assertFalse(os.path.exists(user))
        session = confuse.EditSession(config)
        session.set("greeting", "mine")
        session.save()
        self.assertTrue(os.path.exists(user))
        self.assertIn("greeting: mine", open(user, encoding="utf-8").read())

    def test_external_change_causes_conflict(self):
        config, user, _ = self.make(user="greeting: hi\n")
        session = confuse.EditSession(config)
        session.set("greeting", "new")
        with open(user, "a", encoding="utf-8") as handle:
            handle.write("other: concurrent\n")
        original = open(user, "rb").read()
        with self.assertRaises(confuse.EditConflictError):
            session.save()
        # File untouched; staged edits retained for a fresh retry.
        self.assertEqual(open(user, "rb").read(), original)
        self.assertEqual(session.edited_paths, ("greeting",))

    def test_external_same_content_is_not_conflict(self):
        config, _, _ = self.make(user="greeting: hi\n")
        session = confuse.EditSession(config)
        session.set("greeting", "ho")
        # Rewriting identical content must not count as a conflict.
        with open(session.filename, "wb") as handle:
            handle.write(session._baseline_bytes)
        report = session.save()
        self.assertTrue(report.changed)

    def test_write_failure_preserves_original(self):
        config, user, _ = self.make(user="greeting: hi\nkeep: yes\n")
        original = open(user, "rb").read()
        session = confuse.EditSession(config)
        session.set("greeting", "new")
        os.chmod(self.tmp.path, stat.S_IRUSR | stat.S_IXUSR)
        try:
            with self.assertRaises(confuse.EditWriteError):
                session.save()
        finally:
            os.chmod(self.tmp.path, stat.S_IRWXU)
        self.assertEqual(open(user, "rb").read(), original)
        self.assertEqual(os.listdir(self.tmp.path).count("config.yaml"), 1)


class SessionLifecycleTest(EditSessionFixture):
    def test_context_manager_discards_unsaved_edits(self):
        config, user, _ = self.make(user="greeting: hi\n")
        with confuse.edit_session(config) as session:
            session.set("greeting", "unsaved")
        self.assertEqual(open(user, encoding="utf-8").read(), "greeting: hi\n")

    def test_preview_matches_saved_content(self):
        config, _, _ = self.make(
            user="greeting: hi\nnested:\n    a: 1\n",
            default="x: 1\n",
        )
        session = confuse.EditSession(config)
        session.set("nested.a", 2)
        session.delete("greeting")
        preview = session.preview_yaml()
        session.save()
        self.assertEqual(
            preview,
            open(session.filename, encoding="utf-8").read(),
        )

    def test_sources_describes_layers(self):
        config, _, _ = self.make(
            env={"MYAPP_X": "1"},
            user="greeting: hi\n",
            default="greeting: d\n",
        )
        session = confuse.EditSession(config)
        infos = session.sources()
        self.assertEqual([info.kind for info in infos], ["env", "yaml", "yaml"])
        self.assertTrue(infos[2].default)
        self.assertFalse(infos[1].default)


class EditSequenceTest(EditSessionFixture):
    def test_delete_then_set_revives_path(self):
        config, _, _ = self.make(
            user="greeting: hi\n", default="greeting: d\n"
        )
        session = confuse.EditSession(config)
        session.delete("greeting")
        self.assertEqual(session.effective("greeting").value, "d")
        outcome = session.set("greeting", "again")
        self.assertEqual(outcome.effective.value, "again")
        report = session.save()
        self.assertEqual(report.paths[0].status, "set")

    def test_repeated_save_is_idempotent(self):
        config, user, _ = self.make(user="greeting: hi\n")
        session = confuse.EditSession(config)
        session.set("greeting", "ho")
        first = session.save()
        self.assertTrue(first.changed)
        second = session.save()
        self.assertFalse(second.changed)
        self.assertEqual(second.paths, ())
        self.assertEqual(
            open(user, encoding="utf-8").read(), "greeting: ho\n"
        )

    def test_deleting_every_user_key_yields_empty_file(self):
        config, user, _ = self.make(
            user="greeting: hi\nother: x\n", default="greeting: d\n"
        )
        session = confuse.EditSession(config)
        session.delete("greeting")
        session.delete("other")
        session.save()
        self.assertEqual(open(user, encoding="utf-8").read(), "")
        self.assertEqual(session.effective("greeting").value, "d")
        self.assertFalse(session.effective("other").exists)

    def test_set_none_then_delete_reveals_default(self):
        config, _, _ = self.make(
            user="greeting: hi\n", default="greeting: d\n"
        )
        session = confuse.EditSession(config)
        session.set("greeting", None)
        self.assertIsNone(session.effective("greeting").value)
        session.delete("greeting")
        self.assertEqual(session.effective("greeting").value, "d")

    def test_save_removes_only_target_path_not_other_layers(self):
        config, user, default = self.make(
            env={"MYAPP_GREETING": "env"},
            user="greeting: hi\nuser_only: 1\n",
            default="greeting: d\ndefault_only: 2\n",
        )
        session = confuse.EditSession(config)
        session.delete("user_only")
        session.save()
        content = open(user, encoding="utf-8").read()
        self.assertNotIn("user_only", content)
        self.assertNotIn("default_only", content)
        self.assertEqual(open(default, encoding="utf-8").read().count("greeting"), 1)


if __name__ == "__main__":
    unittest.main()
