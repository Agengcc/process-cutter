"""原声模式共用：Whisper 转写、中文归一、Whisper 常见编造句。

转写优先用 mlx-whisper（Apple 芯片），没有再用 openai-whisper；都没有就停下报告。
运行示例：uv run --python 3.13 --with mlx-whisper --with zhconv --with numpy python3 脚本.py ...
"""
import difflib
import os
import re
import subprocess

import numpy as np

SR = 16000
MLX_MODEL = os.environ.get('ASR_MODEL', 'mlx-community/whisper-large-v3-turbo')
PROMPT = '以下是普通话的句子。'
# Whisper 在安静、风声、锅声上常编的套话（借自 footage-to-vlog speech.py，MIT）
INVENTED = re.compile(r'(中文)?字幕(由|志愿者|提供|制作|by)[^。，,.!?]*|请不吝[^。]*|(点赞|订阅|打赏)[^。]*(频道|栏目)?[^。]*|'
                      r'明镜与点点栏目|优优独播剧场[^。]*|谢谢(观看|收看|大家)[。！]?|感谢观看[。！]?|'
                      r'thank(s| you) for watching[.!]?|subtitles? by[^.]*\.?|please subscribe[.!]?', re.I)

try:
    import zhconv
    def simp(t):
        return zhconv.convert(t, 'zh-cn')
except ImportError:          # 没装 zhconv 时不做繁简转换
    def simp(t):
        return t

_backend = None


def _load():
    global _backend
    if _backend is None:
        try:
            import mlx_whisper
            _backend = ('mlx', mlx_whisper)
        except ImportError:
            try:
                import whisper
                _backend = ('openai', whisper.load_model('large-v3-turbo'))
            except ImportError:
                raise SystemExit('需要 mlx-whisper 或 openai-whisper；例：uv run --with mlx-whisper ... python3 本脚本')
    return _backend


def transcribe(x, lang='zh'):
    """x: 16kHz 单声道 float32 数组。返回去掉编造句后的文字（简体）。"""
    if len(x) < SR * 0.05:
        return ''
    kind, m = _load()
    # temperature=0：同一段声音每次转写结果一样，清单和核对报告可复现
    opts = dict(language=lang, condition_on_previous_text=False, initial_prompt=PROMPT if lang == 'zh' else None,
                temperature=0.0)
    if kind == 'mlx':
        r = m.transcribe(x.astype(np.float32), path_or_hf_repo=MLX_MODEL, **opts)
    else:
        r = m.transcribe(x.astype(np.float32), **opts)
    segs = [s for s in r.get('segments', []) if s.get('no_speech_prob', 0) < 0.8]
    return simp(INVENTED.sub('', ''.join(s['text'] for s in segs)).strip())


def load_audio(path, af=None):
    cmd = ['ffmpeg', '-v', 'error', '-i', str(path), '-vn', '-ac', '1', '-ar', str(SR)]
    if af:
        cmd += ['-af', af]
    raw = subprocess.run(cmd + ['-f', 'f32le', '-'], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).copy()


def cut(x, a, b):
    return x[max(0, int(a * SR)):max(0, int(b * SR))]


def norm(t):
    """只留汉字、字母、数字，用于比对。"""
    return re.sub(r'[^0-9a-z一-鿿]', '', simp(t).lower())


def similar(a, b):
    a, b = norm(a), norm(b)
    if not a and not b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def recall(expected, heard):
    """expected 的字有多少在 heard 里按顺序出现（0–1）。"""
    e, h = norm(expected), norm(heard)
    if not e:
        return 1.0
    return sum(m.size for m in difflib.SequenceMatcher(None, e, h, autojunk=False).get_matching_blocks()) / len(e)
