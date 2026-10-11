# calendar-events

只读读取 macOS Apple Calendar 的 CLI 工具。为 Codex 在生成学习计划前提供日程数据：可以读取今天、指定日期、从指定起点开始的 N 天，或明确的起止时间范围。

**只读。** 不创建、不修改、不删除任何事件；不使用网络；所有数据留在本机。

## 构建

```bash
./build.sh          # 需要 Xcode Command Line Tools（xcode-select --install）
```

要求 macOS 14 或更高版本。构建脚本生成同时支持 Apple Silicon 与 Intel Mac 的通用二进制 `./calendar-events`，并嵌入 Calendar 权限用途说明。如需全局使用，复制到 PATH 中即可（如 `cp calendar-events ~/.local/bin/`）。

## 用法

```bash
calendar-events                 # 默认：从当前时刻起未来 7 天
calendar-events --days 14       # 从当前时刻起未来 14 天
calendar-events --today         # 今天 00:00 至明天 00:00
calendar-events --date 2040-09-07
calendar-events --from today --days 14
calendar-events --from "2040-09-07 09:00" --to "2040-09-07 18:00"
calendar-events --help
```

`--from` 和 `--to` 支持 `now`、`today`、`tomorrow`、`YYYY-MM-DD`、本地日期时间和带时区的 ISO 8601。`--to` 是不含边界，并与 `--days` 互斥。`--today` 与 `--date` 都查询完整日历日，不能与其他范围参数混用。单次查询最长 4 个日历年。

- stdout 只输出 JSON 数组（UTF-8），可直接交给 Codex 或其他脚本解析。
- 所有错误、警告走 stderr，不污染 stdout。
- 退出码：`0` 成功；`1` 运行失败（权限被拒、无可用 calendar 等）；`2` 参数错误。

## 输出格式

```json
[
  {
    "title": "TEST 101 - Example Deadline",
    "start": "2040-09-08T23:59:00+08:00",
    "end": "2040-09-09T00:00:00+08:00",
    "all_day": false,
    "calendar": "Example Calendar",
    "location": "",
    "notes": ""
  }
]
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `title` | 是 | 事件标题，缺失时为空字符串 |
| `start` | 是 | ISO 8601，本地时区带数值偏移，如 `2040-09-08T23:59:00+08:00` |
| `end` | 是 | 同上 |
| `all_day` | 是 | 全天事件标记；多日全天事件的 `end` 为最后一个全天的次日 00:00（不含） |
| `calendar` | 是 | 事件来源 calendar 的名称 |
| `location` | 可选 | 无则为 `""` |
| `notes` | 可选 | 无则为 `""` |

## 行为契约（已定稿）

- 时间范围统一采用 `[rangeStart, rangeEnd)`，结束边界不含；**进行中的事件**（start 早于 rangeStart、end 晚于 rangeStart）包含在内。
- 读取所有可访问的 calendar，不要求预先建立 Academic calendar，不按标题关键词过滤。
- 重复事件展开为实际发生的实例（含改期后的时间），尊重用户对单次 occurrence 的删除；不返回 recurrence rule 本身。
- 全天事件保留并标记 `all_day`；跨天事件保留真实起止时间，不截断。
- 排除已取消（canceled）与被拒绝（declined）的事件；tentative 事件保留。
- 事件按开始时间升序排列，并使用标题、calendar、结束时间和 EventKit 标识符逐级打破平局，保证输出确定。
- 没有任何事件时输出 `[]`，退出码 0，不算错误。
- 权限被拒、没有可访问的 calendar 等失败情形输出明确错误到 stderr 并退出码非 0，不会把访问失败伪装成"没有事件"。
- 工具不做课程 / deadline / 优先级 / 每日任务等语义判断，不读 COURSE.md，这些是上层 Codex 的职责。

## 权限

首次运行时 macOS 会弹出日历访问授权对话框，点击允许后即可静默运行。EventKit 不提供只读级别的系统授权；读取事件必须申请 full access，但本工具的源码没有保存、修改或删除调用，行为仍然严格只读。

- 拒绝授权后工具会给出明确错误信息。
- 优先在 系统设置 → 隐私与安全性 → 日历 中检查或调整权限。
- `tccutil reset Calendar` 会重置本机**所有应用**的 Calendar 授权；仅在理解这一影响并确实需要全局重置时使用，然后再次运行本工具。
- 若授权对话框始终不出现（少见），通常与宿主程序本身被拒绝过 Calendar 权限有关，在 系统设置 > 隐私与安全性 > 日历 中检查。

## 集成示例

```bash
calendar-events --days 7 > /tmp/events.json   # Codex 随后读取该文件
```

生成当天计划时应使用 `--today` 或从 `today` 开始的范围，避免漏掉当天早些时候已经结束的事件。
