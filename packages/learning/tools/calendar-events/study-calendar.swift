// Structured EventKit tool. The original calendar-events binary stays read-only.
import Foundation
import EventKit
func fail(_ message: String) -> Never { FileHandle.standardError.write(Data(message.utf8)); exit(1) }
func encode(_ value: Any) {
    guard let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]), let text = String(data: data, encoding: .utf8) else { fail("日历结果编码失败") }
    print(text)
}
guard let input = try? JSONSerialization.jsonObject(with: FileHandle.standardInput.readDataToEndOfFile()) as? [String: Any], let action = input["action"] as? String else { fail("请求必须是JSON对象") }
let store = EKEventStore()
if EKEventStore.authorizationStatus(for: .event) != .fullAccess {
    let semaphore = DispatchSemaphore(value: 0)
    var granted = false
    store.requestFullAccessToEvents { ok, _ in granted = ok; semaphore.signal() }
    guard semaphore.wait(timeout: .now() + .seconds(110)) == .success, granted else { fail("未获得日历权限，请检查系统设置中的日历授权") }
}
let formatter = ISO8601DateFormatter()
func instant(_ key: String, allDay: Bool = false) -> Date {
    guard let value = input[key] as? String else { fail("缺少时间 \(key)") }
    if allDay {
        let day = DateFormatter(); day.dateFormat = "yyyy-MM-dd"; day.locale = Locale(identifier: "en_US_POSIX"); day.isLenient = false
        guard let result = day.date(from: value), day.string(from: result) == value else { fail("全天日期格式无效") }
        return result
    }
    guard let result = formatter.date(from: value) else { fail("时间必须是带时区的ISO8601") }
    return result
}
func record(_ event: EKEvent) -> [String: Any] {
    return ["id": event.eventIdentifier ?? "", "expected": formatter.string(from: event.lastModifiedDate ?? event.creationDate ?? event.startDate), "title": event.title ?? "", "start": formatter.string(from: event.startDate), "end": formatter.string(from: event.endDate), "all_day": event.isAllDay, "notes": event.notes ?? "", "location": event.location ?? "", "calendar": event.calendar.calendarIdentifier, "recurring": event.hasRecurrenceRules]
}
if action == "calendars" { encode(store.calendars(for: .event).map { ["id": $0.calendarIdentifier, "name": $0.title, "writable": $0.allowsContentModifications] as [String: Any] }); exit(0) }
if action == "list" {
    let start = instant("start"), end = instant("end")
    guard end > start, end.timeIntervalSince(start) <= 366 * 86400 else { fail("查询范围须大于零且不超过一年") }
    let events = store.events(matching: store.predicateForEvents(withStart: start, end: end, calendars: nil)).filter { $0.status != .canceled && $0.startDate < end && $0.endDate > start }.sorted { $0.startDate < $1.startDate }
    encode(events.map(record)); exit(0)
}
var event: EKEvent
if action == "create" {
    event = EKEvent(eventStore: store)
    if let calendar = input["calendar"] as? String { event.calendar = store.calendar(withIdentifier: calendar) }
    else { event.calendar = store.defaultCalendarForNewEvents }
    guard event.calendar != nil, event.calendar.allowsContentModifications else { fail("没有可写日历") }
} else {
    guard let id = input["id"] as? String, let existing = store.event(withIdentifier: id) else { fail("日历事件不存在") }
    event = existing
    if action == "read" { encode([record(event)]); exit(0) }
    guard let expected = input["expected"] as? String, expected == (record(event)["expected"] as? String) else { fail("事件已变化，请重新读取") }
    guard !event.hasRecurrenceRules, !event.isDetached else { fail("重复事件请在日历应用中修改") }
}
do {
    if action == "delete" { try store.remove(event, span: .thisEvent, commit: true); encode(["deleted": true, "id": input["id"]!]); exit(0) }
    guard action == "create" || action == "update", let title = input["title"] as? String, !title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { fail("标题不能为空或操作无效") }
    event.isAllDay = input["all_day"] as? Bool ?? false
    event.startDate = instant("start", allDay: event.isAllDay); event.endDate = instant("end", allDay: event.isAllDay)
    guard event.endDate > event.startDate else { fail("结束必须晚于开始") }
    event.title = title; event.notes = input["notes"] as? String ?? ""; event.location = input["location"] as? String ?? ""
    try store.save(event, span: .thisEvent, commit: true)
    encode(["saved": true, "events": [record(event)]])
} catch { fail("日历写入失败：\(error.localizedDescription)") }
