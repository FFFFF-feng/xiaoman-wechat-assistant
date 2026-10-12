# ClawBot 服务器部署交接

更新：2026-10-12。本文面向部署人员，是操作清单，不是已验证的服务器安装器。公司服务器尚未部署本轮 ClawBot 版本，现有本机双击脚本不能直接搬到 Linux 使用。

## 部署目标与边界

- 部署独立小满服务，不改已有售后服务号回调、菜单或业务。
- Python、OpenClaw/腾讯插件及提醒投递服务必须同时在线；仅部署 Python 不会接通微信。
- 原型按单 Python 进程、单调度器和单提醒 worker 运行，不启用 `--reload` 或多个副本。
- 只向公网提供受限日程入口。`18083`、`18084`、Gateway 和管理入口保留回环监听，不直接开放公网端口。
- 多人统一入口是下一阶段开发目标，当前部署不会自动获得公开注册能力，详见 [用户接入与多人规划](USER_ACCESS.md)。

## 部署人员需要准备

1. 授权的服务器登录方式、系统版本、独立服务账号、安装目录和端口。
2. 固定 HTTPS 日程域名、证书和独立反向代理配置；示例域名不可直接使用。
3. Python 运行时、Node.js，以及本轮验证的 OpenClaw `2026.10.1` 和腾讯插件 `2.4.10`。Python 依赖范围见 `requirements.txt`，服务器安装后应锁定实际版本再验收。
4. 独立百炼密钥、内部桥接密钥、私有绑定文件和数据库存储目录。凭据通过私下安全渠道交付，不发 GitHub、群聊或文档。
5. 本机数据是否迁移的明确选择；绑定标识变化会影响任务归属，不在无备份的情况下重建账号或覆盖数据库。

## 安装与私有配置

```bash
git clone https://github.com/FFFFF-feng/xiaoman-wechat-assistant.git
cd xiaoman-wechat-assistant
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

从 `.env.clawbot.example` 建立私有配置，填写模型密钥、同一份桥接密钥、绑定文件路径和实际日程域名。生产配置不要和旧公众号 `.env` 混用。

`bindings.json` 是私有允许列表，元素为实际登录返回的 `account_id` 和扫码者 `sender_id`。不要用昵称、手机号码、消息正文或猜测的 ID 代替。当前仓库没有自动生成多人绑定的脚本，部署人员须核对真实登录结果；不能直接删除允许列表检查或开放所有发送者。

内部桥接密钥应由安全随机数生成。Node 连接器的 `tokenFile` 与 Python 的 `CLAWBOT_BRIDGE_TOKEN` 必须一致；模型密钥与微信登录凭据互不替代。配置、密钥文件和数据库目录只允许服务账号及授权管理员访问。

## 启动与连接器

以下展示已实现的两个 Python 入口，环境文件路径为示例。分别在独立进程中运行，并在上线前转换为经过验证的常驻服务：

```bash
XIAOMAN_ENV_FILE=/etc/xiaoman/clawbot.env .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 18083 --workers 1 --no-access-log
```

```bash
XIAOMAN_ENV_FILE=/etc/xiaoman/clawbot.env .venv/bin/python -m uvicorn app.schedule_gateway:app --host 127.0.0.1 --port 18084 --workers 1 --no-access-log
```

OpenClaw 使用独立运行配置和状态目录，不复用公司其他机器人的实例。按 [腾讯插件说明](https://github.com/Tencent/openclaw-weixin/blob/main/README.zh_CN.md) 安装指定版本并登录微信；本仓库连接器入口为 `integrations/openclaw-xiaoman/`。

连接器配置含 `backendUrl=http://127.0.0.1:18083`、私有 `tokenFile` 和 `bindingsFile`。启用 `before_dispatch` 的会话访问，以及 `xiaoman-reminders` 服务。默认模型不得作为桥接失败时的聊天回退；保持 Agent 工具、浏览器和自动执行任务关闭，不把小满部署成远程系统操作代理。配置与运行验证见 [连接器说明](../integrations/openclaw-xiaoman/README.md)。

微信 Gateway 使用令牌鉴权并监听回环地址。安装后的账号、运行钩子和提醒服务必须检查，不能只以插件文件存在或登录成功作为接通证明。服务器常驻、重启策略和日志轮转尚未打包为本仓库安装器，需部署人员补齐并实测。

## 公网、备份与验收

反向代理只转发到日程专用网关 `127.0.0.1:18084`，由该网关白名单限制路径；不直接代理整个主应用或 Gateway。保持 Authorization 头传递，禁用私人日程缓存，不在访问日志记录凭据。

正式启用前：

- `/schedule` 可访问，无授权日程 API 返回 401，内部聊天/健康/调试路径不向公网开放。
- 本人真实微信聊天、创建任务、到期提醒、单击日程、完成/取消/改期通过。
- API `accepted` 和本人实际收到分别验证。
- 保存数据库与私有运行状态的可恢复备份，并实际演练；在线 SQLite 备份必须一致，不能只复制正在写入的主文件。
- 重启后恢复服务、断网状态可解释、过期任务不集中补发、发送不确定不重复发送。
- 记录实际依赖与 Git 提交，具备旧版本及数据库恢复方案，不用自动更新替代发布验收。

当前本机结果见 [功能验收](本地功能验收_2026-10-12.md)。正式服务器验收通过前，不发“已上线”通知或大范围用户邀请。
