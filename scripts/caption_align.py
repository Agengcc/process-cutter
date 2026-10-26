#!/usr/bin/env python3
"""按真实发声时间给字幕定时：在每个讲话窗口里找出人声段，按字数比例把每行字幕分配到发声时间上。

输入 JSON：[{"source": "IMG_1.MOV", "start": 1.2, "end": 6.8, "lines": ["第一行", "第二行"]}, ...]
lines 由人核对后填写（转写只是线索）。
可选 --words-dir：每个源文件一份逐字时间 JSON（[{"w","s","e"}]，如 Whisper word_timestamps），
有它时按字匹配找每行的时间锚点；匹配不上的行才按字数比例分配。输出 字幕表.csv（源文件,源起秒,源止秒,文字），给 cutter.py render --captions 用。
这是比例估算，不是逐字强制对齐；审片时仍要看字幕是否和口型、声音对得上。
"""
import argparse
import csv
import difflib
import json
import re
from pathlib import Path
import numpy as np
from audio_review import adaptive_gate, envelope_db, load_audio

HOP = 0.02


def voiced_spans(video, start, end, merge_gap=0.25, min_len=0.12):
    samples, rate = load_audio(video, start, end)
    db = envelope_db(samples, rate)
    if len(db) == 0:
        return []
    active = db > adaptive_gate(db)
    spans, first = [], None
    for i, on in enumerate(list(active) + [False]):
        if on and first is None:
            first = i
        elif not on and first is not None:
            spans.append([first * HOP, i * HOP])
            first = None
    merged = []
    for s in spans:
        if merged and s[0] - merged[-1][1] < merge_gap:
            merged[-1][1] = s[1]
        else:
            merged.append(s)
    return [(start + a, start + b) for a, b in merged if b - a >= min_len]


def _chars(text):
    return re.sub(r'[^0-9A-Za-z\u4e00-\u9fff]', '', text)


def anchor_lines(lines, words, start, end):
    """把每行字幕和逐字转写按字符对齐，返回每行的(起,止)；对不上的行返回 None。"""
    stream = []  # (字, 时间)
    for w in words:
        if w['e'] < start or w['s'] > end:
            continue
        chars = _chars(w['w'])
        for k, ch in enumerate(chars):
            stream.append((ch, w['s'] + (w['e'] - w['s']) * (k + 0.5) / len(chars)))
    joined = ''.join(_chars(l) for l in lines)
    owner = [i for i, l in enumerate(lines) for _ in _chars(l)]
    times = [[] for _ in lines]
    matcher = difflib.SequenceMatcher(None, joined, ''.join(c for c, _ in stream), autojunk=False)
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            times[owner[block.a + k]].append(stream[block.b + k][1])
    return [(min(t), max(t)) if len(t) >= max(1, len(_chars(l)) // 3) else None for t, l in zip(times, lines)]


def align_window(video, start, end, lines, words=None):
    spans = voiced_spans(video, start, end) or [(start, end)]
    voiced = sum(b - a for a, b in spans)
    weights = [max(1, len(line.replace('，', '').replace('。', ''))) for line in lines]
    total = sum(weights)

    def at(fraction):  # 把“说到第几成”换算成源时间
        target, acc = fraction * voiced, 0.0
        for a, b in spans:
            if acc + (b - a) >= target - 1e-9:
                return a + (target - acc)
            acc += b - a
        return spans[-1][1]

    gaps = [(a[1], b[0]) for a, b in zip(spans, spans[1:])]  # 句间停顿

    def snap(t):  # 行与行的分界尽量落在真实停顿里（0.7秒内有停顿才挪）
        best = min(gaps, key=lambda g: abs((g[0] + g[1]) / 2 - t), default=None)
        if best and abs((best[0] + best[1]) / 2 - t) <= 0.7:
            return best
        return (t, t)

    cuts, done = [], 0
    for weight in weights[:-1]:
        done += weight
        cuts.append(snap(at(done / total)))
    starts = [spans[0][0]] + [g[1] for g in cuts]
    ends = [g[0] for g in cuts] + [spans[-1][1]]
    if words:  # 有逐字时间时，用匹配到的锚点替换比例估算，并收拢到所在的发声段边界
        for i, anchor in enumerate(anchor_lines(lines, words, start, end)):
            if anchor:
                a, b = anchor
                starts[i] = next((sa for sa, sb in spans if sa <= a <= sb), a - 0.1)
                ends[i] = next((sb for sa, sb in spans if sa <= b <= sb), b + 0.2)
        for i in range(1, len(lines)):
            starts[i] = max(starts[i], ends[i - 1] - 0.15) if starts[i] < ends[i - 1] else starts[i]
    # 每行在它最后一段发声结束后0.3秒内收掉，不盖住长时间的静默操作
    for i, (s, e) in enumerate(zip(starts, ends)):
        inside = [sb for sa, sb in spans if sa < e and sb > s]
        if inside:
            ends[i] = min(e, max(inside) + 0.3)
    rows = []
    for line, s, e in zip(lines, starts, ends):
        rows.append({'源文件': Path(video).name, '源起秒': round(max(start, s - 0.08), 2),
                     '源止秒': round(min(end, e + 0.15), 2), '文字': line})
    for prev, cur in zip(rows, rows[1:]):
        prev['源止秒'] = min(prev['源止秒'], cur['源起秒'])
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('windows', help='讲话窗口与字幕行 JSON')
    parser.add_argument('media_dir')
    parser.add_argument('output', help='输出 字幕表.csv')
    parser.add_argument('--words-dir', help='逐字时间 JSON 目录，文件名为 源文件名去扩展名.json')
    args = parser.parse_args()
    windows = json.loads(Path(args.windows).read_text(encoding='utf-8'))
    rows = []
    for w in windows:
        words = None
        if args.words_dir:
            path = Path(args.words_dir) / (Path(w['source']).stem + '.json')
            words = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
        rows += align_window(Path(args.media_dir) / w['source'], float(w['start']), float(w['end']), w['lines'], words)
    with open(args.output, 'w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.DictWriter(handle, fieldnames=['源文件', '源起秒', '源止秒', '文字'])
        writer.writeheader()
        writer.writerows(rows)
    print(f'{len(rows)} 条字幕 → {args.output}')


if __name__ == '__main__':
    main()
