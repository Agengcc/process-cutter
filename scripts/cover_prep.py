#!/usr/bin/env python3
"""
cover_prep.py - 封面照片居中黑边适配工具 (Letterbox Fit)
解决问题：
1. 自动纠正 EXIF 旋转；
2. 保持原图完整内容（不进行任何暴力裁切破坏构图），按原图宽高比自适应缩放并居中放置在 1080x1920 竖屏画布上；
3. 上下自动留出对称纯黑边 (Letterbox)，适合非编与短视频平台发布。
"""

import sys
import os
import shutil
import subprocess
import tempfile
from PIL import Image, ImageOps


def open_image(input_path):
    """打开封面照片；iPhone 的 HEIC 优先用 pillow-heif，没有时在 macOS 上用系统 sips 转 JPG。"""
    try:
        return Image.open(input_path)
    except Exception as first_error:
        if os.path.splitext(input_path)[1].lower() not in ('.heic', '.heif'):
            raise
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
            return Image.open(input_path)
        except ImportError:
            pass
        if not shutil.which('sips'):
            raise RuntimeError('无法读取 HEIC：请安装 pillow-heif（pip install pillow-heif），或先把照片转成 JPG') from first_error
        tmp = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False).name
        subprocess.run(['sips', '-s', 'format', 'jpeg', input_path, '--out', tmp], check=True, capture_output=True)
        im = Image.open(tmp)
        im.load()
        os.unlink(tmp)
        return im

def prepare_cover(input_path, output_path="cover_upright.jpg"):
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"找不到封面图: {input_path}")
    
    im = open_image(input_path)
    # 纠正 EXIF 旋转
    im = ImageOps.exif_transpose(im)
    
    w, h = im.size
    target_w, target_h = 1080, 1920
    
    # 保持原图长宽比，等比缩放到画布内（以宽或高撑满）
    ratio = min(target_w / float(w), target_h / float(h))
    new_w = int(round(w * ratio))
    new_h = int(round(h * ratio))
    
    resized = im.resize((new_w, new_h), Image.Resampling.LANCZOS)
    
    # 创建纯黑背景
    canvas = Image.new('RGB', (target_w, target_h), (0, 0, 0))
    paste_x = (target_w - new_w) // 2
    paste_y = (target_h - new_h) // 2
    canvas.paste(resized, (paste_x, paste_y))
    
    canvas.save(output_path, quality=95)
    print(f"✅ 封面已生成（原图居中不裁剪，上下黑边适配 1080x1920）: {output_path}")
    return output_path

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python3 cover_prep.py <input_image> [output_image]")
        sys.exit(1)
    out = sys.argv[2] if len(sys.argv) > 2 else "cover_upright.jpg"
    prepare_cover(sys.argv[1], out)
