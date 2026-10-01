from __future__ import annotations

__all__ = [
    "ConfigMigrator",
    "MISSING",
    "MigrationChange",
    "MigrationPlan",
    "MoveRule",
    "ValueConverter",
]

import copy
import os
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import yaml

from .exceptions import (
    ConfigMigrationError,
    MigrationConflictError,
    MissingMigrationError,
)
from .sources import ConfigSource, YamlSource

_YamlSourceType = YamlSource
from .yaml_util import Dumper


ConfigPath = tuple[str, ...]
ValueConverter = Callable[[Any], Any]


class _Missing:
    """Sentinel for a value that is absent from a configuration source."""

    def __repr__(self) -> str:
        return "MISSING"


MISSING = _Missing()
_MISSING = MISSING


@dataclass(frozen=True)
class MoveRule:
    """Move the value at one path to another path."""

    source: ConfigPath
    destination: ConfigPath
    convert: str | None = None



@dataclass(frozen=True)
class MigrationChange:
    """A single planned value change."""

    operation: str
    old: Any
    new: Any
    source: ConfigPath | None = None
    destination: ConfigPath | None = None


@dataclass(frozen=True)
class _MigrationRules:
    version: int
    moves: tuple[MoveRule, ...]
    deletes: tuple[ConfigPath, ...]
    converts: tuple[tuple[ConfigPath, ValueConverter], ...]


def _as_path(value: Any) -> ConfigPath:
    if isinstance(value, str):
        return (value,)
    if (
        isinstance(value, tuple)
        and value
        and all(isinstance(part, str) and part for part in value)
    ):
        return value
    raise ConfigMigrationError("configuration paths must be non-empty strings")


def _paths_overlap(left: ConfigPath, right: ConfigPath) -> bool:
    return left == right or left[: len(right)] == right or right[: len(left)] == left


def _format_path(path: ConfigPath) -> str:
    return ".".join(path)


def _get(data: dict[str, Any], path: ConfigPath) -> Any:
    current: Any = data
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _remove(data: dict[str, Any], path: ConfigPath) -> None:
    parents: list[tuple[dict[str, Any], str]] = []
    current: Any = data
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return
        parents.append((current, part))
        current = current[part]

    parents[-1][0].pop(parents[-1][1])
    for parent, part in reversed(parents[:-1]):
        if isinstance(parent[part], dict) and not parent[part]:
            parent.pop(part)
        else:
            break


def _set(data: dict[str, Any], path: ConfigPath, value: Any) -> None:
    current: Any = data
    for part in path[:-1]:
        existing = (
            current.get(part, _MISSING) if isinstance(current, dict) else _MISSING
        )
        if existing is _MISSING:
            existing = {}
            current[part] = existing
        if not isinstance(existing, dict):
            raise MigrationConflictError(
                f"{_format_path((part,))} is not a mapping while setting "
                f"{_format_path(path)}"
            )
        current = existing
    if not isinstance(current, dict):
        raise MigrationConflictError(f"cannot set {_format_path(path)}")
    current[path[-1]] = value


class MigrationPlan:
    """A validated set of changes for one configuration source."""

    def __init__(
        self,
        source: ConfigSource,
        changes: tuple[MigrationChange, ...],
        result: dict[str, Any],
        from_version: int,
        target_version: int,
    ) -> None:
        self.source = source
        self.changes = changes
        self.before = copy.deepcopy(dict(source))
        self.after = copy.deepcopy(result)
        self.from_version = from_version
        self.target_version = target_version
        self.applied = False

    def __bool__(self) -> bool:
        return bool(self.changes)

    def apply(self) -> None:
        """Apply the plan and persist file-backed sources atomically."""
        if self.applied:
            return
        if not self.changes:
            self.applied = True
            return

        result = copy.deepcopy(self.after)
        filename = getattr(self.source, "filename", None)
        source_type = type(self.source)
        if issubclass(source_type, _YamlSourceType) and filename is not None:
            directory = os.path.dirname(filename) or "."
            os.makedirs(directory, exist_ok=True)
            yaml_text = yaml.dump(
                result,
                Dumper=Dumper,
                default_flow_style=None,
                indent=4,
                width=1000,
            )
            temporary_name = None
            previous_source = copy.deepcopy(dict(self.source))
            source_loader = self.source.loader
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    newline="",
                    dir=directory,
                    prefix=f".{os.path.basename(filename)}.",
                    suffix=".tmp",
                    delete=False,
                ) as temporary:
                    temporary_name = temporary.name
                    if os.path.exists(filename):
                        temporary.flush()
                        os.chmod(
                            temporary_name,
                            stat.S_IMODE(os.stat(filename).st_mode),
                        )
                    temporary.write(yaml_text)
                    temporary.flush()
                    os.fsync(temporary.fileno())

                loaded_source = YamlSource(
                    temporary_name,
                    default=self.source.default,
                    base_for_paths=self.source.base_for_paths,
                    loader=source_loader,
                )
                self.source.clear()
                self.source.update(loaded_source)
                os.replace(temporary_name, filename)
                temporary_name = None
            except BaseException:
                self.source.clear()
                self.source.update(previous_source)
                raise
            finally:
                if temporary_name is not None:
                    try:
                        os.unlink(temporary_name)
                    except FileNotFoundError:
                        pass
        else:
            self.source.clear()
            self.source.update(result)
        self.applied = True


class ConfigMigrator:
    """Registered, explicit configuration migration rules."""

    def __init__(
        self,
        version_path: str | tuple[str, ...] = ("config_version",),
        initial_version: int = 0,
    ) -> None:
        self.version_path = _as_path(version_path)
        if isinstance(initial_version, bool) or not isinstance(
            initial_version, int
        ):
            raise ConfigMigrationError("initial_version must be an integer")
        self.initial_version = initial_version
        self._converters: dict[str, ValueConverter] = {}
        self._rules: dict[int, _MigrationRules] = {}

    def register_converter(
        self, name: str, converter: ValueConverter | None = None
    ) -> ValueConverter | Callable[[ValueConverter], ValueConverter]:
        """Register a named value transformation supplied by application code."""

        def register(func: ValueConverter) -> ValueConverter:
            if not callable(func):
                raise ConfigMigrationError("converter must be callable")
            self._converters[name] = func
            return func

        if converter is not None:
            return register(converter)
        return register

    def register(
        self,
        version: int,
        *,
        move: list[MoveRule | tuple[Any, Any] | tuple[Any, Any, Any]] | None = None,
        delete: list[Any] | None = None,
        convert: dict[Any, str] | None = None,
    ) -> None:
        """Register rules that upgrade a source to one explicit version."""
        self._validate_version(version)
        if version in self._rules:
            raise ConfigMigrationError(
                f"migration rules for version {version} already registered"
            )

        moves = tuple(self._as_move_rule(rule) for rule in move or [])
        deletes = tuple(_as_path(path) for path in delete or [])
        converts = tuple(
            (path, self._get_converter(converter))
            for path, converter in (convert or {}).items()
        )
        self._validate_rule_paths(version, moves, deletes, converts)
        self._rules[version] = _MigrationRules(version, moves, deletes, converts)

    def _validate_version(self, version: int) -> None:
        if isinstance(version, bool) or not isinstance(version, int):
            raise ConfigMigrationError("migration version must be an integer")
        if version <= self.initial_version:
            raise ConfigMigrationError(
                f"migration version must be greater than {self.initial_version}"
            )

    def _validate_target_version(self, version: int) -> None:
        if isinstance(version, bool) or not isinstance(version, int):
            raise ConfigMigrationError("migration version must be an integer")
        if version < self.initial_version:
            raise ConfigMigrationError(
                f"target version cannot be lower than {self.initial_version}"
            )

    def _as_move_rule(
        self, rule: MoveRule | tuple[Any, Any] | tuple[Any, Any, Any]
    ) -> MoveRule:
        if isinstance(rule, MoveRule):
            source = _as_path(rule.source)
            destination = _as_path(rule.destination)
            converter = rule.convert
        else:
            try:
                source_value, destination_value, *rest = rule
            except (TypeError, ValueError):
                raise ConfigMigrationError(
                    "move rules require a source and destination"
                )
            if len(rest) > 1:
                raise ConfigMigrationError("invalid move rule")
            source = _as_path(source_value)
            destination = _as_path(destination_value)
            converter = rest[0] if rest else None
        if source == destination:
            raise ConfigMigrationError("cannot move a path to itself")
        if converter is not None:
            converter = self._get_converter(converter)
        return MoveRule(source, destination, converter)

    def _get_converter(self, converter: str) -> ValueConverter:
        if isinstance(converter, str):
            try:
                return self._converters[converter]
            except KeyError:
                raise ConfigMigrationError(
                    f"converter {converter!r} is not registered"
                )
        raise ConfigMigrationError("converter must be registered and named")

    def _validate_rule_paths(
        self,
        version: int,
        moves: tuple[MoveRule, ...],
        deletes: tuple[ConfigPath, ...],
        converts: tuple[tuple[ConfigPath, ValueConverter], ...],
    ) -> None:
        endpoints: list[tuple[ConfigPath, str]] = []
        for path in deletes:
            endpoints.append((path, "delete"))
        for path, _ in converts:
            endpoints.append((path, "convert"))
        for move in moves:
            endpoints.append((move.source, "move"))
            endpoints.append((move.destination, "move"))

        seen: set[tuple[ConfigPath, str]] = set()
        for path, operation in endpoints:
            if _paths_overlap(path, self.version_path):
                raise ConfigMigrationError(
                    f"migration to version {version} cannot modify version path "
                    f"{_format_path(self.version_path)}"
                )
            key = (path, operation)
            if key in seen:
                raise ConfigMigrationError(
                    f"duplicate {operation} rule for {_format_path(path)}"
                )
            seen.add(key)

        for index, (path, operation) in enumerate(endpoints):
            for other_path, other_operation in endpoints[index + 1 :]:
                if _paths_overlap(path, other_path):
                    raise ConfigMigrationError(
                        f"overlapping migration rules {_format_path(path)} "
                        f"({operation}) and {_format_path(other_path)} "
                        f"({other_operation})"
                    )

    def _current_version(self, data: dict[str, Any]) -> int:
        value = _get(data, self.version_path)
        if value is _MISSING:
            return self.initial_version
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigMigrationError(
                f"{_format_path(self.version_path)} must be an integer"
            )
        return value

    def plan(
        self,
        source: ConfigSource,
        target_version: int | None = None,
    ) -> MigrationPlan:
        """Return a plan without modifying the supplied source."""
        if not isinstance(source, ConfigSource):
            raise ConfigMigrationError("migration source must be a ConfigSource")
        if target_version is None:
            target_version = max(self._rules, default=self.initial_version)
        self._validate_target_version(target_version)

        data = dict(source)
        from_version = self._current_version(data)
        if from_version == target_version:
            return MigrationPlan(source, (), data, from_version, target_version)
        if from_version > target_version:
            raise ConfigMigrationError(
                f"cannot migrate configuration from version {from_version} "
                f"back to {target_version}"
            )

        required_versions = set(range(from_version + 1, target_version + 1))
        missing_versions = sorted(required_versions.difference(self._rules))
        if missing_versions:
            breakpoint_version = missing_versions[0]
            raise MissingMigrationError(
                f"missing migration rule to version {breakpoint_version}; "
                f"cannot continue from version {breakpoint_version - 1}"
            )

        result = copy.deepcopy(data)
        changes: list[MigrationChange] = []
        for version in sorted(required_versions):
            rules = self._rules[version]
            for path in rules.deletes:
                old = _get(result, path)
                if old is not _MISSING:
                    _remove(result, path)
                    changes.append(MigrationChange("delete", old, _MISSING, path))

            for path, converter in rules.converts:
                old = _get(result, path)
                if old is not _MISSING:
                    original = copy.deepcopy(old)
                    new = self._convert_value(converter, old, path)
                    _set(result, path, new)
                    changes.append(
                        MigrationChange("convert", original, new, path, path)
                    )

            for move in rules.moves:
                old = _get(result, move.source)
                new_value = _get(result, move.destination)
                if old is not _MISSING and new_value is not _MISSING:
                    raise MigrationConflictError(
                        f"both {_format_path(move.source)} and "
                        f"{_format_path(move.destination)} are set; refusing to "
                        "guess which value should win"
                    )
                if old is not _MISSING:
                    original = copy.deepcopy(old)
                    converted = (
                        self._convert_value(move.convert, old, move.source)
                        if move.convert
                        else old
                    )
                    _remove(result, move.source)
                    _set(result, move.destination, converted)
                    changes.append(
                        MigrationChange(
                            "move",
                            original,
                            converted,
                            move.source,
                            move.destination,
                        )
                    )

        old_version = _get(result, self.version_path)
        _set(result, self.version_path, target_version)
        changes.append(
            MigrationChange(
                "version",
                self.initial_version if old_version is _MISSING else old_version,
                target_version,
                self.version_path,
                self.version_path,
            )
        )
        return MigrationPlan(
            source, tuple(changes), result, from_version, target_version
        )

    def _convert_value(
        self, converter: ValueConverter, value: Any, path: ConfigPath
    ) -> Any:
        try:
            return converter(value)
        except ConfigMigrationError:
            raise
        except Exception as exc:
            raise ConfigMigrationError(
                f"value conversion failed for {_format_path(path)}: {exc}"
            ) from exc
