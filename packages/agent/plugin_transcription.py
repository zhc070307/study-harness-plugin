"""GPU diagnostics before the plugin starts its managed loopback-only host."""
import hashlib
import json
from transcription import NativeTranscription, SOURCE
import sys
sys.path.insert(0, str(SOURCE))
from device import check


class PluginTranscription(NativeTranscription):
    def resources(self):
        if self.host_config(): return self.host_status()['resources']
        result = check(self.shared/'host', False, self.ready())
        if sys.version_info[:2] != (3, 12):
            result.update(eligible=False, message='录音转写需要 Python 3.12，请在插件配置中指定该解释器；没有下载任何组件')
        return result

    def ready(self):
        if self.host_config(): return self.host_status()['installed']
        marker = self.shared/'host/installed.json'
        expected = hashlib.sha256(b''.join((SOURCE/name).read_bytes() for name in ('SOURCE.json','requirements.txt'))+b'False').hexdigest()
        try: return json.loads(marker.read_text(encoding='utf-8'))['runtime'] == expected
        except (OSError, ValueError, KeyError): return False
