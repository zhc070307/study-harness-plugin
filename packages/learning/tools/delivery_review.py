#!/usr/bin/env python3
"""Prepare and accept note/worksheet delivery using existing evidence receipts."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from study_agent.workspace import workspace_root
from study_agent.delivery import prepare, accept
parser = argparse.ArgumentParser()
parser.add_argument('--workspace')
sub = parser.add_subparsers(dest='command', required=True)
a = sub.add_parser('prepare')
a.add_argument('work'); a.add_argument('--course', required=True)
a.add_argument('--kind', choices=['note', 'worksheet'], required=True)
a.add_argument('--input', required=True)
a = sub.add_parser('accept')
a.add_argument('work'); a.add_argument('--artifact', required=True)
a.add_argument('--receipt', required=True); a.add_argument('--review', required=True)
args = parser.parse_args()
try:
    root = Path(args.workspace).resolve() if args.workspace else workspace_root(Path.cwd())
    if args.command == 'prepare':
        result = prepare(root, root / args.work, args.course, args.kind, root / args.input)
        print(json.dumps({'prepared': True, 'kind': result['kind'], 'course': result['course']}))
    else:
        print(json.dumps(accept(root, root / args.work, root / args.artifact, root / args.receipt,
                                json.loads((root / args.review).read_text()))))
except (ValueError, OSError, KeyError) as exc:
    parser.exit(1, str(exc) + '\n')
