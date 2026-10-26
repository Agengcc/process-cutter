#!/usr/bin/env python3
"""检查素材是不是手机HDR，是就转成普通画面（SDR）副本再剪。

iPhone 可能用 HLG/杜比视界录 HDR；不转换直接剪，成片会发灰发闷。
做法借自 TinyAlmond/footage-to-vlog 的 hdr_to_sdr.py（MIT）：
macOS 用系统自带 avconvert（苹果自己的色调映射，与“照片”导出一致，保留帧率和声音，竖屏方向自动转正）；
其他系统用带 zscale 的 FFmpeg。两者都没有就停下报告，不硬剪。

用法：python3 hdr_to_sdr.py 素材目录 输出目录
输出目录里：HDR 素材是转好的副本，SDR 素材是指向原片的链接，同名；之后用它做 build --media-dir。
原素材只读。全部是SDR时只打印结果，不建输出目录。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

VIDEO = {'.mov', '.mp4', '.m4v'}
HDR_TRC = {'arib-std-b67': 'HLG', 'smpte2084': 'PQ'}


def transfer(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                          'stream=color_transfer', '-of', 'json', str(path)], capture_output=True, text=True)
    streams = json.loads(out.stdout or '{}').get('streams') or [{}]
    return HDR_TRC.get(streams[0].get('color_transfer', ''))


def has_zscale():
    out = subprocess.run(['ffmpeg', '-hide_banner', '-filters'], capture_output=True, text=True).stdout
    return ' zscale ' in out


def convert(src, dst):
    if sys.platform == 'darwin' and shutil.which('avconvert'):
        subprocess.run(['avconvert', '--source', str(src), '--output', str(dst),
                        '--preset', 'PresetHighestQuality', '--replace'], check=True, capture_output=True)
    elif has_zscale():
        vf = ('zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,'
              'zscale=t=bt709:m=bt709:r=tv,format=yuv420p')
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(src), '-vf', vf, '-c:v', 'libx264', '-crf', '16',
                        '-preset', 'medium', '-color_primaries', 'bt709', '-color_trc', 'bt709',
                        '-colorspace', 'bt709', '-c:a', 'copy', str(dst)], check=True)
    else:
        raise SystemExit('有HDR素材，但本机没有 avconvert（macOS自带）也没有带 zscale 的 FFmpeg，无法转换；请先装带 libzimg 的 FFmpeg')
    if transfer(dst):
        raise SystemExit(f'{dst.name} 转换后仍是HDR，停止')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('src')
    ap.add_argument('out')
    a = ap.parse_args()
    src = Path(a.src).resolve()
    clips = sorted(p for p in src.iterdir() if p.suffix.lower() in VIDEO and not p.name.startswith('.'))
    kinds = {p: transfer(p) for p in clips}
    hdr = [p for p in clips if kinds[p]]
    if not hdr:
        print(f'{len(clips)}段素材都是SDR，不用转换，直接用原素材目录。')
        return
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    for p in clips:
        dst = out / p.name
        if kinds[p]:
            if not dst.exists() or dst.is_symlink():
                if dst.is_symlink():
                    dst.unlink()
                convert(p, dst)
            print(f'{p.name}: {kinds[p]} → SDR 副本')
        elif not dst.exists():
            dst.symlink_to(p)
    print(f'{len(hdr)}/{len(clips)}段是HDR，已转换。之后 build --media-dir 用：{out}')


if __name__ == '__main__':
    main()
