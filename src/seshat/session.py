"""Compositor session detection and IPC environment recovery.

seshat records a Wayland screen; the only session facts it needs are which
compositor owns the session and which output to hand to the capture backend.
Detection is evidence-based — the IPC variable each compositor exports to its
children, or its socket under ``XDG_RUNTIME_DIR`` — so a server started by an
MCP harness outside the desktop still finds the session it was launched from.
An explicit ``SESHAT_COMPOSITOR`` override breaks any ambiguity.
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import core


COMPOSITOR_ENV = "SESHAT_COMPOSITOR"


def _discover_sway_socket(runtime_dir: str) -> str | None:
    return newest_socket(f"{runtime_dir}/sway-ipc.{os.getuid()}.*.sock")


def _discover_hyprland_instance(runtime_dir: str) -> str | None:
    """The newest Hyprland instance directory name under the runtime dir."""
    candidates = [Path(path) for path in glob.glob(f"{runtime_dir}/hypr/*") if Path(path).is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime).name


@dataclass(frozen=True)
class Compositor:
    """One supported compositor's IPC surface.

    ``ipc_env`` names the endpoint the compositor exports to its children
    (``SWAYSOCK`` for Sway, ``HYPRLAND_INSTANCE_SIGNATURE`` for Hyprland);
    ``discover`` recovers that endpoint from the runtime directory when the
    variable is missing, and ``probe`` is the IPC call ``require_session``
    uses to prove the endpoint actually answers.
    """

    name: str
    label: str
    client: str
    ipc_env: str
    probe: tuple[str, ...]
    discover: Callable[[str], str | None]


SWAY = Compositor(
    name="sway",
    label="Sway",
    client="swaymsg",
    ipc_env="SWAYSOCK",
    probe=("swaymsg", "-t", "get_outputs"),
    discover=_discover_sway_socket,
)
HYPR = Compositor(
    name="hyprland",
    label="Hyprland",
    client="hyprctl",
    ipc_env="HYPRLAND_INSTANCE_SIGNATURE",
    probe=("hyprctl", "-j", "monitors"),
    discover=_discover_hyprland_instance,
)

COMPOSITORS = (SWAY, HYPR)
_COMPOSITORS_BY_NAME = {compositor.name: compositor for compositor in COMPOSITORS}


def newest_socket(pattern: str) -> str | None:
    candidates = [Path(path) for path in glob.glob(pattern) if Path(path).is_socket()]
    if not candidates:
        return None
    return str(max(candidates, key=lambda path: path.stat().st_mtime))


def ensure_session_environment() -> None:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        candidate = f"/run/user/{os.getuid()}"
        if Path(candidate).is_dir():
            runtime_dir = candidate
            os.environ["XDG_RUNTIME_DIR"] = candidate

    if runtime_dir:
        for compositor in COMPOSITORS:
            if not os.environ.get(compositor.ipc_env):
                endpoint = compositor.discover(runtime_dir)
                if endpoint:
                    os.environ[compositor.ipc_env] = endpoint

    if runtime_dir and not os.environ.get("WAYLAND_DISPLAY"):
        displays = sorted(Path(runtime_dir).glob("wayland-*"), key=lambda path: path.stat().st_mtime, reverse=True)
        for display in displays:
            if display.is_socket():
                os.environ["WAYLAND_DISPLAY"] = display.name
                break


def _has_ipc_evidence(compositor: Compositor) -> bool:
    if os.environ.get(compositor.ipc_env):
        return True
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    return bool(runtime_dir) and compositor.discover(runtime_dir) is not None


def detect_compositor() -> str:
    """The compositor this process can talk to: ``sway`` or ``hyprland``.

    With evidence for both — say, a nested session — the desktop hint in
    ``XDG_CURRENT_DESKTOP`` decides; without one, Sway keeps the historical
    default and the override decides on purpose.
    """
    override = os.environ.get(COMPOSITOR_ENV)
    if override:
        name = override.strip().lower()
        if name not in _COMPOSITORS_BY_NAME:
            raise core.ToolError(
                f"{COMPOSITOR_ENV} must be one of: " + ", ".join(_COMPOSITORS_BY_NAME)
            )
        return name

    candidates = [compositor for compositor in COMPOSITORS if _has_ipc_evidence(compositor)]
    if not candidates:
        raise core.ToolError(
            "no Sway or Hyprland session detected; missing "
            + " and ".join(compositor.ipc_env for compositor in COMPOSITORS)
            + f" (or set {COMPOSITOR_ENV}=sway|hyprland to override)"
        )
    if len(candidates) == 1:
        return candidates[0].name

    desktop_hint = " ".join(
        hint
        for hint in (os.environ.get("XDG_CURRENT_DESKTOP"), os.environ.get("XDG_SESSION_DESKTOP"))
        if hint
    ).lower()
    if "hypr" in desktop_hint:
        return HYPR.name
    return SWAY.name


def require_session() -> str:
    """Verify a usable compositor session and return its name.

    The IPC probe doubles as the gate ``outputs.get_outputs`` relies on: a
    session that cannot answer its compositor's own query cannot resolve
    output geometry either.
    """
    ensure_session_environment()
    compositor = _COMPOSITORS_BY_NAME[detect_compositor()]
    missing = [
        name
        for name in (compositor.ipc_env, "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR")
        if not os.environ.get(name)
    ]
    if missing:
        raise core.ToolError(
            f"not running inside a usable {compositor.label} session; missing "
            + ", ".join(missing)
        )
    core.require_binaries([compositor.client])
    core.read_json_command(list(compositor.probe))
    return compositor.name


def session_environment() -> dict[str, Any]:
    """The raw session variables diagnostics report, recovered or not."""
    return {
        "WAYLAND_DISPLAY": os.environ.get("WAYLAND_DISPLAY"),
        "SWAYSOCK": os.environ.get("SWAYSOCK"),
        "HYPRLAND_INSTANCE_SIGNATURE": os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"),
        "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR"),
    }
