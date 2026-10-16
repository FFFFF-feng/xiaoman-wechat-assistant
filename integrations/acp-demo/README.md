# 小满 ACP / acpx 隔离演示

这个 demo 验证“其他服务调用一个 ACP Agent，然后拿到可回复给用户的文字”。不加载现有小满数据库、绑定、微信授权或线上 Gateway。请求字段与现有 Python 微信桥接相同，身份只允许合成的 `demo-bot/Alice`、`demo-bot/Bob`。

```text
模拟微信文字 → localhost HTTP（自建包装层）→ acpx CLI
                                                ↓ stdio / ACP v1
                           离线协议样例 或 Codex ACP adapter → Codex App Server
```

HTTP 是这个 demo 提供的包装接口，ACP 仍走 stdio JSON-RPC；不是让微信直接支持 ACP。

建议输入与 Python 共用仓库中的 `skills/xiaoman-natural-chat/references/chat-policy.txt`，并携带参考时钟及上海时区。运行时需要保留该目录；离线样例的回答仍是固定文本，不能用它验证模型语气。当前12项测试，真实Codex另有两次本机成功验证。

## 运行

需要 Node >=22.13。独立依赖均已固定并生成 lockfile：acpx `0.19.5`、ACP TypeScript SDK `1.8.0`、Codex ACP adapter `2.2.2`。没有更新项目生产 OpenClaw 或全局插件。

在本目录执行：

```powershell
npm ci --ignore-scripts --no-audit --no-fund
npm test
npm run demo
```

`demo` 启动随机的 `127.0.0.1` 端口，生成仅在内存中使用的 Bearer token，通过 HTTP 发送一条合成请求，核对重复请求，然后关闭接口。离线模式使用真正的 acpx 客户端和 ACP SDK Agent，但回答是固定样例，**不是大模型效果验证**。

```json
{"account_id":"demo-bot","sender_id":"Alice","message_id":"synthetic-1","content":"中午提醒我取快递，先给一个建议和两个选项"}
```

响应中 `task_write:false`、`wechat_send:false`：只做建议，不创建实际提醒，不投递微信。JSON 字段不能指定命令、工作目录、模型、MCP server 或用户身份之外的运行配置。

## 尝试真实 Codex

需要本机已经完成 Codex 登录或配置 Codex 运行时认证；`codex login status` 可检查。本演示不会自动登录、打印密钥或重新配置登录。真实模式保留宿主现有的 `OPENAI_BASE_URL`、`OPENAI_API_KEY`/`CODEX_API_KEY` 等原生路由配置，不加载小满的 `.env`，也不把项目 `MODEL_API_KEY`/百炼或微信密钥交给 Codex。

当前宿主WebSocket路径曾连接被拒绝。演示现在只读宿主TOML，对本次会话禁用WebSocket、关闭请求/流自动重试，流空闲上限20秒，总推理进程预算45秒（超时后等待其进程清理）。保留原模型及认证，不改全局文件；短建议采用低推理强度。`smol-toml`解析依赖固定1.9.1。本机真实Codex已两次成功，第二次`elapsed_ms:14602`，详见[超时修复](../../docs/ACP超时排查与修复_2026-10-16.md)。

```powershell
# 可选：使用已安装的 Codex 可执行文件；不填则用适配器随包的兼容版本。
$env:CODEX_PATH = (Get-Command codex.exe).Source
npm run demo:codex
```

此命令会使用现有 Codex 的登录、默认模型及用量；真实请求只有脚本内的合成文字。适配器使用 `read-only` 初始模式；acpx 不提供客户端文件/终端能力，并拒绝工具权限请求。它不是 OS 隔离容器，外部 Agent 的原生文件及工具行为仍需由其自身和部署环境约束，不应直接把此 demo 开放给真实用户。

acpx 使用临时 HOME 与独立 `.acpxrc.json`、空 MCP 配置；真实 Codex 通过原来的 `CODEX_HOME` 复用登录，可能按其自身规则保存合成会话。临时工作目录位于系统 Temp，服务去重状态只在内存，重启会失效；这不等于生产的持久用户会话或数据删除实现。

## 已覆盖的工程边界

- Bearer 鉴权与固定合成允许名单；非法身份、额外字段及超长文字拒绝。
- 每个身份独立目录；每次请求新建一次性 ACP 会话，不共用聊天历史。
- 同一消息 ID 和正文复用结果；正文冲突拒绝。同一人同时新请求返回 busy，两位用户可分别执行。
- 只取 `agent_message_chunk` 文本和成功结束信号；不返回思考、工具输出或原始供应商错误。
- 超时、不完整回复、权限拒绝视为失败，不自动重试；Windows 通过本次子进程 PID 清理该进程树。
- 只允许100个去重条目，达到容量拒绝，避免为了腾空间而重复执行。

本目录是独立实验。生产微信插件、Python聊天接口、日程与提醒发送逻辑均未切换到 ACP。文档与验证结论见项目内 [ACP 接入验证](../../docs/ACP接入验证_2026-10-16.md)。
