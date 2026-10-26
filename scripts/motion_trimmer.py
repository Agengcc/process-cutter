#!/usr/bin/env python3
"""像素运动线索工具。无法识别接触、空挤、成功动作或半截动作，不自动剪片。"""
import argparse
import json
import subprocess
import math
import numpy as np
from motion_cut_helper import validate_window


def extract_frame_metrics(video_path, start_t, end_t, fps=10, scale_w=160, scale_h=284):
    validate_window(video_path, start_t, end_t, fps)
    if end_t-start_t > 120:
        raise ValueError('单次运动扫描最多120秒，请拆分扫描')
    result = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-ss', str(start_t),
                             '-i', str(video_path), '-t', str(end_t-start_t),
                             '-vf', f'fps={fps}:start_time=0,scale={scale_w}:{scale_h},format=gray',
                             '-f', 'rawvideo', '-pix_fmt', 'gray', 'pipe:1'], capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors='replace')[-2000:])
    frame_size = scale_w*scale_h
    if len(result.stdout) % frame_size or len(result.stdout) < 2*frame_size:
        raise ValueError('有效帧不足两帧，无法计算运动线索')
    frames = np.frombuffer(result.stdout, dtype=np.uint8).reshape(-1, scale_h, scale_w)
    ts, global_diff, center_diff, center_std = [], [], [], []
    y1, y2, x1, x2 = int(scale_h*.35), int(scale_h*.75), int(scale_w*.25), int(scale_w*.75)
    for i in range(len(frames)-1):
        a, b = frames[i].astype(np.float32), frames[i+1].astype(np.float32)
        ts.append(start_t+i/fps)
        global_diff.append(float(np.abs(b-a).mean()))
        center_diff.append(float(np.abs(b[y1:y2,x1:x2]-a[y1:y2,x1:x2]).mean()))
        center_std.append(float(b[y1:y2,x1:x2].std()))
    return ts, global_diff, center_diff, center_std


def low_motion_intervals(ts, diffs, fps, threshold=1.0, min_gap_sec=.3):
    intervals = []
    first = None
    for i in range(len(diffs)+1):
        low = i < len(diffs) and diffs[i] < threshold
        if low and first is None:
            first = i
        if not low and first is not None:
            if (i-first)/fps >= min_gap_sec-1e-9:
                intervals.append({'start': round(ts[first], 6), 'end': round(ts[first]+(i-first)/fps, 6),
                                  'duration': round((i-first)/fps, 6), 'kind': 'low_pixel_motion',
                                  'visual_review': '待看片，可能是等待，也可能是有效慢动作或展示'})
            first = None
    return intervals


def motion_report(video, start, end, fps=10, min_gap=.3):
    if not math.isfinite(min_gap) or min_gap <= 0:
        raise ValueError('min-gap 必须大于0')
    ts, g, c, _ = extract_frame_metrics(video, start, end, fps)
    return {'window': [start, end], 'sample_fps': fps, 'status': '仅运动线索',
            'visual_review': '未检查', 'confirmed_air_gaps': None,
            'low_motion_candidates': low_motion_intervals(ts, g, fps, min_gap_sec=min_gap),
            'global_mean_diff': round(float(np.mean(g)), 4),
            'center_mean_diff': round(float(np.mean(c)), 4),
            'tail_mean_diff': round(float(np.mean(g[-max(1, int(.2*fps)):])), 4),
            'warning': '无候选不等于无气口；高运动不等于有效动作。禁止直接采用为切点。'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('scan-gaps', 'verify-clip'):
        p = sub.add_parser(name, help='输出需看片确认的像素运动线索')
        p.add_argument('video')
        p.add_argument('start', type=float)
        p.add_argument('end', type=float)
        p.add_argument('--fps', type=float, default=10)
        p.add_argument('--min-gap', type=float, default=.3)
    p = sub.add_parser('trim', help='旧入口；仅给运动线索，不返回自动切点')
    p.add_argument('video')
    p.add_argument('--search-in', nargs=2, type=float)
    p.add_argument('--search-out', nargs=2, type=float)
    args = parser.parse_args()
    try:
        if args.command == 'trim':
            if not args.search_in and not args.search_out:
                raise ValueError('需要 --search-in 或 --search-out')
            result = {'touch_in': None, 'lift_out': None, 'status': '不自动判定接触切点', 'windows': []}
            for window in (args.search_in, args.search_out):
                if window:
                    result['windows'].append(motion_report(args.video, *window))
        else:
            result = motion_report(args.video, args.start, args.end, args.fps, args.min_gap)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        parser.exit(1, f'运动扫描失败，未得出合格结论: {exc}\n')


if __name__ == '__main__':
    main()
