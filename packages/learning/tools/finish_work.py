#!/usr/bin/env python3
"""Finish a verified artifact build and move its work directory to Trash."""
import argparse
import json
import sys
from pathlib import Path
program = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(program / 'src'))
from study_agent.workspace import workspace_root
from study_agent.runtime_cleanup import finish_work
parser = argparse.ArgumentParser()
parser.add_argument('work')
parser.add_argument('--workspace')
parser.add_argument('--artifact', required=True)
parser.add_argument('--receipt', required=True)
args = parser.parse_args()
try:
    root = Path(args.workspace).resolve() if args.workspace else workspace_root(Path.cwd())
    print(json.dumps(finish_work(root, root / args.work, root / args.artifact, root / args.receipt)))
except (ValueError, OSError) as exc:
    parser.exit(1, str(exc) + '\n')
