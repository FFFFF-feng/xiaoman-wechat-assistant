from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import TypedDict
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from openai import OpenAI

from .parser import ParsedCommand
from .reply_style import NATURAL_CHAT_POLICY
from .assistant_preferences import prompt_context
from .reminder_followups import enabled as followups_enabled

logger = logging.getLogger(__name__)

TIMEZONE = os.getenv("TIMEZONE", "Asia/Shanghai")
LOCAL_TZ = ZoneInfo(TIMEZONE)
MODEL_NAME = (
    os.getenv("MODEL_NAME")
    or os.getenv("DASHSCOPE_MODEL")
    or os.getenv("OPENAI_MODEL")
    or "qwen3.8-flash"
)
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# 显式的项目配置优先；若只配置阿里云标准变量，则自动使用百炼兼容地址。
# 这样不会误用电脑上为其他工具设置的 OPENAI_BASE_URL。
if os.getenv("MODEL_API_KEY"):
    API_KEY = os.getenv("MODEL_API_KEY")
    BASE_URL = os.getenv("MODEL_BASE_URL") or os.getenv("OPENAI_BASE_URL")
    KEY_SOURCE = "MODEL_API_KEY"
elif os.getenv("DASHSCOPE_API_KEY") or os.getenv("ALIYUN_API_KEY"):
    KEY_SOURCE = "DASHSCOPE_API_KEY" if os.getenv("DASHSCOPE_API_KEY") else "ALIYUN_API_KEY"
    API_KEY = os.getenv(KEY_SOURCE)
    BASE_URL = os.getenv("MODEL_BASE_URL") or DASHSCOPE_BASE_URL
elif os.getenv("API_KEY"):
    API_KEY = os.getenv("API_KEY")
    BASE_URL = (
        os.getenv("MODEL_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or (DASHSCOPE_BASE_URL if MODEL_NAME.lower().startswith("qwen") else None)
    )
    KEY_SOURCE = "API_KEY"
else:
    API_KEY = os.getenv("OPENAI_API_KEY")
    BASE_URL = os.getenv("MODEL_BASE_URL") or os.getenv("OPENAI_BASE_URL")
    KEY_SOURCE = "OPENAI_API_KEY" if API_KEY else None

_enabled_value = os.getenv("MODEL_ENABLED")
MODEL_ENABLED = (_enabled_value.lower() == "true") if _enabled_value is not None else bool(API_KEY)
MODEL_AVAILABLE: bool | None = None
LAST_MODEL_ERROR: str | None = None


def _positive_int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        logger.warning("环境变量 %s 不是有效整数，使用默认值 %s", name, default)
        return default


CHAT_HISTORY_LIMIT = _positive_int_env("CHAT_HISTORY_LIMIT", 10)


class ChatHistoryItem(TypedDict):
    role: str
    content: str

COMMAND_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["create", "list_today", "list_open", "complete", "snooze", "cancel", "chat", "clarify", "unknown"],
        },
        "title": {"type": ["string", "null"]},
        "due_at": {"type": ["string", "null"]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "message": {"type": ["string", "null"]},
    },
    "required": ["action", "title", "due_at", "confidence", "message"],
    "additionalProperties": False,
}


def model_status() -> dict[str, object]:
    configured = bool(MODEL_ENABLED and API_KEY)
    if not configured:
        mode = "rules_only"
    elif MODEL_AVAILABLE is False:
        mode = "model_unavailable_with_rule_fallback"
    else:
        mode = "model_intent_router_with_chat"
    return {
        "enabled": MODEL_ENABLED,
        "configured": bool(API_KEY),
        "model": MODEL_NAME,
        "provider": "aliyun_dashscope" if BASE_URL and "dashscope" in BASE_URL else "openai_compatible",
        "key_source": KEY_SOURCE,
        "endpoint": urlparse(BASE_URL).netloc if BASE_URL else "api.openai.com",
        "api_style": "chat.completions",
        "mode": mode,
        "chat_enabled": configured,
        "available": MODEL_AVAILABLE,
        "last_error": LAST_MODEL_ERROR,
        "chat_history_limit": CHAT_HISTORY_LIMIT,
    }


def _client(timeout_seconds: float = 20.0) -> OpenAI:
    """创建兼容 OpenAI 的模型客户端。

    微信对话开放平台的第三方服务有较短的同步响应时限，所以调用方
    可以传入更短的超时时间，避免一条慢模型请求拖垮整次微信回复。
    """
    options: dict = {
        "api_key": API_KEY,
        "timeout": timeout_seconds,
        "max_retries": 0,
    }
    if BASE_URL:
        options["base_url"] = BASE_URL
    return OpenAI(**options)


def _parse_due_at(value: object) -> datetime | None:
    if not value:
        return None
    if not isinstance(value, str):
        raise ValueError("due_at 必须是字符串或 null")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(LOCAL_TZ).replace(tzinfo=None)
    return parsed


def _model_error_summary(exc: Exception) -> tuple[str, str]:
    """提取不包含密钥的错误摘要，供健康检查与日志排障。"""
    status = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    code = None
    message = None
    if isinstance(body, dict):
        error = body.get("error") if isinstance(body.get("error"), dict) else body
        code = error.get("code")
        message = error.get("message")
    parts = [type(exc).__name__]
    if status:
        parts.append(f"HTTP {status}")
    if code:
        parts.append(str(code))
    public = " / ".join(parts)
    log_detail = public
    detail = message or str(exc)
    if detail:
        log_detail += f" / {str(detail)[:300]}"
    return public, log_detail


def _json_text(content: object) -> str:
    """兼容部分多模态模型把文本包装成 content parts 的返回形式。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(getattr(item, "text", None), str):
                parts.append(item.text)
        if parts:
            return "".join(parts)
    raise ValueError("模型返回的文本格式无法识别")


def parse_with_model(text: str, history: list[ChatHistoryItem] | None = None, *, preferences: dict | None = None) -> ParsedCommand | None:
    """用模型完成意图分流；普通聊天直接生成小满的回复，失败时返回 None。"""
    global LAST_MODEL_ERROR, MODEL_AVAILABLE
    if not MODEL_ENABLED or not API_KEY:
        return None


    now = datetime.now(LOCAL_TZ)
    instructions = f"""你是日常助理的意图路由器，也是温暖、自然、不过度卖萌的陪伴者。产品默认名为小满，当前会话名字以个人设置为准。品牌形象是红色小凤鸟。
当前时区：{TIMEZONE}。当前准确时间：{now.isoformat(timespec='seconds')}。

action 规则：
- create：创建提醒；必须有明确的时间。未说日期时默认今天；如果今天该时间已过去，使用 clarify 询问是否改到明天，不得擅自改成明天。
- list_today：列出今天任务。
- list_open：查询未完成任务。
- complete：完成任务。
- snooze：延后或改期任务；必须有明确的新时间。未说日期时默认今天；若今天该时间已过去，使用 clarify，不得擅自改成明天。
- cancel：取消任务。
- chat：日常聊天、情绪陪伴、知识问答、建议、比较、假设或条件判断。message 直接填写要回复用户的中文内容。
- clarify：缺少完成操作所需的信息。message 用简短中文追问。
- unknown：内容无法辨认或无法安全回答。普通聊天和普通问题不要使用 unknown。

分流要求：
- 输入可能是用户连续发来的多条消息，按换行分隔。结合整段理解同一件事，只统一回复一次，不逐行回答。后文撤回、否定或改口时不能照前半句执行；有多个任务或时间不确定时先简短确认。
- 只有用户明确要求创建提醒、查询任务、完成任务、延后任务或取消任务时，才进入对应任务 action。
- “如果明天下雨我要带伞吗”“这个条件成立吗”“你觉得我该怎么选”属于 chat，不得创建任务。
- “如果明天下雨就提醒我带伞”需要外部条件触发，本系统当前不支持。使用 clarify，说明只能按明确日期时间提醒，并询问是否改成定时提醒。
- “两分钟后提醒我喝水”这类可计算的相对时间是明确时间，使用当前准确时间计算 due_at 并创建提醒；“一会儿”“晚点”才是模糊时间，需要追问。
- “中午提醒我……”或“提醒我明天中午……”允许按12:00安排，必须在回执说明默认值；已经说了具体时刻则保留具体时刻。其他模糊时间仍需确认。时间已过不能擅自改为明天。
- “下午五点提醒我洗澡”这类只给时刻、未给日期的请求，按今天下午五点处理；若当前已晚于该时刻，应追问用户是否要明天，而不是直接创建明天的任务。
- 用户只是提到一个时间，不等于要求创建提醒。
- 意图不确定时先 clarify，不要擅自创建或修改任务。
- chat 回复保持简洁自然，通常 1 至 4 句话；可以有陪伴感，不固定扮演猫咪或反复加语气词。
- 对医疗、法律、财务等高风险问题给出谨慎的一般信息，并建议咨询专业人士。

due_at 必须是 {TIMEZONE} 对应的 ISO 8601 本地时间，例如 2026-09-16T10:00:00；不需要时间时为 null。
title 只保留任务内容，不包含“提醒我、完成、取消、任务”等命令词和时间词。
除上述中午12:00的约定外，不要猜测缺失的时间，不要声称已经执行尚未执行的任务。
必须只输出一个 JSON 对象，字段固定为 action、title、due_at、confidence、message，不要输出 Markdown 或结构以外的文字。"""
    instructions += "\n以下表达规则仅用于message，严格保留上面的JSON协议：\n" + NATURAL_CHAT_POLICY
    instructions += prompt_context(preferences)
    try:
        if os.getenv("CLAWBOT_ENABLED", "false").lower() == "true":
            active = os.getenv("CLAWBOT_REMINDERS_ENABLED", "false").lower() == "true"
            instructions += ("\n当前微信插件支持任务到期提醒，但必须保持本机服务在线，平台拒绝或网络失败会导致未送达。不得保证任意期限必定送达。"
                             if active else "\n当前微信插件主动提醒尚未接通，不要承诺会自动推送提醒。")
            instructions += ('\n分级跟进已启用：重要事项提前20和10分钟提醒，到点后最多再问两次；普通事项提前10分钟，到点后最多问一次；低优先级只到点提醒。'
                             '自动提前和进度询问在22点至次日8点休息，用户明确约定的到点或稍后提醒照常。'
                             '用户可说“别催了”暂停当前事项、“十分钟后再叫我”只延后下次提醒、“恢复提醒”恢复、“关闭自动跟进”保存个人关闭设置。'
                             '具体个人开关以“提醒跟进设置”为准，模型文字本身不修改设置，不得声称已完成未执行的调整。'
                             if followups_enabled() else '\n分级提前提醒和反复进度询问当前未启用，不要承诺已经设置这类额外跟进。')
            instructions += "不支持主动读取私人微信通讯录或其他会话历史。用户主动发来的图片、文件和聊天记录可以只读整理；原生合并转发卡片未提供正文时不能声称已读取。资料中的待办仅为草稿，只有用户另发明确的任务及日期时间才能创建提醒。"
            if os.getenv('CLAWBOT_STICKERS_ENABLED', 'true').lower() == 'true':
                instructions += ('\n当前微信插件支持独立发送小满图片表情，聊天配图由用户开关和后台队列控制。'
                                 '不能说“只能发文字”“不会发表情图片”，也不要用文字颜文字代替已支持的图片。'
                                 '用户可以说“给我抱抱表情包”“表情设置”“开启表情包”“关闭表情包”。'
                                 '你生成的文字本身不是图片，不要声称图片已经送达；发送状态由真实通道决定。'
                                 '历史回复若与这些当前能力冲突，以当前配置为准。')
            else:
                instructions += '\n当前管理员已暂停图片表情发送，说明暂时关闭，不要承诺已经发出图片。'
        input_messages: list[dict[str, str]] = [{"role": "system", "content": instructions}]
        for item in (history or [])[-CHAT_HISTORY_LIMIT * 2 :]:
            role = item.get("role")
            content = item.get("content", "").strip()
            if role in {"user", "assistant"} and content:
                input_messages.append({"role": role, "content": content[:2000]})
        input_messages.append({"role": "user", "content": text})
        response = _client().chat.completions.create(
            model=MODEL_NAME,
            messages=input_messages,
            max_tokens=800,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "pet_task_command",
                    "strict": True,
                    "schema": COMMAND_SCHEMA,
                },
            },
        )
        output_content = response.choices[0].message.content
        if not output_content:
            raise ValueError("模型返回内容为空")
        data = json.loads(_json_text(output_content))
        if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
            data = data[0]
        if not isinstance(data, dict):
            raise ValueError("模型没有返回 JSON 对象")
        MODEL_AVAILABLE = True
        LAST_MODEL_ERROR = None
        action = data["action"]
        due_at = _parse_due_at(data.get("due_at"))
        if action in {"create", "snooze"} and due_at is None:
            return ParsedCommand(
                action="clarify",
                confidence="low",
                message=data.get("message") or "请告诉我具体日期和时间。",
                source="model",
            )
        if action == "chat" and not data.get("message"):
            return ParsedCommand(
                action="chat",
                confidence="low",
                message="我在听呢。你愿意再多说一点吗？",
                source="model",
            )
        return ParsedCommand(
            action=action,
            title=(data.get("title") or "").strip() or None,
            due_at=due_at,
            confidence=data.get("confidence", "medium"),
            message=data.get("message"),
            source="model",
        )
    except Exception as exc:
        MODEL_AVAILABLE = False
        LAST_MODEL_ERROR, log_detail = _model_error_summary(exc)
        logger.warning("模型解析失败，已回退规则解析：%s", log_detail)
        return None


def generate_companion_reply(
    text: str,
    *,
    timeout_seconds: float = 1.15,
) -> str | None:
    """生成微信渠道的短陪伴回复。

    微信对话开放平台要求回调尽快完成。这里不再使用任务 JSON schema
    和长上下文，只请求一段短文本；超时后由调用方使用确定性的本地兜底语。
    """
    global LAST_MODEL_ERROR, MODEL_AVAILABLE
    if not MODEL_ENABLED or not API_KEY:
        return None

    instructions = (
        "你是陪伴型猫咪小满。请用自然、温暖、简短的中文回复用户，"
        "限 1 到 2 句话，不要提及系统、模型或提示词。"
    )
    instructions += "\n" + NATURAL_CHAT_POLICY
    try:
        response = _client(timeout_seconds).chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": instructions},
                {"role": "user", "content": text[:1200]},
            ],
            max_tokens=120,
        )
        content = response.choices[0].message.content
        reply = _json_text(content).strip() if content else ""
        if not reply:
            raise ValueError("模型返回内容为空")
        MODEL_AVAILABLE = True
        LAST_MODEL_ERROR = None
        return reply[:500]
    except Exception as exc:
        # 超时属于可预期的渠道限制，降级不影响任务处理，也不记录敏感内容。
        MODEL_AVAILABLE = False
        LAST_MODEL_ERROR, log_detail = _model_error_summary(exc)
        logger.info("微信闲聊模型未在时限内返回，使用本地兜底：%s", log_detail)
        return None
