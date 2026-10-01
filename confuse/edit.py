"""Editing sessions that persist only user-modified paths.

Unlike :meth:`confuse.Configuration.dump`, which serializes the whole
merged view (and can therefore write default values and environment
overrides into the user's file), an :class:`EditSession` targets a
single writable YAML source. Changes are staged in memory and, when the
session is saved, only the explicitly modified paths are written back
into that one file.

Deleting a path removes it from the target source only, so a value
from a lower-priority source (such as the package defaults) can show
through. The session also reports whether a change is *shadowed* by a
higher-priority source (such as an environment-variable overlay), in
which case the on-disk value changes but it has no effect on the
running configuration.
"""

from __future__ import annotations

__all__ = [
    "EditSession",
    "PathStatus",
    "SaveResult",
]

import copy
import os
import tempfile
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, NamedTuple, Union

import yaml

from . import yaml_util
from .exceptions import (
    ConfigConflictError,
    ConfigError,
    ConfigReadError,
    ConfigTypeError,
    ConfigWriteError,
    NotFoundError,
)
from .sources import ConfigSource, EnvSource, YamlSource

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .core import RootView
    from .templates import ConfigKey

# A path identifies a node in a configuration tree. It may be given as
# a single key ("foo", 2) or as a sequence of keys (("foo", "bar")).
PathLike = Union["ConfigKey", "Sequence[ConfigKey]"]
_DISK_MISSING: Final[object] = object()


def _normalize_path(path: PathLike) -> tuple[ConfigKey, ...]:
    """Turn a PathLike argument into a tuple of keys."""
    if isinstance(path, (str, bytes, int)):
        return (path,)
    try:
        keys = tuple(path)
    except TypeError:
        raise TypeError(
            "path must be a key or a sequence of keys, "
            f"not {type(path).__name__}"
        )
    for key in keys:
        if not isinstance(key, (str, bytes, int)) or isinstance(key, bool):
            raise TypeError("path keys must be str, bytes, or int")
    if not keys:
        raise ValueError("path must not be empty")
    return keys


def _path_name(path: tuple[ConfigKey, ...]) -> str:
    """Render a path for human-readable status messages."""
    parts: list[str] = []
    for key in path:
        if parts and isinstance(key, (str, bytes)):
            parts.append(".")
        if isinstance(key, int):
            parts.append(f"[{key}]")
        elif isinstance(key, bytes):
            parts.append(key.decode("utf-8"))
        else:
            parts.append(key)
    return "".join(parts)


def _walk(
    collection: object, path: tuple[ConfigKey, ...]
) -> tuple[bool, Any]:
    """Return (present, value) for *path* inside a plain data tree.

    Intermediate traversal raises ConfigTypeError when a non-mapping
    blocks the path.
    """
    node: object = collection
    for index, key in enumerate(path):
        if isinstance(node, list):
            if not isinstance(key, int) or isinstance(key, bool):
                raise ConfigTypeError(
                    f"list index must be an int, not {type(key).__name__}"
                )
            if key < 0 or key >= len(node):
                return False, None
            node = node[key]
        elif isinstance(node, dict):
            if key not in node:
                return False, None
            node = node[key]
        else:
            prefix = _path_name(path[:index])
            where = prefix or "root"
            raise ConfigTypeError(
                f"{where} must be a collection, not {type(node).__name__}"
            )
    return True, node


def _apply_change(
    tree: dict[str, Any], path: tuple[ConfigKey, ...], value: Any
) -> None:
    """Set *path* to *value* inside *tree*, creating nested dicts."""
    node: Any = tree
    for key in path[:-1]:
        if isinstance(node, list):
            if not isinstance(key, int) or isinstance(key, bool):
                raise ConfigTypeError(
                    f"list index must be an int, not {type(key).__name__}"
                )
            if key < 0 or key >= len(node):
                raise ConfigTypeError(f"list index {key} out of range")
            child = node[key]
            if not isinstance(child, dict):
                child = {}
                node[key] = child
            node = child
        elif isinstance(node, dict):
            if key in node:
                child = node[key]
                if not isinstance(child, (dict, list)):
                    raise ConfigTypeError(
                        f"{key!r} must be a collection, "
                        f"not {type(child).__name__}"
                    )
                node = child
            else:
                child: dict[Any, Any] = {}
                node[key] = child
                node = child
        else:
            raise ConfigTypeError(
                f"path traverses a {type(node).__name__}, not a collection"
            )
    last = path[-1]
    if isinstance(node, list):
        if not isinstance(last, int) or isinstance(last, bool):
            raise ConfigTypeError(
                f"list index must be an int, not {type(last).__name__}"
            )
        if last < 0 or last >= len(node):
            raise ConfigTypeError(f"list index {last} out of range")
        node[last] = value
    elif isinstance(node, dict):
        node[last] = value
    else:
        raise ConfigTypeError(
            f"cannot set key on {type(node).__name__}"
        )


def _remove_change(
    tree: dict[str, Any], path: tuple[ConfigKey, ...]
) -> bool:
    """Remove *path* from *tree*, pruning empty parent dicts.

    Returns whether the path was present.
    """
    parents: list[tuple[Any, ConfigKey]] = []
    node: object = tree
    for key in path[:-1]:
        if isinstance(node, list):
            if not isinstance(key, int) or isinstance(key, bool):
                raise ConfigTypeError(
                    f"list index must be an int, not {type(key).__name__}"
                )
            if key < 0 or key >= len(node):
                return False
            parents.append((node, key))
            node = node[key]
        elif isinstance(node, dict):
            if key not in node:
                return False
            parents.append((node, key))
            node = node[key]
        else:
            prefix = _path_name(path[: len(parents)])
            where = prefix or "root"
            raise ConfigTypeError(
                f"{where} must be a collection, not {type(node).__name__}"
            )

    last = path[-1]
    if isinstance(node, list):
        if not isinstance(last, int) or isinstance(last, bool):
            raise ConfigTypeError(
                f"list index must be an int, not {type(last).__name__}"
            )
        if last < 0 or last >= len(node):
            return False
        del node[last]
        present = True
    elif isinstance(node, dict):
        if last not in node:
            return False
        del node[last]
        present = True
    else:
        prefix = _path_name(path[:-1])
        where = prefix or "root"
        raise ConfigTypeError(
            f"{where} must be a collection, not {type(node).__name__}"
        )

    # Prune empty mappings left behind by the removal. Lists are kept
    # intact because indices are positional.
    for index in range(len(parents) - 1, -1, -1):
        parent, key = parents[index]
        child = parent[key]
        if isinstance(child, dict) and not child:
            del parent[key]
        else:
            break
    return present


class _Op(NamedTuple):
    set: bool  # True for set, False for delete
    value: Any = None


@dataclass(frozen=True)
class PathStatus:
    """The effective-state report for a single path.

    ``present`` distinguishes a genuinely missing path from one whose
    effective value is ``None``.
    """

    path: tuple[ConfigKey, ...]
    present: bool
    value: Any
    source: ConfigSource | None
    shadowed: bool
    deleted: bool
    staged: bool
    written_value: Any = None

    @property
    def name(self) -> str:
        """Human-readable rendering of the path."""
        return _path_name(self.path)

    def describe(self) -> str:
        """One-line explanation of the pending change and its effect."""
        name = self.name
        if not self.staged:
            if self.present:
                return f"{name} is unchanged; effective value: {self.value!r}"
            return f"{name} is not set in any source"

        if self.deleted:
            if self.shadowed:
                winner = _source_name(self.source)
                return (
                    f"{name} removed from this source, but a higher-priority "
                    f"source ({winner}) still provides {self.value!r}"
                )
            if self.present:
                return (
                    f"{name} removed here; the lower-priority value "
                    f"{self.value!r} is now effective"
                )
            return f"{name} removed; no lower-priority value remains"

        if self.shadowed:
            winner = _source_name(self.source)
            return (
                f"{name} will be written as {self.written_value!r}, but a "
                f"higher-priority source ({winner}) keeps {self.value!r} "
                f"in effect"
            )
        return f"{name} will take effect as {self.value!r}"


@dataclass(frozen=True)
class SaveResult:
    """Outcome of :meth:`EditSession.save`.

    ``changed`` lists every path persisted to the target file. Paths
    whose effective value is masked by a higher-priority source are
    collected in ``shadowed``; deleted paths that reveal a
    lower-priority value are collected in ``revealed``.
    """

    filename: str
    changed: list[tuple[ConfigKey, ...]] = field(default_factory=list)
    shadowed: list[tuple[ConfigKey, ...]] = field(default_factory=list)
    revealed: list[tuple[ConfigKey, ...]] = field(default_factory=list)
    statuses: list[PathStatus] = field(default_factory=list)


def _source_name(source: ConfigSource | None) -> str:
    """Short label for a source in status messages."""
    if source is None:
        return "no source"
    if isinstance(source, EnvSource):
        return f"environment variables ({source.prefix})"
    if source.filename:
        return os.path.basename(source.filename)
    return "in-memory source"


class EditSession:
    """Stage and persist edits to a single writable YAML source.

    A session is bound to one :class:`~confuse.sources.YamlSource` of a
    configuration. Changes made through :meth:`set` and :meth:`delete`
    are held in memory until :meth:`save`, which rewrites *only* that
    one file with just the paths the user touched merged over its
    current contents. Higher-priority overlays (environment variables)
    and lower-priority defaults are never written into the file.

    The file's contents are snapshotted when the session starts. If the
    file changes on disk before :meth:`save`, a
    :class:`~confuse.exceptions.ConfigConflictError` is raised and
    nothing is written. Writes are atomic, so a failure while writing
    leaves the original file in place.
    """

    def __init__(
        self,
        config: RootView,
        *,
        source: YamlSource | None = None,
        filename: str | os.PathLike[str] | None = None,
    ) -> None:
        """Start an editing session.

        :param config: The configuration whose sources should be edited.
        :param source: The specific ``YamlSource`` to write.
        :param filename: Select the writable source by file path.
        :raises confuse.ConfigError: if no suitable writable YAML source
            can be found or the given source is not part of *config*.
        """
        if not hasattr(config, "sources") or not hasattr(config, "resolve"):
            raise TypeError("config must be a Confuse configuration view")

        # Materialize lazy configurations before we inspect sources.
        for _ in config.resolve():
            break

        self.config: RootView = config
        self.target = self._select_source(list(config.sources), source, filename)
        self.loader = self.target.loader
        self.changes: OrderedDict[tuple[ConfigKey, ...], _Op] = OrderedDict()
        self._snapshot: bytes | object = self._read_disk()

    # -- construction helpers -------------------------------------------

    @staticmethod
    def _select_source(
        sources: list[ConfigSource],
        source: YamlSource | None,
        filename: str | os.PathLike[str] | None,
    ) -> YamlSource:
        if source is not None:
            if not isinstance(source, YamlSource):
                raise ConfigError(
                    "edit target must be a YamlSource, "
                    f"not {type(source).__name__}"
                )
            for candidate in sources:
                if candidate is source:
                    if not source.filename:
                        raise ConfigError(
                            "the target YamlSource has no filename"
                        )
                    return source
            raise ConfigError("the given YamlSource is not part of this config")

        if filename is not None:
            wanted = os.path.abspath(os.fspath(filename))
            for candidate in sources:
                if (
                    isinstance(candidate, YamlSource)
                    and candidate.filename == wanted
                ):
                    return candidate
            raise ConfigError(f"no YAML source for {wanted}")

        for candidate in sources:
            if (
                isinstance(candidate, YamlSource)
                and not candidate.default
                and candidate.filename
            ):
                return candidate
        raise ConfigError(
            "no writable YAML source is available; add one with "
            "set_file() or read()"
        )

    def _target_position(self) -> int:
        """Current index of the target source in the live source list.

        Sources may shift while a session is open (e.g., another
        ``set_env()`` overlay is inserted), so the position is located
        by identity on each use rather than cached.
        """
        for index, candidate in enumerate(self.config.sources):
            if candidate is self.target:
                return index
        raise ConfigError(
            "the edit target was removed from the configuration"
        )

    @property
    def filename(self) -> str:
        """Path of the YAML file this session writes to."""
        return self.target.filename  # type: ignore[return-value]

    # -- disk helpers ----------------------------------------------------

    def _read_disk(self) -> bytes | object:
        filename = self.filename
        if not os.path.isfile(filename):
            return _DISK_MISSING
        try:
            with open(filename, "rb") as fp:
                return fp.read()
        except OSError as exc:
            raise ConfigReadError(filename, exc)

    def _check_disk(self) -> None:
        """Raise if the target file changed since the session began."""
        current = self._read_disk()
        if current != self._snapshot:
            raise ConfigConflictError(self.filename)

    def _load_disk_data(self) -> dict[str, Any]:
        """Load the target file's current contents as a plain dict."""
        fresh = YamlSource(
            self.filename, optional=True, loader=self.loader
        )
        return dict(fresh)

    # -- staging ---------------------------------------------------------

    @property
    def changed_paths(self) -> list[tuple[ConfigKey, ...]]:
        """All paths with pending (unsaved) modifications."""
        return list(self.changes)

    def _drop_descendants(self, path: tuple[ConfigKey, ...]) -> None:
        for existing in list(self.changes):
            if len(existing) > len(path) and existing[: len(path)] == path:
                del self.changes[existing]

    def _staged_tree(self) -> tuple[dict[str, Any], ConfigSource, list[ConfigSource]]:
        """Build a virtual source stack with the pending edits applied."""
        data = copy.deepcopy(dict(self.target))
        for path, op in self.changes.items():
            if op.set:
                _apply_change(data, path, op.value)
            else:
                _remove_change(data, path)

        target_index = self._target_position()
        virtual = ConfigSource(
            data,
            filename=self.target.filename,
            default=self.target.default,
            base_for_paths=self.target.base_for_paths,
        )
        sources = [
            *self.config.sources[:target_index],
            virtual,
            *self.config.sources[target_index + 1 :],
        ]
        return data, virtual, sources

    def set(self, path: PathLike, value: Any) -> PathStatus:
        """Stage setting *path* to *value* in the target file.

        Editing a parent path discards pending edits nested beneath it.
        Raises `ConfigTypeError` if existing containers block the path.
        """
        keys = _normalize_path(path)
        self._drop_descendants(keys)

        # Validate the traversal against the staged tree before recording.
        data, _, _ = self._staged_tree()
        _apply_change(data, keys, value)

        self.changes[keys] = _Op(set=True, value=value)
        return self.status(keys)

    def delete(self, path: PathLike) -> PathStatus:
        """Stage removal of *path* from the target file.

        Only this source's value is removed: if a lower-priority source
        (e.g., the defaults file) defines the same path, that value
        becomes effective. The returned status reports whether the path
        is instead masked by a higher-priority source.
        """
        keys = _normalize_path(path)
        self._drop_descendants(keys)

        data, _, _ = self._staged_tree()
        _remove_change(data, keys)

        self.changes[keys] = _Op(set=False)
        return self.status(keys)

    def rollback(self) -> None:
        """Discard all pending changes without touching the file."""
        self.changes.clear()

    # -- introspection ---------------------------------------------------

    @staticmethod
    def _winner(
        sources: list[ConfigSource], path: tuple[ConfigKey, ...]
    ) -> tuple[bool, Any, ConfigSource | None]:
        for source in sources:
            present, value = _walk(dict(source), path)
            if present:
                return True, value, source
        return False, None, None

    def _compute_status(
        self,
        path: tuple[ConfigKey, ...],
        sources: list[ConfigSource],
        virtual: ConfigSource | None,
        target_index: int | None = None,
    ) -> PathStatus:
        present, value, winner = self._winner(sources, path)

        if virtual is not None and winner is virtual:
            shadowed = False
        else:
            if target_index is None:
                target_index = self._target_position()
            shadowed = (
                present
                and winner is not None
                and sources.index(winner) < target_index
            )

        op = self.changes.get(path)
        written_value = op.value if op is not None and op.set else None
        return PathStatus(
            path=path,
            present=present,
            value=value,
            source=winner,
            shadowed=shadowed,
            deleted=op is not None and not op.set,
            staged=op is not None,
            written_value=written_value,
        )

    def status(
        self, path: PathLike, *, apply_changes: bool = True
    ) -> PathStatus:
        """Report the effective state of *path*.

        With ``apply_changes`` (the default), pending edits are treated
        as if already saved. Set it to false to inspect the current,
        unmodified running configuration.
        """
        keys = _normalize_path(path)
        if apply_changes and self.changes:
            _, virtual, sources = self._staged_tree()
            return self._compute_status(keys, sources, virtual)

        sources = list(self.config.sources)
        return self._compute_status(keys, sources, None)

    def effective(self, path: PathLike) -> Any:
        """The value currently in effect (before pending edits).

        Raises `NotFoundError` if no source defines the path.
        """
        keys = _normalize_path(path)
        present, value, _ = self._winner(list(self.config.sources), keys)
        if not present:
            raise NotFoundError(f"{_path_name(keys)} not found")
        return value

    def preview(self, path: PathLike) -> Any:
        """The value that will be in effect after pending edits.

        Raises `NotFoundError` if the path would remain undefined.
        """
        keys = _normalize_path(path)
        if self.changes:
            _, _, sources = self._staged_tree()
        else:
            sources = list(self.config.sources)
        present, value, _ = self._winner(sources, keys)
        if not present:
            raise NotFoundError(f"{_path_name(keys)} not found")
        return value

    def is_shadowed(self, path: PathLike) -> bool:
        """Whether a higher-priority source masks *path* after edits."""
        return self.status(path).shadowed

    # -- persistence -----------------------------------------------------

    def refresh(self) -> None:
        """Re-read the target source and adopt its current snapshot.

        Raises `ConfigConflictError` if the file was modified on disk.
        """
        self._check_disk()
        self.target.load()
        self._snapshot = self._read_disk()

    def save(self) -> SaveResult:
        """Persist all pending paths into the target YAML file.

        Only explicitly modified paths are written; defaults and
        environment overlays are not copied into the file. Deletions
        take effect solely in this source, letting lower-priority
        values show through.

        :raises ConfigConflictError: the file changed on disk since the
            session began.
        :raises ConfigWriteError: the file could not be written; the
            original file is preserved.
        """
        if not self.changes:
            return SaveResult(filename=self.filename)

        # Bail out if the file was touched externally.
        self._check_disk()

        # Merge the pending paths over the file's latest on-disk state.
        data = self._load_disk_data()
        touched: list[tuple[ConfigKey, ...]] = []
        for path, op in self.changes.items():
            if op.set:
                _apply_change(data, path, op.value)
                touched.append(path)
            elif _remove_change(data, path):
                touched.append(path)

        # Compute the effective before/after status for every path.
        target_index = self._target_position()
        virtual = ConfigSource(
            data,
            filename=self.target.filename,
            default=self.target.default,
            base_for_paths=self.target.base_for_paths,
        )
        sources = [
            *self.config.sources[:target_index],
            virtual,
            *self.config.sources[target_index + 1 :],
        ]
        statuses = [
            self._compute_status(path, sources, virtual, target_index)
            for path in self.changes
        ]
        shadowed = [st.path for st in statuses if st.shadowed]
        revealed = [
            st.path
            for st in statuses
            if st.deleted and st.present and not st.shadowed
        ]

        yaml_text = yaml.dump(
            data,
            Dumper=yaml_util.Dumper,
            default_flow_style=None,
            indent=4,
            width=1000,
        )
        self._atomic_write(yaml_text.encode("utf-8"))

        # Reflect the new contents in the running configuration.
        self.target.load()
        self._snapshot = self._read_disk()
        result = SaveResult(
            filename=self.filename,
            changed=touched,
            shadowed=shadowed,
            revealed=revealed,
            statuses=statuses,
        )
        self.changes.clear()
        return result

    def _atomic_write(self, payload: bytes) -> None:
        filename = self.filename
        directory = os.path.dirname(filename) or "."
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=directory, prefix=".confuse-edit-", suffix=".tmp"
            )
        except OSError as exc:
            raise ConfigWriteError(filename, exc)

        try:
            with os.fdopen(fd, "wb") as fp:
                fp.write(payload)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp_name, filename)
        except OSError as exc:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise ConfigWriteError(filename, exc)

    # -- context manager -------------------------------------------------

    def __enter__(self) -> EditSession:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        if exc_type is None:
            self.save()
        else:
            self.changes.clear()
        return False
