// calendar-events.swift
// 只读读取 macOS Apple Calendar（EventKit）的 CLI 工具。
// stdout: 按开始时间升序的 JSON 事件数组（纯 JSON，无其他输出）。
// stderr: 所有错误与警告。退出码: 0 成功 / 1 运行失败 / 2 参数错误。
// 不创建、修改、删除任何事件；不使用网络；不上传任何数据。

import EventKit
import Foundation

let usage = """
Usage:
  calendar-events [--days N] [--from START]
  calendar-events --from START --to END
  calendar-events --today
  calendar-events --date YYYY-MM-DD
  calendar-events --authorization-status
  calendar-events --request-access

读取 Apple Calendar 所有可访问 calendar 中指定范围的事件，
以 JSON 数组输出到 stdout。只读，绝不修改日历。

Options:
  --days N       从查询起点向后读取 N 天（默认 7）
  --from VALUE   查询起点：now、today、tomorrow、日期、本地时间或 ISO 8601
  --to VALUE     查询终点（不含）；与 --days 互斥
  --today        查询本地今天 00:00 至明天 00:00
  --date DATE    查询指定本地日历日，格式 YYYY-MM-DD
  --authorization-status
                  只报告权限状态，不弹窗、不读取事件
  --request-access
                  在前台明确请求读取权限，不读取事件
  -h, --help     显示本帮助
"""

struct Config {
    var days: Int?
    var from: String?
    var to: String?
    var today = false
    var date: String?
    var authorizationStatus = false
    var requestAccess = false
}

func writeStderr(_ message: String) {
    FileHandle.standardError.write(Data((message + "\n").utf8))
}

func fail(_ message: String, code: Int32) -> Never {
    writeStderr("error: " + message)
    exit(code)
}

func parseArgs(_ args: [String]) -> Config {
    var config = Config()
    var index = 0

    func value(after option: String) -> String {
        guard index + 1 < args.count else {
            fail("\(option) 缺少参数\n\n\(usage)", code: 2)
        }
        return args[index + 1]
    }

    while index < args.count {
        let arg = args[index]
        switch arg {
        case "-h", "--help":
            print(usage)
            exit(0)
        case "--days":
            guard config.days == nil else { fail("--days 不能重复指定", code: 2) }
            guard let days = Int(value(after: arg)), days >= 1 else {
                fail("--days 需要一个大于等于 1 的整数\n\n\(usage)", code: 2)
            }
            config.days = days
            index += 1
        case "--from":
            guard config.from == nil else { fail("--from 不能重复指定", code: 2) }
            config.from = value(after: arg)
            index += 1
        case "--to":
            guard config.to == nil else { fail("--to 不能重复指定", code: 2) }
            config.to = value(after: arg)
            index += 1
        case "--today":
            guard !config.today else { fail("--today 不能重复指定", code: 2) }
            config.today = true
        case "--date":
            guard config.date == nil else { fail("--date 不能重复指定", code: 2) }
            config.date = value(after: arg)
            index += 1
        case "--authorization-status":
            config.authorizationStatus = true
        case "--request-access":
            config.requestAccess = true
        default:
            fail("未知参数: \(arg)\n\n\(usage)", code: 2)
        }
        index += 1
    }

    if config.today && (config.days != nil || config.from != nil || config.to != nil || config.date != nil) {
        fail("--today 不能与其他范围参数同时使用", code: 2)
    }
    if config.date != nil && (config.days != nil || config.from != nil || config.to != nil) {
        fail("--date 不能与其他范围参数同时使用", code: 2)
    }
    if config.to != nil && config.days != nil {
        fail("--to 与 --days 互斥", code: 2)
    }
    if config.authorizationStatus && config.requestAccess {
        fail("--authorization-status 与 --request-access 互斥", code: 2)
    }
    if (config.authorizationStatus || config.requestAccess) &&
        (config.days != nil || config.from != nil || config.to != nil || config.today || config.date != nil) {
        fail("权限命令不能与事件范围参数同时使用", code: 2)
    }
    return config
}

let config = parseArgs(Array(CommandLine.arguments.dropFirst()))
let calendar = Calendar.current

func parseLocalDate(_ value: String) -> Date? {
    let formatter = DateFormatter()
    formatter.calendar = calendar
    formatter.locale = Locale(identifier: "en_US_POSIX")
    formatter.timeZone = calendar.timeZone
    formatter.dateFormat = "yyyy-MM-dd"
    formatter.isLenient = false
    guard value.count == 10, let date = formatter.date(from: value), formatter.string(from: date) == value else {
        return nil
    }
    return date
}

func parseRangeValue(_ value: String, now: Date) -> Date? {
    switch value.lowercased() {
    case "now": return now
    case "today": return calendar.startOfDay(for: now)
    case "tomorrow": return calendar.date(byAdding: .day, value: 1, to: calendar.startOfDay(for: now))
    default: break
    }
    if let date = parseLocalDate(value) { return date }

    for options: ISO8601DateFormatter.Options in [
        [.withInternetDateTime, .withFractionalSeconds],
        [.withInternetDateTime],
    ] {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = options
        if let date = formatter.date(from: value) { return date }
    }

    for format in ["yyyy-MM-dd HH:mm:ss", "yyyy-MM-dd HH:mm", "yyyy-MM-dd'T'HH:mm:ss", "yyyy-MM-dd'T'HH:mm"] {
        let formatter = DateFormatter()
        formatter.calendar = calendar
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = calendar.timeZone
        formatter.dateFormat = format
        formatter.isLenient = false
        if let date = formatter.date(from: value), formatter.string(from: date) == value { return date }
    }
    return nil
}

let now = Date()
let rangeStart: Date
let rangeEnd: Date

if config.today {
    rangeStart = calendar.startOfDay(for: now)
    guard let end = calendar.date(byAdding: .day, value: 1, to: rangeStart) else { fail("无法计算今天的结束时间", code: 1) }
    rangeEnd = end
} else if let requestedDate = config.date {
    guard let start = parseLocalDate(requestedDate) else { fail("--date 需要有效的 YYYY-MM-DD", code: 2) }
    rangeStart = start
    guard let end = calendar.date(byAdding: .day, value: 1, to: start) else { fail("无法计算指定日期的结束时间", code: 1) }
    rangeEnd = end
} else {
    if let requestedStart = config.from {
        guard let start = parseRangeValue(requestedStart, now: now) else { fail("无法解析 --from：\(requestedStart)", code: 2) }
        rangeStart = start
    } else {
        rangeStart = now
    }

    if let requestedEnd = config.to {
        guard let end = parseRangeValue(requestedEnd, now: now) else { fail("无法解析 --to：\(requestedEnd)", code: 2) }
        rangeEnd = end
    } else {
        guard let end = calendar.date(byAdding: .day, value: config.days ?? 7, to: rangeStart) else { fail("无法计算查询结束时间", code: 1) }
        rangeEnd = end
    }
}

guard rangeStart < rangeEnd else { fail("查询终点必须晚于查询起点", code: 2) }
guard let maximumEnd = calendar.date(byAdding: .year, value: 4, to: rangeStart), rangeEnd <= maximumEnd else {
    fail("查询范围超出支持长度（最大 4 个日历年）", code: 2)
}

// ISO 8601，本地时区，带数值偏移（如 2026-09-08T23:59:00+08:00）
let iso = ISO8601DateFormatter()
iso.formatOptions = [.withInternetDateTime]
iso.timeZone = TimeZone.current

let store = EKEventStore()

// ---- 权限 ----
var status = EKEventStore.authorizationStatus(for: .event)

func statusName(_ value: EKAuthorizationStatus) -> String {
    switch value {
    case .fullAccess: return "fullAccess"
    case .denied: return "denied"
    case .restricted: return "restricted"
    case .writeOnly: return "writeOnly"
    case .notDetermined: return "notDetermined"
    @unknown default: return "unknown"
    }
}

if config.authorizationStatus {
    print(statusName(status))
    exit(0)
}

if config.requestAccess || status == .notDetermined {
    let semaphore = DispatchSemaphore(value: 0)
    var granted = false
    var requestError: Error?
    store.requestFullAccessToEvents { ok, error in
        granted = ok
        requestError = error
        semaphore.signal()
    }
    guard semaphore.wait(timeout: .now() + .seconds(120)) == .success else {
        fail("等待 Calendar 授权超时。请确认系统授权对话框是否可见，然后重试。", code: 1)
    }
    if let requestError {
        fail("请求 Calendar 权限失败: \(requestError.localizedDescription)", code: 1)
    }
    status = EKEventStore.authorizationStatus(for: .event)
    if granted && status != .fullAccess {
        fail("系统报告已授权，但 Calendar 权限状态尚未生效。请稍后重试。", code: 1)
    }
}

if config.requestAccess {
    print(statusName(status))
    exit(status == .fullAccess ? 0 : 1)
}

switch status {
case .fullAccess:
    break
case .denied:
    fail("未获得 Calendar 读取权限。请在 系统设置 > 隐私与安全性 > 日历 中检查本工具或其宿主程序（如终端、Codex）的权限。", code: 1)
case .restricted:
    fail("Calendar 访问在本机被限制。", code: 1)
case .writeOnly:
    fail("Calendar 权限为仅写入，无法读取。", code: 1)
case .notDetermined:
    fail("无法确定 Calendar 权限状态。", code: 1)
@unknown default:
    fail("未知的 Calendar 权限状态。", code: 1)
}

// ---- 读取 ----
let calendars = store.calendars(for: .event)
guard !calendars.isEmpty else {
    fail("没有可访问的 calendar（或权限尚未生效）。", code: 1)
}

// predicate 返回指定范围内发生的事件；随后再显式执行 [rangeStart, rangeEnd) 重叠过滤，
// 因此会包含"已开始、未结束"的进行中事件，并固定边界语义。
// 重复事件会被展开为实际发生的实例，并尊重用户对单次 occurrence 的删除/改期。
let predicate = store.predicateForEvents(withStart: rangeStart, end: rangeEnd, calendars: nil)
let events = store.events(matching: predicate)

// 排除已取消（canceled）的事件，以及当前用户拒绝过（declined）的邀请；tentative 保留。
// 被拒邀请的判定：事件的 attendees 中存在 isCurrentUser 且状态为 declined。
func isDeclinedByCurrentUser(_ event: EKEvent) -> Bool {
    guard let attendees = event.attendees else { return false }
    return attendees.contains { $0.isCurrentUser && $0.participantStatus == .declined }
}

struct EventRecord {
    let title: String
    let start: Date
    let end: Date
    let allDay: Bool
    let calendarTitle: String
    let calendarIdentifier: String
    let location: String
    let notes: String
    let eventIdentifier: String
    let calendarItemIdentifier: String
    let occurrenceDate: Date?
}

// EventKit 的全天事件 endDate 可能是最后一天 23:59:59，也可能已经是次日 00:00。
// 统一为半开区间的次日 00:00，避免上层对全天持续时间产生两种解释。
func normalizedAllDayEnd(_ rawEnd: Date) -> Date? {
    let startOfEndDay = calendar.startOfDay(for: rawEnd)
    if rawEnd == startOfEndDay {
        return rawEnd
    }
    return calendar.date(byAdding: .day, value: 1, to: startOfEndDay)
}

let records: [EventRecord] = events.compactMap { event in
    guard let rawStart = event.startDate, let rawEnd = event.endDate else {
        writeStderr("warning: 跳过缺少开始或结束时间的异常事件。")
        return nil
    }
    guard rawStart < rangeEnd && rawEnd > rangeStart else {
        return nil
    }
    guard event.status != .canceled && !isDeclinedByCurrentUser(event) else {
        return nil
    }

    let outputEnd: Date
    if event.isAllDay {
        guard let normalizedEnd = normalizedAllDayEnd(rawEnd) else {
            writeStderr("warning: 跳过无法规范化结束时间的全天事件。")
            return nil
        }
        outputEnd = normalizedEnd
    } else {
        outputEnd = rawEnd
    }

    return EventRecord(
        title: event.title ?? "",
        start: rawStart,
        end: outputEnd,
        allDay: event.isAllDay,
        calendarTitle: event.calendar?.title ?? "",
        calendarIdentifier: event.calendar?.calendarIdentifier ?? "",
        location: event.location ?? "",
        notes: event.notes ?? "",
        eventIdentifier: event.eventIdentifier ?? "",
        calendarItemIdentifier: event.calendarItemIdentifier,
        occurrenceDate: event.occurrenceDate
    )
}

// 逐级打破平局。若所有键都相同，两个输出对象也相同，因此交换顺序不影响 JSON。
let sortedRecords = records.sorted {
    if $0.start != $1.start { return $0.start < $1.start }
    if $0.title != $1.title { return $0.title < $1.title }
    if $0.calendarTitle != $1.calendarTitle { return $0.calendarTitle < $1.calendarTitle }
    if $0.calendarIdentifier != $1.calendarIdentifier { return $0.calendarIdentifier < $1.calendarIdentifier }
    if $0.end != $1.end { return $0.end < $1.end }
    if $0.eventIdentifier != $1.eventIdentifier { return $0.eventIdentifier < $1.eventIdentifier }
    if $0.calendarItemIdentifier != $1.calendarItemIdentifier {
        return $0.calendarItemIdentifier < $1.calendarItemIdentifier
    }
    return ($0.occurrenceDate ?? .distantPast) < ($1.occurrenceDate ?? .distantPast)
}

let objects: [[String: Any]] = sortedRecords.map { record in
    [
        "title": record.title,
        "start": iso.string(from: record.start),
        "end": iso.string(from: record.end),
        "all_day": record.allDay,
        "calendar": record.calendarTitle,
        "location": record.location,
        "notes": record.notes,
    ]
}

do {
    let data = try JSONSerialization.data(withJSONObject: objects, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
} catch {
    fail("JSON 序列化失败: \(error.localizedDescription)", code: 1)
}
exit(0)
