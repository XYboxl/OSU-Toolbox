# 开发与贡献约定

本指南面向参与 OSU 工具箱开发的贡献者，说明开发环境、代码组织、验证方式和提交要求。功能、使用方式与已知限制见 [README](README.md)。

## 报告问题与提出改进

请通过 [Issues](https://github.com/XYboxl/OSU-Toolbox/issues) 反馈问题，并提供：

- 工具版本、Windows 版本及运行方式（发行包或源码）。
- 使用的工具、参数、输入格式和可重复的操作步骤。
- 预期结果、实际结果及相关日志；画面问题可附截图或录制。

涉及新工具、设置分类、调度方式或大范围架构调整时，请先讨论需求与兼容边界，再提交实现。

## 开发环境

开发环境为 Windows、Python 3.9+，使用 [uv](https://docs.astral.sh/uv/) 管理依赖：

```powershell
git clone https://github.com/XYboxl/OSU-Toolbox.git
cd OSU-Toolbox
uv sync
uv run osu-toolbox
```

图形界面需要 Microsoft Edge WebView2 Runtime。视频渲染与音频变速需要 FFmpeg，可加入系统 `PATH`，或在源码运行时放入项目根目录。开发依赖与入口以 [pyproject.toml](pyproject.toml) 为准。

## 代码组织

| 位置 | 职责与依赖约束 |
| --- | --- |
| `osu_toolbox/parsing/`、`skin/`、`mania/` | 解析、皮肤、判定与帧状态；不得依赖 `render`、`video`、`tools`、`gui` |
| `osu_toolbox/render/`、`video/` | 画面合成与视频导出；不得依赖 `tools`、`gui` |
| `osu_toolbox/tools/` | 工具参数、预检与执行编排；调用核心公共 API |
| `osu_toolbox/gui/` | 界面、Bridge 与任务展示；只调用根包元数据和工具公共平台 API |
| `osu_toolbox/config.py`、`schema.py` | 运行时配置与参数契约 |
| `tests/` | `unittest` 回归测试及架构约束 |

- 新逻辑放入对应模块与类，避免在 GUI 中加入业务算法。
- 新工具采用一个工具模块和一条显式注册：实现真实 controller，在 `tools/registry.py` 的 `build_default_registry()` 注册。普通工具无需修改通用 Bridge、路由或任务运行器。
- 参数登记在工具的 `ToolSpec.params`；新增渲染参数还需同步 `config.DEFAULT_RENDER_PARAMS` 与渲染工具参数，并保持默认值和界面契约一致。
- 函数体内通过 `config.X` 读取运行时常量，避免用 `from config import X` 固定旧值。派生常量统一在 `configure_runtime` 中重算。
- 包根保持轻量导入，新增能力按既有 `_DEFERRED_ATTRS` 机制惰性加载，避免拖慢右键菜单启动。
- 当前任务只支持 `concurrency="exclusive"`；全局运行时状态尚未隔离前，保持明确的资源互斥。
- 用户可见的界面、确认框、日志和结果标签使用中文。新设置分类需先讨论确认。

## 行为与兼容约定

- 兼容行为以官方格式与皮肤文档、公开源码、实机画面和回归测试为依据。保留已知限制，不将近似实现描述为完整还原。
- 性能优化前先测量帧状态生成、CPU 合成和编码耗时，提交时说明优化依据与结果。

## 验证

修改 Python 代码或业务行为后，运行回归测试和语法检查：

```powershell
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q osu_toolbox tests
```

为行为修复补充能复现问题的针对性用例，覆盖相关入口及边界。依赖方向、运行时配置、工具参数与界面契约由现有架构和冒烟测试共同约束。

涉及 GUI 时，运行 `uv run osu-toolbox` 检查实际流程。视觉还原需与同谱面、同皮肤的实机录制逐帧对照，不能仅凭测试通过宣称画面一致。

部分验证需要不随仓库分发的本地谱面、皮肤或录制。缺少输入时，在 Pull Request 中说明未完成的验证与原因；不要将未运行的检查记录为通过。纯文档修改检查链接、示例和排版即可。

## 提交改动

- 保持改动聚焦，沿用现有命名与代码风格。提交信息简短、以动词开头，例如 `Fix chart timing conversion`。
- Pull Request 说明具体问题、改动后的行为、验证结果及仍存在的限制。
- Git 只跟踪必要的核心代码、测试、图标、项目元数据、使用说明与公开开发约定。
- `.gitignore` 采用核心文件白名单和通用缓存规则。新增顶层核心文件或目录时，同步更新白名单
- 保留第三方代码及资源必需的版权和许可证声明。
