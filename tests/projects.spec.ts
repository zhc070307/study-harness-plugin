import { afterEach, expect, it } from 'vitest'
import { mkdtempSync, mkdirSync, writeFileSync, symlinkSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { projectFor } from '../src/projects.ts'
const roots: string[] = []
afterEach(() => { for (const root of roots.splice(0)) rmSync(root,{recursive:true,force:true}) })
function directory() { const root=mkdtempSync(join(tmpdir(),'study-binding-'));roots.push(root);return root }
function bind(root: string, data='.') { writeFileSync(join(root,'.study-plugin.json'),JSON.stringify({version:1,enabled:true,data})) }
it('inherits the nearest binding and respects explicit disabling',()=>{
  const root=directory();const child=join(root,'course');mkdirSync(child)
  expect(projectFor(child)).toBeUndefined();bind(root)
  expect(projectFor(child)?.root).toBe(root)
  writeFileSync(join(child,'.study-plugin.json'),JSON.stringify({version:1,enabled:false,data:'.'}))
  expect(projectFor(child)).toBeUndefined()
})
it('rejects traversal and symlink escapes, including missing descendants',()=>{
  const root=directory(),outside=directory();bind(root,'../escape')
  expect(()=>projectFor(root)).toThrow('项目之外')
  symlinkSync(outside,join(root,'linked'),'dir');bind(root,'linked/new-data')
  expect(()=>projectFor(root)).toThrow('符号链接')
})
