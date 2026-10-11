#!/usr/bin/env node
import { readFileSync, existsSync, realpathSync } from 'node:fs'
import { writeFile, rename } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { BINDING_FILE, projectFor } from './projects.ts'
const [action, directory = '.', extra] = process.argv.slice(2)
try {
  const root = realpathSync(resolve(directory))
  const file = resolve(root,BINDING_FILE)
  if (action === 'bind' || action === 'unbind') {
    const previous = existsSync(file) ? JSON.parse(readFileSync(file,'utf8')) : { version: 1, data: '.' }
    const temporary = `${file}.${randomUUID()}.tmp`
    await writeFile(temporary,JSON.stringify({ ...previous, version: 1, enabled: action === 'bind' },null,2)+'\n',{mode:0o600,flag:'wx'})
    await rename(temporary,file)
    projectFor(root)
    console.log(action === 'bind' ? `已绑定学习项目：${root}` : `已停用学习项目：${root}`)
  } else if (action === 'status') {
    console.log(JSON.stringify({ project: projectFor(root) ?? null },null,2))
  } else if (action === 'doctor') {
    const python = extra ?? 'python3'
    const script = fileURLToPath(new URL('../packages/agent/plugin_doctor.py',import.meta.url))
    const result = spawnSync(python,[script],{stdio:'inherit'})
    if (result.error) throw result.error
    process.exitCode = result.status ?? 1
  } else {
    console.log('study-plugin bind|unbind|status PROJECT\nstudy-plugin doctor PROJECT [PYTHON]\n安装插件后，绑定项目中的新对话自动启用学习工具。')
    process.exitCode = action === undefined || action === '--help' ? 0 : 1
  }
} catch (error) { console.error(String(error)); process.exitCode = 1 }
