#!/usr/bin/env python3
"""看图测验：确认当前模型真的收到了图片像素，而不是凭文件名、转写或想象填写“已看片确认”。

起因：2026-10-24 葱姜鸡测验中，某个 Agent 环境里的模型把鸡块写成“面条”“豆腐”，
空盘子写成“码豆腐入锅”——它很可能根本没收到图片，却照样把每行写成“已看片确认”。

用法：
  python3 vision_check.py make  OUT目录      # 生成 OUT/看图测验.png（随机四位数字＋颜色），答案只存哈希
  python3 vision_check.py verify OUT目录 数字 颜色   # 例：verify OUT 4827 绿色；通过后写 OUT/看图校验.json
cutter.py build 要求选片表所在目录有通过的 看图校验.json。
不要用OCR或读取脚本、文件字节来作答；答不出就如实报告“看不到图片”，停在候选整理。
"""
import hashlib
import json
import random
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

COLORS = {'红色': (220, 40, 40), '绿色': (30, 160, 60), '蓝色': (40, 80, 220), '橙色': (240, 140, 20), '紫色': (140, 60, 190)}
PROBE, PASS = '看图测验.png', '看图校验.json'
STATE = '.看图测验.json'


def _digest(salt, digits, color):
    return hashlib.sha256(f'{salt}|{digits}|{color}'.encode()).hexdigest()


def _font(size):
    for path in ('/System/Library/Fonts/Supplemental/Arial Bold.ttf', '/System/Library/Fonts/Helvetica.ttc',
                 '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 'C:/Windows/Fonts/arialbd.ttf'):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def make(out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    digits = ''.join(random.choice('23456789') for _ in range(4))
    color = random.choice(list(COLORS))
    img = Image.new('RGB', (800, 450), (245, 245, 240))
    draw = ImageDraw.Draw(img)
    draw.ellipse((40, 110, 270, 340), fill=COLORS[color])
    draw.text((330, 130), digits, fill=(20, 20, 20), font=_font(170))
    for _ in range(40):  # 干扰线，防止简单的文字提取
        x, y = random.randrange(800), random.randrange(450)
        draw.line((x, y, x + random.randrange(-60, 60), y + random.randrange(-60, 60)), fill=(200, 200, 200), width=2)
    img.save(out / PROBE)
    salt = secrets.token_hex(8)
    (out / STATE).write_text(json.dumps({'salt': salt, 'digest': _digest(salt, digits, color)}), encoding='utf-8')
    (out / PASS).unlink(missing_ok=True)
    print(f'已生成 {out / PROBE}：打开图片，回答图中的四位数字和圆形的颜色（红色/绿色/蓝色/橙色/紫色）。')


def verify(out, digits, color):
    out = Path(out)
    state = json.loads((out / STATE).read_text(encoding='utf-8'))
    ok = _digest(state['salt'], digits.strip(), color.strip()) == state['digest']
    if not ok:
        state['fails'] = state.get('fails', 0) + 1
        if state['fails'] >= 2:          # 不允许反复猜：答错两次作废，要重新 make
            (out / STATE).unlink()
        else:
            (out / STATE).write_text(json.dumps(state), encoding='utf-8')
        raise SystemExit('看图测验未通过：当前模型很可能收不到图片像素。停止选片，向用户报告“看不到画面”，不能输出正式版。')
    (out / PASS).write_text(json.dumps({'passed': True, 'at': datetime.now(timezone.utc).isoformat()},
                                       ensure_ascii=False), encoding='utf-8')
    (out / STATE).unlink()
    print(f'看图测验通过，已写 {out / PASS}')


def passed(directory):
    path = Path(directory) / PASS
    try:
        return json.loads(path.read_text(encoding='utf-8')).get('passed') is True
    except (OSError, ValueError):
        return False


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == 'make':
        make(sys.argv[2])
    elif len(sys.argv) == 5 and sys.argv[1] == 'verify':
        verify(*sys.argv[2:])
    else:
        raise SystemExit(__doc__)
