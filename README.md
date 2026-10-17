<p align="center">
  <img src="assets/logo.svg" alt="OSU 工具箱项目图标" width="128" height="128">
</p>

<h1 align="center">osu-toolbox</h1>

<p align="center">
  <strong>OSU 工具箱</strong><br>
  面向 osu!mania 与 Malody Key 谱面的离线工具箱，提供视频渲染、回放生成与编辑、转谱和变速功能，支持图形界面与 Windows 右键菜单。
</p>

<p align="center">
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/version-0.3.2-9088DD?style=flat" alt="项目版本：0.3.2"></a>
  <a href="#从源码运行"><img src="https://img.shields.io/badge/Python-3.9%2B-3776AB?style=flat&amp;logo=python&amp;logoColor=white" alt="Python 3.9 及以上"></a>
  <a href="#windows-发行包"><img src="https://img.shields.io/badge/platform-Windows-0078D4?style=flat" alt="平台：Windows"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-2E7D5B?style=flat" alt="许可证：MIT"></a>
</p>

<p align="center">
  <a href="https://github.com/XYboxl/OSU-Toolbox/releases">发行页面</a> ·
  <a href="#安装">快速开始</a> ·
  <a href="https://github.com/XYboxl/OSU-Toolbox/issues">问题反馈</a> ·
  <a href="CONTRIBUTING.md">开发约定</a>
</p>

## 功能

| 工具 | 用途 |
| --- | --- |
| 渲染视频 | 将 `.osu` 谱面或 `.osr` 回放导出为 MP4，可调整分辨率、帧率、滚动速度与显示内容 |
| 回放生成 | 从 `.osu` 或 `.mc` 谱面生成完美的 Malody `.mr` 回放 |
| 回放编辑 | 修改 `.mr` 的谱面信息、成绩、MOD、判定类型与事件数据 |
| 转谱 | 转换 `.osu`、`.mc`、`.sm` 谱面及 `.osz`、`.mcz` 归档，也可将文件夹打包为归档 |
| 变速 | 同时调整谱面时间与音乐速度，支持文件、文件夹和归档，可选择保持音调 |

## 安装

### Windows 发行包

请在 [Releases](https://github.com/XYboxl/OSU-Toolbox/releases) 页面查看发行包；如果暂无附件，请使用下方的源码运行方式。

解压整个发行包，运行 `osu-toolbox.exe`。发行包已包含 Python 运行环境，无需另行安装 Python。

请保留同目录的 `osu-toolbox-menu.exe`、`_main` 与 `_menu` 文件夹；右键菜单使用独立的菜单程序。不要只复制两个 EXE。

图形界面需要 Microsoft Edge WebView2 Runtime。视频渲染与音频变速还需要 FFmpeg：**发行包不附带 FFmpeg**，请自行安装并将其加入系统 `PATH`或放入`osu-toolbox.exe`同级目录下。

### 从源码运行

需要 Python 3.9 或更高版本。克隆仓库后，使用 [uv](https://docs.astral.sh/uv/) 安装依赖并启动：

```powershell
git clone https://github.com/XYboxl/OSU-Toolbox.git
cd OSU-Toolbox
uv sync
uv run osu-toolbox
```

也可使用 `python -m pip install -e .` 安装。源码运行时，除系统 `PATH` 外，也可将 `ffmpeg.exe` 放在项目根目录。

## 使用

### 图形界面

1. 选择工具，添加输入文件或文件夹。
2. 按需调整参数和输出位置，点击执行。
3. 在界面查看进度、日志和结果。

视频渲染需要选择 `.osu` 谱面与自定义皮肤目录；需要还原真实游玩时，再选择对应的 `.osr` 回放。未选择回放时使用 Auto。皮肤缺失的资源不会由内置默认皮肤补齐。

主程序默认输出到程序所在目录，右键菜单默认输出到输入所在目录；可在运行前调整输出位置。游戏目录可在设置中配置，供工具按回放的谱面 MD5 查找对应谱面。

### Windows 右键菜单

打开“设置 → 右键菜单”，勾选所需功能即可立即启用，取消勾选即可移除。默认全部关闭，也可点击“卸载右键菜单”一次移除全部项目。

- `.osu`、`.mc`：生成完美回放。
- `.osu`、`.mc`、`.sm`：转谱；`.osz`、`.mcz`：转换归档。
- 文件夹或文件夹内空白处：打包为 `.osz` 或 `.mcz`。
- 谱面与归档文件：按预设或自定义倍率变速。

启用菜单后，请保留程序所在路径；移动程序后可重新勾选以更新菜单指向。所有设置均可通过“恢复到默认”清除。

### 命令行渲染（源码安装）

```powershell
uv run osu-toolbox-render "谱面.osu" --skin-dir "皮肤目录" -o "输出.mp4"
uv run osu-toolbox-render --help
```

## 已知限制

- 视频渲染仅面向 osu!mania，尚未完整还原 osu!stable 的全部表现；分数、判定边界及部分 HUD 动画仍有差异。
- 暂不支持背景视频与故事板，MOD 的渲染与判定兼容仍不完整。
- 皮肤兼容性会因键数、布局、动画与资源配置而异，部分皮肤可能显示不完整。
- 谱面格式之间存在能力差异，转换可能丢失目标格式无法表达的效果。请查看转换提示，并在目标游戏中验证结果。
- 高分辨率或高帧率渲染耗时较长，实际速度取决于 CPU 与可用的视频编码器。

## Roadmap

- 持续校准 mania 判定、回放边界与画面表现。
- 扩充皮肤、键数和布局的兼容测试。
- 完善转谱的时间线、资源引用与格式兼容。
- 改善启动、批量处理效率与 Windows 发行体验。
- 添加更多工具...

## 参与贡献

欢迎通过 [Issues](https://github.com/XYboxl/OSU-Toolbox/issues) 报告问题或提出建议，也欢迎提交 [Pull Request](https://github.com/XYboxl/OSU-Toolbox/pulls)。反馈问题时，请提供工具版本、Windows 版本、复现步骤和相关日志。

提交代码前，请阅读[开发约定](CONTRIBUTING.md)，并运行以下检查：

```powershell
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q osu_toolbox tests
```

## 许可证

本项目采用 [MIT 许可证](LICENSE)。作者：Difficile（XYboxl）。

本项目与 osu!（ppy）官方无隶属关系，不附带谱面、回放、音频或皮肤；相关资源由使用者自行提供。
