import argparse
import os
import optparse
import tempfile
import unittest
from argparse import Namespace

import pytest

import confuse


class ArgparseOverlayTest(unittest.TestCase):
    """Tests for `set_argparse`, which overlays only arguments actually
    present on the command line, leaving parser defaults out of the
    override layer.
    """

    def setUp(self):
        self.config = confuse.Configuration("test", read=False)
        self.parser = argparse.ArgumentParser()
        self._config_files = []

    def tearDown(self):
        for path in self._config_files:
            os.remove(path)

    def _config_from_file(self, yaml_text):
        fd, path = tempfile.mkstemp(suffix=".yaml")
        self._config_files.append(path)
        with os.fdopen(fd, "w") as f:
            f.write(yaml_text)
        self.config.set_file(path)

    def _parse(self, args, **kwargs):
        self.config.set_argparse(self.parser, args.split(), **kwargs)

    def test_parser_default_does_not_override_config_file(self):
        self._config_from_file("foo: from_file\n")
        self.parser.add_argument("--foo", default="from_default")
        self._parse("")
        assert self.config["foo"].get() == "from_file"

    def test_command_line_value_overrides_config_file(self):
        self._config_from_file("foo: from_file\n")
        self.parser.add_argument("--foo", default="from_default")
        self._parse("--foo from_cli")
        assert self.config["foo"].get() == "from_cli"

    def test_explicit_value_equal_to_default_still_overrides(self):
        self._config_from_file("foo: from_file\n")
        self.parser.add_argument("--foo", default="from_default")
        self._parse("--foo from_default")
        assert self.config["foo"].get() == "from_default"

    def test_absent_argument_not_in_overlay(self):
        self.parser.add_argument("--foo", default="from_default")
        self._parse("")
        with pytest.raises(confuse.NotFoundError):
            self.config["foo"].get()

    def test_store_true(self):
        self._config_from_file("verbose: false\n")
        self.parser.add_argument("--verbose", action="store_true")
        self._parse("")
        assert self.config["verbose"].get() is False
        self.config.clear()
        self._config_from_file("verbose: false\n")
        self._parse("--verbose")
        assert self.config["verbose"].get() is True

    def test_store_false(self):
        self._config_from_file("cache: true\n")
        self.parser.add_argument(
            "--no-cache", dest="cache", action="store_false"
        )
        self._parse("")
        assert self.config["cache"].get() is True
        self.config.clear()
        self._config_from_file("cache: true\n")
        self._parse("--no-cache")
        assert self.config["cache"].get() is False

    def test_append_list_argument(self):
        self._config_from_file("include:\n  - from_file\n")
        self.parser.add_argument("--include", action="append", default=[])
        self._parse("")
        assert self.config["include"].get() == ["from_file"]

    def test_append_list_argument_overrides_config_file(self):
        self._config_from_file("include:\n  - from_file\n")
        self.parser.add_argument("--include", action="append", default=[])
        self._parse("--include a --include b")
        assert self.config["include"].get() == ["a", "b"]

    def test_count_action(self):
        self._config_from_file("verbosity: 1\n")
        self.parser.add_argument(
            "-v", "--verbosity", action="count", default=0
        )
        self._parse("")
        assert self.config["verbosity"].get() == 1
        self.config.clear()
        self._config_from_file("verbosity: 1\n")
        self._parse("-vvv")
        assert self.config["verbosity"].get() == 3

    def test_mutually_exclusive_group(self):
        self._config_from_file("format: json\n")
        group = self.parser.add_mutually_exclusive_group()
        group.add_argument(
            "--json", dest="format", action="store_const", const="json"
        )
        group.add_argument(
            "--yaml", dest="format", action="store_const", const="yaml"
        )
        self._parse("")
        assert self.config["format"].get() == "json"
        self.config.clear()
        self._config_from_file("format: json\n")
        self._parse("--yaml")
        assert self.config["format"].get() == "yaml"

    def test_subcommand(self):
        self._config_from_file("port: 9000\n")
        subparsers = self.parser.add_subparsers(dest="command")
        serve = subparsers.add_parser("serve")
        serve.add_argument("--port", type=int, default=8080)
        self._parse("serve")
        assert self.config["command"].get() == "serve"
        # Subparser default does not clobber the config file value.
        assert self.config["port"].get() == 9000

    def test_subcommand_argument_overrides_config_file(self):
        self._config_from_file("port: 9000\n")
        subparsers = self.parser.add_subparsers(dest="command")
        serve = subparsers.add_parser("serve")
        serve.add_argument("--port", type=int, default=8080)
        self._parse("serve --port 80")
        assert self.config["port"].get() == 80

    def test_no_subcommand_leaves_no_overlay(self):
        subparsers = self.parser.add_subparsers(dest="command")
        serve = subparsers.add_parser("serve")
        serve.add_argument("--port", type=int, default=8080)
        self._parse("")
        with pytest.raises(confuse.NotFoundError):
            self.config["command"].get()
        with pytest.raises(confuse.NotFoundError):
            self.config["port"].get()

    def test_nested_destination_dots(self):
        self._config_from_file("logging:\n  level: debug\n  other: x\n")
        self.parser.add_argument("--level", dest="logging.level",
                                 default="INFO")
        self._parse("")
        assert self.config["logging"]["level"].get() == "debug"
        self.config.clear()
        self._config_from_file("logging:\n  level: debug\n  other: x\n")
        self._parse("--level warning", dots=True)
        assert self.config["logging"]["level"].get() == "warning"
        assert self.config["logging"]["other"].get() == "x"

    def test_positional_argument(self):
        self.parser.add_argument("input", nargs="?")
        self._parse("file.txt")
        assert self.config["input"].get() == "file.txt"

    def test_parser_defaults_restored_after_parsing(self):
        self.parser.add_argument("--foo", default="from_default")
        self._parse("")
        assert self.parser.parse_args([]).foo == "from_default"


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
