import { spawnSync } from 'node:child_process'
import { mkdtempSync, rmSync, mkdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { resolve, join } from 'node:path'
import assert from 'node:assert/strict'

// Use an isolated official profile: no existing Harness settings or study data.
const temporary = mkdtempSync(join(tmpdir(), 'study-plugin-install-'))
const home = join(temporary, 'harness')
const project = join(temporary, 'project')
mkdirSync(project)
const cli = resolve('node_modules/@deepseek-ai/dsh/lib/bin.js')
const profile = 'study-plugin-check'
function run(args, cwd = process.cwd()) {
  const result = spawnSync(process.execPath, [cli, ...args], {
    cwd, env: { ...process.env, DSH_HOME: home }, input: '',
    encoding: 'utf8', timeout: 120000, maxBuffer: 4 * 1024 * 1024,
  })
  if (result.error || result.status !== 0) throw new Error(result.error?.message ?? result.stderr + result.stdout)
  return result.stdout
}
try {
  const archive = resolve(process.argv[2] ?? 'dist/study-agent-harness-plugin-0.1.0.tgz')
  run(['--profile', profile, '--from-default-profile', 'sdk-minimal', '--dump-config'])
  run(['plugin', '--profile', profile, 'add', archive])
  assert.match(run(['--profile', profile, '--dump-config']), /study-projects/)
  run(['--profile', profile], project)
  console.log('官方 Harness：安装包、profile 配置和实际启动通过。')
} finally {
  rmSync(temporary, { recursive: true, force: true })
}
