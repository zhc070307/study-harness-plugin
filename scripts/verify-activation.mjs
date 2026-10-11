import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { resolve, join } from 'node:path'
import { pathToFileURL } from 'node:url'

// Resolve the SDK from the tested installation, including the stable-version fixture.
const require = createRequire(resolve('package.json'))
const sdk = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-app-boot')))
// FiberState is a TypeScript const enum; ACTIVE is 2 in both supported SDKs.
const FIBER_ACTIVE = 2
const [home, name] = process.argv.slice(2)
const installAnchor = require.resolve('@deepseek-ai/dsh/package.json')
const profile = sdk.loadProfile('study-plugin-check', name, installAnchor, home)
const context = { name, dir: profile.dir, patchPath: profile.patchPath, installAnchor,
  startedBundles: profile.layers.map(layer => layer.packageName), cwd: process.cwd(), home, overlays: [] }
const patches = sdk.readProfilePatches('study-plugin-check', context, profile)
// Keep the real profile services, but prevent stdin EOF from closing the tree
// before the optional plugin has finished importing and activating.
for (const patch of patches) {
  if (!patch.insert) continue
  patch.insert = patch.insert.filter(entry => !['sdk-app-startup', 'sdk-jsonrpc-server'].includes(entry.id))
  for (const entry of patch.insert) if (entry.id === 'study-projects') entry.required = true
}
const resolution = await sdk.createRuntimeResolution({ installAnchor, profile })
let ctx
try {
  ctx = await sdk.boot('study-plugin-check', join(profile.dir, 'cordis.yml'), patches, async host => {
    host.provide('profileContext', context)
    await host.plugin(sdk.PluginPackages, { resolution })
  })
  const entry = [...ctx.loader.entries()].find(entry => entry.options.id === 'study-projects')
  assert.ok(entry?.fiber, '安装包中的 study-projects 必须成功导入')
  await entry.fiber.await()
  assert.equal(entry.fiber.state, FIBER_ACTIVE, 'study-projects 必须实际启用')
  assert.ok(ctx.tools.get('study_project'), '学习插件必须注册 study_project 工具')
  console.log('study-projects 实际启用，study_project 工具已注册。')
} finally {
  await ctx?.fiber.dispose()
}
