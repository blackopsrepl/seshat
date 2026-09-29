"""Capture-target geometry for the output being recorded.

seshat records a screen, not a desktop session: the only session fact it needs
is which output (and which rect) to hand to the capture backend. Keeping that
probe here, rather than importing a desktop-control module, is what lets the
recorder live on its own. Sway outputs are passed through verbatim; Hyprland
monitors are projected onto the same ``name``/``rect`` shape the recorder and
the diagnostics consume, with the rect derived in logical layout coordinates
rather than copied.
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

    ``hyprctl`` reports ``width``/``height`` as pixel dimensions but ``x``/``y``
    as layout positions, so the rect is derived rather than copied: it has to be
    the monitor's logical box, which is the space Sway's rect and wf-recorder's
    region live in. Hyprland has no ``active`` flag either — a monitor is active
    unless it is ``disabled``. ``current_mode`` keeps the unscaled pixel
    dimensions and the refresh rate in mHz, matching Sway, so diagnostics can
    compare outputs across compositors.
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
        "current_mode": _monitor_mode(monitor),
        "available_modes": monitor.get("availableModes"),
    }


def _rotated(transform: Any) -> bool:
    """Whether a wl_output transform swaps the output's axes (90°/270°)."""
    try:
        return int(transform) % 2 == 1
    except (TypeError, ValueError):
        return False


def _monitor_rect(monitor: dict[str, Any]) -> dict[str, int]:
    """The monitor's box in logical layout coordinates.

    Hyprland's monitor size is its pixel size divided by the monitor scale, with
    the axes swapped for a rotated transform — the geometry it advertises through
    xdg-output, which is the geometry ``wf-recorder`` measures a ``-g`` region
    against. ``x``/``y`` are already layout positions and pass through unchanged.
    """
    name = monitor.get("name")
    try:
        x = int(monitor["x"])
        y = int(monitor["y"])
        width = int(monitor["width"])
        height = int(monitor["height"])
        scale = float(monitor.get("scale", 1))
    except (KeyError, TypeError, ValueError) as exc:
        raise core.ToolError(f"monitor {name!r} has invalid geometry") from exc
    if scale <= 0:
        raise core.ToolError(f"monitor {name!r} has an invalid scale {monitor.get('scale')!r}")
    if _rotated(monitor.get("transform")):
        width, height = height, width
    return {"x": x, "y": y, "width": round(width / scale), "height": round(height / scale)}


def _monitor_mode(monitor: dict[str, Any]) -> dict[str, int] | None:
    """The monitor's mode as Sway reports one: the pixel resolution and mHz refresh."""
    try:
        return {
            "width": int(monitor["width"]),
            "height": int(monitor["height"]),
            "refresh": int(float(monitor["refreshRate"]) * 1000),
        }
    except (KeyError, TypeError, ValueError):
        return None


_OUTPUT_ADAPTORS = {"sway": _sway_outputs, "hyprland": _hyprland_outputs}


def get_outputs() -> list[dict[str, Any]]:
    """Active outputs of the detected compositor, each with ``name`` and ``rect``."""
    compositor = session.require_session()
    return _OUTPUT_ADAPTORS[compositor]()
