# 来源与改动

- 来源：https://github.com/op7418/Humanizer-zh
- 固定提交：`f4518a8eab97b8bfebc66a89d34320a89bef6930`
- 下载与审查日期：2026-10-16；上游 SKILL 修订日期：2026-09-23。
- 原作者版权及 MIT 授权见 `UPSTREAM-LICENSE`；完整规则快照见 `Humanizer-zh.original.md`。

上游面向文章润色。本改版保留信息、确定程度和作者声音的约束，压缩为微信短回复规则，增加图片分类、真实身份、任务执行状态、只读来源和 ACP 权限边界。没有照搬31条长清单到每次推理，也没有引入“绕过检测”的目标。

`chat-policy.txt` 是运行时共用规则：Python 普通聊天、短回复、资料识别及 ACP 演示直接读取。`SKILL.md` 是开发和编辑时的指南，不能仅凭安装 skill 就假设服务端已使用它。部署必须携带此目录。

2026-10-19补充研究：[MaiBot表达选择](https://github.com/Mai-with-u/MaiBot/blob/main/src/chat/replyer/maisaka_expression_selector.py)按情境和表达风格挑选候选，[AstrBot人格配置](https://github.com/AstrBotDevs/AstrBot/wiki/zh-dev-astrbot-config)把会话人格作为独立提示设置。本轮只借鉴按情境选择表达、保持个人语气的设计，没有复制、安装或运行其代码、提示词或学习数据，也没有把用户聊天上传给这些项目。MaiBot为GPL-3.0项目，不作为本项目的内嵌代码或依赖。

小满的接梗示例和备用短句由本项目原创。实际提醒上下文按本人有效投递/任务焦点提供给原有一次模型调用；玩笑消息只能走chat，不让模型借此关闭、完成或改期任务。模型不可用、转成说明书或声称修改状态时回退到本地接话。明确停催仍走业务控制，语气不覆盖用户选择。
