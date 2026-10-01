import argparse
import optparse
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

import pytest

import confuse


class ArgparseTest(unittest.TestCase):
    def setUp(self):
        self.config = confuse.Configuration("test", read=False)
        self.parser = argparse.ArgumentParser()

    def _parse(self, args, **kwargs):
        args = self.parser.parse_args(args.split())
        self.config.set_args(args, **kwargs)

    def test_text_argument_parsed(self):
        self.parser.add_argument("--foo", metavar="BAR")
        self._parse("--foo bar")
        assert self.config["foo"].get() == "bar"

    def test_boolean_argument_parsed(self):
        self.parser.add_argument("--foo", action="store_true")
        self._parse("--foo")
        assert self.config["foo"].get()

    def test_missing_optional_argument_not_included(self):
        self.parser.add_argument("--foo", metavar="BAR")
        self._parse("")
        with pytest.raises(confuse.NotFoundError):
            self.config["foo"].get()

    def test_argument_overrides_default(self):
        self.config.add({"foo": "baz"})

        self.parser.add_argument("--foo", metavar="BAR")
        self._parse("--foo bar")
        assert self.config["foo"].get() == "bar"

    def test_nested_destination_single(self):
        self.parser.add_argument("--one", dest="one.foo")
        self.parser.add_argument("--two", dest="one.two.foo")
        self._parse("--two TWO", dots=True)
        assert self.config["one"]["two"]["foo"].get() == "TWO"

    def test_nested_destination_nested(self):
        self.parser.add_argument("--one", dest="one.foo")
        self.parser.add_argument("--two", dest="one.two.foo")
        self._parse("--two TWO --one ONE", dots=True)
        assert self.config["one"]["foo"].get() == "ONE"
        assert self.config["one"]["two"]["foo"].get() == "TWO"

    def test_nested_destination_nested_rev(self):
        self.parser.add_argument("--one", dest="one.foo")
        self.parser.add_argument("--two", dest="one.two.foo")
        # Reverse to ensure order doesn't matter
        self._parse("--one ONE --two TWO", dots=True)
        assert self.config["one"]["foo"].get() == "ONE"
        assert self.config["one"]["two"]["foo"].get() == "TWO"

    def test_nested_destination_clobber(self):
        self.parser.add_argument("--one", dest="one.two")
        self.parser.add_argument("--two", dest="one.two.foo")
        self._parse("--two TWO --one ONE", dots=True)
        # Clobbered
        assert self.config["one"]["two"].get() == {"foo": "TWO"}
        assert self.config["one"]["two"]["foo"].get() == "TWO"

    def test_nested_destination_clobber_rev(self):
        # Reversed order
        self.parser.add_argument("--two", dest="one.two.foo")
        self.parser.add_argument("--one", dest="one.two")
        self._parse("--one ONE --two TWO", dots=True)
        # Clobbered just the same
        assert self.config["one"]["two"].get() == {"foo": "TWO"}
        assert self.config["one"]["two"]["foo"].get() == "TWO"


class ArgparseDefaultsTest(unittest.TestCase):
    def setUp(self):
        self.config = confuse.Configuration("test", read=False)
        self.parser = argparse.ArgumentParser()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

        config_path = Path(self.temp_dir.name) / "config.yaml"
        config_path.write_text(
            """
name: from-config
enabled: true
verbosity: 3
items:
  - config-item
section:
  value: config-value
  other: retained
bar: config-bar
"""
        )
        self.config.set_file(str(config_path))

    def _parser(self):
        self.parser.add_argument("--name", default="default-value")
        self.parser.add_argument("--enable", action="store_true", default=False)
        self.parser.add_argument(
            "--disable",
            action="store_false",
            dest="enabled",
            default=False,
        )
        self.parser.add_argument("-v", "--verbosity", action="count", default=0)
        self.parser.add_argument("--items", action="append", default=[])
        self.parser.add_argument(
            "--section-value",
            dest="section.value",
            default="default-value",
        )

        group = self.parser.add_mutually_exclusive_group()
        group.add_argument(
            "--group-enable",
            action="store_true",
            dest="group_enabled",
            default=False,
        )
        group.add_argument(
            "--group-disable",
            action="store_false",
            dest="group_enabled",
            default=False,
        )

        subparsers = self.parser.add_subparsers(dest="command")
        child = subparsers.add_parser("child")
        child.add_argument("--bar", default="default-bar")
        child.add_argument(
            "--child-disable",
            action="store_false",
            dest="enabled",
            default=False,
        )
        return self.parser

    def _set_args(self, *args):
        return self.config.set_argparse_args(
            self._parser(), list(args), dots=True
        )

    def test_config_used_when_arguments_are_absent(self):
        self._set_args()

        assert self.config["name"].get() == "from-config"
        assert self.config["enabled"].get() is True
        assert self.config["verbosity"].get() == 3
        assert self.config["items"].get() == ["config-item"]
        assert self.config["section"]["value"].get() == "config-value"
        assert self.config["section"]["other"].get() == "retained"
        assert dict(self.config.sources[0]) == {}

    def test_explicit_arguments_override_config(self):
        self._set_args(
            "--name",
            "cli-value",
            "--enable",
            "-vv",
            "--items",
            "one",
            "--items",
            "two",
            "--section-value",
            "cli-value",
            "--group-enable",
        )

        assert self.config["name"].get() == "cli-value"
        assert self.config["enabled"].get() is True
        assert self.config["verbosity"].get() == 2
        assert self.config["items"].get() == ["one", "two"]
        assert self.config["section"]["value"].get() == "cli-value"
        assert self.config["section"]["other"].get() == "retained"
        assert self.config["group_enabled"].get() is True

    def test_explicit_value_equal_to_default_still_overrides(self):
        self._set_args("--name", "default-value", "--disable")

        overlay = self.config.sources[0]
        assert overlay["name"] == "default-value"
        assert overlay["enabled"] is False
        assert self.config["name"].first()[1] is overlay
        assert self.config["enabled"].get() is False

    def test_mutually_exclusive_group_omits_unselected_option(self):
        self._set_args("--group-disable")

        overlay = self.config.sources[0]
        assert overlay == {"group_enabled": False}
        assert self.config["group_enabled"].get() is False

    def test_subcommand_defaults_remain_config_values(self):
        namespace = self._set_args("child")

        assert namespace.command == "child"
        assert namespace.bar == "default-bar"
        assert self.config["command"].get() == "child"
        assert self.config["bar"].get() == "config-bar"
        assert self.config["enabled"].get() is True

    def test_selected_subcommand_options_override_config(self):
        self._set_args(
            "child",
            "--bar",
            "default-bar",
            "--child-disable",
        )

        overlay = self.config.sources[0]
        assert overlay["command"] == "child"
        assert overlay["bar"] == "default-bar"
        assert overlay["enabled"] is False
        assert self.config["bar"].first()[1] is overlay
        assert self.config["bar"].get() == "default-bar"
        assert self.config["enabled"].get() is False

    def test_falsy_positional_values_are_explicit(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("count", type=int)
        config = confuse.Configuration("test", read=False)
        config.add({"count": 3})

        config.set_argparse_args(parser, ["0"])

        assert config["count"].get() == 0

    def test_absent_optional_positional_uses_config(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("name", nargs="?", default="default-value")
        config = confuse.Configuration("test", read=False)
        config.add({"name": "from-config"})

        config.set_argparse_args(parser, [])

        assert config["name"].get() == "from-config"
        assert dict(config.sources[0]) == {}

    def test_explicit_none_is_retained_as_override(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("--name", nargs="?", default=None)
        config = confuse.Configuration("test", read=False)
        config.add({"name": "from-config"})

        config.set_argparse_args(parser, ["--name"])

        assert dict(config.sources[0]) == {"name": None}
        assert config["name"].get() is None


class OptparseTest(unittest.TestCase):
    def setUp(self):
        self.config = confuse.Configuration("test", read=False)
        self.parser = optparse.OptionParser()

    def _parse(self, args, **kwargs):
        options, _ = self.parser.parse_args(args.split())
        self.config.set_args(options, **kwargs)

    def test_text_argument_parsed(self):
        self.parser.add_option("--foo", metavar="BAR")
        self._parse("--foo bar")
        assert self.config["foo"].get() == "bar"

    def test_boolean_argument_parsed(self):
        self.parser.add_option("--foo", action="store_true")
        self._parse("--foo")
        assert self.config["foo"].get()

    def test_missing_optional_argument_not_included(self):
        self.parser.add_option("--foo", metavar="BAR")
        self._parse("")
        with pytest.raises(confuse.NotFoundError):
            self.config["foo"].get()

    def test_argument_overrides_default(self):
        self.config.add({"foo": "baz"})

        self.parser.add_option("--foo", metavar="BAR")
        self._parse("--foo bar")
        assert self.config["foo"].get() == "bar"

    def test_nested_destination_single(self):
        self.parser.add_option("--one", dest="one.foo")
        self.parser.add_option("--two", dest="one.two.foo")
        self._parse("--two TWO", dots=True)
        assert self.config["one"]["two"]["foo"].get() == "TWO"

    def test_nested_destination_nested(self):
        self.parser.add_option("--one", dest="one.foo")
        self.parser.add_option("--two", dest="one.two.foo")
        self._parse("--two TWO --one ONE", dots=True)
        assert self.config["one"]["foo"].get() == "ONE"
        assert self.config["one"]["two"]["foo"].get() == "TWO"

    def test_nested_destination_nested_rev(self):
        self.parser.add_option("--one", dest="one.foo")
        self.parser.add_option("--two", dest="one.two.foo")
        # Reverse to ensure order doesn't matter
        self._parse("--one ONE --two TWO", dots=True)
        assert self.config["one"]["foo"].get() == "ONE"
        assert self.config["one"]["two"]["foo"].get() == "TWO"


class GenericNamespaceTest(unittest.TestCase):
    def setUp(self):
        self.config = confuse.Configuration("test", read=False)

    def test_value_added_to_root(self):
        self.config.set_args(Namespace(foo="bar"))
        assert self.config["foo"].get() == "bar"

    def test_value_added_to_subview(self):
        self.config["baz"].set_args(Namespace(foo="bar"))
        assert self.config["baz"]["foo"].get() == "bar"

    def test_nested_namespace(self):
        args = Namespace(first="Hello", nested=Namespace(second="World"))
        self.config.set_args(args, dots=True)
        assert self.config["first"].get() == "Hello"
        assert self.config["nested"]["second"].get() == "World"
