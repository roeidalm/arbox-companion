#!/usr/bin/env python3
"""Bundle the shared feedback UI into the HACS package; --check detects drift."""
import argparse
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--check', action='store_true')
args = parser.parse_args()
for name in ('feedback-form.js', 'feedback.css', 'membership-policy.js', 'membership-ui.js', 'membership-ui.css', 'filter-picker.js'):
    source = root / 'arbox-server' / 'frontend' / name
    target = root / 'custom_components' / 'arbox' / 'frontend' / name
    if args.check:
        if not target.exists() or source.read_bytes() != target.read_bytes():
            print(f'{name} is out of sync; run python3 scripts/sync-feedback-frontend.py', file=sys.stderr)
            sys.exit(1)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
