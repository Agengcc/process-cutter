#!/usr/bin/env python3
"""字幕：按“源文件+源时间”标注，渲染时换算到成片时间并画成透明图片叠加。

字幕表.csv 列：源文件,源起秒,源止秒,文字。时间写原素材里的时间，所以改选片、改速度后
字幕会自动跟着走；某句被剪掉，它的字幕也就不会出现。不依赖 FFmpeg 的 libass/drawtext。
"""
import csv
import math
import subprocess
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = [
    ('PingFang SC', 'Semibold'), ('PingFang SC', 'Medium'), ('PingFang SC', 'Regular'),
    ('Hiragino Sans GB', 'W6'), ('Hiragino Sans GB', 'W3'),
    ('Noto Sans CJK SC', 'Bold'), ('Noto Sans CJK SC', 'Regular'),
    ('Source Han Sans SC', 'Bold'), ('Microsoft YaHei', 'Bold'), ('Microsoft YaHei', 'Regular'),
]
STATIC_FONT_PATHS = ['/System/Library/Fonts/Hiragino Sans GB.ttc', '/System/Library/Fonts/STHeiti Medium.ttc',
                     '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc', 'C:/Windows/Fonts/msyhbd.ttc']


def load_captions(path):
    rows = []
    with open(path, encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        missing = {'源文件', '源起秒', '源止秒', '文字'} - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f'字幕表缺少列: {", ".join(sorted(missing))}')
        for number, row in enumerate(reader, 1):
            if not any((v or '').strip() for v in row.values()):
                continue
            text = (row['文字'] or '').strip()
            start, end = float(row['源起秒']), float(row['源止秒'])
            if not text:
                raise ValueError(f'字幕第{number}行没有文字')
            if not (math.isfinite(start) and math.isfinite(end)) or start < 0 or end <= start:
                raise ValueError(f'字幕第{number}行时间无效')
            rows.append({'source': row['源文件'].strip(), 'start': start, 'end': end, 'text': text})
    return rows


def map_to_timeline(captions, clips, media_by_id, fps):
    """把源时间字幕换算成成片时间。只保留落在选段里的部分；不足0.3秒的残片丢弃。"""
    placed = []
    for cap in captions:
        for clip in clips:
            media = media_by_id[clip['media']]
            if media['kind'] != 'video' or Path(media['path']).name != Path(cap['source']).name:
                continue
            speed = float(clip.get('speed') or 1.0)
            frames = int(round(float(clip['duration']) * fps))
            src_in = float(clip['in'])
            src_out = src_in + frames / fps * speed
            a, b = max(cap['start'], src_in), min(cap['end'], src_out)
            if b - a <= 0:
                continue
            start = float(clip['start']) + (a - src_in) / speed
            end = float(clip['start']) + (b - src_in) / speed
            if end - start >= 0.3:
                placed.append({'start': round(start, 4), 'end': round(end, 4), 'text': cap['text'],
                               'sources': [{'path': media['path'], 'start': round(a, 4), 'end': round(b, 4)}]})
    placed.sort(key=lambda c: c['start'])
    merged = []
    for cap in placed:  # 同一句跨相邻两段时合成一条，避免闪一下
        if merged and merged[-1]['text'] == cap['text'] and cap['start'] - merged[-1]['end'] < 0.05:
            merged[-1]['end'] = cap['end']
            merged[-1]['sources'] = merged[-1]['sources'] + cap['sources']
        else:
            merged.append(dict(cap))
    for prev, cur in zip(merged, merged[1:]):  # 前后两句重叠时，前一句提前收
        if cur['start'] < prev['end']:
            prev['end'] = cur['start']
    return [c for c in merged if c['end'] - c['start'] >= 0.3]


def find_font(font_path=None):
    if font_path:
        return font_path, 0
    try:
        listing = subprocess.run(['fc-list', ':', 'family', 'style', 'file'], capture_output=True, text=True).stdout
    except FileNotFoundError:
        listing = ''
    entries = []  # fc-list 每行形如 "/路径/字体.ttc: 家族,别名:style=字重,别名"
    for line in listing.splitlines():
        file_part, _, rest = line.partition(': ')
        families, _, styles = rest.partition(':style=')
        entries.append((file_part.strip(), families.split(','), styles.split(',')))
    for family, style in FONT_CANDIDATES:
        for file_part, families, styles in entries:
            if family in families and style in styles:
                return file_part, (family, style)
    for path in STATIC_FONT_PATHS:
        if Path(path).exists():
            return path, 0
    raise RuntimeError('找不到中文字体，请用 --caption-font 指定一个 .ttf/.ttc/.otf 文件')


def _open_font(path, wanted, size):
    if not str(path).lower().endswith('.ttc') or not isinstance(wanted, tuple):
        return ImageFont.truetype(str(path), size)
    for index in range(32):  # .ttc 里按字体名和字重挑一个
        try:
            font = ImageFont.truetype(str(path), size, index=index)
        except OSError:
            break
        family, style = font.getname()
        if family == wanted[0] and style == wanted[1]:
            return font
    return ImageFont.truetype(str(path), size)


def wrap(text, font, max_width, draw):
    """按画面宽度折行；中文逐字折，最多两行，超出时报错让人改短。"""
    lines, current = [], ''
    for char in text:
        trial = current + char
        if draw.textlength(trial, font=font) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = char
    if current:
        lines.append(current)
    if len(lines) > 2:
        raise ValueError(f'字幕太长，超过两行：「{text}」，请拆成两条')
    return lines


def render_caption_images(captions, width, height, directory, font_path=None, size=None):
    """每条字幕画成一张与画布等大的透明 PNG：白字黑描边，位于画面下方三分之一，避开平台底部按钮区。"""
    path, wanted = find_font(font_path)
    size = size or round(width * 0.06)
    font = _open_font(path, wanted, size)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stroke = max(3, round(size * 0.09))
    files = []
    for index, cap in enumerate(captions):
        image = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        lines = wrap(cap['text'], font, width * 0.84, draw)
        line_height = round(size * 1.3)
        bottom = round(height * 0.80)  # 9:16 竖屏时约在 y=1536，标题/评论按钮之上
        top = bottom - line_height * len(lines)
        for row, line in enumerate(lines):
            draw.text((width / 2, top + row * line_height), line, font=font, anchor='mt',
                      fill=(255, 255, 255, 255), stroke_width=stroke, stroke_fill=(0, 0, 0, 230))
        out = directory / f'caption_{index + 1:03d}.png'
        image.save(out)
        files.append(str(out))
    return files
