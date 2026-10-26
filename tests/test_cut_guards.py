"""Regression checks for source-bound cuts, review gates and honest tool failures."""
import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import cutter as editor
import motion_trimmer as motion
import qc_tools as qc
import captions as caption_tools

FIELDS = next(csv.reader((SCRIPTS.parent/'templates/selection_template.csv').open()))


class CutGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        Image.new('RGB', (20, 20), 'red').save(self.base/'review.jpg')
        (self.base/'clip.mov').write_bytes(b'fixture')
        (self.base/'看图校验.json').write_text('{"passed": true}', encoding='utf-8')
        self.row = {'源文件': 'clip.mov', '主体ID': 'A-虎斑', '片段类型': '制作',
                    '源入点秒': '1.13', '源出点秒': '3.37', '速度倍率': '1.5',
                    '有效动作': '挤耳', '入点依据': '黑奶油开始沉积', '出点依据': '耳朵成形、工具离开',
                    '复核图': 'review.jpg', '复核状态': '已看片确认', 'scale': '1', 'y_offset': '0'}
        self.probe = {'path': str(self.base/'clip.mov'), 'kind': 'video', 'duration': 10,
                      'width': 1080, 'height': 1920, 'fps': 60, 'has_video': True, 'has_audio': False}

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, rows):
        path = self.base/'selection.csv'
        with path.open('w', newline='', encoding='utf-8-sig') as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        with patch.object(editor, 'probe_media', return_value=self.probe.copy()):
            return editor.build_project_from_csv(str(path), str(self.base))

    def test_unreviewed_or_unknown_subject_blocks_build(self):
        for change in ({'复核状态': '未检查'}, {'主体ID': '待确认'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.build([{**self.row, **change}])

    def test_missing_or_fake_review_image_blocks_build(self):
        for image in ('missing.jpg', 'clip.mov'):
            with self.subTest(image=image), self.assertRaises((OSError, ValueError)):
                self.build([{**self.row, '复核图': image}])

    def test_same_source_seconds_cannot_appear_twice(self):
        with self.assertRaisesRegex(ValueError, '重叠'):
            self.build([self.row, {**self.row, '源入点秒': '3.0', '源出点秒': '5.0'}])
        self.build([self.row, {**self.row, '源入点秒': '3.37', '源出点秒': '5.0'}])
        self.build([self.row, {**self.row, '用途': '回放'}])

    def test_build_requires_passed_vision_check(self):
        (self.base/'看图校验.json').unlink()
        with self.assertRaisesRegex(ValueError, '看图测验'):
            self.build([self.row])

    def test_phone_hdr_blocks_build(self):
        self.probe['hdr'] = 'HLG'
        with self.assertRaisesRegex(ValueError, 'hdr_to_sdr'):
            self.build([self.row])

    def test_invalid_source_ranges_and_numeric_values_block_build(self):
        changes = [{'源入点秒': '-1'}, {'源出点秒': '11'}, {'源出点秒': '1.13'},
                   {'速度倍率': '0'}, {'速度倍率': 'nan'}, {'scale': 'inf'}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.build([{**self.row, **change}])

    def test_no_clip_reads_beyond_selected_source_out_and_no_timeline_gaps(self):
        rows = [{**self.row, '源出点秒': str(2.05+i*.071), '速度倍率': str(speed), '用途': '回放'}
                for i, speed in enumerate((1, 1.2, 1.3, 1.5, 1.8))]
        project, total = self.build(rows)
        previous_end = 0
        for row, clip in zip(rows, project['tracks'][0]['clips']):
            self.assertLess(abs(clip['start']-previous_end), 1.0001e-6)
            self.assertLessEqual(clip['in']+clip['duration']*clip['speed'], float(row['源出点秒'])+1e-9)
            self.assertAlmostEqual(clip['duration']*30, round(clip['duration']*30))
            previous_end = clip['start']+clip['duration']
        self.assertLess(abs(total-previous_end), 1.0001e-6)

    def test_renderer_activation_never_rounds_past_output_frame(self):
        rows = [{**self.row, '源入点秒': '0', '源出点秒': '0.1', '速度倍率': '1'}]*3
        rows.insert(0, {**self.row, '源入点秒': '0', '源出点秒': str(275/30), '速度倍率': '1'})
        project, _ = self.build(rows)
        frame_index = 0
        for clip in project['tracks'][0]['clips']:
            self.assertLessEqual(float(f"{clip['start']:.6f}"), frame_index/30)
            frame_index += round(clip['duration']*30)

    def test_notes_cannot_force_zoom(self):
        project, _ = self.build([{**self.row, '构图说明': '旧笔记强制放大1.35x'}])
        self.assertEqual(project['tracks'][0]['clips'][0]['transform']['scale'], 1)

    def test_still_image_cannot_be_called_making(self):
        self.probe.update(kind='image', duration=0)
        with self.assertRaises(ValueError):
            self.build([{**self.row, '源入点秒': '0', '源出点秒': '0', '速度倍率': '1'}])

    def test_static_motion_and_high_motion_never_certify_success(self):
        for differences in ([0]*20, [10]*20):
            with self.subTest(differences=differences[0]):
                with patch.object(motion, 'extract_frame_metrics', return_value=([i/10 for i in range(20)], differences, differences, [0]*20)):
                    report = motion.motion_report('unused', 0, 2)
                self.assertIsNone(report['confirmed_air_gaps'])
                self.assertEqual(report['visual_review'], '未检查')
                self.assertEqual(report['status'], '仅运动线索')
        intervals = motion.low_motion_intervals([i/10 for i in range(20)], [0]*20, 10)
        self.assertEqual(intervals[0]['end'], 2)

    def test_bad_video_does_not_pass_any_tool(self):
        commands = [('qc_tools.py', ['--report', str(self.base/'failure.json')]),
                    ('motion_cut_helper.py', ['0', '1', str(self.base/'bad.jpg')]),
                    ('motion_trimmer.py', ['0', '1'])]
        for script, suffix in commands:
            prefix = ['scan-gaps'] if script == 'motion_trimmer.py' else []
            result = subprocess.run([sys.executable, str(SCRIPTS/script), *prefix, str(self.base/'clip.mov'), *suffix], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0, script)
        report = json.loads((self.base/'failure.json').read_text())
        self.assertEqual(report['technical_status'], '执行失败')

    def test_black_video_is_a_candidate_not_a_visual_pass(self):
        video = self.base/'black.mp4'
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi',
                        '-i', 'color=black:s=1080x1920:r=30:d=0.5', '-an', '-c:v', 'libx264',
                        '-preset', 'ultrafast', str(video)], check=True)
        report = qc.inspect(video)
        self.assertTrue(report['blackdetect']['candidates'])
        self.assertEqual(report['technical_status'], '需处理或复核')
        self.assertEqual(report['visual_review'], '未检查')


def _ffmpeg(*args):
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', *args], check=True)


def _stream_info(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-count_frames', '-show_entries',
                          'stream=codec_type,width,height,nb_read_frames:format=duration',
                          '-of', 'json', str(path)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


class FfmpegRenderTests(unittest.TestCase):
    """真实渲染：只依赖 FFmpeg，帧数、画幅、音轨与工程一致。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = cls.base = Path(cls.tmp.name)
        _ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=1080x1920:r=60:d=4', '-f', 'lavfi', '-i', 'sine=f=440:d=4',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', '-shortest', str(base/'a.mov'))
        # 横拍素材带90°旋转标记，模拟 iPhone 竖拍：转正后应为 1080x1920。
        _ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=1920x1080:r=30:d=3', '-c:v', 'libx264', '-preset', 'ultrafast',
                '-an', str(base/'raw_landscape.mp4'))
        _ffmpeg('-display_rotation:v', '90', '-i', str(base/'raw_landscape.mp4'), '-c', 'copy', str(base/'b.mov'))
        Image.new('RGB', (1200, 900), 'orange').save(base/'cover.jpg')
        _ffmpeg('-f', 'lavfi', '-i', 'sine=f=220:d=20', '-c:a', 'aac', str(base/'bgm.m4a'))
        Image.new('RGB', (20, 20), 'red').save(base/'review.jpg')
        (base/'看图校验.json').write_text('{"passed": true}', encoding='utf-8')
        rows = [
            {'源文件': 'cover.jpg', '主体ID': '全体', '片段类型': '封面', '源入点秒': '0', '源出点秒': '0',
             '速度倍率': '1', '成片时长秒': '0.2', 'scale': '1', 'y_offset': '0'},
            {'源文件': 'a.mov', '主体ID': 'A', '片段类型': '制作', '源入点秒': '0.5', '源出点秒': '2.5',
             '速度倍率': '1.5', 'scale': '1', 'y_offset': '0'},
            {'源文件': 'b.mov', '主体ID': 'A', '片段类型': '展示', '源入点秒': '0', '源出点秒': '2',
             '速度倍率': '1', 'scale': '1.1', 'y_offset': '-40'},
        ]
        for row in rows:
            row.update({'有效动作': '测试', '入点依据': '测试', '出点依据': '测试',
                        '复核图': 'review.jpg', '复核状态': '已看片确认'})
        with (base/'selection.csv').open('w', newline='', encoding='utf-8-sig') as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        cls.project, cls.total = editor.build_project_from_csv(str(base/'selection.csv'), str(base))
        (base/'project.json').write_text(json.dumps(cls.project, ensure_ascii=False), encoding='utf-8')
        cls.frames = round(cls.total * 30)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def render(self, name, **options):
        out = self.base/name
        editor.render_project(str(self.base/'project.json'), str(out), **options)
        return _stream_info(out)

    def test_probe_uses_upright_size_and_detects_images(self):
        rotated = editor.probe_media(self.base/'b.mov')
        self.assertEqual((rotated['width'], rotated['height']), (1080, 1920))
        self.assertEqual(editor.probe_media(self.base/'cover.jpg')['kind'], 'image')
        self.assertTrue(editor.probe_media(self.base/'a.mov')['has_audio'])
        self.assertIsNone(editor.probe_media(self.base/'a.mov')['hdr'])

    def test_selection_contact_sheet_pairs_frames_with_written_actions(self):
        pages = qc.selection_contact_sheet(self.base/'project.json', self.base/'selection.csv', self.base/'sheets')
        self.assertEqual(len(pages), 1)
        with Image.open(pages[0]) as sheet:
            self.assertEqual(sheet.size[1], 300 * 3)

    def test_default_render_is_silent_and_frame_exact(self):
        info = self.render('silent.mp4')
        kinds = [s['codec_type'] for s in info['streams']]
        self.assertEqual(kinds, ['video'])
        video = info['streams'][0]
        self.assertEqual((video['width'], video['height']), (1080, 1920))
        self.assertEqual(int(video['nb_read_frames']), self.frames)

    def test_music_and_source_audio_match_video_length(self):
        for name, options in (('music.mp4', {'music': str(self.base/'bgm.m4a')}),
                              ('source.mp4', {'source_audio': True}),
                              ('both.mp4', {'source_audio': True, 'music': str(self.base/'bgm.m4a')})):
            with self.subTest(name=name):
                info = self.render(name, **options)
                audio = [s for s in info['streams'] if s['codec_type'] == 'audio']
                self.assertEqual(len(audio), 1)
                video = next(s for s in info['streams'] if s['codec_type'] == 'video')
                self.assertEqual(int(video['nb_read_frames']), self.frames)
                self.assertLess(abs(float(info['format']['duration']) - self.total), 0.05)

    def test_voice_track_with_music_keeps_length_and_uses_script_captions(self):
        _ffmpeg('-f', 'lavfi', '-i', 'sine=f=500:d=0.8', str(self.base/'v1.wav'))
        with (self.base/'配音表.csv').open('w', newline='', encoding='utf-8-sig') as handle:
            writer = csv.DictWriter(handle, fieldnames=['句号', '成片起秒', '文件', '文案'])
            writer.writeheader()
            writer.writerows([{'句号': '1', '成片起秒': '0.3', '文件': 'v1.wav', '文案': '先抓匀，再下锅。'},
                              {'句号': '2', '成片起秒': '1.5', '文件': 'v1.wav', '文案': '翻一翻。'}])
        for name, options in (('voice.mp4', {}), ('voice_bgm.mp4', {'music': str(self.base/'bgm.m4a'), 'source_audio': True})):
            with self.subTest(name=name):
                info = self.render(name, voice=str(self.base/'配音表.csv'), **options)
                self.assertEqual(len([s for s in info['streams'] if s['codec_type'] == 'audio']), 1)
                video = next(s for s in info['streams'] if s['codec_type'] == 'video')
                self.assertEqual(int(video['nb_read_frames']), self.frames)
                self.assertLess(abs(float(info['format']['duration']) - self.total), 0.05)
                caps = json.loads((self.base/name).with_suffix('.字幕时间.json').read_text(encoding='utf-8'))
                self.assertEqual([c['text'] for c in caps], ['先抓匀', '再下锅', '翻一翻'])

    def test_captions_with_music_and_source_audio(self):
        table = self.base/'captions.csv'
        table.write_text('源文件,源起秒,源止秒,文字\na.mov,0.6,1.8,先把锅烧热\na.mov,3.0,3.9,被剪掉的话\n', encoding='utf-8')
        info = self.render('captioned.mp4', source_audio=True, music=str(self.base/'bgm.m4a'), captions=str(table))
        self.assertEqual([s['codec_type'] for s in info['streams']], ['video', 'audio'])
        placed = json.loads((self.base/'captioned.字幕时间.json').read_text(encoding='utf-8'))
        self.assertEqual([c['text'] for c in placed], ['先把锅烧热'])
        # a.mov 选段从源0.5秒起、1.5倍速，接在0.2秒封面之后：源0.6→成片0.2+0.1/1.5
        self.assertAlmostEqual(placed[0]['start'], 0.2 + 0.1/1.5, places=3)
        self.assertAlmostEqual(placed[0]['end'], 0.2 + 1.3/1.5, places=3)

    def test_missing_music_fails_loudly(self):
        with self.assertRaises(FileNotFoundError):
            editor.render_project(str(self.base/'project.json'), str(self.base/'x.mp4'), music=str(self.base/'none.mp3'))


class CaptionMappingTests(unittest.TestCase):
    media = {'m1': {'kind': 'video', 'path': '/x/a.MOV'}, 'm2': {'kind': 'video', 'path': '/x/b.MOV'}}

    def clips(self):
        return [{'media': 'm1', 'start': 0.0, 'in': 10.0, 'duration': 2.0, 'speed': 1.0},
                {'media': 'm2', 'start': 2.0, 'in': 0.0, 'duration': 2.0, 'speed': 2.0},
                {'media': 'm1', 'start': 4.0, 'in': 12.0, 'duration': 1.0, 'speed': 1.0}]

    def test_source_time_maps_through_cuts_and_speed(self):
        caps = [{'source': 'b.MOV', 'start': 1.0, 'end': 3.0, 'text': '加速段'},
                {'source': 'a.MOV', 'start': 20.0, 'end': 21.0, 'text': '没选进来'}]
        placed = caption_tools.map_to_timeline(caps, self.clips(), self.media, 30)
        self.assertEqual(placed, [{'start': 2.5, 'end': 3.5, 'text': '加速段',
                                   'sources': [{'path': '/x/b.MOV', 'start': 1.0, 'end': 3.0}]}])

    def test_sentence_spanning_adjacent_cuts_is_merged(self):
        caps = [{'source': 'a.MOV', 'start': 11.0, 'end': 12.6, 'text': '同一句'}]
        clips = [{'media': 'm1', 'start': 0.0, 'in': 10.0, 'duration': 2.0, 'speed': 1.0},
                 {'media': 'm1', 'start': 2.0, 'in': 12.0, 'duration': 1.0, 'speed': 1.0}]
        self.assertEqual(caption_tools.map_to_timeline(caps, clips, self.media, 30),
                         [{'start': 1.0, 'end': 2.6, 'text': '同一句',
                           'sources': [{'path': '/x/a.MOV', 'start': 11.0, 'end': 12.0},
                                       {'path': '/x/a.MOV', 'start': 12.0, 'end': 12.6}]}])

    def test_overlong_caption_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            caption_tools.render_caption_images([{'start': 0, 'end': 1, 'text': '这是一句非常非常长的字幕' * 4}],
                                                1080, 1920, tmp)


if __name__ == '__main__':
    unittest.main()
