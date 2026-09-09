"""Runs one action, and renders a capture as either an image or a line of text."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cufast import _native
from cufast.actions.limits import (
    CAPTURE_WAS_FREE,
    SCREEN_UNCHANGED,
    _sleep_interruptibly,
)
from cufast.actions.names import _CLICK_BUTTONS, canonical
from cufast.actions.validate import _coordinate, _duration, _text, validate
from cufast.session import ActionError, Screenshot, Session


@dataclass
class ActionResult:
    label: str
    text: str | None = None
    image: Screenshot | None = None
    is_error: bool = False


def capture_result(session: Session, label: str, shot: Screenshot,
                   note: str = "") -> ActionResult:
    """One capture, as either an image or the news that it is the same image.

    A desktop is static for most of the time an agent spends looking at it, and an
    identical frame costs a full image of context to say nothing. Suppressing it is
    worth about 777 visual tokens a call, and it is also the only thing that teaches
    the model that looking again was not worth a round trip.
    """
    # Which screen this is, whenever there is more than one. Without it the only way
    # to learn another display exists is to think to ask, and a model hunting for a
    # window that is simply on the other monitor has no reason to suspect that -- it
    # searches the screen it can see, does not find the thing, and concludes the
    # thing is not there. One short marker on every image is the cheapest way to make
    # the second display a fact it already has rather than one it has to go looking
    # for. Silent on a single-monitor machine, where it would be noise.
    count = session.display_count
    if count > 1:
        label = f"{label} (display {session.screen.index} of {count} attached)"

    if not session.mark_delivered(shot):
        return ActionResult(label, text=SCREEN_UNCHANGED + note)
    return ActionResult(label, image=shot, text=note or None)


# Actions after which the UI needs a moment before the next action is meaningful.
# mouse_move is included because hovering is its whole purpose: without a settle the
# screenshot that follows is the pre-hover frame, and the model concludes the tooltip


def execute(session: Session, name: str, params: dict[str, Any]) -> ActionResult:
    """Runs one action. Raises ActionError with a message meant for the model.

    Every parameter is validated before the first native call, so an action can never
    half-apply -- moving the cursor and then rejecting its own arguments.
    """
    validate(name, params)
    name = canonical(name)

    if name == "screenshot":
        # Free either way; say so, because an explicit screenshot is a whole
        # round trip spent asking for something that arrives on its own.
        return capture_result(session, name, session.screenshot(), CAPTURE_WAS_FREE)

    if name == "zoom":
        shot = session.zoom([float(v) for v in params["region"]])
        _, _, rw, rh = shot.region
        # The magnification against the full screenshot, which is the number that
        # decides whether zooming again is worth a round trip. Reported rather than
        # left to be inferred: the model cannot see the native size of the region it
        # asked for, so it has no way to work this out.
        gain = (shot.width / rw) * (session.screen.width / max(session.ref_width, 1))
        note = (
            f" ({shot.width}x{shot.height} covering a {rw}x{rh} native area, "
            f"about {gain:.1f}x the detail of the full screenshot). To act on "
            "something you can see here, pass in_zoom=true with a coordinate in "
            "THIS image; that reaches individual native pixels, which a "
            "full-screenshot coordinate cannot."
        )
        return capture_result(session, name, shot, note)

    if name in _CLICK_BUTTONS:
        button, clicks = _CLICK_BUTTONS[name]
        modifiers = _text(params, required=False)
        # Resolved before the move. "ctrl+zzz" used to move the cursor and only then
        # fail, leaving a hover applied and the batch halted with no screenshot.
        _native.validate_chord(modifiers)
        target = None
        if params.get("coordinate") is not None:
            x, y = _coordinate(params["coordinate"], "coordinate")
            # may raise; nothing has moved yet
            target = session.resolve_point(x, y, bool(params.get("in_zoom")))
        if target is not None:
            _native.mouse_move(*target)
        _native.mouse_click(button, clicks, modifiers)
        return ActionResult(name, text="OK")

    if name == "left_click_drag":
        x0, y0 = _coordinate(params["start_coordinate"], "start_coordinate")
        x1, y1 = _coordinate(params["coordinate"], "coordinate")
        in_zoom = bool(params.get("in_zoom"))
        start = session.resolve_point(x0, y0, in_zoom)
        end = session.resolve_point(x1, y1, in_zoom)
        _native.mouse_drag(start[0], start[1], end[0], end[1], _text(params, required=False))
        return ActionResult(name, text="OK")

    if name == "mouse_move":
        x, y = _coordinate(params["coordinate"], "coordinate")
        _native.mouse_move(*session.resolve_point(x, y, bool(params.get("in_zoom"))))
        return ActionResult(name, text="OK")

    if name == "mouse_move_rel":
        # Kept before scaling: these are the numbers the model wrote, in screenshot
        # space. Quoting the scaled native ones back at it names a move it never
        # made -- it asked for 100 and would be shown 188.
        asked = (float(params.get("dx", 0)), float(params.get("dy", 0)))
        dx, dy = session.scale_delta(*asked)
        was_on = session.cursor_is_on_display()
        _native.mouse_move_relative(dx, dy, int(params.get("steps", 1)))
        session.confine_cursor(was_on)
        # Recorded after the move, so a rejected one is not counted as a turn that
        # happened. The hint is advisory: looking around legitimately reverses too.
        hint = session.note_relative_move(*asked)
        return ActionResult(
            name, text=f"OK (moved {dx:+d}, {dy:+d} native pixels){hint}"
        )

    if name == "aim":
        x, y = _coordinate(params["coordinate"], "coordinate")
        # Before the probe, which moves the view: rejecting the coordinate afterwards
        # left the camera rotated and then blamed the coordinate.
        session.check_in_frame(x, y)
        # Calibrating here rather than making the model do it first is the whole
        # point: a primitive with a setup step does not get used.
        note = ""
        already_turned = 0
        if session.aim_ratio is None:
            already_turned = session.autocalibrate_aim()
            note = f" [calibrated itself: aim_ratio={session.aim_ratio:.3g}]"
        dx, dy = session.aim_delta(x, y)
        # The probe turned the view as a side effect, and the coordinate was given
        # against the frame from before it, so that much of the turn is already done.
        dx -= already_turned
        was_on = session.cursor_is_on_display()
        _native.mouse_move_relative(dx, dy, int(params.get("steps", 1)))
        session.confine_cursor(was_on)
        # An aim is a measured turn, not a guess, so it must not leave a delta behind
        # for the next relative move to be accused of reversing.
        session.forget_relative_move()
        return ActionResult(
            name,
            text=f"OK (turned {dx:+d}, {dy:+d} native pixels to bring ({x:g}, {y:g}) "
            f"onto the crosshair){note}",
        )

    if name == "look":
        yaw = float(params.get("yaw", 0))
        pitch = float(params.get("pitch", 0))
        dx, dy = session.look_delta(yaw, pitch)
        was_on = session.cursor_is_on_display()
        _native.mouse_move_relative(dx, dy, int(params.get("steps", 1)))
        session.confine_cursor(was_on)
        return ActionResult(name, text=f"OK (yaw {yaw:+g}, pitch {pitch:+g} degrees "
                                       f"= {dx:+d}, {dy:+d} native pixels)")

    if name == "calibrate":
        if params.get("aim_ratio") is not None:
            session.set_aim_ratio(float(params["aim_ratio"]))
        if params.get("look_degrees_per_pixel") is not None:
            dpp = float(params["look_degrees_per_pixel"])
            session.set_look_scale(dpp, dpp)
        aim = "unset" if session.aim_ratio is None else f"{session.aim_ratio:g}"
        look = "unset" if session.look_scale is None else f"{session.look_scale[0]:g}"
        return ActionResult(
            name, text=f"OK (aim_ratio={aim}, look_degrees_per_pixel={look})"
        )

    if name == "key_down":
        _native.key_down(_text(params))
        held = _native.held_keys()
        listed = ", ".join(held) if held else "nothing"
        return ActionResult(name, text=f"OK (now held: {listed})")

    if name == "key_up":
        _native.key_up(_text(params))
        held = _native.held_keys()
        listed = ", ".join(held) if held else "nothing"
        return ActionResult(name, text=f"OK (still held: {listed})")

    if name == "left_mouse_down":
        _native.mouse_down("left")
        return ActionResult(name, text="OK")

    if name == "left_mouse_up":
        _native.mouse_up("left")
        return ActionResult(name, text="OK")

    if name == "cursor_position":
        x, y, on_display = session.cursor_in_screenshot_space()
        if not on_display:
            return ActionResult(
                name,
                text=f"X={x}, Y={y} (the cursor is on another display; this is the "
                f"nearest point on the one being controlled)",
            )
        return ActionResult(name, text=f"X={x}, Y={y}")

    if name == "scroll":
        direction = params["scroll_direction"]
        clicks = int(params["scroll_amount"])
        modifiers = _text(params, required=False)
        _native.validate_chord(modifiers)
        target = None
        if params.get("coordinate") is not None:
            x, y = _coordinate(params["coordinate"], "coordinate")
            target = session.resolve_point(x, y, bool(params.get("in_zoom")))
        if target is not None:
            _native.mouse_move(*target)
        _native.mouse_scroll(direction, clicks, modifiers)
        return ActionResult(name, text="OK")

    if name == "type":
        _native.type_text(_text(params))
        return ActionResult(name, text="OK")

    if name == "key":
        _native.press_key(_text(params), int(params.get("repeat", 1)))
        return ActionResult(name, text="OK")

    if name == "hold_key":
        _native.hold_key(_text(params), _duration(params))
        return ActionResult(name, text="OK")

    if name == "wait":
        _sleep_interruptibly(_duration(params))
        return ActionResult(name, text="OK")

    if name == "wait_for_change":
        limit = _duration(params)
        waited = session.wait_for_change(limit)
        if waited is None:
            return ActionResult(
                name,
                text=f"nothing changed within {limit:g}s. The screen is still. Either "
                "the action you were waiting on has already finished, or it never "
                "started -- check the screenshot rather than waiting again.",
            )
        return ActionResult(name, text=f"OK (the screen changed after {waited:.0f} ms)")

    raise ActionError(f"unhandled action {name!r}")  # pragma: no cover - guarded above

