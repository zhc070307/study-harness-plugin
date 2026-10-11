# 下载和安装

返回[用户指南](../README.md)。这份指南从下载开始，直到你能在 Harness 里使用学习助手。

## 1. 下载插件

点击[下载插件安装包（0.1.0）](https://github.com/zhc070307/study-harness-plugin/raw/refs/heads/main/downloads/study-agent-harness-plugin-0.1.0.tgz)，将文件保存到电脑的“下载”文件夹。

下载的是已经构建好的插件包，无需下载源码、安装开发工具或自行编译。文件名应为 `study-agent-harness-plugin-0.1.0.tgz`，保留这份原始压缩包，后面安装时会用到。

仓库是私有仓库，需要在浏览器登录有访问权限的 GitHub 账号。如果点击链接显示 404，先确认登录状态和仓库访问权限。也可以打开仓库的 `downloads` 文件夹，进入安装包文件页面，点击 **Download raw file** 下载。

先确认你使用的 Harness 版本：本插件目前适配 **0.2.1-alpha.2**。其他版本尚未验证，遇到版本不匹配时请先核对版本。

## 2. 准备一次 Python 环境

插件用 Python 读取材料、保存学习记录和处理 PDF。建议安装 [Python 3.12](https://www.python.org/downloads/release/python-31214/)。下面的命令会为插件准备一个单独的环境，并下载所需的小型依赖，不会下载录音识别模型。

### macOS / Linux

打开“终端”，先运行：

```sh
python3.12 --version
```

显示 `Python 3.12.x` 后，逐行运行下面的命令。它们假设安装包保存在你的“下载”文件夹中；如果放在别处，请修改第二行的文件路径。

```sh
mkdir -p "$HOME/.study-harness-plugin"
tar -xzf "$HOME/Downloads/study-agent-harness-plugin-0.1.0.tgz" -C "$HOME/.study-harness-plugin"
python3.12 -m venv "$HOME/.study-harness-plugin/.venv"
"$HOME/.study-harness-plugin/.venv/bin/python" -m pip install -r "$HOME/.study-harness-plugin/package/requirements.txt"
"$HOME/.study-harness-plugin/.venv/bin/python" -c 'import sys; print(sys.executable)'
```

记下最后一行输出的完整路径，例如 `/Users/你的用户名/.study-harness-plugin/.venv/bin/python`。第 4 步会用到它。这个文件夹需要保留，安装后不要删除或移动。

### Windows

安装 Python 3.12 后，打开 **PowerShell**，先运行：

```powershell
py -3.12 --version
```

显示 `Python 3.12.x` 后，逐行运行：

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.study-harness-plugin" | Out-Null
tar -xzf "$env:USERPROFILE\Downloads\study-agent-harness-plugin-0.1.0.tgz" -C "$env:USERPROFILE\.study-harness-plugin"
py -3.12 -m venv "$env:USERPROFILE\.study-harness-plugin\.venv"
& "$env:USERPROFILE\.study-harness-plugin\.venv\Scripts\python.exe" -m pip install -r "$env:USERPROFILE\.study-harness-plugin\package\requirements.txt"
& "$env:USERPROFILE\.study-harness-plugin\.venv\Scripts\python.exe" -c "import sys; print(sys.executable)"
```

如果安装包保存在其他位置，请修改第二行的路径。记下最后输出的完整路径，例如 `C:/Users/你的用户名/.study-harness-plugin/.venv/Scripts/python.exe`。这个文件夹需要保留。

## 3. 装进 DeepSeek Harness

### 桌面版：在窗口里安装

1. 打开 DeepSeek Harness，进入侧栏的 **插件** 页面。
2. 点击 **添加插件**。在输入框中填入第 1 步下载的 `.tgz` 文件的**完整路径**，不要填解压后的 Python 环境路径。
3. 点击 **安装**，等待完成。如果提示是否启用新安装的插件，选择启用；也可以在已安装插件列表中打开它的开关。

路径示例：

- macOS：`/Users/你的用户名/Downloads/study-agent-harness-plugin-0.1.0.tgz`
- Windows：`C:/Users/你的用户名/Downloads/study-agent-harness-plugin-0.1.0.tgz`

将“你的用户名”替换为实际用户名。输入框里只填路径，不加引号。安装时需要联网获取插件依赖。

### 网页版：用命令安装

在运行 Harness 的那台电脑上下载并准备环境，然后运行下面的命令。将文件路径换成实际路径：

```sh
dsh plugin --profile web add "/完整路径/study-agent-harness-plugin-0.1.0.tgz"
```

如果你使用自己命名的配置，例如 `my-harness`，就把 `web` 换成这个名称。网页版使用远程服务器时，插件包和 Python 环境都需要放在服务器上，不能填写浏览器所在电脑的路径。

桌面版优先使用窗口里的安装入口。如果你要通过命令安装桌面插件，先在应用菜单的 **管理 dsh 命令…** 中安装桌面版提供的命令，启动一次桌面版后完全退出，再使用 `dsh plugin --profile desktop add "安装包完整路径"`。通过 npm 安装的 `dsh` 命令不能管理桌面版插件。

## 4. 告诉插件使用哪个 Python

安装插件后，完全退出 Harness。用文本编辑器打开下面的配置文件：

- macOS/Linux 桌面版：`~/.dsh/profiles/desktop/cordis.patch.yml`。
- Windows 桌面版：用户文件夹下的 `.dsh/profiles/desktop/cordis.patch.yml`。
- 网页版：将上述路径中的 `desktop` 换成 `web`，或你使用的配置名称。

macOS 可在 Finder 中按 **⌘⇧G**，粘贴 `~/.dsh/profiles/desktop` 打开所在文件夹。Windows 可在资源管理器地址栏输入 `%USERPROFILE%\.dsh\profiles\desktop`。

在配置文件中加入以下内容，将 `python` 后面的路径替换成第 2 步输出的完整路径：

```yaml
- id: study-projects
  config:
    python: "/Users/你的用户名/.study-harness-plugin/.venv/bin/python"
```

Windows 的这一行可写成：

```yaml
    python: "C:/Users/你的用户名/.study-harness-plugin/.venv/Scripts/python.exe"
```

保留缩进。Windows 路径请使用 `/`。如果文件已有 `id: study-projects` 这一项，只修改该项的 `config.python`；其他配置保留。如果文件中只有 `[]`，用上面的完整三行内容替换它。如果你自定义了 `DSH_HOME`，配置文件位于那个目录下。

保存后重新打开 Harness。

## 5. 启用你的学习项目，并确认安装成功

在 Harness 中打开一个准备存放课程材料的项目，向助手发送：

> 把这个项目绑定为学习项目，然后查看当前课程记录。

确认它完成绑定，并能返回课程记录；新项目没有课程是正常的。随后上传一份讲义，再说：

> 导入这份讲义，读一下内容，告诉我它主要讲什么。

如果能导入并读取材料，基础功能就可以使用了。在这个项目下新开的对话也会获得学习工具，不需要再次安装或绑定。已有学习数据会继续保留。

遇到 Harness 的工具执行权限请求时，按本次学习操作的需要授权；拒绝执行后，助手无法读写学习材料。

## 可选：PDF 成品、录音和日历

- **生成 PDF 笔记或练习：** 需要另外安装 LaTeX，并确保 `latexmk` 和 `xelatex` 可运行。只读取已有 PDF 材料不需要 LaTeX。
- **录音转写：** 需要受支持的 GPU 和足够空间。可以先说“检查这台电脑能否转写录音”；首次下载模型会另外征求同意。
- **日历：** 可以选择 macOS 的 Apple Calendar，或提供 CalDAV 接口的服务，需要单独配置。详见[日历配置](setup.md#外部日历)。不接日历也可以使用学习功能。

## 如果没有安装成功

| 现象 | 可以先检查什么 |
| --- | --- |
| 下载链接显示 404 | 浏览器是否登录了有权访问这个私有仓库的 GitHub 账号。 |
| 终端找不到 Python | 是否已经安装 Python 3.12；安装后重新打开终端。 |
| Harness 提示找不到安装文件 | 填入的是 `.tgz` 文件的实际完整路径，而不是网页地址或示例用户名。 |
| 插件已安装，但助手没有学习工具 | 插件开关是否开启，是否重启过 Harness，当前项目是否完成绑定。 |
| 读取材料时报 Python 或依赖错误 | 第 2 步的依赖安装是否成功，第 4 步的路径是否与输出一致。 |
| 不能生成 PDF | 是否安装了 LaTeX；这是生成成品的额外要求。 |

需要反馈问题时，提供 Harness 版本、操作系统和实际错误提示，方便定位。
