"""Tests for named templates and forward references (`Ref`)."""

from __future__ import annotations

import pytest

import confuse

from . import _root


# A classic recursive tree: a menu node has children that are themselves
# menu nodes. The template references itself by name, so no recursive
# Python object has to be constructed.
NODE_TEMPLATE = {
    "title": str,
    "children": confuse.Sequence(confuse.Ref("node")),
}


class TestSelfRecursion:
    def test_self_referencing_template(self):
        config = _root(
            {
                "menu": {
                    "title": "root",
                    "children": [
                        {"title": "a", "children": []},
                        {
                            "title": "b",
                            "children": [{"title": "c", "children": []}],
                        },
                    ],
                }
            }
        )
        config.add_template("node", NODE_TEMPLATE)

        menu = config.get({"menu": confuse.Ref("node")}).menu
        assert menu.title == "root"
        assert [child.title for child in menu.children] == ["a", "b"]
        assert menu.children[1].children[0].title == "c"

    def test_empty_recursive_collection(self):
        config = _root({"menu": {"title": "only", "children": []}})
        config.add_template("node", NODE_TEMPLATE)
        menu = config.get({"menu": confuse.Ref("node")}).menu
        assert menu.children == []

    def test_deep_type_error_keeps_full_key_path(self):
        config = _root(
            {
                "menu": {
                    "title": "root",
                    "children": [
                        {
                            "title": "a",
                            "children": [
                                {
                                    "title": "deep",
                                    "children": [{"title": 99}],
                                }
                            ],
                        }
                    ],
                }
            }
        )
        config.add_template("node", NODE_TEMPLATE)
        with pytest.raises(confuse.ConfigTypeError) as exc:
            config.get({"menu": confuse.Ref("node")})
        assert (
            "menu.children#0.children#0.children#0.title" in str(exc.value)
        )

    def test_deep_type_error_in_sequence_item(self):
        config = _root(
            {
                "menu": {
                    "title": "root",
                    "children": [
                        {"title": "ok", "children": []},
                        {"title": 3, "children": []},
                    ],
                }
            }
        )
        config.add_template("node", NODE_TEMPLATE)
        with pytest.raises(confuse.ConfigTypeError) as exc:
            config.get({"menu": confuse.Ref("node")})
        assert "menu.children#1.title" in str(exc.value)

    def test_missing_required_value_is_not_found(self):
        config = _root({})
        config.add_template("node", NODE_TEMPLATE)
        with pytest.raises(confuse.NotFoundError) as exc:
            config.get({"menu": confuse.Ref("node")})
        assert "menu" in str(exc.value)


# Two named templates referencing each other: a bookmark is either a URL
# or a folder containing more bookmarks.
BOOKMARK_ENTRY = confuse.OneOf([confuse.Ref("url"), confuse.Ref("folder")])


def register_bookmarks(config: confuse.RootView) -> None:
    config.add_template("url", {"url": str})
    config.add_template(
        "folder",
        {
            "title": str,
            "bookmarks": confuse.Sequence(BOOKMARK_ENTRY),
        },
    )


class TestMutualRecursion:
    def test_mutually_recursive_templates(self):
        config = _root(
            {
                "bookmarks": [
                    {"url": "https://example.com"},
                    {
                        "title": "nested",
                        "bookmarks": [{"url": "https://sub.example.com"}],
                    },
                ]
            }
        )
        register_bookmarks(config)

        result = config.get({"bookmarks": confuse.Sequence(BOOKMARK_ENTRY)})
        assert result.bookmarks[0]["url"] == "https://example.com"
        nested = result.bookmarks[1]
        assert nested.title == "nested"
        assert nested.bookmarks[0]["url"] == "https://sub.example.com"

    def test_mutual_recursion_deep_type_error_path(self):
        # Two templates reference each other through alternating child
        # lists (no OneOf, which discards candidates' inner errors). A
        # type error at the deepest level keeps its full key path.
        config = _root(
            {
                "tree": {
                    "name": "a",
                    "b_children": [
                        {
                            "label": "b",
                            "a_children": [
                                {
                                    "name": "c",
                                    "b_children": [{"label": 7}],
                                }
                            ],
                        }
                    ],
                }
            }
        )
        config.add_template(
            "a_node",
            {
                "name": str,
                "b_children": confuse.Sequence(confuse.Ref("b_node")),
            },
        )
        config.add_template(
            "b_node",
            {
                "label": str,
                "a_children": confuse.Sequence(confuse.Ref("a_node")),
            },
        )
        with pytest.raises(confuse.ConfigTypeError) as exc:
            config.get({"tree": confuse.Ref("a_node")})
        path = "tree.b_children#0.a_children#0.b_children#0.label"
        assert path in str(exc.value)

    def test_no_branch_matches_reports_value_error(self):
        config = _root({"bookmarks": [{"neither": True}]})
        register_bookmarks(config)
        with pytest.raises(confuse.ConfigValueError):
            config.get({"bookmarks": confuse.Sequence(BOOKMARK_ENTRY)})


class TestForwardReference:
    def test_referenced_template_registered_later(self):
        config = _root({"v": {"n": 5}})
        config.add_template("outer", {"n": confuse.Ref("inner")})
        config.add_template("inner", confuse.Integer())
        assert config.get({"v": confuse.Ref("outer")}) == {"v": {"n": 5}}

    def test_unknown_name_raises_template_error(self):
        config = _root({"x": 1})
        with pytest.raises(confuse.ConfigTemplateError) as exc:
            config.get({"x": confuse.Ref("missing")})
        assert "missing" in str(exc.value)

    def test_named_template_can_carry_default(self):
        config = _root({})
        config.add_template("number", confuse.Integer(42))
        assert config.get({"x": confuse.Ref("number")}) == {"x": 42}


class TestCycleDetection:
    def test_direct_self_cycle_without_data(self):
        config = _root({})
        config.add_template("loop", confuse.Ref("loop"))
        with pytest.raises(confuse.ConfigTemplateError) as exc:
            config.get({"x": confuse.Ref("loop")})
        assert "loop" in str(exc.value)

    def test_mutual_cycle_without_data(self):
        config = _root({})
        config.add_template("a", confuse.Ref("b"))
        config.add_template("b", confuse.Ref("a"))
        with pytest.raises(confuse.ConfigTemplateError) as exc:
            config.get({"x": confuse.Ref("a")})
        message = str(exc.value)
        assert "a" in message and "b" in message

    def test_cycle_reached_through_missing_key(self):
        # The root contains unrelated data, but the referenced branch
        # runs out of data while still hopping between templates.
        config = _root({"present": 1})
        config.add_template("p", confuse.Ref("q"))
        config.add_template("q", confuse.Ref("p"))
        with pytest.raises(confuse.ConfigTemplateError):
            config.get({"x": confuse.Ref("p")})

    def test_same_position_cycle_with_data(self):
        # An Optional reference re-validates the *same* view whenever a
        # non-null value is present. Without descending into child data,
        # the reference loops in place and must be reported.
        config = _root({"m": {"k": "v"}})
        config.add_template(
            "loop", confuse.Optional(confuse.Ref("loop"), None)
        )
        with pytest.raises(confuse.ConfigTemplateError) as exc:
            config.get({"m": confuse.Ref("loop")})
        assert "loop" in str(exc.value)

    def test_cycle_inside_oneof_candidate_is_still_detected(self):
        config = _root({"m": {}})
        config.add_template(
            "x", confuse.OneOf([confuse.Ref("y"), confuse.Integer()])
        )
        config.add_template("y", confuse.Ref("x"))
        with pytest.raises(confuse.ConfigTemplateError):
            config.get({"m": confuse.Ref("x")})

    def test_genuine_recursion_does_not_trigger_cycle_error(self):
        # The same template name is used repeatedly, but every use is at
        # a deeper data-backed position, so no cycle is reported.
        config = _root({"root": {"a": {"b": {"c": 1}}}})
        config.add_template(
            "layer",
            confuse.OneOf(
                [
                    confuse.Integer(),
                    confuse.MappingValues(confuse.Ref("layer")),
                ]
            ),
        )
        assert config.get({"root": confuse.Ref("layer")}) == {
            "root": {"a": {"b": {"c": 1}}}
        }


class TestComposition:
    @staticmethod
    def _register_task(config: confuse.RootView) -> None:
        config.add_template(
            "task",
            {
                "done": confuse.Optional(bool, False),
                "deps": confuse.Optional(
                    confuse.MappingValues(confuse.Ref("task")), {}
                ),
            },
        )

    def test_optional_defaults_and_mapping_values(self):
        config = _root(
            {
                "tasks": {
                    "a": {"done": True, "deps": {"x": {"done": False}}},
                    "b": {"done": False},
                }
            }
        )
        self._register_task(config)
        tasks = config.get(
            {"tasks": confuse.MappingValues(confuse.Ref("task"))}
        )["tasks"]
        assert tasks["a"]["done"] is True
        assert tasks["a"]["deps"]["x"]["done"] is False
        assert tasks["b"]["done"] is False
        assert tasks["b"]["deps"] == {}

    def test_optional_null_self_reference(self):
        config = _root({"x": None})
        config.add_template("self", confuse.Optional(confuse.Ref("self")))
        assert config.get({"x": confuse.Ref("self")}) == {"x": None}

    def test_optional_missing_self_reference_uses_default(self):
        config = _root({})
        config.add_template(
            "self", confuse.Optional(confuse.Ref("self"), "fallback")
        )
        assert config.get({"x": confuse.Ref("self")}) == {
            "x": "fallback"
        }

    def test_optional_sequence_in_mutual_recursion(self):
        config = _root({"folder": {"title": "empty"}})
        config.add_template("url", {"url": str})
        config.add_template(
            "folder",
            {
                "title": confuse.Optional(str, "untitled"),
                "bookmarks": confuse.Optional(
                    confuse.Sequence(
                        confuse.OneOf(
                            [confuse.Ref("url"), confuse.Ref("folder")]
                        )
                    ),
                    [],
                ),
            },
        )
        folder = config.get({"folder": confuse.Ref("folder")}).folder
        assert folder.title == "empty"
        assert folder.bookmarks == []


class TestIsolation:
    def test_templates_do_not_leak_between_configurations(self):
        first = _root({"x": 1})
        second = _root({"x": 2})
        first.add_template("shared", confuse.Integer())

        with pytest.raises(confuse.ConfigTemplateError):
            second.get({"x": confuse.Ref("shared")})

    def test_same_name_can_mean_different_templates(self):
        first = _root({"x": 1})
        second = _root({"x": "two"})
        first.add_template("shared", confuse.Integer())
        second.add_template("shared", confuse.String())

        assert first.get({"x": confuse.Ref("shared")}) == {"x": 1}
        assert second.get({"x": confuse.Ref("shared")}) == {"x": "two"}

    def test_validation_chain_is_cleaned_up(self):
        config = _root(
            {
                "menu": {
                    "title": "root",
                    "children": [{"title": "a", "children": []}],
                }
            }
        )
        config.add_template("node", NODE_TEMPLATE)
        config.get({"menu": confuse.Ref("node")})
        assert config._active_refs == []


class TestRegistration:
    def test_invalid_name_rejected(self):
        config = _root({})
        for bad in ("", 7):
            with pytest.raises(ValueError):
                config.add_template(bad, confuse.Integer())

    def test_reregistering_replaces_template(self):
        config = _root({"x": 1})
        config.add_template("t", confuse.String())
        config.add_template("t", confuse.Integer())
        assert config.get({"x": confuse.Ref("t")}) == {"x": 1}

    def test_shorthand_templates_are_converted(self):
        config = _root({"x": {"n": 5}})
        config.add_template("t", {"n": int})
        assert isinstance(config.named_templates["t"], confuse.MappingTemplate)
        assert config.get({"x": confuse.Ref("t")}) == {"x": {"n": 5}}
