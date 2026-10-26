"""Benchmark scoring against the fake example answer."""
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import benchmark

ANSWER = json.loads((ROOT / 'benchmarks/example/answer.json').read_text(encoding='utf-8'))
FIELDS = next(csv.reader((ROOT / 'templates/selection_template.csv').open(encoding='utf-8')))
GOOD = [('C.MOV', 1, 4, 1, '展示', '开头钩子'), ('A.MOV', 2, 8, 1.5, '制作', '打底'),
        ('A.MOV', 10.2, 14, 1.5, '制作', '抹平'), ('B.MOV', 3, 6.3, 1, '制作', '点眼')]


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def score(self, rows, vision=True):
        path = self.dir / '选片表.csv'
        with path.open('w', encoding='utf-8', newline='') as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            for i, (fn, a, b, speed, kind, usage) in enumerate(rows, 1):
                w.writerow({'镜头号': i, '源文件': fn, '源入点秒': a, '源出点秒': b, '速度倍率': speed,
                            '片段类型': kind, '用途': usage, '有效动作': usage})
        return benchmark.score(ANSWER, benchmark.read_shots(path), vision)

    def test_all_correct_is_full_marks(self):
        result = self.score(GOOD)
        self.assertEqual(result['total'], 100)
        self.assertEqual(result['hard_errors'], [])

    def test_missing_step_loses_coverage(self):
        result = self.score([r for r in GOOD if r[5] != '点眼'])
        self.assertLess(result['items']['coverage'][0], 50)
        missing = [r['name'] for r in result['steps'] if r['required'] and not r['ok']]
        self.assertEqual(missing, ['点眼睛'])

    def test_wrong_opening(self):
        result = self.score(GOOD[1:] + GOOD[:1])
        self.assertEqual(result['items']['opening'][0], 0)
        self.assertEqual(result['total'], 85)

    def test_overlap_is_hard_error(self):
        result = self.score(GOOD + [('A.MOV', 6, 9, 1, '制作', '再挤一次')])
        self.assertTrue(any('重叠' in e for e in result['hard_errors']))
        self.assertLessEqual(result['total'], 75)

    def test_replay_and_cut_deviation(self):
        rows = GOOD + [('A.MOV', 6, 9, 1, '制作', '回放')]
        self.assertEqual(self.score(rows)['hard_errors'], [])
        off = [GOOD[0], ('A.MOV', 1, 8, 1.5, '制作', '打底')] + GOOD[2:]
        result = self.score(off)
        self.assertLess(result['items']['cuts'][0], 15)
        self.assertEqual(result['items']['coverage'][0], 50)

    def test_vision_and_unknown_media(self):
        result = self.score(GOOD + [('D.MOV', 0, 2, 1, '制作', '其他')], vision=False)
        self.assertTrue(any('看图校验' in e for e in result['hard_errors']))
        self.assertTrue(any('D.MOV' in w for w in result['warnings']))

    def test_report_and_project_input(self):
        project = {'media': [{'id': 'm1', 'name': 'C.MOV', 'kind': 'video'}, {'id': 'm2', 'name': 'A.MOV', 'kind': 'video'},
                             {'id': 'm3', 'name': 'B.MOV', 'kind': 'video'}],
                   'tracks': [{'kind': 'video', 'clips': [
                       {'media': 'm1', 'start': 0, 'in': 1, 'duration': 3, 'speed': 1, 'name': '开头'},
                       {'media': 'm2', 'start': 3, 'in': 2, 'duration': 4, 'speed': 1.5},
                       {'media': 'm2', 'start': 7, 'in': 10.2, 'duration': 2.5, 'speed': 1.5},
                       {'media': 'm3', 'start': 9.5, 'in': 3, 'duration': 3.3, 'speed': 1}]}]}
        path = self.dir / 'project.json'
        path.write_text(json.dumps(project), encoding='utf-8')
        result = benchmark.score(ANSWER, benchmark.read_shots(path))
        self.assertEqual(result['total'], 100)
        text = benchmark.report_md(ANSWER, result, path)
        self.assertIn('100.0 / 100', text)
        self.assertNotIn(str(self.dir), text)


if __name__ == '__main__':
    unittest.main()
