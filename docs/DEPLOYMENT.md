# Linux 部署与运维

更新：2026-10-12。当前主路线为微信ClawBot，不修改公司已有公众号/售后服务号。用户已反馈多位体验者能正常使用功能；Linux配置已准备，**尚未在公司服务器完成运行验收**。本机双击脚本不能直接搬到Linux使用。

## 部署目标与边界

- 部署独立小满服务，不改已有售后服务号回调、菜单或业务。
- Python、OpenClaw/腾讯插件及提醒投递服务必须同时在线；仅部署 Python 不会接通微信。
- 原型按单 Python 进程、单调度器和单提醒 worker 运行，不启用 `--reload` 或多个副本。
- 只向公网提供受限日程入口。`18083`、`18084`、Gateway 和管理入口保留回环监听，不直接开放公网端口。
- 邀请制多人入口已本机真人体验，不开放匿名注册；服务器仍需独立验收，详见 [多人接入与管理](USER_ACCESS.md)。

## 部署人员需要准备

1. 授权的服务器登录方式、系统版本、独立服务账号、安装目录和端口。
2. 固定 HTTPS 日程域名、证书和独立反向代理配置；示例域名不可直接使用。
3. Python 运行时、Node.js，以及本轮验证的 OpenClaw `2026.10.1` 和腾讯插件 `2.4.10`。Python 依赖范围见 `requirements.txt`，服务器安装后应锁定实际版本再验收。
4. 独立百炼密钥、内部桥接密钥、私有绑定文件和数据库存储目录。凭据通过私下安全渠道交付，不发 GitHub、群聊或文档。
5. 本机数据是否迁移的明确选择；绑定标识变化会影响任务归属，不在无备份的情况下重建账号或覆盖数据库。

## 1. 路径与组件约定

| 项目 | 本文使用的路径/版本 |
| --- | --- |
| 服务账号 | `xiaoman`，非root |
| 新源码目录 | `/srv/xiaoman/app` |
| 私有运行目录 | `/srv/xiaoman/runtime`，不在Git中 |
| 固定HTTPS | `https://schedule.example.com`，必须替换为实际域名 |
| Python | 3.12/3.13；本机3.13通过，Linux版本须实测 |
| Node / OpenClaw / 微信插件 | 24.21.0 / 2026.10.1 / 2.4.10 |

先读第7节选择全新部署或迁移。**不在旧项目目录原地覆盖**，不覆盖旧 `.env` 或数据库。先核对内存、磁盘、版本和端口是否空闲，Node须在systemd的PATH中可用。

```text
微信 ←→ Gateway:18793 ←→ Python:18083（业务、调度、SQLite）
                          ↑
                enrollment worker（官方扫码、18795单例锁）
手机HTTPS → Nginx:443 → 白名单网关:18084 → Python:18083
```

18083、18084、18793、18795只监听127.0.0.1，不向公网开防火墙；不公开Python调试首页和OpenClaw控制台。

## 2. 安装源码和私有配置

管理员先准备服务账号及其可写的 `/srv/xiaoman`。若同名账号/目录已存在，核对归属，不重复创建或修改其他业务。以下命令以服务账号执行，不用root运行应用：

先检查，不修改现有内容：

```bash
getent passwd xiaoman
ls -ld /srv/xiaoman
python3 --version
node --version
npm --version
```

仅确认账号和目录都不存在、且公司允许创建专用账号时，管理员执行下面两行。若已存在，先人工核对，不照搬执行，也不递归改变旧目录归属：

```bash
sudo useradd --system --create-home --home-dir /srv/xiaoman --shell /bin/bash xiaoman
sudo -iu xiaoman
```

Python/Node/npm缺失或版本不符时，按公司的软件安装方式准备指定版本，再继续；不要将当前本机Windows运行时复制到Linux。安装完源码/配置后退出服务账号终端，由管理员安装systemd与Nginx配置。

```bash
git clone https://github.com/FFFFF-feng/xiaoman-wechat-assistant.git /srv/xiaoman/app
cd /srv/xiaoman/app
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install tzdata

# 替换示例域名。仅初始化新的空目录；已有运行目录不能再次init。
.venv/bin/python scripts/setup-clawbot.py init \
  --root /srv/xiaoman/runtime --public-url https://schedule.example.com
```

初始化生成独立桥接密钥、网关密钥、空允许列表、私有 `runtime.env`、安全基线与本地“小满”身份。不会扫码、启动进程、导入旧公众号凭据或自动添加用户；非空目录拒绝初始化，不能通过删除旧目录解决迁移问题。

用服务器编辑器打开 `/srv/xiaoman/runtime/state/xiaoman/runtime.env`，私下设置模型：

```dotenv
MODEL_ENABLED=true
MODEL_NAME=qwen3.8-flash
MODEL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
MODEL_API_KEY=填写自己的百炼兼容接口密钥
```

默认关闭模型，避免缺密钥时误认为正常；模型ID以实际账号能力为准。保留生成的桥接密钥和绝对路径，固定域名正确，`CLAWBOT_PUBLIC_URL_FILE`保持空。密钥不要贴入Git、群聊、截图或终端历史。

`bindings.json` 是私有允许列表，元素为实际登录返回的 `account_id` 和扫码者 `sender_id`。新增成员由邀请扫码 worker 在官方 CLI 确认后原子追加，既有绑定不覆盖、被撤销者不自动恢复。不要用昵称、消息或猜测的 ID 代替真实身份，不能开放所有发送者。

内部桥接密钥应由安全随机数生成。Node 连接器的 `tokenFile` 与 Python 的 `CLAWBOT_BRIDGE_TOKEN` 必须一致；模型密钥与微信登录凭据互不替代。配置、密钥文件和数据库目录只允许服务账号及授权管理员访问。

## 3. 安装官方插件并启用小满

使用独立npm目录，不修改公司全局OpenClaw。以下仍以服务账号运行：

```bash
npm install --prefix /srv/xiaoman/runtime/npm --save-exact openclaw@2026.10.1 qrcode@1.5.4

export OPENCLAW_HOME=/srv/xiaoman/runtime/home
export OPENCLAW_STATE_DIR=/srv/xiaoman/runtime/state
export OPENCLAW_CONFIG_PATH=/srv/xiaoman/runtime/state/openclaw.json
export OPENCLAW_WORKSPACE_DIR=/srv/xiaoman/runtime/workspace

node /srv/xiaoman/runtime/npm/node_modules/openclaw/openclaw.mjs \
  plugins install @tencent-weixin/openclaw-weixin@2.4.10

/srv/xiaoman/app/.venv/bin/python /srv/xiaoman/app/scripts/setup-clawbot.py \
  activate --root /srv/xiaoman/runtime
node /srv/xiaoman/runtime/npm/node_modules/openclaw/openclaw.mjs config validate
```

`activate`保留官方插件安装记录、检查固定版本，不重新生成密钥或绑定；再次执行不会覆盖已激活配置。本地身份并不是微信端联系人资料，见 [名称与头像](BRANDING.md)。小满插件通过源码绝对路径加载，不随意移动目录；升级代码后重启微信网关。

安装命令必须正常完成再继续。若超时，先检查该独立目录的安装进程和官方安装清单，不盲目重复安装或删除目录；仅有package.json不代表完整安装成功。本机临时安装探针曾超时，后续安装产物激活及官方配置校验通过，Linux完整安装/常驻启动仍待实测。

多人由单个 `enrollment-worker.mjs` 串行调用官方CLI登录，默认10个绑定。初始化配置启用 `CLAWBOT_ENROLLMENT_ENABLED=true`，保持 `per-account-channel-peer` 会话隔离、关闭控制台与Agent工具。未开放匿名注册。

连接器配置含 `backendUrl=http://127.0.0.1:18083`、私有 `tokenFile` 和 `bindingsFile`。启用 `before_dispatch` 的会话访问，以及 `xiaoman-reminders` 服务。默认模型不得作为桥接失败时的聊天回退；保持 Agent 工具、浏览器和自动执行任务关闭，不把小满部署成远程系统操作代理。配置与运行验证见 [连接器说明](../integrations/openclaw-xiaoman/README.md)。

## 4. 四个systemd服务

模板位于 [`deployment/linux`](../deployment/linux)。先核对用户名、路径、Node PATH；非systemd系统须另选等效进程管理。管理员确认服务名称未被其他业务占用后安装：

```bash
sudo install -m 0644 /srv/xiaoman/app/deployment/linux/xiaoman-backend.service /etc/systemd/system/
sudo install -m 0644 /srv/xiaoman/app/deployment/linux/xiaoman-web.service /etc/systemd/system/
sudo install -m 0644 /srv/xiaoman/app/deployment/linux/xiaoman-weixin.service /etc/systemd/system/
sudo install -m 0644 /srv/xiaoman/app/deployment/linux/xiaoman-enrollment.service /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/xiaoman-*.service
sudo systemctl daemon-reload
sudo systemctl enable --now xiaoman-backend xiaoman-web xiaoman-weixin xiaoman-enrollment
```

Python仅一个worker，不用 `--reload` 或多副本。systemd的active不等于微信已连接；Gateway首次冷启动可能需要几分钟，不要重复启动或重扫。网关停止也可能需要数分钟排空，模板已设置360秒停止预算。

```bash
# 检查
sudo systemctl status xiaoman-backend xiaoman-web xiaoman-weixin xiaoman-enrollment --no-pager
curl --fail http://127.0.0.1:18083/health
curl --fail http://127.0.0.1:18084/join -o /dev/null
ss -ltnp
sudo journalctl -u xiaoman-backend -u xiaoman-weixin -u xiaoman-enrollment -n 100 --no-pager

# 改Python配置或业务代码
sudo systemctl restart xiaoman-backend
# 改桥接/扫码代码：先确认无人正在扫码
sudo systemctl stop xiaoman-enrollment
sudo systemctl restart xiaoman-weixin
sudo systemctl start xiaoman-enrollment

# 停止本项目，保留任务/授权，不停其他公司服务
sudo systemctl stop xiaoman-enrollment xiaoman-weixin xiaoman-web xiaoman-backend
```

日志只在授权终端查看，反馈前脱敏；官方插件日志可能含私人身份。日志轮转、磁盘告警、保留周期及恢复演练需按公司要求补齐，不因提供模板就当作完成运维验收。

## 5. 固定域名与HTTPS

[`deployment/nginx/xiaoman.conf.example`](../deployment/nginx/xiaoman.conf.example)只适合新的独立域名，需替换域名与证书路径。沿用旧域名时先核对现有server block、回调和业务：**不能新建重复server_name，也不能覆盖整个旧配置**。需要保留其他业务时，只调整小满 `/join`、`/schedule`、`/api/join/*`、`/api/schedule/*` 及必要静态路径到18084。

反向代理只转发到日程专用网关 `127.0.0.1:18084`，由该网关白名单限制路径；不直接代理整个主应用或 Gateway。保持 Authorization 头传递，禁用私人日程缓存，不在访问日志记录凭据。

```bash
sudo nginx -t
sudo systemctl reload nginx
# 替换成实际域名。依次预期200、401、401、404、404。
curl -o /dev/null -s -w '%{http_code}\n' https://schedule.example.com/join
curl -o /dev/null -s -w '%{http_code}\n' https://schedule.example.com/api/join/status
curl -o /dev/null -s -w '%{http_code}\n' https://schedule.example.com/api/schedule/tasks
curl -o /dev/null -s -w '%{http_code}\n' https://schedule.example.com/api/internal/clawbot/health
curl -o /dev/null -s -w '%{http_code}\n' https://schedule.example.com/docs
```

手机使用固定公网HTTPS，不使用127.0.0.1或临时trycloudflare域名。

## 6. 签发邀请与用户管理

在服务器私有终端以服务账号执行，每位用户分别签发一次：

```bash
cd /srv/xiaoman/app
.venv/bin/python scripts/manage-clawbot.py invite \
  --token-file /srv/xiaoman/runtime/state/xiaoman/bridge.key \
  --label '体验者A' --public-url https://schedule.example.com \
  --output /srv/xiaoman/runtime/state/xiaoman/invite-A.json --show-link
```

`--show-link`直接显示完整专属邀请；不加时仅保存私有JSON。30分钟有效，输出文件须为新文件，只把链接给该用户，不公开贴群或截图。本人打开→同意→扫码/配对数字→ClawBot测试；用户不用登录服务器。已绑定的人不要重复扫码。

```bash
.venv/bin/python scripts/manage-clawbot.py members \
  --token-file /srv/xiaoman/runtime/state/xiaoman/bridge.key
.venv/bin/python scripts/manage-clawbot.py revoke \
  --token-file /srv/xiaoman/runtime/state/xiaoman/bridge.key \
  --member 'clawbot:替换为members返回的成员哈希'
```

默认10人，`CLAWBOT_MAX_USERS`可调整，代码硬上限200，**不代表做过200人压力测试**。成员列表含worker健康。撤销拒绝新业务、未开始提醒并使旧日程失效，历史保留；不追回已发送消息，也不承诺微信官方侧解绑。详见 [使用与管理](USER_ACCESS.md)。

## 7. 从本机迁移已有授权和任务

保留授权迁移通常不需要所有人重新扫码。必须通过私密渠道传输，不上传GitHub；不直接复制Windows二进制、整个运行目录或带盘符配置。

1. Linux新目录完成第2～3节，停止目标四个服务；保留新密钥/配置。
2. 暂停邀请，等待本机在途扫码结束，停止本机worker、Gateway、Python。**同一授权不能两地同时轮询/发送**。
3. 私密备份/迁移：SQLite一致备份、`bindings.json`、`state/openclaw-weixin/`原生账号/context/sync，以及相关 `state/credentials/` 微信配对文件。任务和邀请在同一数据库中，先保存可恢复备份。
4. DB放到Linux `state/xiaoman/tasks.db`，绑定放到对应文件；使用Linux新生成的桥接/Gateway密钥。不复制Windows `openclaw.json`、`runtime.env`、进程PID或隧道文件。
5. 文件归属恢复给服务账号；私有目录700、凭据600。不要从原生账号重建允许列表，否则会重新放行被撤销者。检查固定域名和新路径。
6. 只启动Linux服务，逐位发新消息验证聊天、任务、提醒和日程。旧链接须重新发“打开日程”，不保证旧短时令牌/旧域名可迁移。

选择全新授权时保留原DB备份，新账号ID不会自动继承旧任务；当前没有账号轮换后的任务自动迁移工具。已进入sending/unknown的提醒不得盲目补发；默认超计划5分钟的旧提醒不集中补发。

## 8. 备份、更新和上线验收

首次切换前备份数据库、授权、允许列表和私有配置。在线SQLite不能只复制主文件；使用SQLite备份机制，或本项目全部写入进程停止后备份完整状态。备份含私人数据，需受限保存并演练恢复。

更新顺序：暂停扫码→备份→记录当前Git提交→工作区干净且没有服务器私改时 `git pull --ff-only` →依赖/离线测试→重启本项目→两位真人验收。不要使用 `git reset --hard`、盲目强推或未经迁移检查恢复旧数据库。

正式启用前：

- `/schedule` 可访问，无授权日程 API 返回 401，内部聊天/健康/调试路径不向公网开放。
- `/join` 可访问，无邀请状态 API 返回 401；两位真人分别扫码后，聊天/日程/提醒不串人，撤销后旧日程失效。
- 本人真实微信聊天、创建任务、到期提醒、单击日程、完成/取消/改期通过。
- API `accepted` 和本人实际收到分别验证。
- 保存数据库与私有运行状态的可恢复备份，并实际演练；在线 SQLite 备份必须一致，不能只复制正在写入的主文件。
- 重启后恢复服务、断网状态可解释、过期任务不集中补发、发送不确定不重复发送。
- 记录实际依赖与 Git 提交，具备旧版本及数据库恢复方案，不用自动更新替代发布验收。

当前本机结果见 [功能验收](本地功能验收_2026-10-12.md)。正式服务器验收通过前，不发“已上线”通知或大范围用户邀请。

某个账号能入站但手机无回复时，先用原生 `/echo 7313` 和正常账号对照；不立刻认定人数上限、风控或绑定失败，不循环重扫、重发，不整体重启影响正常用户。
