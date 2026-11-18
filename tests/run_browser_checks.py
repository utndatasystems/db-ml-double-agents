"""Run the checked-in browser assertions in headless Chrome using real time.

Usage: python tests/run_browser_checks.py [--browser PATH] [--timeout 180]
Requires Chrome or Chromium, no additional Python packages. Serves this checkout
on localhost and never downloads sources or modifies website data.
"""
from __future__ import annotations

import argparse
import base64
import functools
import http.server
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
from urllib.request import urlopen
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
HTML = '''<!doctype html><meta charset="utf-8">
<iframe id="f" width="1440" height="1100" style="border:0"></iframe><pre id="result">WAIT</pre>
<script src="/website/deploy/assets/home-trends.js"></script>
<script src="/website/deploy/assets/db-venue-overlap-model.js"></script>
<script src="/tests/test_home_trends.js"></script>
<script src="/tests/test_db_venue_overlap.js"></script>
<script src="/tests/test_population_focus.js"></script>
<script src="/tests/test_people_navigation.js"></script>
<script src="/tests/test_all_presentation.js"></script>
<script src="/tests/test_all_home_links.js"></script>
<script src="/tests/test_coverage_sankey.js"></script>
<script src="/tests/test_static_loading.js"></script>
<script src="/tests/test_loading_previews.js"></script>
<script src="/tests/test_review_provenance.js"></script>
<script>(async()=>{try{
const f=document.querySelector('#f'),base=location.origin;
const run=async(label,check)=>{
  document.querySelector('#result').textContent='RUN '+label;
  return await check();
};
const models=await run('models',()=>runHomeTrendTests(DBML_HOME_TRENDS)+runDbVenueOverlapTests(DBVenueOverlap));
const p=await run('presentation',()=>runAllPresentationTests(f,base+'/website/deploy/'));
const h=await run('Home links',()=>runAllHomeLinkTests(f,base+'/website/deploy/'));
const n=await run('navigation',()=>runPeopleNavigationTests(f,base+'/website/deploy/'));
const loadInsights=async()=>{
  const previous=f.contentDocument;
  f.style.width='';f.width=1440;
  f.src=base+'/website/deploy/all.html';
  for(let attempt=0;attempt<300;attempt++){
    const doc=f.contentDocument;
    if(doc!==previous&&doc?.readyState==='complete'&&doc.querySelector('#overlap-matrix .overlap-matrix-cell')&&doc.querySelector('#focus-groups .focus-row-action'))return doc;
    await new Promise(resolve=>setTimeout(resolve,30));
  }
  throw Error('All Insights did not become ready for overlap/classification tests');
};
const o=await run('overlap',async()=>runDbVenueOverlapIntegrationTests(await loadInsights()));
const c=await run('classification',async()=>runPopulationFocusTests(await loadInsights()));
const s=await run('Sankey',()=>runCoverageSankeyTests(f,base+'/'));
const l=await run('static loading',()=>runStaticLoadingTests(f,base+'/website/deploy/'));
const v=await run('loading previews',()=>runLoadingPreviewTests(f,base+'/website/deploy/'));
const r=await run('review provenance',()=>runReviewProvenanceTests(f,base+'/website/deploy/'));
document.querySelector('#result').textContent=`PASS ${models} model + ${p} presentation + ${h} Home-link + ${n} navigation + ${o} overlap + ${c} classification + ${s} Sankey + ${l} static-loading + ${v} preview + ${r} review-provenance checks`;
}catch(e){document.querySelector('#result').textContent='FAIL '+e.stack;}})()</script>'''


class Handler(http.server.SimpleHTTPRequestHandler):
    failed_preview_requests = set()
    def log_message(self, *args):
        pass

    def do_GET(self):
        # Fault injection is confined to explicit test-page URLs. Normal page
        # loads and the separate performance server are unaffected.
        page_url = urlsplit(self.path)
        page_mode = parse_qs(page_url.query).get('preview-check', [''])[0]
        if page_mode and page_url.path in {'/website/deploy/index.html', '/website/deploy/people.html', '/website/deploy/all.html'}:
            content = Path(self.translate_path(self.path)).read_text(encoding='utf-8')
            content = content.replace('data/site_data.js?v=', f'data/site_data.js?preview-test={page_mode}&preview-run={time.monotonic_ns()}&v=')
            if 'first-frames=1' in page_url.query:
                content = content.replace('<head>', '<head><script src="/tests/record_loading_frames.js"></script>', 1)
                content = content.replace('assets/loading-preview.js?v=', f'assets/loading-preview.js?frame-run={time.monotonic_ns()}&v=')
            body = content.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        referer = self.headers.get('Referer', '')
        mode = parse_qs(urlsplit(referer).query).get('preview-check', [''])[0]
        if 'first-frames=1' in urlsplit(referer).query and urlsplit(self.path).path == '/website/deploy/assets/loading-preview.js':
            time.sleep(0.6)
        if urlsplit(self.path).path in {'/website/deploy/data/site_data.js', '/website/deploy/data/home_summary.json'}:
            if mode == 'slow':
                time.sleep(1.25)
            elif mode == 'fail' and referer not in self.failed_preview_requests:
                self.failed_preview_requests.add(referer)
                self.send_error(503, 'Expected loading-preview test failure')
                return
        if self.path == '/.browser-checks.html':
            content = HTML.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        else:
            try:
                super().do_GET()
            except ConnectionError:
                # Navigation can cancel an in-flight lazy data request.
                pass


def browser_path(explicit):
    if explicit:
        return str(Path(explicit).resolve())
    for name in ('google-chrome', 'chromium', 'chromium-browser', 'chrome'):
        if found := shutil.which(name):
            return found
    for variable in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
        if location := os.environ.get(variable):
            candidate = Path(location) / 'Google/Chrome/Application/chrome.exe'
            if candidate.is_file():
                return str(candidate)
    raise RuntimeError('Chrome/Chromium not found; supply --browser PATH.')


class DevTools:
    """Minimal localhost WebSocket client for read-only Runtime.evaluate calls."""
    def __init__(self, port, path):
        self.socket = socket.create_connection(('127.0.0.1', port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode('ascii')
        self.socket.sendall((f'GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n'
                             f'Upgrade: websocket\r\nConnection: Upgrade\r\n'
                             f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
        header = b''
        while not header.endswith(b'\r\n\r\n'):
            header += self.read(1)
        if b' 101 ' not in header.split(b'\r\n')[0]:
            self.socket.close()
            raise RuntimeError('DevTools WebSocket handshake failed')
        self.sequence = 0

    def read(self, size):
        result = b''
        while len(result) < size:
            chunk = self.socket.recv(size - len(result))
            if not chunk:
                raise RuntimeError('DevTools connection closed')
            result += chunk
        return result

    def send(self, payload, opcode=1):
        mask = os.urandom(4)
        length = len(payload)
        header = bytes([128 | opcode, 128 | (length if length < 126 else 126 if length < 65536 else 127)])
        if length >= 126:
            header += struct.pack('!H' if length < 65536 else '!Q', length)
        self.socket.sendall(header + mask + bytes(value ^ mask[i % 4] for i, value in enumerate(payload)))

    def evaluate(self, expression):
        self.sequence += 1
        self.send(json.dumps({'id': self.sequence, 'method': 'Runtime.evaluate',
                              'params': {'expression': expression, 'returnByValue': True}}).encode())
        while True:
            first, second = self.read(2)
            length = second & 127
            if length >= 126:
                length = struct.unpack('!H' if length == 126 else '!Q', self.read(2 if length == 126 else 8))[0]
            mask = self.read(4) if second & 128 else None
            payload = self.read(length)
            if mask:
                payload = bytes(value ^ mask[i % 4] for i, value in enumerate(payload))
            if first & 15 == 9:
                self.send(payload, opcode=10)
                continue
            if first & 15 == 8:
                raise RuntimeError('Browser closed DevTools')
            message = json.loads(payload)
            if message.get('id') == self.sequence:
                if 'error' in message:
                    raise RuntimeError(str(message['error']))
                return message.get('result', {}).get('result', {}).get('value', '')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser')
    parser.add_argument('--timeout', type=float, default=180)
    args = parser.parse_args()
    browser = browser_path(args.browser)
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(ROOT)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    process = client = None
    try:
        with tempfile.TemporaryDirectory(prefix='dbml-browser-check-', ignore_cleanup_errors=True) as profile:
            profile_path = Path(profile).resolve()
            assert profile_path.is_relative_to(Path(tempfile.gettempdir()).resolve())
            process = subprocess.Popen([browser, '--headless', '--disable-gpu', '--no-first-run',
                '--no-default-browser-check', '--disable-background-timer-throttling',
                '--disable-renderer-backgrounding', '--disable-backgrounding-occluded-windows',
                '--remote-debugging-port=0', '--user-data-dir=' + profile, '--window-size=1600,1200',
                f'http://127.0.0.1:{server.server_port}/.browser-checks.html'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + args.timeout
                port_file = profile_path / 'DevToolsActivePort'
                while not port_file.exists():
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError('Chrome did not start DevTools')
                    time.sleep(0.1)
                port = int(port_file.read_text(encoding='utf-8').splitlines()[0])
                with urlopen(f'http://127.0.0.1:{port}/json', timeout=10) as response:
                    tabs = json.load(response)
                tab = next(tab for tab in tabs if tab['type'] == 'page')
                client = DevTools(port, tab['webSocketDebuggerUrl'].split(f':{port}', 1)[1])
                result = 'WAIT'
                while time.monotonic() < deadline:
                    result = client.evaluate("document.querySelector('#result')?.textContent || 'WAIT'")
                    if result.startswith(('PASS', 'FAIL')):
                        print(result, flush=True)
                        return 0 if result.startswith('PASS') else 1
                    time.sleep(0.1)
                raise RuntimeError(f'Browser assertions timed out: {result}')
            finally:
                if client:
                    client.socket.close()
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=15)
                assert profile_path.is_relative_to(Path(tempfile.gettempdir()).resolve())
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    raise SystemExit(main())
