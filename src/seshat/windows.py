"""Capture-target windows: resolve a titled window to a static rect at start.

The record tool drives the desktop's own facts — a window the caller names by
class or title substring becomes a capture rectangle, resolved once at
recording start against the compositor's tree. The rect is fixed for the
whole take on purpose: a narrated screencast anchors events against a
constant region, so a window that resizes or moves mid-take keeps the
geometry it had when the take began.
"""

from __future__ import annotations

import json
from typing import Any

from . import core, session


def find_window_rect(needle: Any) -> dict[str, int]:
    """The matched window's rect: {"x", "y", "width", "height"} in layout pixels.

    Matching is a case-insensitive substring over the composite of the
    window's class (or app_id) and title; the first match in the compositor's
    tree order wins, which is the stacking order the desktop itself reports.
    """
    needle_text = str(needle or "").strip()
    if not needle_text:
        raise core.ToolError("window must be a non-empty class or title substring")
    compositor_name = session.detect_compositor()
    compositor = session._COMPOSITORS_BY_NAME[compositor_name]
    if compositor_name == "sway":
        tree = core.read_json_command(["swaymsg", "-t", "get_tree"])
        rect = _walk_sway_tree(needle_text, tree)
    else:
        clients = core.read_json_command(["hyprctl", "-j", "clients"])
        rect = _walk_hyprland_clients(needle_text, clients)
    if rect is None:
        raise core.ToolError(
            f'no window matches "{needle_text}" (matched against classes '
            "and titles as the compositor reports them)"
        )
    return rect


def _sway_window_match(needle: str, node: dict[str, Any]) -> tuple[str, dict[str, int]] | None:
    rect = node.get("rect") or {}
    width = int(rect.get("width") or 0)
    height = int(rect.get("height") or 0)
    if width < 2 or height < 2:
        return None
    hay = f"{node.get('app_id') or ''} {node.get('name') or ''}".lower()
    if needle.lower() not in hay:
        return None
    return f"{node.get('app_id') or ''} {node.get('name') or ''}".strip(), {
        "x": int(rect.get("x") or 0),
        "y": int(rect.get("y") or 0),
        "width": width,
        "height": height,
    }


def _walk_sway_tree(needle: str, node: Any) -> dict[str, int] | None:
    if not isinstance(node, dict):
        return None
    match = _sway_window_match(needle, node)
    if match is not None:
        return match[1]
    for child in (node.get("nodes") or []) + (node.get("floating_nodes") or []):
        found = _walk_sway_tree(needle, child)
        if found is not None:
            return found
    return None


def _walk_hyprland_clients(needle: str, clients: Any) -> dict[str, int] | None:
    if not isinstance(clients, list):
        return None
    for client in clients:
        if not isinstance(client, dict):
            continue
        at = client.get("at") or [0, 0]
        size = client.get("size") or [0, 0]
        if not isinstance(at, list) or not isinstance(size, list) or len(at) < 2 or len(size) < 2:
            continue
        rect = {"x": int(at[0]), "y": int(at[1]), "width": int(size[0]), "height": int(size[1])}
        if rect["width"] < 2 or rect["height"] < 2:
            continue
        hay = f"{client.get('class') or ''} {client.get('title') or ''}".lower()
        if needle.lower() not in hay:
            continue
        return rect
    return None


def containing_output(rect: dict[str, int], outputs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The active output whose rect contains the window's top-left corner."""
    for output in outputs:
        box = output.get("rect") or {}
        try:
            x, y = int(box["x"]), int(box["y"])
            width, height = int(box["width"]), int(box["height"])
        except (KeyError, TypeError, ValueError):
            continue
        if x <= rect["x"] < x + width and y <= rect["y"] < y + height:
            return output
    return None


def json_of(rect: dict[str, int]) -> str:
    return json.dumps(rect, sort_keys=True)
