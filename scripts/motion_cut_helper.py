#!/usr/bin/env python3
"""生成可读、分页的动作复核图。采样标签用于定位，不代表语义或毫秒精度。"""
import argparse
import json
import math
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

GENERATOR_VERSION = 3


def run_checked(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr[-2000:] or f"命令失败: {cmd[0]}")
    return result


def probe_video(video_path):
    result = run_checked(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                          '-show_entries', 'format=duration:stream=width,height,avg_frame_rate',
                          '-of', 'json', str(video_path)])
    data = json.loads(result.stdout)
    if not data.get('streams'):
        raise ValueError('文件没有可读取的视频流')
    stream = data['streams'][0]
    n, d = stream.get('avg_frame_rate', '0/1').split('/')
    return {**stream, 'duration': float(data['format']['duration']),
            'fps': float(n) / float(d) if float(d) else 0}


def validate_window(video_path, start, end, fps):
    if not all(math.isfinite(v) for v in (start, end, fps)):
        raise ValueError('时间和采样率必须是有限数值')
    if start < 0 or end <= start or not 0 < fps <= 120:
        raise ValueError('要求 0 <= start < end，且 0 < fps <= 120')
    info = probe_video(video_path)
    if end > info['duration'] + 0.001:
        raise ValueError(f"出点超出视频时长 {info['duration']:.6f}s")
    return info


def analyze_cut_window(video_path, start_sec, end_sec, output_jpg, fps=10, crop=None):
    started = time.monotonic()
    source = Path(video_path).resolve()
    output = Path(output_jpg).resolve()
    crop = list(crop) if crop is not None else None
    fingerprint = {'generator_version': GENERATOR_VERSION, 'source': str(source), 'output': str(output),
                   'source_mtime_ns': source.stat().st_mtime_ns,
                   'source_size': source.stat().st_size,
                   'window': [start_sec, end_sec], 'sample_fps': fps,
                   'crop_after_autorotation': crop}
    manifest_path = output.with_suffix('.json')
    if manifest_path.exists():
        try:
            cached = json.loads(manifest_path.read_text(encoding='utf-8'))
            if (all(cached.get(k) == v for k, v in fingerprint.items())
                    and cached.get('page_files')
                    and cached.get('pages') == [p['path'] for p in cached['page_files']]
                    and all(Path(p['path']).is_file()
                            and Path(p['path']).stat().st_size == p['size']
                            and Path(p['path']).stat().st_mtime_ns == p['mtime_ns']
                            for p in cached['page_files'])):
                return {**cached, 'cache_hit': True, 'elapsed_seconds': round(time.monotonic()-started, 3),
                        'visual_review': '未检查'}
        except (OSError, ValueError, KeyError, TypeError):
            pass  # 缺页、改图或参数变化时重新抽帧；缓存不是当前模型的看片证明。
    info = validate_window(video_path, start_sec, end_sec, fps)
    if (end_sec - start_sec) * fps > 7200:
        raise ValueError('单窗口超过7200个采样，请拆分窗口')
    filters = []
    if crop is not None:
        x, y, w, h = crop
        if not all(math.isfinite(v) for v in crop) or min(x, y) < 0 or min(w, h) <= 0 or x+w > 1 or y+h > 1:
            raise ValueError('crop 应为归一化 x y w h，且完全位于画面内')
        filters.append(f'crop=iw*{w}:ih*{h}:iw*{x}:ih*{y}')
    filters.extend([f'fps={fps}:start_time=0', 'scale=360:640:force_original_aspect_ratio=decrease'])
    output.parent.mkdir(parents=True, exist_ok=True)
    font_path = Path('/System/Library/Fonts/Supplemental/Arial.ttf')
    font = ImageFont.truetype(str(font_path), 22) if font_path.exists() else ImageFont.load_default()
    pages, samples = [], []
    with tempfile.TemporaryDirectory(prefix='cake_review_') as tmp:
        run_checked(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                     '-ss', str(start_sec), '-i', str(video_path), '-t', str(end_sec-start_sec),
                     '-vf', ','.join(filters), '-q:v', '2', str(Path(tmp)/'%06d.jpg')])
        frames = sorted(Path(tmp).glob('*.jpg'))
        if not frames:
            raise RuntimeError('抽帧为空，不能算复核成功')
        times = [start_sec+i/fps for i in range(len(frames))]
        # 稀疏采样额外保留区间末尾，避免最后不足一个采样间隔的完成态漏看。
        endpoint = max(start_sec, end_sec-1/(info['fps'] or 30))
        if fps <= 3 and endpoint > times[-1]+1e-6:
            tail = Path(tmp)/'tail.jpg'
            run_checked(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                         '-ss', str(endpoint), '-i', str(video_path), '-frames:v', '1',
                         '-vf', ','.join(f for f in filters if not f.startswith('fps=')),
                         '-q:v', '2', str(tail)])
            if not tail.is_file():
                raise RuntimeError('末尾抽帧为空，请确认区间结束位置')
            frames.append(tail)
            times.append(endpoint)
        with Image.open(frames[0]) as first:
            w, h = first.size
        for page_idx in range(math.ceil(len(frames)/12)):
            batch = frames[page_idx*12:(page_idx+1)*12]
            cols = min(4, len(batch))
            grid = Image.new('RGB', (cols*w, math.ceil(len(batch)/cols)*(h+30)), '#202020')
            page_path = output if page_idx == 0 else output.with_name(f'{output.stem}_{page_idx+1:03d}{output.suffix}')
            for j, frame in enumerate(batch):
                index = page_idx*12+j
                nominal = times[index]
                with Image.open(frame) as im:
                    tile = Image.new('RGB', (w, h+30), 'black')
                    tile.paste(im.convert('RGB'), (0, 30))
                ImageDraw.Draw(tile).text((5, 3), f'~{nominal:.3f}s   #{index+1}', fill='yellow', font=font)
                grid.paste(tile, ((j%cols)*w, (j//cols)*(h+30)))
                samples.append({'sample_time_approx': round(nominal, 6), 'page': str(page_path)})
            grid.save(page_path, quality=92)
            pages.append(str(page_path))
    manifest = {**fingerprint,
                'generated_at': datetime.now(timezone.utc).isoformat(),
                'source_fps': info['fps'], 'pages': pages, 'samples': samples,
                'page_files': [{'path': p, 'size': Path(p).stat().st_size,
                                'mtime_ns': Path(p).stat().st_mtime_ns} for p in pages],
                'cache_hit': False, 'elapsed_seconds': round(time.monotonic()-started, 3),
                'timestamp_note': '名义采样时间；存在采样/源帧舍入误差。不是接触识别或毫秒精度。',
                'visual_review': '未检查'}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest


def summary(result):
    return {k: result[k] for k in ('source', 'window', 'pages', 'sample_fps',
                                   'cache_hit', 'elapsed_seconds', 'visual_review', 'timestamp_note')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('video', nargs='?')
    parser.add_argument('start', type=float, nargs='?')
    parser.add_argument('end', type=float, nargs='?')
    parser.add_argument('output', nargs='?')
    parser.add_argument('--fps', type=float, default=3, help='普通动作默认3；小五官/疑点用10')
    parser.add_argument('--crop', nargs=4, type=float, metavar=('X', 'Y', 'W', 'H'))
    parser.add_argument('--batch', help='JSON任务数组，每项含video/start/end/output，可选fps/crop')
    args = parser.parse_args()
    try:
        if args.batch:
            if any(v is not None for v in (args.video, args.start, args.end, args.output)):
                raise ValueError('--batch 不能同时带单窗口参数')
            jobs = json.loads(Path(args.batch).read_text(encoding='utf-8'))
            if not isinstance(jobs, list) or not jobs:
                raise ValueError('批量文件必须是非空任务数组')
            outputs = [str(Path(job['output']).resolve().with_suffix('.json')) for job in jobs]
            if len(set(outputs)) != len(outputs):
                raise ValueError('每个批量窗口必须使用不同输出路径')
            for job in jobs:
                result = analyze_cut_window(job['video'], float(job['start']), float(job['end']),
                                            job['output'], float(job.get('fps', args.fps)), job.get('crop'))
                print(json.dumps(summary(result), ensure_ascii=False), flush=True)
        else:
            if any(v is None for v in (args.video, args.start, args.end, args.output)):
                raise ValueError('需要video/start/end/output，或--batch任务文件')
            result = analyze_cut_window(args.video, args.start, args.end, args.output, args.fps, args.crop)
            print(json.dumps(summary(result), ensure_ascii=False))
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        parser.exit(1, f'抽帧失败: {exc}\n')


if __name__ == '__main__':
    main()
