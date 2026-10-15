# 微信联系人头像/名称自动初始化：能力边界与可行方案

调研时间：2026-10-13。调研对象：`@tencent-weixin/openclaw-weixin` 2.4.10（本机实际安装版本）。
本文是对 [小满品牌与微信资料边界](BRANDING.md) 的补充，回答"邀请别人添加后自动初始化头像和名字"到底有没有解、怎么解。

## 一、结论摘要

1. **在 ClawBot 这条路线内，这个需求做不到，而且不是代码问题。** 本机插件 2.4.10 的全部后端接口只有 7 个（收消息、取上传地址、发消息、取配置、输入状态、启动/停止通知），**没有任何联系人资料接口**；协议里连"对方的昵称和头像"都不下发。
2. **这个需求有官方可行解法：换到「企业微信 / 微信客服」体系。** 两条路线：
   - **A1（改造最小）**：用 OpenClaw 官方渠道目录里的 **WeCom 通道**（`@wecom/wecom-openclaw-plugin`，腾讯企业微信团队维护），名称与头像在企业微信后台统一配置。
   - **A2（可代码自动初始化）**：直接调**微信客服（Kf）**接口 —— `kf/account/add`、`kf/account/update` 能设置客服账号的**名称**和**头像**，`kf/add_contact_way` 生成添加/邀请入口。这是目前唯一能用官方接口把"名称＋头像"写进代码自动设置的微信内入口。
3. 若品牌统一是**硬性**上线要求，还可以考虑**公众号/服务号**：名称与头像由运营者在后台设置，关注者看到的就是它，"统一"天然成立，不需要逐用户初始化。
4. **不建议**逆向私有接口或使用第三方"改名服务"：违反平台条款、有封号风险，也会破坏现有绑定与安全基线。

## 二、证据：为什么 ClawBot 路线没有这个能力

本节结论来自本机已安装插件的源码实测，不是推测。

### 2.1 插件的全部后端接口清单

`src/api/api.ts` 中所有对微信后端的调用，只有 7 个：

| 接口 | 用途 |
| --- | --- |
| `ilink/bot/getupdates` | 长轮询收消息 |
| `ilink/bot/getuploadurl` | 取媒体上传地址 |
| `ilink/bot/sendmessage` | 发消息 |
| `ilink/bot/getconfig` | 取 typing ticket |
| `ilink/bot/sendtyping` | 输入状态 |
| `ilink/bot/msg/notifystart` | 客户端启动通知 |
| `ilink/bot/msg/notifystop` | 客户端停止通知 |

在插件 `src/` 目录内检索 `avatar`、`nickname`、`nick_name`、`headimg`、`profile`、`remark`、`setName`：**0 处命中**。

### 2.2 协议类型里没有昵称和头像

`src/api/types.ts`：

- `BaseInfo.bot_agent` 注释明确写着 *"For observability only (logging, monitoring aggregation); not used for authentication or routing."* —— 只是自报身份的 User-Agent 式标识，**不是微信昵称**。
- `WeixinMessage` 只有 `from_user_id`、`to_user_id`、`session_id`、`group_id`、`message_id`、`item_list` 等。**没有昵称字段，也没有头像字段。**

也就是说：**不但改不了对方看到的名片，连"对方叫什么、头像是什么"都拿不到。**

### 2.3 插件的能力清单与配置项

`README.zh_CN.md` 的"主要能力"只列了：扫码登录并保存凭证、一个 Gateway 多微信账号、文本/图片/语音/文件/视频、长轮询与输入状态、渠道路由/配对/会话隔离。

可配置项只有：多账号、`botAgent`（**仅观测**）、`quoteCache`（引用消息本地缓存）。
`openclaw.plugin.json` 的 `configSchema` 为空对象 `{"properties": {}}` —— 插件本身没有额外配置面。

### 2.4 容易误认成"改名开关"的三处，都不是

| 位置 | 真实作用 |
| --- | --- |
| `channels.openclaw-weixin.accounts[].name` | 只在 `channel.ts` 的 `describeAccount()` 里出现，是 **OpenClaw 本地账号标签**，不会下发到微信 |
| `agents.entries.main.identity{name,emoji,avatar}` + `workspace/IDENTITY.md` + `avatar.svg` | OpenClaw 的**本地 Agent 身份**，供后台/项目页面使用；微信客户端不读取 |
| `scripts/setup-clawbot.py` 的 `init`/`features` | 同上，只初始化项目后台身份，**不改微信联系人资料**（脚本自己的注释也这么写） |

### 2.5 项目侧拿到的入站数据同样没有昵称头像

`app/clawbot_router.py` 的 `BridgeRequest` 只有四个字段：

```python
account_id, sender_id, message_id, content
```

**结论：两个方向都断了。** 既写不进"小满"到对方的微信名片，也读不到对方的昵称头像来自动填充自己的成员资料。所以"自动初始化头像和名字"如果指项目后台的成员资料，同样缺少数据来源，只能由用户自报或后台手填。

## 三、平台层面的原因

ClawBot 是**微信/腾讯侧的官方机器人身份**，它的联系人名片由平台下发，按"机器人/agent"维度走，**不按你的部署或用户维度走**。插件只是"用扫码得到的凭据收发消息"的通道。

因此：

- 微信联系人名片是**平台资产**，不是应用资产；应用侧没有写入口。
- 要让所有添加者看到统一的名称和头像，**必须换一个"名片归你管"的官方入口**，而不是在 ClawBot 上想办法。
- 官方协议也声明客户端实现并非完整服务端契约，所以这是**当前公开能力边界**，不表示微信未来永远不支持；但只要平台不提供接口，就不该承诺已实现。

## 四、可行方案对比

### 方案 A（推荐，唯一能官方设置"名称＋头像"）：改用企业微信 / 微信客服体系

关键区别：**ClawBot 的名片归平台管；企业微信和微信客服的名片归你的企业管。** 两条实现路线：

#### A1：走 OpenClaw 官方渠道目录里的 WeCom 通道（改造成本最低，建议先试）

本机 OpenClaw 自带文档 `docs/channels/wecom.md` 明确写道：OpenClaw 通过**腾讯企业微信团队维护**的外部插件 `@wecom/wecom-openclaw-plugin` 提供 WeCom 通道，该插件在 OpenClaw 官方渠道目录中，但**不随核心安装打包**。

```bash
openclaw channels add --channel wecom
openclaw channels status --channel wecom
```

- 收益：通道层由腾讯官方插件负责，**不需要自己写 kf 回调**；企业微信侧的成员/应用名称与头像在企业微信后台统一配置，外部联系人看到的就是统一品牌。
- 注意：凭证、连接模式、回调路由、访问控制都归该外部插件，且可能独立于 OpenClaw 变化；**必须按实际安装版本对应的插件文档配置**，不要套用这里的示例。
- 限制：用户侧是"企业微信/微信客服"形态，不是个人微信好友；会话与通知受企业微信规则约束。

#### A2：直接调微信客服（Kf）接口，把"名称＋头像"写进代码自动初始化

如果你要的正是"签发邀请时自动把名称和头像设好"这个动作，就走这条 —— 官方接口支持：

| 能力 | 接口 | 关键参数 |
| --- | --- | --- |
| 添加客服账号 | `POST /cgi-bin/kf/account/add` | `name`（≤16 字）、`media_id`（头像临时素材） |
| 修改客服账号 | `POST /cgi-bin/kf/account/update` | `name`、`media_id` |
| 列出 / 删除客服账号 | `kf/account/list` / `kf/account/del` | — |
| 添加入口（邀请二维码） | `kf/add_contact_way` | 生成客户扫码添加的方式 |
| 欢迎语 | `kf/send_msg_on_event` | 首次进入自动发送 |
| 收发消息 | `kf/send_msg` / `kf/sync_msg` / `kf/callback` | 回调需加密与验签 |
| 客户资料 | `kf/customer/batchget` | — |
| 临时素材（传头像） | `kf/media` | 上传后得到 `media_id` |

要点与限制：

- **先分清两个"客服"**：本项目已有的"客服消息"（`app/adapters/wechat_official.py`、`app/wechat_notifications.py` 里的 48 小时窗口）是**微信公众号客服消息**，属于公众号路线；本方案说的是**微信客服（企业微信 Kf）**，是另一套主体和接口。两者名字像，能力完全不同，**现有的公众号客服适配器不能直接当 Kf 复用**。
- **凭证**：必须用**企业微信 `corpid` + 微信客服 Secret** 换 `access_token`；公众号/小程序的 token **不被 kf 接口接受**。
- 单个企业最多添加 **5000** 个客服账号。
- **收益**：名称和头像可以**代码化一次性设置**，对所有添加者生效 —— 这正是"自动初始化"想要的形态；还能批量建多个客服账号做多品牌。
- **代价**：需要企业微信主体（建议已认证）；用户添加的是"客服"而非"好友"，受客服消息窗口与会话规则约束；**通道层需要从 ClawBot 换成 kf 回调**。好消息是你的业务逻辑本来就在 Python 侧（意图解析、任务、提醒、日程页），可复用，需要重写的是桥接/入站出站适配、邀请与绑定（`clawbot_enrollment.py`、扫码 worker）那一层。
- **待实测**：客服账号名称与头像的审核要求、以及真机上客户侧的实际展示效果，需在真实环境验收，不能只凭接口成功就认为已达成。

参考：[添加客服账号](https://developer.work.weixin.qq.com/document/path/94662)、[修改客服账号](https://developer.work.weixin.qq.com/document/path/94664)、[微信客服 access_token](https://kf.weixin.qq.com/api/doc/path/93304)。
接口参数细节（`name` ≤16 字、`media_id` 头像需先走临时素材上传）另见第三方 SDK 镜像文档 [wxkefu-rs account 模块](https://docs.rs/wxkefu-rs/0.1.3/wxkefu_rs/account/index.html)；企业微信官方文档页为前端渲染，本次未能直接读到正文，落地前请以官方页面为准复核。

### 方案 B：保留 ClawBot 路线，用户侧手动备注（零成本兜底）

- 用户可以在自己微信里给这个联系人**设置备注名**改成"小满"；**头像改不了** —— 微信不允许改好友头像。
- 可做的产品化补充：接入后第一条消息就发一张"小满名牌"图片作为视觉锚点。**但这不等于名片头像，不要当作等价验收。**
- 优点：立刻可用、零改造成本。缺点：每人手动、且没有头像。

### 方案 C：公众号 / 服务号（项目已保留的路线）

- 公众号的**名称和头像由运营者在后台设置**，关注者看到的就是它 —— "名称＋头像统一"天然成立，**不需要逐用户初始化**。
- 限制：被动回复有时限、客服消息 48 小时窗口、模板/订阅需要真实授权。
- 如果品牌统一是硬指标，公众号/服务号其实是比 ClawBot **更可控的品牌载体**。

### 方案 D：企业微信自建应用 / 客户联系

- 外部联系人看到的是企业微信成员的**名称＋头像＋企业简称**，由管理员在企业微信后台统一配置。
- 需要注意方向：官方"外部联系人"类接口改的是**企业对客户的备注/标签**，不是"客户看到的企业成员名片"。成员名片走后台配置。
- 适合 To B 场景。参考：[设置成员对外信息中的企业简称](https://open.work.weixin.qq.com/help2/pc/17406)。

### 方案 E：OpenClaw 本地身份（现状，仅本地）

保持即可。它能统一项目页面和后台的"小满"身份，但**不改变微信端**，不能作为该需求的验收证据。

### 方案对比一览

| 方案 | 能设名称 | 能设头像 | 能否代码自动化 | 需要新增主体/资质 | 改造量 | 定位 |
| --- | --- | --- | --- | --- | --- | --- |
| A1 WeCom 通道（官方插件） | ✅ | ✅ | 后台配置（非代码） | 企业微信 | **小**（换通道，插件官方维护） | **建议先试** |
| A2 微信客服 Kf | ✅ 官方接口 | ✅ 官方接口 | ✅ 可代码自动初始化 | 企业微信 | 中～大（换通道层+邀请/绑定重写） | **要"自动初始化"就选它** |
| B ClawBot＋手动备注 | 仅用户手动 | ❌ | ❌ | 无 | 无 | 临时兜底 |
| C 公众号/服务号 | ✅（后台设） | ✅（后台设） | 后台配置 | 已认证服务号等 | 中 | 品牌最可控 |
| D 企业微信客户联系 | ✅（后台设） | ✅（后台设） | 后台配置 | 企业微信 | 中 | To B |
| E 本地身份 | ❌（不影响微信） | ❌ | — | 无 | 无 | 仅后台 |

一句话选型：
- 只要能统一品牌、不要求用代码设置 → **A1**。
- 明确要求"签发邀请时自动把名称头像写好" → **A2**。
- 不想引入企业微信主体 → **B** 兜底 + 长期评估 **C**。

## 五、明确做不到 / 不要做

- ❌ 用插件 API 自动修改用户在微信里看到的 ClawBot 名片：**当前做不到**。
- ❌ 取用户的微信昵称/头像来自动填充项目成员资料：**协议里没有这些字段，拿不到**。
- ⛔ 逆向私有接口、第三方"改名服务"、hook 微信客户端：**不建议**。违反平台条款、封号风险，并且会破坏现有绑定、登录凭据和安全基线。本项目 [品牌边界](BRANDING.md) 已明确不做。

## 六、建议落地顺序

1. **短期（今天就能做）**：保留 ClawBot。把本文结论作为正式验收口径；接入后首条欢迎语改发带品牌图片的名牌消息；在邀请页/说明里告诉用户可以自行把备注名改成"小满"（明确说明头像改不了，避免预期落空）。
2. **中期（真正的解法）**：先在企业微信里建好主体，然后二选一——
   - **A1（省事）**：`openclaw channels add --channel wecom` 装官方 WeCom 插件，在企业微信后台把名称和头像配好，小范围验证外部联系人看到的效果。
   - **A2（要自动化）**：用 `kf/media` 上传猫爪头像拿到 `media_id` → `kf/account/add`（或 `update`）把名称"小满"和头像一次设好 → `kf/add_contact_way` 作为邀请入口 → `kf/send_msg_on_event` 做欢迎语；业务仍走现有 Python，把桥接的入站/出站适配到 kf 回调。先小范围验证真机展示效果，再放量。
3. **若品牌统一是硬性上线要求**：评估公众号/服务号或 WeCom 作为主入口，而不是继续在 ClawBot 上找开关。

## 七、追加调研：企业微信能否保留「自主回复」和「接受转发」

结论：**不能"全都要"。微信客服和现在的 ClawBot 各有一半，正好互补 —— 微信客服换来了转发，但把"主动回复"让出去了。**

| 能力 | 现在的 ClawBot | 微信客服（Kf） | 企业微信自建应用 |
| --- | --- | --- | --- |
| 自主/主动回复 | ✅ 基本不受窗口限制，像好友发消息 | ❌ **48 小时内、每个用户消息最多 5 条** | ✅ 纯推送、**无 48 小时窗口**；但对象是企业通讯录成员 |
| 用户能否把消息转发进来 | ❌ 不出现在微信转发联系人列表 | ✅ 接口支持（**入口是否可用待实测**） | 未验证 |
| 后端能否读到合并记录正文 | ❌ 只有 `[聊天记录]` 占位符 | ✅ **完整**：发件人、时间、每条正文 | 未验证 |

### 7.1 微信客服：转发问题基本解决

`kf/sync_msg` 的入站 `msgtype` 共 13 种：
`text`、`image`、`voice`、`video`、`file`、`location`、`miniprogram`、`channels_shop_product`、`channels_shop_order`、**`merged_msg`**、`channels`、`note`、`event`。

其中合并转发记录的结构是：

```rust
MergedMsgContent { title: String, item: Vec<MergedMsgItem> }
MergedMsgItem    { send_time: u64, msgtype: String, sender_name: String, msg_content: String }
```

也就是说 **发件人姓名、发送时间、每条正文都能拿到** —— 这正是你们 [MATERIALS.md](MATERIALS.md) 里"原生合并转发卡片正文拿不到"的解药（对应 `integrations/openclaw-xiaoman/index.js` 里那句"微信未提供这张合并转发卡片的正文"）。

### 7.2 微信客服：主动回复是硬伤

官方文档原文（经 Go SDK 镜像的文档说明核对）：

> 注意仅当微信客户在主动发送消息给客服后的 **48 小时内**，企业可发送消息给客户，**最多可发送 5 条消息**；若用户继续发送消息，企业可再次下发消息。

| 用户动作 | 允许下发条数限制 | 下发时限 |
| --- | --- | --- |
| 用户发送消息 | 5 条 | 48 小时 |

对你们产品的直接影响：

- **到期提醒**若落在用户最后一次消息的 48 小时之外 → **发不出去**。
- **一条用户消息只能换 5 条下行** → 一天多条提醒会被截断。
- **"隔夜/长时间未互动后的提醒"**（你们正在验收的场景）基本不可行。
- 这和公众号客服消息的 48 小时窗口是同一类限制 —— 只是换了个壳。

**所以：微信客服适合"用户发起、客服响应"的客服场景，不适合"主动定时提醒"的产品形态。** 你们的持久投递队列 + 到期提醒是核心卖点，迁过去会被这条限制直接砍掉。

### 7.3 企业微信自建应用：能保住主动回复，但要换用户体系

已核对的接口事实（`/cgi-bin/message/send`，[官方文档 90236](https://developer.work.weixin.qq.com/document/path/90236)）：

- 这是一个**纯推送**接口：`touser` 传成员 ID（多个用 `|` 分隔，**最多 1000 个**），也支持 `@all`、`toparty`、`totag`。
- 请求参数里**没有任何"会话窗口 / 48 小时"概念** —— 对比微信客服的 5 条/48 小时，这是本质区别。**主动提醒能保住。**
- 自带幂等能力：`enable_duplicate_check` + `duplicate_check_interval`（默认 1800s，最大 4 小时）—— 对你们"结果不确定时不盲目重发"的告警语义反而更友好。
- 支持文本、图片、语音、视频、文件、文本卡片、图文、Markdown 等类型（SDK 里实现了常用几种，其余按文档扩展）。

但关键约束：

- `touser` 只接受**企业通讯录内的成员 ID**，**不是任意微信好友**。所以外部体验者没法通过这个接口收消息。
- 响应里有 `unlicenseduser`（没有基础接口许可 / 已过期）字段 —— 说明现在有**许可与配额**模型，落地前要按官方文档确认配额与许可要求。
- 要让外部用户接入，得走**客户联系 / 外部联系人**体系，那套有自己的群发规则与限额（本次未逐条核实）。

换句话说：换过去意味着产品形态从"邀请朋友体验"变成"企业成员使用"或"企业加客户"。

### 7.4 建议

1. 如果**"转发/聊天记录整理"是刚需，而"主动提醒"可以让步** → 选微信客服。
2. 如果**"主动提醒"是核心（从代码看它是）** → **不要整体迁到微信客服**。可以考虑混合：
   - 主链路继续 ClawBot，保住主动提醒和现有绑定/日程；
   - 另开一个**微信客服入口专做"聊天记录整理 / 资料提交"**，用户要发合并记录时走那边，拿到完整正文后仍交给现有 Python 业务处理。
3. 若两条都要、且愿意换用户体系 → 评估**企业微信自建应用 + 客户联系**，但先小范围验证外部联系人的主动发送配额。

## 八、参考

- [Tencent/openclaw-weixin 后端协议说明](https://github.com/Tencent/openclaw-weixin/blob/main/docs/protocol_zh_CN.md)
- [@tencent-weixin/openclaw-weixin（npm）](https://www.npmjs.com/package/@tencent-weixin/openclaw-weixin)
- [@wecom/wecom-openclaw-plugin（npm，腾讯企业微信团队维护）](https://www.npmjs.com/package/@wecom/wecom-openclaw-plugin)
- [OpenClaw 微信渠道文档](https://docs.openclaw.ai/zh-CN/channels/wechat) / [WeCom 渠道文档](https://docs.openclaw.ai/channels/wecom)
  - 本机同一份文档也在 `clawbot-local/npm/node_modules/openclaw/docs/channels/` 下的 `wechat.md`、`wecom.md`，可直接离线核对。
- [企业微信：添加客服账号](https://developer.work.weixin.qq.com/document/path/94662) / [修改客服账号](https://developer.work.weixin.qq.com/document/path/94664)
- [微信客服 access_token](https://kf.weixin.qq.com/api/doc/path/93304)
- [企业微信：设置成员对外信息中的企业简称](https://open.work.weixin.qq.com/help2/pc/17406)
- [企业微信：如何使用微信客服](https://open.work.weixin.qq.com/help2/pc/17961)
- 项目内：[小满品牌与微信资料边界](BRANDING.md)、[用户接入与管理员操作](USER_ACCESS.md)

### 第七节（自主回复与转发）的来源

- [微信客服：接收消息和事件](https://developer.work.weixin.qq.com/document/path/94670) / [发送消息](https://developer.work.weixin.qq.com/document/path/94677)（官方页面为前端渲染，正文需在浏览器打开核对）
- [微信客服 sync_msg 接口文档](https://kf.weixin.qq.com/api/doc/path/94745)
- **48 小时 / 5 条限制的原文出处**：[silenceper/wechat 更新接口消息发送限制说明文档（#504）](https://git.gopher.ink/git/wechat/commit/05ec6a42ae555b701f3fe07a29f735fab668d2bc)（官方文档原文的 Go SDK 镜像；上游仓库 [silenceper/wechat](https://github.com/silenceper/wechat)）
- 入站 13 种 `msgtype` 与合并记录结构：[wxkefu-rs `sync_msg` 模块](https://docs.rs/wxkefu-rs/0.1.3/wxkefu_rs/sync_msg/index.html)、[`MergedMsgContent`](https://docs.rs/wxkefu-rs/0.1.3/wxkefu_rs/sync_msg/struct.MergedMsgContent.html)、[`MergedMsgItem`](https://docs.rs/wxkefu-rs/0.1.3/wxkefu_rs/sync_msg/struct.MergedMsgItem.html)、[`MsgPayload`](https://docs.rs/wxkefu-rs/0.1.3/wxkefu_rs/sync_msg/enum.MsgPayload.html)
- 出站消息类型与 48 小时窗口：[wxkefu-rs `send_msg` 模块](https://docs.rs/wxkefu-rs/0.1.3/wxkefu_rs/send_msg/index.html)
- 企业微信自建应用：[发送应用消息](https://developer.work.weixin.qq.com/document/path/90236)、[接收消息与事件](https://developer.work.weixin.qq.com/document/path/90239)、[主动推送消息](https://developer.work.weixin.qq.com/document/path/101837)
  - 接口参数（`touser` 最多 1000 个成员、`@all`、`enable_duplicate_check`、`unlicenseduser`）核对自 Go SDK 镜像源码：[silenceper/wechat `work/message/message.go`](https://git.gopher.ink/git/wechat/src/commit/e4fc0bd615ff0ee3bf7bfe9abe3a0f858d0e5799/work/message/message.go)——**参数中确无任何会话窗口字段**。
- 转发入口存疑的旁证（仅标题可见，正文未取到）：[企业微信客服，接收消息和事件，消息类型是"聊天记录消息"，在微信上没办法转发给企业微信客服？](https://developer.work.weixin.qq.com/community/question/detail?content_id=16794353139297990806)

## 附：证据复现命令

在 `clawbot-local/state/npm/projects/tencent-weixin-openclaw-weixin-7783ac86ba/node_modules/@tencent-weixin/openclaw-weixin/` 下：

```bash
# 1) 列出全部后端接口
grep -n 'endpoint:' src/api/api.ts
# 2) 确认没有任何资料类接口
grep -rniE 'avatar|nickname|nick_name|headimg|profile|remark|setName' src/   # 期望无结果
# 3) 确认协议里没有昵称/头像字段
grep -nE 'from_user_id|to_user_id|bot_agent' src/api/types.ts
```

本机实测结果：步骤 2 返回 0 处命中；步骤 1 得到第 2.1 节表中的 7 个接口。
