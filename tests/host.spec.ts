import { afterEach, expect, it } from 'vitest'
import { Context } from '@deepseek-ai/cordis'
import { SessionId } from '@deepseek-ai/dsh-session'
import { ToolCallId, LlmAdapter, createUserMessage, type GenerateOptions, type StreamChunk } from '@deepseek-ai/dsh-llm'
import SandboxPolicy, { setSandboxMode } from '@deepseek-ai/dsh-sandbox-policy'
import Attachments from '@deepseek-ai/dsh-attachment-local'
import Persistence from '@deepseek-ai/dsh-session-persistence-jsonl'
import { mountAgentLoopTestDependencies, mountAgentLoopTestHarness } from '@deepseek-ai/dsh-agent-loop-testkit'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import * as Plugin from '../lib/index.js'

const contexts: Context[]=[]
const directories: string[]=[]
afterEach(async()=>{
  for(const ctx of contexts.splice(0)) await ctx.fiber.dispose()
  for(const directory of directories.splice(0)) rmSync(directory,{recursive:true,force:true})
})
async function runtime(bound=true) {
  const directory=mkdtempSync(join(tmpdir(),'study-host-'));directories.push(directory)
  const project=join(directory,'project'),other=join(directory,'other');mkdirSync(project);mkdirSync(other)
  if(bound)writeFileSync(join(project,'.study-plugin.json'),JSON.stringify({version:1,enabled:true,data:'.'}))
  const ctx=new Context();contexts.push(ctx)
  await mountAgentLoopTestDependencies(ctx,{tools:{mode:'native'}})
  await ctx.plugin(SandboxPolicy,{mode:'danger-full-access',workspaceRoot:project})
  await ctx.plugin(Persistence,{root:join(directory,'sessions')})
  await ctx.plugin(Plugin,{python:'python3'})
  const harness=await mountAgentLoopTestHarness(ctx)
  const agent=await harness.create(SessionId('learning'),{}, {cwd:project})
  const stranger=await harness.create(SessionId('other'),{}, {cwd:other})
  return {ctx,agent,stranger,project,harness}
}
async function call(ctx:Context,agent:any,name:string,args:object,id=name) {
  const result=await ctx.tools.execute({agent,name,arguments:args,callId:ToolCallId(id),signal:new AbortController().signal})
  return result
}
it('loads the built plugin into published Harness services and isolates project tools',async()=>{
  const {ctx,agent,stranger,project,harness}=await runtime()
  const names=ctx.tools.schemas(agent).map(t=>t.name)
  expect(names).toContain('study_desktop');expect(names).toContain('study_transcribe');expect(names).toContain('study_calendar')
  expect(ctx.tools.schemas(stranger).map(t=>t.name)).not.toContain('study_desktop')
  const course=await call(ctx,agent,'study_desktop',{action:'course',input:JSON.stringify({code:'MATH',name:'数学'})})
  expect(course.isError).toBe(false)
  const second=await harness.create(SessionId('second'),{}, {cwd:project})
  const context=await call(ctx,second,'study_context',{action:'read',input:'{}'})
  expect(context.isError).toBe(false);expect(JSON.stringify(context.content)).toContain('数学')
  expect(existsSync(join(project,'MATH/COURSE.md'))).toBe(true)
  const prompt=await ctx.systemPrompt.assemble({agent,scope:agent})
  expect(prompt.sections.some(s=>s.name==='study')).toBe(true)
  const otherPrompt=await ctx.systemPrompt.assemble({agent:stranger,scope:stranger})
  expect(otherPrompt.sections.some(s=>s.name==='study')).toBe(false)
})
it('denies Python execution when host policy requires unavailable approval',async()=>{
  const {ctx,agent,project}=await runtime()
  await setSandboxMode(agent.session,'read-only')
  const result=await call(ctx,agent,'study_desktop',{action:'course',input:JSON.stringify({code:'MATH',name:'数学'})})
  expect(result.isError).toBe(true)
  expect(existsSync(join(project,'MATH'))).toBe(false)
})
it('binds an existing chat and exposes tools without restarting the host',async()=>{
  const {ctx,agent,project}=await runtime(false)
  expect(ctx.tools.schemas(agent).map(t=>t.name)).not.toContain('study_desktop')
  const result=await call(ctx,agent,'study_project',{action:'bind',reason:'用户要求启用学习项目'})
  expect(result.isError).toBe(false)
  expect(existsSync(join(project,'.study-plugin.json'))).toBe(true)
  expect(ctx.tools.schemas(agent).map(t=>t.name)).toContain('study_desktop')
})
it('injects persisted project context through a real model request',async()=>{
  const {ctx,agent,project}=await runtime()
  await call(ctx,agent,'study_desktop',{action:'course',input:JSON.stringify({code:'PHYS',name:'物理'})})
  const requests: GenerateOptions[]=[]
  class Adapter extends LlmAdapter {
    resolveModel(provider:string,model:string) {return Promise.resolve({provider,id:model,name:model})}
    async *stream(options:GenerateOptions):AsyncIterable<StreamChunk> {
      requests.push(options)
      yield {type:'block-start',index:0,blockType:'text'}
      yield {type:'text-delta',index:0,text:'已读取课程状态'}
      yield {type:'block-end',index:0,block:{type:'text',text:'已读取课程状态'}}
      yield {type:'finish',reason:{kind:'stop'}}
    }
  }
  ctx.llm.registerAdapter(['mock'],new Adapter())
  // A concrete model route is required when creating the Agent.
  const handle=await ctx.agents.create({sessionId:SessionId('model'),meta:{cwd:project},agentOptions:{provider:'mock',model:'mock'}})
  const errors:unknown[]=[]
  const off=ctx.on('agent/error',event=>errors.push(event.error))
  const idle=new Promise<void>(resolve=>{const dispose=ctx.on('agent/status',event=>{if(event.agent===handle.agent&&event.status==='idle'){dispose();resolve()}})})
  handle.agent.followup(createUserMessage({source:{kind:'user'},content:[{type:'text',text:'我有哪些课程？'}]}))
  await idle;off()
  expect(errors).toEqual([])
  expect(requests).toHaveLength(1)
  expect(JSON.stringify(requests[0].messages)).toContain('物理')
  await handle.dispose()
},30000)

it('imports a real Harness file attachment and refuses attachments from another chat',async()=>{
  const {ctx,agent,project,harness}=await runtime()
  await ctx.plugin(Attachments,{dshHome:join(project,'.attachments')})
  const ref=await ctx.attachments.saveFile({name:'lecture.txt',data:Buffer.from('真实的附件材料')})
  agent.session.append('user/message',createUserMessage({source:{kind:'user'},content:[{type:'file',attachment:ref}]}),{surfaceOp:'append'})
  const result=await call(ctx,agent,'study_import',{attachment_id:ref.attachmentId})
  expect(result.isError, JSON.stringify(result)).toBe(false)
  const record=JSON.parse((result.content[0] as any).text)
  expect(record.name).toBe('lecture.txt')
  const second=await harness.create(SessionId('attachment-stranger'),{}, {cwd:project})
  const strangerResult=await call(ctx,second,'study_import',{attachment_id:ref.attachmentId})
  expect(strangerResult.isError).toBe(true)
  expect(JSON.stringify(strangerResult.content)).toContain('此附件不属于当前对话')
  const read=await call(ctx,agent,'study_desktop',{action:'read',input:JSON.stringify({file:record.id})})
  expect(JSON.stringify(read.content)).toContain('真实的附件材料')
})
