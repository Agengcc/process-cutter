#!/usr/bin/env python3
"""火山引擎（豆包）语音合成：一句文案 → 一个音频文件。给 voice_tools.py synth 的 --cmd 用。

接口：豆包语音 V3。默认用 WebSocket 双向流式 wss://openspeech.bytedance.com/api/v3/tts/bidirection
（二进制帧：StartConnection→StartSession→TaskRequest→FinishSession，收 TTSResponse 音频分片直到 SessionFinished），
官方文档：https://docs.volcengine.com/docs/DoubaoVoice/bidirectional-streaming-text-to-speech-websocket
帧格式参照官方示例 volcengine/ai-app-lab（arkitect/core/component/tts）。
--protocol http 改用 HTTP 单向流式 POST /api/v3/tts/unidirectional（逐行 JSON，base64 分片，code 20000000 结束）。
同一个密钥在两个接口上的授权可能不同：2026-10-26 实测一个密钥 WebSocket 可用、HTTP 报 not granted。

密钥只从环境变量或本机配置文件读取，不写进仓库、不打印：
  新版控制台：VOLC_TTS_API_KEY
  旧版控制台：VOLC_TTS_APP_ID ＋ VOLC_TTS_ACCESS_KEY
  也可放在 ~/.config/process-cutter/volcengine.json：{"api_key": "..."} 或 {"app_id": "...", "access_key": "..."}，
  可选 "voice"、"resource_id"、"speech_rate"、"protocol"（ws／http）。
音色（speaker）在火山控制台的音色列表里查，例如 zh_female_vv_uranus_bigtts；声音复刻音色以 icl_/S_ 开头。
只用你有权使用的音色；复刻他人声音须获授权。

用法：
  python3 tts_volcengine.py --text-file 句01.txt --out 句01.wav --voice zh_female_vv_uranus_bigtts
  voice_tools.py synth 配音脚本.csv 配音 --cmd 'python3 "$SKILL/scripts/tts_volcengine.py" --text-file {text} --out {out} --voice 音色'
  --words-out 句01.words.json 另存逐字时间（WebSocket），voice_tools 的 {words} 会填这个路径，字幕据此对齐。
  --dry-run 只打印请求（不含密钥），不调用接口。
"""
import argparse
import base64
import json
import os
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BASE = os.environ.get('VOLC_TTS_BASE_URL', 'https://openspeech.bytedance.com')
URL = BASE + '/api/v3/tts/unidirectional'
WS_PATH = '/api/v3/tts/bidirection'
CONFIG = Path.home() / '.config' / 'process-cutter' / 'volcengine.json'
END_CODE = 20000000


def load_config():
    cfg = {}
    if CONFIG.is_file():
        cfg = json.loads(CONFIG.read_text(encoding='utf-8'))
    env = {'api_key': 'VOLC_TTS_API_KEY', 'app_id': 'VOLC_TTS_APP_ID', 'access_key': 'VOLC_TTS_ACCESS_KEY',
           'voice': 'VOLC_TTS_VOICE', 'resource_id': 'VOLC_TTS_RESOURCE_ID', 'protocol': 'VOLC_TTS_PROTOCOL'}
    for key, name in env.items():
        if os.environ.get(name):
            cfg[key] = os.environ[name]
    return cfg


def resource_for(voice, override=None):
    """按音色推断资源 ID：复刻音色→seed-icl-2.0，2.0 音色（含 _uranus_/_saturn_）→seed-tts-2.0，其余→seed-tts-1.0。
    推断不准时用 --resource-id 指定（以控制台为准）。"""
    if override:
        return override
    v = voice.lower()
    if v.startswith(('icl_', 's_')):
        return 'seed-icl-2.0'
    if '_uranus_' in v or '_saturn_' in v:
        return 'seed-tts-2.0'
    return 'seed-tts-1.0'


def build_request(cfg, text, voice, resource, speech_rate):
    headers = {'Content-Type': 'application/json', 'X-Api-Resource-Id': resource,
               'X-Api-Request-Id': str(uuid.uuid4())}
    if cfg.get('api_key'):
        headers['X-Api-Key'] = cfg['api_key']
    elif cfg.get('app_id') and cfg.get('access_key'):
        headers['X-Api-App-Id'] = cfg['app_id']
        headers['X-Api-Access-Key'] = cfg['access_key']
    body = {'user': {'uid': 'process-cutter'},
            'req_params': {'text': text, 'speaker': voice,
                           'audio_params': {'format': 'mp3', 'sample_rate': 24000, 'speech_rate': speech_rate}}}
    return headers, body


def hide(text, cfg):
    for key in ('api_key', 'access_key', 'app_id'):
        if cfg.get(key):
            text = text.replace(cfg[key], '[已隐藏]')
    return text[:500]


# ---- WebSocket 双向流式（只用标准库） ----
EV_START_CONN, EV_FINISH_CONN, EV_CONN_STARTED, EV_CONN_FAILED, EV_CONN_FINISHED = 1, 2, 50, 51, 52
EV_START_SESSION, EV_FINISH_SESSION, EV_SESSION_STARTED, EV_SESSION_FINISHED, EV_SESSION_FAILED = 100, 102, 150, 152, 153
EV_TASK, EV_AUDIO, EV_SUBTITLE = 200, 352, 364   # 364：逐字时间（开 enable_subtitle 时返回）
CONN_EVENTS = (EV_START_CONN, EV_FINISH_CONN, EV_CONN_STARTED, EV_CONN_FAILED, EV_CONN_FINISHED)


def pack_frame(event, payload, sid=None):
    """头4字节：版本1/头长1、完整客户端请求带事件号、JSON不压缩；之后 事件号、[会话ID]、载荷。"""
    out = bytes([0x11, 0x14, 0x10, 0x00]) + struct.pack('>I', event)
    if sid is not None:
        out += struct.pack('>I', len(sid.encode())) + sid.encode()
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    return out + struct.pack('>I', len(data)) + data


def parse_frame(buf):
    """返回 (事件号, 载荷字节, 是否音频, 错误码)。"""
    ptr, msg_type, flags, serial = (buf[0] & 0x0F) * 4, buf[1] >> 4, buf[1] & 0x0F, buf[2] >> 4
    if msg_type == 0b1111:                                    # 错误帧：错误码＋载荷
        code = struct.unpack('>I', buf[ptr:ptr + 4])[0]
        size = struct.unpack('>I', buf[ptr + 4:ptr + 8])[0]
        return None, buf[ptr + 8:ptr + 8 + size], False, code
    event = None
    if flags & 0b0100:
        event = struct.unpack('>i', buf[ptr:ptr + 4])[0]
        ptr += 4
        if event not in (EV_START_CONN, EV_FINISH_CONN):      # 连接事件带连接ID，其余带会话ID
            n = struct.unpack('>I', buf[ptr:ptr + 4])[0]
            ptr += 4 + n
    size = struct.unpack('>I', buf[ptr:ptr + 4])[0]
    return event, buf[ptr + 4:ptr + 4 + size], serial == 0 and msg_type != 0b1001, None


class WebSocket:
    def __init__(self, url, headers, timeout=120):
        scheme, rest = url.split('://', 1)
        host, _, path = rest.partition('/')
        port = 443 if scheme in ('wss', 'https') else 80
        if ':' in host:
            host, port = host.rsplit(':', 1)
        sock = socket.create_connection((host, int(port)), timeout=timeout)
        if scheme in ('wss', 'https'):
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        self.sock, self.buf = sock, b''
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [f'GET /{path} HTTP/1.1', f'Host: {host}', 'Upgrade: websocket', 'Connection: Upgrade',
                 f'Sec-WebSocket-Key: {key}', 'Sec-WebSocket-Version: 13'] + [f'{k}: {v}' for k, v in headers.items()]
        sock.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode())
        while b'\r\n\r\n' not in self.buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError('握手时连接被关闭')
            self.buf += chunk
        head, self.buf = self.buf.split(b'\r\n\r\n', 1)
        status = head.split(b'\r\n', 1)[0].decode()
        if ' 101 ' not in status + ' ':
            body = self.buf
            sock.settimeout(5)
            try:
                while len(body) < 2000:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    body += chunk
            except OSError:
                pass
            raise PermissionError(f'{status.split(" ", 1)[-1]}：{body.decode("utf-8", "replace")}')

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError('连接被服务器关闭')
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, data, opcode=0x2):
        mask = os.urandom(4)
        n = len(data)
        head = bytes([0x80 | opcode])
        head += bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack('>H', n) if n < 65536 \
            else bytes([0x80 | 127]) + struct.pack('>Q', n)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self):
        message = b''
        while True:
            b1, b2 = self._read(2)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack('>H', self._read(2))[0]
            elif n == 127:
                n = struct.unpack('>Q', self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            opcode = b1 & 0x0F
            if opcode == 0x8:
                raise ConnectionError('服务器关闭了连接：' + data[2:].decode('utf-8', 'replace'))
            if opcode == 0x9:
                self.send(data, opcode=0xA)
                continue
            if opcode == 0xA:
                continue
            message += data
            if b1 & 0x80:
                return message

    def close(self):
        try:
            self.send(b'', opcode=0x8)
        except OSError:
            pass
        self.sock.close()


def collect_words(payload, words):
    """事件 364 载荷：{"text": 整句, "words": [{"word": "粉，", "startTime": 1.955, "endTime": 2.665}, ...]}，单位秒。"""
    try:
        data = json.loads(payload or b'{}')
    except ValueError:
        return
    for w in data.get('words') or []:
        words.append({'text': w.get('word', ''), 'start': float(w.get('startTime', 0)), 'end': float(w.get('endTime', 0))})


def synthesize_ws(cfg, headers, body, words=None):
    """words 给了列表时请求逐字时间并追加进去。"""
    hdr = {k: v for k, v in headers.items() if k not in ('Content-Type', 'X-Api-Request-Id')}
    hdr['X-Api-Connect-Id'] = str(uuid.uuid4())
    ws_url = BASE.replace('https://', 'wss://').replace('http://', 'ws://') + WS_PATH
    try:
        ws = WebSocket(ws_url, hdr)
    except PermissionError as exc:
        detail = hide(str(exc), cfg)
        raise SystemExit(f'火山接口拒绝连接 {detail}' + (NOT_GRANTED_HINT if 'not granted' in detail else ''))
    except OSError as exc:
        raise SystemExit(f'连不上火山接口：{exc}')
    sid, req = str(uuid.uuid4()), body['req_params']
    chunks = []

    def expect(*wanted):
        while True:
            event, payload, audio, err = parse_frame(ws.recv())
            if err is not None:
                raise SystemExit(f'火山合成失败：{err} {hide(payload.decode("utf-8", "replace"), cfg)}')
            if event in (EV_CONN_FAILED, EV_SESSION_FAILED):
                raise SystemExit(f'火山合成失败：{hide(payload.decode("utf-8", "replace"), cfg)}')
            if audio and payload:
                chunks.append(payload)
            elif event == EV_SUBTITLE and words is not None:
                collect_words(payload, words)
            if event in wanted:
                return payload

    try:
        ws.send(pack_frame(EV_START_CONN, {}))
        expect(EV_CONN_STARTED)
        audio_params = dict(req['audio_params'])
        if words is not None:   # 2026-10-26 实测两个一起开时返回事件 364
            audio_params.update(enable_subtitle=True, enable_timestamp=True)
        params = {'speaker': req['speaker'], 'audio_params': audio_params}
        ws.send(pack_frame(EV_START_SESSION, {'user': body['user'], 'event': EV_START_SESSION,
                                              'namespace': 'BidirectionalTTS', 'req_params': params}, sid))
        expect(EV_SESSION_STARTED)
        ws.send(pack_frame(EV_TASK, {'user': body['user'], 'event': EV_TASK, 'namespace': 'BidirectionalTTS',
                                     'req_params': dict(params, text=req['text'])}, sid))
        ws.send(pack_frame(EV_FINISH_SESSION, {}, sid))
        done = expect(EV_SESSION_FINISHED)
        try:
            info = json.loads(done or b'{}')
        except ValueError:
            info = {}
        if info.get('status_code') not in (None, 0, END_CODE):
            raise SystemExit(f'火山合成失败：{info.get("status_code")} {hide(info.get("message", ""), cfg)}')
        ws.send(pack_frame(EV_FINISH_CONN, {}))
    except (OSError, ConnectionError) as exc:
        raise SystemExit(f'火山连接中断：{hide(str(exc), cfg)}')
    finally:
        ws.close()
    if not chunks:
        raise SystemExit('火山接口没有返回音频')
    return b''.join(chunks)


NOT_GRANTED_HINT = ('\n提示：密钥能通过验证，但这个资源没有开通。到火山控制台“豆包语音”里开通语音合成（大模型）并确认音色所属版本；'
                    '同一密钥在 WebSocket 和 HTTP 接口上的授权可能不同，可用 --protocol 换一个试试；'
                    '也可用 --resource-id 换成已开通的资源 ID（seed-tts-2.0／seed-tts-1.0／seed-icl-2.0）。')


def synthesize(cfg, headers, body):
    req = urllib.request.Request(URL, data=json.dumps(body, ensure_ascii=False).encode('utf-8'), headers=headers,
                                 method='POST')
    chunks, failure = [], None
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            for raw in resp:
                line = raw.decode('utf-8').strip()
                if not line:
                    continue
                if line.startswith('data:'):          # 兼容 SSE 写法
                    line = line[5:].strip()
                payload = json.loads(line)
                if isinstance(payload.get('data'), str) and payload['data']:
                    chunks.append(base64.b64decode(payload['data']))
                code = payload.get('code', 0)
                if code not in (0, END_CODE, None):
                    failure = f"{code}: {payload.get('message', '')}"
                if code == END_CODE:
                    break
    except urllib.error.HTTPError as exc:
        detail = hide(exc.read().decode('utf-8', 'replace'), cfg)
        hint = NOT_GRANTED_HINT if 'not granted' in detail else ''   # 45000030：密钥有效，但资源没开通
        raise SystemExit(f'火山接口返回 HTTP {exc.code}：{detail}{hint}')
    except urllib.error.URLError as exc:
        raise SystemExit(f'连不上火山接口：{exc.reason}')
    if failure:
        raise SystemExit(f'火山合成失败：{hide(failure, cfg)}')
    if not chunks:
        raise SystemExit('火山接口没有返回音频')
    return b''.join(chunks)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--text-file', required=True)
    ap.add_argument('--out', required=True, help='.mp3 直接写入；其他后缀（如 .wav）用 FFmpeg 转换')
    ap.add_argument('--voice', help='音色 speaker；也可用环境变量 VOLC_TTS_VOICE 或配置文件')
    ap.add_argument('--resource-id', help='资源 ID，如 seed-tts-2.0、seed-tts-1.0、seed-icl-2.0；默认按音色推断')
    ap.add_argument('--speech-rate', type=int, help='语速 -50～100，0 为正常（100≈2倍速，-50≈0.5倍速）')
    ap.add_argument('--protocol', choices=('ws', 'http'), help='默认 ws（WebSocket 双向流式）；也可在配置里写 "protocol"')
    ap.add_argument('--words-out', help='逐字时间写到这个 JSON：[{"text","start","end"}]（秒）；仅 WebSocket 方式支持')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    cfg = load_config()
    voice = a.voice or cfg.get('voice')
    if not voice:
        raise SystemExit('没有音色：用 --voice 指定，或设置 VOLC_TTS_VOICE')
    rate = a.speech_rate if a.speech_rate is not None else int(cfg.get('speech_rate', 0))
    if not -50 <= rate <= 100:
        raise SystemExit('--speech-rate 取值 -50～100')
    text = Path(a.text_file).read_text(encoding='utf-8').strip()
    if not text:
        raise SystemExit('文案为空')
    headers, body = build_request(cfg, text, voice, resource_for(voice, a.resource_id or cfg.get('resource_id')), rate)
    protocol = a.protocol or cfg.get('protocol', 'ws')
    if a.dry_run:
        shown = {k: ('[已隐藏]' if k in ('X-Api-Key', 'X-Api-Access-Key') else v) for k, v in headers.items()}
        url = URL if protocol == 'http' else BASE.replace('https://', 'wss://') + WS_PATH
        print(json.dumps({'url': url, 'headers': shown, 'body': body}, ensure_ascii=False, indent=1))
        return
    if not ('X-Api-Key' in headers or 'X-Api-Access-Key' in headers):
        raise SystemExit('没有火山密钥：设置 VOLC_TTS_API_KEY（新版控制台），或 VOLC_TTS_APP_ID＋VOLC_TTS_ACCESS_KEY（旧版）')
    words = [] if a.words_out and protocol != 'http' else None
    if a.words_out and protocol == 'http':
        print('HTTP 方式不返回逐字时间，不写 --words-out；字幕会按字数估算', file=sys.stderr)
    audio = synthesize(cfg, headers, body) if protocol == 'http' else synthesize_ws(cfg, headers, body, words)
    if words:
        Path(a.words_out).write_text(json.dumps(words, ensure_ascii=False) + '\n', encoding='utf-8')
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix.lower() == '.mp3':
        out.write_bytes(audio)
    else:
        with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as tmp:
            tmp.write(audio)
        try:
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', tmp.name, str(out)], check=True)
        finally:
            os.unlink(tmp.name)
    print(f'已生成 {out}（{len(audio)} 字节 mp3 数据，音色 {voice}）', file=sys.stderr)


if __name__ == '__main__':
    main()
