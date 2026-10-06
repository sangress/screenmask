//
// Screen Mask for macOS - cover areas of the screen with black rectangles.
//
//   pitch black   A solid black rectangle above everything. Nobody sees what is
//                 underneath, including you.
//
//   zoom block    You keep seeing the content. People watching your shared
//                 screen see a black rectangle. macOS cannot show a window as
//                 black to screen capture only, so this works through a
//                 "masked feed": the app shows a live copy of your screen with
//                 the areas blacked out in its own window, and you share THAT
//                 window in Zoom / Meet / Teams instead of the whole screen.
//
// Build with ./build.sh (needs the Xcode command line tools). macOS 12.3+.
//
// The code deliberately uses no async/await: only delegates, completion
// handlers and DispatchQueue.main, so it builds the same with old and new
// Swift compilers.
//

import Cocoa
import CoreMedia
import CoreVideo
import IOSurface
import QuartzCore
import ScreenCaptureKit

let appName = "Screen Mask"
let shareTitle = "Screen Mask - share this window"
let amber = NSColor(calibratedRed: 0.96, green: 0.65, blue: 0.14, alpha: 1)
let minimumSize: CGFloat = 16

enum Mode: String {
    case pitch
    case zoom

    var label: String { self == .pitch ? "Pitch black" : "Zoom block" }
}

/// One rectangle. `frame` is in AppKit screen coordinates: points, origin at
/// the bottom-left corner of the primary screen, y pointing up.
final class Area {
    var mode: Mode
    var frame: NSRect
    var window: AreaWindow?

    init(mode: Mode, frame: NSRect) {
        self.mode = mode
        self.frame = frame
    }
}

// MARK: - The rectangles on screen

/// Borderless, never takes the keyboard, does not activate the app when clicked.
final class AreaWindow: NSPanel {
    override var canBecomeKey: Bool { return false }
    override var canBecomeMain: Bool { return false }
}

let hitLeft = 1, hitRight = 2, hitTop = 4, hitBottom = 8

final class AreaView: NSView {
    let area: Area
    unowned let app: AppDelegate
    private var dragKind = 0
    private var dragStart = NSPoint.zero
    private var dragFrame = NSRect.zero
    private var dragging = false

    init(area: Area, app: AppDelegate) {
        self.area = area
        self.app = app
        super.init(frame: NSRect(origin: .zero, size: area.frame.size))
        autoresizingMask = [.width, .height]
        addTrackingArea(NSTrackingArea(rect: .zero,
                                       options: [.mouseMoved, .activeAlways, .inVisibleRect],
                                       owner: self, userInfo: nil))
    }

    required init?(coder: NSCoder) {
        fatalError("not used")
    }

    override var isOpaque: Bool { return false }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { return true }

    // -- drawing

    override func draw(_ dirtyRect: NSRect) {
        let box = bounds
        NSColor.clear.setFill()
        box.fill(using: .copy)

        if area.mode == .pitch {
            NSColor.black.setFill()
            box.fill()
            if app.editing {
                let grey = NSColor(calibratedWhite: 0.62, alpha: 1)
                dashedFrame(grey, width: 2, dash: [6, 4], inset: 1)
                drawTag("Pitch black", background: grey)
            }
        } else if app.editing {
            amber.withAlphaComponent(0.28).setFill()
            box.fill()
            dashedFrame(amber, width: 2, dash: [6, 4], inset: 1)
            drawTag("Zoom block - hidden from viewers", background: amber)
        } else {
            // only a thin outline; the window ignores the mouse in this state
            let line: CGFloat = 3
            let path = NSBezierPath(rect: box.insetBy(dx: line / 2, dy: line / 2))
            path.lineWidth = line
            amber.setStroke()
            path.stroke()
            dashedFrame(.black, width: line, dash: [8, 8], inset: line / 2)
        }
    }

    private func dashedFrame(_ color: NSColor, width: CGFloat, dash: [CGFloat], inset: CGFloat) {
        let path = NSBezierPath(rect: bounds.insetBy(dx: inset, dy: inset))
        path.lineWidth = width
        path.setLineDash(dash, count: dash.count, phase: 0)
        color.setStroke()
        path.stroke()
    }

    private func drawTag(_ text: String, background: NSColor) {
        guard bounds.width > 110, bounds.height > 40 else { return }
        let attributes: [NSAttributedString.Key: Any] = [
            .font: NSFont.boldSystemFont(ofSize: 11),
            .foregroundColor: NSColor.black,
        ]
        let string = text as NSString
        let size = string.size(withAttributes: attributes)
        let width = min(size.width + 14, bounds.width - 16)
        let rect = NSRect(x: 8, y: bounds.height - 8 - (size.height + 6),
                          width: width, height: size.height + 6)
        background.setFill()
        rect.fill()
        NSGraphicsContext.saveGraphicsState()
        NSBezierPath(rect: rect).addClip()
        string.draw(at: NSPoint(x: rect.minX + 7, y: rect.minY + 3), withAttributes: attributes)
        NSGraphicsContext.restoreGraphicsState()
    }

    // -- mouse

    private func hit(_ point: NSPoint) -> Int {
        let edge = max(4, min(8, bounds.width / 3, bounds.height / 3))
        var kind = 0
        if point.x < edge { kind |= hitLeft } else if point.x >= bounds.width - edge { kind |= hitRight }
        if point.y < edge { kind |= hitBottom } else if point.y >= bounds.height - edge { kind |= hitTop }
        return kind
    }

    override func mouseMoved(with event: NSEvent) {
        guard app.editing, !dragging else { return }
        let kind = hit(convert(event.locationInWindow, from: nil))
        let horizontal = kind & (hitLeft | hitRight) != 0
        let vertical = kind & (hitTop | hitBottom) != 0
        if horizontal && vertical {
            NSCursor.crosshair.set()
        } else if horizontal {
            NSCursor.resizeLeftRight.set()
        } else if vertical {
            NSCursor.resizeUpDown.set()
        } else {
            NSCursor.openHand.set()
        }
    }

    override func mouseDown(with event: NSEvent) {
        guard app.editing else { return }
        dragKind = hit(convert(event.locationInWindow, from: nil))
        dragStart = NSEvent.mouseLocation
        dragFrame = area.frame
        dragging = true
    }

    override func mouseDragged(with event: NSEvent) {
        guard dragging else { return }
        let now = NSEvent.mouseLocation
        let dx = (now.x - dragStart.x).rounded()
        let dy = (now.y - dragStart.y).rounded()
        var left = dragFrame.minX, right = dragFrame.maxX
        var bottom = dragFrame.minY, top = dragFrame.maxY
        if dragKind == 0 {
            left += dx; right += dx; bottom += dy; top += dy
        } else {
            if dragKind & hitLeft != 0 { left = min(left + dx, right - minimumSize) }
            if dragKind & hitRight != 0 { right = max(right + dx, left + minimumSize) }
            if dragKind & hitBottom != 0 { bottom = min(bottom + dy, top - minimumSize) }
            if dragKind & hitTop != 0 { top = max(top + dy, bottom + minimumSize) }
        }
        area.frame = NSRect(x: left, y: bottom, width: right - left, height: top - bottom)
        window?.setFrame(area.frame, display: true)
        app.areaGeometryChanged()
    }

    override func mouseUp(with event: NSEvent) {
        if dragging {
            dragging = false
            app.save()
        }
    }

    override func rightMouseDown(with event: NSEvent) {
        let menu = NSMenu()
        let other: Mode = area.mode == .pitch ? .zoom : .pitch
        add(menu, "Switch to \(other.label)", #selector(menuSwitch(_:)))
        let edit = add(menu, "Edit Areas (move / resize)", #selector(menuEdit(_:)))
        edit.state = app.editing ? .on : .off
        add(menu, "Remove This Area", #selector(menuRemove(_:)))
        menu.addItem(.separator())
        add(menu, "Open \(appName)", #selector(menuOpen(_:)))
        // Menus open below our window level, so step down while it is showing.
        app.setAreaLevel(raised: false)
        NSMenu.popUpContextMenu(menu, with: event, for: self)
        app.setAreaLevel(raised: true)
    }

    @discardableResult
    private func add(_ menu: NSMenu, _ title: String, _ action: Selector) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
        item.target = self
        menu.addItem(item)
        return item
    }

    // The actions run after the menu has closed, because they may remove this view.
    @objc private func menuSwitch(_ sender: Any?) {
        let area = self.area, app = self.app
        DispatchQueue.main.async { app.setMode(area, area.mode == .pitch ? .zoom : .pitch) }
    }

    @objc private func menuEdit(_ sender: Any?) {
        let app = self.app
        DispatchQueue.main.async { app.setEditing(!app.editing) }
    }

    @objc private func menuRemove(_ sender: Any?) {
        let area = self.area, app = self.app
        DispatchQueue.main.async { app.remove(area) }
    }

    @objc private func menuOpen(_ sender: Any?) {
        let app = self.app
        DispatchQueue.main.async { app.showPanel() }
    }
}

// MARK: - Drawing a new rectangle

final class DrawWindow: NSWindow {
    override var canBecomeKey: Bool { return true }
}

final class DrawView: NSView {
    unowned let app: AppDelegate
    let mode: Mode
    private var from: NSPoint?
    private var to = NSPoint.zero

    init(frame: NSRect, mode: Mode, app: AppDelegate) {
        self.mode = mode
        self.app = app
        super.init(frame: frame)
        autoresizingMask = [.width, .height]
    }

    required init?(coder: NSCoder) {
        fatalError("not used")
    }

    override var acceptsFirstResponder: Bool { return true }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { return true }

    override func resetCursorRects() {
        addCursorRect(bounds, cursor: .crosshair)
    }

    private var selection: NSRect? {
        guard let from = from else { return nil }
        return NSRect(x: min(from.x, to.x), y: min(from.y, to.y),
                      width: abs(to.x - from.x), height: abs(to.y - from.y))
    }

    override func draw(_ dirtyRect: NSRect) {
        NSColor(calibratedWhite: 0, alpha: 0.45).setFill()
        bounds.fill(using: .copy)
        if let rect = selection {
            (mode == .pitch ? NSColor.black : amber.withAlphaComponent(0.30)).setFill()
            rect.fill(using: .copy)
            (mode == .pitch ? NSColor(calibratedWhite: 0.7, alpha: 1) : amber).setStroke()
            let path = NSBezierPath(rect: rect.insetBy(dx: 1, dy: 1))
            path.lineWidth = 2
            path.stroke()
        }
        let hint = "Drag to mark the \(mode.label.lowercased()) area.   Right-click or Esc to cancel."
        let text = hint as NSString
        let attributes: [NSAttributedString.Key: Any] = [
            .font: NSFont.boldSystemFont(ofSize: 18),
            .foregroundColor: NSColor.white,
        ]
        let size = text.size(withAttributes: attributes)
        let origin = NSPoint(x: (bounds.width - size.width) / 2, y: bounds.height - 110)
        NSColor(calibratedWhite: 0, alpha: 0.75).setFill()
        NSRect(x: origin.x - 18, y: origin.y - 10, width: size.width + 36, height: size.height + 20)
            .fill()
        text.draw(at: origin, withAttributes: attributes)
    }

    override func mouseDown(with event: NSEvent) {
        from = convert(event.locationInWindow, from: nil)
        to = from!
        needsDisplay = true
    }

    override func mouseDragged(with event: NSEvent) {
        guard from != nil else { return }
        to = convert(event.locationInWindow, from: nil)
        needsDisplay = true
    }

    override func mouseUp(with event: NSEvent) {
        guard from != nil else { return }
        to = convert(event.locationInWindow, from: nil)
        let rect = selection ?? .zero
        from = nil
        if rect.width >= minimumSize, rect.height >= minimumSize, let window = window {
            let global = rect.offsetBy(dx: window.frame.minX, dy: window.frame.minY)
            finish(global)
        } else {
            needsDisplay = true     // a stray click: keep waiting
        }
    }

    override func rightMouseDown(with event: NSEvent) {
        finish(nil)
    }

    override func keyDown(with event: NSEvent) {
        if event.keyCode == 53 {    // Esc
            finish(nil)
        }
    }

    /// Finishing closes this view's window, so it happens after the current
    /// event has been handled.
    private func finish(_ frame: NSRect?) {
        let app = self.app, mode = self.mode
        DispatchQueue.main.async { app.finishDrawing(mode: mode, frame: frame) }
    }
}

// MARK: - Screen capture

/// Captures one display with ScreenCaptureKit, leaving out our own share
/// window so the copy never shows itself. Callbacks arrive on the main queue.
final class Feed: NSObject, SCStreamOutput, SCStreamDelegate {
    var onStarted: (() -> Void)?
    var onFrame: ((IOSurface) -> Void)?
    var onStopped: ((String?) -> Void)?

    private var activeStream: SCStream?
    private let queue = DispatchQueue(label: "screenmask.frames")
    private(set) var running = false
    private(set) var starting = false

    func start(displayID: CGDirectDisplayID, scale: CGFloat, excludedWindow: CGWindowID) {
        guard !running, !starting else { return }
        starting = true
        SCShareableContent.getExcludingDesktopWindows(false, onScreenWindowsOnly: false) { content, error in
            DispatchQueue.main.async {
                self.begin(content: content, error: error, displayID: displayID, scale: scale,
                           excludedWindow: excludedWindow)
            }
        }
    }

    private func begin(content: SCShareableContent?, error: Error?, displayID: CGDirectDisplayID,
                       scale: CGFloat, excludedWindow: CGWindowID) {
        guard starting else { return }     // stopped in the meantime
        guard let content = content else {
            let reason = error?.localizedDescription ?? "no details"
            fail("macOS did not allow screen capture. Turn on Screen Mask under System Settings > "
                + "Privacy & Security > Screen & System Audio Recording, then quit and reopen "
                + "Screen Mask. (\(reason))")
            return
        }
        guard let display = content.displays.first(where: { $0.displayID == displayID })
                ?? content.displays.first else {
            fail("No screen to capture was found.")
            return
        }
        let ours = content.windows.filter { $0.windowID == excludedWindow }
        let filter = SCContentFilter(display: display, excludingWindows: ours)
        let config = SCStreamConfiguration()
        config.width = Int((CGFloat(display.width) * scale).rounded())
        config.height = Int((CGFloat(display.height) * scale).rounded())
        config.minimumFrameInterval = CMTime(value: 1, timescale: 20)
        config.pixelFormat = kCVPixelFormatType_32BGRA
        config.showsCursor = true
        config.queueDepth = 5

        let stream = SCStream(filter: filter, configuration: config, delegate: self)
        do {
            try stream.addStreamOutput(self, type: .screen, sampleHandlerQueue: queue)
        } catch {
            fail("Could not set up screen capture: \(error.localizedDescription)")
            return
        }
        activeStream = stream
        stream.startCapture { error in
            DispatchQueue.main.async {
                guard self.activeStream === stream else { return }
                if let error = error {
                    self.activeStream = nil
                    self.fail("Screen capture did not start: \(error.localizedDescription)")
                } else {
                    self.starting = false
                    self.running = true
                    self.onStarted?()
                }
            }
        }
    }

    private func fail(_ message: String) {
        starting = false
        running = false
        onStopped?(message)
    }

    func stop() {
        starting = false
        running = false
        if let stream = activeStream {
            activeStream = nil
            stream.stopCapture { _ in }
        }
    }

    // SCStreamOutput - called on the frames queue
    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
                of type: SCStreamOutputType) {
        guard type == .screen, sampleBuffer.isValid else { return }
        // Frames without a new picture (an unchanged screen) are skipped; the
        // last picture stays up.
        guard let attachmentsArray = CMSampleBufferGetSampleAttachmentsArray(
                sampleBuffer, createIfNecessary: false) as? [[SCStreamFrameInfo: Any]],
              let attachments = attachmentsArray.first,
              let statusRawValue = attachments[SCStreamFrameInfo.status] as? Int,
              let status = SCFrameStatus(rawValue: statusRawValue),
              status == .complete else { return }
        guard let pixelBuffer = sampleBuffer.imageBuffer,
              let surfaceRef = CVPixelBufferGetIOSurface(pixelBuffer)?.takeUnretainedValue()
        else { return }
        let surface = unsafeBitCast(surfaceRef, to: IOSurface.self)
        DispatchQueue.main.async {
            guard self.activeStream === stream, self.running else { return }
            self.onFrame?(surface)
        }
    }

    // SCStreamDelegate
    func stream(_ stream: SCStream, didStopWithError error: Error) {
        DispatchQueue.main.async {
            guard self.activeStream === stream else { return }
            self.activeStream = nil
            self.starting = false
            self.running = false
            self.onStopped?("Screen capture stopped: \(error.localizedDescription)")
        }
    }
}

/// The content of the share window: the screen picture with black rectangles
/// on top. The rectangles are layers above the picture, so no frame can show
/// without them, and they never animate.
final class FeedView: NSView {
    private let root = CALayer()
    private let picture = CALayer()
    private var maskLayers: [CALayer] = []
    private var maskFrames: [NSRect] = []        // screen coordinates
    private var screenFrame = NSRect(x: 0, y: 0, width: 16, height: 9)
    private let still: [String: CAAction] = [
        "contents": NSNull(), "bounds": NSNull(), "position": NSNull(), "frame": NSNull(),
        "hidden": NSNull(), "sublayers": NSNull(), "opacity": NSNull(),
    ]

    override init(frame: NSRect) {
        super.init(frame: frame)
        root.backgroundColor = NSColor.black.cgColor
        root.actions = still
        picture.contentsGravity = .resize
        picture.masksToBounds = true
        picture.backgroundColor = NSColor.black.cgColor
        picture.actions = still
        root.addSublayer(picture)
        // layer-hosting view: set the layer first, then wantsLayer
        layer = root
        wantsLayer = true
        autoresizingMask = [.width, .height]
    }

    required init?(coder: NSCoder) {
        fatalError("not used")
    }

    override func setFrameSize(_ newSize: NSSize) {
        super.setFrameSize(newSize)
        arrange()
    }

    func show(_ surface: IOSurface) {
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        picture.contents = surface
        CATransaction.commit()
    }

    func clear() {
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        picture.contents = nil
        CATransaction.commit()
    }

    func setMasks(_ frames: [NSRect], screenFrame: NSRect) {
        maskFrames = frames
        if screenFrame.width > 0, screenFrame.height > 0 {
            self.screenFrame = screenFrame
        }
        arrange()
    }

    private func arrange() {
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        // fit the screen's shape into the view
        let scale = min(bounds.width / screenFrame.width, bounds.height / screenFrame.height)
        let size = NSSize(width: screenFrame.width * scale, height: screenFrame.height * scale)
        picture.frame = NSRect(x: (bounds.width - size.width) / 2,
                               y: (bounds.height - size.height) / 2,
                               width: size.width, height: size.height)
        while maskLayers.count < maskFrames.count {
            let mask = CALayer()
            mask.backgroundColor = NSColor.black.cgColor
            mask.actions = still
            picture.addSublayer(mask)
            maskLayers.append(mask)
        }
        while maskLayers.count > maskFrames.count {
            maskLayers.removeLast().removeFromSuperlayer()
        }
        // Layer and screen coordinates both have y pointing up, so the mapping
        // is a plain scale. Each rectangle is grown a little and snapped
        // outward to whole points so scaling cannot leak an edge.
        let pad: CGFloat = 2
        for (mask, frame) in zip(maskLayers, maskFrames) {
            let x0 = ((frame.minX - screenFrame.minX) * scale - pad).rounded(.down)
            let y0 = ((frame.minY - screenFrame.minY) * scale - pad).rounded(.down)
            let x1 = ((frame.maxX - screenFrame.minX) * scale + pad).rounded(.up)
            let y1 = ((frame.maxY - screenFrame.minY) * scale + pad).rounded(.up)
            mask.frame = NSRect(x: x0, y: y0, width: x1 - x0, height: y1 - y0)
        }
        CATransaction.commit()
    }
}

// MARK: - The app

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    var areas: [Area] = []
    var editing = false
    var showOutlines = true

    private var panel: NSWindow!
    private var stack: NSStackView!
    private var rows: NSStackView!
    private var rowLabels: [NSTextField] = []
    private var editBox: NSButton!
    private var outlineBox: NSButton!
    private var screenPopup: NSPopUpButton!
    private var feedButton: NSButton!
    private var showShareButton: NSButton!
    private var status: NSTextField!

    private var drawWindows: [DrawWindow] = []
    private let feed = Feed()
    private var shareWindow: NSWindow!
    private var feedView: FeedView!
    private var capturedScreenFrame = NSRect.zero
    private var activity: NSObjectProtocol?
    private var sharePlaced = false
    private var quitting = false

    // -- launch

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        buildShareWindow()
        loadSaved()
        buildPanel()
        for area in areas { refresh(area) }
        rebuildRows()

        feed.onStarted = { [weak self] in self?.feedStarted() }
        feed.onFrame = { [weak self] surface in self?.feedView.show(surface) }
        feed.onStopped = { [weak self] message in self?.feedStopped(message, isError: true) }

        showPanel()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return false
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showPanel()
        return true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if !areas.isEmpty || feed.running {
            let alert = NSAlert()
            alert.messageText = "Quit Screen Mask?"
            alert.informativeText = "All black areas disappear and the masked feed stops. "
                + "To keep them and just get the Screen Mask window out of the way, minimise it."
            alert.addButton(withTitle: "Quit")
            alert.addButton(withTitle: "Cancel")
            if alert.runModal() != .alertFirstButtonReturn {
                return .terminateCancel
            }
        }
        quitting = true
        save()
        feed.stop()
        return .terminateNow
    }

    private func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Quit \(appName)", action: #selector(NSApplication.terminate(_:)),
                        keyEquivalent: "q")
        appItem.submenu = appMenu
        NSApp.mainMenu = main
    }

    func showPanel() {
        if panel.isMiniaturized { panel.deminiaturize(nil) }
        NSApp.activate(ignoringOtherApps: true)
        panel.makeKeyAndOrderFront(nil)
    }

    // -- saved state

    func save() {
        let list: [[String: Any]] = areas.map {
            ["mode": $0.mode.rawValue, "x": Double($0.frame.minX), "y": Double($0.frame.minY),
             "w": Double($0.frame.width), "h": Double($0.frame.height)]
        }
        let defaults = UserDefaults.standard
        defaults.set(list, forKey: "areas")
        defaults.set(showOutlines, forKey: "showOutlines")
    }

    private func loadSaved() {
        let defaults = UserDefaults.standard
        if defaults.object(forKey: "showOutlines") != nil {
            showOutlines = defaults.bool(forKey: "showOutlines")
        }
        guard let list = defaults.array(forKey: "areas") as? [[String: Any]] else { return }
        for item in list {
            guard let raw = item["mode"] as? String, let mode = Mode(rawValue: raw),
                  let x = item["x"] as? Double, let y = item["y"] as? Double,
                  let w = item["w"] as? Double, let h = item["h"] as? Double else { continue }
            areas.append(Area(mode: mode, frame: NSRect(x: x, y: y, width: max(w, Double(minimumSize)),
                                                        height: max(h, Double(minimumSize)))))
        }
    }

    // -- areas

    private var raisedLevel: NSWindow.Level { return .screenSaver }

    /// Bring an area's window in line with its mode and the edit state.
    private func refresh(_ area: Area) {
        let window: AreaWindow
        if let existing = area.window {
            window = existing
        } else {
            window = AreaWindow(contentRect: area.frame, styleMask: [.borderless, .nonactivatingPanel],
                                backing: .buffered, defer: false)
            window.isOpaque = false
            window.backgroundColor = .clear
            window.hasShadow = false
            window.level = raisedLevel
            window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary,
                                         .ignoresCycle]
            window.hidesOnDeactivate = false
            window.canHide = false
            window.isReleasedWhenClosed = false
            window.isMovable = false
            window.contentView = AreaView(area: area, app: self)
            area.window = window
        }
        let outlineOnly = area.mode == .zoom && !editing
        window.ignoresMouseEvents = outlineOnly
        window.setFrame(area.frame, display: false)
        if outlineOnly && !showOutlines {
            window.orderOut(nil)
        } else {
            window.contentView?.needsDisplay = true
            window.orderFrontRegardless()
        }
    }

    func setAreaLevel(raised: Bool) {
        let lowered = NSWindow.Level(rawValue: NSWindow.Level.popUpMenu.rawValue - 1)
        for area in areas { area.window?.level = raised ? raisedLevel : lowered }
    }

    private func areasChanged() {
        rebuildRows()
        updateMasks()
        save()
    }

    func areaGeometryChanged() {
        for (label, area) in zip(rowLabels, areas) { label.stringValue = describe(area) }
        updateMasks()
    }

    func setMode(_ area: Area, _ mode: Mode) {
        guard areas.contains(where: { $0 === area }), area.mode != mode else { return }
        area.mode = mode
        refresh(area)
        areasChanged()
    }

    func remove(_ area: Area) {
        guard let index = areas.firstIndex(where: { $0 === area }) else { return }
        areas.remove(at: index)
        area.window?.orderOut(nil)
        area.window?.close()
        area.window = nil
        areasChanged()
    }

    func setEditing(_ on: Bool) {
        editing = on
        editBox.state = on ? .on : .off
        for area in areas { refresh(area) }
    }

    private func updateMasks() {
        feedView.setMasks(areas.map { $0.frame }, screenFrame: capturedScreenFrame)
    }

    // -- drawing a new area

    @objc private func addPitch(_ sender: Any?) { beginDrawing(.pitch) }
    @objc private func addZoom(_ sender: Any?) { beginDrawing(.zoom) }

    private func beginDrawing(_ mode: Mode) {
        guard drawWindows.isEmpty else { return }
        for screen in NSScreen.screens {
            let window = DrawWindow(contentRect: screen.frame, styleMask: .borderless,
                                    backing: .buffered, defer: false)
            window.isOpaque = false
            window.backgroundColor = .clear
            window.hasShadow = false
            window.level = NSWindow.Level(rawValue: NSWindow.Level.screenSaver.rawValue + 1)
            window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
            window.isReleasedWhenClosed = false
            let view = DrawView(frame: NSRect(origin: .zero, size: screen.frame.size), mode: mode,
                                app: self)
            window.contentView = view
            window.setFrame(screen.frame, display: true)
            window.makeKeyAndOrderFront(nil)
            window.makeFirstResponder(view)
            drawWindows.append(window)
        }
        NSApp.activate(ignoringOtherApps: true)
    }

    func finishDrawing(mode: Mode, frame: NSRect?) {
        for window in drawWindows {
            window.orderOut(nil)
            window.close()
        }
        drawWindows = []
        if let frame = frame {
            let area = Area(mode: mode, frame: frame.integral)
            areas.append(area)
            refresh(area)
            areasChanged()
        }
    }

    // -- the masked feed

    private func buildShareWindow() {
        shareWindow = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 720),
                               styleMask: [.titled, .closable, .resizable],
                               backing: .buffered, defer: false)
        shareWindow.title = shareTitle
        shareWindow.isReleasedWhenClosed = false
        shareWindow.canHide = false
        shareWindow.delegate = self
        shareWindow.backgroundColor = .black
        shareWindow.contentMinSize = NSSize(width: 320, height: 180)
        feedView = FeedView(frame: NSRect(x: 0, y: 0, width: 1280, height: 720))
        shareWindow.contentView = feedView
    }

    private func chosenScreen() -> NSScreen? {
        let screens = NSScreen.screens
        let index = screenPopup.indexOfSelectedItem
        if index >= 0, index < screens.count { return screens[index] }
        return screens.first
    }

    @objc private func toggleFeed(_ sender: Any?) {
        if feed.running || feed.starting {
            feed.stop()
            feedStopped("Masked feed is off.", isError: false)
            return
        }
        guard let screen = chosenScreen() else { return }
        if !CGPreflightScreenCaptureAccess() {
            _ = CGRequestScreenCaptureAccess()  // shows the system prompt the first time
        }
        capturedScreenFrame = screen.frame
        updateMasks()
        if !sharePlaced {
            // about two thirds of the screen, with the screen's shape
            let visible = screen.visibleFrame
            let width = (visible.width * 0.66).rounded()
            let height = (width * screen.frame.height / screen.frame.width).rounded()
            shareWindow.setContentSize(NSSize(width: width, height: height))
            shareWindow.setFrameOrigin(NSPoint(x: visible.midX - width / 2,
                                               y: visible.midY - height / 2))
            sharePlaced = true
        }
        shareWindow.orderBack(nil)              // on screen, behind the other windows
        let number = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber
        let displayID = number?.uint32Value ?? CGMainDisplayID()
        setStatus("Starting screen capture...", isError: false)
        feedButton.title = "Stop Masked Feed"
        screenPopup.isEnabled = false
        let windowNumber = shareWindow.windowNumber
        feed.start(displayID: displayID, scale: screen.backingScaleFactor,
                   excludedWindow: windowNumber > 0 ? CGWindowID(windowNumber) : 0)
    }

    private func feedStarted() {
        // keep delivering frames while the app is in the background
        activity = ProcessInfo.processInfo.beginActivity(
            options: [.userInitiated, .latencyCritical], reason: "Masked screen feed")
        showShareButton.isEnabled = true
        setStatus("Masked feed is running. In Zoom, Meet or Teams share the window "
            + "\u{201C}\(shareTitle)\u{201D}. Viewers get it at the size you make it. It can sit "
            + "behind other windows.", isError: false)
    }

    private func feedStopped(_ message: String?, isError: Bool) {
        if quitting { return }
        if let activity = activity {
            ProcessInfo.processInfo.endActivity(activity)
            self.activity = nil
        }
        feedView.clear()                        // the share window goes black at once
        shareWindow.orderOut(nil)
        feedButton.title = "Start Masked Feed"
        screenPopup.isEnabled = true
        showShareButton.isEnabled = false
        setStatus(message ?? "Masked feed is off.", isError: isError)
    }

    @objc private func showShare(_ sender: Any?) {
        guard feed.running else { return }
        shareWindow.makeKeyAndOrderFront(nil)
    }

    private func setStatus(_ text: String, isError: Bool) {
        status.stringValue = text
        status.textColor = isError ? .systemRed : .labelColor
        fitPanel()
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if sender === shareWindow {
            if feed.running || feed.starting {
                feed.stop()
                feedStopped("Masked feed is off.", isError: false)
            } else {
                shareWindow.orderOut(nil)
            }
        } else if sender === panel {
            NSApp.terminate(nil)                // asks first when areas are in use
        }
        return false
    }

    // -- the control panel

    private func heading(_ text: String) -> NSTextField {
        let label = NSTextField(labelWithString: text)
        label.font = .boldSystemFont(ofSize: NSFont.systemFontSize)
        return label
    }

    private func note(_ text: String) -> NSTextField {
        let label = NSTextField(wrappingLabelWithString: text)
        label.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        label.textColor = .secondaryLabelColor
        label.preferredMaxLayoutWidth = 420
        label.isSelectable = false
        return label
    }

    private func buildPanel() {
        let addPitchButton = NSButton(title: "Add Pitch-Black Area", target: self,
                                      action: #selector(addPitch(_:)))
        let addZoomButton = NSButton(title: "Add Zoom-Block Area", target: self,
                                     action: #selector(addZoom(_:)))
        let addRow = NSStackView(views: [addPitchButton, addZoomButton])
        addRow.orientation = .horizontal
        addRow.distribution = .fillEqually
        addRow.spacing = 8

        let rows = NSStackView()
        rows.orientation = .vertical
        rows.alignment = .leading
        rows.spacing = 6
        self.rows = rows

        let editBox = NSButton(
            checkboxWithTitle: "Edit areas (drag to move, drag an edge to resize)",
            target: self, action: #selector(editToggled(_:)))
        self.editBox = editBox
        let outlineBox = NSButton(checkboxWithTitle: "Show an outline around zoom-block areas",
                                  target: self, action: #selector(outlineToggled(_:)))
        outlineBox.state = showOutlines ? .on : .off
        self.outlineBox = outlineBox

        let line = NSBox()
        line.boxType = .separator

        let screenPopup = NSPopUpButton(frame: .zero, pullsDown: false)
        self.screenPopup = screenPopup
        for (index, screen) in NSScreen.screens.enumerated() {
            let size = "\(Int(screen.frame.width)) \u{00D7} \(Int(screen.frame.height))"
            screenPopup.addItem(withTitle: "\(index + 1): \(screen.localizedName) (\(size))")
        }
        let screenLabel = NSTextField(labelWithString: "Screen to copy:")
        let screenRow = NSStackView(views: [screenLabel, screenPopup])
        screenRow.orientation = .horizontal
        screenRow.spacing = 8
        screenRow.isHidden = NSScreen.screens.count < 2

        let feedButton = NSButton(title: "Start Masked Feed", target: self,
                                  action: #selector(toggleFeed(_:)))
        self.feedButton = feedButton
        let showShareButton = NSButton(title: "Show Share Window", target: self,
                                       action: #selector(showShare(_:)))
        showShareButton.isEnabled = false
        self.showShareButton = showShareButton
        let feedRow = NSStackView(views: [feedButton, showShareButton])
        feedRow.orientation = .horizontal
        feedRow.spacing = 8

        let status = NSTextField(wrappingLabelWithString: "Masked feed is off.")
        status.preferredMaxLayoutWidth = 420
        status.isSelectable = true
        self.status = status

        let areasNote = note("Pitch black hides the area from everyone, including you. Zoom block "
            + "keeps it visible to you and hides it only in the masked feed.")
        let sharingNote = note("Start the masked feed, then in Zoom, Meet or Teams share the "
            + "window named \u{201C}\(shareTitle)\u{201D}. Do not share the entire screen: that "
            + "shows zoom-block areas uncovered.")
        let views: [NSView] = [
            heading("Areas"), addRow, areasNote, rows, editBox, outlineBox, line,
            heading("Screen sharing"), sharingNote, screenRow, feedRow, status,
        ]
        let stack = NSStackView(views: views)
        self.stack = stack
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 10
        stack.edgeInsets = NSEdgeInsets(top: 16, left: 16, bottom: 16, right: 16)
        stack.translatesAutoresizingMaskIntoConstraints = false
        addRow.widthAnchor.constraint(equalToConstant: 420).isActive = true
        line.widthAnchor.constraint(equalToConstant: 420).isActive = true
        rows.widthAnchor.constraint(equalToConstant: 420).isActive = true

        let panel = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 452, height: 400),
                             styleMask: [.titled, .closable, .miniaturizable],
                             backing: .buffered, defer: false)
        panel.title = appName
        panel.isReleasedWhenClosed = false
        panel.delegate = self
        self.panel = panel
        let content = NSView(frame: NSRect(x: 0, y: 0, width: 452, height: 400))
        content.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            stack.topAnchor.constraint(equalTo: content.topAnchor),
            stack.widthAnchor.constraint(equalToConstant: 452),
        ])
        panel.contentView = content
        fitPanel()
        panel.center()
    }

    /// Resize the panel to its content, keeping the title bar where it is.
    private func fitPanel() {
        guard let panel = panel, let stack = stack else { return }
        stack.layoutSubtreeIfNeeded()
        let height = max(200, stack.fittingSize.height)
        let top = panel.frame.maxY
        panel.setContentSize(NSSize(width: 452, height: height))
        panel.setFrameTopLeftPoint(NSPoint(x: panel.frame.minX, y: top))
    }

    private func describe(_ area: Area) -> String {
        let f = area.frame
        return "\(Int(f.width)) \u{00D7} \(Int(f.height)) at \(Int(f.minX)), \(Int(f.minY))"
    }

    private func rebuildRows() {
        for view in rows.arrangedSubviews {
            rows.removeArrangedSubview(view)
            view.removeFromSuperview()
        }
        rowLabels = []
        if areas.isEmpty {
            let empty = NSTextField(labelWithString: "No areas yet")
            empty.textColor = .secondaryLabelColor
            rows.addArrangedSubview(empty)
        }
        for (index, area) in areas.enumerated() {
            let chip = NSView(frame: NSRect(x: 0, y: 0, width: 14, height: 14))
            chip.wantsLayer = true
            chip.layer?.backgroundColor = (area.mode == .pitch ? NSColor.black : amber).cgColor
            chip.layer?.cornerRadius = 3
            chip.layer?.borderWidth = 1
            chip.layer?.borderColor = NSColor.gray.cgColor
            chip.widthAnchor.constraint(equalToConstant: 14).isActive = true
            chip.heightAnchor.constraint(equalToConstant: 14).isActive = true

            let popup = NSPopUpButton(frame: .zero, pullsDown: false)
            popup.addItems(withTitles: [Mode.pitch.label, Mode.zoom.label])
            popup.selectItem(at: area.mode == .pitch ? 0 : 1)
            popup.tag = index
            popup.target = self
            popup.action = #selector(rowModeChanged(_:))

            let label = NSTextField(labelWithString: describe(area))
            label.textColor = .secondaryLabelColor
            label.setContentHuggingPriority(.defaultLow, for: .horizontal)
            rowLabels.append(label)

            let remove = NSButton(title: "Remove", target: self, action: #selector(rowRemove(_:)))
            remove.tag = index
            remove.controlSize = .small
            remove.bezelStyle = .rounded

            let row = NSStackView(views: [chip, popup, label, remove])
            row.orientation = .horizontal
            row.spacing = 8
            rows.addArrangedSubview(row)
            row.widthAnchor.constraint(equalToConstant: 420).isActive = true
        }
        fitPanel()
    }

    @objc private func rowModeChanged(_ sender: NSPopUpButton) {
        let index = sender.tag
        guard index >= 0, index < areas.count else { return }
        let area = areas[index]
        let mode: Mode = sender.indexOfSelectedItem == 0 ? .pitch : .zoom
        DispatchQueue.main.async { self.setMode(area, mode) }   // after the pop-up has closed
    }

    @objc private func rowRemove(_ sender: NSButton) {
        let index = sender.tag
        guard index >= 0, index < areas.count else { return }
        let area = areas[index]
        DispatchQueue.main.async { self.remove(area) }
    }

    @objc private func editToggled(_ sender: NSButton) {
        setEditing(sender.state == .on)
    }

    @objc private func outlineToggled(_ sender: NSButton) {
        showOutlines = sender.state == .on
        for area in areas { refresh(area) }
        save()
    }
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()
