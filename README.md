# 小满 Xiaoman · 微信 AI 日程助理

一个以微信聊天为入口的轻量个人助理：用自然语言创建和管理日程，以“小满”的陪伴式对话降低记录任务的门槛，并在平台允许的条件下发送提醒。

**版本：0.2.0｜更新：2026-10-13｜阶段：ClawBot 邀请制多人，Linux独立部署与两位手机基础验收通过。**

> 当前主要使用微信 ClawBot：邀请制多人接入、文字聊天、任务管理、主动提醒和手机日程。Linux独立部署已运行，迁移后用户确认聊天、固定域名日程、语音创建提醒与本人送达正常，第二位查询/日程没有串人。本机语音查询也已确认。其他语音操作、多人语音隔离、隔夜稳定性、真人撤销、恢复演练和逐人完整验收仍待完成。公众号后端保留，不修改售后服务号；图片、文件和合并聊天记录未实现。

**分享代码不等于分享已上线的微信机器人。** `/join`是统一接入页，每人领取自己的30分钟邀请并扫码授权，无需安装代码或操作服务器。默认最多10个绑定，不开放匿名注册。管理员先部署完整服务，再签发邀请；不要把127.0.0.1、私有目录或一个人的日程链接分享给所有人。

## 怎么运行、怎么给别人用

| 你要做什么 | 入口 |
| --- | --- |
| 无微信凭据，本机模拟体验 | 本文“本地快速开始” |
| Linux安装、启停、HTTPS、迁移、备份 | [Linux部署与运维](docs/DEPLOYMENT.md) |
| Linux实际运行与手机验收结果 | [2026-10-13服务器验收](docs/Linux部署验收_2026-10-13.md) |
| 生成邀请、手机扫码、功能示例、撤销 | [用户接入与管理员操作](docs/USER_ACCESS.md) |
| 初始化独立私有配置 | [setup-clawbot.py](scripts/setup-clawbot.py)，只用于新目录 |
| 常驻后台、重启恢复模板 | [四个systemd服务](deployment/linux) |
| 名称与头像能否自动改变 | [小满品牌与微信资料边界](docs/BRANDING.md) |
| 微信语音与意图识别 | [语音配置、限制与验收](docs/VOICE.md) |

公司部署顺序：独立账号/目录与固定HTTPS域名 → 安装Python与固定版本Node/OpenClaw/腾讯插件 → 初始化并激活桥接 → 启动四个服务 → 私密迁移或本人扫码 → 手机验收 → 小范围邀请。**Python单独启动不能接通微信。** 不将本机和服务器的同一微信授权同时运行。

服务器生成邀请示例（实际路径、域名须与部署一致）：

```bash
/srv/xiaoman/app/.venv/bin/python /srv/xiaoman/app/scripts/manage-clawbot.py invite \
  --token-file /srv/xiaoman/runtime/state/xiaoman/bridge.key \
  --label '体验者A' --public-url https://schedule.example.com \
  --output /srv/xiaoman/runtime/state/xiaoman/invite-A.json --show-link
```

终端直接显示完整专属链接；只发给这一位用户，每加一人重新生成。用户本人打开→同意→微信扫码/输入配对数字→回ClawBot发“你好，小满”。授权完成还需要手机实际收到回复验证。密钥、数据库、授权、邀请和日程令牌均不随源码发布。

## 项目定位

小满尝试把“想到一件事 → 记录下来 → 到时行动”串成一段自然对话，而不是让用户学习复杂的日程表单。

首版场景：日常待办、明确时间的提醒、任务查询与调整、轻量陪聊。语音查询待办、创建提醒与本人到期送达已获手机确认，其他语音操作待补测。企业知识库、长期记忆、条件触发和多端同步属于候选方向，**当前未实现**。

```text
两分钟后提醒我喝水
下午五点提醒我收拾出门
明天上午十点提醒我提交周报
今天有哪些任务？
把周报延后到明天下午三点
取消喝水任务
打开日程
你好小满，今天想随便聊聊。
```

未写日期但给出时刻时默认今天；今天该时刻已过去则追问，不擅自改成明天。缺时间、任务不明确或条件提醒无法执行时要求补充信息，不假称已经创建任务。

## 功能与开发进度

| 模块 | 当前实现 | 验证情况与边界 |
| --- | --- | --- |
| 自然语言日程 | 创建、查询、完成、取消、改期；多轮补全与默认当天 | 有离线测试；复杂表达仍需积累案例 |
| AI 对话 | OpenAI 兼容协议；已联调百炼；模型路由＋规则校验/兜底 | 历史本地真实调用成功；慢响应仍影响微信回复 |
| 本地页面 | 聊天、任务列表、任务操作和模拟提醒 | 无微信凭据也可运行；模拟提醒不发送到手机 |
| 微信回调 | Token 验签、XML、明文/兼容/安全模式、AES、账号校验和排重 | 官方公开样例与离线测试通过；新号须重新实测 |
| 微信回复 | 被动加密回复、异步 ACK 后回复 | 被动有响应时限；异步需要发送权限 |
| 提醒渠道 | 模拟、客服、传统模板、订阅通知后端与资格检查 | 权限、真实模板和用户授权不能由代码自动获得 |
| 私人日程 | 短时个人令牌、用户/账号隔离、任务操作 | 无令牌或跨账号访问被拒绝；链接属于敏感凭据 |
| 调度与状态 | 到期检查、窗口/额度保护、漏发与失败状态；ClawBot 持久投递队列 | ClawBot 领取可恢复、发送不确定不自动重发；旧公众号调度恢复仍待增强 |
| 公网网关 | 只放行指定回调与个人日程路径 | 不公开聊天调试、配置、健康检查等内部接口 |
| ClawBot 连接器 | 消息拦截直达 Python、任务与历史隔离、持久提醒和日程短时链接 | 手机聊天/待办、单击日程和一分钟提醒通过；隔夜提醒及长期运行待验收 |
| 微信语音意图 | 原生转写或百炼WAV识别，回显原文、持久去重和原有意图保护 | 本人手机确认查询、创建提醒与到期送达；其他语音操作和多人语音隔离待测 |

2026-10-12 最新离线回归：**Python 134项、仓库Node 41项通过**，另有此前仓库外本机隧道策略Node 4项通过。新增语音13项、身份/开关配置2项、品牌说明1项Python及语音9项Node用例。覆盖多人隔离、提醒路由、首次扫码、初始化和邀请显示。手机/模型结果见 [本地功能验收](docs/本地功能验收_2026-10-12.md)；多人反馈见 [多人验收](docs/多人接入验收_2026-10-12.md)。

### 当前困难与上线阻塞项

1. **账号与权限**：公众号路线需核对实际可用账号、认证和发送权限；旧号曾出现 `48001`（接口未授权），不能用代码绕过。ClawBot 采用邀请制，各用户独立授权，不等于永久公共好友二维码。
2. **回复速度**：历史百炼调用单次约7.46秒；任务意图模型客户端仍有较长默认超时。正式号回调整体的严格短时预算尚未实现。
3. **跨窗口提醒**：客服消息不是无限主动通知。模板/订阅需要业务适配、真实权限及相应授权，窗口外送达须真实测试。
4. **授权入口**：已有订阅事件与发送后端，没有完整 H5 订阅按钮、OAuth 或 JS-SDK 签名流程。
5. **长期运行**：ClawBot 已有持久队列；监控告警、日志轮转、备份恢复、压力测试和依赖锁定仍待完善。电脑关机期间不能发送，默认超过 5 分钟的旧提醒不补发。
6. **ClawBot 日程与转发**：单击日程与短延时提醒已手机验证，但不等于隔夜或长期保证。临时 HTTPS 仍会抖动，正式部署需固定域名和托管环境；合并转发内容获取未实现，不能当作已支持。

## 功能结构与实现

当前ClawBot主路线：

```text
每人专属邀请 → /join → 官方扫码/配对 → 私有绑定允许列表
微信文字 → 腾讯插件 → before_dispatch连接器 → Python意图/任务/对话
                                               ↓
                                      按账号＋用户隔离SQLite
                                               ↓
                            到期任务 → 持久投递队列 → 腾讯插件发提醒
打开日程 → 短时个人令牌 → HTTPS白名单网关 → 本人任务与操作
```

扫码worker和微信Gateway为独立常驻进程，业务仍由本项目Python处理，不把用户消息转给OpenClaw默认Agent。平台接口受理与手机实际收到分别记录、验证。名称和头像只初始化项目后台，不会改微信联系人资料。

保留的公众号路线：

```text
微信消息 / 本地网页
    ↓
回调验签、解密、账号核对、排重
    ↓
意图识别：模型 + 规则解析 + 本地时间/操作校验
    ├── 闲聊 / 追问 → 对话回复
    ├── 日程操作 → SQLAlchemy + SQLite
    └── 打开日程 → 短时令牌 → 私人日程页

APScheduler 到期检查
    ↓
账号、关注状态、客服窗口、额度、模板 / 订阅授权检查
    ↓
渠道适配器 → 模拟 / 微信接口 → 发送结果与可关联回执
```

事件回调处理关注状态、订阅授权和通知回执，不启动 AI 陪聊。异步模式先 ACK 再处理业务；被动模式在回调响应中返回 XML，可按安全模式加密。

关键设计：模型不能绕过时间与发送资格校验；微信与模拟任务分离；用户与账号隔离；`accepted` 不等于 `delivered`；结果不明确时不盲目重发；公网入口采用路径白名单。

### 技术栈与目录

Python、FastAPI / Uvicorn、SQLAlchemy / SQLite、APScheduler、原生 HTML/CSS/JavaScript、OpenAI 兼容 SDK、HTTPX、Cryptography。

本机 Python 3.13 已验证；目标服务器 Python 3.12 的完整运行验收仍待执行。依赖范围见 [`requirements.txt`](requirements.txt)，目前不是完全锁定的依赖集合。

```text
app/
  main.py                     应用与本地聊天/任务 API
  chat_service.py             意图协调、上下文和任务执行
  parser.py / model_parser.py  规则时间解析与模型路由
  models.py / database.py      数据模型与 SQLite 增量迁移
  scheduler.py                到期调度与提醒状态
  wechat_router.py             公众号回调与回复
  wechat_crypto.py             消息签名与加解密
  wechat_notifications.py      窗口、额度、授权及回执
  schedule_access.py           私人日程访问令牌
  schedule_router.py           个人日程 API
  clawbot_router.py            微信插件内部桥接、消息身份与排重
  clawbot_bindings.py          私有允许列表、成员隔离与撤销
  clawbot_enrollment.py        邀请、扫码租约与成员管理
  clawbot_delivery.py          持久提醒投递、受理与不确定状态
  join_page.py                 手机邀请入口
  local_gateway.py             公网联调路径白名单
  adapters/                   渠道适配器
web/                          本地体验页与私人日程页
tests/                        时间、微信、安全和网关回归
scripts/                      启动、冒烟检查与发布检查
integrations/openclaw-xiaoman/ 微信连接器、扫码与提醒worker
deployment/                   独立Linux服务与HTTPS配置模板
docs/                         路线与发布说明
```

## 本地快速开始

### 安装与模拟配置

在新克隆的项目根目录运行。先关闭真实微信和模型，体验基本功能。

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
}
```

Linux：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
test -f .env || cp .env.example .env
```

已有 `.env` 不要覆盖。确认 `MODEL_ENABLED=false`、`WECHAT_ENABLED=false`。Windows 若缺少 `Asia/Shanghai` 时区数据库，可在虚拟环境中安装 `tzdata`。

### 启动本地体验

Windows PowerShell：

```powershell
Remove-Item -LiteralPath 'Env:XIAOMAN_ENV_FILE' -ErrorAction SilentlyContinue
$env:MODEL_ENABLED='false'
$env:WECHAT_ENABLED='false'
$env:WECHAT_DIALOGUE_ENABLED='false'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-local.ps1
```

Linux：

```bash
env -u XIAOMAN_ENV_FILE MODEL_ENABLED=false WECHAT_ENABLED=false WECHAT_DIALOGUE_ENABLED=false \
  .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 18080 --workers 1
```

- 体验页面：<http://127.0.0.1:18080/>
- 健康检查：<http://127.0.0.1:18080/health>
- 本地 API 文档：<http://127.0.0.1:18080/docs>

保留窗口，`Ctrl+C` 停止。已运行时不重复启动，不用多个 worker 或 `--reload` 运行调度器。`127.0.0.1` 不是其他人的体验地址，微信也无法直接回调它。模拟提醒显示在页面，不发手机；没有个人令牌时 `/schedule` 不能读取私人日程。

### 可选：接入自己的模型

只在私有 `.env` 中设置，模型名称填写平台当前实际可用的 ID：

```dotenv
MODEL_ENABLED=true
MODEL_API_KEY=你的模型密钥
MODEL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
MODEL_NAME=你实际可用的模型ID
```

这是百炼兼容接口示例；其他提供方使用其官方地址，不能把密钥发到不明接口。也可采用模板中的 `DASHSCOPE_API_KEY` 配置。

前面的启动命令显式关闭模型；开启时须在终端同步调整或移除 `MODEL_ENABLED` 并重启。默认 `.env` 受已有环境变量影响，仅刷新页面不会重新加载配置。调用可能收费，聊天内容会发送至所配置的提供方，使用真实资料前需确认授权和隐私要求。

## 接入微信与安全边界

从 [正式号模板](.env.official.example) 建立独立私有配置，填写自己的 AppID、AppSecret、原始 ID、Token、AESKey 和 HTTPS 地址。`example.com` 不是真实联调入口。

顺序：账号/认证/权限核对 → 受限 HTTPS 与出口白名单 → 私有配置 → 离线检查 → 握手与加密消息 → 即时回复 → 日程操作 → 到时提醒 → 跨窗口单独验收。

`scripts/run-official-local.ps1` 读取 `.env.official.local`，默认监听 `127.0.0.1:18082`。`app.local_gateway` 作为网关应监听18081，默认上游18082；公网隧道只连接网关，不直接连接主应用。新号使用独立配置和数据库；旧号 OpenID 和日程链接不能直接当作新号数据迁移。

```powershell
$env:XIAOMAN_ENV_FILE='.env.official.local'
.\.venv\Scripts\python.exe -m app.wechat_preflight
# 正式服务与受限公网入口已就绪后，才做联网检查：
.\.venv\Scripts\python.exe -m app.wechat_local_check --check-token
```

离线检查不联网；联网检查验证回调并请求令牌，不向用户发送测试消息。它们不能单独证明发送权限或送达。普通用户的 IP 不必逐人加入 API 白名单。

生产部署只修改本项目独立域名配置，不修改其他项目兜底，不设全局 `default_server` 指向小满。`screen` 不是自动恢复或监控系统。公网调试、健康检查和配置路径应被拒绝；主应用含宽松 CORS 与本地开发接口，**不能直接暴露整站**。

见 [发布与安全检查](docs/PUBLISHING.md)。微信规则以接入时官方文档、实际权限和测试结果为准。

## 测试与验收

**2026-10-12：Python 134项、仓库Node 41项离线测试通过。** 另有此前仓库外隧道策略4项通过。真实模型与手机结果另行记录，不与模拟用例混算。

使用内存数据库，关闭真实发送和模型：

```powershell
Remove-Item -LiteralPath 'Env:XIAOMAN_ENV_FILE' -ErrorAction SilentlyContinue
$env:DATABASE_URL='sqlite://'
$env:MODEL_ENABLED='false'
$env:WECHAT_ENABLED='false'
$env:WECHAT_DIALOGUE_ENABLED='false'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

```bash
env -u XIAOMAN_ENV_FILE DATABASE_URL=sqlite:// MODEL_ENABLED=false \
  WECHAT_ENABLED=false WECHAT_DIALOGUE_ENABLED=false \
  .venv/bin/python -m unittest discover -s tests -v
```

测试后不要直接用这组环境变量启动正式业务。测试通过不等于真实微信/模型通过。当前有 Starlette TestClient 弃用警告，依赖兼容升级列入维护计划。

正式验收至少包含：本人手机收发、真实提醒、日程操作、撤回授权、无令牌拒绝、账号隔离、窗口外通知、重启和断网。API接受与最终送达分别记录。

## 开发路线与规划

以下是候选里程碑，不是承诺发布日期；根据真实权限和用户反馈调整。

| 阶段 | 目标 | 完成标准 |
| --- | --- | --- |
| 当前0.2.x：原型 | 聊天、日程、回调、邀请制多人、语音意图、提醒资格检查 | Linux134项Python＋41项Node回归及迁移后两位手机基础验收通过；其他语音、恢复演练和长期运行待完成 |
| P0：正式号闭环 | 注册/认证/权限、回复耗时、真实提醒与日程 | 手机端全链路通过，权限/超时错误可解释 |
| P1：稳定运行 | 持久队列、状态恢复、监控、备份、依赖锁定、自动回归 | 重启/断网不重复发送，备份恢复演练通过 |
| P1：跨窗口通知 | 真实模板、授权入口与撤回流程 | 权限与模板匹配，真实窗口外送达 |
| P2：智能增强 | 偏好记忆、任务拆解、语音确认/纠错、知识库等候选能力 | 每项有独立评测、授权、开关与回滚 |
| P3：多端产品化 | 渠道适配、角色权限、管理工具和服务指标 | 多租户隔离、成本质量可观测、运维边界明确 |

详细任务见 [开发路线与创新规划](docs/ROADMAP.md)。先解决“权限＋响应耗时＋真实提醒验收”，再扩展。

## 后续创新与拓展

以下均为**候选规划，未完成**：

| 方向 | 用户价值 | 前提与验收 |
| --- | --- | --- |
| 可纠正的结构化记忆 | 记住用户同意的作息/偏好，减少重复说明 | 可查看、编辑、删除；不把模型猜测作为事实 |
| 大任务拆成下一步 | 把“准备周报”转为可确认的小步骤 | 用户确认后再建任务，不自动批量提醒 |
| 自适应提醒节奏 | 安静时段、合并提醒、延后方式 | 显式授权、可关闭，遵守窗口和额度 |
| 语音到日程增强 | 已有转写与意图代码，后续加入操作前确认/纠错 | 查询、创建提醒与本人送达已手机确认；其他语音场景待测。回显不等于操作前确认，不默认长期存储音频 |
| 日程风险提示 | 提示冲突、临近截止和当天负担 | 可解释规则，不擅自移动或删除任务 |
| 企业知识问答＋待办 | 在授权知识库找答案，再由用户决定是否建任务 | RAG出处、权限隔离、提示注入防护，独立验收 |
| 可解释的提醒轨迹 | 看清为何创建、为何没发、进行到哪一步 | 区分权限、窗口、网络和送达，不泄露他人数据 |
| 多渠道日程助理 | 复用业务层适配不同入口 | 能力矩阵、授权绑定，不擅自合并身份 |

评价重点：创建准确率、误操作率、回复延迟、合规提醒可靠性、任务完成体验和可控成本，而非功能数量。

## 文档与协作

- [用户接入与统一入口规划](docs/USER_ACCESS.md)：别人怎么使用、当前限制和多人开发项。
- [Linux部署与运维](docs/DEPLOYMENT.md)：配置初始化、官方插件安装、systemd、HTTPS、邀请、迁移、备份、启停与验收；实际运行结果见[服务器验收](docs/Linux部署验收_2026-10-13.md)，不将模板或短时检查当作长期稳定性保证。
- [小满名称与头像](docs/BRANDING.md)：本地Agent身份和微信联系人资料分别说明，不承诺不存在的自动改名接口。
- [本地功能验收](docs/本地功能验收_2026-10-12.md)：自动测试、真实模型和手机确认分开记录。
- [ClawBot 配置模板](.env.clawbot.example) / [连接器实现](integrations/openclaw-xiaoman/README.md)。
- [开发路线与创新规划](docs/ROADMAP.md)：阶段、优先级、候选创新和评测。
- [发布与安全检查](docs/PUBLISHING.md)：上传范围、凭据与数据保护。
- [本地配置模板](.env.example) / [正式号配置模板](.env.official.example)。
- 官方入口：[服务号开发](https://developers.weixin.qq.com/doc/service/)、[客服消息](https://developers.weixin.qq.com/doc/service/api/customer/message/api_sendcustommessage)、[消息加密](https://developers.weixin.qq.com/doc/service/guide/dev/push/encryption.html)、[订阅通知](https://developers.weixin.qq.com/doc/service/api/notify/notify/api_sendnewsubscribemsg)。链接不代表已获得权限。

反馈时附脱敏输入、预期结果、实际行为、复现步骤和环境。不要上传密钥、访问令牌、完整私人日程链接、用户聊天、数据库和公司内网信息。提交改动需补测试并说明隐私/权限影响；历史内部交接记录不随仓库发布。

**许可证与权属**：目前未指定开源许可证。公开开源或授予商用权限前，由项目负责人确认代码权属、第三方许可和可公开范围；公开仓库本身不等于授予任意使用许可。
