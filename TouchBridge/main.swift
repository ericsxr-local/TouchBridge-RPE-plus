import AppKit
import CoreFoundation
import CoreText
import CoreGraphics
import QuartzCore

// MARK: - 触控板读取

typealias ContactCB = @convention(c) (UnsafeMutableRawPointer?, UnsafeMutableRawPointer?, Int32, Double, Int32) -> Int32
typealias CreateListFn = @convention(c) () -> UnsafeMutableRawPointer?
typealias RegFn = @convention(c) (UnsafeMutableRawPointer, ContactCB) -> Int32
typealias StartFn = @convention(c) (UnsafeMutableRawPointer, Int32) -> Int32

struct RawTouch {
    let id: Int32
    let x: Float
    let y: Float
    let size: Float
    let state: Int32
    let ts: Double
}

final class TouchHub {
    static let shared = TouchHub()
    private let lock = NSLock()
    private var buf: [RawTouch] = []
    private(set) var latencyMS: Double = 0
    private(set) var frameCount: UInt64 = 0
    private(set) var lastContactCount: Int = 0
    private var lastFrameAt: Double = 0

    func ingest(_ data: UnsafeMutableRawPointer?, _ n: Int32, _ ts: Double) {
        var arr = [RawTouch]()
        if let d = data, n > 0 {
            arr.reserveCapacity(Int(n))
            for i in 0..<Int(n) {
                let o = i * 96
                arr.append(RawTouch(id: d.load(fromByteOffset: o + 16, as: Int32.self),
                                    x: d.load(fromByteOffset: o + 32, as: Float.self),
                                    y: d.load(fromByteOffset: o + 36, as: Float.self),
                                    // MTTouch is a 96-byte record. +40/+44 are
                                    // velocity; contact size is stored at +48.
                                    size: d.load(fromByteOffset: o + 48, as: Float.self),
                                    state: d.load(fromByteOffset: o + 20, as: Int32.self),
                                    ts: ts))
            }
        }
        lock.lock()
        buf = arr
        frameCount += 1
        lastContactCount = Int(n)
        lastFrameAt = CACurrentMediaTime()
        if n > 0 {
            let lat = (CACurrentMediaTime() - ts) * 1000.0
            if lat >= 0 && lat < 120 { latencyMS = lat }
        }
        lock.unlock()
    }

    func snapshot() -> [RawTouch] {
        lock.lock(); defer { lock.unlock() }
        // MultitouchSupport normally sends a final zero-contact frame. If it
        // misses that frame, never keep a ghost finger forever: stale data
        // previously occupied all slots and made every later tap disappear.
        if lastFrameAt > 0 && CACurrentMediaTime() - lastFrameAt > 0.20 {
            return []
        }
        return buf
    }

    func diagnostics() -> (UInt64, Int, Double) {
        lock.lock(); defer { lock.unlock() }
        return (frameCount, lastContactCount, latencyMS)
    }
}

let gContactCallback: ContactCB = { _, data, n, ts, _ in
    TouchHub.shared.ingest(data, n, ts)
    return 0
}

@discardableResult
func startTrackpad() -> Bool {
    guard let h = dlopen("/System/Library/PrivateFrameworks/MultitouchSupport.framework/MultitouchSupport", RTLD_NOW) else { return false }
    guard let s1 = dlsym(h, "MTDeviceCreateList"),
          let s2 = dlsym(h, "MTRegisterContactFrameCallback"),
          let s3 = dlsym(h, "MTDeviceStart") else { return false }
    let createList = unsafeBitCast(s1, to: CreateListFn.self)
    let register = unsafeBitCast(s2, to: RegFn.self)
    let start = unsafeBitCast(s3, to: StartFn.self)
    guard let ref = createList() else { return false }
    let arr = Unmanaged<CFArray>.fromOpaque(ref).takeUnretainedValue()
    let count = CFArrayGetCount(arr)
    guard count > 0 else { return false }
    for i in 0..<count {
        guard let p = CFArrayGetValueAtIndex(arr, i) else { continue }
        let dev = UnsafeMutableRawPointer(mutating: p)
        _ = register(dev, gContactCallback)
        _ = start(dev, 0)
    }
    return true
}

@discardableResult
func ensureInputMonitoringAccess() -> Bool {
    if CGPreflightListenEventAccess() { return true }
    return CGRequestListenEventAccess()
}

// MARK: - OSC 发送

enum OSCArg {
    case i(Int32)
    case f(Float32)
}

final class OSCSender {
    private var fd: Int32 = -1
    private var target = sockaddr_in()
    private(set) var port: UInt16 = 9000
    private(set) var ready = false
    private var sent = 0

    func open(port: UInt16) -> Bool {
        close()
        self.port = port
        fd = socket(AF_INET, SOCK_DGRAM, 0)
        guard fd >= 0 else { return false }
        target = sockaddr_in()
        target.sin_family = sa_family_t(AF_INET)
        target.sin_port = port.bigEndian
        target.sin_addr.s_addr = inet_addr("127.0.0.1")
        ready = true
        return true
    }

    func close() {
        if fd >= 0 { Darwin.close(fd) }
        fd = -1
        ready = false
    }

    private func pad(_ d: inout Data) {
        let rem = d.count % 4
        if rem != 0 { for _ in 0..<(4 - rem) { d.append(0) } }
    }

    private func str(_ s: String) -> Data {
        var d = Data(s.utf8)
        d.append(0)
        pad(&d)
        return d
    }

    @discardableResult
    func send(_ address: String, _ args: [OSCArg]) -> Bool {
        guard ready, fd >= 0 else { return false }
        var packet = str(address)
        var tags = ","
        for a in args {
            switch a {
            case .i: tags += "i"
            case .f: tags += "f"
            }
        }
        packet.append(str(tags))
        for a in args {
            switch a {
            case .i(let v):
                withUnsafeBytes(of: v.bigEndian) { packet.append(contentsOf: $0) }
            case .f(let v):
                withUnsafeBytes(of: v.bitPattern.bigEndian) { packet.append(contentsOf: $0) }
            }
        }
        let n = packet.withUnsafeBytes { buf -> Int in
            var addrCopy = target
            return withUnsafePointer(to: &addrCopy) { ap -> Int in
                ap.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                    sendto(fd, buf.baseAddress, packet.count, 0, sa, socklen_t(MemoryLayout<sockaddr_in>.size))
                }
            }
        }
        if n > 0 { sent += 1 }
        return n > 0
    }

    var sentCount: Int { sent }
}

// MARK: - 手势引擎

struct Slot {
    var id: Int32
    var start: Double
    var now: Double
    var x: CGFloat
    var y: CGFloat
    var area: CGFloat
    var maxDist: CGFloat = 0
    var startX: CGFloat
    var startY: CGFloat
    var dragging = false
    var holdSent = false
    var phase = "按下"
    var lastMoveSend: Double = 0
    var releasedAt: Double = -1
    var fadeX: CGFloat = 0
    var fadeY: CGFloat = 0
}

final class Engine {
    static let shared = Engine()
    var sender = OSCSender()
    var sending = false
    var slots: [Int: Slot] = [:]
    var lastCount = -1
    var trackpadOK = false
    var messageLog: [String] = []
    var lastHeartbeat: Double = 0

    func zoneIndex(_ x: CGFloat, _ y: CGFloat) -> Int {
        let cx = min(2, max(0, Int(x * 3)))
        let cy = 2 - min(2, max(0, Int(y * 3)))
        return cy * 3 + cx
    }

    func zoneName(_ z: Int) -> String {
        let names = ["左上", "中上", "右上", "左中", "中心", "右中", "左下", "中下", "右下"]
        return names[min(max(z, 0), 8)]
    }

    func freeSlot() -> Int? {
        for s in 1...5 where slots[s] == nil { return s }
        // Released slots are kept briefly only for the fade animation. They
        // must never block a rapid repeated tap; reuse the oldest one.
        if let reusable = slots
            .filter({ $0.value.releasedAt >= 0 })
            .min(by: { $0.value.releasedAt < $1.value.releasedAt })?.key {
            slots.removeValue(forKey: reusable)
            return reusable
        }
        return nil
    }

    func tick() {
        let now = CACurrentMediaTime()
        let snap = TouchHub.shared.snapshot()
        var pressed: [Int32: RawTouch] = [:]
        for t in snap where (t.state == 4 || t.state == 3) {
            pressed[t.id] = t
        }

        // 新手指
        for (id, t) in pressed {
            // macOS can reuse a path ID immediately after lift. A fading
            // visual slot with the same ID is not an active touch.
            if let stale = slots.first(where: { $0.value.id == id && $0.value.releasedAt >= 0 })?.key {
                slots.removeValue(forKey: stale)
            }
            let exists = slots.values.contains { $0.id == id && $0.releasedAt < 0 }
            if exists { continue }
            guard let slot = freeSlot() else { continue }
            let x = CGFloat(t.x)
            let y = CGFloat(t.y)
            let area = CGFloat(t.size)
            slots[slot] = Slot(id: id, start: now, now: now, x: x, y: y, area: area,
                               startX: x, startY: y, lastMoveSend: now)
            if sending {
                let z = zoneIndex(x, y)
                sender.send("/down", [.i(Int32(slot)), .f(Float32(x)), .f(Float32(y)),
                                      .f(Float32(area)), .i(Int32(z))])
                log("down \(slot) → \(zoneName(z))")
            }
        }

        // 更新
        for slot in slots.keys {
            guard var s = slots[slot] else { continue }
            if let t = pressed[s.id] {
                let x = CGFloat(t.x), y = CGFloat(t.y)
                let dist = hypot(x - s.startX, y - s.startY)
                s.maxDist = max(s.maxDist, dist)
                s.x = x; s.y = y; s.now = now; s.area = CGFloat(t.size)
                let dur = now - s.start

                if !s.dragging && dist > 0.02 && dur > 0.12 {
                    s.dragging = true
                    s.phase = "滑动"
                    if sending {
                        sender.send("/drag", [.i(Int32(slot)), .f(Float32(x)), .f(Float32(y))])
                        log("drag \(slot)")
                    }
                }
                if !s.dragging && !s.holdSent && dur > 0.22 {
                    s.holdSent = true
                    s.phase = "长按"
                    if sending {
                        sender.send("/hold", [.i(Int32(slot)), .f(Float32(x)), .f(Float32(y))])
                        log("hold \(slot)")
                    }
                }
                if now - s.lastMoveSend > 0.012 {
                    s.lastMoveSend = now
                    if sending {
                        sender.send("/move", [.i(Int32(slot)), .f(Float32(x)), .f(Float32(y)),
                                              .f(Float32(s.area))])
                    }
                }
                slots[slot] = s
            } else if s.releasedAt < 0 {
                // 抬起
                s.releasedAt = now
                let dur = now - s.start
                let isTap = dur < 0.20 && s.maxDist < 0.02
                if sending {
                    // Always let the configurable Python classifier decide
                    // Tap/Hold/Flick/Drag. Previously Swift used a second set
                    // of hard-coded thresholds, which varied by trackpad size.
                    sender.send("/up", [.i(Int32(slot)), .f(Float32(dur * 1000)),
                                        .f(Float32(s.maxDist))])
                    log("up \(slot) \(Int(dur * 1000))ms")
                }
                s.phase = isTap ? "点击" : "松开"
                s.fadeX = s.x; s.fadeY = s.y
                slots[slot] = s
            }
        }

        // 清理
        for slot in slots.keys {
            if let s = slots[slot], s.releasedAt > 0, now - s.releasedAt > 0.45 {
                slots.removeValue(forKey: slot)
            }
        }

        let c = pressed.count
        if c != lastCount {
            lastCount = c
            if sending { sender.send("/count", [.i(Int32(c))]) }
        }
        // A periodic packet lets the web UI distinguish "UDP is listening"
        // from "the native trackpad collector has exited" even while idle.
        if sending && now - lastHeartbeat >= 1.0 {
            lastHeartbeat = now
            let diag = TouchHub.shared.diagnostics()
            let activeSlots = slots.values.filter { $0.releasedAt < 0 }.count
            sender.send("/heartbeat", [
                .i(Int32(truncatingIfNeeded: diag.0)),
                .i(Int32(diag.1)),
                .i(Int32(activeSlots))
            ])
        }
    }

    func log(_ s: String) {
        messageLog.append(s)
        if messageLog.count > 60 { messageLog.removeFirst() }
    }

    func reset() {
        slots.removeAll()
        messageLog.removeAll()
        lastCount = -1
    }
}

// MARK: - 绘制工具

enum Pal {
    static let bg = NSColor(srgbRed: 0.043, green: 0.043, blue: 0.059, alpha: 1)
    static let card = NSColor(srgbRed: 0.078, green: 0.078, blue: 0.106, alpha: 1)
    static let accent = NSColor(srgbRed: 0.42, green: 0.84, blue: 0.98, alpha: 1)
    static let text = NSColor(white: 1, alpha: 0.92)
    static let dim = NSColor(white: 1, alpha: 0.40)
    static let ok = NSColor(srgbRed: 0.36, green: 0.86, blue: 0.62, alpha: 1)
    static let warn = NSColor(srgbRed: 0.98, green: 0.66, blue: 0.30, alpha: 1)
    static let zoneTint = NSColor(srgbRed: 0.55, green: 0.55, blue: 1.0, alpha: 1)
}

enum Align { case left, center, right }

func drawText(_ ctx: CGContext, _ s: String, x: CGFloat, y: CGFloat, size: CGFloat,
              medium: Bool = false, mono: Bool = false, color: NSColor, align: Align = .left) {
    let name: String
    if mono { name = medium ? "Menlo-Bold" : "Menlo-Regular" }
    else { name = medium ? "PingFangSC-Semibold" : "PingFangSC-Regular" }
    let font = CTFontCreateWithName(name as CFString, size, nil)
    let attr = NSAttributedString(string: s, attributes: [.font: font, .foregroundColor: color])
    let line = CTLineCreateWithAttributedString(attr)
    let w = CGFloat(CTLineGetTypographicBounds(line, nil, nil, nil))
    var px = x
    if align == .center { px -= w / 2 }
    if align == .right { px -= w }
    ctx.saveGState()
    ctx.textMatrix = .identity
    ctx.translateBy(x: px, y: y)
    ctx.scaleBy(x: 1, y: -1)
    ctx.textPosition = .zero
    CTLineDraw(line, ctx)
    ctx.restoreGState()
}

// MARK: - 主视图

final class Panel: NSView {
    var timer: Timer?
    var onStateChange: (() -> Void)?

    override var isFlipped: Bool { true }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        guard window != nil, timer == nil else { return }
        timer = Timer.scheduledTimer(timeInterval: 1.0 / 90.0, target: self,
                                     selector: #selector(tick), userInfo: nil, repeats: true)
        RunLoop.main.add(timer!, forMode: .common)
    }

    @objc func tick() {
        needsDisplay = true
    }

    override func draw(_ dirtyRect: NSRect) {
        guard let ctx = NSGraphicsContext.current?.cgContext else { return }
        let W = bounds.width

        ctx.setFillColor(Pal.bg.cgColor)
        ctx.fill(bounds)

        drawText(ctx, "触控板 → Max", x: 24, y: 30, size: 16, medium: true, color: Pal.text)
        drawText(ctx, "把每根手指的位置 / 轨迹 / 区域 / 手势，变成 OSC 消息发给 Max",
                 x: 24, y: 52, size: 11.5, color: Pal.dim)

        let sending = Engine.shared.sending
        let ok = Engine.shared.trackpadOK
        let dotColor = !ok ? Pal.warn : (sending ? Pal.ok : Pal.dim)
        ctx.setFillColor(dotColor.cgColor)
        ctx.fillEllipse(in: CGRect(x: W - 176, y: 24, width: 9, height: 9))
        var status = !ok ? "读不到触控板" : (sending ? "正在发送" : "已暂停")
        status += String(format: "  →  127.0.0.1:%d", Engine.shared.sender.port)
        drawText(ctx, status, x: W - 24, y: 30, size: 11.5, mono: false, color: Pal.dim, align: .right)

        // 触控板地图
        let padW: CGFloat = 250
        let padH = padW * 22.0 / 30.0
        let padRect = CGRect(x: 24, y: 84, width: padW, height: padH)
        ctx.setFillColor(NSColor(white: 1, alpha: 0.03).cgColor)
        ctx.fill(padRect)
        ctx.setStrokeColor(NSColor(white: 1, alpha: 0.09).cgColor)
        ctx.setLineWidth(1)
        for i in 1..<3 {
            let gx = padRect.minX + padRect.width * CGFloat(i) / 3
            ctx.move(to: CGPoint(x: gx, y: padRect.minY))
            ctx.addLine(to: CGPoint(x: gx, y: padRect.maxY))
            let gy = padRect.minY + padRect.height * CGFloat(i) / 3
            ctx.move(to: CGPoint(x: padRect.minX, y: gy))
            ctx.addLine(to: CGPoint(x: padRect.maxX, y: gy))
        }
        ctx.strokePath()
        ctx.setStrokeColor(NSColor(white: 1, alpha: 0.16).cgColor)
        ctx.stroke(padRect)
        drawText(ctx, "触控板 3×3 区域", x: 24, y: padRect.maxY + 18, size: 10.5, color: Pal.dim)

        let now = CACurrentMediaTime()
        for (slot, s) in Engine.shared.slots {
            let px = padRect.minX + s.x * padRect.width
            let py = padRect.minY + (1 - s.y) * padRect.height
            let alpha: CGFloat = s.releasedAt < 0 ? 1.0 : max(0, 1 - CGFloat(now - s.releasedAt) / 0.45)
            ctx.setFillColor(Pal.accent.withAlphaComponent(0.16 * alpha).cgColor)
            ctx.fillEllipse(in: CGRect(x: px - 18, y: py - 18, width: 36, height: 36))
            ctx.setStrokeColor(Pal.accent.withAlphaComponent(0.9 * alpha).cgColor)
            ctx.setLineWidth(1.6)
            ctx.strokeEllipse(in: CGRect(x: px - 10, y: py - 10, width: 20, height: 20))
            drawText(ctx, "\(slot)", x: px, y: py + 4, size: 10, medium: true,
                     mono: true, color: Pal.text.withAlphaComponent(alpha), align: .center)
        }

        // 每个槽位
        let listX: CGFloat = padRect.maxX + 46
        drawText(ctx, "手指槽位", x: listX, y: 84, size: 10.5, color: Pal.dim)
        for slot in 1...5 {
            let y = 108 + CGFloat(slot - 1) * 44
            let active = Engine.shared.slots[slot]
            let c = active == nil ? NSColor(white: 1, alpha: 0.05) : Pal.card
            let r = CGRect(x: listX, y: y, width: W - listX - 24, height: 36)
            ctx.setFillColor(c.cgColor)
            ctx.beginPath()
            ctx.addPath(CGPath(roundedRect: r, cornerWidth: 8, cornerHeight: 8, transform: nil))
            ctx.fillPath()

            let idxColor = active == nil ? Pal.dim : Pal.accent
            drawText(ctx, "\(slot)", x: r.minX + 14, y: y + 22, size: 12, medium: true, mono: true, color: idxColor)
            if let s = active {
                let z = Engine.shared.zoneIndex(s.x, s.y)
                drawText(ctx, Engine.shared.zoneName(z), x: r.minX + 44, y: y + 22, size: 11.5, color: Pal.text)
                drawText(ctx, s.phase, x: r.minX + 104, y: y + 22, size: 11.5, color: Pal.dim)
                drawText(ctx, String(format: "x %.2f   y %.2f", s.x, s.y),
                         x: r.maxX - 14, y: y + 22, size: 11, mono: true, color: Pal.dim, align: .right)
            } else {
                drawText(ctx, "空闲", x: r.minX + 44, y: y + 22, size: 11.5, color: NSColor(white: 1, alpha: 0.22))
            }
        }

        // 日志
        let logY: CGFloat = 108 + 5 * 44 + 8
        drawText(ctx, "最近消息", x: listX, y: logY + 4, size: 10.5, color: Pal.dim)
        let recent = Engine.shared.messageLog.suffix(3)
        var ly: CGFloat = logY + 22
        for m in recent.reversed() {
            drawText(ctx, m, x: listX, y: ly, size: 10.5, mono: true,
                     color: NSColor(white: 1, alpha: 0.35))
            ly += 15
        }
    }
}

// MARK: - App

final class AppDelegate: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    var startButton: NSButton!
    var portField: NSTextField!
    var engineTimer: DispatchSourceTimer?
    var activityToken: NSObjectProtocol?

    func startEngineLoop() {
        // Gesture processing must not depend on an NSView repaint timer.
        // macOS coalesces/pauses view timers under App Nap when Safari/Chrome
        // covers this window, which previously stopped input after a while.
        activityToken = ProcessInfo.processInfo.beginActivity(
            options: [.userInitiated, .latencyCritical, .automaticTerminationDisabled],
            reason: "Continuous low-latency trackpad capture"
        )
        let timer = DispatchSource.makeTimerSource(queue: .main)
        timer.schedule(deadline: .now(), repeating: .milliseconds(11), leeway: .milliseconds(1))
        timer.setEventHandler {
            Engine.shared.tick()
        }
        timer.resume()
        engineTimer = timer
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.appearance = NSAppearance(named: .darkAqua)

        let rect = NSRect(x: 0, y: 0, width: 660, height: 560)
        window = NSWindow(contentRect: rect,
                          styleMask: [.titled, .closable, .miniaturizable],
                          backing: .buffered, defer: false)
        window.title = "触控板 → Max (OSC)"
        window.backgroundColor = Pal.bg
        window.titlebarAppearsTransparent = true
        window.appearance = NSAppearance(named: .darkAqua)
        window.center()

        let content = NSView(frame: rect)
        content.wantsLayer = true

        let panel = Panel(frame: NSRect(x: 0, y: 96, width: 660, height: 464))
        content.addSubview(panel)

        let bar = NSView(frame: NSRect(x: 0, y: 0, width: 660, height: 96))
        bar.wantsLayer = true
        bar.layer?.backgroundColor = NSColor(srgbRed: 0.06, green: 0.06, blue: 0.08, alpha: 1).cgColor
        content.addSubview(bar)

        startButton = NSButton(title: "暂停发送", target: self, action: #selector(toggleSend))
        startButton.frame = NSRect(x: 24, y: 30, width: 120, height: 34)
        startButton.bezelStyle = .rounded
        startButton.keyEquivalent = " "
        bar.addSubview(startButton)

        let clearButton = NSButton(title: "清空", target: self, action: #selector(clearAll))
        clearButton.frame = NSRect(x: 156, y: 30, width: 76, height: 34)
        clearButton.bezelStyle = .rounded
        bar.addSubview(clearButton)

        let label = NSTextField(labelWithString: "端口")
        label.frame = NSRect(x: 252, y: 37, width: 40, height: 20)
        label.textColor = Pal.dim
        label.font = NSFont.systemFont(ofSize: 12)
        bar.addSubview(label)

        portField = NSTextField(frame: NSRect(x: 292, y: 33, width: 74, height: 26))
        portField.stringValue = "9000"
        portField.font = NSFont.monospacedDigitSystemFont(ofSize: 13, weight: .regular)
        portField.alignment = .center
        bar.addSubview(portField)

        let applyButton = NSButton(title: "应用", target: self, action: #selector(applyPort))
        applyButton.frame = NSRect(x: 372, y: 30, width: 64, height: 34)
        applyButton.bezelStyle = .rounded
        bar.addSubview(applyButton)

        let hint = NSTextField(labelWithString: "先在 Max 里打开 TouchBloom.maxpat，再回到这里点“开始发送”")
        hint.frame = NSRect(x: 24, y: 8, width: 600, height: 18)
        hint.textColor = NSColor(white: 1, alpha: 0.30)
        hint.font = NSFont.systemFont(ofSize: 11)
        bar.addSubview(hint)

        window.contentView = content
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        _ = ensureInputMonitoringAccess()
        Engine.shared.trackpadOK = startTrackpad()
        _ = Engine.shared.sender.open(port: 9000)
        Engine.shared.sending = true
        startEngineLoop()
        buildMenu()
    }

    @objc func toggleSend() {
        Engine.shared.sending.toggle()
        startButton.title = Engine.shared.sending ? "暂停发送" : "开始发送"
    }

    @objc func clearAll() {
        Engine.shared.reset()
    }

    @objc func applyPort() {
        if let p = UInt16(portField.stringValue.trimmingCharacters(in: .whitespaces)), p > 0 {
            _ = Engine.shared.sender.open(port: p)
        }
    }

    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于本版本", action: #selector(showEditionCredits), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        NSApp.mainMenu = main
    }

    @objc func showEditionCredits() {
        let alert = NSAlert()
        alert.messageText = "TouchBridge-RPE · Eric_SXR Edition"
        alert.informativeText = "由 Eric_SXR 整合、修复与维护。\n基于上游项目与社区 MultitouchSupport 方案继续开发，不声明为原创。"
        alert.addButton(withTitle: "知道了")
        alert.runModal()
    }

    // The red close button is an explicit user request to quit capture.
    // Do not hide or resurrect the app behind the user's back.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag {
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        }
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        engineTimer?.cancel()
        engineTimer = nil
        if let token = activityToken {
            ProcessInfo.processInfo.endActivity(token)
            activityToken = nil
        }
    }
}

if CommandLine.arguments.contains("--trackpadtest") {
    // When launched through Finder/LaunchServices stdout is not visible. Keep
    // a deterministic log so remote diagnostics can inspect the GUI-session
    // result as well.
    _ = freopen("/tmp/touchbridge_trackpad_test.log", "w", stdout)
    setbuf(stdout, nil)
    let access = ensureInputMonitoringAccess()
    print("input_monitoring:", access ? "granted" : "not_granted")
    let ok = startTrackpad()
    print("trackpad_start:", ok ? "ok" : "failed")
    guard ok else { exit(2) }
    print("Touch or click the trackpad for 10 seconds...")
    let deadline = Date().addingTimeInterval(10)
    var lastFrames: UInt64 = 0
    while Date() < deadline {
        RunLoop.current.run(until: Date().addingTimeInterval(1))
        let (frames, contacts, latency) = TouchHub.shared.diagnostics()
        print(String(format: "frames=%llu (+%llu) contacts=%d latency=%.2fms",
                     frames, frames - lastFrames, contacts, latency))
        lastFrames = frames
    }
    exit(lastFrames > 0 ? 0 : 3)
}

if CommandLine.arguments.contains("--osctest") {
    let s = OSCSender()
    _ = s.open(port: 9000)
    s.send("/down", [.i(1), .f(0.31), .f(0.62), .f(0.5), .i(6)])
    s.send("/move", [.i(1), .f(0.33), .f(0.60), .f(0.5)])
    s.send("/hold", [.i(1), .f(0.33), .f(0.60)])
    s.send("/drag", [.i(2), .f(0.72), .f(0.40)])
    s.send("/tap", [.i(3), .f(0.50), .f(0.50), .f(78.0)])
    s.send("/up", [.i(1), .f(920.0), .f(0.12)])
    s.send("/count", [.i(0)])
    print("sent:", s.sentCount)
    exit(0)
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
