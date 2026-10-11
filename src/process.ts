import { execFile } from 'node:child_process'

export function jsonProcess(python: string, script: string, root: string, request: object, signal?: AbortSignal, env: NodeJS.ProcessEnv = process.env): Promise<any> {
  return new Promise((resolve, reject) => {
    const child = execFile(python, [script, root], { signal, timeout: 120000, maxBuffer: 32 * 1024 * 1024, encoding: 'utf8', env: { ...env, PYTHONUTF8: '1' } }, (error, stdout, stderr) => {
      if (signal?.aborted) { reject(signal.reason); return }
      let response: any
      try { response = JSON.parse(stdout) }
      catch { reject(new Error(stderr.trim() || error?.message || '学习后台返回了无效JSON')); return }
      if (!response.ok) { reject(new Error(response.error)); return }
      if (error) { reject(error); return }
      resolve(response.result)
    })
    child.stdin!.on('error', () => {})
    child.stdin!.end(JSON.stringify(request))
  })
}
