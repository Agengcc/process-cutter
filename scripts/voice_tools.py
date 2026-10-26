#!/usr/bin/env python3
"""配音工具：量语速、算每句字数、逐句调用用户自己的配音命令并量时长。

不内置任何密钥。用户用 --cmd 给出自己的命令模板，{text} 换成文案文件路径、{out} 换成输出音频路径，例：
  --cmd "node /path/千问配音.cjs synth {text} {out}"
  --cmd "python3 tts_volcengine.py --text-file {text} --out {out} --voice 音色ID"   （自带的火山接入）

用法：
  python3 voice_tools.py rate 测速.wav "测速用的那句文案" [--voice 音色 --save]   # 打印实测 字/秒；--save 记到本机
  python3 voice_tools.py limits 配音脚本.csv --rate 5.0             # 填“可用秒数、字数上限、建议字数”，列出超字和可能重复的句子
  python3 voice_tools.py limits 配音脚本.csv --voice 音色           # 同上，字/秒从本机记录读（换了 --speech-rate 要一起给）
  python3 voice_tools.py synth 配音脚本.csv 输出目录 --cmd "..."    # 逐句合成→量时长→略超就不变调加速→写 配音表.csv 和 时长核对.md
  命令里可选 {words}：换成 句NN.words.json，配音服务把逐字时间写到这里（自带火山接入用 --words-out {words}），字幕据此对齐。

配音脚本.csv 列：句号,成片起秒,成片止秒,文案,对应画面（limits 会补上 可用秒数,字数上限,建议字数）。
规则（来自2026-10-24葱姜鸡两轮测试）：一句一个完整短句、各自合成；合成好的句子一律不切开；
超出可用时长15%以内用 atempo 加快（最多1.15倍），超出更多就标“需改短”，由Agent改文案重合成，不剪音频、不擅自改画面。
"""
import argparse
import csv
import json
import math
import re
import shlex
import subprocess
from pathlib import Path

VOICES = Path.home() / '.config' / 'process-cutter' / 'voices.json'   # 本机记录的各音色实测字/秒，不进仓库
MAX_TEMPO = 1.15
FILL = 0.85                 # 字数上限：可用秒数 × 字/秒 × 0.85
LONG_WINDOW = 6.0           # 超过约6秒的窗口，一句话撑不满也不该写太长，拆两句
SUGGEST = (0.80, 1.00)      # 建议字数：上限的80–100%＝配音约占窗口70–85%（暂定，来自葱姜鸡v6）；窗口太长就拆两句


# 重复信息检查：相邻和隔一句的两句共享2–4字片段就提示。只提示，不报错（同一食材在不同步骤出现是正常的）。
STOP = {'然后', '一下', '就行', '我们', '这个', '那个', '可以', '时候', '一起', '一点', '单独', '以后', '之后',
        '不要', '不用', '就是', '还是', '已经', '这样', '起来', '下去', '进去', '出来', '再把', '先把', '的时'}


def _grams(text):
    out = set()
    for seg in re.findall(r'[0-9A-Za-z一-鿿]+', text):
        out |= {seg[i:i + n] for n in (2, 3, 4) for i in range(len(seg) - n + 1)}
    return {g for g in out if g not in STOP and not re.search('[的得了着]', g)}


def repeated_info(rows):
    """返回提示列表。在很多句里都出现的词（如整道菜的主料“鸡块”）当作主题词，含主题词的片段不提示。"""
    texts = [(r['句号'], r['文案']) for r in rows if not is_blank(r['文案'])]
    first = [_grams(t) for _, t in texts]
    common = max(3, math.ceil(len(texts) * 0.3))
    topic = {g for g in set().union(*first) if sum(g in gs for gs in first) >= common} if texts else set()
    # 先把主题词从原文挖掉再切片段，避免“和鸡块”剩下“和鸡”这类残片
    pattern = re.compile('|'.join(sorted(map(re.escape, topic), key=len, reverse=True))) if topic else None
    lines = [(k, _grams(pattern.sub('，', t) if pattern else t)) for k, t in texts]
    notes = []
    for i, (a, ga) in enumerate(lines):
        for gap, (b, gb) in enumerate(lines[i + 1:i + 3], 1):
            shared = ga & gb
            shared = sorted((g for g in shared if not any(g != h and g in h for h in shared)), key=len, reverse=True)
            # 只共享一个2字片段（如“抓匀”）时只看紧挨着的两句，隔一句再提示太吵
            if shared and (gap == 1 or len(shared) > 1 or len(shared[0]) > 2):
                words = '”“'.join(shared)
                notes.append(f'第{a}句和第{b}句都提到“{words}”，确认不是重复信息')
    return notes


def is_blank(text):
    return not count_chars(text) or text.strip().strip('（）()') == '留白'


def count_chars(text):
    return len(re.findall(r'[0-9A-Za-z一-鿿]', text))


def duration(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(path)],
                         capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def read_rows(path):
    with open(path, encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        rows = [r for r in reader if any((v or '').strip() for v in r.values())]
        fields = list(reader.fieldnames or [])
    missing = {'句号', '成片起秒', '成片止秒', '文案'} - set(fields)
    if missing:
        raise SystemExit(f'配音脚本缺少列：{"、".join(sorted(missing))}')
    for r in rows:
        a, b = float(r['成片起秒']), float(r['成片止秒'])
        if not (math.isfinite(a) and math.isfinite(b)) or a < 0 or b <= a:
            raise SystemExit(f'第{r["句号"]}句时间无效')
    rows.sort(key=lambda r: float(r['成片起秒']))
    for prev, cur in zip(rows, rows[1:]):
        if float(cur['成片起秒']) < float(prev['成片止秒']) - 1e-6:
            raise SystemExit(f'第{prev["句号"]}句和第{cur["句号"]}句的窗口重叠')
    return rows, fields


def write_rows(path, rows, fields):
    with open(path, 'w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def voice_key(voice, speech_rate=None):
    """同一音色换了 --speech-rate 就是另一条记录。"""
    return voice if not speech_rate else f'{voice}@speech_rate={speech_rate}'


def load_voices():
    try:
        return json.loads(VOICES.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def cmd_rate(a):
    sec, n = duration(a.wav), count_chars(a.text)
    rate = n / sec
    print(f'{n}字 / {sec:.3f}秒 = {rate:.3f} 字/秒')
    if a.save:
        if not a.voice:
            raise SystemExit('--save 要同时给 --voice 音色名')
        voices = load_voices()
        voices[voice_key(a.voice, a.speech_rate)] = round(rate, 3)
        VOICES.parent.mkdir(parents=True, exist_ok=True)
        VOICES.write_text(json.dumps(voices, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
        print(f'已记到 {VOICES}：{voice_key(a.voice, a.speech_rate)} = {rate:.3f}')


def resolve_rate(a):
    if a.rate:
        return a.rate
    if a.voice:
        key = voice_key(a.voice, a.speech_rate)
        rate = load_voices().get(key)
        if rate:
            return float(rate)
        raise SystemExit(f'还没有 {key} 的实测语速：先合成一句约20字的测速句，'
                         f'运行 voice_tools.py rate 测速.wav "文案" --voice {a.voice}'
                         + (f' --speech-rate {a.speech_rate}' if a.speech_rate else '') + ' --save')
    raise SystemExit('要给 --rate 实测字/秒，或 --voice 音色名（从本机记录读取）')


def cmd_limits(a):
    a.rate = resolve_rate(a)
    rows, fields = read_rows(a.script)
    for col in ('可用秒数', '字数上限', '建议字数'):
        if col not in fields:
            fields.append(col)
    over = []
    for r in rows:
        sec = float(r['成片止秒']) - float(r['成片起秒'])
        top = math.floor(sec * a.rate * FILL)
        r['可用秒数'] = f'{sec:.2f}'
        r['字数上限'] = str(top)
        low = math.ceil(top * SUGGEST[0])
        r['建议字数'] = f'{low}–{math.floor(top * SUGGEST[1])}'
        n = count_chars(r['文案'])
        if is_blank(r['文案']):
            continue
        if n > top:
            over.append(f'第{r["句号"]}句 {n}字 > 上限{top}')
        elif n < low:
            over.append(f'第{r["句号"]}句 {n}字偏少（建议{r["建议字数"]}）：补信息，或把窗口改短、剩下的标（留白）')
        if sec > LONG_WINDOW and n >= low:   # 字偏少时上一条已提示改短窗口，不重复
            over.append(f'第{r["句号"]}句窗口{sec:.1f}秒偏长：有两条信息就在动作切换处拆两句，否则改短窗口、剩下留白')
    write_rows(a.script, rows, fields)
    print(f'已按 {a.rate} 字/秒填好字数上限。' + ('需要看的：' + '；'.join(over) if over else '字数都在建议范围内。'))
    repeats = repeated_info(rows)
    if repeats:
        print('可能重复（只是提醒）：' + '；'.join(repeats))


def _atempo(src, dst, tempo):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(src), '-af', f'atempo={tempo:.4f}', str(dst)], check=True)


def cmd_synth(a):
    rows, _fields = read_rows(a.script)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    only = {s.strip() for s in a.only.split(',')} if a.only else None
    table, report, calls = [], [], 0
    film_end = max(float(r['成片止秒']) for r in rows)    # 脚本写到片尾（含留白行）时即成片长度
    rows = [r for r in rows if not is_blank(r['文案'])]   # （留白）行不合成
    for r in rows:
        key = r['句号'].strip()
        name = f'句{int(key):02d}' if key.isdigit() else f'句{key}'
        text_file, wav, words = out / f'{name}.txt', out / f'{name}.wav', out / f'{name}.words.json'
        window = float(r['成片止秒']) - float(r['成片起秒'])
        changed = not text_file.exists() or text_file.read_text(encoding='utf-8') != r['文案']
        # 已合成且文案没变的句子不再调用接口（省钱）；--only 限定只动哪几句
        if (only is None or key in only) and (a.force or changed or not wav.exists()):
            text_file.write_text(r['文案'], encoding='utf-8')
            words.unlink(missing_ok=True)   # 旧句的逐字时间不能留给新音频
            cmd = a.cmd.replace('{text}', shlex.quote(str(text_file))).replace('{out}', shlex.quote(str(wav))) \
                .replace('{words}', shlex.quote(str(words)))
            res = subprocess.run(cmd, shell=True)
            calls += 1
            if res.returncode != 0 or not wav.exists():
                raise SystemExit(f'第{key}句配音命令失败（退出码{res.returncode}），已停止；不要自动重试，先看命令输出')
        if not wav.exists():
            raise SystemExit(f'第{key}句还没有音频：{wav}')
        sec = duration(wav)
        use, how = wav, '原速通过'
        if sec > window:
            tempo = sec / window
            if tempo <= MAX_TEMPO:
                use = out / f'{name}_加速.wav'
                _atempo(wav, use, tempo)
                how = f'加速×{tempo:.2f}（不变调）'
                if words.exists():   # 加速后逐字时间按 1/倍速 缩放
                    fast = [dict(w, start=w['start'] / tempo, end=w['end'] / tempo)
                            for w in json.loads(words.read_text(encoding='utf-8'))]
                    words = out / f'{name}_加速.words.json'
                    words.write_text(json.dumps(fast, ensure_ascii=False) + '\n', encoding='utf-8')
            else:
                how = f'需改短：{sec:.2f}秒放不进{window:.2f}秒（要×{tempo:.2f}）'
        final = duration(use)
        table.append({'句号': key, '成片起秒': r['成片起秒'], '文件': use.name, '文案': r['文案'],
                      '实际秒': f'{final:.3f}', '可用秒数': f'{window:.2f}', '处理': how,
                      '逐字时间': words.name if words.exists() else ''})
        report.append((key, count_chars(r['文案']), r.get('字数上限', ''), sec, window, final / window, how))
    gaps = []
    for prev, cur in zip(table, table[1:]):
        gap = float(cur['成片起秒']) - (float(prev['成片起秒']) + float(prev['实际秒']))
        if gap > a.max_gap:
            gaps.append(f'第{prev["句号"]}→{cur["句号"]}句之间空{gap:.1f}秒')
    write_rows(out / '配音表.csv', table, ['句号', '成片起秒', '文件', '文案', '实际秒', '可用秒数', '处理', '逐字时间'])
    bad = [t for t in table if t['处理'].startswith('需改短')]
    voiced = sum(float(t['实际秒']) for t in table)
    share = voiced / film_end if film_end else 0
    # 40%／65% 是葱姜鸡 v6 的暂定参考值，只是提醒；讲解少、动作好看的片子低于40%也可以，以用户听感为准
    share_note = (f'配音占全片{share:.0%}' + (
        '，低于葱姜鸡参考值（40%）：若听起来空，补带标准、窍门或顺序的句子，或用配乐／现场声垫住留白；讲解少的片子可以更低，交付时写明。'
        if share < 0.40 else '，高于葱姜鸡参考值（65%）：确认画面有喘息的地方。' if share > 0.65 else '。'))
    L = ['# 配音时长核对', '',
         f'本次调用配音命令{calls}次；{len(table)}句，配音共{voiced:.1f}秒；{share_note}'
         + (f'**{len(bad)}句需改短**，改文案后用 `--only 句号` 只重做这几句。' if bad else '全部放得下。'), '',
         '| 句号 | 字数 | 上限 | 合成秒 | 可用秒 | 占窗口 | 处理 |', '|---|---|---|---|---|---|---|']
    L += [f'| {k} | {n} | {top} | {s:.2f} | {w:.2f} | {p:.0%} | {h} |' for k, n, top, s, w, p, h in report]
    L += ['', '空档超过' + f'{a.max_gap:g}秒的地方：' + ('；'.join(gaps) if gaps else '没有。'),
          '连续讲解里超过1秒会显得断；留白处没有配乐或现场声时超过约3秒、有垫底时超过约5秒，听起来像卡住（暂定，来自葱姜鸡v6）。',
          '', '占窗口低于约60%的句子说明写短了，可以补一点画面里没有的信息（理由、窍门、感受），不要拆句子或切音频来凑。']
    (out / '时长核对.md').write_text('\n'.join(L) + '\n', encoding='utf-8')
    print('\n'.join(L[:3]))
    if bad:
        raise SystemExit(2)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='action', required=True)
    p = sub.add_parser('rate')
    p.add_argument('wav')
    p.add_argument('text')
    p.add_argument('--voice', help='音色名；和 --save 一起用，把实测值记到本机 voices.json')
    p.add_argument('--speech-rate', type=int, help='合成时用的 --speech-rate（不是0时单独记一条）')
    p.add_argument('--save', action='store_true')
    p.set_defaults(func=cmd_rate)
    p = sub.add_parser('limits')
    p.add_argument('script')
    p.add_argument('--rate', type=float, help='实测 字/秒；不给就按 --voice 从本机记录读')
    p.add_argument('--voice', help='音色名，从 ~/.config/process-cutter/voices.json 读实测字/秒')
    p.add_argument('--speech-rate', type=int, help='合成时用的 --speech-rate')
    p.set_defaults(func=cmd_limits)
    p = sub.add_parser('synth')
    p.add_argument('script')
    p.add_argument('out')
    p.add_argument('--cmd', required=True, help='配音命令模板，含 {text} 和 {out}')
    p.add_argument('--only', help='只重做这些句号，逗号分隔')
    p.add_argument('--force', action='store_true', help='文案没变也重新合成')
    p.add_argument('--max-gap', type=float, default=1.0, help='句间空档提醒阈值（秒）')
    p.set_defaults(func=cmd_synth)
    a = ap.parse_args()
    if a.action == 'synth' and ('{text}' not in a.cmd or '{out}' not in a.cmd):
        raise SystemExit('--cmd 里要有 {text} 和 {out}')
    a.func(a)


if __name__ == '__main__':
    main()
