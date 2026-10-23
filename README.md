# Virtual Character QQ Bot (AstrBot plugin set)

![static-checks](https://github.com/Anta6aka/virtual-character-qq-bot-public/actions/workflows/static-check.yml/badge.svg)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)

一套运行在 [AstrBot](https://github.com/AstrBotDevs/AstrBot) + NapCat 上的 QQ
角色机器人插件与运维脚本。仓库只包含代码和示例配置，**不包含任何角色人设、
聊天记录、表情素材、账号配置、数据库或日志**。

## 功能

- **LTM FTS 长期记忆**：带来源、可见性和记忆边界，不依赖向量模型。
- **关系状态**：熟悉度/信任度、阶段、知心席位、跨场景记忆和表达提示；可选、失败关闭。
- **本地表情**：低概率向模型提供候选目录，由模型选择后随回复发送；带冷却和群范围。
- **日语 TTS**：把中文对照前的台词送往外部 GPT-SoVITS，并保留文字输出。
- **输出守卫**：最终回复格式校验、限时补译和匿名运行统计。
- **网页搜索**：可选的联网搜索工具。
- **运维**：连接守卫、备份/恢复、临时目录清理、静态检查。

## 目录

```
AstrBot/data/plugins/    AstrBot 插件（LTM、关系、表情、TTS、守卫、搜索）
docs/                    架构、部署和关系设计说明
tests/                   隔离运行的单元测试
examples/                示例配置（不含真实账号）
connection_guard.py      双角色 NapCat/AstrBot 健康检查与恢复
static_check.py          AST/JSON/链接/隐私/密钥静态检查
cleanup_temp.py          临时目录清理
restore_backup.py        备份恢复
requirements-local.txt   AstrBot 之外的本地依赖
CHANGELOG.md             版本记录
```

## 环境

- Python 3.12+
- AstrBot 4.29.x（插件按 4.29.1 开发）
- NapCat / OneBot 11，反向 WebSocket 接入 AstrBot
- 可选：GPT-SoVITS HTTP 服务
- 运维脚本主要面向 Windows（PowerShell/CIM）

## 快速开始

1. 安装 AstrBot，并确认 `aiocqhttp` 平台能收到消息。
2. 把 `AstrBot/data/plugins/` 下的插件目录复制到你的 AstrBot 实例的
   `data/plugins/`。
3. 按 `examples/` 准备配置：
   - `examples/relationship.example.json` → 插件的
     `data/relationship.json`（关系开关和角色 ID）
   - `examples/sticker_groups.example.json` → 实例的
     `AstrBot/data/sticker_groups.json`
   - `examples/character_profile.example.json` → 实例的
     `AstrBot/data/plugins/character_profile.json`
   - `examples/connection_guard.example.json` → 项目根目录
     `connection_guard.json`
4. 表情素材不随仓库分发。把自有素材放到项目根目录 `stickers/`，或用
   `sticker_profile.json` 指向自己的清单。
5. 重启 AstrBot，检查插件加载日志。

## 关键配置

- `relationship.json`：`enable`、`character_id`、`max_visible`。文件损坏时
  关系功能自动关闭，不影响对话。
- `sticker_groups.json`：`{"schema":1,"groups":["<群号>"]}`，不在名单内的群不会附图。
- `character_profile.json`：`schema`、`name`、`aliases`、`interest_pattern`、
  `turn_guidance`。
- `connection_guard.json`：角色 UIN、端口、运行目录、群号和 token 文件路径。

## 文档

- [架构说明](docs/ARCHITECTURE.md)
- [部署步骤](docs/SETUP.md)
- [关系系统设计概览](docs/RELATIONSHIP.md)
- [更新日志](CHANGELOG.md)

`relationship/params.py` 里的数值是示例工程默认值，不是生产调参建议；请按自己的
部署目标审阅和调整。公开文档只讲设计，不提供可照抄的加分表。

## 测试

```bash
python tests/run_tests.py
python static_check.py
```

`tests/run_tests.py` 使用隔离临时目录，不读取生产数据库。


## 已知限制

- 运维脚本以 Windows 为主；`connection_guard.py` 的“账号进程不存在”恢复分支会调用
  部署方自己的 `start-qq.ps1`，本仓库不包含该启动器（路径和账号因部署而异），
  请自行提供或关闭该分支。
- `restore_backup.py` 假设仓库根目录下存在 `AstrBot/`、`backups/` 等目录结构，
  迁移到其他布局时需要调整路径。
- CI 只运行静态检查；功能测试请在本地运行 `python tests/run_tests.py`。

## 隐私与安全

- 不要提交 `cmd_config.json`、`relationship.json`、`connection_guard.json`、
  token 文件、数据库、日志或真实 QQ 号。
- `.gitignore` 已排除常见私有文件；`static_check.py` 会检查密钥和 QQ 号模式。
- 任何真实部署都应按最小权限运行，并自行评估 QQ 自动化是否符合平台条款。

## 版权与免责声明

- 本仓库只提供框架代码和示例，不包含任何作品角色的台词、
  人设文本、图片或音频素材。
- 角色名称、人设和素材的权利归各自权利人所有；使用者需自行取得授权。
- 本项目与 AstrBot、NapCat、腾讯及任何作品权利人无隶属或背书关系。
- 使用本项目产生的账号风险、平台条款风险和数据合规责任由使用者自行承担。

## License

MIT，见 [LICENSE](LICENSE)。
