"""Read-only instrumented presentation harness; does not modify served sources."""
import argparse
import functools
import http.server
import json
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(root / 'tests'))
from run_browser_checks import browser_path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--browser', help='Chrome/Chromium executable, if not detected automatically')
browser = browser_path(parser.parse_args().browser)
test = (root / 'tests/test_all_presentation.js').read_text(encoding='utf-8')
needle = "assert(doc.querySelector('.publication-line-chart').viewBox.baseVal.width === Math.max(240, doc.querySelector('#global-timeline').clientWidth), 'Line layout redraws after resize, including when page size is fixed');"
replacement = """
  const dimensions=()=>({view:doc.querySelector('.publication-line-chart').viewBox.baseVal.width,host:doc.querySelector('#global-timeline').clientWidth,frame:frame.clientWidth,inner:win.innerWidth,visibility:doc.visibilityState,time:win.performance.now()});
  const diagnostics={after250:dimensions()};
  win.dispatchEvent(new win.Event('resize'));
  await new Promise(resolve=>win.setTimeout(resolve,1000));
  diagnostics.after1250=dimensions();
  await new Promise(resolve=>win.requestAnimationFrame(()=>resolve()));
  diagnostics.afterFrame=dimensions();
  window.resizeDiagnostics=diagnostics;
  assert(diagnostics.afterFrame.view===Math.max(240,diagnostics.afterFrame.host),'Instrumented resize '+JSON.stringify(diagnostics));
"""
assert needle in test
test = test.replace(needle, replacement)
html = """<!doctype html><meta charset=utf-8><iframe id=f width=1440 height=1100 style='border:0'></iframe><pre id=result>WAIT</pre><script src=/instrumented.js></script><script>(async()=>{try{const n=await runAllPresentationTests(document.querySelector('#f'),location.origin+'/website/');document.querySelector('#result').textContent=JSON.stringify({status:'PASS',checks:n,resize:window.resizeDiagnostics});}catch(e){document.querySelector('#result').textContent=JSON.stringify({status:'FAIL',error:e.stack,resize:window.resizeDiagnostics});}})()</script>"""

class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_GET(self):
        content = html if self.path == '/probe.html' else test if self.path == '/instrumented.js' else None
        if content is None:
            return super().do_GET()
        payload = content.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8' if self.path.endswith('.html') else 'text/javascript; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(root)))
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    with tempfile.TemporaryDirectory(prefix='dbml-round2-browser-') as profile:
        result = subprocess.run([browser, '--headless', '--disable-gpu', '--no-first-run', '--no-default-browser-check', '--user-data-dir='+profile, '--window-size=1600,1200', '--virtual-time-budget=120000', '--dump-dom', 'http://127.0.0.1:'+str(server.server_port)+'/probe.html'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
        match = re.search(r'<pre id="result">(.*?)</pre>', result.stdout, re.S)
        message = match.group(1) if match else 'No result'
        print(message)
        (root/'data/reports/round2/exports_browser_evidence.json').write_text(message+'\n', encoding='utf-8')
finally:
    server.shutdown()
    server.server_close()
