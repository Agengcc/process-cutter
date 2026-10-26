#!/usr/bin/env python3
"""按拍摄时间辅助排序，生成分页1fps总览；不用于确认动作切点。"""
import argparse
import json
from pathlib import Path
from motion_cut_helper import analyze_cut_window, probe_video, run_checked


def get_creation_time(video_path):
    result = run_checked(['ffprobe', '-v', 'error', '-show_entries', 'format_tags=creation_time',
                          '-of', 'json', str(video_path)])
    return json.loads(result.stdout).get('format', {}).get('tags', {}).get('creation_time', '')


def generate_sheets_for_dir(media_dir, output_dir=None):
    directory = Path(media_dir)
    output = Path(output_dir) if output_dir else directory/'review_sheets'
    videos = [p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in ('.mov', '.mp4', '.m4v')]
    if not videos:
        raise ValueError('没有找到视频素材')
    records = [(p, get_creation_time(p)) for p in videos]
    records.sort(key=lambda row: (row[1] or row[0].name, row[0].name))
    report = []
    for video, created in records:
        info = probe_video(video)
        result = analyze_cut_window(video, 0, info['duration'], output/f'{video.stem}_sheet.jpg', fps=1)
        record = {'source': str(video.resolve()), 'creation_time': created or '缺失，按文件名辅助排序',
                  'pages': result['pages'], 'visual_review': '未检查'}
        report.append(record)
        print(json.dumps(record, ensure_ascii=False))
    (output/'素材总览.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('media_dir')
    parser.add_argument('output_dir', nargs='?')
    args = parser.parse_args()
    try:
        generate_sheets_for_dir(args.media_dir, args.output_dir)
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        parser.exit(1, f'总览生成失败: {exc}\n')


if __name__ == '__main__':
    main()
