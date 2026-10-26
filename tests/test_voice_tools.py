"""配音流程：逐句合成量时长、略超加速、超太多标需改短；渲染时整句放置、字幕用文案。"""
import csv
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import cutter as editor
import voice_tools as voice

FAKE = 'ffmpeg -v error -y -f lavfi -i sine=f=300:d=1 -ar 24000 -ac 1 {out} && test -s {text}'


def write_script(path, rows):
    with open(path, 'w', encoding='utf-8-sig', newline='') as h:
        w = csv.DictWriter(h, fieldnames=['句号', '成片起秒', '成片止秒', '文案', '对应画面'])
        w.writeheader()
        w.writerows(rows)


def tool(*args):
    return subprocess.run([sys.executable, str(SCRIPTS / 'voice_tools.py'), *args], capture_output=True, text=True)


class VoiceToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_limits_fill_columns_and_flag_overlong_lines(self):
        script = self.base / 's.csv'
        write_script(script, [{'句号': '1', '成片起秒': '0', '成片止秒': '2', '文案': '鸡块先抓匀。'},
                              {'句号': '2', '成片起秒': '2', '成片止秒': '3', '文案': '这一句明显写得太长了放不下'}])
        res = tool('limits', str(script), '--rate', '5')
        self.assertIn('第2句', res.stdout)
        rows = list(csv.DictReader(open(script, encoding='utf-8-sig')))
        self.assertEqual(rows[0]['字数上限'], '8')

    def test_rate_saved_per_voice_and_read_by_limits(self):
        env = dict(os.environ, HOME=str(self.base))   # 不碰本机真实 voices.json
        run = lambda *args: subprocess.run([sys.executable, str(SCRIPTS / 'voice_tools.py'), *args],
                                           capture_output=True, text=True, env=env)
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'sine=d=2', str(self.base / 't.wav')],
                       check=True)
        script = self.base / 's.csv'
        write_script(script, [{'句号': '1', '成片起秒': '0', '成片止秒': '2', '文案': '鸡块先抓匀。'}])
        res = run('limits', str(script), '--voice', '猴哥')
        self.assertNotEqual(res.returncode, 0)
        self.assertIn('--save', res.stderr)
        self.assertEqual(run('rate', str(self.base / 't.wav'), '十个字的测速文案句子', '--voice', '猴哥',
                             '--save').returncode, 0)
        run('rate', str(self.base / 't.wav'), '八个字测速文案', '--voice', '猴哥', '--speech-rate', '20', '--save')
        voices = json.loads((self.base / '.config/process-cutter/voices.json').read_text(encoding='utf-8'))
        self.assertAlmostEqual(voices['猴哥'], 5.0, delta=0.1)
        self.assertAlmostEqual(voices['猴哥@speech_rate=20'], 3.5, delta=0.1)
        res = run('limits', str(script), '--voice', '猴哥')
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(list(csv.DictReader(open(script, encoding='utf-8-sig')))[0]['字数上限'], '8')
        res = run('limits', str(script), '--voice', '猴哥', '--speech-rate', '20')
        self.assertEqual(list(csv.DictReader(open(script, encoding='utf-8-sig')))[0]['字数上限'], '5')
        self.assertNotEqual(run('limits', str(script)).returncode, 0)

    def test_repeated_info_hint_on_v6_script(self):
        # 葱姜鸡 v6 真实脚本：第7句和第19句都讲了水淀粉（v6 实际出现过的重复）
        lines = ['姜和葱分开煸，这盘姜葱鸡才香。', '（留白）', '鸡块加生抽和生粉抓匀，生抽少放些，免得太咸。',
                 '（留白）', '再淋一点油抓匀，主要是为了炒的时候不那么干。', '大葱切成大段就行，不用切太细。',
                 '碗汁用味精、生抽、蚝油，最后加水淀粉搅匀。', '（留白）', '水淀粉倒进去以后，用勺子把碗汁搅匀。',
                 '（留白）', '鸡块先单独煎，表面变色后再翻动。', '（留白）', '大葱单独下锅煸，不能煸得太久。',
                 '煸到表面有点焦黄，就先盛出来。', '（留白）', '姜要单独煸，比葱煸得久些。',
                 '姜煸好了，葱和鸡块再回锅一起炒。', '（留白）', '碗汁分次倒进锅里，和鸡块、葱段翻拌匀。', '（留白）',
                 '鸡块和葱段都裹上酱汁，出锅前已经有人喊好香。', '（留白）']
        numbers = [1, 2, 3, 4, 5, 6, 7, 8, 19, 20, 9, 10, 11, 12, 21, 13, 14, 22, 15, 16, 17, 18]
        rows = [{'句号': str(n), '文案': t} for n, t in zip(numbers, lines)]
        notes = voice.repeated_info(rows)
        self.assertTrue(any('第7句和第19句' in n and '水淀粉' in n for n in notes))
        joined = '；'.join(notes)
        for word in ('“葱”', '“姜”', '鸡块', '和鸡', '煸得'):
            self.assertNotIn(word, joined)
        script = self.base / 's.csv'
        write_script(script, [{'句号': '1', '成片起秒': '0', '成片止秒': '4', '文案': '碗汁调好，最后加水淀粉。'},
                              {'句号': '2', '成片起秒': '4', '成片止秒': '8', '文案': '水淀粉倒进去搅匀。'}])
        res = tool('limits', str(script), '--rate', '5')
        self.assertEqual(res.returncode, 0)
        self.assertIn('第1句和第2句都提到“水淀粉”', res.stdout)

    def test_repeated_info_quiet_without_repeats(self):
        rows = [{'句号': '1', '文案': '鸡块加生抽抓匀。'}, {'句号': '2', '文案': '大葱切成大段。'},
                {'句号': '3', '文案': '平底锅倒油烧热。'}, {'句号': '4', '文案': '姜片煸到发干。'}]
        self.assertEqual(voice.repeated_info(rows), [])

    def test_synth_fits_slightly_long_and_flags_too_long_without_cutting(self):
        script = self.base / 's.csv'
        write_script(script, [{'句号': '1', '成片起秒': '0', '成片止秒': '1.2', '文案': '放得下'},
                              {'句号': '2', '成片起秒': '1.5', '成片止秒': '2.4', '文案': '略超一点'},
                              {'句号': '3', '成片起秒': '3', '成片止秒': '3.5', '文案': '超太多'},
                              {'句号': '4', '成片起秒': '4', '成片止秒': '9', '文案': '（留白）'}])
        res = tool('synth', str(script), str(self.base / 'vo'), '--cmd', FAKE)
        self.assertEqual(res.returncode, 2)
        rows = {r['句号']: r for r in csv.DictReader(open(self.base / 'vo/配音表.csv', encoding='utf-8-sig'))}
        self.assertEqual(rows['1']['处理'], '原速通过')
        self.assertTrue(rows['2']['处理'].startswith('加速×1.11'))
        self.assertLessEqual(float(rows['2']['实际秒']), 0.92)
        self.assertTrue(rows['3']['处理'].startswith('需改短'))
        self.assertNotIn('4', rows)  # 留白行不合成
        # 已合成且文案没变：再跑不再调用命令
        res = tool('synth', str(script), str(self.base / 'vo'), '--cmd', 'false {text} {out}')
        self.assertEqual(res.returncode, 2)
        self.assertIn('调用配音命令0次', (self.base / 'vo/时长核对.md').read_text(encoding='utf-8'))


    def test_synth_words_placeholder_scales_with_tempo(self):
        script = self.base / 's.csv'
        write_script(script, [{'句号': '1', '成片起秒': '0', '成片止秒': '1.2', '文案': '放得下'},
                              {'句号': '2', '成片起秒': '1.5', '成片止秒': '2.4', '文案': '略超一点'}])
        dump = ('import json,sys; json.dump([{"text":"略","start":0.0,"end":0.5},{"text":"超","start":0.5,"end":1.0}],'
                'open(sys.argv[1],"w"))')
        cmd = FAKE + f' && {shlex.quote(sys.executable)} -c {shlex.quote(dump)} {{words}}'
        res = tool('synth', str(script), str(self.base / 'vo'), '--cmd', cmd)
        self.assertEqual(res.returncode, 0, res.stderr)
        rows = {r['句号']: r for r in csv.DictReader(open(self.base / 'vo/配音表.csv', encoding='utf-8-sig'))}
        self.assertEqual(rows['1']['逐字时间'], '句01.words.json')
        self.assertEqual(rows['2']['逐字时间'], '句02_加速.words.json')
        fast = json.loads((self.base / 'vo/句02_加速.words.json').read_text(encoding='utf-8'))
        self.assertAlmostEqual(fast[1]['end'], 1.0 / 1.1111, places=3)
        # 旧流程：命令里没有 {words} 时这一列留空
        res = tool('synth', str(script), str(self.base / 'vo2'), '--cmd', FAKE)
        rows = list(csv.DictReader(open(self.base / 'vo2/配音表.csv', encoding='utf-8-sig')))
        self.assertEqual({r['逐字时间'] for r in rows}, {''})


class VoiceRenderTests(unittest.TestCase):
    def test_voice_captions_use_word_times(self):
        sample = json.loads((Path(__file__).parent / 'fixtures/volc_subtitle_364.json').read_text(encoding='utf-8'))
        words = [{'text': w['word'], 'start': w['startTime'], 'end': w['endTime']} for w in sample['payload']['words']]
        caps = editor.voice_captions([{'start': 10.0, 'duration': 4.248, 'text': '鸡块先加生抽和生粉，用手抓匀。',
                                       'words': words}])
        self.assertEqual([c['text'] for c in caps], ['鸡块先加生抽和生粉', '用手抓匀'])
        self.assertAlmostEqual(caps[0]['start'], 10.0)
        self.assertAlmostEqual(caps[1]['start'], 12.825)   # “用”开口的时刻，不是按字数估的 12.83
        self.assertAlmostEqual(caps[0]['end'], 12.825)
        self.assertAlmostEqual(caps[1]['end'], 14.248)
        # 字数对不上（如服务把数字读法改了）就退回按字数估算
        caps = editor.voice_captions([{'start': 0, 'duration': 2, 'text': '鸡块先加生抽和生粉，用手抓匀。',
                                       'words': words[:5]}])
        self.assertAlmostEqual(caps[-1]['end'], 2.0)
        self.assertAlmostEqual(caps[1]['start'], 2 * 9 / 13, places=3)


    def test_voice_captions_split_by_punctuation_within_line_duration(self):
        caps = editor.voice_captions([{'start': 1.0, 'duration': 2.0, 'text': '鸡块先加生抽和生粉，用手抓匀。'}])
        self.assertEqual([c['text'] for c in caps], ['鸡块先加生抽和生粉', '用手抓匀'])
        self.assertAlmostEqual(caps[0]['start'], 1.0)
        self.assertAlmostEqual(caps[-1]['end'], 3.0)

    def test_overlapping_voice_lines_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'sine=d=1', str(base / 'a.wav')], check=True)
            with open(base / '配音表.csv', 'w', encoding='utf-8-sig', newline='') as h:
                w = csv.DictWriter(h, fieldnames=['句号', '成片起秒', '文件', '文案'])
                w.writeheader()
                w.writerows([{'句号': '1', '成片起秒': '0', '文件': 'a.wav', '文案': '一'},
                             {'句号': '2', '成片起秒': '0.5', '文件': 'a.wav', '文案': '二'}])
            with self.assertRaisesRegex(ValueError, '重叠'):
                editor.load_voice_table(base / '配音表.csv')


if __name__ == '__main__':
    unittest.main()
