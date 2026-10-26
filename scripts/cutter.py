#!/usr/bin/env python3
"""
cutter.py - 工序剪辑引擎：选片表 → 工程 → 竖屏成片（只依赖 FFmpeg）
负责：
1. 用 ffprobe 探测媒体尺寸、时长与方向（iPhone 竖屏旋转由 FFmpeg 自动转正）；
2. 解析选片表 CSV，构建 project.json；
3. 按输出帧网格排连续时间线，不越过已确认的源出点；
4. 按速度倍率与显式构图数值，用 FFmpeg 逐段裁切、变速、适配画幅后拼接导出；
5. 默认静音待配乐；可选直接混入 BGM，或保留原片现场声。
"""

import os
import sys
import json
import uuid
import csv
import argparse
import subprocess
import math
import re
import tempfile
from pathlib import Path
from PIL import Image
import captions as caption_tools
import vision_check

IMAGE_SUFFIXES = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff', '.heic', '.heif'}


def run_checked(cmd):
    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError(f'缺少 {cmd[0]}，请先安装 FFmpeg（含 ffprobe）') from exc
    if res.returncode:
        raise RuntimeError(f'{cmd[0]} 执行失败\n{res.stderr[-1500:]}')
    return res


def _rotation(stream):
    """读取 iPhone 等设备写入的旋转信息；FFmpeg 解码时会自动转正。"""
    for side in stream.get('side_data_list') or []:
        if 'rotation' in side:
            return int(round(float(side['rotation'])))
    return int(round(float((stream.get('tags') or {}).get('rotate', 0) or 0)))


def probe_media(file_path):
    """用 ffprobe 探测媒体尺寸（按转正后方向）、时长、帧率和音频；"""
    path = Path(file_path).resolve()
    res = run_checked(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)])
    data = json.loads(res.stdout)
    streams = data.get('streams') or []
    video = next((st for st in streams if st.get('codec_type') == 'video'), None)
    audio = next((st for st in streams if st.get('codec_type') == 'audio'), None)
    if video is None:
        raise RuntimeError(f'媒体没有可读取的画面: {path}')
    width, height = int(video.get('width') or 0), int(video.get('height') or 0)
    if abs(_rotation(video)) % 180 == 90:
        width, height = height, width
    num, den = (video.get('avg_frame_rate') or '0/1').split('/')
    fps = float(num) / float(den) if float(den) else 0.0
    is_image = path.suffix.lower() in IMAGE_SUFFIXES or (data.get('format') or {}).get('format_name', '').endswith('_pipe')
    duration = 0.0 if is_image else float((data.get('format') or {}).get('duration') or video.get('duration') or 0)
    return {'path': str(path), 'kind': 'image' if is_image else 'video', 'duration': round(duration, 6),
            'width': width, 'height': height, 'fps': round(fps, 6) if fps else 25.0,
            'has_video': True, 'has_audio': audio is not None and not is_image,
            'sample_rate': int(audio.get('sample_rate') or 0) if audio else 0,
            'channels': int(audio.get('channels') or 0) if audio else 0,
            'proxy': None, 'name': path.name, 'folder': '',
            'hdr': {'arib-std-b67': 'HLG', 'smpte2084': 'PQ'}.get(video.get('color_transfer', ''))}

def build_project_from_csv(csv_path, media_base_dir, project_name="工序剪辑", overlap_sec=0.0):
    """校验复核记录后构建工程。记录完整不等于视觉判断已被脚本证明。"""
    if overlap_sec != 0:
        raise ValueError('禁止在已确认源出点后自动添加重叠时长')
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"找不到选片表文件: {csv_path}")
    if not vision_check.passed(Path(csv_path).resolve().parent):
        raise ValueError('选片表目录没有通过的看图测验（看图校验.json）：先运行 scripts/vision_check.py make/verify，'
                         '证明当前模型真的看得到图片；看不到就停在候选整理，不构建正式工程')

    shots = []
    with open(csv_path, 'r', encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        required = {'源文件', '主体ID', '片段类型', '源入点秒', '源出点秒',
                    '速度倍率', '有效动作', '入点依据', '出点依据', '复核图', '复核状态'}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f'选片表缺少新版必填列: {", ".join(sorted(missing))}')
        for row in reader:
            if not any(row.values()):
                continue
            number = len(shots) + 1
            for field in required:
                if not str(row.get(field) or '').strip():
                    raise ValueError(f'第{number}段缺少 {field}')
            if row['片段类型'].strip() not in ('制作', '讲解', '展示', '封面'):
                raise ValueError(f'第{number}段片段类型只能是制作/讲解/展示/封面')
            if row['复核状态'].strip() != '已看片确认':
                raise ValueError(f'第{number}段未看片确认，不构建正式工程')
            if row['主体ID'].strip() in ('未知', '待确认', '未确认', '未检查'):
                raise ValueError(f'第{number}段主体尚未辨认')
            for image_path in row['复核图'].split('|'):
                image = Path(image_path.strip())
                if not image.is_absolute():
                    image = Path(csv_path).resolve().parent / image
                if not image.is_file():
                    raise FileNotFoundError(f'第{number}段复核图不存在: {image}')
                with Image.open(image) as opened:
                    opened.verify()
            shots.append(row)

    if not shots:
        raise ValueError("选片表为空！")

    # 1. 扫描用到的所有不同媒体文件
    media_map = {}
    media_list = []
    
    for row in shots:
        fn = row['源文件'].strip()
        if fn not in media_map:
            # 查找文件路径
            full_path = os.path.join(media_base_dir, fn)
            if not os.path.exists(full_path):
                # 尝试在上一级目录查找
                parent_path = os.path.join(os.path.dirname(media_base_dir), fn)
                if os.path.exists(parent_path):
                    full_path = parent_path
                else:
                    raise FileNotFoundError(f"找不到媒体文件: {fn} (在 {media_base_dir} 或其上级)")
            
            info = probe_media(full_path)
            if info.get('hdr'):
                raise ValueError(f"{fn} 是手机HDR（{info['hdr']}），直接剪会发灰；先用 scripts/hdr_to_sdr.py 转换，再用输出目录做 --media-dir")
            m_id = f"m{uuid.uuid4().hex[:8]}"
            info['id'] = m_id
            media_map[fn] = m_id
            media_list.append(info)

    # 2. 生成轨道 clips
    clips = []
    current_frames = 0
    output_fps = 30
    info_by_id = {m['id']: m for m in media_list}
    used = {}

    for idx, row in enumerate(shots):
        fn = row['源文件'].strip()
        src_in = float(row['源入点秒'])
        src_out = float(row['源出点秒'])
        speed = float(row['速度倍率'])
        usage = row.get('用途', f"镜头_{idx+1}")
        
        # 只采用显式数值，文字说明不触发隐式放大。
        scale = float(row.get('scale', 1.0)) if row.get('scale') else 1.0
        y_offset = float(row.get('y_offset', 0.0)) if row.get('y_offset') else 0.0
        if not all(math.isfinite(v) for v in (src_in, src_out, speed, scale, y_offset)):
            raise ValueError(f'第{idx+1}段包含非有限数值')
        if src_in < 0 or speed <= 0 or scale <= 0:
            raise ValueError(f'第{idx+1}段入点不能为负，速度和缩放必须大于0')
            
        # 计算播放时长
        media_info = info_by_id[media_map[fn]]
        if media_info['kind'] == 'video':
            if src_out <= src_in or src_out > float(media_info['duration']) + 0.000001:
                raise ValueError(f'第{idx+1}段源范围无效或超出媒体时长')
            src_dur = src_out - src_in
            play_dur = src_dur / speed
        else:
            if row['片段类型'].strip() == '制作':
                raise ValueError('静态图片不能标记为制作动作')
            if src_in != 0 or src_out != 0 or speed != 1:
                raise ValueError('静态图片要求源入/出点为0、速度为1')
            play_dur = float(row.get('成片时长秒') or 0.20)
            src_in = 0.0
        if not math.isfinite(play_dur) or play_dur <= 0:
            raise ValueError(f'第{idx+1}段时长必须是有限正数')
        # 同一画面不放两次：同一素材的选段不能重叠（有意回放时“用途”写“回放”）。
        if media_info['kind'] == 'video' and '回放' not in usage:
            for prev, (a, b) in used.get(fn, []):
                if min(b, src_out) - max(a, src_in) > 0.1:
                    raise ValueError(f'第{idx+1}段与第{prev}段在 {fn} 的 {max(a, src_in):.2f}–{min(b, src_out):.2f} 秒重叠，同一画面会出现两次')
            used.setdefault(fn, []).append((idx + 1, (src_in, src_out)))
        # 向下对齐输出帧，最多舍去不足1输出帧，绝不读到源出点之后。
        clip_frames = math.floor(play_dur * output_fps + 1e-9)
        if clip_frames < 1:
            raise ValueError(f'第{idx+1}段不足1输出帧，请复核切点或速度')
        clip_dur = clip_frames / output_fps

        clip_obj = {
            "id": f"c{uuid.uuid4().hex[:8]}",
            "type": "media",
            "media": media_map[fn],
            # 起点向下取到微秒，保证写入的小数不会晚于对应输出帧。
            "start": (current_frames * 1_000_000 // output_fps) / 1_000_000,
            "in": src_in,
            "duration": clip_dur,
            "speed": speed,
            "reverse": False,
            "fit": "contain",
            "enabled": True,
            "name": f"镜头{idx+1}_{usage}",
            "transform": {
                "x": 0.0,
                "y": y_offset,
                "scale": scale,
                "rotation": 0.0,
                "opacity": 1.0
            },
            "audio": {
                "gain_db": 0.0,
                "mute": True,
                "fade_in": 0.0,
                "fade_out": 0.0,
                "pan": 0.0
            },
            "effects": [],
            "fade_in": 0.0,
            "fade_out": 0.0,
            "transition_out": {
                "type": "dissolve",
                "duration": 0.0
            },
            "text": None,
            "color": "black"
        }
        clips.append(clip_obj)
        current_frames += clip_frames

    project_data = {
        "version": 1,
        "name": project_name,
        "settings": {
            "width": 1080,
            "height": 1920,
            "fps": 30.0,
            "sample_rate": 48000,
            "background": "black"
        },
        "media": media_list,
        "tracks": [
            {
                "id": "V1",
                "name": "V1 - 画面主轨",
                "kind": "video",
                "muted": False,
                "solo": False,
                "locked": False,
                "volume": 1.0,
                "clips": clips
            },
            {
                "id": "A1",
                "name": "A1 - 背景音乐(待配乐)",
                "kind": "audio",
                "muted": False,
                "solo": False,
                "locked": False,
                "volume": 1.0,
                "clips": []
            }
        ]
    }
    return project_data, current_frames / output_fps

def _num(value):
    return f'{value:.6f}'.rstrip('0').rstrip('.') or '0'


def _atempo_chain(speed):
    """atempo 单级只支持 0.5–2.0，超出时串联。"""
    parts, remain = [], speed
    while remain > 2.0:
        parts.append('atempo=2.0')
        remain /= 2.0
    while remain < 0.5:
        parts.append('atempo=0.5')
        remain /= 0.5
    if abs(remain - 1.0) > 1e-9:
        parts.append(f'atempo={_num(remain)}')
    return parts


def _filter_flag():
    out = run_checked(['ffmpeg', '-hide_banner', '-version']).stdout
    try:
        major = int(out.split()[2].split('.')[0].lstrip('n'))
    except (IndexError, ValueError):
        major = 0
    return '-/filter_complex' if major >= 7 else '-filter_complex_script'


AUDIO_EDGE_FADE = 0.02  # 每个剪接点两侧各20毫秒淡入淡出，防止原声在切口处爆音
CAPTION_MAX = 14        # 配音字幕每行最多字数


def load_voice_table(path):
    """配音表.csv（voice_tools.py synth 生成）：句号,成片起秒,文件,文案。每句整段放在成片起秒，不切开。"""
    base = Path(path).resolve().parent
    lines = []
    with open(path, encoding='utf-8-sig', newline='') as handle:
        for row in csv.DictReader(handle):
            if not any((v or '').strip() for v in row.values()):
                continue
            if (row.get('处理') or '').startswith('需改短'):
                raise ValueError(f'第{row["句号"]}句标着“需改短”，先改文案重新合成')
            file = Path(row['文件'].strip())
            file = file if file.is_absolute() else base / file
            if not file.is_file():
                raise FileNotFoundError(f'找不到第{row["句号"]}句配音: {file}')
            start = float(row['成片起秒'])
            if not math.isfinite(start) or start < 0:
                raise ValueError(f'第{row["句号"]}句起点无效')
            probe = run_checked(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(file)])
            words = []
            if (row.get('逐字时间') or '').strip():   # voice_tools synth 写的逐字时间（秒，已按加速缩放）
                words = json.loads((base / row['逐字时间'].strip()).read_text(encoding='utf-8'))
            lines.append({'no': row['句号'], 'path': str(file), 'start': start,
                          'duration': float(probe.stdout.strip()), 'text': (row.get('文案') or '').strip(),
                          'words': words})
    lines.sort(key=lambda v: v['start'])
    for prev, cur in zip(lines, lines[1:]):
        if prev['start'] + prev['duration'] > cur['start'] + 0.02:
            raise ValueError(f'第{prev["no"]}句配音还没念完，第{cur["no"]}句就开始了（重叠）')
    return lines


def _char_times(words):
    """逐字时间 → 每个文字（不含标点）的 (起, 止)；一个条目含多个字时平分。"""
    out = []
    for w in words:
        chars = re.findall(r'[0-9A-Za-z一-鿿]', w.get('text', ''))
        step = (float(w['end']) - float(w['start'])) / max(1, len(chars))
        out += [(float(w['start']) + i * step, float(w['start']) + (i + 1) * step) for i in range(len(chars))]
    return out


def voice_captions(lines):
    """字幕直接用配音文案：按标点断成每行不超过14字的短句。
    有逐字时间且字数对得上时，每行从它第一个字开口时出现、到下一行开口时换掉；否则按字数分配到这句配音的时长里。"""
    placed = []
    for line in lines:
        parts = [p.strip() for p in re.split(r'(?<=[。！？!?，,；;、：:])', line['text']) if p.strip()]
        chunks = []
        for part in parts:
            while len(part) > CAPTION_MAX:
                chunks.append(part[:CAPTION_MAX])
                part = part[CAPTION_MAX:]
            if part:
                chunks.append(part)
        weights = [max(1, len(re.findall(r'[0-9A-Za-z一-鿿]', c))) for c in chunks]
        chars = _char_times(line.get('words') or [])
        end = line['start'] + line['duration']
        if chars and len(chars) == sum(len(re.findall(r'[0-9A-Za-z一-鿿]', c)) for c in chunks):
            starts, k = [], 0
            for chunk in chunks:
                starts.append(line['start'] + chars[min(k, len(chars) - 1)][0])
                k += len(re.findall(r'[0-9A-Za-z一-鿿]', chunk))
            for i, chunk in enumerate(chunks):
                stop = starts[i + 1] if i + 1 < len(chunks) else end
                placed.append({'start': round(starts[i], 4), 'end': round(stop, 4), 'text': chunk.rstrip('。，,；;、：:')})
            continue
        t = line['start']
        for chunk, w in zip(chunks, weights):
            span = line['duration'] * w / sum(weights)
            placed.append({'start': round(t, 4), 'end': round(t + span, 4), 'text': chunk.rstrip('。，,；;、：:')})
            t += span
    return placed


def compile_render(project, source_audio=False, music=None, music_gain_db=0.0, music_fade_out=2.0,
                   captions=None, workdir=None, caption_font=None, duck=True, voice=None):
    """把工程编成 FFmpeg 参数：每段 裁切→变速→统一帧率→等比适配画幅→（显式构图）→精确帧数，最后顺序拼接。"""
    settings = project['settings']
    width, height, fps = int(settings['width']), int(settings['height']), float(settings['fps'])
    media = {m['id']: m for m in project['media']}
    track = next(t for t in project['tracks'] if t['kind'] == 'video')
    clips = sorted((c for c in track['clips'] if c.get('enabled', True)), key=lambda c: float(c['start']))
    if not clips:
        raise ValueError('工程没有有效视频片段')
    inputs, chains, labels = [], [], []
    expected_frame = 0
    for k, clip in enumerate(clips):
        if abs(float(clip['start']) * fps - expected_frame) > 0.01:
            raise ValueError(f'第{k+1}段起点与前一段结尾不连续，时间线有空隙或重叠')
        frames = int(round(float(clip['duration']) * fps))
        if frames < 1:
            raise ValueError(f'第{k+1}段不足1帧')
        expected_frame += frames
        m = media[clip['media']]
        speed = float(clip.get('speed') or 1.0)
        index = len(inputs)
        if m['kind'] == 'image':
            inputs.append(['-loop', '1', '-framerate', _num(fps), '-t', _num(frames / fps + 0.1), '-i', m['path']])
            pre = 'setpts=PTS-STARTPTS'
        else:
            src_in = float(clip['in'])
            # 多读0.05秒只为让帧率换算有后续帧可取；输出按帧数截断，不会显示源出点之后的画面。
            inputs.append(['-ss', _num(src_in), '-t', _num(frames / fps * speed + 0.05), '-i', m['path']])
            pre = 'setpts=PTS-STARTPTS' + (f',setpts=PTS/{_num(speed)}' if abs(speed - 1) > 1e-9 else '')
        chain = (f'[{index}:v]{pre},fps={_num(fps)},'
                 f'scale={width}:{height}:force_original_aspect_ratio=decrease:flags=bicubic,'
                 f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1')
        transform = clip.get('transform') or {}
        scale, x, y = float(transform.get('scale', 1.0)), float(transform.get('x', 0.0)), float(transform.get('y', 0.0))
        if abs(scale - 1) > 1e-9 or abs(x) > 1e-9 or abs(y) > 1e-9:
            chains.append(f'color=c=black:s={width}x{height}:r={_num(fps)}[bg{k}]')
            chain += f',scale=iw*{_num(scale)}:ih*{_num(scale)}:flags=bicubic[sc{k}];' \
                     f'[bg{k}][sc{k}]overlay=x=(W-w)/2+({_num(x)}):y=(H-h)/2+({_num(y)}):shortest=1'
        chain += f',trim=end_frame={frames},setpts=PTS-STARTPTS,format=yuv420p[v{k}]'
        chains.append(chain)
        labels.append(f'[v{k}]')
        if source_audio:
            seconds = _num(frames / fps)
            if m['kind'] == 'video' and m.get('has_audio'):
                tempo = ''.join(',' + p for p in _atempo_chain(speed))
                edge = _num(min(AUDIO_EDGE_FADE, frames / fps / 4))
                chains.append(f'[{index}:a]asetpts=PTS-STARTPTS{tempo},aresample=48000,'
                              f'aformat=channel_layouts=stereo,apad,atrim=0:{seconds},asetpts=PTS-STARTPTS,'
                              f'afade=t=in:d={edge},areverse,afade=t=in:d={edge},areverse[a{k}]')
            else:
                chains.append(f'anullsrc=r=48000:cl=stereo,atrim=0:{seconds}[a{k}]')
            labels.append(f'[a{k}]')
    total = expected_frame / fps
    voice_lines = load_voice_table(voice) if voice else []
    for line in voice_lines:
        if line['start'] + line['duration'] > total + 0.05:
            raise ValueError(f'第{line["no"]}句配音超出成片结尾（成片{total:.2f}秒）')
    want_caps = bool(captions or voice_lines)
    video_out = '[vcat]' if want_caps else '[vout]'
    chains.append(''.join(labels) + f'concat=n={len(clips)}:v=1:a={1 if source_audio else 0}'
                  + (f'{video_out}[src]' if source_audio else video_out))
    audio_label = '[src]' if source_audio else None
    placed = []
    if want_caps:
        if captions:
            placed = caption_tools.map_to_timeline(caption_tools.load_captions(captions), clips, media, fps)
        placed = sorted(placed + voice_captions(voice_lines), key=lambda c: c['start'])
        files = caption_tools.render_caption_images(placed, width, height, Path(workdir) / 'captions', caption_font)
        current = '[vcat]'
        for n, (cap, png) in enumerate(zip(placed, files)):
            cap_index = len(inputs)
            inputs.append(['-i', png])
            nxt = '[vout]' if n == len(placed) - 1 else f'[cv{n}]'
            chains.append(f"{current}[{cap_index}:v]overlay=0:0:eof_action=repeat:"
                          f"enable='between(t,{_num(cap['start'])},{_num(cap['end'] - 0.0001)})'{nxt}")
            current = nxt
        if not placed:
            chains.append('[vcat]null[vout]')
    if music:
        music_index = len(inputs)
        inputs.append(['-i', str(Path(music).resolve())])
        fade = min(max(music_fade_out, 0.0), total)
        chains.append(f'[{music_index}:a]aresample=48000,aformat=channel_layouts=stereo,volume={_num(music_gain_db)}dB,'
                      f'apad,atrim=0:{_num(total)},asetpts=PTS-STARTPTS'
                      + (f',afade=t=out:st={_num(total - fade)}:d={_num(fade)}' if fade > 0 else '') + '[bgm]')
        if source_audio:
            if duck:  # 有人说话或现场声大时，BGM 自动让位
                chains.append('[src]asplit=2[srcmix][srckey]')
                chains.append('[bgm][srckey]sidechaincompress=threshold=0.03:ratio=6:attack=60:release=500[bgmduck]')
                chains.append('[srcmix][bgmduck]amix=inputs=2:normalize=0:duration=first[aout]')
            else:
                chains.append('[src][bgm]amix=inputs=2:normalize=0:duration=first[aout]')
            audio_label = '[aout]'
        else:
            audio_label = '[bgm]'
    if voice_lines:  # 每句整段放到成片起秒；有现场声或配乐时，它们给配音让位
        vlabels = []
        for n, line in enumerate(voice_lines):
            vi = len(inputs)
            inputs.append(['-i', line['path']])
            ms = int(round(line['start'] * 1000))
            chains.append(f'[{vi}:a]aresample=48000,aformat=channel_layouts=stereo,adelay={ms}:all=1[vo{n}]')
            vlabels.append(f'[vo{n}]')
        mix = f'amix=inputs={len(vlabels)}:normalize=0,' if len(vlabels) > 1 else ''
        chains.append(''.join(vlabels) + f'{mix}apad,atrim=0:{_num(total)},asetpts=PTS-STARTPTS[voice]')
        if audio_label:
            chains.append('[voice]asplit=2[vmix][vkey]')
            chains.append(f'{audio_label}[vkey]sidechaincompress=threshold=0.02:ratio=8:attack=30:release=400[beddk]')
            chains.append('[vmix][beddk]amix=inputs=2:normalize=0:duration=first[afinal]')
            audio_label = '[afinal]'
        else:
            audio_label = '[voice]'
    return inputs, ';\n'.join(chains), audio_label, total, placed


def render_project(project_path, output_path, preview=False, source_audio=False, music=None,
                   music_gain_db=0.0, music_fade_out=2.0, captions=None, caption_font=None, duck=True, voice=None):
    """用 FFmpeg 导出：libx264、yuv420p、恒定帧率；默认无音轨（静音待配乐）。"""
    project = json.loads(Path(project_path).read_text(encoding='utf-8'))
    if music and not Path(music).is_file():
        raise FileNotFoundError(f'找不到配乐文件: {music}')
    if captions and not Path(captions).is_file():
        raise FileNotFoundError(f'找不到字幕表: {captions}')
    fps = _num(float(project['settings']['fps']))
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='cake_render_') as tmp:
        inputs, graph, audio_label, total, placed = compile_render(
            project, source_audio, music, music_gain_db, music_fade_out, captions, tmp, caption_font, duck, voice)
        script = Path(tmp) / 'graph.txt'
        script.write_text(graph, encoding='utf-8')
        cmd = ['ffmpeg', '-hide_banner', '-nostdin', '-loglevel', 'error', '-stats', '-y']
        for part in inputs:
            cmd += part
        cmd += [_filter_flag(), str(script), '-map', '[vout]']
        cmd += ['-c:v', 'libx264', '-preset', 'veryfast' if preview else 'slow', '-crf', '26' if preview else '19',
                '-pix_fmt', 'yuv420p', '-fps_mode', 'cfr', '-r', fps]
        if audio_label:
            cmd += ['-map', audio_label, '-c:a', 'aac', '-b:a', '192k']
        else:
            cmd += ['-an']
        cmd += ['-movflags', '+faststart', str(output)]
        print(f'执行渲染: {len(inputs)} 个输入，成片 {total:.2f} 秒' + ('，含配乐' if music else '')
              + ('，保留现场声' if source_audio else '') + ('，含配音' if voice else '')
              + (f'，字幕{len(placed)}条' if placed else ''))
        res = subprocess.run(cmd)
        if res.returncode != 0:
            raise RuntimeError(f'渲染失败，退出码: {res.returncode}')
    if placed:  # 留一份成片时间的字幕清单，供审片核对时间与文字
        output.with_suffix('.字幕时间.json').write_text(json.dumps(placed, ensure_ascii=False, indent=1), encoding='utf-8')
    print(f'渲染成功: {output}')


def main():
    parser = argparse.ArgumentParser(description="工序剪辑引擎：选片表 → 工程 → 竖屏成片")
    subparsers = parser.add_subparsers(dest="action", help="执行动作")

    # build
    p_build = subparsers.add_parser("build", help="从选片表生成 project.json")
    p_build.add_argument("--csv", required=True, help="选片表 CSV 路径")
    p_build.add_argument("--media-dir", required=True, help="素材目录路径")
    p_build.add_argument("--output", required=True, help="导出的 project.json 路径")
    p_build.add_argument("--name", default="工序剪辑", help="工程名称")

    # render
    p_render = subparsers.add_parser("render", help="用 FFmpeg 渲染工程")
    p_render.add_argument("--project", required=True, help="project.json 路径")
    p_render.add_argument("--output", required=True, help="输出 MP4 路径")
    p_render.add_argument("--preview", action="store_true", help="快速低码率预览（不作为交付）")
    p_render.add_argument("--music", help="配乐文件；不传则导出无音轨的待配乐版")
    p_render.add_argument("--music-gain", type=float, default=0.0, help="配乐增益 dB，默认 0")
    p_render.add_argument("--music-fade-out", type=float, default=2.0, help="配乐结尾淡出秒数，默认 2")
    p_render.add_argument("--source-audio", action="store_true", help="保留原片现场声（变速不变调）")
    p_render.add_argument("--captions", help="字幕表 CSV（源文件,源起秒,源止秒,文字），按源时间自动换算并烧进画面")
    p_render.add_argument("--voice", help="配音表 CSV（voice_tools.py synth 生成）：每句整段放在成片起秒，字幕直接用文案")
    p_render.add_argument("--caption-font", help="字幕字体文件；不传时自动找苹方/冬青黑/思源黑等")
    p_render.add_argument("--no-duck", action="store_true", help="配乐与现场声同时存在时，不让配乐自动避让")

    args = parser.parse_args()
    if args.action == "build":
        proj, total_dur = build_project_from_csv(args.csv, args.media_dir, project_name=args.name)
        with open(args.output, 'w', encoding='utf-8') as f:
            json.dump(proj, f, ensure_ascii=False, indent=2)
        print(f"成功生成工程: {args.output} (总时长: {total_dur:.2f}秒)")
    elif args.action == "render":
        render_project(args.project, args.output, preview=args.preview, source_audio=args.source_audio,
                       music=args.music, music_gain_db=args.music_gain, music_fade_out=args.music_fade_out,
                       captions=args.captions, caption_font=args.caption_font, duck=not args.no_duck,
                       voice=args.voice)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
