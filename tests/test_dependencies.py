from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'packages/agent'))
import plugin_dependencies as dependencies


class DependencyTests(unittest.TestCase):
    def test_existing_versions_are_reused_without_installation_or_lock(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(sys, 'prefix', directory):
            with patch.object(dependencies.importlib, 'import_module'), patch.object(dependencies.metadata, 'version', return_value='existing-host-version'), patch.object(dependencies, 'run_command') as command:
                result=dependencies.execute('install')
                self.assertEqual(result['installed'], [])
                self.assertTrue(all(item['version']=='existing-host-version' for item in result['packages']))
                command.assert_not_called()
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_only_missing_packages_are_sent_to_current_interpreter_pip(self):
        before={'python':sys.executable,'packages':[{'name':'pillow','available':True,'version':'11.2.1'}, {'name':'tzdata','available':False,'install':'tzdata==2025.2'}], 'missing':['tzdata']}
        after=dict(before,missing=[],packages=[{'name':'pillow','available':True,'version':'11.2.1'},{'name':'tzdata','available':True,'version':'2025.2'}])
        with tempfile.TemporaryDirectory() as directory, patch.object(sys,'prefix',directory):
            with patch.object(dependencies,'inspect',side_effect=[before,before,after]), patch.object(dependencies.util,'find_spec',return_value=object()), patch.object(dependencies,'run_command') as command:
                result=dependencies.execute('install')
                args=command.call_args.args[0]
                self.assertEqual(args[:3],['-m','pip','install'])
                self.assertEqual(args[-1],'tzdata==2025.2')
                self.assertNotIn('pillow==11.3.0',args)
                self.assertNotIn('--upgrade',args)
                self.assertEqual(result['installed'],['tzdata'])

    def test_check_is_readonly_and_does_not_install_missing_packages(self):
        with patch.object(dependencies,'inspect',return_value={'missing':['tzdata']}), patch.object(dependencies,'run_command') as command:
            self.assertEqual(dependencies.execute('check')['missing'],['tzdata'])
            command.assert_not_called()

    def test_installation_failure_and_broken_existing_package_are_reported(self):
        before={'packages':[{'name':'tzdata','available':False,'install':'tzdata==2025.2'}],'missing':['tzdata']}
        with tempfile.TemporaryDirectory() as directory, patch.object(sys,'prefix',directory):
            with patch.object(dependencies,'inspect',return_value=before), patch.object(dependencies.util,'find_spec',return_value=object()), patch.object(dependencies,'run_command',side_effect=ValueError('offline')):
                with self.assertRaisesRegex(ValueError,'offline'): dependencies.execute('install')
        with patch.object(dependencies.importlib,'import_module',side_effect=RuntimeError('broken binary')):
            with self.assertRaisesRegex(ValueError,'已存在但无法加载'): dependencies.execute('check')


if __name__=='__main__':unittest.main()
