# 微信插件与小满后端连接器

版本 0.1.0；固定验证环境：OpenClaw 2026.10.1、腾讯微信插件 2.4.10、Node 24.21.0。升级宿主或渠道插件前需重新回归。

## 实现

```text
微信私聊 → 腾讯插件 → before_dispatch → Python 内部鉴权接口
                                         ↓
                              小满意图解析 / 聊天 / 任务数据库
                                         ↓
微信回复 ← 腾讯插件 ← handled:true,text ← 小满回复
```

连接器使用宿主支持的 `before_dispatch` 消息拦截接口，不修改腾讯插件或 OpenClaw 源码。原始文字直接进入 `handle_chat_message`，由 Python 小满调用百炼；不让 OpenClaw 再调用另一层大模型。

新增语音：`message_received`只缓存精确身份下的可信媒体事实，`before_dispatch`将单个本地WAV交给Python私有语音接口，百炼识别后进入同一意图逻辑。原生已有转写不重复付费识别。失败不创建任务、不回退默认Agent；手机整链路待验收，详见[语音说明](../../docs/VOICE.md)。

| 入口 | 作用 |
| --- | --- |
| `POST /api/internal/clawbot/chat` | 鉴权、绑定校验、去重、聊天与任务操作 |
| `GET /api/internal/clawbot/health` | 鉴权后的实例检查，不返回凭据或用户身份 |
| `POST /api/internal/clawbot/outbox/claim` | 领取一条持久提醒，验证绑定与任务状态 |
| `POST /api/internal/clawbot/outbox/{id}/start` | 领取凭据校验、原子进入发送中；阻止重复发送 |
| `POST /api/internal/clawbot/outbox/{id}/result` | 回写平台受理、失败或结果不确定 |
| `app/clawbot_identity.py` | 按绑定账号与发送者生成稳定、独立的用户身份 |
| `ClawbotMessage` | 独立历史、处理状态和去重缓存 |
| `/join`、`/api/join/*` | 邀请认证、本人扫码、配对数字、取消和撤销；不允许自选微信身份 |
| `/api/internal/clawbot/enrollment/*` | 仅本机管理员签发邀请及 worker 租约/可信绑定确认 |
| `enrollment-worker.mjs` | 使用官方 CLI 登录，多账号原子追加，不直接调用私有微信协议 |

身份来自宿主上下文，不能从消息正文或模型输出指定。后端再次校验绑定，任务不与公众号 OpenID 或本地模拟用户自动合并。相同消息 ID 的重复投递只返回原结果；处理失败或崩溃导致结果不确定时不自动重做，用户须先查询任务。

微信插件 2.4.10 未设置 `SenderId`，但其 `From`、`To` 和 `OriginatingTo` 都是同一私聊用户。宿主将该身份映射为 `conversationId`，连接器仅在 `senderId` 缺失时使用该可信字段；仍须匹配实际绑定，存在身份冲突时拒绝。不从消息正文、会话键字符串或默认用户推断身份。每位绑定用户每分钟最多接受 30 条新消息，重复投递不消耗额外额度。

## 配置与运行

插件配置仅包含 `backendUrl`、`tokenFile`、`bindingsFile`。`backendUrl` 固定为 `http://127.0.0.1:18083`；鉴权令牌和绑定身份只存在私有运行目录，不写入源码。内部接口禁止通过原公网路径白名单网关访问。

本机 Windows 的插件热更新遇到符号链接权限 `EPERM`；源码变更后使用停止/启动网关加载，不开启系统开发者模式或绕过权限检查。插件实际注册情况须通过 `plugins inspect xiaoman-bridge --runtime --json` 检查；不带 `--runtime` 的清单不能代替实时钩子检查。

多人版本保留现有绑定，每人由官方 CLI 确认后追加。开启 `CLAWBOT_ENROLLMENT_ENABLED=true` 并单独运行一个扫码 worker；默认最多 10 个绑定。宿主必须配置 `session.dmScope=per-account-channel-peer`，控制页与 Agent 工具保持关闭。扫码 worker 使用回环 `18795` 作为无 API 的独占锁，拒绝重复进程；后台需要单个 Python worker。管理与操作步骤见 [多人接入](../../docs/USER_ACCESS.md)。

公司Linux新部署使用仓库内 `scripts/setup-clawbot.py init` 生成独立私有配置；安装官方插件后 `activate` 启用本源码插件路径与会话读取许可。四个systemd模板和完整操作见 [Linux部署](../../docs/DEPLOYMENT.md)。已有本机私有启动器保留，不直接搬到Linux。保持关闭Agent工具/浏览器/自动执行、无默认大模型回退的安全基线。

使用本机 `Start-Clawbot.cmd` 准备 Python 服务、微信网关与仅供日程使用的临时 HTTPS 隧道，`Stop-Clawbot.cmd` 停止经进程归属验证的服务。无开机自启，不改动公司服务器。隧道不可用时聊天和提醒仍可运行，日程链接暂不可用。

主动提醒由 Python APScheduler 到期入队，`ClawbotDelivery.reminder_id` 唯一；Gateway 内 `xiaoman-reminders` 服务每 5 秒领取一次，通过宿主的 `runtime.channel.outbound.loadAdapter('openclaw-weixin').sendText(...)` 投递。微信凭据、上下文和平台调用仍由腾讯插件管理；不读私有登录令牌，不直接调用未公开接口，也不开放 Agent 定时任务或系统工具。

队列状态：`pending → claimed → sending → accepted / failed / unknown`。未开始发送的领取超过 45 秒可恢复；开始发送后超过 120 秒记为不确定，不自动重发。发送前再次核对任务未完成、未取消、未改期及绑定有效。默认超过计划时间 5 分钟记为 `missed`，重启后不批量补发旧提醒。`accepted` 仅代表平台受理，不代表手机已收到；本次一分钟提醒已由本人确认实际收到。

## 能力与限制

- 接入文字聊天、创建/查询/完成/取消/改期任务、个人历史；未说日期默认当天，过期时刻须澄清。
- 已实现主动提醒投递；须保持电脑、Python、Gateway 在线并联网。平台上下文失效或限流可能导致失败，隔夜、长时间未互动和断网仍需实测。关闭开关时保留原 `blocked/clawbot_not_configured` 行为，不能落到 mock 通道。
- 日程专用网关 `127.0.0.1:18084` 仅放行 `/schedule`、必要静态文件和受令牌保护的日程 API。临时 HTTPS 地址随启动生成、定期检查可用；不公开聊天、健康、配置、公众号回调或内部连接器接口。访问令牌保留 30 分钟有效期、哈希存储和用户隔离，不存聊天历史。
- ClawBot 日程回复采用 Markdown 超链接“点击打开我的日程”；本人已确认实际日程链接可单击并看到页面。不是原生网页卡片，不能用自己发送的网址可点击来推断机器人所有回复都可点击。
- 日程 HTML 合并样式和脚本，使用精确哈希 CSP，不开启 `unsafe-inline`。GET 网络/临时边缘错误最多尝试三次，鉴权失败不重试；完成/取消/延后 POST 不自动重发，结果未知时先重新加载确认。
- 本机隧道使用 `--protocol auto`，断线优先由 cloudflared 重连；进程退出按 5/10/20/40/80/120 秒退避重建，连续 5 分钟无边缘连接才重启。重建会换临时域名，旧链接不随之恢复。启动器区分后台恢复与监督进程退出，不再把冷启动超过 30 秒一律误报为停止。
- 已验证公网页面 200、无令牌日程 API 401、内部接口 404；手机端实际日程跳转已确认。临时隧道只用于联调，不是长期部署或可用性保证。
- 否定提醒与未实现的条件提醒有本地安全保护；模型断连或给出错误任务动作时也不执行。下一轮补时间不能绕过保护，“提醒我不要忘记……”仍可创建。复杂自然语言仍需积累测试，不承诺完整语义理解。
- 支持邀请制分别扫码授权的多人，默认10个绑定；用户反馈多位体验者功能正常。群聊、媒体和合并转发聊天记录尚未接入；隔夜稳定性、真人撤销、迁移和压力仍需验收。
- 超时不自动重试业务操作；内部 HTTP 成功仅代表小满处理成功，不等于微信最终送达。

## 回归

```powershell
# 在 Python 项目目录
.\.venv\Scripts\python.exe -m unittest discover -s tests

# 使用 Node 24，在此插件目录
node --test tests/*.test.js
```

2026-10-12最新离线回归：Python 134项、仓库Node 41项通过；此前仓库外隧道策略Node 4项通过。本人已确认既有手机聊天、待办、日程链接单击和提醒送达，用户随后反馈多位体验者功能正常；新增语音手机验收尚待确认。不将其写成逐人完整或公司部署验收。历史临时隧道曾断连/返回530，正式部署使用固定HTTPS。隔夜、断网和长期运行仍待验证。详见 [本地功能验收](../../docs/本地功能验收_2026-10-12.md) 与 [多人验收](../../docs/多人接入验收_2026-10-12.md)。

手机验收：发送“你是谁”、“两分钟后提醒我喝水”、“查询待办”、“打开日程”。确认实际收到到期提醒、日程仅显示本人任务；再用临时测试任务验证完成、取消和延后。不要转发日程链接、真实聊天截图或私有状态目录。
