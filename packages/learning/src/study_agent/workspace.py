"""Locate reusable program files and the selected semester without fixed paths."""
import json
from pathlib import Path


def workspace_config(start):
    start = Path(start).resolve()
    for parent in (start, *start.parents):
        config = parent / '.study-workspace.json'
        if config.is_file():
            data = json.loads(config.read_text(encoding='utf-8'))
            return parent, data
    return None


def workspace_root(start):
    found = workspace_config(start)
    if found is None:
        raise ValueError('No .study-workspace.json found; select a workspace explicitly')
    parent, data = found
    return (parent / data['workspace']).resolve()


def program_root(workspace):
    found = workspace_config(workspace)
    if found:
        parent, data = found
        return (parent / data['program']).resolve()
    # Legacy/test workspaces retain their own tools directory.
    return Path(workspace).resolve() / '.study-system'
