#!/usr/bin/env python3
"""技术检查与成片剪接复核图生成。不会判定有效动作或自动填写视觉通过。"""
import argparse
import csv
import json
import re
import subprocess
import textwrap
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from PIL import Image, ImageDraw
from motion_cut_helper import analyze_cut_window, probe_video, run_checked
import captions as caption_tools


def check_black_frames(video_path):
    # -xerror 确保解码错误失败，避免工具失败被当成零黑帧。
    result = run_checked(['ffmpeg', '-hide_banner', '-xerror', '-i', str(video_path),
                          '-map', '0:v:0', '-vf', 'blackdetect=d=0.03:pix_th=0.10:pic_th=0.98',
                          '-an', '-f', 'null', '-'])
    return [{'start': float(a), 'end': float(b), 'duration': float(c)}
            for a, b, c in re.findall(r'black_start:([0-9.]+) black_end:([0-9.]+) black_duration:([0-9.]+)', result.stderr)]


def generate_qc_grid(video_path, output_image, num_frames=14):
    duration = probe_video(video_path)['duration']
    return analyze_cut_window(video_path, 0, duration, output_image, fps=min(10, num_frames/duration))


def generate_boundary_reviews(video, project, directory, duration):
    data = json.loads(Path(project).read_text(encoding='utf-8'))
    clips = [c for t in data['tracks'] if t['kind'] == 'video' for c in t['clips'] if c.get('enabled', True)]
    if not clips:
        raise ValueError('工程没有有效视频片段')
    # 用输出帧编号去重，避免浮点末位不同造成同一剪接点重复。
    fps = float(data['settings']['fps'])
    raw_times = [0.0, duration, *(float(c['start']) for c in clips),
                 *(float(c['start'])+float(c['duration']) for c in clips)]
    times = sorted({round(t*fps)/fps for t in raw_times})
    reviews = []
    for index, time in enumerate(times):
        if time < -1e-6 or time > duration+1/30+.001:
            raise ValueError('工程剪接时间超出成片范围，请确认工程与成片匹配')
        time = min(duration, max(0, time))
        start, end = max(0, time-.3), min(duration, time+.3)
        manifest = analyze_cut_window(video, start, end, Path(directory)/f'cut_{index+1:03d}_{time:.3f}s.jpg')
        reviews.append({'cut_time': time, 'pages': manifest['pages'], 'visual_review': '未检查'})
    return reviews


def _source_frame(path, t, height):
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-ss', f'{max(t, 0):.3f}', '-i', str(path), '-frames:v', '1',
                          '-vf', f'scale=-2:{height}', '-f', 'image2pipe', '-vcodec', 'png', '-'],
                         capture_output=True).stdout
    return Image.open(BytesIO(raw)).convert('RGB') if raw else Image.new('RGB', (height * 9 // 16, height), 'gray')


def selection_contact_sheet(project, csv_path, out_dir, rows_per_page=6):
    """选片对照图：每个镜头三帧源画面＋选片表里写的“有效动作”，让用户一眼看出写的和画面是否一致。"""
    data = json.loads(Path(project).read_text(encoding='utf-8'))
    media = {m['id']: m for m in data['media']}
    clips = [c for t in data['tracks'] if t['kind'] == 'video' for c in t['clips'] if c.get('enabled', True)]
    with open(csv_path, encoding='utf-8-sig', newline='') as handle:
        rows = [r for r in csv.DictReader(handle) if any((v or '').strip() for v in r.values())]
    if len(rows) != len(clips):
        raise ValueError('选片表行数与工程片段数不一致，先重新 build')
    font_path, wanted = caption_tools.find_font()
    font = caption_tools._open_font(font_path, wanted, 26)
    small = caption_tools._open_font(font_path, wanted, 22)
    H, W = 300, 1400
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pages = []
    for start in range(0, len(rows), rows_per_page):
        part = list(zip(rows, clips))[start:start + rows_per_page]
        sheet = Image.new('RGB', (W, H * len(part)), 'white')
        draw = ImageDraw.Draw(sheet)
        for k, (row, clip) in enumerate(part):
            m, y = media[clip['media']], k * H
            a = float(clip['in'])
            b = a + float(clip['duration']) * float(clip.get('speed') or 1)
            times = (a + 0.1, (a + b) / 2, max(a, b - 0.1)) if m['kind'] == 'video' else (0, 0, 0)
            x = 0
            for t in times:
                im = _source_frame(m['path'], t, H - 10)
                sheet.paste(im, (x, y + 5))
                x += im.width + 4
            tx = x + 16
            draw.text((tx, y + 12), f"镜头{start + k + 1}  {m['name']}  {a:.2f}–{b:.2f}秒  {row.get('用途', '')}",
                      fill='black', font=font)
            lines = textwrap.wrap('有效动作：' + (row.get('有效动作') or ''), 24)[:7]
            draw.multiline_text((tx, y + 56), '\n'.join(lines), fill=(20, 20, 160), font=small, spacing=8)
            draw.line((0, y + H - 1, W, y + H - 1), fill=(200, 200, 200), width=2)
        path = out_dir / f'选片对照图_{len(pages) + 1:02d}.jpg'
        sheet.save(path, quality=85)
        pages.append(str(path))
    return pages


def inspect(video, project=None, review_dir=None, grid=None, selection=None):
    info = probe_video(video)
    black = check_black_frames(video)
    result = {'video': str(Path(video).resolve()), 'video_mtime_ns': Path(video).stat().st_mtime_ns,
              'generated_at': datetime.now(timezone.utc).isoformat(), 'decode': '通过',
              'width': info['width'], 'height': info['height'], 'fps': info['fps'],
              'duration': info['duration'],
              'dimensions_ok': (info['width'], info['height']) == (1080, 1920),
              'fps_ok': abs(info['fps']-30) < .001,
              'blackdetect': {'min_duration': .03, 'pixel_threshold': .10, 'picture_ratio': .98,
                              'candidates': black, 'visual_review': '未检查'},
              'visual_review': '未检查', 'continuous_watch': '未检查', 'user_acceptance': '待用户试看'}
    if project:
        data = json.loads(Path(project).read_text(encoding='utf-8'))
        expected = max((float(c['start'])+float(c['duration']) for t in data['tracks']
                        for c in t['clips'] if c.get('enabled', True)), default=0)
        result['project'] = str(Path(project).resolve())
        result['duration_matches_project'] = abs(info['duration']-expected) <= 1/30+.001
    if review_dir:
        if not project:
            raise ValueError('--review-dir 需要 --project 才能定位真实剪接点')
        result['boundary_reviews'] = generate_boundary_reviews(video, project, review_dir, info['duration'])
    if grid:
        result['overview_pages'] = generate_qc_grid(video, grid)['pages']
    if selection:
        if not project:
            raise ValueError('--csv 需要 --project')
        result['selection_sheets'] = selection_contact_sheet(project, selection, review_dir or Path(video).parent)
        result['selection_sheets_note'] = '交给用户对照：画面与“有效动作”文字是否一致；不一致说明模型没看清或没看图'
    checks = result['dimensions_ok'] and result['fps_ok'] and result.get('duration_matches_project', True)
    result['technical_status'] = '通过' if checks and not black else '需处理或复核'
    result['note'] = '技术通过只代表本报告中的技术项通过；不代表无气口、动作完整或视觉通过。'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('video')
    parser.add_argument('--grid')
    parser.add_argument('--project')
    parser.add_argument('--review-dir')
    parser.add_argument('--report')
    parser.add_argument('--csv', help='选片表；生成选片对照图（每镜头三帧＋有效动作文字）')
    args = parser.parse_args()
    status = 0
    try:
        result = inspect(args.video, args.project, args.review_dir, args.grid, args.csv)
        if result['technical_status'] != '通过':
            status = 2
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        result = {'video': args.video, 'technical_status': '执行失败', 'error': str(exc),
                  'visual_review': '未检查', 'continuous_watch': '未检查'}
        status = 1
    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(status)


if __name__ == '__main__':
    main()
