"""Demonstrate an edit session against one writable YAML source.

Run directly; it builds a throwaway three-layer configuration:

1. environment variables (highest priority),
2. a writable user YAML file (the edit target),
3. a default YAML file (lowest priority).

The output shows the effective value of every path before editing,
which edits are shadowed by a higher-priority layer, how deleting a
user value reveals the default underneath, conflict detection when the
target file changes mid-session, and preservation of the original file
when the write fails.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

import confuse

PATHS = ("greeting", "retries", "theme", "nested.a", "nested.b")


def _write(path: Path, content: str) -> None:
    path.write_text(content.lstrip("\n"), encoding="utf-8")


def _build(root: Path, env: dict[str, str]) -> confuse.RootView:
    user = root / "config.yaml"
    default = root / "config_default.yaml"
    for name, value in env.items():
        os.environ[name] = value
    return confuse.RootView(
        [
            confuse.EnvSource("DEMO_"),
            confuse.YamlSource(str(user), optional=True),
            confuse.YamlSource(str(default), optional=True, default=True),
        ]
    )


def _show(session: confuse.EditSession, title: str) -> None:
    print(title)
    for dotted in PATHS:
        resolved = session.effective(dotted)
        if resolved.exists:
            assert resolved.source is not None
            origin = resolved.source.label
            print(f"  {dotted:<10} = {resolved.value!r:<12} <- {origin}")
        else:
            print(f"  {dotted:<10} = <not set>")


def main() -> None:
    workspace = Path(tempfile.mkdtemp(prefix="confuse-edit-demo-"))
    user = workspace / "config.yaml"
    default = workspace / "config_default.yaml"

    _write(
        default,
        """
        greeting: hello
        retries: 3
        theme: light
        nested:
            a: from-default
            b: from-default
        """,
    )
    _write(
        user,
        """
        greeting: hi
        nested:
            a: from-user
        """,
    )

    env = {"DEMO_GREETING": "from-environment"}
    config = _build(workspace, env)

    print("=" * 66)
    print("Layers (high -> low priority):")
    print("  1. environment variables (DEMO_*)")
    print(f"  2. user YAML file   : {user}")
    print(f"  3. default YAML file: {default}")
    print("=" * 66)

    with confuse.edit_session(config, str(user)) as session:
        _show(session, "\nEffective values BEFORE edits:")

        print("\nStaging edits ...")

        outcome = session.set("greeting", "howdy")
        print(f"  set greeting='howdy'")
        warning = session.explain_shadow("greeting")
        print(f"    effective value: {outcome.effective.value!r}")
        print(f"    -> {warning}")

        outcome = session.set("retries", 9)
        print(
            "  set retries=9 "
            f"(effective now: {outcome.effective.value!r}, "
            f"shadowed: {outcome.shadowed})"
        )

        outcome = session.delete("nested.a")
        revealed = outcome.revealed
        print(
            "  delete nested.a "
            f"-> reveals {revealed.value!r} from {revealed.source.label}"
        )

        outcome = session.set("nested.b", "from-user")
        print(f"  set nested.b='from-user' (was default, now user-owned)")

        print("\nFile contents that WILL be written (user layer only):")
        for line in session.preview_yaml().splitlines():
            print(f"    {line}")

        report = session.save()
        print(f"\nSaved ({len(report.paths)} paths, changed={report.changed}):")
        for entry in report.paths:
            print(f"    {entry.path:<10} {entry.status}")

    print("\nUser file on disk (no env/default values leaked in):")
    for line in user.read_text(encoding="utf-8").splitlines():
        print(f"    {line}")

    config = _build(workspace, env)
    with confuse.edit_session(config, str(user)) as session:
        _show(session, "\nEffective values AFTER save (same three layers):")

    # --- conflict detection -------------------------------------------------
    config = _build(workspace, env)
    with confuse.edit_session(config, str(user)) as session:
        session.set("retries", 10)
        user.write_text(user.read_text(encoding="utf-8") + "retries: 77\n")
        print("\nConcurrent modification detected mid-session:")
        try:
            session.save()
        except confuse.EditConflictError as exc:
            print(f"  EditConflictError: {exc}")
            print("  original file left in place: retries is 77 on disk")

    # --- write failure keeps the original -----------------------------------
    config = _build(workspace, env)
    before = user.read_bytes()
    os.chmod(workspace, stat.S_IRUSR | stat.S_IXUSR)
    with confuse.edit_session(config, str(user)) as session:
        session.set("theme", "dark")
        print("\nSimulating an unwritable directory:")
        try:
            session.save()
        except confuse.EditWriteError as exc:
            print(f"  EditWriteError: {exc}")
        finally:
            os.chmod(workspace, stat.S_IRWXU)
    intact = user.read_bytes() == before
    print(f"  original user file preserved unchanged: {intact}")


if __name__ == "__main__":
    main()
