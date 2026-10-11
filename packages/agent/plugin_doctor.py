"""Read-only capability check; does not install tools or download models."""
import importlib.util
import json
import shutil
import sys
result = {'python': sys.version.split()[0], 'learning': sys.version_info >= (3,10), 'pdf': all(importlib.util.find_spec(name) for name in ('pypdfium2','PIL')), 'caldav': importlib.util.find_spec('icalendar') is not None, 'latex': all(shutil.which(name) for name in ('latexmk','xelatex')), 'transcription_python': sys.version_info[:2] == (3,12), 'apple_calendar_build': sys.platform == 'darwin' and shutil.which('swiftc') is not None}
print(json.dumps(result,ensure_ascii=False,indent=2))
if not result['learning']: sys.exit(1)
