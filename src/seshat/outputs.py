"""Capture-target geometry for the Sway output being recorded.

seshat records a screen, not a desktop session: the only session fact it needs
is which output (and which rect) to hand to the capture backend. Keeping that
probe here, rather than importing a desktop-control module, is what lets the
recorder live on its own.
"""

from __future__ import annotations

from typing import Any

from . import core


def get_outputs() -> list[dict[str, Any]]:
    """Active Sway outputs, each with ``name`` and ``rect``."""
    core.require_session()
    outputs = core.read_json_command(["swaymsg", "-t", "get_outputs"])
    return [output for output in outputs if output.get("active")]
