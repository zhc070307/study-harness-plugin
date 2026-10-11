import type { Context } from '@deepseek-ai/cordis'
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import type { ContextFormed } from '@deepseek-ai/dsh-llm'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { assertImageCapableRoute } from './model-image.ts'
import { admitEncodedImages } from '@deepseek-ai/dsh-attachment'
import { execFile } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { readFileSync } from 'node:fs'

declare module '@deepseek-ai/dsh-llm' {
  interface MessageSourceMap { 'study': { kind: 'study' } & ContextFormed }
}

export const name = 'study'
export const inject = ['tools', 'systemPrompt']
export interface Config {
  data: string
  python?: string
  /** Host adapter owns session routing, resources and permission checks. */
  bridge?: (action: string, args: object, id: string, signal?: AbortSignal, desktop?: boolean) => Promise<any>
  host?: boolean
}

const worker = fileURLToPath(new URL('./worker.py', import.meta.url))

export function apply(ctx: Context, config: Config) {
  if (!config.data?.trim()) throw new Error('Study data directory is required')
  const python = config.python ?? 'python3'

  async function call(action: string, args: object, id: string, signal?: AbortSignal, desktop = false) {
    if (config.bridge) return config.bridge(action, args, id, signal, desktop)
    // execFile provides cancellation and timeout; Python owns ledger transactions.
    return new Promise<any>((resolve, reject) => {
      const child = execFile(python, [desktop ? fileURLToPath(new URL('./workspace.py', import.meta.url)) : worker, config.data], {
        signal, timeout: desktop ? 120000 : 15000, maxBuffer: desktop ? 32 * 1024 * 1024 : 1024 * 1024, encoding: 'utf8',
      }, (error, stdout, stderr) => {
        if (signal?.aborted) { reject(signal.reason ?? new Error('Cancelled')); return }
        try {
          const message = JSON.parse(stdout)
          if (!message.ok) { reject(new Error(message.error)); return }
          if (error) { reject(error); return }
          resolve(message.result)
        } catch (failure) { reject(error ?? new Error(stderr || String(failure))) }
      })
      child.stdin!.on('error', () => {}) // Process failure is reported by the callback.
      child.stdin!.end(JSON.stringify({ action, args, id }))
    })
  }

  const output = {
    schema: { type: 'object' as const, additionalProperties: true },
    render: (_args: object, value: object) => [{ type: 'text' as const, text: JSON.stringify(value) }],
  }
  ctx.effect(() => ctx.tools.register(defineTool({
    name: 'study_read', description: '读取学习进度、课程事实与核对版本、作答与错点历史、长期约定或流程实现。context=true 获取完整当前上下文；day 可指定日期。',
    parameters: { source:{type:'string',enum:['operations','implementation','desktop'],description:'读取实际操作或流程实现，start/end 指定行范围'},start:{type:'integer'},end:{type:'integer'}, standard: {type:'string',description:'读取 standards 中指定名称的完整规范，例如 note、worksheet、files；仅在本次需要时读取。'}, semester: { type: 'string' }, course: { type: 'string' }, run: { type: 'string' }, day: { type: 'string' }, context: { type: 'boolean' }, calendar: { type: 'boolean' } }, output,
    async execute(args, exec) {
      return call('read', { ...args, session: exec.agent?.session.id }, '', exec.signal)
    },
  })))
  ctx.effect(() => ctx.tools.register(defineTool({
    name: 'study_textbook',
    description: '统一教材检索：保留原 PDF，按页建立 SQLite 全文索引，关键词定位后再读原页。status 查看当前版本的索引、章节与页码映射；index 建立或更新，expected 使用 list/read 的文件 sha256。同版本只更新映射时复用页文字。教材名称、版本、chapters 和 page_map 必须来自实际标题页、目录和原页，未知时不填；不猜固定页码差。search 按实际语言关键词、章节或页码检索；英文教材的中文问题由你结合语境转成英文术语，不存在自动语义检索。read 读取命中的 PDF 页文字；公式、图形、扫描页仍用 study_desktop page 查看原页，传 expected 校验版本。结果不是完整题面或视觉核验；未命中不等于书中不存在。',
    parameters: {
      action: { type: 'string', enum: ['status','index','search','read'], required: true },
      input: { type: 'string', required: true, description: 'JSON 对象，必填 file，可选 semester。index:{file,expected,title?,edition?,chapters?:[{number:章节编号文字,title,pdf_start,pdf_end}],page_map?:[{pdf_start,pdf_end,book_start:纸书起始页整数}或{pdf_start,label:实际非数字页码}]}；PDF 页码从1开始，各页码映射不能重叠。search:{file,query?:关键词,chapter?:确切编号或标题,book_page?:纸书页码文字,pdf_start?,pdf_end?,limit?:最多30,offset?}；返回引用、版本、PDF/纸书页码、片段和 next_offset。read:{file,pdf_page,expected?,offset?,limit?:最多50000字}。' },
    }, output,
    async execute(args, exec) {
      const input = JSON.parse(args.input)
      if (!input || Array.isArray(input) || typeof input !== 'object') throw new Error('教材输入必须是对象')
      return call('textbook', {...input, action: args.action, session: exec.agent?.session.id},
        `${exec.agent?.session.id ?? 'direct'}:${exec.callId}`, exec.signal, true)
    },
  })))
  ctx.effect(() => ctx.tools.register(defineTool({
    name: 'study_transcribe', description: '可选的本地录音转写工具。已有讲稿或转写稿直接读取，不安装、不转写。check 检查设备资源，资源不足禁止下载；默认检测并使用宿主 GPU，设备不支持时说明检测结果与可直接使用文字稿的选择，不自动使用 CPU。status 查看安装及任务；start 启动或继续，file/expected 使用真实录音身份与哈希。首次 needs_install 时先说明约3.8 GiB模型、实际检测选用的后端及内存/显存/磁盘要求，用户明确同意才 allow_install=true。start 返回后台任务，及时告诉用户正在处理，不循环等待；完成的原稿保存到原录音文件夹。stop 传job停止并保留断点。',
    parameters: {action:{type:'string',enum:['status','check','start','stop'],required:true},input:{type:'string',required:true,description:'JSON对象。status:{}；start:{file,expected,allow_install可选}；stop:{job}。'}},output,
    async execute(args, exec) {
      const input=JSON.parse(args.input)
      if (!input || Array.isArray(input) || typeof input !== 'object') throw new Error('转写输入必须是对象')
      if (config.bridge) return call('transcribe', {...input, action: args.action, session: exec.agent?.session.id}, `${exec.agent?.session.id ?? 'direct'}:${exec.callId}`, exec.signal)
      return new Promise<any>((resolve,reject)=>{
        const child=execFile(python,[fileURLToPath(new URL('./transcription.py',import.meta.url)),config.data],{signal:exec.signal,timeout:15000,maxBuffer:1024*1024,encoding:'utf8'},(error,stdout)=>{
          try {const reply=JSON.parse(stdout);if(error||!reply.ok)reject(new Error(reply.error||'转写请求失败'));else resolve(reply.result)}catch(failure){reject(failure)}
        })
        child.stdin!.on('error',()=>{});child.stdin!.end(JSON.stringify({action:args.action,args:input}))
      })
    },
  })))
  ctx.effect(() => ctx.tools.register(defineTool({
    name: 'study_work',
    description: '开始或继续学习流程。先读取可执行操作，选择所需操作并保存真实输入和结果；出卷不代表学生已作答。switch 只在用户要求改变正在执行的流程时使用。',
    parameters: {
      action: { type: 'string', enum: ['start', 'advance', 'switch','service'], required: true },
      input: { type: 'string', required: true, description: 'JSON 对象。semester(可选)处理已准备的指定学期；start: flow,course(课程流程需要),name；全局 daily/memory 无需课程。处理器字段见 study_read 返回的 implementation 和 operations 文件；advance: run,step,input；switch: run,reason。service: arguments 参数列表，调用现有学习服务处理任务更新、日期、材料、作答和计划；先用 [--help] 或子命令的 --help 查看实际用法，不猜参数，不需要终端。数据库固定为选定学期；中断后先核对实际记录，不能重复创建证据。' },
    }, output,
    async execute(args, exec) {
      const input = JSON.parse(args.input)
      if (!input || Array.isArray(input) || typeof input !== 'object') throw new Error('步骤输入必须是对象')
      return call(args.action, { ...input, session: exec.agent?.session.id },
        `${exec.agent?.session.id ?? 'direct'}:${exec.callId}`, exec.signal)
    },
  })))
  ctx.effect(() => ctx.tools.register(defineTool({
    name: 'study_change',
    description: '根据用户要求修改实际流程。preview 保存并验证修改，apply 采用通过验证的版本，rollback 恢复上个版本。范围可为一门课程或通用流程；已运行的记录不会自动改写。handler 仅允许纯 JSON 数据转换的 run(input, context) 函数，可用条件、算术、min/max/len 等内置函数及对象 .get；禁止导入、文件/网络访问、属性探查和循环，必须带输入和预期结果 tests。',
    parameters: {
      action: { type: 'string', enum: ['preview', 'apply', 'rollback'], required: true },
      input: { type: 'string', required: true, description: 'JSON 对象：flow,course(可选)，scope=learning 表示跨学期通用流程，不与 course 同用；study_read.learning_flows 返回通用现行版本，省略scope只改当前学期。preview: reason,definition；apply: version,expected；rollback: expected。definition 包含 title 和 steps，每步有 id,kind,instructions，after 只声明模型操作所需的真实证据依赖；列表顺序不构成依赖；可加 handler Python 代码 run(input,context) 与 tests。' },
    }, output,
    async execute(args, exec) {
      const input = JSON.parse(args.input)
      if (!input || Array.isArray(input) || typeof input !== 'object') throw new Error('流程修改必须是对象')
      return call('change', { ...input, action: args.action },
        `${exec.agent?.session.id ?? 'direct'}:${exec.callId}`, exec.signal)
    },
  })))
  ctx.effect(() => ctx.systemPrompt.section({
    name: 'study', order: 0, interpolate: false,
    text: readFileSync(fileURLToPath(new URL(config.host ? './HOST_ASSISTANT.md' : './ASSISTANT.md', import.meta.url)), 'utf8') + '\n你是学习助手。说话简洁自然，先看材料和学生要求，再决定怎样帮助学习。使用 study 工具读取、执行和修改实际流程；不知道的事实保持未知，不从文件名推断材料类型。读笔记与练习由用户自主安排，不能默认两者有先后，不自动添加用户任务依赖。流程约束模型保存依据与核对事实。用户只需表达学习要求，不让用户填写流程配置、版本或核验字段。技术记录留在工具内部，向用户说明学习上的改变与下一步。修改流程须依据用户要求，验证失败先修正；保留原始作答，不把讲解、笔记或发卷当作掌握。',
  }))
  ctx.effect(() => ctx.tools.register(defineTool({
    name: 'study_desktop',
    description: '读取与操作用户的真实桌面。semester(action=list/create/rename/archive/reopen/remove/restore/select,name,semester,expected,reason) 由模型依用户意图管理学期；create只准备新学期，select在本次处理结束后切换；expected使用semester list的version。其他桌面操作可传semester处理指定学期。carry(file,target,reason,folder可选) 保留原件并把自主选中的资料复制到另一学期，保存来源，不复制任务或掌握结论。course(code,name) 建立课程与文件夹，不决定学习方法。agreement(scope=learning/semester/course,key,text,reason,expected,course可选) 保存或 remove=true 停用约定；learning跨学期，semester只在当前学期，course只在本课程；expected 为 list.agreements 对应范围的 version。各范围作为参考返回，当前用户意愿优先，不能把记忆作为自动安排用户的命令；历史保留。remove_file/restore_file(file,expected) 可恢复地移除文件，不删证据。list/read 获取文件身份与原文；PDF read 每次最多100000字，返回 characters、truncated 与 next_offset，用 offset/limit/expected 继续，未读完不能当作全文；按页定位用 study_textbook 或 page；create 保存文件（kind=practice 可显示作答框，sources 记录真实题源）；write/restore 必须带当前 sha256 的 expected；folder(name,parent,category可选) 建子文件夹，parent 使用真实上级身份。生成笔记和练习 create 必须明确 course 与 category（notes/practice/materials/assignments）或制作依据，归课程内分类，不在根目录新建本次成果；metadata 移动/重命名需 list 的 version 作 expected。publish 根据已读内容将文件显式关联到已注册 course 的 path（课程内相对位置），必须说明 reason；原材料扫描和回执使用此真实课程文件。课程文件在桌面之外被修改时保留两份并报告冲突。task 建学生任务，无默认前后依赖；不自动勾选或登记掌握。compile 编译 .tex，expected 为源码哈希，正式学习交付关联写作前已保存的 preparation；assets 为明确附件 {file,path}；失败返回日志并保留上版 PDF。page 传 PDF file/page 返回实际页面图像；逐页阅读图像并核对来源后 review，编译成功不是验收。正式笔记/练习须先 prepare(course,kind,brief) 保存真实草案，create 带返回的 preparation 身份；review 还须 kind=note/worksheet、course、brief，并遵守 study_read 返回的交付标准。history/restore 可恢复文件。',
    parameters: {
      action: { type: 'string', enum: ['semester','carry','course','agreement','remove_file','restore_file','list','read','prepare','publish','create','write','restore','folder','metadata','task','compile','page','review','history'], required: true },
      input: { type: 'string', required: true, description: 'JSON 对象。文件操作使用 file 身份；create: name,text,mime,folder(可选),kind,sources；write:file,text,expected；compile:file,expected,assets(可选)。page:file,page。review:file,sha256,pages:[{page,status:passed,summary:具体核验}],checks:{source_scope,layout,...交付核验}。源码或 PDF 变化会拒绝旧核验。task:title,file(可选),course(已注册可选),date(安排日期YYYY-MM-DD，null表示暂不安排，省略为今天),due_at(正式截止，可选)。操作实现位于 packages/agent/workspace.py，可通过 study_read source=desktop 查阅。' },
    },
    output: {
      schema: { type: 'object' as const, additionalProperties: true },
      render: (_args: object, value: any) => {
        const { images, ...record } = value
        return [{ type: 'text' as const, text: JSON.stringify(record) },
          ...(images ?? []).map((img: any) => ({ type: 'image' as const, attachment: img }))]
      },
    },
    async execute(args, exec) {
      const input = JSON.parse(args.input)
      if (!input || Array.isArray(input) || typeof input !== 'object') throw new Error('桌面输入必须是对象')
      if (args.action === 'page' || args.action === 'review') await assertImageCapableRoute(ctx, exec, 'PDF 页面')
      const result = await call(args.action, {...input, session: exec.agent?.session.id}, `${exec.agent?.session.id ?? 'direct'}:${exec.callId}`, exec.signal, true)
      if (result.images) {
        await assertImageCapableRoute(ctx, exec, '桌面图像')
        if (!ctx.get('attachments')) throw new Error('页面图像服务不可用')
        result.images = await admitEncodedImages(ctx.attachments, result.images.map((img: any) => ({data: img.data, mediaType: img.mimeType})))
      }
      return result
    },
  })))
  ctx.effect(() => ctx.systemPrompt.section({
    name: 'desktop', order: 1, interpolate: false,
    text: (config.host ? '学习文件由 study_desktop 保存，课程成果发布到宿主工作区。' : '桌面文件由 study_desktop 与网页共同保存。') + '用户关联的是文件身份和当前哈希，必须 read 实际内容；选区与作答是用户提供的原文，不替代完整题目。生成练习须依据已读材料，不使用固定示例充当真实出题；结果必须 create/write 保存，不能只在回复声称完成。修改 LaTeX 后 compile，再逐页 page 看实际图像，核对来源和版式后才 review；失败先根据日志修正。笔记与练习分别遵守 study-notes、study-worksheets 的质量要求；由你负责写作、编译与核验，不因文档类型强制用户阅读或练习顺序。任务完成、模型批改、知识掌握仍需现有 study 服务的真实证据；桌面勾选仅是学生确认完成。',
  }))
  const supplied = new Map<string, string>()
  ctx.effect(() => () => supplied.clear())
  if (config.host) return
  ctx.on('agent/pre-step', async ({ agent, signal }, next) => {
    const decision = await next()
    if (decision.kind === 'reject' || signal.aborted) return decision
    const state = await call('read', { session: agent.session.id }, '', signal)
    const context = { pending: state.pending ?? [], memory: state.memory?.content ?? '', recovery: state.recovery_warnings ?? [], agreements: state.agreements?.references ?? [] }
    if (!context.pending.length && !context.memory && !context.recovery.length && !context.agreements.length) return decision
    const content = JSON.stringify(context)
    if (supplied.get(agent.session.id) === content) return decision
    supplied.set(agent.session.id, content)
    return { ...decision, messages: [...decision.messages, createUserMessage({
      source: { kind: 'study' },
      content: [{ type: 'text', text: `当前学习流程（内部上下文）：${content}` }],
    })] }
  })
}
