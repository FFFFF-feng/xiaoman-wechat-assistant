from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


TZ = ZoneInfo("Asia/Shanghai")
CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
TIME_NUMBER = r"(?:\d{1,2}|[零〇一二两三四五六七八九十百]+)"
TIME_PATTERN = rf"(?:(?P<meridiem>上午|早上|中午|下午|晚上)\s*)?(?P<hour>{TIME_NUMBER})(?:[:：](?P<colon_minute>{TIME_NUMBER}))?\s*点(?:\s*(?P<half>半)|\s*(?P<minute>{TIME_NUMBER})\s*分?)?"
DATE_PATTERN = r"(?:今天|明天|后天|大后天|\d{1,2}月\d{1,2}[日号]?)"
RELATIVE_MINUTES_PATTERN = r"(?P<relative_minutes>\d{1,4}|[零〇一二两三四五六七八九十百]+)\s*分钟后"
CREATE_PATTERN = r"提醒|叫我|记得|安排|创建|新增|定时"


@dataclass
class ParsedCommand:
    action: str
    title: str | None = None
    due_at: datetime | None = None
    confidence: str = "high"
    message: str | None = None
    source: str = "rules"


def now_local() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None, second=0, microsecond=0)


def now_exact_local() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None, microsecond=0)


def _number_value(value: str) -> int:
    """把中文数字（如“八”“三十”“二十五”）或阿拉伯数字转为整数。"""
    if value.isdigit():
        return int(value)
    total = 0
    current = 0
    units = {"十": 10, "百": 100}
    for char in value:
        if char in CN_DIGITS:
            current = current * 10 + CN_DIGITS[char]
        elif char in units:
            unit = units[char]
            total += (current or 1) * unit
            current = 0
    return total + current


def has_explicit_date(text: str) -> bool:
    return bool(re.search(DATE_PATTERN, text) or re.search(RELATIVE_MINUTES_PATTERN, text))


def has_time_expression(text: str) -> bool:
    return bool(re.search(TIME_PATTERN, text) or re.search(RELATIVE_MINUTES_PATTERN, text)
                or re.search(r"\d{1,2}(?:[:：]\d{1,2})?\s*(?:am|pm)", text, re.I))


def _extract_datetime(text: str) -> tuple[datetime | None, str | None]:
    relative_match = re.search(RELATIVE_MINUTES_PATTERN, text)
    if relative_match:
        minutes = _number_value(relative_match.group("relative_minutes"))
        if not 1 <= minutes <= 1440:
            return None, "请把相对提醒时间设在 1 到 1440 分钟之间。"
        return now_exact_local() + timedelta(minutes=minutes), None

    base = now_local()
    day_offset: int | None = None
    if "大后天" in text:
        day_offset = 3
    elif "后天" in text:
        day_offset = 2
    elif "明天" in text:
        day_offset = 1
    elif "今天" in text:
        day_offset = 0

    date_match = re.search(r"(\d{1,2})月(\d{1,2})[日号]?", text)
    if date_match:
        month, day = int(date_match.group(1)), int(date_match.group(2))
        year = base.year + (1 if month < base.month - 6 else 0)
        date_value = base.replace(year=year, month=month, day=day)
    elif day_offset is not None:
        date_value = base + timedelta(days=day_offset)
    else:
        date_value = base

    time_match = re.search(TIME_PATTERN, text)
    if time_match:
        meridiem = time_match.group("meridiem")
        hour = _number_value(time_match.group("hour"))
        if time_match.group("half"):
            minute = 30
        else:
            minute_text = time_match.group("colon_minute") or time_match.group("minute")
            minute = _number_value(minute_text) if minute_text else 0
    else:
        ampm_match = re.search(r"(\d{1,2})(?:[:：](\d{1,2}))?\s*(am|pm)", text, re.I)
        if not ampm_match:
            return None, None
        hour_text, minute_text, meridiem = ampm_match.groups()
        hour = int(hour_text)
        minute = int(minute_text or 0)
    if meridiem in {"下午", "晚上"} and hour < 12:
        hour += 12
    if meridiem == "中午" and hour < 11:
        hour += 12
    if meridiem and meridiem.lower() == "pm" and hour < 12:
        hour += 12
    if meridiem and meridiem.lower() == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None, "时间格式不太对，小时应为 0-23，分钟应为 0-59。"
    result = date_value.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if result <= base:
        if day_offset is None and date_match is None:
            return None, "今天这个时间已经过去了。如果要明天提醒，请明确说“明天几点提醒我……”。"
        return None, "这个日期和时间已经过去了，请换一个将来的时间。"
    return result, None


def _extract_title(text: str) -> str:
    cleaned = text
    cleaned = re.sub(RELATIVE_MINUTES_PATTERN, "", cleaned)
    cleaned = re.sub(r"(?:明天|后天|大后天|今天|\d{1,2}月\d{1,2}[日号]?)", "", cleaned)
    cleaned = re.sub(TIME_PATTERN, "", cleaned)
    cleaned = re.sub(r"(?:提醒我|提醒|叫我|帮我|请|在|任务)", "", cleaned)
    cleaned = re.sub(r"(创建|新增|安排|设置|记得|定时|到时|的时候)", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ，,。.!！")
    cleaned = re.sub(r"^(?:一个|一条)\s*", "", cleaned)
    cleaned = re.sub(r"的$", "", cleaned).strip()
    return cleaned or "未命名任务"


def parse_time_followup(
    text: str,
    previous_user_text: str | None,
    previous_assistant_text: str | None = None,
) -> ParsedCommand | None:
    """把“今天10点”这类补充信息合并到上一轮提醒请求中。"""
    if not previous_user_text:
        return None
    # 上一条若已成功创建，不能因用户再回“今天”而创建重复任务。
    if previous_assistant_text and previous_assistant_text.startswith("收到，我会在 "):
        return None
    # 新消息已经带有“提醒/创建”等完整动作词时，应视为一条新的请求。
    # 不能把它误当成上一条日程的补充，否则会擅自继承上一轮的日期。
    if re.search(CREATE_PATTERN, text):
        return None
    if not (has_explicit_date(text) or has_time_expression(text)):
        return None
    # 只有独立的日期/时间回答才可补全上一轮请求。“今天有点累”
    # 虽含日期词，但属于新的聊天内容，不能继承上一轮提醒的时间。
    remainder = re.sub(DATE_PATTERN, "", text)
    remainder = re.sub(RELATIVE_MINUTES_PATTERN, "", remainder)
    remainder = re.sub(TIME_PATTERN, "", remainder)
    remainder = re.sub(r"\d{1,2}(?:[:：]\d{1,2})?\s*(?:am|pm)", "", remainder, flags=re.I)
    remainder = re.sub(r"[\s，,。.!！?？、:：]+", "", remainder)
    remainder = re.sub(r"^(?:是|就|在|对|嗯|那)+", "", remainder)
    remainder = re.sub(r"(?:啊|呀|呢|哦|吧|嘛)+$", "", remainder)
    if remainder:
        return None
    if not re.search(CREATE_PATTERN, previous_user_text):
        return None
    combined = f"{text} {previous_user_text}"
    if not has_time_expression(combined):
        return None
    # 上一句只说了任务、这一句补“下午五点”时，也默认今天。
    # 上一句已有时间却仍无日期时，不把另一句时间误当作重复创建请求。
    if not has_explicit_date(combined) and has_time_expression(previous_user_text):
        return None
    due_at, error = _extract_datetime(combined)
    if error:
        return ParsedCommand("clarify", confidence="low", message=error)
    if due_at is None:
        return None
    return ParsedCommand("create", title=_extract_title(previous_user_text), due_at=due_at)


def parse_command(text: str) -> ParsedCommand:
    content = text.strip()
    if re.search(r"未完成|没完成|没有完成|还没完成|待办", content) and re.search(r"列出|查看|查询|看看|哪些|有什么|任务|事项", content):
        return ParsedCommand("list_open")
    if re.search(r"列出|查看|查询|今天.*任务|任务列表", content):
        if re.search(r"未完成|待办|没完成", content):
            return ParsedCommand("list_open")
        return ParsedCommand("list_today")
    if re.search(r"完成|搞定|做完", content):
        title = re.sub(r"^(请)?(完成|搞定|做完)\s*", "", content).strip(" ：:，,")
        return ParsedCommand("complete", title=title or None)
    if re.search(r"取消|删除", content):
        title = re.sub(r"^(请)?(取消|删除)\s*", "", content).strip(" ：:，,")
        title = re.sub(r"任务$", "", title).strip()
        return ParsedCommand("cancel", title=title or None)
    if re.search(r"延后|推迟|延期|改到", content):
        due_at, error = _extract_datetime(content)
        if error:
            return ParsedCommand("clarify", confidence="low", message=error)
        title = re.sub(r"^(请)?(把)?", "", content)
        title = re.sub(r"(延后|推迟|延期|改到).*$", "", title).strip(" ：:，,")
        if not title:
            title = _extract_title(content)
        if due_at is None:
            return ParsedCommand("clarify", confidence="low", message="你想把任务延后到几点？不写日期默认今天，例如“把周报延后到今天17点”。")
        return ParsedCommand("snooze", title=title, due_at=due_at)
    if re.search(CREATE_PATTERN, content):
        due_at, error = _extract_datetime(content)
        if error:
            return ParsedCommand("clarify", confidence="low", message=error)
        if due_at is None:
            return ParsedCommand("clarify", confidence="low", message="请告诉我几点提醒；不写日期默认今天，例如“下午5点提醒我提交周报”。")
        return ParsedCommand("create", title=_extract_title(content), due_at=due_at)
    return ParsedCommand("unknown", confidence="low", message="我可以帮你创建、查询、完成、延后或取消任务。试试“明天10点提醒我提交周报”。")
