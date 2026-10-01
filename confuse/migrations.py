"""Explicit, versioned configuration migrations.

When an application renames settings (e.g., moving ``server.port`` to
``http.listen.port``) or changes value formats (e.g., turning a timeout
in seconds into a string with a unit), this module lets the application
ship *migration rules* that bring old configuration files up to date.

The framework is deliberately restricted for safety:

- Rules can only *move* a key path, *remove* a key path, or *convert* a
  value in place. Arbitrary edits are not possible.
- Value conversions are functions registered by the application, in
  code, under a name. Rules refer to converters by name only, so a
  configuration file can never supply executable code.
- If both the old and the new location of a moved setting exist, a
  `MigrationConflictError` is raised. The migrator never guesses which
  value should win.

Migration is a two-step process. First, `Migrator.plan` inspects one
specific configuration source (other layers of the configuration are
left alone) and produces a `MigrationPlan` describing every operation
with its old and new values. The caller may inspect the plan and then
apply it with `MigrationPlan.apply`, which updates the version marker
and, for file-backed sources, atomically replaces the file so a failure
can never leave a half-migrated file behind. Planning a source that is
already at the target version yields an empty plan, so loading the same
version repeatedly is idempotent.
"""

from __future__ import annotations

__all__ = [
    "Convert",
    "MigrationPlan",
    "Migrator",
    "Move",
    "Operation",
    "Remove",
]

import copy
import os
import tempfile
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import yaml

from . import yaml_util
from .exceptions import (
    MigrationConflictError,
    MigrationError,
    MigrationGapError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

DEFAULT_VERSION_KEY = "config_version"
INITIAL_VERSION = 1

MOVE = "move"
REMOVE = "remove"
CONVERT = "convert"

# Helpers for working with nested key paths.


def _parse_path(path: str | Sequence[str]) -> tuple[str, ...]:
    """Normalize a dotted string or a sequence of keys into a tuple."""
    if isinstance(path, str):
        parts = tuple(part for part in path.split(".") if part)
    else:
        parts = tuple(path)
    if not parts:
        raise MigrationError("a configuration path must not be empty")
    return parts


def _format_path(path: Sequence[str]) -> str:
    return ".".join(path)


def _get_path(data: dict[str, Any], path: Sequence[str]) -> Any:
    """Return the value at `path` or raise `KeyError` if it is absent."""
    node: Any = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            raise KeyError(key)
        node = node[key]
    return node


def _set_path(data: dict[str, Any], path: Sequence[str], value: Any) -> None:
    """Set the value at `path`, creating intermediate dictionaries.

    A `MigrationError` is raised if an existing non-dictionary value
    blocks the path, rather than silently clobbering it.
    """
    node = data
    for key in path[:-1]:
        child = node.get(key)
        if child is None:
            child = {}
            node[key] = child
        elif not isinstance(child, dict):
            raise MigrationError(
                f"cannot create {_format_path(path)}: "
                f"{key!r} already holds a non-dictionary value"
            )
        node = child
    node[path[-1]] = value


def _del_path(data: dict[str, Any], path: Sequence[str]) -> None:
    """Remove the value at `path`, pruning dictionaries left empty."""
    nodes = []
    node: Any = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return
        nodes.append((node, key))
        node = node[key]
    parent, key = nodes[-1]
    del parent[key]
    for parent, key in reversed(nodes[:-1]):
        child = parent[key]
        if isinstance(child, dict) and not child:
            del parent[key]
        else:
            break


# Migration rules.


@dataclass(frozen=True)
class Move:
    """Move the value at `old` to the key path `new`.

    If `convert` names a registered converter, the value is transformed
    during the move. If both paths exist in the source, planning the
    migration raises a `MigrationConflictError`.
    """

    old: str | Sequence[str]
    new: str | Sequence[str]
    convert: str | None = None


@dataclass(frozen=True)
class Remove:
    """Delete the value at `path`."""

    path: str | Sequence[str]


@dataclass(frozen=True)
class Convert:
    """Replace the value at `path` with `convert` applied to it.

    `convert` names a converter registered with the migrator's
    `ConverterRegistry`.
    """

    path: str | Sequence[str]
    convert: str


Rule = Move | Remove | Convert


@dataclass(frozen=True)
class Operation:
    """One planned change, recording the old and the new value."""

    kind: str
    old_path: tuple[str, ...]
    old_value: Any
    new_path: tuple[str, ...] | None = None
    new_value: Any = None


class ConverterRegistry:
    """A named registry of value conversion functions.

    Converters are ordinary Python callables registered by the
    application. Migration rules refer to them by name, so configuration
    data itself can never cause code to run.
    """

    def __init__(self) -> None:
        self._converters: dict[str, Callable[[Any], Any]] = {}

    def register(
        self, name: str, func: Callable[[Any], Any] | None = None
    ) -> Any:
        """Register `func` under `name`. Usable as a decorator."""
        if func is None:

            def decorator(f: Callable[[Any], Any]) -> Callable[[Any], Any]:
                self.register(name, f)
                return f

            return decorator
        if name in self._converters:
            raise MigrationError(f"a converter named {name!r} is already registered")
        if not callable(func):
            raise TypeError("converter must be callable")
        self._converters[name] = func
        return func

    def convert(self, name: str, value: Any) -> Any:
        """Apply the converter registered as `name` to `value`."""
        try:
            func = self._converters[name]
        except KeyError:
            raise MigrationError(
                f"no value converter is registered under the name {name!r}"
            )
        try:
            return func(value)
        except MigrationError:
            raise
        except Exception as exc:
            raise MigrationError(
                f"converter {name!r} failed for value {value!r}: {exc}"
            ) from exc


class MigrationPlan:
    """The result of planning a migration for one configuration source.

    `operations` lists every planned change with its old and new values
    so the caller can review them. `data` is the full planned result,
    including the updated version marker. Nothing changes until `apply`
    is called.
    """

    def __init__(
        self,
        migrator: Migrator,
        source: dict[str, Any],
        from_version: int,
        to_version: int,
        operations: list[Operation],
        data: dict[str, Any],
    ) -> None:
        self._migrator = migrator
        self.source = source
        self.from_version = from_version
        self.to_version = to_version
        self.operations = operations
        self.data = data

    def __bool__(self) -> bool:
        """Whether applying this plan would change anything."""
        return bool(self.operations) or self.from_version != self.to_version

    def __repr__(self) -> str:
        return (
            f"MigrationPlan({self.from_version} -> {self.to_version}, "
            f"{len(self.operations)} operations)"
        )

    def apply(self) -> dict[str, Any]:
        """Apply the planned migration to the source.

        For file-backed sources (e.g., `YamlSource`), the migrated data
        is written atomically: a temporary file is synced and moved over
        the original, so a failure never leaves a half-migrated file.
        An empty plan (source already at the target version) is a no-op
        and does not touch the file. Returns the migrated data.
        """
        if not self:
            return self.data
        current = self._migrator.current_version(self.source)
        if current != self.from_version:
            raise MigrationError(
                f"this plan was made for version {self.from_version}, but "
                f"the source is now at version {current}; re-plan the migration"
            )
        filename = getattr(self.source, "filename", None)
        if filename:
            self._write_file(filename, self.data)
        self.source.clear()
        self.source.update(self.data)
        return self.data

    @staticmethod
    def _write_file(filename: str, data: dict[str, Any]) -> None:
        """Atomically replace `filename` with `data` serialized as YAML."""
        text = yaml.dump(
            data,
            Dumper=yaml_util.Dumper,
            default_flow_style=None,
            indent=4,
            width=1000,
        )
        directory = os.path.dirname(os.path.abspath(filename))
        fd, tmp_path = tempfile.mkstemp(
            dir=directory, prefix=".confuse-migrate-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(text)
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp_path, filename)
        except Exception as exc:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise MigrationError(
                f"could not write migrated configuration to {filename}: {exc}"
            ) from exc


class Migrator:
    """Plans and applies versioned migrations for configuration sources.

    Rules are registered per target version with `add_migration`: the
    rules for version *N* describe how to migrate a source from version
    *N - 1* to *N*. A source's current version is read from
    `version_key` (defaulting to ``config_version``); sources without
    the key are treated as `initial_version`.
    """

    def __init__(
        self,
        target_version: int | None = None,
        *,
        version_key: str | Sequence[str] = DEFAULT_VERSION_KEY,
        initial_version: int = INITIAL_VERSION,
        registry: ConverterRegistry | None = None,
    ) -> None:
        self.version_key = _parse_path(version_key)
        self.initial_version = initial_version
        self.registry = registry if registry is not None else ConverterRegistry()
        self._migrations: dict[int, list[Rule]] = {}
        self._explicit_target = target_version

    @property
    def target_version(self) -> int:
        """The version sources are migrated to.

        Defaults to the highest version for which rules are registered.
        """
        if self._explicit_target is not None:
            return self._explicit_target
        return max(self._migrations, default=self.initial_version)

    def register_converter(
        self, name: str, func: Callable[[Any], Any] | None = None
    ) -> Any:
        """Register a value converter. Usable as a decorator."""
        return self.registry.register(name, func)

    def add_migration(self, version: int, rules: Iterable[Rule]) -> None:
        """Register the rules that migrate from `version - 1` to `version`."""
        if not isinstance(version, int) or isinstance(version, bool):
            raise MigrationError("migration version must be an integer")
        if version <= self.initial_version:
            raise MigrationError(
                f"migration version must be greater than the initial "
                f"version {self.initial_version}"
            )
        if version in self._migrations:
            raise MigrationError(
                f"migration rules for version {version} are already registered"
            )
        self._migrations[version] = list(rules)

    def current_version(self, source: dict[str, Any]) -> int:
        """Read the version marker from a configuration source."""
        try:
            value = _get_path(source, self.version_key)
        except KeyError:
            return self.initial_version
        if not isinstance(value, int) or isinstance(value, bool):
            raise MigrationError(
                f"the version marker at {_format_path(self.version_key)} "
                f"must be an integer, got {value!r}"
            )
        return value

    def plan(self, source: dict[str, Any]) -> MigrationPlan:
        """Build a `MigrationPlan` for one specific configuration source.

        Only the given source is examined; other layers of a layered
        configuration remain independent and untouched. Raises
        `MigrationGapError` if rules for an intermediate version are
        missing and `MigrationConflictError` if a moved setting exists
        at both its old and its new location.
        """
        current = self.current_version(source)
        target = self.target_version
        if current > target:
            raise MigrationError(
                f"the source is at version {current}, which is newer than "
                f"the target version {target}"
            )
        data = copy.deepcopy(dict(source))
        operations: list[Operation] = []
        for version in range(current + 1, target + 1):
            try:
                rules = self._migrations[version]
            except KeyError:
                raise MigrationGapError(
                    f"cannot migrate from version {current} to {target}: "
                    f"no rules are registered for version {version}, so the "
                    f"migration chain breaks at version {version}"
                ) from None
            for rule in rules:
                operation = self._apply_rule(rule, data)
                if operation is not None:
                    operations.append(operation)
        _set_path(data, self.version_key, target)
        return MigrationPlan(self, source, current, target, operations, data)

    def migrate(self, source: dict[str, Any]) -> dict[str, Any]:
        """Plan a migration for `source` and apply it immediately."""
        return self.plan(source).apply()

    def _apply_rule(self, rule: Rule, data: dict[str, Any]) -> Operation | None:
        """Apply one rule to `data` and describe it, or return None if
        the rule's subject is absent (making re-runs idempotent).
        """
        if isinstance(rule, Move):
            old = _parse_path(rule.old)
            new = _parse_path(rule.new)
            try:
                old_value = _get_path(data, old)
            except KeyError:
                return None
            try:
                existing = _get_path(data, new)
            except KeyError:
                pass
            else:
                raise MigrationConflictError(
                    f"cannot move {_format_path(old)} to {_format_path(new)}: "
                    f"both exist (old value {old_value!r}, new value "
                    f"{existing!r}); resolve the conflict manually"
                )
            if rule.convert is not None:
                new_value = self.registry.convert(rule.convert, old_value)
            else:
                new_value = old_value
            _set_path(data, new, new_value)
            _del_path(data, old)
            return Operation(MOVE, old, old_value, new, new_value)
        elif isinstance(rule, Convert):
            path = _parse_path(rule.path)
            try:
                old_value = _get_path(data, path)
            except KeyError:
                return None
            new_value = self.registry.convert(rule.convert, old_value)
            _set_path(data, path, new_value)
            return Operation(CONVERT, path, old_value, path, new_value)
        elif isinstance(rule, Remove):
            path = _parse_path(rule.path)
            try:
                old_value = _get_path(data, path)
            except KeyError:
                return None
            _del_path(data, path)
            return Operation(REMOVE, path, old_value)
        else:
            raise MigrationError(f"unknown migration rule {rule!r}")
