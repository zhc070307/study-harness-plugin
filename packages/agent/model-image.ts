import type { Context } from '@deepseek-ai/cordis'
import type { ToolExecution } from '@deepseek-ai/dsh-tools'

/** Use the public model service, without importing a filesystem tool implementation. */
export async function assertImageCapableRoute(ctx: Context, exec: ToolExecution, label: string): Promise<void> {
  const routed = exec.agent?.session.requestHeader()?.config
  const provider = routed?.provider ?? exec.agent?.options.provider
  const model = routed?.model ?? exec.agent?.options.model
  const llm = ctx.get('llm')
  if (!llm || !provider || !model) throw new Error(`${label}：无法确定当前模型的图像能力`)
  const info = await llm.resolveModelInfo(provider, model, exec.signal)
  if (!info.inputModalities?.includes('image')) throw new Error(`${label}：当前模型不支持图像，请切换支持图像的模型`)
}
