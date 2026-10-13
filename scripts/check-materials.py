"""Optional paid model smoke check using synthetic material; no accounts/DB/platform sends."""
import argparse
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.database import Base  # Load the selected project environment, never initialize DB.
from app import materials, model_parser
from PIL import Image, ImageDraw, ImageFont


def main():
    parser = argparse.ArgumentParser(description="合成资料模型验证，不读真人聊天，不操作日程或微信")
    parser.add_argument('--live', action='store_true', help='显式调用现有配置的模型，会产生模型费用')
    args = parser.parse_args()
    if not args.live:
        parser.error('仅在明确需要真实模型检查时加 --live')
    if not model_parser.API_KEY:
        print('模型未配置'); return 1
    model_parser.MODEL_ENABLED = True
    canvas = Image.new('RGB', (1000, 360), 'white')
    candidates = [Path('C:/Windows/Fonts/msyh.ttc'),
                  Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')]
    font = next((ImageFont.truetype(str(p), 36) for p in candidates if p.exists()), ImageFont.load_default())
    ImageDraw.Draw(canvas).multiline_text((30, 30), 'XIAOMAN IMAGE TEST\nProject: Alpha\nDeadline: 2026-10-20 10:00\nOwner: Alice', font=font, fill='black', spacing=14)
    output = io.BytesIO(); canvas.save(output, format='PNG')
    checks = [
        ('image', lambda: materials.summarize(image=materials.prepare_image(output.getvalue())), ('Alpha', '2026')),
        ('file', lambda: materials.summarize(text=materials.extract_file('项目代号：蓝鲸。负责人：甲。初稿截止：2026-10-20 10:00。'.encode(), 'fixture.txt')), ('蓝鲸', '10:00')),
        ('records', lambda: materials.summarize(text=materials.record_text('{"messages":[{"sender":"甲","time":"2026-10-13 09:00","content":"原定周五十点交初稿"},{"sender":"乙","time":"2026-10-13 09:10","content":"取消周五安排，改下周一，具体日期时间待确认"}]}')), ('取消', '确认')),
    ]
    failures = 0
    for name, run, expected in checks:
        try:
            reply = run()
            ok = all(word in reply for word in expected)
            print(f'{name}: {"PASS" if ok else "FAIL"} (synthetic source facts)')
            failures += not ok
        except materials.MaterialError:
            print(f'{name}: FAIL (model unavailable or invalid result)'); failures += 1
    return int(bool(failures))


if __name__ == '__main__':
    raise SystemExit(main())
