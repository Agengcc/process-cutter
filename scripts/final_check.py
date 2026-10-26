#!/usr/bin/env python3
"""成片逐句核对：从最终成片里把每一句重新转写，和字幕文字比对，找出被吞、被切、被盖住的句子。

方法借自 TinyAlmond/footage-to-vlog 的 finish.py --verify（MIT）：
每句取两个宽窄不同的窗口各转写一次（Whisper 在边界辅音上会摇摆），取较好的一次；
字幕里的字在听到的文字里找不回来，就标“缺字”。原声和配音都适用。

它替代不了人耳：通过只说明 Whisper 在成片里听得出这句话；标出来的句子请用户重点听。

用法：python3 final_check.py 成片.mp4 [成片.字幕时间.json] [--report 逐句核对.md]
      python3 final_check.py 成片.mp4 --project project.json --captions 字幕表.csv   （旧版字幕时间文件没有源时间时）
字幕时间文件由 cutter.py render --captions 自动生成。
"""
import argparse
import difflib
import json
from pathlib import Path

import asr_tools as asr
import captions as caption_tools

WINDOWS = ((0.2, 0.3), (0.5, 0.6))


def missing_chars(expected, heard):
    e, h = asr.norm(expected), asr.norm(heard)
    hit = [False] * len(e)
    for m in difflib.SequenceMatcher(None, e, h, autojunk=False).get_matching_blocks():
        for k in range(m.a, m.a + m.size):
            hit[k] = True
    return ''.join(c if not ok else '·' for c, ok in zip(e, hit)).strip('·') if not all(hit) else ''


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('video')
    ap.add_argument('lines', nargs='?')
    ap.add_argument('--report')
    ap.add_argument('--project', help='project.json；与 --captions 一起用来算每句的源时间')
    ap.add_argument('--captions', help='字幕表.csv')
    ap.add_argument('--min-recall', type=float, default=0.85, help='字幕里的字至少要听回这么多（0.85）')
    a = ap.parse_args()
    video = Path(a.video)
    if a.project and a.captions:
        project = json.loads(Path(a.project).read_text(encoding='utf-8'))
        clips = project['tracks'][0]['clips']
        media = {m['id']: m for m in project['media']}
        lines = caption_tools.map_to_timeline(caption_tools.load_captions(a.captions), clips, media,
                                              float(project['settings']['fps']))
        lines_path = Path(a.captions)
    else:
        lines_path = Path(a.lines) if a.lines else video.with_suffix('.字幕时间.json')
        lines = json.loads(lines_path.read_text(encoding='utf-8'))
    x = asr.load_audio(video)
    dur = len(x) / asr.SR
    sources = {}

    def best_of(audio, a0, a1, text):
        total = len(audio) / asr.SR
        best = None
        for pa, pb in WINDOWS:
            heard = asr.transcribe(asr.cut(audio, max(0, a0 - pa), min(total, a1 + pb)))
            score = asr.recall(text, heard)
            if best is None or score > best[1]:
                best = (heard, score)
        return best

    rows = []
    for n, ln in enumerate(lines, 1):
        heard, score = best_of(x, ln['start'], ln['end'], ln['text'])
        src_score = None
        if ln.get('sources'):
            parts = []
            for sp in ln['sources']:
                if sp['path'] not in sources:
                    sources[sp['path']] = asr.load_audio(sp['path'])
                parts.append(best_of(sources[sp['path']], sp['start'], sp['end'], ln['text'])[0])
            src_score = asr.recall(ln['text'], ''.join(parts))
        if score >= a.min_recall:
            verdict = '听得出'
        elif src_score is None or src_score - score >= 0.15 or src_score >= a.min_recall:
            verdict = f'**重点听**：疑似缺“{missing_chars(ln['text'], heard)}”' + ('' if src_score is None else '（原素材听得出，成片变差）')
        else:
            verdict = f'原素材这里也没听出“{missing_chars(ln['text'], heard)}”：字幕多写了字／选错一遍／太吵，请听'
        rows.append((n, ln['start'], ln['end'], ln['text'], heard, score, src_score, verdict))
        print(f"{n:3d} {ln['start']:7.2f} 成片{score:4.0%} 原{'' if src_score is None else f'{src_score:4.0%}'} {verdict[:14]:14s} {ln['text']}  ←  {heard}", flush=True)
    first = [r for r in rows if r[7].startswith('**')]
    second = [r for r in rows if r[7].startswith('原素材')]
    head = '| # | 成片时间 | 字幕 | 成片里听到 | 成片听回 | 原素材听回 | 结论 |\n|---|---|---|---|---|---|---|'
    fmt = lambda r: (f"| {r[0]} | {r[1]:.2f}–{r[2]:.2f} | {r[3]} | {r[4][:40]} | {r[5]:.0%} | "
                     f"{'—' if r[6] is None else f'{r[6]:.0%}'} | {r[7]} |")
    out = ['# 成片逐句核对', '',
           f'成片：`{video.name}`；字幕：`{lines_path.name}`。共{len(rows)}句：'
           f'**先听{len(first)}句**（成片比原素材差），再听{len(second)}句（原素材这里也没听出）。',
           '“听回”= 字幕里的字在转写里找回的比例。“听得出”只说明Whisper听得出，不等于人耳满意。', '',
           '## 先听：成片变差（剪接切到字、混音盖住）', '', head] + [fmt(r) for r in first] + [
           '', '## 再听：原素材这里也没听出', '', head] + [fmt(r) for r in second] + [
           '', '## 全部', '', head] + [fmt(r) for r in rows]
    flagged = first + second
    report = Path(a.report) if a.report else video.with_name(video.stem + '.逐句核对.md')
    report.write_text('\n'.join(out) + '\n', encoding='utf-8')
    print(f'先听{len(first)}句、再听{len(second)}句（共{len(rows)}句）；报告：{report}')


if __name__ == '__main__':
    main()
