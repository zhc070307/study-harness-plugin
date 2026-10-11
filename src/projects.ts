import { readFileSync, realpathSync, existsSync } from 'node:fs'
import { dirname, isAbsolute, relative, resolve } from 'node:path'

export const BINDING_FILE = '.study-plugin.json'
export interface Project {
  root: string
  data: string
  calendar?: { provider: 'caldav' | 'apple'; url?: string; usernameEnv?: string; passwordEnv?: string; executable?: string }
}

export function inside(root: string, target: string): boolean {
  const path = relative(root, target)
  return path === '' || (!path.startsWith('..' + (process.platform === 'win32' ? '\\' : '/')) && path !== '..' && !isAbsolute(path))
}

/** Nearest explicit binding wins. Merely opening a directory never binds it. */
export function projectFor(cwd: string | undefined): Project | undefined {
  if (!cwd || !isAbsolute(cwd)) return
  let root = realpathSync(cwd)
  for (;;) {
    const file = resolve(root, BINDING_FILE)
    if (existsSync(file)) {
      const config = JSON.parse(readFileSync(file, 'utf8'))
      if (config.version !== 1 || typeof config.enabled !== 'boolean') throw new Error('学习项目配置格式无效')
      if (!config.enabled) return
      if (typeof config.data !== 'string' || isAbsolute(config.data)) throw new Error('学习数据目录必须是项目内的相对路径')
      const requested = resolve(root, config.data)
      if (!inside(root, requested)) throw new Error('学习数据目录不能位于项目之外')
      // Resolve every existing ancestor to reject escapes through symlinks, including a missing leaf.
      let ancestor = requested
      while (!existsSync(ancestor)) ancestor = dirname(ancestor)
      if (!inside(root, realpathSync(ancestor))) throw new Error('学习数据目录不能通过符号链接指向项目之外')
      const data = existsSync(requested) ? realpathSync(requested) : requested
      return { root, data, calendar: config.calendar }
    }
    const parent = dirname(root)
    if (parent === root) return
    root = parent
  }
}
