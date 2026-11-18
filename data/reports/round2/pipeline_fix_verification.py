import json
from pathlib import Path
import subprocess
import types

import pipeline_probe as probe

root = Path(__file__).resolve().parents[3]
source = subprocess.run(['git', 'show', '567b35b:scripts/refresh_affiliations.py'], cwd=root,
                        check=True, capture_output=True, text=True, encoding='utf-8').stdout
baseline = types.ModuleType('baseline_refresh_affiliations')
exec(compile(source, '567b35b:scripts/refresh_affiliations.py', 'exec'), baseline.__dict__)
fixed = probe.refresh
try:
    probe.refresh = baseline
    before = probe.fixture()
finally:
    probe.refresh = fixed
after = probe.fixture()
result = {'baseline': before, 'fixed': after,
          'production': probe.production(),
          'interpretation': 'The same fixture restores one cached DOI response; only fixed ordinary offline retry consumes it.'}
target = Path(__file__).with_name('pipeline_fix_verification_rerun.json')
target.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
print(json.dumps({which: {step: len(value['candidates']) for step, value in result[which].items()}
                  for which in ('baseline', 'fixed')}, indent=2))
