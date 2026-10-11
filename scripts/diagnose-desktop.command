#!/bin/bash
# Read-only import inspection using the Electron application and its package resolver.
set -eu
application=''
for candidate in '/Applications/DeepSeek Harness.app' "$HOME/Applications/DeepSeek Harness.app"; do
  if [ -x "$candidate/Contents/MacOS/DeepSeek Harness" ]; then
    application="$candidate"
    break
  fi
done
if [ -z "$application" ]; then
  echo '没有在 Applications 中找到 DeepSeek Harness.app；请反馈应用实际位置。' >&2
  exit 1
fi
exec env ELECTRON_RUN_AS_NODE=1 "$application/Contents/MacOS/DeepSeek Harness" \
  --expose-internals --input-type=module - "$application/Contents/Resources" <<'JAVASCRIPT'
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { homedir } from 'node:os'
import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'

try {
  const resources = process.argv[2]
  const runtime = ['app.asar/dsh', 'app/dsh', 'dsh'].map(path => join(resources, path))
    .find(path => existsSync(join(path, 'node_modules/@deepseek-ai/dsh/package.json')))
  if (!runtime) throw new Error('找不到桌面应用的内置 Harness runtime；请反馈应用版本和位置。')
  const require = createRequire(join(runtime, 'package.json'))
  const sdk = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-app-boot')))
  const installAnchor = require.resolve('@deepseek-ai/dsh/package.json')
  const dir = process.argv[3] ?? join(process.env.DSH_HOME ?? join(homedir(), '.dsh'), 'profiles/desktop')
  if (!existsSync(join(dir, 'package.json'))) throw new Error('找不到桌面插件配置：' + dir)
  // Construct an inspection profile without calling profile initialization or migration.
  const profile = { name: 'desktop', dir, layers: [], patches: [], skippedBundles: [],
    patchPath: join(dir, 'cordis.patch.yml') }
  console.log('Harness:', JSON.parse(readFileSync(installAnchor, 'utf8')).version)
  console.log('Electron:', process.versions.electron ?? 'test runtime', 'Node:', process.version)
  const dump = await sdk.generateConfigSchema(profile, [[{ insert: [{
    id: 'study-projects', name: '@study-agent/harness-plugin', config: { python: 'auto' },
  }] }]], installAnchor)
  const diagnostics = dump['x-cordis'].diagnostics
  if (!dump['x-cordis'].complete) {
    for (const diagnostic of diagnostics) console.error(diagnostic.level + ': ' + diagnostic.message)
    process.exitCode = 1
  } else {
    console.log('插件模块导入成功。若窗口仍提示失败，请反馈这份输出；需进一步检查运行中的宿主。')
  }
} catch (error) {
  console.error(error instanceof Error ? error.stack : String(error))
  process.exitCode = 1
}
JAVASCRIPT
