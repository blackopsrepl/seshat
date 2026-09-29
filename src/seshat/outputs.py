"""Capture-target geometry for the output being recorded.

seshat records a screen, not a desktop session: the only session fact it needs
is which output (and which rect) to hand to the capture backend. Keeping that
probe here, rather than importing a desktop-control module, is what lets the
recorder live on its own. Sway outputs are passed through verbatim; Hyprland
monitors are projected onto the same ``name``/``rect`` shape the recorder and
the diagnostics consume.
"""

from __future__ import annotations

from typing import Any

from . import core, session


def _sway_outputs() -> list[dict[str, Any]]:
    outputs = core.read_json_command(["swaymsg", "-t", "get_outputs"])
    return [output for output in outputs if output.get("active")]


def _hyprland_outputs() -> list[dict[str, Any]]:
    monitors = core.read_json_command(["hyprctl", "-j", "monitors"])
    return [
        _monitor_output(monitor) for monitor in monitors if isinstance(monitor, dict) and not monitor.get("disabled")
    ]


def _monitor_output(monitor: dict[str, Any]) -> dict[str, Any]:
    """Project one ``hyprctl`` monitor onto the output shape the recorder consumes.

    Hyprland reports layout geometry — position and scaled size — which is the
    same logical space Sway's rect and wf-recorder's region live in, and it has
    no ``active`` flag: a monitor is active unless it is ``disabled``.
    ``current_mode`` is synthesized from the layout size and refresh rate (in
    mHz, matching Sway) so diagnostics can compare outputs across compositors.
    """
    name = monitor.get("name")
    if not isinstance(name, str) or not name:
        raise core.ToolError("hyprctl reported a monitor without a name")
    rect = _monitor_rect(monitor)
    return {
        "name": name,
        "description": monitor.get("description"),
        "active": True,
        "focused": bool(monitor.get("focused")),
        "dpms": monitor.get("dpmsStatus"),
        "mirror_of": monitor.get("mirrorOf"),
        "transform": monitor.get("transform"),
        "scale": monitor.get("scale"),
        "rect": rect,
        "current_mode": _monitor_mode(monitor, rect),
        "available_modes": monitor.get("availableModes"),
    }


def _monitor_rect(monitor: dict[str, Any]) -> dict[str, int]:
    try:
        return {
            "x": int(monitor["x"]),
            "y": int(monitor["y"]),
            "width": int(monitor["width"]),
            "height": int(monitor["height"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise core.ToolError(f"monitor {monitor.get('name')!r} has invalid geometry") from exc


def _monitor_mode(monitor: dict[str, Any], rect: dict[str, int]) -> dict[str, int] | None:
    try:
        return {
            "width": rect["width"],
            "height": rect["height"],
            "refresh": int(float(monitor["refreshRate"]) * 1000),
        }
    except (KeyError, TypeError, ValueError):
        return None


_OUTPUT_ADAPTORS = {"sway": _sway_outputs, "hyprland": _hyprland_outputs}


def get_outputs() -> list[dict[str, Any]]:
    """Active outputs of the detected compositor, each with ``name`` and ``rect``."""
    compositor = session.require_session()
    return _OUTPUT_ADAPTORS[compositor]()
