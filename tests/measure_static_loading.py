"""Measure cold browser loads of the generated static site, without refreshing data.

Examples:
  python tests/measure_static_loading.py
  python tests/measure_static_loading.py --profile all --runs 3
  python tests/measure_static_loading.py --url https://example.com/dbml/ --profile network

Requires Chrome/Chromium; uses only the Python standard library. JSON results go
under ignored data/reports/. Network simulation is a laboratory comparison, not
a prediction of AWS latency or real-user Core Web Vitals.
"""
from __future__ import annotations

import argparse
import functools
import gzip
import http.server
import json
from pathlib import Path
import statistics
import struct
import subprocess
import tempfile
import threading
import time
from urllib.parse import urljoin
from urllib.request import urlopen

from run_browser_checks import DevTools, ROOT, browser_path


class Client(DevTools):
    def command(self, method, params=None):
        self.sequence += 1
        self.send(json.dumps({'id': self.sequence, 'method': method, 'params': params or {}}).encode())
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
                return message.get('result', {})

    def evaluate(self, expression):
        result = self.command('Runtime.evaluate', {'expression': expression, 'returnByValue': True})
        if result.get('exceptionDetails'):
            raise RuntimeError(str(result['exceptionDetails']))
        return result.get('result', {}).get('value')


class Handler(http.server.SimpleHTTPRequestHandler):
    compressed = {}
    use_gzip = False

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = Path(self.translate_path(self.path))
        body = self.compressed.get(path)
        if self.use_gzip and body is not None and 'gzip' in self.headers.get('Accept-Encoding', ''):
            self.send_response(200)
            self.send_header('Content-Type', self.guess_type(str(path)))
            self.send_header('Content-Encoding', 'gzip')
            self.send_header('Vary', 'Accept-Encoding')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()


OBSERVER = r"""(() => {
  const m = window.__loadMeasurement = {readyMs: null, lcpMs: null, cls: 0, longTaskMs: 0, errors: []};
  addEventListener('error', e => {
    if(e.target?.src || e.target?.href) m.errors.push(e.target.src || e.target.href);
    else if(e.message) m.errors.push(e.message);
  }, true);
  addEventListener('unhandledrejection', e => m.errors.push(String(e.reason)));
  for (const type of ['largest-contentful-paint', 'layout-shift', 'longtask']) {
    try { new PerformanceObserver(list => list.getEntries().forEach(e => {
      if(type === 'largest-contentful-paint') m.lcpMs = e.startTime;
      if(type === 'layout-shift' && !e.hadRecentInput) m.cls += e.value;
      if(type === 'longtask') m.longTaskMs += e.duration;
    })).observe({type, buffered: true}); } catch {}
  }
  function ready() {
    const page = location.pathname.split('/').pop() || 'index.html';
    const populated = page === 'index.html'
      ? /\d/.test(document.querySelector('[data-home-total]')?.textContent || '') &&
        document.querySelector('[data-home-trend-chart] svg:not(.loading-preview-svg)') && document.querySelector('[data-home-new-chart] svg:not(.loading-preview-svg)')
      : page === 'people.html'
        ? document.querySelector('#people-body tr[data-person-id]') && document.querySelector('#page-summary')?.textContent
        : document.querySelector('#global-timeline .publication-line-chart') &&
          document.querySelector('#overlap-matrix [data-overlap-row]');
    if (populated) m.readyMs = performance.now();
    else requestAnimationFrame(ready);
  }
  requestAnimationFrame(ready);
})();"""

READ_METRICS = r"""(() => {
  const m = window.__loadMeasurement;
  if(!m || m.readyMs === null || document.readyState !== 'complete') return null;
  const nav = performance.getEntriesByType('navigation')[0];
  const resources = performance.getEntriesByType('resource');
  const all = [nav, ...resources];
  return {...m,
    ttfbMs: nav.responseStart, domContentLoadedMs: nav.domContentLoadedEventEnd,
    loadMs: nav.loadEventEnd,
    fcpMs: performance.getEntriesByName('first-contentful-paint')[0]?.startTime ?? null,
    transferBytes: all.reduce((s,e) => s + e.transferSize, 0),
    encodedBodyBytes: all.reduce((s,e) => s + e.encodedBodySize, 0),
    decodedBodyBytes: all.reduce((s,e) => s + e.decodedBodySize, 0),
    resources: resources.map(e => ({path: new URL(e.name).pathname,
      encodedBytes: e.encodedBodySize, decodedBytes: e.decodedBodySize,
      durationMs: e.duration, status: e.responseStatus}))};
})()"""


def measure(browser, base, page, profile, timeout):
    with tempfile.TemporaryDirectory(prefix='dbml-load-', ignore_cleanup_errors=True) as directory:
        process = subprocess.Popen([browser, '--headless', '--disable-gpu', '--no-first-run',
            '--no-default-browser-check', '--remote-debugging-port=0', '--user-data-dir=' + directory,
            '--window-size=1440,1100', 'about:blank'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        client = None
        try:
            deadline = time.monotonic() + timeout
            port_file = Path(directory) / 'DevToolsActivePort'
            while not port_file.exists():
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError('Chrome did not start')
                time.sleep(0.1)
            port = int(port_file.read_text().splitlines()[0])
            with urlopen(f'http://127.0.0.1:{port}/json', timeout=10) as response:
                tab = next(t for t in json.load(response) if t['type'] == 'page')
            client = Client(port, tab['webSocketDebuggerUrl'].split(f':{port}', 1)[1])
            client.socket.settimeout(30)
            client.command('Page.enable')
            client.command('Network.enable')
            client.command('Network.setCacheDisabled', {'cacheDisabled': True})
            if profile != 'local':
                client.command('Network.emulateNetworkConditions', {
                    'offline': False, 'latency': 80, 'downloadThroughput': 10_000_000 / 8,
                    'uploadThroughput': 2_000_000 / 8, 'connectionType': 'cellular4g'})
                client.command('Emulation.setCPUThrottlingRate', {'rate': 4})
            client.command('Page.addScriptToEvaluateOnNewDocument', {'source': OBSERVER})
            client.command('Page.navigate', {'url': urljoin(base, page)})
            # Include another second after readiness to capture final paints and layout shifts.
            ready_since = None
            while time.monotonic() < deadline:
                try:
                    result = client.evaluate(READ_METRICS)
                except RuntimeError as error:
                    if 'context' not in str(error).lower():
                        raise
                    result = None
                if result:
                    ready_since = ready_since or time.monotonic()
                    if time.monotonic() - ready_since >= 1:
                        return result
                time.sleep(0.1)
            raise RuntimeError(f'{page} did not become ready: {client.evaluate("JSON.stringify(window.__loadMeasurement)")}')
        finally:
            if client:
                client.socket.close()
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=['local', 'network', 'network-gzip', 'all'], default='local')
    parser.add_argument('--runs', type=int, default=3)
    parser.add_argument('--browser')
    parser.add_argument('--url', help='Existing static deployment base URL, including any /dbml/ prefix.')
    parser.add_argument('--timeout', type=float, default=90)
    parser.add_argument('--output', type=Path, default=ROOT / 'data/reports/static_loading.json')
    args = parser.parse_args()
    if args.runs < 1:
        parser.error('--runs must be positive')
    if args.url and (not args.url.startswith(('http://', 'https://')) or args.profile in {'all', 'network-gzip'}):
        parser.error('Remote URLs require http(s) and --profile local or network; compression is controlled by the real host.')
    browser = browser_path(args.browser)
    profiles = ['local', 'network', 'network-gzip'] if args.profile == 'all' else [args.profile]
    server = None
    if args.url:
        base = args.url.rstrip('/') + '/'
    else:
        site = ROOT / 'website' / 'deploy'
        # Precompress initial assets before timing. No generated files are changed.
        for path in [*site.glob('*.html'), *site.joinpath('assets').glob('*.js'),
                     *site.joinpath('assets').glob('*.css'), site / 'data/site_data.js',
                     site / 'data/matrix_stats.js', site / 'data/home_summary.json']:
            Handler.compressed[path.resolve()] = gzip.compress(path.read_bytes(), compresslevel=6, mtime=0)
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(site)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}/'
    report = {'baseUrl': base, 'timestampUtc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'conditions': 'Cold cache, new Chrome profile per run; 1440x1100 desktop viewport. '
                  'network: 10 Mbps down, 80 ms latency, 4x CPU slowdown. '
                  'network-gzip: same conditions with precompressed local responses, including files over 10 MB. '
                  'No DNS/TLS/CDN emulation, scrolling or interactions; LCP/CLS are early laboratory observations. '
                  'Ready means Home counts + both trend charts; People rows + pagination; Insights timeline + overlap matrix.',
              'results': []}
    print('Profile       Page          Ready    LCP      Load     Body MB', flush=True)
    try:
        for profile in profiles:
            Handler.use_gzip = profile == 'network-gzip'
            for page in ['index.html', 'people.html', 'all.html']:
                samples = [measure(browser, base, page, profile, args.timeout) for _ in range(args.runs)]
                keys = ['readyMs', 'lcpMs', 'loadMs', 'fcpMs', 'domContentLoadedMs',
                        'ttfbMs', 'encodedBodyBytes', 'decodedBodyBytes', 'transferBytes', 'cls', 'longTaskMs']
                medians = {k: statistics.median(s[k] for s in samples if s[k] is not None)
                           if any(s[k] is not None for s in samples) else None for k in keys}
                report['results'].append({'profile': profile, 'page': page, 'median': medians, 'samples': samples})
                lcp = f'{medians["lcpMs"]/1000:.2f}s' if medians['lcpMs'] is not None else 'n/a'
                print(f'{profile:13} {page:13} {medians["readyMs"]/1000:5.2f}s   {lcp:7} '
                      f'{medians["loadMs"]/1000:5.2f}s   {medians["encodedBodyBytes"]/1_000_000:6.2f}', flush=True)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        print(f'Results: {args.output}')
        warnings = sorted({f'{r.get("status")} {r["path"]}'
                           for result in report['results'] for s in result['samples']
                           for r in s['resources'] if r.get('status', 0) >= 400})
        for warning in warnings:
            print(f'Resource warning: {warning}')
        return 1 if any(s['errors'] or any(r.get('status', 0) >= 400 and r['path'] != '/favicon.ico' for r in s['resources'])
                        for result in report['results'] for s in result['samples']) else 0
    finally:
        if server:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    raise SystemExit(main())
