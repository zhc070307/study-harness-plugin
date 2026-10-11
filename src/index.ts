import z from '@deepseek-ai/schemastery'
import type { Context } from '@deepseek-ai/cordis'
import type { Agent } from '@deepseek-ai/dsh-agent'
import type { ToolExecution, ParameterSchemaSpec } from '@deepseek-ai/dsh-tools'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import { createScope, type Scope } from '@deepseek-ai/dsh-scope'
import { approveEscalation } from '@deepseek-ai/dsh-sandbox'
import type {} from '@deepseek-ai/dsh-sandbox-policy'
import type {} from '@deepseek-ai/dsh-user-approval'
import { writeFile, rename } from 'node:fs/promises'
import { realpathSync, readFileSync, existsSync } from 'node:fs'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { randomUUID } from 'node:crypto'
import * as Study from '../packages/agent/study.ts'
import { BINDING_FILE, projectFor, type Project } from './projects.ts'
import { jsonProcess } from './process.ts'
import { TranscriptionHosts } from './transcription-host.ts'

export const name = 'study-projects'
export const inject = ['agents', 'tools', 'systemPrompt', 'sandboxPolicy']
export interface Config { python?: string }
export const Config = z.object({ python: z.string().default('python3') })
const bridge = fileURLToPath(new URL('../packages/agent/plugin_bridge.py', import.meta.url))
const host = fileURLToPath(new URL('../packages/transcription/host.py', import.meta.url))
const output = { schema: { type: 'object' as const, additionalProperties: true }, render: (_args: object, value: object) => [{ type: 'text' as const, text: JSON.stringify(value) }] }

export function apply(ctx: Context, config: Config = {}): void {
  const python = config.python ?? 'python3'
  const hosts = new TranscriptionHosts(python, host)
  const mounted = new Map<Agent, { project: Project; scope: Scope; supplied?: string; generation?: number }>()

  async function authorize(exec: ToolExecution): Promise<void> {
    const policy = ctx.sandboxPolicy.resolve(exec.agent ? { session: exec.agent.session } : {})
    // Python owns transactions and compilation outside the shell sandbox. Be explicit about this boundary.
    await approveEscalation({ requestedMode: 'danger-full-access', effectiveMode: policy.mode,
      subject: '学习工具', justification: `${exec.name} 使用本机 Python 处理学习数据；写入、编译、日历和转写遵守本次宿主审批。` },
      { approver: ctx.get('approval'), agent: exec.agent, callId: exec.callId, toolName: exec.name, signal: exec.signal })
    exec.signal.throwIfAborted()
  }

  async function mount(agent: Agent): Promise<void> {
    const project = projectFor(agent.session.header.cwd)
    const old = mounted.get(agent)
    if (old && JSON.stringify(old.project) === JSON.stringify(project)) return
    if (old) { mounted.delete(agent); await old.scope.dispose() }
    if (!project) return
    const scope = createScope(ctx, agent)
    mounted.set(agent, { project, scope })
    async function call(action: string, args: object, id: string, signal?: AbortSignal, desktop = false) {
      const input = args as Record<string, any>
      let connection = hosts.config(project!.data)
      if (action === 'transcribe' && input.action === 'start') {
        const check = await jsonProcess(python, bridge, project!.data, { action: 'transcribe', args: { action: 'check', session: agent.session.id } }, signal, { ...process.env, STUDY_PDFIUM: '1', STUDY_TRANSCRIBE_CONFIG: connection ?? '' })
        if (!check.eligible) return { blocked: true, resources: check }
        connection = await hosts.start(project!.data, signal)
      }
      return jsonProcess(python, bridge, project!.data, { action, args: { ...input, session: agent.session.id }, id, desktop, project: project!.root, calendar: project!.calendar }, signal,
        { ...process.env, STUDY_PDFIUM: '1', STUDY_TRANSCRIBE_CONFIG: connection ?? '' })
    }
    Study.apply(scope.ctx, { data: project.data, python, bridge: call, host: true })
    scope.ctx.on('tools/execute', async (exec, next) => {
      if (exec.name.startsWith('study_') && exec.name !== 'study_project') await authorize(exec)
      return next()
    })
    const extraTools: { name: string; description: string; action: string; parameters: ParameterSchemaSpec }[] = [
      { name: 'study_import', description: '导入本项目已有的课程材料或录音，保留原件、文件身份和版本。同一文件同一版本重复导入复用记录。path 是项目内路径；也可传当前对话文件附件的 attachment_id。expected 可核对源文件 sha256。', action: 'import', parameters: { path: { type: 'string' }, attachment_id: { type: 'string' }, expected: { type: 'string' } } },
      { name: 'study_context', description: '读取本项目共享状态，或 remember 保存其他对话也能读到的材料摘要。summary 仅保存实际读过的内容；sources:[{path,sha256}] 保留学期内来源，源文件变化时摘要标为过期。保存须传读取结果的 expected 版本。学习约定请用 study_desktop agreement。', action: 'context', parameters: { action: { type: 'string', enum: ['read','remember'], required: true }, input: { type: 'string', required: true, description: 'JSON对象。read:{}；remember:{key,summary,sources:[{path,sha256}],expected}。' } } },
      { name: 'study_calendar', description: '查询或管理实际外部日历。先 status/calendars 核对连接，list/read 核对真实事件；create/update/delete 只依用户要求执行。写操作不证明学习任务完成。update/delete 必须使用读取结果的 expected；仅修改指定事件，重复系列不支持批量改写。未配置或访问失败要如实说明。', action: 'calendar', parameters: { action: { type: 'string', enum: ['status','calendars','list','read','create','update','delete'], required: true }, input: { type: 'string', required: true, description: 'JSON对象。list:{start,end}（带时区ISO8601）；read:{id}；create:{title,start,end,all_day?,notes?,location?,task?}；update:{id,expected,title,start,end,all_day?,notes?,location?}；delete:{id,expected}。全天事件使用YYYY-MM-DD，end不含边界。' } } },
    ]
    for (const tool of extraTools) {
      scope.ctx.effect(() => scope.ctx.tools.register(defineTool({ name: tool.name, description: tool.description, parameters: tool.parameters, output,
        async execute(args: any, exec) {
          if (tool.action === 'import' && args.attachment_id) {
            if (args.path) throw new Error('请提供文件路径或附件身份其中之一')
            const ref = agent.session.deriveMessages().flatMap(message => message.role === 'user' ? message.content : []).find(block => block.type === 'file' && block.attachment.attachmentId === args.attachment_id)
            if (!ref || ref.type !== 'file') throw new Error('此附件不属于当前对话，请使用实际上传的文件身份')
            const store = ctx.get('attachments')
            if (!store) throw new Error('宿主附件服务不可用')
            if (ref.attachment.bytes > 200 * 1024 * 1024) throw new Error('附件超过支持的大小')
            const chunks: Buffer[] = []
            let size = 0
            for await (const chunk of store.readFileStream(ref.attachment, exec.signal)) {
              size += chunk.byteLength
              if (size > 200 * 1024 * 1024) throw new Error('附件超过支持的大小')
              chunks.push(Buffer.from(chunk))
            }
            return call('attachment_import', { name: ref.attachment.name, attachment_id: ref.attachment.attachmentId, data: Buffer.concat(chunks).toString('base64'), expected: args.expected }, `${agent.session.id}:${exec.callId}`, exec.signal)
          }
          const input = 'input' in args ? JSON.parse(args.input) : args
          if (!input || Array.isArray(input) || typeof input !== 'object') throw new Error('工具输入必须是JSON对象')
          const action = tool.action === 'context' ? (args.action === 'remember' ? 'remember' : 'context') : tool.action
          return call(action, { ...input, ...(tool.action === 'calendar' ? { action: args.action } : {}) }, `${agent.session.id}:${exec.callId}`, exec.signal)
        },
      })))
    }
  }

  ctx.effect(() => ctx.tools.register(defineTool({
    name: 'study_project', description: '查看当前项目的学习插件绑定，或按用户明确要求 bind/unbind。绑定一次后，此项目每个对话自动获得学习工具与共享状态。不会改变其他项目或宿主默认预设。',
    parameters: { action: { type: 'string', required: true, enum: ['status','bind','unbind'] }, reason: { type: 'string', description: '绑定或解绑的用户要求' } }, output,
    async execute(args, exec): Promise<any> {
      const cwd = exec.agent?.session.header.cwd
      if (!cwd) throw new Error('当前会话没有项目目录')
      const project = projectFor(cwd)
      if (args.action === 'status') return { bound: !!project, project }
      if (!args.reason?.trim()) throw new Error('请保留用户的绑定或解绑要求')
      await authorize(exec)
      const root = args.action === 'unbind' && project ? project.root : realpathSync(cwd)
      const path = resolve(root, BINDING_FILE)
      const temporary = `${path}.${randomUUID()}.tmp`
      // Disabling retains settings, including an existing calendar connection.
      const previous = existsSync(path) ? JSON.parse(readFileSync(path, 'utf8')) : { version: 1, data: '.' }
      const value = { ...previous, version: 1, enabled: args.action === 'bind' }
      await writeFile(temporary, JSON.stringify(value, null, 2)+'\n', { mode: 0o600, flag: 'wx' })
      await rename(temporary, path)
      await mount(exec.agent!)
      return { bound: args.action === 'bind', root }
    },
  })))
  ctx.on('agent/created', async ({ agent }) => { await mount(agent) })
  ctx.on('agent/disposed', ({ agent }) => {
    const entry = mounted.get(agent)
    mounted.delete(agent)
    if (entry) void entry.scope.dispose().catch(error => ctx.logger.warn(String(error)))
  })
  ctx.on('agent/pre-step', async ({ agent, signal }, next) => {
    await mount(agent)
    const decision = await next()
    const entry = mounted.get(agent)
    if (!entry || decision.kind === 'reject' || signal.aborted) return decision
    const value = await jsonProcess(python, bridge, entry.project.data, { action: 'context', args: { session: agent.session.id } }, signal)
    const text = JSON.stringify(value)
    const generation = agent.session.surface.contentGeneration
    if (entry.supplied === text && entry.generation === generation) return decision
    entry.supplied = text
    entry.generation = generation
    return { ...decision, messages: [...decision.messages, createUserMessage({ source: { kind: 'study' }, content: [{ type: 'text', text: `当前项目学习状态（记录是参考，过期摘要须重新核对原文）：${text}` }] })] }
  })
  ctx.effect(() => async () => {
    await Promise.all([...mounted.values()].map(v => v.scope.dispose()))
    mounted.clear()
    await hosts.dispose()
  })
}
