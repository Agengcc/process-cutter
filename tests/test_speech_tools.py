"""原声清单与成片核对的纯逻辑检查（不调用 Whisper）。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import asr_tools as asr
import final_check
import speech_takes as st


class SpeechTakeTests(unittest.TestCase):
    def test_half_take_is_suggested_cut_and_last_take_stays(self):
        items = [(0.0, 2.4, '今天我们要吃豪华版'), (6.0, 8.1, '今天我们要吃豪华版牛肉鸡蛋拉面。')]
        self.assertEqual(st.false_starts(items)[0][0], 'half')
        self.assertNotIn(1, st.false_starts(items))

    def test_two_complete_takes_go_to_the_user(self):
        items = [(5.1, 7.0, '豪华版不倒翁拉面。'), (8.4, 10.9, '豪华版不倒翁拉面。')]
        self.assertEqual(st.false_starts(items)[0][0], 'whole')

    def test_different_sentences_are_not_retakes(self):
        items = [(0.0, 2.0, '把泡面放最底层。'), (3.0, 5.0, '这样煮出来的娃娃菜就是烂烂的。')]
        self.assertEqual(st.false_starts(items), {})

    def test_invented_phrases_are_removed(self):
        self.assertEqual(asr.INVENTED.sub('', '请不吝点赞 订阅 转发 打赏支持明镜与点点栏目').strip(), '')

    def test_missing_characters_are_named(self):
        self.assertTrue(final_check.missing_chars('豪华版不倒翁拉面', '豪华版不倒翁').endswith('拉面'))
        self.assertLess(asr.recall('豪华版不倒翁拉面', '豪华版不倒翁'), 0.85)


if __name__ == '__main__':
    unittest.main()
