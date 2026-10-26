"""火山配音脚本：用本地假接口检查请求格式、分片拼接、出错处理和密钥不外泄。"""
import base64
import hashlib
import json
import os
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'tts_volcengine.py'
SUBTITLE = json.loads((Path(__file__).parent / 'fixtures/volc_subtitle_364.json').read_text(encoding='utf-8'))


class FakeVolc(BaseHTTPRequestHandler):
    audio, seen, fail = b'', [], False

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        FakeVolc.seen.append((dict(self.headers), body))
        self.send_response(200)
        self.end_headers()
        if FakeVolc.fail:
            self.wfile.write(json.dumps({'code': 45000001, 'message': 'speaker not found'}).encode() + b'\n')
            return
        half = len(FakeVolc.audio) // 2
        for part in (FakeVolc.audio[:half], FakeVolc.audio[half:]):
            self.wfile.write(json.dumps({'code': 0, 'data': base64.b64encode(part).decode()}).encode() + b'\n')
        self.wfile.write(json.dumps({'code': 20000000, 'message': 'OK', 'data': None}).encode() + b'\n')

    def log_message(self, *args):
        pass


def ws_send(sock, data):
    n = len(data)
    head = bytes([0x82, n]) if n < 126 else bytes([0x82, 126]) + struct.pack('>H', n) if n < 65536 \
        else bytes([0x82, 127]) + struct.pack('>Q', n)
    sock.sendall(head + data)


def ws_recv(f):
    b1, b2 = f.read(2)
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack('>H', f.read(2))[0]
    elif n == 127:
        n = struct.unpack('>Q', f.read(8))[0]
    mask, data = f.read(4), f.read(n)
    return b1 & 0x0F, bytes(b ^ mask[i % 4] for i, b in enumerate(data))


def server_frame(event, ident, payload, audio=False):
    head = bytes([0x11, 0xB4 if audio else 0x94, 0x00 if audio else 0x10, 0])
    body = payload if audio else json.dumps(payload).encode()
    return head + struct.pack('>i', event) + struct.pack('>I', len(ident)) + ident.encode() \
        + struct.pack('>I', len(body)) + body


class FakeVolcWS(socketserver.StreamRequestHandler):
    """按豆包 V3 双向流式协议应答：连接→会话→收文本→两段音频→会话结束。"""
    audio, seen, deny = b'', [], False

    def handle(self):
        lines = []
        while True:
            line = self.rfile.readline().decode().strip()
            if not line:
                break
            lines.append(line)
        headers = dict(l.split(': ', 1) for l in lines[1:])
        FakeVolcWS.seen.append({'headers': headers, 'frames': []})
        if FakeVolcWS.deny:
            body = b'{"error":"[resource_id=volc.seedtts.default] requested resource not granted"}'
            self.wfile.write(b'HTTP/1.1 403 Forbidden\r\nContent-Length: %d\r\n\r\n' % len(body) + body)
            return
        accept = base64.b64encode(hashlib.sha1((headers['Sec-WebSocket-Key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11')
                                               .encode()).digest()).decode()
        self.wfile.write(f'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                         f'Sec-WebSocket-Accept: {accept}\r\n\r\n'.encode())
        while True:
            opcode, data = ws_recv(self.rfile)
            if opcode == 0x8:
                return
            event = struct.unpack('>i', data[4:8])[0]
            ptr, sid = 8, ''
            if event >= 100:
                n = struct.unpack('>I', data[8:12])[0]
                sid, ptr = data[12:12 + n].decode(), 12 + n
            size = struct.unpack('>I', data[ptr:ptr + 4])[0]
            FakeVolcWS.seen[-1]['frames'].append((event, json.loads(data[ptr + 4:ptr + 4 + size])))
            if event == 1:
                ws_send(self.connection, server_frame(50, 'conn1', {}))
            elif event == 100:
                ws_send(self.connection, server_frame(150, sid, {}))
            elif event == 102:
                half = len(FakeVolcWS.audio) // 2
                for part in (FakeVolcWS.audio[:half], FakeVolcWS.audio[half:]):
                    ws_send(self.connection, server_frame(352, sid, part, audio=True))
                task = dict(FakeVolcWS.seen[-1]['frames']).get(200, {})
                if task.get('req_params', {}).get('audio_params', {}).get('enable_subtitle'):
                    ws_send(self.connection, server_frame(364, sid, SUBTITLE['payload']))
                ws_send(self.connection, server_frame(152, sid, {'status_code': 20000000, 'message': 'ok'}))


class VolcTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name)
        mp3 = cls.base / 'src.mp3'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'sine=d=1', str(mp3)], check=True)
        FakeVolc.audio = FakeVolcWS.audio = mp3.read_bytes()
        cls.server = HTTPServer(('127.0.0.1', 0), FakeVolc)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.ws = socketserver.ThreadingTCPServer(('127.0.0.1', 0), FakeVolcWS)
        cls.ws.daemon_threads = True
        threading.Thread(target=cls.ws.serve_forever, daemon=True).start()
        (cls.base / 't.txt').write_text('姜和葱分开煸。', encoding='utf-8')

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.ws.shutdown()
        cls.ws.server_close()
        cls.tmp.cleanup()

    def run_tts(self, *args, ws=False, **env):
        e = {k: v for k, v in os.environ.items() if not k.startswith('VOLC_')}
        port = self.ws.server_address[1] if ws else self.server.server_port
        e.update({'VOLC_TTS_BASE_URL': f'http://127.0.0.1:{port}', 'HOME': str(self.base)}, **env)
        if not ws:
            args = (*args, '--protocol', 'http')
        return subprocess.run([sys.executable, str(SCRIPT), '--text-file', str(self.base / 't.txt'), *args],
                              capture_output=True, text=True, env=e)

    def test_chunks_are_joined_and_converted_to_wav(self):
        FakeVolc.fail = False
        out = self.base / 'o.wav'
        res = self.run_tts('--out', str(out), '--voice', 'zh_female_vv_uranus_bigtts', VOLC_TTS_API_KEY='secret-key')
        self.assertEqual(res.returncode, 0, res.stderr)
        dur = float(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0',
                                    str(out)], capture_output=True, text=True).stdout)
        self.assertAlmostEqual(dur, 1.0, delta=0.15)
        headers, body = FakeVolc.seen[-1]
        self.assertEqual(headers['X-Api-Resource-Id'], 'seed-tts-2.0')
        self.assertEqual(body['req_params']['speaker'], 'zh_female_vv_uranus_bigtts')
        self.assertEqual(body['req_params']['text'], '姜和葱分开煸。')

    def test_service_error_stops_without_leaking_key(self):
        FakeVolc.fail = True
        res = self.run_tts('--out', str(self.base / 'x.mp3'), '--voice', 'icl_abc', VOLC_TTS_API_KEY='secret-key')
        FakeVolc.fail = False
        self.assertNotEqual(res.returncode, 0)
        self.assertIn('speaker not found', res.stderr)
        self.assertNotIn('secret-key', res.stderr + res.stdout)
        self.assertEqual(FakeVolc.seen[-1][0]['X-Api-Resource-Id'], 'seed-icl-2.0')

    def test_missing_key_and_dry_run_hide_secrets(self):
        res = self.run_tts('--out', str(self.base / 'y.mp3'), '--voice', 'zh_male_x_bigtts')
        self.assertIn('没有火山密钥', res.stderr)
        res = self.run_tts('--out', str(self.base / 'y.mp3'), '--voice', 'zh_male_x_bigtts', '--dry-run',
                           VOLC_TTS_APP_ID='app1', VOLC_TTS_ACCESS_KEY='secret-ak')
        self.assertEqual(res.returncode, 0)
        self.assertNotIn('secret-ak', res.stdout)
        self.assertIn('seed-tts-1.0', res.stdout)

    def test_websocket_default_joins_audio_frames(self):
        FakeVolcWS.deny = False
        out = self.base / 'w.wav'
        res = self.run_tts('--out', str(out), '--voice', 'zh_male_sunwukong_uranus_bigtts', '--speech-rate', '10',
                           ws=True, VOLC_TTS_API_KEY='secret-key')
        self.assertEqual(res.returncode, 0, res.stderr)
        dur = float(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0',
                                    str(out)], capture_output=True, text=True).stdout)
        self.assertAlmostEqual(dur, 1.0, delta=0.15)
        seen = FakeVolcWS.seen[-1]
        self.assertEqual(seen['headers']['X-Api-Resource-Id'], 'seed-tts-2.0')
        self.assertEqual(seen['headers']['X-Api-Key'], 'secret-key')
        self.assertIn('X-Api-Connect-Id', seen['headers'])
        events = [e for e, _ in seen['frames']]
        self.assertEqual(events[:4], [1, 100, 200, 102])
        task = dict(seen['frames'])[200]
        self.assertEqual(task['req_params']['text'], '姜和葱分开煸。')
        self.assertEqual(task['req_params']['speaker'], 'zh_male_sunwukong_uranus_bigtts')
        self.assertEqual(task['req_params']['audio_params']['speech_rate'], 10)

    def test_websocket_words_out_writes_seconds(self):
        FakeVolcWS.deny = False
        words = self.base / 'w.words.json'
        res = self.run_tts('--out', str(self.base / 'w2.mp3'), '--voice', 'zh_male_sunwukong_uranus_bigtts',
                           '--words-out', str(words), ws=True, VOLC_TTS_API_KEY='secret-key')
        self.assertEqual(res.returncode, 0, res.stderr)
        task = dict(FakeVolcWS.seen[-1]['frames'])[200]
        self.assertTrue(task['req_params']['audio_params']['enable_subtitle'])
        data = json.loads(words.read_text(encoding='utf-8'))
        self.assertEqual(len(data), 13)
        self.assertEqual(data[0], {'text': '鸡', 'start': 0, 'end': 0.245})
        self.assertEqual(data[8]['text'], '粉，')
        # 不要逐字时间时不加字幕参数，也不写文件
        self.run_tts('--out', str(self.base / 'w3.mp3'), '--voice', 'zh_male_x_uranus_bigtts', ws=True,
                     VOLC_TTS_API_KEY='secret-key')
        self.assertNotIn('enable_subtitle', dict(FakeVolcWS.seen[-1]['frames'])[200]['req_params']['audio_params'])

    def test_websocket_not_granted_gives_hint_without_key(self):
        FakeVolcWS.deny = True
        res = self.run_tts('--out', str(self.base / 'z.mp3'), '--voice', 'zh_male_x_uranus_bigtts', ws=True,
                           VOLC_TTS_API_KEY='secret-key')
        FakeVolcWS.deny = False
        self.assertNotEqual(res.returncode, 0)
        self.assertIn('not granted', res.stderr)
        self.assertIn('--protocol', res.stderr)
        self.assertNotIn('secret-key', res.stderr + res.stdout)


if __name__ == '__main__':
    unittest.main()
