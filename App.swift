import Cocoa
import WebKit
import EventKit
import Darwin

final class Delegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate {
    let store = EKEventStore()
    let fm = FileManager.default
    let data = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/Shishi")
    var bridge: URL { data.appendingPathComponent("bridge") }
    var window: NSWindow!
    var web: WKWebView!
    var status: NSStatusItem!
    var child: Process?
    var timer: Timer?
    var busy = false
    var snapshotBusy = false
    var lastSnapshot = Date.distantPast
    var nativeItems = [[String:Any]]()
    var lockFD: Int32 = -1
    var ownsInstance = false
    var lastMenuSig = ""
    var calendarID: String { get { UserDefaults.standard.string(forKey:"calendarID") ?? "" } set { UserDefaults.standard.set(newValue,forKey:"calendarID") } }
    var authorized: Bool { EKEventStore.authorizationStatus(for:.reminder) == .fullAccess }
    var selected: EKCalendar? { store.calendar(withIdentifier:calendarID) }

    func applicationDidFinishLaunching(_ note: Notification) {
        try? fm.createDirectory(at:data,withIntermediateDirectories:true,attributes:[.posixPermissions:0o700])
        lockFD = Darwin.open(data.appendingPathComponent("app.lock").path,O_CREAT|O_RDWR,0o600)
        guard lockFD >= 0 && flock(lockFD,LOCK_EX|LOCK_NB) == 0 else { NSApp.terminate(nil); return }
        ownsInstance = true
        for folder in [bridge,bridge.appendingPathComponent("requests"),bridge.appendingPathComponent("responses")] {
            try? fm.createDirectory(at:folder,withIntermediateDirectories:true,attributes:[.posixPermissions:0o700])
        }
        let menu = NSMenu()
        let appMenu = NSMenuItem();menu.addItem(appMenu)
        let submenu = NSMenu();submenu.addItem(withTitle:"关于拾事",action:#selector(about),keyEquivalent:"")
        submenu.addItem(.separator());submenu.addItem(withTitle:"退出拾事",action:#selector(NSApplication.terminate(_:)),keyEquivalent:"q")
        appMenu.submenu = submenu;NSApp.mainMenu = menu
        let edit = NSMenuItem(title:"编辑",action:nil,keyEquivalent:"");let em = NSMenu(title:"编辑")
        for (name,act,key) in [("复制","copy:","c"),("粘贴","paste:","v"),("全选","selectAll:","a")] { em.addItem(withTitle:name,action:Selector(act),keyEquivalent:key) };edit.submenu=em;menu.addItem(edit)
        status=NSStatusBar.system.statusItem(withLength:NSStatusItem.squareLength)
        status.button?.image=NSImage(systemSymbolName:"checklist",accessibilityDescription:"拾事")
        status.button?.toolTip="拾事"
        buildStatusMenu(items: [], pending: 0, review: 0, active: 0)
        window=NSWindow(contentRect:NSRect(x:0,y:0,width:1180,height:800),styleMask:[.titled,.closable,.miniaturizable,.resizable],backing:.buffered,defer:false)
        // 修复：窗口关闭后默认会被释放，点 Dock 图标重开时访问已释放窗口会崩溃。
        window.isReleasedWhenClosed=false
        window.title="拾事 · 从聊天到行动";window.minSize=NSSize(width:900,height:650);window.delegate=self
        if !window.setFrameUsingName("ShishiMain") { window.center() }
        _=window.setFrameAutosaveName("ShishiMain")
        web=WKWebView(frame:window.contentView!.bounds);web.autoresizingMask=[.width,.height];web.navigationDelegate=self
        window.contentView?.addSubview(web)
        startBackend()
        DispatchQueue.main.asyncAfter(deadline:.now()+1) { self.loadPage() }
        timer=Timer.scheduledTimer(withTimeInterval:1,repeats:true) { _ in self.tick() }
        // 后台常驻：启动时不显示主窗口，从菜单栏「打开仪表盘」进入。
        NotificationCenter.default.addObserver(forName:.EKEventStoreChanged,object:nil,queue:.main) { _ in self.lastSnapshot = .distantPast }
        writeStatus()
        // 重新签名安装后系统会重置授权；首次启动主动请求，用户点一次允许即可。
        if EKEventStore.authorizationStatus(for:.reminder) == .notDetermined {
            store.requestFullAccessToReminders { granted,error in
                DispatchQueue.main.async { if granted { self.chooseDefault() };self.writeStatus() }
            }
        }
        if CommandLine.arguments.contains("--selftest") { runSelfTest() }
    }
    func runningServerPID() -> pid_t? {
        let path=data.appendingPathComponent("server.pid")
        guard let text=try? String(contentsOf:path,encoding:.utf8),let pid=pid_t(text.trimmingCharacters(in:.whitespacesAndNewlines)) else { return nil }
        guard pid > 0 else { return nil }
        return kill(pid,0) == 0 ? pid : nil
    }
    func startBackend() {
        // 上次异常退出留下的后台仍在运行时直接复用，避免重复启动。
        if runningServerPID() != nil { return }
        guard let resources=Bundle.main.resourceURL else { return }
        let p=Process();p.executableURL=URL(fileURLWithPath:"/usr/bin/python3");p.arguments=["-u",resources.appendingPathComponent("server.py").path]
        let log=data.appendingPathComponent("app.log")
        if !fm.fileExists(atPath:log.path) { fm.createFile(atPath:log.path,contents:nil,attributes:[.posixPermissions:0o600]) }
        if let f=try? FileHandle(forWritingTo:log) { f.seekToEndOfFile();p.standardOutput=f;p.standardError=f }
        do { try p.run();child=p } catch { }
    }
    func loadPage() { web.load(URLRequest(url:URL(string:"http://127.0.0.1:8766/")!)) }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        DispatchQueue.main.asyncAfter(deadline:.now()+2) { self.loadPage() }
    }
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy)->Void) {
        guard let u=action.request.url else { decisionHandler(.cancel);return }
        decisionHandler(u.host == "127.0.0.1" && u.port == 8766 ? .allow : .cancel)
    }
    @objc func show() { guard window != nil else { return };window.makeKeyAndOrderFront(nil);NSApp.activate(ignoringOtherApps:true) }
    @objc func openDashboard(_ sender: Any?) { show() }
    @objc func openMenuItem(_ sender: NSMenuItem) {
        show()
        let tab = (sender.representedObject as? String) ?? "review"
        web?.evaluateJavaScript("window.__shishiOpenTab&&window.__shishiOpenTab('\(tab)')", completionHandler: nil)
    }
    func buildStatusMenu(items: [[String: Any]], pending: Int, review: Int, active: Int) {
        // 菜单栏只展示待查看项目；已确认待办留在仪表盘。
        let menu = NSMenu()
        let heading = NSMenuItem(title: "待查看", action: nil, keyEquivalent: "")
        heading.isEnabled = false
        menu.addItem(heading)
        let reviewItems = items.filter { ["review", "sync_error"].contains(($0["status"] as? String) ?? "") }
        if reviewItems.isEmpty {
            let empty = NSMenuItem(title: "暂无项目", action: nil, keyEquivalent: "")
            empty.isEnabled = false
            menu.addItem(empty)
        } else {
            for item in reviewItems {
                let raw = ((item["title"] as? String) ?? "未命名事项").replacingOccurrences(of: "^(?:待查看|待办)\\s*[：:·]\\s*", with: "", options: .regularExpression)
                let title = raw.count > 27 ? String(raw.prefix(27)) + "…" : raw
                let entry = NSMenuItem(title: title, action: #selector(openMenuItem(_:)), keyEquivalent: "")
                entry.target = self
                entry.representedObject = "review"
                menu.addItem(entry)
            }
        }
        menu.addItem(.separator())
        let open = NSMenuItem(title: "打开仪表盘", action: #selector(openDashboard(_:)), keyEquivalent: "")
        open.target = self
        menu.addItem(open)
        status.menu = menu
    }
    func updateStatusMenu() {
        guard let data = try? Data(contentsOf: bridge.appendingPathComponent("menu.json")),
              let payload = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return }
        let items = (payload["items"] as? [[String: Any]]) ?? []
        let pending = (payload["pending"] as? NSNumber)?.intValue ?? items.count
        let review = (payload["review"] as? NSNumber)?.intValue ?? 0
        let active = (payload["active"] as? NSNumber)?.intValue ?? 0
        let sig = String(data: (try? JSONSerialization.data(withJSONObject: items, options: [.sortedKeys])) ?? Data(), encoding: .utf8) ?? ""
        if sig == lastMenuSig { return }
        lastMenuSig = sig
        buildStatusMenu(items: items, pending: pending, review: review, active: active)
    }
    @objc func about() { let a=NSAlert();a.messageText="拾事";a.informativeText="从微信聊天中记录你答应的事。\n独立本地应用 · 1.0.0";a.runModal() }
    func applicationShouldHandleReopen(_ sender: NSApplication,hasVisibleWindows flag: Bool)->Bool { if ownsInstance,window != nil { show() };return true }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender:NSApplication)->Bool { false }
    func applicationWillTerminate(_ n:Notification) {
        timer?.invalidate()
        if ownsInstance {
            // 退出应用时一并停止后台，避免无界面进程继续消耗模型额度。
            if child?.isRunning == true { child?.terminate() }
            if let pid=runningServerPID() { kill(pid,SIGTERM) }
        }
        if lockFD>=0 { close(lockFD) }
    }
    func runSelfTest() {
        // 自动验证：页面加载 → 关闭窗口 → 从 Dock 重开，不应崩溃。
        DispatchQueue.main.asyncAfter(deadline:.now()+3.5) {
            let urlOK=(self.web.url?.absoluteString ?? "").contains("127.0.0.1:8766")
            let title=self.web.title ?? ""
            self.window.close()
            DispatchQueue.main.asyncAfter(deadline:.now()+1.0) {
                _=self.applicationShouldHandleReopen(NSApp,hasVisibleWindows:false)
                DispatchQueue.main.asyncAfter(deadline:.now()+1.0) {
                    let result="SHISHI_SELFTEST url=\(urlOK) title=\(title) reopened=\(self.window?.isVisible ?? false)"
                    print(result);fflush(stdout)
                    try? result.write(to:self.data.appendingPathComponent("selftest.log"),atomically:true,encoding:.utf8)
                    NSApp.terminate(nil)
                }
            }
        }
    }
    func write(_ value:[String:Any],to url:URL) { if let d=try? JSONSerialization.data(withJSONObject:value,options:[.sortedKeys]) { try? d.write(to:url,options:.atomic);try? fm.setAttributes([.posixPermissions:0o600],ofItemAtPath:url.path) } }
    func writeStatus() {
        let calendars = authorized ? store.calendars(for:.reminder).filter{$0.allowsContentModifications} : []
        let list:[[String:Any]]=calendars.map { c in ["id":c.calendarIdentifier,"title":c.title,"source":c.source.title,"icloud":c.source.title.lowercased().contains("icloud")] }
        write(["authorized":authorized,"authorization":EKEventStore.authorizationStatus(for:.reminder).rawValue,
               "calendars":list,"calendar_id":selected?.calendarIdentifier ?? "","calendar_name":selected?.title ?? "",
               "source":selected?.source.title ?? "","updated":Date().timeIntervalSince1970,"items":nativeItems],to:bridge.appendingPathComponent("status.json"))
    }
    func chooseDefault() {
        if selected != nil { return }
        let calendars=store.calendars(for:.reminder).filter{$0.allowsContentModifications}
        // 只在明确名为 iCloud 的来源创建专用清单；其他账户交由用户选择。
        guard let source=calendars.first(where:{$0.source.title.lowercased().contains("icloud")})?.source else { return }
        if let found=calendars.first(where:{$0.title == "拾事 · 微信待办" && $0.source.sourceIdentifier == source.sourceIdentifier}) { calendarID=found.calendarIdentifier;return }
        let c=EKCalendar(for:.reminder,eventStore:store);c.title="拾事 · 微信待办";c.source=source;c.cgColor=NSColor.systemTeal.cgColor
        do { try store.saveCalendar(c,commit:true);calendarID=c.calendarIdentifier } catch { }
    }
    func tick() {
        writeStatus()
        updateStatusMenu()
        if authorized && Date().timeIntervalSince(lastSnapshot)>15 && !snapshotBusy {
            snapshotBusy=true;lastSnapshot=Date()
            let lists=store.calendars(for:.reminder)
            store.fetchReminders(matching:store.predicateForReminders(in:lists)) { reminders in
                DispatchQueue.main.async {
                    self.nativeItems=(reminders ?? []).compactMap { r in
                        guard let u=r.url,u.scheme == "shishi",u.host == "item" else { return nil }
                        return ["item_key":u.lastPathComponent,"reminder_id":r.calendarItemIdentifier,"completed":r.isCompleted]
                    }
                    self.snapshotBusy=false;self.writeStatus()
                }
            }
        }
        guard !busy,let files=try? fm.contentsOfDirectory(at:bridge.appendingPathComponent("requests"),includingPropertiesForKeys:nil),let file=files.filter({$0.pathExtension=="json"}).sorted(by:{$0.lastPathComponent<$1.lastPathComponent}).first else { return }
        guard let d=try? Data(contentsOf:file),let request=(try? JSONSerialization.jsonObject(with:d)) as? [String:Any],let id=request["id"] as? String,id+".json" == file.lastPathComponent else { try? fm.removeItem(at:file);return }
        let resultURL=bridge.appendingPathComponent("responses").appendingPathComponent(file.lastPathComponent)
        if fm.fileExists(atPath:resultURL.path) { try? fm.removeItem(at:file);return }
        busy=true
        func finish(_ result:[String:Any]) { self.write(result,to:resultURL);try? self.fm.removeItem(at:file);self.busy=false;self.writeStatus() }
        let op=request["op"] as? String ?? ""
        if op == "authorize" {
            store.requestFullAccessToReminders { granted,error in
                DispatchQueue.main.async {
                    if granted { self.chooseDefault() }
                    finish(granted ? ["ok":true] : ["ok":false,"error":"提醒事项权限未授予，请在系统设置中允许拾事访问提醒事项"])
                }
            };return
        }
        if op == "refresh" { lastSnapshot = .distantPast;finish(["ok":true]);return }
        guard authorized else { finish(["ok":false,"error":"请先授权苹果提醒事项"]);return }
        if op == "choose" {
            guard let id=request["calendar_id"] as? String,let c=store.calendar(withIdentifier:id),c.allowedEntityTypes.contains(.reminder),c.allowsContentModifications else { finish(["ok":false,"error":"清单不可写"]);return }
            calendarID=id;finish(["ok":true]);return
        }
        guard op == "save",let calendar=selected,calendar.allowsContentModifications,let key=request["item_key"] as? String, key.range(of:"^[a-f0-9]{32}$",options:.regularExpression) != nil else { finish(["ok":false,"error":"请先选择可写的提醒事项清单"]);return }
        // URL 标记幂等：即使保存后程序崩溃，也可找回原提醒，避免重建。
        let marker=URL(string:"shishi://item/"+key)!
        store.fetchReminders(matching:store.predicateForReminders(in:store.calendars(for:.reminder))) { reminders in
            DispatchQueue.main.async {
                let existing=(reminders ?? []).first(where:{$0.url == marker})
                let action=request["action"] as? String ?? "create"
                if ["cancel","complete","reschedule","reopen"].contains(action) && existing == nil { finish(["ok":false,"error":"原提醒已被删除或不可访问，未创建替代提醒"]);return }
                // 重试 create 时保持用户在提醒事项中的编辑，不覆盖完成状态。
                if let old=existing,["create","confirm"].contains(action) { finish(["ok":true,"reminder_id":old.calendarItemIdentifier]);return }
                let r=existing ?? EKReminder(eventStore:self.store)
                if existing == nil { r.calendar=calendar;r.url=marker }
                do {
                    if action == "cancel" {
                        // 已由用户在待查看界面批准取消，才执行删除。
                        let rid=r.calendarItemIdentifier;try self.store.remove(r,commit:true);finish(["ok":true,"reminder_id":rid]);return
                    }
                    if action == "complete" { r.isCompleted=true }
                    else if action == "reopen" { r.isCompleted=false }
                    else {
                        r.title=request["title"] as? String ?? "微信待办";r.notes=request["notes"] as? String
                        r.alarms=[];r.dueDateComponents=nil
                        if let due=request["due"] as? String,!due.isEmpty {
                            var cal=Calendar(identifier:.gregorian);cal.timeZone=TimeZone(identifier:"Asia/Shanghai")!
                            if due.count == 10 {
                                let f=DateFormatter();f.calendar=cal;f.timeZone=cal.timeZone;f.dateFormat="yyyy-MM-dd";f.isLenient=false
                                guard let date=f.date(from:due) else { finish(["ok":false,"error":"日期格式错误"]);return }
                                var comps=cal.dateComponents([.year,.month,.day],from:date);comps.calendar=cal;comps.timeZone=cal.timeZone;r.dueDateComponents=comps
                            } else {
                                let f=ISO8601DateFormatter()
                                guard let date=f.date(from:due) else { finish(["ok":false,"error":"提醒时间格式错误"]);return }
                                var comps=cal.dateComponents([.year,.month,.day,.hour,.minute,.second],from:date);comps.calendar=cal;comps.timeZone=cal.timeZone
                                r.dueDateComponents=comps;r.addAlarm(EKAlarm(absoluteDate:date))
                            }
                        }
                    }
                    try self.store.save(r,commit:true);self.lastSnapshot = .distantPast;finish(["ok":true,"reminder_id":r.calendarItemIdentifier])
                } catch { finish(["ok":false,"error":"系统提醒事项写入失败："+error.localizedDescription]) }
            }
        }
    }
}
let app=NSApplication.shared
let delegate=Delegate()
app.setActivationPolicy(.accessory);app.delegate=delegate;app.run()
