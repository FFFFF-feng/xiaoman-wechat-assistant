"""Shared, versioned writing policy; never rewrite task parameters or receipts."""
from pathlib import Path
import hashlib
import re


NATURAL_CHAT_POLICY = (
    Path(__file__).resolve().parents[1]
    / "skills/xiaoman-natural-chat/references/chat-policy.txt"
).read_text(encoding="utf-8").strip()


def reminder_reply(title: str, preferences: dict, seed: str) -> str:
    """Render real task facts locally: no extra model latency or invented status."""
    name = preferences.get('assistant_name', '小满')
    tone = preferences.get('reply_tone', 'natural')
    address = preferences.get('user_address')
    serious = bool(re.search(r'就医|医院|看病|吃药|服药|航班|火车|赶车|会议|开会|面试|考试|转账|付款|还款|急救', title))
    if serious:
        return f'{name}提醒你：{title}。'
    variants = {
        'natural': [f'{title}，到时间啦。', f'来叫你啦，别忘了{title}。', f'提醒你一下：{title}。'],
        'warm': [f'到时间啦，{title}。', f'轻轻提醒一下：{title}。', f'记着这件事呢，{title}。'],
        'playful': [f'{title}，开工啦！', f'叮，{title}该安排上啦！', f'来敲敲你，别忘了{title}。'],
    }
    choices = variants.get(tone, variants['natural'])
    body = choices[int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16) % len(choices)]
    prefix = f'{name}来叫你啦：' if name != '小满' else ''
    if address:
        prefix += address + '，'
    return prefix + body


def completion_reply(title: str, preferences: dict) -> str:
    if preferences.get('reply_tone') == 'playful' and not re.search(r'就医|医院|看病|吃药|服药|急救', title):
        return f'好，勾掉「{title}」啦，收工！'
    return f'好，「{title}」勾掉啦。'
