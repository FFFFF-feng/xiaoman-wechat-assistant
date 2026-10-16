"""Opt-in model evaluation on synthetic inputs; no DB initialization or WeChat sends."""
import argparse
import io
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import materials, model_parser
from PIL import Image, ImageDraw


def main():
    parser = argparse.ArgumentParser(description="用合成聊天、图片检查自然表达，不访问用户记录或发送微信")
    parser.add_argument('--live', action='store_true', help='调用已配置模型，会产生少量模型用量')
    parser.add_argument('--output', default='.cache/natural-replies-evaluation.json')
    parser.add_argument('--case', action='append', choices=['chat', 'plan', 'records', 'image'])
    args = parser.parse_args()
    if not args.live:
        parser.error('实际模型检查需要 --live')
    if not model_parser.API_KEY:
        print('模型未配置'); return 1
    logging.disable(logging.CRITICAL)  # No raw provider diagnostic or secrets in this report.
    model_parser.MODEL_ENABLED = True
    image = Image.new('RGB', (900, 240), 'white')
    ImageDraw.Draw(image).multiline_text((24, 24),
        'SYNTHETIC NOTICE\nProject: Bluewhale\nDeadline: 2026-10-20 10:00\nLocation: TBD',
        fill='black', font_size=28, spacing=12)
    raw = io.BytesIO(); image.save(raw, format='PNG')
    cases = [
        ('chat', '今天事情好多，有点不知道先干哪个', None),
        ('plan', '帮我想想周报怎么开始，先不要创建提醒', None),
        ('records', '甲：原定2026年10月23日10:00交初稿。\n乙：取消23日安排，改到26日，具体时间待确认。', ('取消', '26', '确认')),
        ('image', materials.prepare_image(raw.getvalue()), ('Bluewhale', '10:00')),
    ]
    results = []
    for name, content, required in cases:
        if args.case and name not in args.case:
            continue
        try:
            if name in {'chat', 'plan'}:
                command = model_parser.parse_with_model(content)
                if command is None:
                    results.append({'case': name, 'structural_check': 'fail',
                                    'reason': model_parser.LAST_MODEL_ERROR or 'model-unavailable'})
                    print(f'{name}: fail (model unavailable or invalid result)')
                    continue
                reply = command.message or ''
                ok = command.action == 'chat' and bool(reply)
                row = {'case': name, 'input': content, 'action': command.action, 'reply': reply}
            else:
                reply = materials.summarize(**({'image': content} if name == 'image' else {'text': content}))
                ok = all(word in reply for word in required)
                row = {'case': name, 'input': '[synthetic image]' if name == 'image' else content, 'reply': reply}
            row['structural_check'] = 'pass' if ok else 'fail'
            print(f'{name}: {row["structural_check"]}')
        except Exception:
            row = {'case': name, 'structural_check': 'fail', 'reason': 'model-unavailable-or-invalid-result'}
            print(f'{name}: fail (model unavailable or invalid result)')
        results.append(row)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({'synthetic_only': True, 'task_write': False, 'wechat_send': False,
        'note': 'Structural checks do not measure naturalness. Review replies manually.', 'results': results},
        ensure_ascii=False, indent=2), encoding='utf-8')
    return int(any(r['structural_check'] != 'pass' for r in results))


if __name__ == '__main__':
    raise SystemExit(main())
