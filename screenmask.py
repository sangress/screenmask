#!/usr/bin/env python3
"""Screen Mask - cover areas of the screen with black rectangles.

Two kinds of area:

  pitch-black   A solid black rectangle on top of everything. Nobody sees what
                is underneath, including you.

  zoom-block    You keep seeing the content. People watching your shared
                screen see a black rectangle. Linux cannot hide a window from
                screen capture, so this works through a "masked feed": the app
                shows a live copy of your screen with the areas blacked out in
                its own window, and you share THAT window in Zoom / Meet /
                Teams instead of the whole screen.

Runs on X11 and on Wayland (through XWayland, which is what lets the
rectangles sit at exact screen positions above every other window).
"""

import argparse
import json
import math
import os
import random
import signal
import sys

APP_NAME = "Screen Mask"
APP_ID = "io.github.screenmask.ScreenMask"
VERSION = "0.1.0"
SHARE_TITLE = "Screen Mask - share this window"

IS_WAYLAND_SESSION = bool(os.environ.get("WAYLAND_DISPLAY")) or (
    os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
)

DEPS_HINT = """\
Screen Mask needs GTK 3 and (for the masked feed) GStreamer with PipeWire.

  Ubuntu / Debian:
    sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 \\
        gir1.2-gstreamer-1.0 gstreamer1.0-plugins-base \\
        gstreamer1.0-plugins-good gstreamer1.0-pipewire

  Fedora:
    sudo dnf install python3-gobject gtk3 gstreamer1-plugins-base \\
        gstreamer1-plugins-good pipewire-gstreamer

  Arch:
    sudo pacman -S python-gobject gtk3 gst-plugins-base gst-plugins-good gst-plugin-pipewire
"""

if not os.environ.get("DISPLAY"):
    sys.stderr.write(
        "Screen Mask needs an X display. On Wayland this is provided by "
        "XWayland, which is enabled by default on GNOME and KDE.\n"
    )
    sys.exit(1)

# Wayland gives applications no way to place a window at an exact position or
# to keep it above all others, so the overlays always go through X11/XWayland.
os.environ["GDK_BACKEND"] = "x11"

try:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    gi.require_version("GdkX11", "3.0")
    from gi.repository import Gdk, GdkX11, Gio, GLib, Gtk  # noqa: F401
    import cairo
except (ImportError, ValueError) as exc:  # pragma: no cover
    sys.stderr.write("Missing dependency: %s\n\n%s" % (exc, DEPS_HINT))
    sys.exit(1)

PITCH = "pitch"
ZOOM = "zoom"
MODE_LABEL = {PITCH: "Pitch black", ZOOM: "Zoom block"}

AMBER = (0.96, 0.65, 0.14)
MIN_SIZE = 16
EDGE = 10
RING = 3

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "screenmask"
)
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

CURSORS = {
    "n": "n-resize", "s": "s-resize", "e": "e-resize", "w": "w-resize",
    "ne": "ne-resize", "nw": "nw-resize", "se": "se-resize", "sw": "sw-resize",
    "move": "move",
}


def log(*parts):
    sys.stderr.write("[screenmask] " + " ".join(str(p) for p in parts) + "\n")


# ----------------------------------------------------------------------------
# Data
# ----------------------------------------------------------------------------

class Region:
    _next_id = 1

    def __init__(self, mode, x, y, w, h):
        self.id = Region._next_id
        Region._next_id += 1
        self.mode = mode if mode in (PITCH, ZOOM) else PITCH
        self.x, self.y = int(x), int(y)
        self.w, self.h = max(MIN_SIZE, int(w)), max(MIN_SIZE, int(h))

    def to_json(self):
        return {"mode": self.mode, "x": self.x, "y": self.y, "w": self.w, "h": self.h}


def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(data):
    try:
        os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
        tmp = CONFIG_FILE + ".tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w",
                  encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, CONFIG_FILE)
    except OSError as exc:
        log("could not save settings:", exc)


def use_rgba_visual(window):
    """Give the window an alpha channel when a compositor is running."""
    screen = window.get_screen()
    visual = screen.get_rgba_visual()
    if visual is not None and screen.is_composited():
        window.set_visual(visual)
        return True
    return False


def screen_bounds():
    """Bounding box of all monitors, in GDK coordinates."""
    display = Gdk.Display.get_default()
    x0 = y0 = 10 ** 9
    x1 = y1 = -(10 ** 9)
    for i in range(display.get_n_monitors()):
        g = display.get_monitor(i).get_geometry()
        x0, y0 = min(x0, g.x), min(y0, g.y)
        x1, y1 = max(x1, g.x + g.width), max(y1, g.y + g.height)
    if x1 <= x0 or y1 <= y0:
        root = Gdk.Screen.get_default().get_root_window()
        return 0, 0, root.get_width(), root.get_height()
    return x0, y0, x1 - x0, y1 - y0


# ----------------------------------------------------------------------------
# The rectangles on screen
# ----------------------------------------------------------------------------

class RegionWindow(Gtk.Window):
    """One rectangle. Override-redirect, so it stays above every other window,
    including fullscreen ones, on every workspace.

    A window is created either as a full rectangle or as a click-through
    outline and keeps that form for life; the app replaces the window when
    the form has to change. (GNOME Shell does not redraw a window correctly
    after its shape is removed, so the shape is never changed after creation.)
    """

    def __init__(self, app, region):
        super().__init__(type=Gtk.WindowType.POPUP)
        self.app = app
        self.region = region
        self.drag = None
        self.outline = self.wants_outline(app, region)
        self.set_title("Screen Mask area")
        self.set_app_paintable(True)
        self.set_accept_focus(False)
        self.composited = use_rgba_visual(self)
        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self.connect("draw", self.on_draw)
        self.connect("button-press-event", self.on_press)
        self.connect("button-release-event", self.on_release)
        self.connect("motion-notify-event", self.on_motion)
        self.move(region.x, region.y)
        self.resize(region.w, region.h)
        if self.outline:
            # Only a thin frame is part of the window, and it ignores the
            # mouse, so the content underneath stays fully usable.
            ring = cairo.Region(cairo.RectangleInt(0, 0, region.w, region.h))
            if region.w > 2 * RING and region.h > 2 * RING:
                ring.subtract(cairo.RectangleInt(
                    RING, RING, region.w - 2 * RING, region.h - 2 * RING))
            self.shape_combine_region(ring)
            self.input_shape_combine_region(cairo.Region())
        self.apply_state()

    @staticmethod
    def wants_outline(app, region):
        return region.mode == ZOOM and not app.editing

    # -- appearance ---------------------------------------------------------

    def apply_state(self):
        if self.outline and not self.app.show_outlines:
            self.hide()
            return
        self.show()
        self.queue_draw()

    def on_draw(self, _widget, cr):
        r = self.region
        w, h = self.get_allocated_width(), self.get_allocated_height()
        editing = self.app.editing
        cr.set_operator(cairo.OPERATOR_SOURCE)

        if r.mode == PITCH:
            cr.set_source_rgb(0, 0, 0)
            cr.paint()
            if editing:
                self._edit_decor(cr, w, h, (0.6, 0.6, 0.6), "Pitch black")
            return False

        if self.outline:
            cr.set_source_rgb(*AMBER)
            cr.paint()
            cr.set_source_rgb(0, 0, 0)
            cr.set_line_width(RING)
            cr.set_dash([8, 8])
            cr.rectangle(RING / 2, RING / 2, w - RING, h - RING)
            cr.stroke()
            return False

        if self.composited:
            cr.set_source_rgba(AMBER[0], AMBER[1], AMBER[2], 0.28)
        else:
            cr.set_source_rgb(0.30, 0.21, 0.05)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        self._edit_decor(cr, w, h, AMBER, "Zoom block - hidden from viewers")
        return False

    @staticmethod
    def _edit_decor(cr, w, h, color, text):
        cr.set_operator(cairo.OPERATOR_OVER)
        cr.set_source_rgb(*color)
        cr.set_line_width(2)
        cr.set_dash([6, 4])
        cr.rectangle(1, 1, w - 2, h - 2)
        cr.stroke()
        cr.set_dash([])
        for cx, cy in ((0, 0), (w - 8, 0), (0, h - 8), (w - 8, h - 8)):
            cr.rectangle(cx, cy, 8, 8)
        cr.fill()
        if w > 90 and h > 34:
            cr.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL,
                                cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(12)
            ext = cr.text_extents(text)
            cr.save()
            cr.rectangle(0, 0, w - 10, h)
            cr.clip()
            cr.rectangle(8, 8, ext.width + 14, 22)
            cr.fill()
            cr.set_source_rgb(0, 0, 0)
            cr.move_to(15, 23)
            cr.show_text(text)
            cr.restore()

    # -- mouse --------------------------------------------------------------

    def _hit(self, x, y):
        w, h = self.region.w, self.region.h
        e = max(4, min(EDGE, w // 3, h // 3))
        name = ("n" if y < e else "s" if y >= h - e else "") + (
            "w" if x < e else "e" if x >= w - e else ""
        )
        return name or "move"

    def _set_cursor(self, name):
        win = self.get_window()
        if win is None:
            return
        cursor = None
        if name:
            cursor = Gdk.Cursor.new_from_name(self.get_display(), CURSORS.get(name, name))
        win.set_cursor(cursor)

    def on_press(self, _widget, event):
        if event.button == 3:
            self._menu(event)
            return True
        if event.button != 1 or not self.app.editing:
            return True
        r = self.region
        self.drag = (self._hit(event.x, event.y), event.x_root, event.y_root,
                     r.x, r.y, r.w, r.h)
        return True

    def on_release(self, _widget, event):
        if event.button == 1 and self.drag:
            self.drag = None
            self.app.regions_changed(geometry_only=True)
        return True

    def on_motion(self, _widget, event):
        if not self.app.editing:
            self._set_cursor(None)
            return True
        if not self.drag:
            self._set_cursor(self._hit(event.x, event.y))
            return True
        kind, px, py, x, y, w, h = self.drag
        dx, dy = int(round(event.x_root - px)), int(round(event.y_root - py))
        if kind == "move":
            x, y = x + dx, y + dy
        else:
            if "e" in kind:
                w = max(MIN_SIZE, w + dx)
            if "s" in kind:
                h = max(MIN_SIZE, h + dy)
            if "w" in kind:
                nw = max(MIN_SIZE, w - dx)
                x, w = x + (w - nw), nw
            if "n" in kind:
                nh = max(MIN_SIZE, h - dy)
                y, h = y + (h - nh), nh
        bx, by, bw, bh = screen_bounds()
        x = min(max(x, bx - w + 24), bx + bw - 24)
        y = min(max(y, by - h + 24), by + bh - 24)
        r = self.region
        r.x, r.y, r.w, r.h = x, y, w, h
        self.move(x, y)
        self.resize(w, h)
        self.queue_draw()
        self.app.regions_changed(geometry_only=True, save=False)
        return True

    def _menu(self, event):
        menu = Gtk.Menu()
        r = self.region
        other = ZOOM if r.mode == PITCH else PITCH
        item = Gtk.MenuItem(label="Switch to %s" % MODE_LABEL[other].lower())
        item.connect("activate", lambda *_: GLib.idle_add(self.app.set_mode, r, other))
        menu.append(item)
        item = Gtk.CheckMenuItem(label="Edit areas (move / resize)")
        item.set_active(self.app.editing)
        item.connect("toggled",
                     lambda i: GLib.idle_add(self.app.set_editing, i.get_active()))
        menu.append(item)
        item = Gtk.MenuItem(label="Remove this area")
        item.connect("activate", lambda *_: GLib.idle_add(self.app.remove_region, r))
        menu.append(item)
        menu.append(Gtk.SeparatorMenuItem())
        item = Gtk.MenuItem(label="Open Screen Mask")
        item.connect("activate", lambda *_: self.app.panel.present())
        menu.append(item)
        menu.show_all()
        self.app.menu = menu   # outlives this window, which an action may replace
        menu.popup_at_pointer(event)


class DrawOverlay(Gtk.Window):
    """Full-screen layer on which the user drags out a new rectangle."""

    def __init__(self, app, mode):
        super().__init__(type=Gtk.WindowType.POPUP)
        self.app = app
        self.mode = mode
        self.start = None
        self.cur = None
        self.set_title("Screen Mask area selection")
        self.set_app_paintable(True)
        self.composited = use_rgba_visual(self)
        self.ox, self.oy, w, h = screen_bounds()
        self.move(self.ox, self.oy)
        self.resize(w, h)
        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self.connect("draw", self.on_draw)
        self.connect("button-press-event", self.on_press)
        self.connect("button-release-event", self.on_release)
        self.connect("motion-notify-event", self.on_motion)
        self.connect("realize", lambda *_: self.get_window().set_cursor(
            Gdk.Cursor.new_from_name(self.get_display(), "crosshair")))
        self.show()

    def _rect(self):
        if not self.start or not self.cur:
            return None
        x0, y0 = self.start
        x1, y1 = self.cur
        return min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)

    def on_draw(self, _widget, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        if self.composited:
            cr.set_source_rgba(0, 0, 0, 0.45)
        else:
            cr.set_source_rgb(0.18, 0.18, 0.18)
        cr.paint()
        rect = self._rect()
        if rect:
            x, y, rw, rh = rect
            if self.mode == PITCH:
                cr.set_source_rgba(0, 0, 0, 1)
            elif self.composited:
                cr.set_source_rgba(AMBER[0], AMBER[1], AMBER[2], 0.30)
            else:
                cr.set_source_rgb(0.30, 0.21, 0.05)
            cr.rectangle(x, y, rw, rh)
            cr.fill()
            cr.set_operator(cairo.OPERATOR_OVER)
            cr.set_source_rgb(*(AMBER if self.mode == ZOOM else (0.7, 0.7, 0.7)))
            cr.set_line_width(2)
            cr.rectangle(x + 1, y + 1, max(1, rw - 2), max(1, rh - 2))
            cr.stroke()
        cr.set_operator(cairo.OPERATOR_OVER)
        text = "Drag to mark the %s area.   Right-click to cancel." % (
            MODE_LABEL[self.mode].lower())
        cr.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(20)
        ext = cr.text_extents(text)
        display = self.get_display()
        mon = display.get_primary_monitor() or display.get_monitor(0)
        g = mon.get_geometry()
        tx = g.x - self.ox + (g.width - ext.width) / 2
        ty = g.y - self.oy + 70
        cr.set_source_rgba(0, 0, 0, 0.75)
        cr.rectangle(tx - 18, ty - 30, ext.width + 36, 46)
        cr.fill()
        cr.set_source_rgb(1, 1, 1)
        cr.move_to(tx, ty)
        cr.show_text(text)
        return False

    def on_press(self, _widget, event):
        if event.button == 1:
            self.start = self.cur = (int(event.x), int(event.y))
        else:
            self.app.finish_drawing(None)
        return True

    def on_motion(self, _widget, event):
        if self.start:
            self.cur = (int(event.x), int(event.y))
            self.queue_draw()
        return True

    def on_release(self, _widget, event):
        if event.button != 1 or not self.start:
            return True
        self.cur = (int(event.x), int(event.y))
        rect = self._rect()
        self.start = self.cur = None
        if rect and rect[2] >= MIN_SIZE and rect[3] >= MIN_SIZE:
            x, y, w, h = rect
            self.app.finish_drawing((self.mode, x + self.ox, y + self.oy, w, h))
        else:
            self.queue_draw()  # too small: treat as a stray click, keep waiting
        return True


# ----------------------------------------------------------------------------
# Screen capture
# ----------------------------------------------------------------------------

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_SCREENCAST = "org.freedesktop.portal.ScreenCast"


class PortalSession:
    """Asks the desktop for a screen-capture stream (Wayland).

    The first time, the desktop shows its own "share your screen" dialog. The
    choice is remembered with a restore token so later starts are silent.
    """

    def __init__(self, restore_token, on_ready, on_fail, on_closed):
        self.restore_token = restore_token or ""
        self.on_ready, self.on_fail, self.on_closed = on_ready, on_fail, on_closed
        self.bus = None
        self.session = None
        self.closed_sub = 0
        self.subs = set()
        self.done = False
        self.version = 1
        self.cursor_modes = 1

    @staticmethod
    def _token():
        return "screenmask%d" % random.randint(1, 2 ** 31)

    def start(self):
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self.sender = self.bus.get_unique_name()[1:].replace(".", "_")
            self.version = self._prop("version") or 1
            self.cursor_modes = self._prop("AvailableCursorModes") or 1
        except GLib.Error as exc:
            self._fail("The desktop's screen-sharing service (xdg-desktop-portal) "
                       "is not available: %s" % exc.message)
            return
        token = self._token()
        self._request("CreateSession", GLib.Variant("(a{sv})", ({
            "handle_token": GLib.Variant("s", token),
            "session_handle_token": GLib.Variant("s", self._token()),
        },)), token, self._on_session)

    def _prop(self, name):
        res = self.bus.call_sync(
            PORTAL_BUS, PORTAL_PATH, "org.freedesktop.DBus.Properties", "Get",
            GLib.Variant("(ss)", (PORTAL_SCREENCAST, name)),
            GLib.VariantType("(v)"), Gio.DBusCallFlags.NONE, 5000, None)
        return res.unpack()[0]

    def _request(self, method, params, token, callback):
        path = "%s/request/%s/%s" % (PORTAL_PATH, self.sender, token)
        holder = {}

        def on_response(_conn, _sender, _path, _iface, _signal, args):
            self.bus.signal_unsubscribe(holder["id"])
            self.subs.discard(holder["id"])
            if self.done:
                return
            code, results = args.unpack()
            if code == 0:
                callback(results)
            elif code == 1:
                self._fail("Screen capture was not allowed.")
            else:
                self._fail("The desktop refused the screen capture request (%s)." % method)

        holder["id"] = self.bus.signal_subscribe(
            PORTAL_BUS, "org.freedesktop.portal.Request", "Response", path, None,
            Gio.DBusSignalFlags.NONE, on_response)
        self.subs.add(holder["id"])

        def on_called(bus, result):
            try:
                bus.call_finish(result)
            except GLib.Error as exc:
                self._fail("Screen capture request failed (%s): %s" % (method, exc.message))

        self.bus.call(PORTAL_BUS, PORTAL_PATH, PORTAL_SCREENCAST, method, params,
                      None, Gio.DBusCallFlags.NONE, -1, None, on_called)

    def _on_session(self, results):
        self.session = results.get("session_handle")
        if not self.session:
            self._fail("The desktop did not open a screen capture session.")
            return
        self.closed_sub = self.bus.signal_subscribe(
            PORTAL_BUS, "org.freedesktop.portal.Session", "Closed", self.session,
            None, Gio.DBusSignalFlags.NONE, self._on_session_closed)
        token = self._token()
        opts = {
            "handle_token": GLib.Variant("s", token),
            "types": GLib.Variant("u", 1),          # whole monitors only
            "multiple": GLib.Variant("b", False),
        }
        if self.version >= 2:
            mode = 2 if self.cursor_modes & 2 else (4 if self.cursor_modes & 4 else 1)
            opts["cursor_mode"] = GLib.Variant("u", mode)
        if self.version >= 4:
            opts["persist_mode"] = GLib.Variant("u", 2)
            if self.restore_token:
                opts["restore_token"] = GLib.Variant("s", self.restore_token)
        self._request("SelectSources",
                      GLib.Variant("(oa{sv})", (self.session, opts)),
                      token, self._on_selected)

    def _on_selected(self, _results):
        token = self._token()
        self._request("Start", GLib.Variant("(osa{sv})", (
            self.session, "", {"handle_token": GLib.Variant("s", token)})),
            token, self._on_started)

    def _on_started(self, results):
        streams = results.get("streams") or []
        if not streams:
            self._fail("The desktop did not provide a screen stream.")
            return
        node_id, props = streams[0]
        try:
            res, fds = self.bus.call_with_unix_fd_list_sync(
                PORTAL_BUS, PORTAL_PATH, PORTAL_SCREENCAST, "OpenPipeWireRemote",
                GLib.Variant("(oa{sv})", (self.session, {})),
                GLib.VariantType("(h)"), Gio.DBusCallFlags.NONE, -1, None, None)
            fd = fds.get(res.unpack()[0])
        except GLib.Error as exc:
            self._fail("Could not connect to the screen stream: %s" % exc.message)
            return
        self.on_ready(fd, node_id, props.get("position"), props.get("size"),
                      results.get("restore_token") or "")

    def _on_session_closed(self, *_args):
        if not self.done:
            self.done = True
            self._unsubscribe()
            self.on_closed()

    def _fail(self, message):
        if self.done:
            return
        self.close()
        self.on_fail(message)

    def _unsubscribe(self):
        if self.bus is None:
            return
        for sub in list(self.subs):
            self.bus.signal_unsubscribe(sub)
        self.subs.clear()
        if self.closed_sub:
            self.bus.signal_unsubscribe(self.closed_sub)
            self.closed_sub = 0

    def close(self):
        self.done = True
        self._unsubscribe()
        if self.bus is not None and self.session:
            self.bus.call(PORTAL_BUS, self.session, "org.freedesktop.portal.Session",
                          "Close", None, None, Gio.DBusCallFlags.NONE, -1, None, None)
            self.session = None


def match_monitor(position, size):
    """Find the monitor (in our X11 coordinates) that a portal stream shows."""
    display = Gdk.Display.get_default()
    geoms = []
    for i in range(display.get_n_monitors()):
        g = display.get_monitor(i).get_geometry()
        geoms.append((g.x, g.y, g.width, g.height))
    if not geoms:
        return screen_bounds()
    if len(geoms) == 1 or not position or not size or not size[0]:
        if len(geoms) > 1:
            mon = display.get_primary_monitor()
            if mon is not None:
                g = mon.get_geometry()
                return g.x, g.y, g.width, g.height
        return geoms[0]
    px, py = position
    sw, sh = size

    def score(g):
        k = g[2] / float(sw)  # X11 units per compositor unit on this monitor
        return (abs(g[0] - px * k) + abs(g[1] - py * k)
                + abs(g[3] - sh * k) + abs(g[2] - sw) * 0.01)

    return min(geoms, key=score)


class Feed:
    """Captures the screen and keeps the latest frame as a cairo surface."""

    def __init__(self, app):
        self.app = app
        self.pipeline = None
        self.sink = None
        self.portal = None
        self.fd = -1
        self.surface = None
        self.size = (0, 0)
        self.capture_rect = None      # the captured area in X11 coordinates
        self.timer = 0
        self.interval = 50
        self.idle_ticks = 0
        self.last_data = None
        self.starting = False
        self.Gst = None

    @property
    def running(self):
        return self.pipeline is not None

    def start(self):
        if self.running or self.starting:
            return
        try:
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
            Gst.init(None)
        except (ImportError, ValueError) as exc:
            self.app.feed_stopped("GStreamer is missing (%s). Install it and try again:\n%s"
                                  % (exc, DEPS_HINT), error=True)
            return
        self.Gst = Gst
        mode = self.app.args.capture
        if mode == "auto":
            mode = "portal" if IS_WAYLAND_SESSION else "x11"
        self.starting = True
        if mode == "portal":
            if Gst.ElementFactory.find("pipewiresrc") is None:
                self._abort("The GStreamer PipeWire plugin is missing "
                            "(package gstreamer1.0-pipewire or pipewire-gstreamer).")
                return
            self.app.feed_status("Waiting for permission to capture the screen...")
            self.portal = PortalSession(self.app.restore_token, self._portal_ready,
                                        self._abort, self._portal_closed)
            self.portal.start()
        else:
            if Gst.ElementFactory.find("ximagesrc") is None:
                self._abort("The GStreamer X11 capture plugin is missing "
                            "(package gstreamer1.0-x or gstreamer1-plugins-good).")
                return
            root = Gdk.Screen.get_default().get_root_window()
            self._launch(
                "ximagesrc use-damage=false show-pointer=true ! "
                "video/x-raw,framerate=%d/1" % self.app.args.fps,
                (0, 0, root.get_width(), root.get_height()))

    def _portal_ready(self, fd, node_id, position, size, restore_token):
        self.fd = fd
        if restore_token:
            self.app.restore_token = restore_token
            self.app.save()
        rect = match_monitor(position, size)
        self._launch("pipewiresrc fd=%d path=%d do-timestamp=true keepalive-time=500 ! "
                     "video/x-raw" % (fd, node_id), rect)

    def _portal_closed(self):
        self.stop("Screen capture was stopped by the desktop.")

    def _launch(self, source, capture_rect):
        Gst = self.Gst
        desc = (source + " ! videoconvert ! video/x-raw,format=BGRx ! "
                "appsink name=sink max-buffers=1 drop=true sync=false")
        try:
            self.pipeline = Gst.parse_launch(desc)
        except GLib.Error as exc:
            self.pipeline = None
            self._abort("Could not set up screen capture: %s" % exc.message)
            return
        self.sink = self.pipeline.get_by_name("sink")
        self.capture_rect = capture_rect
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_error)
        bus.connect("message::eos", lambda *_: self.stop("The screen stream ended."))
        self.pipeline.set_state(Gst.State.PLAYING)
        self.starting = False
        self.interval = max(15, int(1000 / self.app.args.fps))
        self.timer = GLib.timeout_add(self.interval, self._poll)
        self.app.feed_started()

    def _on_error(self, _bus, message):
        err, _debug = message.parse_error()
        self.stop("Screen capture failed: %s" % err.message, error=True)

    def _poll(self):
        if self.sink is None:
            self.timer = 0
            return False
        sample = self.sink.emit("try-pull-sample", 0)
        changed = False
        if sample is not None:
            try:
                changed = self._take(sample)
            except Exception as exc:  # never show a half-read frame
                self.stop("Could not read a screen frame: %s" % exc, error=True)
                return False
        self.idle_ticks += 1
        if changed:
            self.idle_ticks = 0
            self.app.share.repaint()
        elif self.idle_ticks == 1:
            self.app.share.nudge()
        elif self.idle_ticks * self.interval >= 400:
            # A still screen produces no new frames. Repaint a couple of
            # times a second anyway, so an app that starts sharing the window
            # at that moment gets a current picture right away instead of
            # waiting for the next change on screen.
            self.idle_ticks = 0
            self.app.share.repaint()
        return True

    def _take(self, sample):
        Gst = self.Gst
        st = sample.get_caps().get_structure(0)
        w, h = st.get_value("width"), st.get_value("height")
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            raise RuntimeError("frame not readable")
        try:
            data = info.data
            if data == self.last_data and self.size == (w, h):
                return False             # nothing on screen changed
            self.last_data = data
            if self.surface is None or self.size != (w, h):
                first = self.surface is None
                self.surface = cairo.ImageSurface(cairo.FORMAT_RGB24, w, h)
                self.size = (w, h)
                if first:
                    GLib.idle_add(self.app.feed_status_running)
            self.surface.flush()
            dst = self.surface.get_data()
            dst_stride = self.surface.get_stride()
            src_stride = len(data) // h
            if src_stride == dst_stride:
                dst[:dst_stride * h] = data[:dst_stride * h]
            else:
                n = min(src_stride, dst_stride)
                for row in range(h):
                    dst[row * dst_stride:row * dst_stride + n] = \
                        data[row * src_stride:row * src_stride + n]
            self.surface.mark_dirty()
        finally:
            buf.unmap(info)
        return True

    def _abort(self, message):
        self.starting = False
        self.stop(message, error=True)

    def stop(self, message=None, error=False):
        was_active = self.running or self.starting or self.portal is not None
        self.starting = False
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        if self.pipeline is not None:
            self.pipeline.get_bus().remove_signal_watch()
            self.pipeline.set_state(self.Gst.State.NULL)
            self.pipeline = None
            self.sink = None
        if self.portal is not None:
            self.portal.close()
            self.portal = None
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1
        self.surface = None          # the share window goes black at once
        self.last_data = None
        self.size = (0, 0)
        self.capture_rect = None
        if was_active or message:
            self.app.feed_stopped(message, error=error)


# ----------------------------------------------------------------------------
# The window that is shared in the meeting
# ----------------------------------------------------------------------------

class ShareWindow(Gtk.Window):
    def __init__(self, app):
        super().__init__(title=SHARE_TITLE)
        self.app = app
        self.parked = False
        self.park_desktop = 0
        self.park_monitor = 0
        self.settle_tries = 0
        self.settle_timer = 0
        self.pump = None
        self.set_default_size(960, 540)
        self.set_icon_name("video-display")
        self.area = Gtk.DrawingArea()
        self.area.connect("draw", self.on_draw)
        self.add(self.area)
        self.connect("delete-event", self.on_delete)

    def on_delete(self, *_):
        self.app.stop_feed()
        return True

    def can_park(self):
        screen = self.get_screen()
        try:
            return screen.get_number_of_desktops() > 1
        except AttributeError:
            return False

    def present_feed(self, park):
        """Show the window, either parked full-screen on a workspace the user
        is not looking at, or as an ordinary window kept behind the others."""
        screen = self.get_screen()
        self.parked = bool(park and self.can_park())
        if self.parked:
            count = screen.get_number_of_desktops()
            current = screen.get_current_desktop()
            self.park_desktop = count - 1 if current != count - 1 else count - 2
            display = self.get_display()
            self.park_monitor = 0
            for i in range(display.get_n_monitors()):
                if display.get_monitor(i).is_primary():
                    self.park_monitor = i
        self._map()
        self.settle_tries = 10
        if not self.settle_timer:
            self.settle_timer = GLib.timeout_add(400, self._settle)

    def _map(self):
        if self.parked:
            # The window has to become full-screen on the workspace the user
            # is looking at and only then move to the spare one: resizing a
            # window that already sits on a hidden workspace leaves GNOME
            # Shell streaming a stale picture of it. For that moment it stays
            # behind the other windows, without taking the keyboard focus.
            self.set_focus_on_map(False)
            self.fullscreen_on_monitor(self.get_screen(), self.park_monitor)
        else:
            self.set_focus_on_map(True)
            self.unfullscreen()
        self.set_keep_below(True)
        self.show_all()

    def _settle(self):
        """Walk the window to its place. This takes about a second at the
        start of the feed, before anyone can be sharing the window."""
        gdk_window = self.get_window()
        self.settle_tries -= 1
        if gdk_window is None or not self.get_mapped():
            self.settle_timer = 0
            return False
        fullscreen = bool(gdk_window.get_state() & Gdk.WindowState.FULLSCREEN)
        desktop = gdk_window.get_desktop()
        if os.environ.get("SCREENMASK_DEBUG"):
            log("settle: parked=%s fullscreen=%s desktop=%s target=%s" % (
                self.parked, fullscreen, desktop, self.park_desktop))
        if self.settle_tries >= 0:
            if not self.parked:
                if fullscreen:
                    self.unfullscreen()
                    return True
            elif not fullscreen:
                if desktop == self.park_desktop:
                    self.hide()          # wrong order happened; start over
                    self._map()
                else:
                    self.fullscreen_on_monitor(self.get_screen(), self.park_monitor)
                return True
            elif desktop != self.park_desktop:
                gdk_window.move_to_desktop(self.park_desktop)
                return True
        elif self.parked and desktop != self.park_desktop:
            self.parked = False          # could not park; say so in the status
        self.settle_timer = 0
        self.app.feed_status_running()
        return False

    def repaint(self):
        self.queue_draw()
        self.nudge()

    def nudge(self):
        """Make the compositor run one update cycle.

        On Wayland a window that is not on screen only gets its new content
        picked up when the compositor updates the screen. After a single
        change on an otherwise still screen that would leave viewers one
        frame behind, so each repaint of the parked window is followed by a
        repaint of a 1-pixel, fully transparent window on the visible
        workspace. It changes nothing on screen but triggers that update.
        """
        if not (self.parked and IS_WAYLAND_SESSION and self.get_mapped()):
            if self.pump is not None:
                self.pump.destroy()
                self.pump = None
            return
        if self.pump is None:
            pump = Gtk.Window(type=Gtk.WindowType.POPUP)
            pump.set_title("Screen Mask sync")
            pump.set_app_paintable(True)
            pump.set_accept_focus(False)
            if not use_rgba_visual(pump):
                pump.destroy()
                return
            pump.connect("draw", self._draw_pump)
            pump.input_shape_combine_region(cairo.Region())
            x, y, _w, _h = screen_bounds()
            pump.move(x, y)
            pump.resize(1, 1)
            pump.show()
            self.pump = pump
        self.pump.queue_draw()

    @staticmethod
    def _draw_pump(_widget, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        return False

    def on_draw(self, _widget, cr):
        aw = self.area.get_allocated_width()
        ah = self.area.get_allocated_height()
        cr.set_source_rgb(0, 0, 0)
        cr.paint()
        feed = self.app.feed
        surface = feed.surface
        if surface is None or not feed.capture_rect:
            cr.set_source_rgb(0.6, 0.6, 0.6)
            cr.select_font_face("sans-serif")
            cr.set_font_size(18)
            text = "Screen Mask: the masked feed is not running"
            ext = cr.text_extents(text)
            cr.move_to((aw - ext.width) / 2, ah / 2)
            cr.show_text(text)
            return False

        fw, fh = feed.size
        scale = min(aw / fw, ah / fh)
        ox, oy = (aw - fw * scale) / 2.0, (ah - fh * scale) / 2.0
        cr.save()
        cr.translate(ox, oy)
        cr.scale(scale, scale)
        cr.rectangle(0, 0, fw, fh)
        cr.clip()
        cr.set_source_surface(surface, 0, 0)
        cr.get_source().set_filter(
            cairo.FILTER_NEAREST if abs(scale - 1.0) < 1e-6 else cairo.FILTER_BILINEAR)
        cr.paint()
        cr.restore()

        # The black rectangles are painted in the same pass as the frame, so
        # no frame can reach the window without them. They are grown slightly
        # and snapped outward to whole pixels so scaling cannot leak an edge.
        cx, cy, cw, ch = feed.capture_rect
        kx, ky = fw / float(cw), fh / float(ch)
        pad = 2
        cr.set_source_rgb(0, 0, 0)
        cr.set_antialias(cairo.ANTIALIAS_NONE)
        for r in self.app.regions:
            fx0, fy0 = (r.x - cx) * kx - pad, (r.y - cy) * ky - pad
            fx1, fy1 = (r.x + r.w - cx) * kx + pad, (r.y + r.h - cy) * ky + pad
            fx0, fy0 = max(0.0, fx0), max(0.0, fy0)
            fx1, fy1 = min(float(fw), fx1), min(float(fh), fy1)
            if fx1 <= fx0 or fy1 <= fy0:
                continue
            x0 = math.floor(ox + fx0 * scale)
            y0 = math.floor(oy + fy0 * scale)
            x1 = math.ceil(ox + fx1 * scale)
            y1 = math.ceil(oy + fy1 * scale)
            cr.rectangle(x0, y0, x1 - x0, y1 - y0)
        cr.fill()
        return False


# ----------------------------------------------------------------------------
# Control panel
# ----------------------------------------------------------------------------

CSS = b"""
.sm-heading { font-weight: bold; }
.sm-dim { opacity: 0.72; }
.sm-error { color: #c01c28; }
.sm-chip-pitch { background: #000000; border-radius: 3px; border: 1px solid #555; }
.sm-chip-zoom { background: #f5a623; border-radius: 3px; border: 1px solid #a66a00; }
"""


class ControlPanel(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME)
        self.app = app
        self.rows = {}
        self.set_icon_name("video-display")
        self.set_default_size(440, -1)
        self.set_resizable(False)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            self.get_screen(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_border_width(16)
        self.add(box)

        box.pack_start(self._heading("Areas"), False, False, 0)
        buttons = Gtk.Box(spacing=8, homogeneous=True)
        self.add_pitch = Gtk.Button(label="Add pitch-black area")
        self.add_pitch.connect("clicked", lambda *_: app.begin_drawing(PITCH))
        self.add_zoom = Gtk.Button(label="Add zoom-block area")
        self.add_zoom.connect("clicked", lambda *_: app.begin_drawing(ZOOM))
        buttons.pack_start(self.add_pitch, True, True, 0)
        buttons.pack_start(self.add_zoom, True, True, 0)
        box.pack_start(buttons, False, False, 0)
        box.pack_start(self._note(
            "Pitch black hides the area from everyone, including you. "
            "Zoom block keeps it visible to you and hides it only in the masked feed."),
            False, False, 0)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        placeholder = Gtk.Label(label="No areas yet")
        placeholder.get_style_context().add_class("sm-dim")
        placeholder.set_margin_top(10)
        placeholder.set_margin_bottom(10)
        placeholder.show()
        self.listbox.set_placeholder(placeholder)
        frame = Gtk.Frame()
        frame.add(self.listbox)
        box.pack_start(frame, False, False, 0)

        self.edit_switch = self._switch_row(
            box, "Edit areas", "Drag to move, drag an edge to resize", app.editing,
            app.set_editing)
        self.outline_switch = self._switch_row(
            box, "Outline zoom-block areas", "A thin frame on your screen shows where they are",
            app.show_outlines, app.set_outlines)

        box.pack_start(Gtk.Separator(), False, False, 4)
        box.pack_start(self._heading("Screen sharing"), False, False, 0)
        box.pack_start(self._note(
            "Start the masked feed, then in Zoom, Meet or Teams share the window "
            "named “%s”. Do not share the entire screen: that shows "
            "zoom-block areas uncovered." % SHARE_TITLE), False, False, 0)

        self.feed_button = Gtk.Button(label="Start masked feed")
        self.feed_button.get_style_context().add_class("suggested-action")
        self.feed_button.connect("clicked", lambda *_: app.toggle_feed())
        box.pack_start(self.feed_button, False, False, 0)

        self.park_check = Gtk.CheckButton(
            label="Keep the share window full-size on a spare workspace")
        self.park_check.set_active(app.park)
        self.park_check.set_tooltip_text(
            "Applies when the masked feed starts. Without it the share window is an "
            "ordinary window on this workspace, and viewers get it at the size you make it.")
        self.park_check.connect("toggled", lambda c: app.set_park(c.get_active()))
        box.pack_start(self.park_check, False, False, 0)

        self.status = Gtk.Label(label="Masked feed is off.", xalign=0)
        self.status.set_line_wrap(True)
        self.status.set_max_width_chars(46)
        self.status.set_selectable(True)
        box.pack_start(self.status, False, False, 0)

        self.connect("delete-event", self.on_delete)
        self.refresh_rows()
        self.show_all()

    @staticmethod
    def _heading(text):
        label = Gtk.Label(label=text, xalign=0)
        label.get_style_context().add_class("sm-heading")
        return label

    @staticmethod
    def _note(text):
        label = Gtk.Label(label=text, xalign=0)
        label.set_line_wrap(True)
        label.set_max_width_chars(52)
        label.get_style_context().add_class("sm-dim")
        return label

    def _switch_row(self, box, title, subtitle, active, callback):
        row = Gtk.Box(spacing=12)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.pack_start(Gtk.Label(label=title, xalign=0), False, False, 0)
        text.pack_start(self._note(subtitle), False, False, 0)
        row.pack_start(text, True, True, 0)
        switch = Gtk.Switch()
        switch.set_active(active)
        switch.set_valign(Gtk.Align.CENTER)
        switch.connect("notify::active", lambda s, _p: callback(s.get_active()))
        row.pack_end(switch, False, False, 0)
        box.pack_start(row, False, False, 0)
        return switch

    @staticmethod
    def _describe(r):
        return "%d × %d at %d, %d" % (r.w, r.h, r.x, r.y)

    def refresh_rows(self):
        for child in self.listbox.get_children():
            self.listbox.remove(child)
        self.rows = {}
        for r in self.app.regions:
            row = Gtk.ListBoxRow()
            row.set_activatable(False)
            line = Gtk.Box(spacing=10)
            line.set_border_width(6)
            chip = Gtk.Box()
            chip.set_size_request(16, 16)
            chip.set_valign(Gtk.Align.CENTER)
            chip.get_style_context().add_class("sm-chip-%s" % r.mode)
            line.pack_start(chip, False, False, 0)
            combo = Gtk.ComboBoxText()
            combo.append(PITCH, MODE_LABEL[PITCH])
            combo.append(ZOOM, MODE_LABEL[ZOOM])
            combo.set_active_id(r.mode)
            combo.connect("changed", lambda c, reg=r: self.app.set_mode(reg, c.get_active_id()))
            line.pack_start(combo, False, False, 0)
            label = Gtk.Label(label=self._describe(r), xalign=0)
            label.get_style_context().add_class("sm-dim")
            line.pack_start(label, True, True, 0)
            remove = Gtk.Button.new_from_icon_name("user-trash-symbolic", Gtk.IconSize.BUTTON)
            remove.set_tooltip_text("Remove this area")
            remove.set_relief(Gtk.ReliefStyle.NONE)
            remove.connect("clicked", lambda _b, reg=r: self.app.remove_region(reg))
            line.pack_end(remove, False, False, 0)
            row.add(line)
            self.listbox.add(row)
            self.rows[r.id] = label
        self.listbox.show_all()

    def refresh_geometry(self):
        for r in self.app.regions:
            label = self.rows.get(r.id)
            if label is not None:
                label.set_text(self._describe(r))

    def set_status(self, text, error=False):
        self.status.set_text(text)
        ctx = self.status.get_style_context()
        if error:
            ctx.add_class("sm-error")
        else:
            ctx.remove_class("sm-error")

    def set_feed_active(self, active):
        self.feed_button.set_label("Stop masked feed" if active else "Start masked feed")
        self.park_check.set_sensitive(not active and self.app.share.can_park())
        ctx = self.feed_button.get_style_context()
        if active:
            ctx.remove_class("suggested-action")
            ctx.add_class("destructive-action")
        else:
            ctx.remove_class("destructive-action")
            ctx.add_class("suggested-action")

    def on_delete(self, *_):
        if self.app.regions or self.app.feed.running:
            dialog = Gtk.MessageDialog(
                transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.NONE, text="Quit Screen Mask?")
            dialog.set_title(APP_NAME)
            dialog.format_secondary_text(
                "All black areas disappear and the masked feed stops. "
                "To keep them and just get this window out of the way, minimise it.")
            dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
            dialog.add_button("Quit", Gtk.ResponseType.OK)
            response = dialog.run()
            dialog.destroy()
            if response != Gtk.ResponseType.OK:
                return True
        self.app.shutdown_all()
        return False


# ----------------------------------------------------------------------------
# Application
# ----------------------------------------------------------------------------

class App(Gtk.Application):
    def __init__(self, args):
        super().__init__(application_id=APP_ID)
        self.args = args
        self.panel = None
        self.share = None
        self.feed = None
        self.overlay = None
        self.menu = None
        self.regions = []
        self.windows = {}
        self.save_timer = 0
        cfg = {} if args.fresh else load_config()
        self.config_loaded = cfg
        self.editing = False
        self.show_outlines = bool(cfg.get("show_outlines", True))
        self.restore_token = "" if args.choose_screen else cfg.get("restore_token", "")
        self.park = cfg.get("park")

    def do_activate(self):
        if self.panel is not None:
            self.panel.present()
            return
        self.feed = Feed(self)
        self.share = ShareWindow(self)
        if self.park is None:
            # Parking relies on the window manager keeping hidden workspaces
            # alive, which compositing desktops (GNOME, KDE) do.
            try:
                wm = self.share.get_screen().get_window_manager_name() or ""
            except AttributeError:
                wm = ""
            self.park = any(n in wm for n in ("GNOME Shell", "Mutter", "KWin"))
        for item in self.config_loaded.get("regions", []):
            try:
                region = Region(item["mode"], item["x"], item["y"], item["w"], item["h"])
            except (KeyError, TypeError, ValueError):
                continue
            self.regions.append(region)
            self.windows[region.id] = RegionWindow(self, region)
        self.panel = ControlPanel(self)
        if not self.share.can_park():
            self.panel.park_check.set_sensitive(False)
            self.panel.park_check.set_active(False)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self._on_signal)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self._on_signal)

    def _on_signal(self):
        self.shutdown_all()
        self.quit()
        return False

    # -- regions ------------------------------------------------------------

    def begin_drawing(self, mode):
        if self.overlay is None:
            self.overlay = DrawOverlay(self, mode)

    def finish_drawing(self, result):
        if self.overlay is not None:
            self.overlay.destroy()
            self.overlay = None
        if result:
            mode, x, y, w, h = result
            region = Region(mode, x, y, w, h)
            self.regions.append(region)
            self.windows[region.id] = RegionWindow(self, region)
            self.regions_changed()

    def remove_region(self, region):
        if region in self.regions:
            self.regions.remove(region)
            win = self.windows.pop(region.id, None)
            if win is not None:
                win.destroy()
            self.regions_changed()
        return False

    def refresh_window(self, region):
        """Bring a region's window in line with its mode and the edit state."""
        old = self.windows.get(region.id)
        if old is not None and old.outline == RegionWindow.wants_outline(self, region):
            old.apply_state()
            return
        # New window first, then remove the old one, so a pitch-black area is
        # never left uncovered in between.
        self.windows[region.id] = RegionWindow(self, region)
        if old is not None:
            old.destroy()

    def set_mode(self, region, mode):
        if mode in (PITCH, ZOOM) and region.mode != mode and region in self.regions:
            region.mode = mode
            self.refresh_window(region)
            # rebuild the list outside the combo box's own signal handler
            GLib.idle_add(self.regions_changed)
        return False

    def set_editing(self, active):
        if active == self.editing:
            return False
        self.editing = active
        if self.panel.edit_switch.get_active() != active:
            self.panel.edit_switch.set_active(active)
        for region in self.regions:
            self.refresh_window(region)
        return False

    def set_outlines(self, active):
        self.show_outlines = active
        for win in self.windows.values():
            win.apply_state()
        self.save_soon()

    def regions_changed(self, geometry_only=False, save=True):
        if geometry_only:
            self.panel.refresh_geometry()
        else:
            self.panel.refresh_rows()
        if self.share is not None:
            self.share.repaint()
        if save:
            self.save_soon()
        return False

    # -- feed ---------------------------------------------------------------

    def toggle_feed(self):
        if self.feed.running or self.feed.starting:
            self.stop_feed()
        else:
            self.feed.start()

    def stop_feed(self):
        self.feed.stop("Masked feed is off.")

    def set_park(self, active):
        self.park = active
        self.save_soon()

    def feed_status(self, text):
        self.panel.set_status(text)

    def feed_started(self):
        self.panel.set_feed_active(True)
        self.panel.set_status("Starting screen capture...")
        self.share.present_feed(self.park)

    def feed_status_running(self):
        if not self.feed.running or self.feed.surface is None:
            return False
        w, h = self.feed.size
        where = ("It is full-size on a spare workspace."
                 if self.share.parked else
                 "It is an ordinary window; viewers get it at the size you make it.")
        self.panel.set_status(
            "Masked feed is running (%d × %d). Share the window "
            "“%s”. %s" % (w, h, SHARE_TITLE, where))
        return False

    def feed_stopped(self, message=None, error=False):
        if self.panel is None:
            return
        self.panel.set_feed_active(False)
        self.panel.set_status(message or "Masked feed is off.", error=error)
        if error and message:
            log(message)
        if self.share is not None:
            self.share.hide()
            self.share.nudge()       # removes the helper window

    # -- persistence --------------------------------------------------------

    def save_soon(self):
        if self.save_timer:
            GLib.source_remove(self.save_timer)
        self.save_timer = GLib.timeout_add(400, self._save_now)

    def _save_now(self):
        self.save_timer = 0
        self.save()
        return False

    def save(self):
        save_config({
            "regions": [r.to_json() for r in self.regions],
            "show_outlines": self.show_outlines,
            "park": self.park,
            "restore_token": self.restore_token,
        })

    def shutdown_all(self):
        if self.save_timer:
            GLib.source_remove(self.save_timer)
            self.save_timer = 0
        self.save()
        if self.feed is not None:
            self.feed.stop()
        for win in list(self.windows.values()):
            win.destroy()
        self.windows = {}
        if self.overlay is not None:
            self.overlay.destroy()
            self.overlay = None
        if self.share is not None:
            if self.share.pump is not None:
                self.share.pump.destroy()
            self.share.destroy()
            self.share = None


def install_launcher():
    """Add Screen Mask to the desktop's application menu."""
    script = os.path.abspath(__file__)
    apps = os.path.join(
        os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
        "applications")
    path = os.path.join(apps, APP_ID + ".desktop")
    os.makedirs(apps, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=%s\n"
            "Comment=Cover areas of the screen with black rectangles\n"
            "Exec=python3 \"%s\"\n"
            "Icon=video-display\n"
            "Terminal=false\n"
            "Categories=Utility;\n"
            "StartupWMClass=screenmask\n" % (APP_NAME, script))
    print("Added %s to the application menu (%s)." % (APP_NAME, path))
    print("It starts %s, so keep that file where it is." % script)


def main():
    parser = argparse.ArgumentParser(
        prog="screenmask", description="Cover areas of the screen with black rectangles.")
    parser.add_argument("--fresh", action="store_true",
                        help="start without the saved areas and settings")
    parser.add_argument("--choose-screen", action="store_true",
                        help="ask again which screen the masked feed captures")
    parser.add_argument("--capture", choices=("auto", "portal", "x11"), default="auto",
                        help="how the masked feed captures the screen (default: auto)")
    parser.add_argument("--fps", type=int, default=20,
                        help="frames per second of the masked feed (default: 20)")
    parser.add_argument("--install-launcher", action="store_true",
                        help="add Screen Mask to the application menu and exit")
    parser.add_argument("--version", action="version", version="%s %s" % (APP_NAME, VERSION))
    args = parser.parse_args()
    if args.install_launcher:
        install_launcher()
        return 0
    GLib.set_prgname("screenmask")
    GLib.set_application_name(APP_NAME)
    Gdk.set_program_class("screenmask")
    args.fps = min(60, max(1, args.fps))
    app = App(args)
    return app.run(sys.argv[:1])


if __name__ == "__main__":
    sys.exit(main())
