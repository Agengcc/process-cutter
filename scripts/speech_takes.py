#!/usr/bin/env python3
"""原声清单：每段素材里说了哪些话、哪句说了几遍、哪些像拍摄沟通。只出清单，不剪视频。

方法来源（均为MIT）：
- vincentventalon/claude-code-video-editing-skill edit_video.py：按静音切成“小岛”、每个小岛单独转写、
  一串小岛的文字正好是后面那句的开头 = 说了一半重来；只在静音里下刀。
- TinyAlmond/footage-to-vlog speech.py：静音阈值 = 本段第20百分位音量 + 6 dB；去掉 Whisper 常编的套话；
  每句从两个错开的窗口再转写一次，真人说话两次结果一致（稳），噪声会变成不同的字（不稳）。
本 skill 的改动：按汉字比较；两遍都说完整时不自动删，标“选一遍”交人选（拉面案例里用户选的是中间那遍）；
拍摄沟通常用语只标“建议删”；整段转写对照，发现有话没进任何小岛时整段标“切点不可靠”。

所有判断都只是建议，交用户确认后才写进选片表。

用法：python3 speech_takes.py 素材目录 输出目录 [--rnn 降噪模型.rnnn] [--files A.MOV B.MOV]
      （有油烟机等持续底噪时给 --rnn：降噪版只用来找静音，转写仍用原声）
"""
import argparse
import difflib
import json
from pathlib import Path

import numpy as np

import asr_tools as asr

FILLERS = set('嗯呃额啊哦唔噢欸诶哎')
LEADINS = ['然后', '那个', '就是', '那', '好', '对', '嗯', '呃', '所以']
CHAT = ['再来一遍', '再来一次', '再来一点', '重来', '声音大', '大声点', '走近', '走进', '靠近', '不要讲这个',
        '我要说啥', '说啥来着', '讲解一下', '等一下', '拍好了', '开始了吗', '录上了吗', '我要先说', '重新说']
HOP = 0.03


def frames_db(x):
    n = int(HOP * asr.SR)
    k = len(x) // n
    return 20 * np.log10(np.sqrt((x[:k * n].reshape(k, n) ** 2).mean(1)) + 1e-9)


def silences(db, thr, min_len):
    quiet = np.append(db < thr, False)
    out, start = [], None
    for i, q in enumerate(quiet):
        if q and start is None:
            start = i
        elif not q and start is not None:
            if (i - start) * HOP >= min_len:
                out.append((start * HOP, i * HOP))
            start = None
    return out


def islands_between(sils, a, b, min_len=0.12):
    isl, cur = [], a
    for s, e in sorted(sils):
        if s - cur >= min_len:
            isl.append((cur, min(s, b)))
        cur = max(cur, e)
    if b - cur >= min_len:
        isl.append((cur, b))
    return isl


def toks(text):
    return list(asr.norm(text))


def strip_lead(t):
    s = ''.join(t)
    changed = True
    while changed and len(s) > 2:
        changed = False
        for lead in LEADINS:
            if s.startswith(lead) and len(s) > len(lead) + 1:
                s, changed = s[len(lead):], True
    return list(s)


def starts_like(ab, rest):
    a, s = ''.join(ab), ''.join(rest)
    if len(a) < 2 or len(s) < len(a):
        return 0.0
    best = 0.0
    for k in range(-1, 2):
        m = difflib.SequenceMatcher(None, a, s[:max(1, len(a) + k)], autojunk=False)
        b0 = m.get_matching_blocks()[0]
        if b0.a == 0 and b0.b == 0 and b0.size >= 1:
            best = max(best, m.ratio())
    return best


def is_start_of(ab, rest, run=1):
    a, r = strip_lead(ab), strip_lead(rest)
    if run > 1:
        ha, hr = ''.join(a)[:3], ''.join(r)[:3]
        if difflib.SequenceMatcher(None, ha, hr[:len(ha)], autojunk=False).ratio() < 0.6:
            return False
    return starts_like(a, r) >= 0.74


def restarts(ti, tj):
    """后一句把前一句的开头重说一遍，结尾可以不同：前5个字相似 >= 0.8。"""
    ti, tj = strip_lead(ti), strip_lead(tj)
    if len(ti) < 5 or len(tj) < 5:
        return False
    return difflib.SequenceMatcher(None, ''.join(ti[:5]), ''.join(tj[:5]), autojunk=False).ratio() >= 0.8


def sentence_done(text):
    return text.rstrip(' ”」』）)').endswith(('。', '！', '？', '.', '!', '?'))


def false_starts(items, gap=10.0, span=6):
    """items=[(a, b, text)]。返回 {下标: (类型, 说明)}；类型 half=说了一半重来，whole=两遍都完整。"""
    T = [toks(t) for _a, _b, t in items]
    out, i = {}, 0
    while i < len(items):
        found = None
        for j in range(i + 1, min(i + 1 + span, len(items))):
            if items[j][0] - items[j - 1][1] > gap:
                break
            ab = [t for k in range(i, j) for t in T[k]]
            nxt = [t for k in range(j, min(j + span, len(items))) for t in T[k]]
            if ab and (is_start_of(ab, nxt, j - i) or (j - i <= 3 and restarts(T[i], T[j]))):
                found = j
                break
        if found is None or len([t for k in range(i, found) for t in T[k]]) <= 1:
            i += 1
            continue
        nx_t = strip_lead([t for k in range(found, min(found + 6, len(items))) for t in T[k]])
        for k in range(i, found):
            own = strip_lead(T[k])        # 逐个小岛判断：自己就是一整句、和后面那遍开头一致，才算“两遍都完整”
            whole = (len(own) >= 5 and sentence_done(items[k][2]) and
                     difflib.SequenceMatcher(None, ''.join(own), ''.join(nx_t[:len(own)]), autojunk=False).ratio() >= 0.8)
            out[k] = ('whole' if whole else 'half', f'{items[found][0]:.2f}秒处又说：“{items[found][2][:24]}”')
        i = found
    return out


def repeats_itself(text):
    t = toks(text)
    for n in range(2, 7):
        for i in range(len(t) - 2 * n + 1):
            if t[i:i + n] == t[i + n:i + 2 * n]:
                return ''.join(t[i:i + n])
    return None


def analyse(path, rnn):
    raw = asr.load_audio(path)
    find = asr.load_audio(path, f'arnndn=m={rnn}:mix=0.75,highpass=f=70,volume=6.4dB') if rnn else raw
    dur = len(raw) / asr.SR
    db = frames_db(find)
    thr = max(float(np.percentile(db, 20)) + 6, -55.0)
    isl = islands_between(silences(db, thr, 0.22), 0.0, dur)
    whole_text = asr.transcribe(raw)
    rows, live = [], []
    for k, (a, b) in enumerate(isl):
        lo = isl[k - 1][1] + 0.02 if k else 0.0
        hi = isl[k + 1][0] - 0.02 if k + 1 < len(isl) else dur
        text = asr.transcribe(asr.cut(raw, max(lo, a - 0.15), min(hi, b + 0.15)))
        if not asr.norm(text):
            rows.append([a, b, '无字', text, '声响或呼吸，没有字'])
            continue
        alts = [asr.transcribe(asr.cut(raw, max(lo, a + d0), min(hi, b + d1))) for d0, d1 in ((-0.3, 0.1), (-0.05, 0.4))]
        # 三次里有两次一致就算稳，文字取一致的那两次里字多的一次
        trio = [text] + alts
        pairs = sorted(((asr.similar(trio[i], trio[j]), i, j) for i, j in ((0, 1), (0, 2), (1, 2))), reverse=True)
        agree, i, j = pairs[0]
        if agree >= 0.6:
            text = max(trio[i], trio[j], key=lambda t: len(asr.norm(t)))
        if not asr.norm(text):
            rows.append([a, b, '无字', text, '错开再听也没有字'])
        elif agree < 0.6:
            rows.append([a, b, '不稳', text, f'错开再听两次结果不同（{" / ".join(alts)}），多半是噪声；画面里有人在说话就请听'])
        elif all(c in FILLERS for c in asr.norm(text)):
            rows.append([a, b, '建议删', text, '只有语气词'])
        else:
            live.append((a, b, text))
    fs = false_starts(live)
    for k, (a, b, text) in enumerate(live):
        if k in fs:
            kind, why = fs[k]
            rows.append([a, b, '选一遍', text, f'这遍也说完整了，{why}；默认留后一遍，请听后选'] if kind == 'whole'
                        else [a, b, '建议删', text, f'说了一半重来，{why}'])
            continue
        rep = repeats_itself(text)
        hit = next((c for c in CHAT if c in asr.norm(text)), None)
        if rep:
            rows.append([a, b, '待听', text, f'句内重复“{rep}”，没有静音可下刀'])
        elif hit:
            rows.append([a, b, '建议删', text, f'像拍摄沟通（“{hit}”）'])
        else:
            rows.append([a, b, '留', text, ''])
    rows.sort(key=lambda r: r[0])
    heard = ''.join(r[3] for r in rows if r[2] not in ('无字', '不稳'))
    cover = asr.recall(whole_text, heard)
    return {'file': path.name, 'duration': round(dur, 2), 'threshold_db': round(thr, 1), 'whole_text': whole_text,
            'coverage': round(cover, 2), 'rows': [[round(r[0], 2), round(r[1], 2)] + r[2:] for r in rows]}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('src')
    ap.add_argument('out')
    ap.add_argument('--rnn', help='arnndn 降噪模型；只用来找静音')
    ap.add_argument('--files', nargs='*', help='只分析这些文件')
    a = ap.parse_args()
    src, out = Path(a.src), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    names = a.files or sorted(p.name for p in src.iterdir() if p.suffix.lower() in ('.mov', '.mp4', '.m4v'))
    results = []
    for name in names:
        r = analyse(src / name, a.rnn)
        results.append(r)
        print(f"{name}: {len(r['rows'])}个小岛，覆盖{r['coverage']:.0%}", flush=True)
    (out / '原声清单.json').write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding='utf-8')
    mark = {'留': '留', '建议删': '**建议删**', '选一遍': '**选一遍**', '待听': '**待听**', '不稳': '不稳', '无字': '无字'}
    L = ['# 原声清单（自动生成，只是建议）', '',
         '每段按静音切成小岛逐个转写。只在静音处下刀；“建议删”“选一遍”“待听”都要人确认后才写进选片表。',
         '“覆盖”= 整段转写的字有多少落进了小岛；低于85%说明有话被当成静音，这段的切点不可靠，只能人工听着剪。', '']
    for r in results:
        warn = '' if r['coverage'] >= 0.85 else ' ⚠ 切点不可靠，人工听'
        L += [f"## {r['file']}（{r['duration']:.1f}秒，覆盖{r['coverage']:.0%}{warn}）", '',
              f"整段转写：{r['whole_text']}", '', '| 时间 | 状态 | 文字 | 原因 |', '|---|---|---|---|']
        L += [f'| {a_:.2f}–{b_:.2f} | {mark[st]} | {t} | {why} |' for a_, b_, st, t, why in r['rows']]
        L.append('')
    (out / '原声清单.md').write_text('\n'.join(L), encoding='utf-8')
    print(f'清单：{out / "原声清单.md"}')


if __name__ == '__main__':
    main()
