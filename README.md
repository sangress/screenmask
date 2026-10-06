# Screen Mask

Screen Mask covers selected parts of your desktop with rectangles. It runs on
X11 and on Wayland through XWayland.

There are two area types:

- **Pitch black** places an opaque black rectangle over the desktop. The covered
  content is hidden from you as well as from anyone viewing your screen.
- **Zoom block** leaves the content visible on your desktop and masks it in a
  separate live feed. In your meeting app, share the window named
  **Screen Mask - share this window** to hide these areas. Do not share the
  entire screen: that will show the original, unmasked content.

## Requirements

- Linux with an X11 display, or a Wayland desktop with XWayland enabled
- Python 3
- GTK 3, PyGObject, and Cairo bindings
- For the masked feed: GStreamer, its base/good plugins, and the PipeWire
  GStreamer plugin when capturing through the Wayland portal

Install the system packages for your distribution:

```sh
# Ubuntu / Debian
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 \
    gir1.2-gstreamer-1.0 gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good gstreamer1.0-pipewire

# Fedora
sudo dnf install python3-gobject gtk3 gstreamer1-plugins-base \
    gstreamer1-plugins-good pipewire-gstreamer

# Arch Linux
sudo pacman -S python-gobject gtk3 gst-plugins-base gst-plugins-good \
    gst-plugin-pipewire
```

## Run

From this directory, start the app with:

```sh
python3 screenmask.py
```

Use **Add pitch-black area** or **Add zoom-block area**, then drag on the
desktop to mark the rectangle. Right-click to cancel drawing. Turn on **Edit
areas** to move a rectangle or resize it by dragging an edge. Use the area list
to change an area's type or remove it.

To use zoom blocks in a meeting, select **Start masked feed**, grant screen
capture permission if prompted, and share the **Screen Mask - share this
window** window in Zoom, Meet, Teams, or another screen-sharing app. The feed
captures at 20 frames per second by default.

## Options

```text
python3 screenmask.py --help
```

- `--fresh`: start without loading saved areas and settings
- `--choose-screen`: ask again which screen the feed captures
- `--capture auto|portal|x11`: choose the capture method; `auto` uses the
  desktop portal on Wayland and X11 capture otherwise
- `--fps N`: set the feed frame rate (clamped to 1–60 FPS; default 20)
- `--install-launcher`: add Screen Mask to the desktop application menu
- `--version`: print the app version

The launcher starts the script from its current location, so keep the script
there after installing the launcher.

## Settings

Areas and preferences are saved to
`~/.config/screenmask/config.json` when the app exits. `--fresh` skips loading
that file for the current run; settings are saved again when the app exits.

On Wayland, Screen Mask uses XWayland for the overlay rectangles and the
desktop's screen-capture portal for the masked feed. The desktop may ask for
screen-capture permission when the feed starts.