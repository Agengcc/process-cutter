#!/usr/bin/env python3
"""讲话素材的声音复核图：音量包络＋逐字转写标注，用来把切点放在两句话之间的真实停顿里。

转写只是定位线索（炒菜声会被识别成“好”“okay”之类），保留哪句、字幕写什么仍要人工或第二个模型核对。
用法：python3 audio_review.py 素材.MOV 输出.png [--words 逐字.json] [--start 0 --end 30]
逐字.json 是 [{"w": 字词, "s": 起秒, "e": 止秒}, ...]。
"""
import argparse
import json
import math
import subprocess
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFont

PX_PER_SEC = 120
PAGE_SECONDS = 15
HEIGHT = 300


def load_audio(path, start, end, rate=16000):
    cmd = ['ffmpeg', '-v', 'error', '-ss', str(start), '-i', str(path)]
    if end is not None:
        cmd += ['-t', str(end - start)]
    # 只看人声频段：沸水、油锅声是宽频噪声，滤掉高低频后讲话的起伏更清楚。
    cmd += ['-af', 'highpass=f=200,lowpass=f=3500', '-ac', '1', '-ar', str(rate), '-f', 's16le', '-']
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0, rate


def envelope_db(samples, rate, window=0.02):
    hop = int(rate * window)
    frames = len(samples) // hop
    if frames == 0:
        return np.array([])
    rms = np.sqrt(np.mean(samples[:frames * hop].reshape(frames, hop) ** 2, axis=1) + 1e-12)
    return 20 * np.log10(rms)


def _font(size):
    for path in ('/System/Library/Fonts/Hiragino Sans GB.ttc', '/System/Library/Fonts/STHeiti Medium.ttc',
                 '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def adaptive_gate(db):
    """停顿阈值：本段底噪（第20百分位）往上6dB。"""
    return float(np.percentile(db, 20)) + 6.0


def draw_pages(video, output, words=None, start=0.0, end=None, gate_db=None):
    samples, rate = load_audio(video, start, end)
    db = envelope_db(samples, rate)
    gate = gate_db if gate_db is not None else adaptive_gate(db)
    duration = len(samples) / rate
    words = [w for w in (words or []) if start <= w['s'] < start + duration]
    font, small = _font(18), _font(14)
    pages = []
    for page in range(max(1, math.ceil(duration / PAGE_SECONDS))):
        t0, t1 = page * PAGE_SECONDS, min(duration, (page + 1) * PAGE_SECONDS)
        width = int((t1 - t0) * PX_PER_SEC) + 60
        image = Image.new('RGB', (width, HEIGHT), '#151515')
        draw = ImageDraw.Draw(image)
        floor, ceil = float(np.percentile(db, 5)), float(np.max(db)) + 1  # 按本段自身的响度范围显示
        base = 200
        for i in range(int(t0 / 0.02), min(len(db), int(t1 / 0.02))):
            x = 30 + (i * 0.02 - t0) * PX_PER_SEC
            level = (min(max(db[i], floor), ceil) - floor) / (ceil - floor)
            draw.line([(x, base), (x, base - level * 150)], fill='#4fc3f7' if db[i] > gate else '#2e5f73', width=2)
        for tick in np.arange(math.ceil(t0 * 2) / 2, t1 + 1e-6, 0.5):
            x = 30 + (tick - t0) * PX_PER_SEC
            whole = abs(tick - round(tick)) < 1e-6
            draw.line([(x, base), (x, base + (12 if whole else 6))], fill='#bbbbbb')
            if whole:
                draw.text((x + 2, base + 12), f'{start + tick:.0f}s', fill='#dddddd', font=small)
        for w in words:
            ws, we = w['s'] - start, w.get('e', w['s']) - start
            if ws < t0 or ws >= t1:
                continue
            x0, x1 = 30 + (ws - t0) * PX_PER_SEC, 30 + (min(we, t1) - t0) * PX_PER_SEC
            draw.rectangle([x0, 236, max(x1, x0 + 2), 240], fill='#ffd54f')
            draw.text((x0, 246 + (hash(w['s']) % 2) * 22), w['w'], fill='#ffd54f', font=font)
        draw.text((6, 6), f'{Path(video).name}  {start + t0:.1f}–{start + t1:.1f}s  亮蓝=人声频段高于{gate:.0f}dB  黄=转写字词（仅线索）',
                  fill='white', font=small)
        out = Path(output) if page == 0 else Path(output).with_name(f'{Path(output).stem}_{page + 1:02d}{Path(output).suffix}')
        out.parent.mkdir(parents=True, exist_ok=True)
        image.save(out)
        pages.append(str(out))
    return pages


def quiet_gaps(video, start=0.0, end=None, threshold_db=None, min_gap=0.15):
    """列出音量低于阈值的停顿区间，作为句间切点候选（现场声大时阈值需按波形图调整）。"""
    samples, rate = load_audio(video, start, end)
    db = envelope_db(samples, rate)
    threshold_db = threshold_db if threshold_db is not None else adaptive_gate(db)
    gaps, first = [], None
    for i, value in enumerate(list(db) + [0.0]):
        if value < threshold_db and first is None:
            first = i
        elif value >= threshold_db and first is not None:
            if (i - first) * 0.02 >= min_gap:
                gaps.append({'start': round(start + first * 0.02, 2), 'end': round(start + i * 0.02, 2)})
            first = None
    return gaps


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('video')
    parser.add_argument('output')
    parser.add_argument('--words', help='逐字转写 JSON')
    parser.add_argument('--start', type=float, default=0.0)
    parser.add_argument('--end', type=float)
    parser.add_argument('--gap-db', type=float, help='停顿判定阈值（dB）；默认取本段底噪+6dB')
    args = parser.parse_args()
    words = json.loads(Path(args.words).read_text(encoding='utf-8')) if args.words else None
    pages = draw_pages(args.video, args.output, words, args.start, args.end, args.gap_db)
    print(json.dumps({'pages': pages, 'quiet_gaps': quiet_gaps(args.video, args.start, args.end, args.gap_db),
                      'note': '停顿与转写都只是线索；切点仍要结合画面确认。'}, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
