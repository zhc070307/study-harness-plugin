import { build } from 'esbuild'
import { cp, mkdir, rm } from 'node:fs/promises'
await rm('lib', { recursive: true, force: true })
await mkdir('lib', { recursive: true })
await build({ entryPoints: ['src/index.ts','src/cli.ts'], outdir: 'lib', bundle: true, platform: 'node', target: 'node24', format: 'esm', packages: 'external', sourcemap: true })
for (const name of ['ASSISTANT.md','HOST_ASSISTANT.md']) await cp(`packages/agent/${name}`,`lib/${name}`)
