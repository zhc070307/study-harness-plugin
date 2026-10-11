# Study Harness Plugin

独立的 DeepSeek Harness 学习插件。使用 Harness 的窗口、文件工作区、会话和模型服务，提供课程与学期、材料整理、教材检索、笔记与练习、作答反馈、录音转写和外部日历。无需运行 Study Agent 的独立桌面应用。

本仓库从 Study Agent 选取学习领域源码独立维护，来源见 [SOURCE.json](SOURCE.json)。不包含 Harness runtime；插件调用宿主提供的公开包接口。当前适配并测试的官方版本是 **0.2.1-alpha.2**，版本不匹配时应先更新适配，不能靠忽略版本检查安装。

## 构建与安装

需要 Node.js 24+；普通学习功能需要 Python 3.10+，录音转写后台需要 Python 3.12。建议统一使用 Python 3.12。

```sh
npm ci
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm run build
mkdir -p dist
npm pack --pack-destination dist
```

Windows 的虚拟环境解释器为 `.venv/Scripts/python.exe`。`requirements.txt` 包含 PDF 渲染、图像、CalDAV 和时区依赖，不下载转写模型。

在实际使用的 Harness profile 中安装生成的包，例如 Web：

```sh
dsh plugin --profile web add /absolute/path/study-agent-harness-plugin-0.1.0.tgz
```

Desktop 使用其内置插件管理器，或 Desktop 安装提供的命令管理 `desktop` profile；npm CLI 不管理 Desktop profile。安装在其他 profile 中不会自动影响 Desktop。

在插件配置中将 `python` 指向上述虚拟环境解释器的绝对路径。默认值为 `python3`。通过 profile patch 配置时：

```yaml
- id: study-projects
  config:
    python: /absolute/path/.venv/bin/python
```

插件代码运行在宿主进程中，Python 工具在本机处理事务和编译。因此显式学习工具使用宿主的 `danger-full-access` 权限或本次操作审批，不自动改变会话权限；拒绝或没有审批通道时不会执行。自动注入的项目状态只读，不初始化数据库。

## 绑定学习项目

安装一次后，可在当前会话中要求助理“把这个项目绑定为学习项目”；也可运行：

```sh
node lib/cli.js bind /absolute/path/learning-project
node lib/cli.js status /absolute/path/learning-project
node lib/cli.js doctor /absolute/path/learning-project /absolute/path/.venv/bin/python
```

绑定保存为项目目录中的 `.study-plugin.json`：

```json
{"version":1,"enabled":true,"data":"."}
```

该项目及其子目录下的对话自动获得学习工具和提示词。未绑定项目只看到绑定工具，保持原来的模型行为。子目录可显式停用；学习数据目录必须位于绑定项目内部，不接受目录穿越或符号链接逃逸。配置更改会在下一次请求生效。

课程成果保存并发布到真实工作区中的课程目录，直接在 Harness 文件窗口打开。已有工作区文件用 `study_import path` 导入；当前聊天上传的文件用 `study_import attachment_id` 导入，宿主校验文件身份与字节，原件保留。同一源文件同一版本重复导入复用记录。

每个会话首次使用学习工具时绑定当前学期，之后保持自己的选择。`study_desktop semester select` 只切换当前会话，其他会话保持各自学期；选择可在重启后恢复。数据沿用 `.study-system`、`.workspace` 和 `.semesters` 布局，可处理原应用的数据副本。

## 跨对话上下文

每次模型请求前读取当前学期的课程、未完成任务、学习流程、已登记材料、约定和共享摘要。内容变化或会话上下文重建时重新注入，未变化时复用已有记录。

`study_context remember` 保存实际读过的材料摘要，带来源路径与 SHA-256。其他对话可以直接读取；原文变化或来源消失时标记为过期，要求重新核对原文。它不自动复制所有聊天历史，也不把生成的文件当作用户已经完成学习。当前状态摘要有数量限制，详细资料继续通过学习工具读取。

## 录音转写

`study_transcribe` 提供 check、status、start、stop。插件按需管理绑定 `127.0.0.1` 的鉴权后台，使用随机端口，不要求单独启动桌面程序。卸载插件时关闭其后台，学习记录和已完成片段保留。

已有文字稿直接读取。检测实际 GPU、内存、显存和磁盘后才允许开始；不支持的设备不自动回退到 CPU。首次 `start` 需要用户同意下载环境和约 3.8 GiB 模型，才能传 `allow_install:true`。GPU 支持 Apple Silicon Metal 和 Windows/Linux x64 NVIDIA，具体资源检查沿用现有实现。

Python 环境中的 PDF、日历依赖与转写推理环境分开管理。后台任务状态可通过工具读取，完成原稿关联回源录音；停止、重启和失败保留可恢复记录。

## 外部日历

`study_calendar` 支持 status、calendars、list、read、create、update、delete。事件与学习任务分别记录，可以关联真实任务身份。非全天事件必须带时区；全天事件结束日期不含边界。更新和删除须提供当前事件版本，冲突拒绝。重复系列不提供批量改写。

### CalDAV

在项目绑定文件中增加：

```json
{
  "version":1,"enabled":true,"data":".",
  "calendar":{
    "provider":"caldav",
    "url":"https://calendar.example/dav/calendars/user/study/",
    "usernameEnv":"STUDY_CALDAV_USERNAME",
    "passwordEnv":"STUDY_CALDAV_PASSWORD"
  }
}
```

凭证通过启动 Harness 的环境变量提供，配置文件不存密码。URL 指向具体日历集合；远程连接要求 HTTPS，拒绝自动重定向。查询用 CalDAV REPORT，创建使用稳定 UID 和 `If-None-Match`，更新/删除使用 ETag 与 `If-Match`。本地操作日志防止重复写入；中断后结果不明时要求先查询真实事件，不自动重发。

### macOS Apple Calendar

macOS 14+，需要 Xcode Command Line Tools 构建 EventKit 辅助程序：

```sh
bash packages/learning/tools/calendar-events/build-plugin.sh
```

绑定配置增加：

```json
{"provider":"apple","executable":"/absolute/path/packages/learning/tools/calendar-events/study-calendar"}
```

此对象放在 `calendar` 字段中。系统会请求日历授权，工具只按指定动作处理事件。原 `calendar-events` 工具仍保留只读行为。Apple 适配器的实际编译、系统授权和真实日历写入需要在 macOS 验证。

## 验证

```sh
npm run check
npm pack --pack-destination dist
npm run test:install
```

检查构建和 TypeScript，使用官方发布的 Harness 服务运行项目隔离、实时绑定、共享状态、附件导入和权限拒绝测试；CalDAV 测试通过临时本地 HTTP 服务验证实际读写与 ETag 冲突，Python 领域测试只使用隔离数据。

安装测试在临时 `DSH_HOME` 中建立官方最小 profile，安装真实 tarball、检查插件配置并启动 Harness，结束后清理，不修改当前用户的配置。首次安装可能需要网络访问 npm registry。

需要真实设备或账户的验证包括 GPU 模型下载与识别、macOS EventKit 编译与授权，以及目标日历服务的连接。上述测试不会下载大模型、访问个人日历或修改真实学习资料。

发布包包含必要 Python 源码、迁移、学习规范和转写资源，不包含独立 UI、Harness runtime、开发依赖、个人数据或模型权重。
