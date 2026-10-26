#!/usr/bin/env python3
"""回归评测：拿一份用户认可的“标准答案”，给待测版本的选片表或工程打分，不出片、不看片。

用途：换模型、换 Agent 或改 skill 后，几分钟内知道选片有没有退步。分数只看源文件时间段，
不能判断“有效动作”写得对不对——那仍要看选片对照图。

用法：
  python3 benchmark.py make  认可版目录 --name 名字 --out answer.json [--media-dir 原素材目录]   # 从认可版生成答案草稿
  python3 benchmark.py score answer.json 待测的选片表.csv或project.json [--report 评分.md] [--skip-vision]

答案草稿生成后要人工补：opening（允许的开头）、各工序 required、note（这版好在哪）。
格式见 benchmarks/README.md。答案里不写本机路径。
"""
import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vision_check

WEIGHTS = {'coverage': 50, 'opening': 15, 'cuts': 15, 'count': 10, 'duration': 10}
HARD_PENALTY = 25
COVER_RATIO = 0.5   # 待测版本覆盖某工序时间段一半以上，才算有这道工序


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def read_shots(path):
    """读选片表或工程，统一成 [{file, in, out, speed, kind, usage, duration}]；duration 为成片时长。"""
    path = Path(path)
    shots, media = [], {}
    if path.suffix.lower() == '.json':
        project = json.loads(path.read_text(encoding='utf-8'))
        media = {m['id']: m for m in project.get('media', [])}
        clips = [c for t in project.get('tracks', []) if t.get('kind') == 'video' for c in t.get('clips', [])]
        for clip in sorted(clips, key=lambda c: c.get('start', 0)):
            m = media.get(clip.get('media'), {})
            name = m.get('name') or Path(m.get('path', '')).name
            speed = _f(clip.get('speed'), 1.0) or 1.0
            dur = _f(clip.get('duration'))
            image = m.get('kind') == 'image'
            shots.append({'file': name, 'in': _f(clip.get('in')), 'out': _f(clip.get('in')) + (0 if image else dur * speed),
                          'speed': speed, 'kind': '封面' if image else '', 'usage': clip.get('name', ''),
                          'duration': dur, 'hdr': m.get('hdr')})
        return shots
    with path.open(encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            if not any(row.values()):
                continue
            a, b, speed = _f(row.get('源入点秒')), _f(row.get('源出点秒')), _f(row.get('速度倍率'), 1.0) or 1.0
            kind = (row.get('片段类型') or '').strip()
            dur = _f(row.get('成片时长秒'), 0.2) if kind == '封面' or b <= a else (b - a) / speed
            shots.append({'file': (row.get('源文件') or '').strip(), 'in': a, 'out': b, 'speed': speed,
                          'kind': kind, 'usage': row.get('用途') or '', 'duration': dur, 'hdr': None,
                          'action': row.get('有效动作') or ''})
    return shots


def media_durations(version_dir, media_dir=None):
    if media_dir:  # 原片还在：列出全部素材，用 ffprobe 量时长
        import cutter
        files = sorted(p for p in Path(media_dir).iterdir() if p.suffix.lower() in ('.mov', '.mp4', '.m4v'))
        return [{'file': p.name, 'duration': round(_f(cutter.probe_media(str(p))['duration']), 3)} for p in files]
    project = Path(version_dir) / 'project.json'
    if not project.is_file():
        return []
    data = json.loads(project.read_text(encoding='utf-8'))
    return [{'file': m.get('name') or Path(m['path']).name, 'duration': round(_f(m.get('duration')), 3)}
            for m in data.get('media', []) if m.get('kind') == 'video']


def make_answer(version_dir, name, media_dir=None):
    version_dir = Path(version_dir)
    shots = read_shots(version_dir / '选片表.csv')
    real = [s for s in shots if s['kind'] != '封面']
    total = sum(s['duration'] for s in shots)
    first = real[0] if real else None
    return {
        'name': name,
        'note': '（由用户填写：这版好在哪，一两句话）',
        'tolerance': 0.5,
        'media': media_durations(version_dir, media_dir),
        'steps': [{'name': s.get('action') or s['usage'], 'file': s['file'], 'in': s['in'], 'out': s['out'],
                   'required': True} for s in real],
        'opening': [{'file': first['file'], 'in': first['in'], 'out': first['out']}] if first else [],
        'shot_count': [max(1, round(len(shots) * 0.75)), round(len(shots) * 1.3)],
        'duration': [round(total * 0.75, 1), round(total * 1.3, 1)],
    }


def _overlap(a, b, c, d):
    return max(0.0, min(b, d) - max(a, c))


def _in_range(value, bounds):
    return bounds[0] <= value <= bounds[1]


def score(answer, shots, vision_ok=None):
    """返回 {'total', 'items', 'hard_errors', 'warnings', 'steps'}；vision_ok=None 表示不检查看图校验。"""
    tol = _f(answer.get('tolerance'), 0.5)
    real = [s for s in shots if s['kind'] != '封面']
    hard, warn = [], []

    # 硬性错误：同源重叠、HDR 未转、超出素材时长、未通过看图校验
    seen = {}
    for i, s in enumerate(real, 1):
        if '回放' in s['usage']:
            continue
        for j, (a, b) in seen.get(s['file'], []):
            if _overlap(a, b, s['in'], s['out']) > 0.1:
                hard.append(f"第{j}段与第{i}段在 {s['file']} 的 {max(a, s['in']):.2f}–{min(b, s['out']):.2f} 秒重叠")
        seen.setdefault(s['file'], []).append((i, (s['in'], s['out'])))
        if s.get('hdr'):
            hard.append(f"第{i}段 {s['file']} 是 HDR（{s['hdr']}）未转 SDR")
    known = {m['file']: m['duration'] for m in answer.get('media', [])}
    for i, s in enumerate(real, 1):
        if s['file'] not in known:
            warn.append(f"第{i}段 {s['file']} 不在答案的素材清单里（素材对不上，或用了答案没用的素材）")
        elif s['out'] > known[s['file']] + 0.05:
            hard.append(f"第{i}段出点 {s['out']:.2f} 秒超出 {s['file']} 时长 {known[s['file']]:.2f} 秒")
    if vision_ok is False:
        hard.append('选片表目录没有通过的看图校验（看图校验.json）')

    # 工序覆盖与切点偏差
    rows, covered_req, req, precise = [], 0, 0, 0
    for step in answer.get('steps', []):
        a, b = _f(step['in']), _f(step['out'])
        hits = [s for s in real if s['file'] == step['file'] and _overlap(a, b, s['in'], s['out']) > 0]
        cover = sum(_overlap(a, b, s['in'], s['out']) for s in hits) / max(b - a, 1e-6)
        ok = cover >= COVER_RATIO
        d_in = min((abs(s['in'] - a) for s in hits), default=None)
        d_out = min((abs(s['out'] - b) for s in hits), default=None)
        exact = ok and d_in is not None and d_in <= tol and d_out <= tol
        if step.get('required', True):
            req += 1
            covered_req += ok
            precise += exact
        rows.append({'name': step['name'], 'file': step['file'], 'range': (a, b), 'required': step.get('required', True),
                     'cover': min(cover, 1.0), 'ok': ok, 'd_in': d_in, 'd_out': d_out, 'exact': exact})

    first = real[0] if real else None
    # 首镜头至少一半落在允许的开头范围内才算命中；不写 in/out 表示该素材任意段都可
    opening_ok = bool(first) and any(
        first['file'] == o['file'] and ('in' not in o or _overlap(_f(o['in']), _f(o['out']), first['in'], first['out'])
                                        >= COVER_RATIO * max(first['out'] - first['in'], 1e-6))
        for o in answer.get('opening', []))
    total_dur = sum(s['duration'] for s in shots)
    count_ok = _in_range(len(shots), answer.get('shot_count', [0, 10 ** 6]))
    dur_ok = _in_range(total_dur, answer.get('duration', [0, 10 ** 6]))

    cov = covered_req / req if req else 1.0
    cut = precise / covered_req if covered_req else 0.0
    items = {
        'coverage': (WEIGHTS['coverage'] * cov, f'必要工序 {covered_req}/{req}'),
        'opening': (WEIGHTS['opening'] * opening_ok,
                    f"开头 {first['file']} {first['in']:.2f}–{first['out']:.2f}" if first else '没有镜头'),
        'cuts': (WEIGHTS['cuts'] * cut, f'已覆盖工序中切点都在 ±{tol:g} 秒内的 {precise}/{covered_req}'),
        'count': (WEIGHTS['count'] * count_ok, f"镜头数 {len(shots)}，要求 {answer.get('shot_count')}"),
        'duration': (WEIGHTS['duration'] * dur_ok, f"成片约 {total_dur:.1f} 秒，要求 {answer.get('duration')}"),
    }
    total = max(0.0, sum(v for v, _ in items.values()) - HARD_PENALTY * len(hard))
    return {'total': round(total, 1), 'items': items, 'hard_errors': hard, 'warnings': warn, 'steps': rows}


def report_md(answer, result, target):
    labels = {'coverage': '工序覆盖', 'opening': '开头', 'cuts': '切点', 'count': '镜头数', 'duration': '时长'}
    lines = [f"# 评分：{answer.get('name', '')}", '',
             f"- 待测：`{Path(target).parent.name}/{Path(target).name}`",
             f"- 总分：**{result['total']} / 100**" + ('（有硬性错误，不合格）' if result['hard_errors'] else ''),
             f"- 答案说明：{answer.get('note', '')}", '', '| 项 | 得分 | 说明 |', '|---|---|---|']
    for key, (value, text) in result['items'].items():
        lines.append(f"| {labels[key]} | {value:.1f} / {WEIGHTS[key]} | {text} |")
    lines += ['', '## 硬性错误', ''] + ([f'- {e}' for e in result['hard_errors']] or ['- 无'])
    if result['warnings']:
        lines += ['', '## 提醒', ''] + [f'- {w}' for w in result['warnings']]
    lines += ['', '## 逐道工序', '', '| 工序 | 源文件 | 答案时间段 | 必要 | 覆盖 | 入点偏差 | 出点偏差 |', '|---|---|---|---|---|---|---|']
    for r in result['steps']:
        mark = '✓' if r['ok'] else '✗ 缺'
        dev = lambda d: '—' if d is None else f'{d:.2f}'
        lines.append(f"| {r['name']} | {r['file']} | {r['range'][0]:.2f}–{r['range'][1]:.2f} | "
                     f"{'是' if r['required'] else '否'} | {mark} {r['cover']:.0%} | {dev(r['d_in'])} | {dev(r['d_out'])} |")
    lines += ['', '分数只按源文件时间段计算，不能证明“有效动作”写得对；仍需看选片对照图。']
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description='选片回归评测')
    sub = parser.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('make', help='从认可版目录生成答案草稿')
    p.add_argument('version_dir')
    p.add_argument('--name', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--media-dir', help='原素材目录；给了就列出全部素材，不给只列认可版用到的')
    s = sub.add_parser('score', help='给待测版本打分')
    s.add_argument('answer')
    s.add_argument('target', help='选片表.csv 或 project.json')
    s.add_argument('--report', help='评分.md 输出路径；不给只打印')
    s.add_argument('--skip-vision', action='store_true', help='不检查看图校验（用于看图测验上线前的历史版本）')
    args = parser.parse_args()

    if args.cmd == 'make':
        answer = make_answer(args.version_dir, args.name, args.media_dir)
        Path(args.out).write_text(json.dumps(answer, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(f"已写 {args.out}：{len(answer['steps'])} 道工序。请人工补 opening、required 和 note。")
        return
    answer = json.loads(Path(args.answer).read_text(encoding='utf-8'))
    vision = None if args.skip_vision else vision_check.passed(Path(args.target).resolve().parent)
    result = score(answer, read_shots(args.target), vision)
    text = report_md(answer, result, args.target)
    if args.report:
        Path(args.report).write_text(text, encoding='utf-8')
    print(text)


if __name__ == '__main__':
    main()
