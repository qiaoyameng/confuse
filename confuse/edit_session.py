"""Edit sessions for a single writable YAML configuration source.

A :class:`EditSession` projects the edits made through the session onto
the configuration's layered sources. Edits are staged in memory and are
only persisted when :meth:`EditSession.save` is called. Saving rewrites
just the targeted YAML file with the values that the user explicitly
set or deleted at that layer -- defaults and environment variables are
never copied into the file.

Layer semantics follow Confuse's usual rules: sources earlier in the
view's source list have higher priority. In particular, deleting a
value from the writable layer removes it from that layer only, which
may reveal a value from a lower-priority layer; it never deletes the
path from every source.
"""

from __future__ import annotations

__all__ = [
    "DELETE",
    "EditOutcome",
    "EditSession",
    "MISSING",
    "ResolvedValue",
    "SaveReport",
    "SavedPath",
    "SourceInfo",
    "edit_session",
]

import copy
import os
import tempfile
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

import yaml

from . import yaml_util
from .exceptions import (
    ConfigReadError,
    ConfigTypeError,
    EditConflictError,
    EditWriteError,
    NotFoundError,
)
from .sources import ConfigSource, EnvSource, YamlSource

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

    from .core import ConfigView, RootView


class _Sentinel(Enum):
    """Internal singletons for missing values and explicit deletes."""

    MISSING = "missing"
    DELETE = "delete"

    def __repr__(self) -> str:
        return f"<{self.name}>"


#: Indicates that a path has no value in any source.
MISSING = _Sentinel.MISSING

#: Operation marker for explicitly deleting a path at the writable layer.
DELETE = _Sentinel.DELETE


def _normalize_path(path: str | Sequence[str]) -> tuple[str, ...]:
    """Turn a dotted string or a sequence of keys into a key tuple."""
    if isinstance(path, str):
        keys = tuple(path.split("."))
    else:
        keys = tuple(path)
    for key in keys:
        if not isinstance(key, str):
            raise TypeError(
                "edit sessions only support string key paths, got "
                f"{key!r} of type {type(key).__name__}"
            )
        if key == "":
            raise ValueError("configuration paths may not contain empty keys")
    if not keys:
        raise ValueError("a non-empty configuration path is required")
    return keys


def _format_path(keys: Iterable[str]) -> str:
    return ".".join(keys)


def _source_label(source: ConfigSource) -> str:
    """Human-readable name of a source, used in layer messages."""
    if isinstance(source, EnvSource):
        return f"environment variables ({source.prefix}…)"
    if isinstance(source, YamlSource):
        if source.default:
            return f"default file ({source.filename})"
        return f"YAML file ({source.filename})"
    if source.default:
        return "defaults"
    return "in-memory source"


@dataclass(frozen=True)
class SourceInfo:
    """Metadata about one configuration layer."""

    index: int
    label: str
    filename: str | None
    default: bool
    kind: str


@dataclass(frozen=True)
class ResolvedValue:
    """The effective value of a path and where it comes from.

    ``value`` is :data:`MISSING` when no source defines the path. A
    value of ``None`` means the highest-priority defining source
    explicitly sets the path to YAML null (which masks lower layers,
    the same way an environment variable of ``null`` does).
    """

    path: str
    value: Any
    source: SourceInfo | None

    @property
    def exists(self) -> bool:
        """Whether the path is defined by at least one source."""
        return self.value is not MISSING

    def get(self, fallback: Any = MISSING) -> Any:
        """Return the effective value, or *fallback* when undefined."""
        if self.value is MISSING and fallback is not MISSING:
            return fallback
        return self.value


@dataclass(frozen=True)
class EditOutcome:
    """Result of staging a single ``set`` or ``delete`` operation.

    The fields describe what the configuration looks like *after* the
    operation, including all edits previously staged in the session.
    """

    path: str
    operation: str
    effective: ResolvedValue
    layer_value: Any
    layer_changed: bool
    shadowed: bool
    revealed: ResolvedValue | _Sentinel

    @property
    def target_value(self) -> Any:
        """Alias for :attr:`layer_value`."""
        return self.layer_value


@dataclass(frozen=True)
class SavedPath:
    """What happened to one path in a :meth:`EditSession.save` call."""

    path: str
    operation: str
    status: str
    effective: ResolvedValue
    shadowed: bool


@dataclass(frozen=True)
class SaveReport:
    """Summary of a save operation.

    ``changed`` is False when the file already had exactly the staged
    content; in that case nothing is written.
    """

    filename: str
    changed: bool
    paths: tuple[SavedPath, ...]


class EditSession:
    """A transactional editing session for one writable YAML source.

    Edits staged through :meth:`set` and :meth:`delete` are visible
    immediately through :meth:`effective` and :meth:`layer_value`, but
    the underlying configuration object is left untouched until
    :meth:`save`. The target file is rewritten to contain its original
    content modified by exactly the staged paths: values from default
    or environment layers are never serialised into it.

    :param view: A configuration view (typically a
        :class:`~confuse.Configuration`). The session uses the view's
        current source stack, ordered highest to lowest priority.
    :param target: The :class:`~confuse.YamlSource` to edit, its
        filename, or a path key that selects a source. When ``None``
        (the default), the highest-priority non-default YAML source is
        used, which for a standard Confuse configuration is the user
        configuration file.

    Saving checks that the target file has not changed since the
    session began; a concurrent modification raises
    :class:`~confuse.exceptions.EditConflictError` and leaves the file
    untouched. A write failure raises
    :class:`~confuse.exceptions.EditWriteError` after restoring the
    original file, so no partial output is ever left behind.
    """

    def __init__(
        self,
        view: ConfigView,
        target: YamlSource | str | os.PathLike[str] | None = None,
    ) -> None:
        self.root: RootView = view.root()
        self._edits: OrderedDict[tuple[str, ...], Any] = OrderedDict()

        self.target_index, self.target = self._select_target(target)
        if self.target.filename is None:
            raise ValueError("the edit target must be a file-backed YamlSource")
        self.filename: str = self.target.filename

        # Snapshot the on-disk bytes (or absence) of the target so we
        # can detect external modifications at save time.
        self._baseline_exists, self._baseline_bytes = self._read_file()

    # -- construction helpers ------------------------------------------------

    def _select_target(
        self, target: YamlSource | str | os.PathLike[str] | None
    ) -> tuple[int, YamlSource]:
        """Locate the target source within the view's source stack."""
        if isinstance(target, YamlSource):
            for index, source in enumerate(self.root.sources):
                if source is target:
                    return index, target
            raise ValueError("target YamlSource is not part of this configuration")

        if target is not None:
            wanted = os.path.abspath(os.fspath(target))
            for index, source in enumerate(self.root.sources):
                if (
                    isinstance(source, YamlSource)
                    and source.filename == wanted
                ):
                    return index, source
            raise ValueError(f"no YAML source for {wanted} is loaded")

        # Default: the highest-priority non-default YAML source.
        for index, source in enumerate(self.root.sources):
            if isinstance(source, YamlSource) and not source.default:
                return index, source
        raise ValueError(
            "configuration has no writable YAML source; "
            "pass an explicit target"
        )

    def _read_file(self) -> tuple[bool, bytes | None]:
        """Return (exists, contents) for the target file."""
        try:
            with open(self.filename, "rb") as handle:
                return True, handle.read()
        except FileNotFoundError:
            return False, None
        except OSError as exc:
            raise ConfigReadError(self.filename, exc)

    # -- introspection --------------------------------------------------------

    @property
    def edited_paths(self) -> tuple[str, ...]:
        """Paths with staged (not yet saved) edits, in staging order."""
        return tuple(_format_path(keys) for keys in self._edits)

    def is_deleted(self, path: str | Sequence[str]) -> bool:
        """Whether *path* is staged for deletion at this layer."""
        return self._edits.get(_normalize_path(path), None) is DELETE

    def sources(self) -> tuple[SourceInfo, ...]:
        """Describe the layers visible to the session, high priority first."""
        infos = []
        for index, source in enumerate(self.root.sources):
            kind = (
                "env"
                if isinstance(source, EnvSource)
                else "yaml"
                if isinstance(source, YamlSource)
                else "memory"
            )
            infos.append(
                SourceInfo(
                    index=index,
                    label=_source_label(source),
                    filename=source.filename,
                    default=source.default,
                    kind=kind,
                )
            )
        return tuple(infos)

    def explain_shadow(self, path: str | Sequence[str]) -> str | None:
        """Return a human-readable warning when *path* is masked.

        The check uses the effective configuration including all staged
        edits. ``None`` is returned when the writable layer controls the
        path.
        """
        keys = _normalize_path(path)
        projected = self._projected_target()
        target_value = self._lookup(projected, keys)
        if target_value is not MISSING:
            resolved = self._resolve(keys, self._projected_sources(projected))
            if (
                resolved.source is None
                or resolved.source.index < self.target_index
            ):
                winner = (
                    resolved.source.label if resolved.source else "no source"
                )
                return (
                    f"{_format_path(keys)} is shadowed by {winner}; "
                    "the value written to this layer will not take effect"
                )
        return None

    # -- projection of staged edits ------------------------------------------

    def _projected_target(self) -> dict[str, Any]:
        """The target layer's data after applying all staged edits."""
        projected = copy.deepcopy(dict(self.target))
        for keys, operation in self._edits.items():
            if operation is DELETE:
                self._delete_path(projected, keys)
            else:
                self._assign_path(projected, keys, copy.deepcopy(operation))
        return projected

    def _projected_sources(
        self, projected_target: dict[str, Any] | None = None
    ) -> list[ConfigSource]:
        """The source stack with the target replaced by its projection."""
        if projected_target is None:
            projected_target = self._projected_target()

        projected_source = ConfigSource(
            projected_target,
            filename=self.filename,
            default=False,
            base_for_paths=self.target.base_for_paths,
        )
        return [
            projected_source if index == self.target_index else source
            for index, source in enumerate(self.root.sources)
        ]

    @staticmethod
    def _lookup(
        data: dict[str, Any], keys: Sequence[str]
    ) -> Any:
        """Fetch a path from a plain mapping, or return MISSING."""
        node: Any = data
        for key in keys:
            if not isinstance(node, dict):
                raise ConfigTypeError(
                    f"cannot descend into {key!r}: value is not a mapping"
                )
            if key not in node:
                return MISSING
            node = node[key]
        return node

    @staticmethod
    def _assign_path(
        data: dict[str, Any], keys: Sequence[str], value: Any
    ) -> None:
        """Set *keys* inside *data*, creating intermediate mappings."""
        node = data
        for key in keys[:-1]:
            if key in node:
                if not isinstance(node[key], dict):
                    raise ConfigTypeError(
                        f"cannot set path below {key!r}: value is not a mapping"
                    )
            else:
                node[key] = OrderedDict()
            node = node[key]
        node[keys[-1]] = value

    @staticmethod
    def _delete_path(data: dict[str, Any], keys: Sequence[str]) -> None:
        """Remove *keys* and prune mappings emptied by the removal."""
        trail: list[tuple[dict[str, Any], str]] = []
        node = data
        for key in keys[:-1]:
            if not isinstance(node, dict) or key not in node:
                return
            trail.append((node, key))
            node = node[key]
        if not isinstance(node, dict) or keys[-1] not in node:
            return
        del node[keys[-1]]

        for parent, key in reversed(trail):
            if isinstance(parent[key], dict) and not parent[key]:
                del parent[key]
            else:
                break

    # -- resolution -----------------------------------------------------------

    def _resolve(
        self,
        keys: Sequence[str],
        sources: Sequence[ConfigSource] | None = None,
    ) -> ResolvedValue:
        """Resolve a path against an explicit or live source stack."""
        if sources is None:
            projected = self._projected_target()
            sources = self._projected_sources(projected)

        node: Any = {}
        winning_info: SourceInfo | None = None
        found = False
        for index, source in enumerate(sources):
            node = self._lookup(dict(source), keys)
            if node is not MISSING:
                found = True
                winning_info = self._info_for(source, index)
                break

        if not found:
            return ResolvedValue(_format_path(keys), MISSING, None)
        return ResolvedValue(_format_path(keys), node, winning_info)

    def _info_for(
        self,
        source: ConfigSource,
        index: int,
    ) -> SourceInfo:
        """Map a (possibly projected) source back to its layer metadata."""
        if index == self.target_index or source is self.target:
            label = f"YAML file ({self.filename})"
            return SourceInfo(index, label, self.filename, False, "yaml")
        real_index = index
        for candidate, real_source in enumerate(self.root.sources):
            if real_source is source:
                real_index = candidate
                break
        kind = (
            "env"
            if isinstance(source, EnvSource)
            else "yaml"
            if isinstance(source, YamlSource)
            else "memory"
        )
        return SourceInfo(
            real_index,
            _source_label(source),
            source.filename,
            source.default,
            kind,
        )

    def effective(self, path: str | Sequence[str]) -> ResolvedValue:
        """Return the effective value of *path* with staged edits applied."""
        keys = _normalize_path(path)
        return self._resolve(keys)

    def layer_value(self, path: str | Sequence[str]) -> Any:
        """Return *path* as it would be stored in the target file."""
        keys = _normalize_path(path)
        return self._lookup(self._projected_target(), keys)

    # -- staging edits --------------------------------------------------------

    def set(self, path: str | Sequence[str], value: Any) -> EditOutcome:
        """Stage setting *path* to *value* in the target file.

        The edit is reflected immediately by :meth:`effective` and
        :meth:`layer_value`, but nothing is written until :meth:`save`.
        Passing ``None`` writes an explicit YAML null (which masks lower
        layers); use :meth:`delete` to remove the path from this layer
        and reveal the value underneath.
        """
        keys = _normalize_path(path)
        self._edits[keys] = value
        return self._outcome(keys, "set")

    def delete(self, path: str | Sequence[str]) -> EditOutcome:
        """Stage deletion of *path* from the target file only.

        Lower-priority sources are not touched. Raises
        :class:`~confuse.exceptions.NotFoundError` when the path is
        absent from the target layer given the edits staged so far.
        """
        keys = _normalize_path(path)
        projected = self._projected_target()
        current = self._lookup(projected, keys)
        if current is MISSING:
            raise NotFoundError(
                f"{_format_path(keys)} is not present in {self.filename}"
            )

        # Resolve the value that the path would take after removal,
        # from sources below the target layer.
        revealed = self._revealed_after_delete(keys)

        self._edits[keys] = DELETE
        return self._outcome(keys, "delete", revealed=revealed)

    def _revealed_after_delete(self, keys: Sequence[str]) -> Any:
        """Effective value at *keys* assuming the target path is gone."""
        sources: list[ConfigSource] = []
        for index, source in enumerate(self.root.sources):
            if index == self.target_index:
                # Target layer without *keys*. Build it from the
                # projection of prior edits, then apply this delete.
                without = self._projected_target()
                self._delete_path(without, keys)
                sources.append(
                    ConfigSource(
                        without,
                        filename=self.filename,
                        default=False,
                        base_for_paths=self.target.base_for_paths,
                    )
                )
            else:
                sources.append(source)
        return self._resolve(keys, sources)

    def _outcome(
        self,
        keys: Sequence[str],
        operation: str,
        revealed: ResolvedValue | None = None,
    ) -> EditOutcome:
        """Build the outcome for a staged operation."""
        projected = self._projected_target()
        sources = self._projected_sources(projected)
        effective = self._resolve(keys, sources)

        layer_value = self._lookup(projected, keys)
        original = self._lookup(dict(self.target), keys)
        layer_changed = not self._equal(layer_value, original)

        if operation == "delete" and revealed is not None:
            # After a deletion the effective value comes from a lower
            # layer (a "reveal") or no layer at all; a higher-priority
            # source still controlling the path is what counts as
            # shadowing here.
            shadowed = (
                revealed.source is not None
                and revealed.source.index < self.target_index
            )
        else:
            shadowed = (
                effective.source is not None
                and effective.source.index < self.target_index
            )

        return EditOutcome(
            path=_format_path(keys),
            operation=operation,
            effective=effective,
            layer_value=layer_value,
            layer_changed=layer_changed,
            shadowed=shadowed,
            revealed=revealed if revealed is not None else MISSING,
        )

    @staticmethod
    def _equal(left: Any, right: Any) -> bool:
        """Structural equality that treats MISSING like MISSING."""
        if left is MISSING or right is MISSING:
            return left is right
        return left == right

    # -- saving ---------------------------------------------------------------

    def preview_yaml(self) -> str:
        """Render the target file content as it would be saved."""
        return self._dump(self._projected_target())

    @staticmethod
    def _dump(data: dict[str, Any]) -> str:
        if not data:
            return ""
        return yaml.dump(
            data,
            Dumper=yaml_util.Dumper,
            default_flow_style=None,
            indent=4,
            width=1000,
            allow_unicode=True,
        )

    def save(self) -> SaveReport:
        """Persist all staged edits to the target YAML file.

        Only the explicitly edited paths are written; the projected
        target layer contains no data from default or environment
        sources. Raises :class:`EditConflictError` when the file
        changed since the session opened and :class:`EditWriteError`
        when the atomic write fails (the original file is preserved).
        """
        self._check_conflict()

        projected = self._projected_target()
        content = self._dump(projected)
        content_bytes = content.encode("utf-8")
        changed = (
            not self._baseline_exists
            or content_bytes != self._baseline_bytes
        )

        saved_paths: list[SavedPath] = []
        sources = self._projected_sources(projected)
        for keys, operation in list(self._edits.items()):
            effective = self._resolve(keys, sources)
            projected_value = self._lookup(projected, keys)
            original = self._lookup(dict(self.target), keys)
            shadowed = (
                effective.source is not None
                and effective.source.index < self.target_index
            )

            if operation is DELETE:
                if original is MISSING:
                    status = "noop"
                elif shadowed:
                    status = "deleted-still-shadowed"
                elif effective.exists:
                    status = "deleted-revealed"
                else:
                    status = "deleted-unset"
            else:
                if self._equal(projected_value, original):
                    status = "noop"
                elif shadowed:
                    status = "set-shadowed"
                else:
                    status = "set"

            saved_paths.append(
                SavedPath(
                    path=_format_path(keys),
                    operation="delete" if operation is DELETE else "set",
                    status=status,
                    effective=effective,
                    shadowed=shadowed,
                )
            )

        if changed:
            self._atomic_write(content_bytes)

        # Refresh the live YAML source from disk and adopt the new
        # baseline (even when nothing needed writing) so subsequent
        # queries from this session reflect the persisted state. Clear
        # first: YamlSource.load() merges and would otherwise keep keys
        # that the save removed from the file.
        self.target.clear()
        self.target.load()
        self._baseline_exists = True
        self._baseline_bytes = content_bytes
        self._edits.clear()

        return SaveReport(
            filename=self.filename,
            changed=changed,
            paths=tuple(saved_paths),
        )

    def _check_conflict(self) -> None:
        """Raise EditConflictError if the target file changed on disk."""
        exists, contents = self._read_file()
        if exists != self._baseline_exists or contents != self._baseline_bytes:
            raise EditConflictError(
                f"{self.filename} changed on disk since the edit session "
                "began; start a new session and reapply the edits"
            )

    def _atomic_write(self, content: bytes) -> None:
        """Write *content* next to the target and atomically replace it."""
        directory = os.path.dirname(self.filename) or "."
        temporary_name = None
        try:
            fd, temporary_name = tempfile.mkstemp(
                dir=directory, prefix=".config-", suffix=".tmp"
            )
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.filename)
            temporary_name = None
        except OSError as exc:
            raise EditWriteError(self.filename, exc)
        finally:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass

    # -- lifecycle ------------------------------------------------------------

    def close(self) -> None:
        """Discard all staged edits without writing.

        The session is unusable afterwards.
        """
        self._edits.clear()

    def __enter__(self) -> EditSession:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        # Edits are never implicitly saved: exiting the context discards
        # staged changes. Call save() explicitly to persist them.
        self.close()


def edit_session(
    view: ConfigView,
    target: YamlSource | str | Path | None = None,
) -> EditSession:
    """Open an :class:`EditSession` for one writable YAML source.

    With no *target*, the session edits the highest-priority non-default
    YAML source in *view* -- the user configuration file for a standard
    :class:`~confuse.Configuration`.
    """
    return EditSession(view, target)
