#!/usr/bin/env python
"""Three-layer demonstration of a Confuse editing session.

Layers, highest to lowest priority:
    1. environment variables
    2. a writable user YAML file
    3. a package-style default YAML file

The script shows the effective values before and after a session that
only writes the paths the user explicitly modified.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import confuse


def show(config: confuse.Configuration, title: str) -> None:
    print(title)
    server = config["server"].flatten()
    for key in ("host", "port", "timeout"):
        if key in server:
            print(f"    server.{key} = {server[key]!r}")
    print()


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="confuse-demo-"))
    default_path = workdir / "config_default.yaml"
    user_path = workdir / "config.yaml"

    default_path.write_text(
        "server:\n"
        "    host: localhost\n"
        "    port: 8080\n"
        "    timeout: 5\n",
        encoding="utf-8",
    )
    user_path.write_text(
        "server:\n"
        "    host: myhost\n",
        encoding="utf-8",
    )

    config = confuse.Configuration("demoapp", read=False)
    # Sources are ordered highest-priority first; read() uses the same
    # order: user file then package defaults.
    config.add(confuse.YamlSource(str(user_path), optional=True))
    config.add(
        confuse.YamlSource(
            str(default_path), default=True, optional=True
        )
    )
    os.environ["DEMOAPP_SERVER__PORT"] = "9000"
    config.set_env("DEMOAPP_")

    print(f"user config file: {user_path}\n")
    show(config, "Effective values BEFORE editing:")

    with config.edit() as session:
        add_status = session.set(("server", "timeout"), 30)
        port_status = session.set(("server", "port"), 7000)
        host_status = session.delete(("server", "host"))

        print("User requests:")
        print(f"    set server.timeout -> {add_status.describe()}")
        print(f"    set server.port    -> {port_status.describe()}")
        print(f"    delete server.host -> {host_status.describe()}")
        result = session.save()

    print()
    print("Save report:")
    print(f"    written paths : {[list(p) for p in result.changed]}")
    print(f"    shadowed      : {[list(p) for p in result.shadowed]}")
    print(f"    revealed      : {[list(p) for p in result.revealed]}")
    print()
    print("Contents of the user file after save (only touched paths):")
    for line in user_path.read_text(encoding="utf-8").splitlines():
        print(f"    {line}")
    print()

    show(config, "Effective values AFTER saving:")


if __name__ == "__main__":
    main()
