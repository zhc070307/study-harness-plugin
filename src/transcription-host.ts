import { spawn, type ChildProcess } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, openSync, closeSync } from 'node:fs'
import { join } from 'node:path'
import { randomUUID } from 'node:crypto'

/** One loopback service per project. Large models are downloaded only by start + consent. */
export class TranscriptionHosts {
  private hosts = new Map<string, { process: ChildProcess; config: string; ready: Promise<string> }>()
  constructor(private python: string, private script: string) {}

  config(data: string): string | undefined { return this.hosts.get(data)?.config }

  async start(data: string, signal?: AbortSignal): Promise<string> {
    const current = this.hosts.get(data)
    if (current) return current.ready
    const directory = join(data, '.transcription')
    mkdirSync(directory, { recursive: true })
    const config = join(directory, `connection-${randomUUID()}.json`)
    const log = join(directory, 'host.log')
    const fd = openSync(log, 'a', 0o600)
    const child = spawn(this.python, [this.script, '--bind', '127.0.0.1', '--port', '0', '--data', join(directory, 'host'), '--config', config], { stdio: ['ignore', fd, fd], env: { ...process.env, PYTHONUTF8: '1' } })
    closeSync(fd)
    let failure: Error | undefined
    child.on('error', error => { failure = error })
    const ready = (async () => {
      for (let attempt = 0; attempt < 200; attempt++) {
        signal?.throwIfAborted()
        if (existsSync(config)) {
          const value = JSON.parse(readFileSync(config, 'utf8'))
          if (!value.url?.startsWith('http://127.0.0.1:')) throw new Error('转写后台没有绑定本机回环地址')
          return config
        }
        if (failure || child.exitCode !== null) throw new Error(`转写后台未启动；请查看 ${log}（需要 Python 3.12 和受支持 GPU）`)
        await new Promise(resolve => setTimeout(resolve, 50))
      }
      throw new Error(`转写后台启动超时；请查看 ${log}`)
    })().catch(error => { child.kill(); this.hosts.delete(data); throw error })
    this.hosts.set(data, { process: child, config, ready })
    child.on('exit', () => { if (this.hosts.get(data)?.process === child) this.hosts.delete(data) })
    return ready
  }

  async dispose(): Promise<void> {
    const children = [...this.hosts.values()].map(v => v.process)
    this.hosts.clear()
    await Promise.all(children.map(child => new Promise<void>(resolve => {
      if (child.exitCode !== null) { resolve(); return }
      const timer = setTimeout(() => { child.kill('SIGKILL'); resolve() }, 4000)
      child.once('exit', () => { clearTimeout(timer); resolve() })
      child.kill('SIGTERM')
    })))
  }
}
