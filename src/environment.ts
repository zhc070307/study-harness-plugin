import { existsSync } from 'node:fs'
import { join } from 'node:path'
import { homedir } from 'node:os'
import { fileURLToPath } from 'node:url'
import { jsonProcess } from './process.ts'

const script = fileURLToPath(new URL('../packages/agent/plugin_dependencies.py', import.meta.url))

/** Writable runtime copy managed by Harness's workspace dependency loader. */
export function installedHarnessPython(): string | undefined {
  const root = join(process.env.DSH_HOME || join(homedir(), '.dsh'), 'dsh-runtimes', 'dsh-primary-runtime', 'dependencies', 'python')
  const python = process.platform === 'win32' ? join(root, 'python.exe') : join(root, 'bin', 'python3')
  return existsSync(python) ? python : undefined
}

export class PythonEnvironment {
  private python?: string
  private preparation?: Promise<any>
  private selection?: Promise<string>
  constructor(private configured?: string) {}
  get current(): string | undefined { return this.python }

  async resolve(load?: () => Promise<string>): Promise<string> {
    if (load && (!this.configured || this.configured === 'auto')) {
      this.selection ??= load().then(python => {
        this.python = python
        this.preparation = undefined
        return python
      }).catch(error => { this.selection = undefined; throw error })
      return this.selection
    }
    if (this.python) return this.python
    const selected = this.configured && this.configured !== 'auto' ? this.configured
      : installedHarnessPython() ?? (process.platform === 'win32' ? 'python' : 'python3')
    this.python = selected
    return selected
  }

  async check(signal?: AbortSignal): Promise<any> {
    return jsonProcess(await this.resolve(), script, '.', { action: 'check' }, signal)
  }

  async ensure(signal?: AbortSignal): Promise<any> {
    if (!this.preparation) {
      this.preparation = (async () => jsonProcess(await this.resolve(), script, '.', { action: 'install' }, signal, process.env, 360000))()
        .catch(error => { this.preparation = undefined; throw error })
    }
    return this.preparation
  }
}
