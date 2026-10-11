// src/index.ts
import z from "@deepseek-ai/schemastery";
import { defineTool as defineTool2 } from "@deepseek-ai/dsh-tools";
import { createUserMessage as createUserMessage2, ToolCallId } from "@deepseek-ai/dsh-llm";
import { createScope } from "@deepseek-ai/dsh-scope";
import { approveEscalation } from "@deepseek-ai/dsh-sandbox";
import { writeFile, rename } from "node:fs/promises";
import { realpathSync as realpathSync2, readFileSync as readFileSync4, existsSync as existsSync4 } from "node:fs";
import { resolve as resolve2, isAbsolute as isAbsolute2 } from "node:path";
import { fileURLToPath as fileURLToPath3 } from "node:url";
import { randomUUID as randomUUID2 } from "node:crypto";

// packages/agent/study.ts
import { createUserMessage } from "@deepseek-ai/dsh-llm";
import { defineTool } from "@deepseek-ai/dsh-tools";

// packages/agent/model-image.ts
async function assertImageCapableRoute(ctx, exec, label) {
  const routed = exec.agent?.session.requestHeader()?.config;
  const provider = routed?.provider ?? exec.agent?.options.provider;
  const model = routed?.model ?? exec.agent?.options.model;
  const llm = ctx.get("llm");
  if (!llm || !provider || !model) throw new Error(`${label}\uFF1A\u65E0\u6CD5\u786E\u5B9A\u5F53\u524D\u6A21\u578B\u7684\u56FE\u50CF\u80FD\u529B`);
  const info = await llm.resolveModelInfo(provider, model, exec.signal);
  if (!info.inputModalities?.includes("image")) throw new Error(`${label}\uFF1A\u5F53\u524D\u6A21\u578B\u4E0D\u652F\u6301\u56FE\u50CF\uFF0C\u8BF7\u5207\u6362\u652F\u6301\u56FE\u50CF\u7684\u6A21\u578B`);
}

// packages/agent/study.ts
import { admitEncodedImages } from "@deepseek-ai/dsh-attachment";
import { execFile } from "node:child_process";
import { fileURLToPath } from "node:url";
import { readFileSync } from "node:fs";
var worker = fileURLToPath(new URL("./worker.py", import.meta.url));
function apply(ctx, config) {
  if (!config.data?.trim()) throw new Error("Study data directory is required");
  const python = config.python ?? "python3";
  async function call(action, args, id, signal, desktop = false) {
    if (config.bridge) return config.bridge(action, args, id, signal, desktop);
    return new Promise((resolve3, reject) => {
      const child = execFile(python, [desktop ? fileURLToPath(new URL("./workspace.py", import.meta.url)) : worker, config.data], {
        signal,
        timeout: desktop ? 12e4 : 15e3,
        maxBuffer: desktop ? 32 * 1024 * 1024 : 1024 * 1024,
        encoding: "utf8"
      }, (error, stdout, stderr) => {
        if (signal?.aborted) {
          reject(signal.reason ?? new Error("Cancelled"));
          return;
        }
        try {
          const message = JSON.parse(stdout);
          if (!message.ok) {
            reject(new Error(message.error));
            return;
          }
          if (error) {
            reject(error);
            return;
          }
          resolve3(message.result);
        } catch (failure) {
          reject(error ?? new Error(stderr || String(failure)));
        }
      });
      child.stdin.on("error", () => {
      });
      child.stdin.end(JSON.stringify({ action, args, id }));
    });
  }
  const output2 = {
    schema: { type: "object", additionalProperties: true },
    render: (_args, value) => [{ type: "text", text: JSON.stringify(value) }]
  };
  ctx.effect(() => ctx.tools.register(defineTool({
    name: "study_read",
    description: "\u8BFB\u53D6\u5B66\u4E60\u8FDB\u5EA6\u3001\u8BFE\u7A0B\u4E8B\u5B9E\u4E0E\u6838\u5BF9\u7248\u672C\u3001\u4F5C\u7B54\u4E0E\u9519\u70B9\u5386\u53F2\u3001\u957F\u671F\u7EA6\u5B9A\u6216\u6D41\u7A0B\u5B9E\u73B0\u3002context=true \u83B7\u53D6\u5B8C\u6574\u5F53\u524D\u4E0A\u4E0B\u6587\uFF1Bday \u53EF\u6307\u5B9A\u65E5\u671F\u3002",
    parameters: { source: { type: "string", enum: ["operations", "implementation", "desktop"], description: "\u8BFB\u53D6\u5B9E\u9645\u64CD\u4F5C\u6216\u6D41\u7A0B\u5B9E\u73B0\uFF0Cstart/end \u6307\u5B9A\u884C\u8303\u56F4" }, start: { type: "integer" }, end: { type: "integer" }, standard: { type: "string", description: "\u8BFB\u53D6 standards \u4E2D\u6307\u5B9A\u540D\u79F0\u7684\u5B8C\u6574\u89C4\u8303\uFF0C\u4F8B\u5982 note\u3001worksheet\u3001files\uFF1B\u4EC5\u5728\u672C\u6B21\u9700\u8981\u65F6\u8BFB\u53D6\u3002" }, semester: { type: "string" }, course: { type: "string" }, run: { type: "string" }, day: { type: "string" }, context: { type: "boolean" }, calendar: { type: "boolean" } },
    output: output2,
    async execute(args, exec) {
      return call("read", { ...args, session: exec.agent?.session.id }, "", exec.signal);
    }
  })));
  ctx.effect(() => ctx.tools.register(defineTool({
    name: "study_textbook",
    description: "\u7EDF\u4E00\u6559\u6750\u68C0\u7D22\uFF1A\u4FDD\u7559\u539F PDF\uFF0C\u6309\u9875\u5EFA\u7ACB SQLite \u5168\u6587\u7D22\u5F15\uFF0C\u5173\u952E\u8BCD\u5B9A\u4F4D\u540E\u518D\u8BFB\u539F\u9875\u3002status \u67E5\u770B\u5F53\u524D\u7248\u672C\u7684\u7D22\u5F15\u3001\u7AE0\u8282\u4E0E\u9875\u7801\u6620\u5C04\uFF1Bindex \u5EFA\u7ACB\u6216\u66F4\u65B0\uFF0Cexpected \u4F7F\u7528 list/read \u7684\u6587\u4EF6 sha256\u3002\u540C\u7248\u672C\u53EA\u66F4\u65B0\u6620\u5C04\u65F6\u590D\u7528\u9875\u6587\u5B57\u3002\u6559\u6750\u540D\u79F0\u3001\u7248\u672C\u3001chapters \u548C page_map \u5FC5\u987B\u6765\u81EA\u5B9E\u9645\u6807\u9898\u9875\u3001\u76EE\u5F55\u548C\u539F\u9875\uFF0C\u672A\u77E5\u65F6\u4E0D\u586B\uFF1B\u4E0D\u731C\u56FA\u5B9A\u9875\u7801\u5DEE\u3002search \u6309\u5B9E\u9645\u8BED\u8A00\u5173\u952E\u8BCD\u3001\u7AE0\u8282\u6216\u9875\u7801\u68C0\u7D22\uFF1B\u82F1\u6587\u6559\u6750\u7684\u4E2D\u6587\u95EE\u9898\u7531\u4F60\u7ED3\u5408\u8BED\u5883\u8F6C\u6210\u82F1\u6587\u672F\u8BED\uFF0C\u4E0D\u5B58\u5728\u81EA\u52A8\u8BED\u4E49\u68C0\u7D22\u3002read \u8BFB\u53D6\u547D\u4E2D\u7684 PDF \u9875\u6587\u5B57\uFF1B\u516C\u5F0F\u3001\u56FE\u5F62\u3001\u626B\u63CF\u9875\u4ECD\u7528 study_desktop page \u67E5\u770B\u539F\u9875\uFF0C\u4F20 expected \u6821\u9A8C\u7248\u672C\u3002\u7ED3\u679C\u4E0D\u662F\u5B8C\u6574\u9898\u9762\u6216\u89C6\u89C9\u6838\u9A8C\uFF1B\u672A\u547D\u4E2D\u4E0D\u7B49\u4E8E\u4E66\u4E2D\u4E0D\u5B58\u5728\u3002",
    parameters: {
      action: { type: "string", enum: ["status", "index", "search", "read"], required: true },
      input: { type: "string", required: true, description: "JSON \u5BF9\u8C61\uFF0C\u5FC5\u586B file\uFF0C\u53EF\u9009 semester\u3002index:{file,expected,title?,edition?,chapters?:[{number:\u7AE0\u8282\u7F16\u53F7\u6587\u5B57,title,pdf_start,pdf_end}],page_map?:[{pdf_start,pdf_end,book_start:\u7EB8\u4E66\u8D77\u59CB\u9875\u6574\u6570}\u6216{pdf_start,label:\u5B9E\u9645\u975E\u6570\u5B57\u9875\u7801}]}\uFF1BPDF \u9875\u7801\u4ECE1\u5F00\u59CB\uFF0C\u5404\u9875\u7801\u6620\u5C04\u4E0D\u80FD\u91CD\u53E0\u3002search:{file,query?:\u5173\u952E\u8BCD,chapter?:\u786E\u5207\u7F16\u53F7\u6216\u6807\u9898,book_page?:\u7EB8\u4E66\u9875\u7801\u6587\u5B57,pdf_start?,pdf_end?,limit?:\u6700\u591A30,offset?}\uFF1B\u8FD4\u56DE\u5F15\u7528\u3001\u7248\u672C\u3001PDF/\u7EB8\u4E66\u9875\u7801\u3001\u7247\u6BB5\u548C next_offset\u3002read:{file,pdf_page,expected?,offset?,limit?:\u6700\u591A50000\u5B57}\u3002" }
    },
    output: output2,
    async execute(args, exec) {
      const input = JSON.parse(args.input);
      if (!input || Array.isArray(input) || typeof input !== "object") throw new Error("\u6559\u6750\u8F93\u5165\u5FC5\u987B\u662F\u5BF9\u8C61");
      return call(
        "textbook",
        { ...input, action: args.action, session: exec.agent?.session.id },
        `${exec.agent?.session.id ?? "direct"}:${exec.callId}`,
        exec.signal,
        true
      );
    }
  })));
  ctx.effect(() => ctx.tools.register(defineTool({
    name: "study_transcribe",
    description: "\u53EF\u9009\u7684\u672C\u5730\u5F55\u97F3\u8F6C\u5199\u5DE5\u5177\u3002\u5DF2\u6709\u8BB2\u7A3F\u6216\u8F6C\u5199\u7A3F\u76F4\u63A5\u8BFB\u53D6\uFF0C\u4E0D\u5B89\u88C5\u3001\u4E0D\u8F6C\u5199\u3002check \u68C0\u67E5\u8BBE\u5907\u8D44\u6E90\uFF0C\u8D44\u6E90\u4E0D\u8DB3\u7981\u6B62\u4E0B\u8F7D\uFF1B\u9ED8\u8BA4\u68C0\u6D4B\u5E76\u4F7F\u7528\u5BBF\u4E3B GPU\uFF0C\u8BBE\u5907\u4E0D\u652F\u6301\u65F6\u8BF4\u660E\u68C0\u6D4B\u7ED3\u679C\u4E0E\u53EF\u76F4\u63A5\u4F7F\u7528\u6587\u5B57\u7A3F\u7684\u9009\u62E9\uFF0C\u4E0D\u81EA\u52A8\u4F7F\u7528 CPU\u3002status \u67E5\u770B\u5B89\u88C5\u53CA\u4EFB\u52A1\uFF1Bstart \u542F\u52A8\u6216\u7EE7\u7EED\uFF0Cfile/expected \u4F7F\u7528\u771F\u5B9E\u5F55\u97F3\u8EAB\u4EFD\u4E0E\u54C8\u5E0C\u3002\u9996\u6B21 needs_install \u65F6\u5148\u8BF4\u660E\u7EA63.8 GiB\u6A21\u578B\u3001\u5B9E\u9645\u68C0\u6D4B\u9009\u7528\u7684\u540E\u7AEF\u53CA\u5185\u5B58/\u663E\u5B58/\u78C1\u76D8\u8981\u6C42\uFF0C\u7528\u6237\u660E\u786E\u540C\u610F\u624D allow_install=true\u3002start \u8FD4\u56DE\u540E\u53F0\u4EFB\u52A1\uFF0C\u53CA\u65F6\u544A\u8BC9\u7528\u6237\u6B63\u5728\u5904\u7406\uFF0C\u4E0D\u5FAA\u73AF\u7B49\u5F85\uFF1B\u5B8C\u6210\u7684\u539F\u7A3F\u4FDD\u5B58\u5230\u539F\u5F55\u97F3\u6587\u4EF6\u5939\u3002stop \u4F20job\u505C\u6B62\u5E76\u4FDD\u7559\u65AD\u70B9\u3002",
    parameters: { action: { type: "string", enum: ["status", "check", "start", "stop"], required: true }, input: { type: "string", required: true, description: "JSON\u5BF9\u8C61\u3002status:{}\uFF1Bstart:{file,expected,allow_install\u53EF\u9009}\uFF1Bstop:{job}\u3002" } },
    output: output2,
    async execute(args, exec) {
      const input = JSON.parse(args.input);
      if (!input || Array.isArray(input) || typeof input !== "object") throw new Error("\u8F6C\u5199\u8F93\u5165\u5FC5\u987B\u662F\u5BF9\u8C61");
      if (config.bridge) return call("transcribe", { ...input, action: args.action, session: exec.agent?.session.id }, `${exec.agent?.session.id ?? "direct"}:${exec.callId}`, exec.signal);
      return new Promise((resolve3, reject) => {
        const child = execFile(python, [fileURLToPath(new URL("./transcription.py", import.meta.url)), config.data], { signal: exec.signal, timeout: 15e3, maxBuffer: 1024 * 1024, encoding: "utf8" }, (error, stdout) => {
          try {
            const reply = JSON.parse(stdout);
            if (error || !reply.ok) reject(new Error(reply.error || "\u8F6C\u5199\u8BF7\u6C42\u5931\u8D25"));
            else resolve3(reply.result);
          } catch (failure) {
            reject(failure);
          }
        });
        child.stdin.on("error", () => {
        });
        child.stdin.end(JSON.stringify({ action: args.action, args: input }));
      });
    }
  })));
  ctx.effect(() => ctx.tools.register(defineTool({
    name: "study_work",
    description: "\u5F00\u59CB\u6216\u7EE7\u7EED\u5B66\u4E60\u6D41\u7A0B\u3002\u5148\u8BFB\u53D6\u53EF\u6267\u884C\u64CD\u4F5C\uFF0C\u9009\u62E9\u6240\u9700\u64CD\u4F5C\u5E76\u4FDD\u5B58\u771F\u5B9E\u8F93\u5165\u548C\u7ED3\u679C\uFF1B\u51FA\u5377\u4E0D\u4EE3\u8868\u5B66\u751F\u5DF2\u4F5C\u7B54\u3002switch \u53EA\u5728\u7528\u6237\u8981\u6C42\u6539\u53D8\u6B63\u5728\u6267\u884C\u7684\u6D41\u7A0B\u65F6\u4F7F\u7528\u3002",
    parameters: {
      action: { type: "string", enum: ["start", "advance", "switch", "service"], required: true },
      input: { type: "string", required: true, description: "JSON \u5BF9\u8C61\u3002semester(\u53EF\u9009)\u5904\u7406\u5DF2\u51C6\u5907\u7684\u6307\u5B9A\u5B66\u671F\uFF1Bstart: flow,course(\u8BFE\u7A0B\u6D41\u7A0B\u9700\u8981),name\uFF1B\u5168\u5C40 daily/memory \u65E0\u9700\u8BFE\u7A0B\u3002\u5904\u7406\u5668\u5B57\u6BB5\u89C1 study_read \u8FD4\u56DE\u7684 implementation \u548C operations \u6587\u4EF6\uFF1Badvance: run,step,input\uFF1Bswitch: run,reason\u3002service: arguments \u53C2\u6570\u5217\u8868\uFF0C\u8C03\u7528\u73B0\u6709\u5B66\u4E60\u670D\u52A1\u5904\u7406\u4EFB\u52A1\u66F4\u65B0\u3001\u65E5\u671F\u3001\u6750\u6599\u3001\u4F5C\u7B54\u548C\u8BA1\u5212\uFF1B\u5148\u7528 [--help] \u6216\u5B50\u547D\u4EE4\u7684 --help \u67E5\u770B\u5B9E\u9645\u7528\u6CD5\uFF0C\u4E0D\u731C\u53C2\u6570\uFF0C\u4E0D\u9700\u8981\u7EC8\u7AEF\u3002\u6570\u636E\u5E93\u56FA\u5B9A\u4E3A\u9009\u5B9A\u5B66\u671F\uFF1B\u4E2D\u65AD\u540E\u5148\u6838\u5BF9\u5B9E\u9645\u8BB0\u5F55\uFF0C\u4E0D\u80FD\u91CD\u590D\u521B\u5EFA\u8BC1\u636E\u3002" }
    },
    output: output2,
    async execute(args, exec) {
      const input = JSON.parse(args.input);
      if (!input || Array.isArray(input) || typeof input !== "object") throw new Error("\u6B65\u9AA4\u8F93\u5165\u5FC5\u987B\u662F\u5BF9\u8C61");
      return call(
        args.action,
        { ...input, session: exec.agent?.session.id },
        `${exec.agent?.session.id ?? "direct"}:${exec.callId}`,
        exec.signal
      );
    }
  })));
  ctx.effect(() => ctx.tools.register(defineTool({
    name: "study_change",
    description: "\u6839\u636E\u7528\u6237\u8981\u6C42\u4FEE\u6539\u5B9E\u9645\u6D41\u7A0B\u3002preview \u4FDD\u5B58\u5E76\u9A8C\u8BC1\u4FEE\u6539\uFF0Capply \u91C7\u7528\u901A\u8FC7\u9A8C\u8BC1\u7684\u7248\u672C\uFF0Crollback \u6062\u590D\u4E0A\u4E2A\u7248\u672C\u3002\u8303\u56F4\u53EF\u4E3A\u4E00\u95E8\u8BFE\u7A0B\u6216\u901A\u7528\u6D41\u7A0B\uFF1B\u5DF2\u8FD0\u884C\u7684\u8BB0\u5F55\u4E0D\u4F1A\u81EA\u52A8\u6539\u5199\u3002handler \u4EC5\u5141\u8BB8\u7EAF JSON \u6570\u636E\u8F6C\u6362\u7684 run(input, context) \u51FD\u6570\uFF0C\u53EF\u7528\u6761\u4EF6\u3001\u7B97\u672F\u3001min/max/len \u7B49\u5185\u7F6E\u51FD\u6570\u53CA\u5BF9\u8C61 .get\uFF1B\u7981\u6B62\u5BFC\u5165\u3001\u6587\u4EF6/\u7F51\u7EDC\u8BBF\u95EE\u3001\u5C5E\u6027\u63A2\u67E5\u548C\u5FAA\u73AF\uFF0C\u5FC5\u987B\u5E26\u8F93\u5165\u548C\u9884\u671F\u7ED3\u679C tests\u3002",
    parameters: {
      action: { type: "string", enum: ["preview", "apply", "rollback"], required: true },
      input: { type: "string", required: true, description: "JSON \u5BF9\u8C61\uFF1Aflow,course(\u53EF\u9009)\uFF0Cscope=learning \u8868\u793A\u8DE8\u5B66\u671F\u901A\u7528\u6D41\u7A0B\uFF0C\u4E0D\u4E0E course \u540C\u7528\uFF1Bstudy_read.learning_flows \u8FD4\u56DE\u901A\u7528\u73B0\u884C\u7248\u672C\uFF0C\u7701\u7565scope\u53EA\u6539\u5F53\u524D\u5B66\u671F\u3002preview: reason,definition\uFF1Bapply: version,expected\uFF1Brollback: expected\u3002definition \u5305\u542B title \u548C steps\uFF0C\u6BCF\u6B65\u6709 id,kind,instructions\uFF0Cafter \u53EA\u58F0\u660E\u6A21\u578B\u64CD\u4F5C\u6240\u9700\u7684\u771F\u5B9E\u8BC1\u636E\u4F9D\u8D56\uFF1B\u5217\u8868\u987A\u5E8F\u4E0D\u6784\u6210\u4F9D\u8D56\uFF1B\u53EF\u52A0 handler Python \u4EE3\u7801 run(input,context) \u4E0E tests\u3002" }
    },
    output: output2,
    async execute(args, exec) {
      const input = JSON.parse(args.input);
      if (!input || Array.isArray(input) || typeof input !== "object") throw new Error("\u6D41\u7A0B\u4FEE\u6539\u5FC5\u987B\u662F\u5BF9\u8C61");
      return call(
        "change",
        { ...input, action: args.action },
        `${exec.agent?.session.id ?? "direct"}:${exec.callId}`,
        exec.signal
      );
    }
  })));
  ctx.effect(() => ctx.systemPrompt.section({
    name: "study",
    order: 0,
    interpolate: false,
    text: readFileSync(fileURLToPath(new URL(config.host ? "./HOST_ASSISTANT.md" : "./ASSISTANT.md", import.meta.url)), "utf8") + "\n\u4F60\u662F\u5B66\u4E60\u52A9\u624B\u3002\u8BF4\u8BDD\u7B80\u6D01\u81EA\u7136\uFF0C\u5148\u770B\u6750\u6599\u548C\u5B66\u751F\u8981\u6C42\uFF0C\u518D\u51B3\u5B9A\u600E\u6837\u5E2E\u52A9\u5B66\u4E60\u3002\u4F7F\u7528 study \u5DE5\u5177\u8BFB\u53D6\u3001\u6267\u884C\u548C\u4FEE\u6539\u5B9E\u9645\u6D41\u7A0B\uFF1B\u4E0D\u77E5\u9053\u7684\u4E8B\u5B9E\u4FDD\u6301\u672A\u77E5\uFF0C\u4E0D\u4ECE\u6587\u4EF6\u540D\u63A8\u65AD\u6750\u6599\u7C7B\u578B\u3002\u8BFB\u7B14\u8BB0\u4E0E\u7EC3\u4E60\u7531\u7528\u6237\u81EA\u4E3B\u5B89\u6392\uFF0C\u4E0D\u80FD\u9ED8\u8BA4\u4E24\u8005\u6709\u5148\u540E\uFF0C\u4E0D\u81EA\u52A8\u6DFB\u52A0\u7528\u6237\u4EFB\u52A1\u4F9D\u8D56\u3002\u6D41\u7A0B\u7EA6\u675F\u6A21\u578B\u4FDD\u5B58\u4F9D\u636E\u4E0E\u6838\u5BF9\u4E8B\u5B9E\u3002\u7528\u6237\u53EA\u9700\u8868\u8FBE\u5B66\u4E60\u8981\u6C42\uFF0C\u4E0D\u8BA9\u7528\u6237\u586B\u5199\u6D41\u7A0B\u914D\u7F6E\u3001\u7248\u672C\u6216\u6838\u9A8C\u5B57\u6BB5\u3002\u6280\u672F\u8BB0\u5F55\u7559\u5728\u5DE5\u5177\u5185\u90E8\uFF0C\u5411\u7528\u6237\u8BF4\u660E\u5B66\u4E60\u4E0A\u7684\u6539\u53D8\u4E0E\u4E0B\u4E00\u6B65\u3002\u4FEE\u6539\u6D41\u7A0B\u987B\u4F9D\u636E\u7528\u6237\u8981\u6C42\uFF0C\u9A8C\u8BC1\u5931\u8D25\u5148\u4FEE\u6B63\uFF1B\u4FDD\u7559\u539F\u59CB\u4F5C\u7B54\uFF0C\u4E0D\u628A\u8BB2\u89E3\u3001\u7B14\u8BB0\u6216\u53D1\u5377\u5F53\u4F5C\u638C\u63E1\u3002"
  }));
  ctx.effect(() => ctx.tools.register(defineTool({
    name: "study_desktop",
    description: "\u8BFB\u53D6\u4E0E\u64CD\u4F5C\u7528\u6237\u7684\u771F\u5B9E\u684C\u9762\u3002semester(action=list/create/rename/archive/reopen/remove/restore/select,name,semester,expected,reason) \u7531\u6A21\u578B\u4F9D\u7528\u6237\u610F\u56FE\u7BA1\u7406\u5B66\u671F\uFF1Bcreate\u53EA\u51C6\u5907\u65B0\u5B66\u671F\uFF0Cselect\u5728\u672C\u6B21\u5904\u7406\u7ED3\u675F\u540E\u5207\u6362\uFF1Bexpected\u4F7F\u7528semester list\u7684version\u3002\u5176\u4ED6\u684C\u9762\u64CD\u4F5C\u53EF\u4F20semester\u5904\u7406\u6307\u5B9A\u5B66\u671F\u3002carry(file,target,reason,folder\u53EF\u9009) \u4FDD\u7559\u539F\u4EF6\u5E76\u628A\u81EA\u4E3B\u9009\u4E2D\u7684\u8D44\u6599\u590D\u5236\u5230\u53E6\u4E00\u5B66\u671F\uFF0C\u4FDD\u5B58\u6765\u6E90\uFF0C\u4E0D\u590D\u5236\u4EFB\u52A1\u6216\u638C\u63E1\u7ED3\u8BBA\u3002course(code,name) \u5EFA\u7ACB\u8BFE\u7A0B\u4E0E\u6587\u4EF6\u5939\uFF0C\u4E0D\u51B3\u5B9A\u5B66\u4E60\u65B9\u6CD5\u3002agreement(scope=learning/semester/course,key,text,reason,expected,course\u53EF\u9009) \u4FDD\u5B58\u6216 remove=true \u505C\u7528\u7EA6\u5B9A\uFF1Blearning\u8DE8\u5B66\u671F\uFF0Csemester\u53EA\u5728\u5F53\u524D\u5B66\u671F\uFF0Ccourse\u53EA\u5728\u672C\u8BFE\u7A0B\uFF1Bexpected \u4E3A list.agreements \u5BF9\u5E94\u8303\u56F4\u7684 version\u3002\u5404\u8303\u56F4\u4F5C\u4E3A\u53C2\u8003\u8FD4\u56DE\uFF0C\u5F53\u524D\u7528\u6237\u610F\u613F\u4F18\u5148\uFF0C\u4E0D\u80FD\u628A\u8BB0\u5FC6\u4F5C\u4E3A\u81EA\u52A8\u5B89\u6392\u7528\u6237\u7684\u547D\u4EE4\uFF1B\u5386\u53F2\u4FDD\u7559\u3002remove_file/restore_file(file,expected) \u53EF\u6062\u590D\u5730\u79FB\u9664\u6587\u4EF6\uFF0C\u4E0D\u5220\u8BC1\u636E\u3002list/read \u83B7\u53D6\u6587\u4EF6\u8EAB\u4EFD\u4E0E\u539F\u6587\uFF1BPDF read \u6BCF\u6B21\u6700\u591A100000\u5B57\uFF0C\u8FD4\u56DE characters\u3001truncated \u4E0E next_offset\uFF0C\u7528 offset/limit/expected \u7EE7\u7EED\uFF0C\u672A\u8BFB\u5B8C\u4E0D\u80FD\u5F53\u4F5C\u5168\u6587\uFF1B\u6309\u9875\u5B9A\u4F4D\u7528 study_textbook \u6216 page\uFF1Bcreate \u4FDD\u5B58\u6587\u4EF6\uFF08kind=practice \u53EF\u663E\u793A\u4F5C\u7B54\u6846\uFF0Csources \u8BB0\u5F55\u771F\u5B9E\u9898\u6E90\uFF09\uFF1Bwrite/restore \u5FC5\u987B\u5E26\u5F53\u524D sha256 \u7684 expected\uFF1Bfolder(name,parent,category\u53EF\u9009) \u5EFA\u5B50\u6587\u4EF6\u5939\uFF0Cparent \u4F7F\u7528\u771F\u5B9E\u4E0A\u7EA7\u8EAB\u4EFD\u3002\u751F\u6210\u7B14\u8BB0\u548C\u7EC3\u4E60 create \u5FC5\u987B\u660E\u786E course \u4E0E category\uFF08notes/practice/materials/assignments\uFF09\u6216\u5236\u4F5C\u4F9D\u636E\uFF0C\u5F52\u8BFE\u7A0B\u5185\u5206\u7C7B\uFF0C\u4E0D\u5728\u6839\u76EE\u5F55\u65B0\u5EFA\u672C\u6B21\u6210\u679C\uFF1Bmetadata \u79FB\u52A8/\u91CD\u547D\u540D\u9700 list \u7684 version \u4F5C expected\u3002publish \u6839\u636E\u5DF2\u8BFB\u5185\u5BB9\u5C06\u6587\u4EF6\u663E\u5F0F\u5173\u8054\u5230\u5DF2\u6CE8\u518C course \u7684 path\uFF08\u8BFE\u7A0B\u5185\u76F8\u5BF9\u4F4D\u7F6E\uFF09\uFF0C\u5FC5\u987B\u8BF4\u660E reason\uFF1B\u539F\u6750\u6599\u626B\u63CF\u548C\u56DE\u6267\u4F7F\u7528\u6B64\u771F\u5B9E\u8BFE\u7A0B\u6587\u4EF6\u3002\u8BFE\u7A0B\u6587\u4EF6\u5728\u684C\u9762\u4E4B\u5916\u88AB\u4FEE\u6539\u65F6\u4FDD\u7559\u4E24\u4EFD\u5E76\u62A5\u544A\u51B2\u7A81\u3002task \u5EFA\u5B66\u751F\u4EFB\u52A1\uFF0C\u65E0\u9ED8\u8BA4\u524D\u540E\u4F9D\u8D56\uFF1B\u4E0D\u81EA\u52A8\u52FE\u9009\u6216\u767B\u8BB0\u638C\u63E1\u3002compile \u7F16\u8BD1 .tex\uFF0Cexpected \u4E3A\u6E90\u7801\u54C8\u5E0C\uFF0C\u6B63\u5F0F\u5B66\u4E60\u4EA4\u4ED8\u5173\u8054\u5199\u4F5C\u524D\u5DF2\u4FDD\u5B58\u7684 preparation\uFF1Bassets \u4E3A\u660E\u786E\u9644\u4EF6 {file,path}\uFF1B\u5931\u8D25\u8FD4\u56DE\u65E5\u5FD7\u5E76\u4FDD\u7559\u4E0A\u7248 PDF\u3002page \u4F20 PDF file/page \u8FD4\u56DE\u5B9E\u9645\u9875\u9762\u56FE\u50CF\uFF1B\u9010\u9875\u9605\u8BFB\u56FE\u50CF\u5E76\u6838\u5BF9\u6765\u6E90\u540E review\uFF0C\u7F16\u8BD1\u6210\u529F\u4E0D\u662F\u9A8C\u6536\u3002\u6B63\u5F0F\u7B14\u8BB0/\u7EC3\u4E60\u987B\u5148 prepare(course,kind,brief) \u4FDD\u5B58\u771F\u5B9E\u8349\u6848\uFF0Ccreate \u5E26\u8FD4\u56DE\u7684 preparation \u8EAB\u4EFD\uFF1Breview \u8FD8\u987B kind=note/worksheet\u3001course\u3001brief\uFF0C\u5E76\u9075\u5B88 study_read \u8FD4\u56DE\u7684\u4EA4\u4ED8\u6807\u51C6\u3002history/restore \u53EF\u6062\u590D\u6587\u4EF6\u3002",
    parameters: {
      action: { type: "string", enum: ["semester", "carry", "course", "agreement", "remove_file", "restore_file", "list", "read", "prepare", "publish", "create", "write", "restore", "folder", "metadata", "task", "compile", "page", "review", "history"], required: true },
      input: { type: "string", required: true, description: "JSON \u5BF9\u8C61\u3002\u6587\u4EF6\u64CD\u4F5C\u4F7F\u7528 file \u8EAB\u4EFD\uFF1Bcreate: name,text,mime,folder(\u53EF\u9009),kind,sources\uFF1Bwrite:file,text,expected\uFF1Bcompile:file,expected,assets(\u53EF\u9009)\u3002page:file,page\u3002review:file,sha256,pages:[{page,status:passed,summary:\u5177\u4F53\u6838\u9A8C}],checks:{source_scope,layout,...\u4EA4\u4ED8\u6838\u9A8C}\u3002\u6E90\u7801\u6216 PDF \u53D8\u5316\u4F1A\u62D2\u7EDD\u65E7\u6838\u9A8C\u3002task:title,file(\u53EF\u9009),course(\u5DF2\u6CE8\u518C\u53EF\u9009),date(\u5B89\u6392\u65E5\u671FYYYY-MM-DD\uFF0Cnull\u8868\u793A\u6682\u4E0D\u5B89\u6392\uFF0C\u7701\u7565\u4E3A\u4ECA\u5929),due_at(\u6B63\u5F0F\u622A\u6B62\uFF0C\u53EF\u9009)\u3002\u64CD\u4F5C\u5B9E\u73B0\u4F4D\u4E8E packages/agent/workspace.py\uFF0C\u53EF\u901A\u8FC7 study_read source=desktop \u67E5\u9605\u3002" }
    },
    output: {
      schema: { type: "object", additionalProperties: true },
      render: (_args, value) => {
        const { images, ...record } = value;
        return [
          { type: "text", text: JSON.stringify(record) },
          ...(images ?? []).map((img) => ({ type: "image", attachment: img }))
        ];
      }
    },
    async execute(args, exec) {
      const input = JSON.parse(args.input);
      if (!input || Array.isArray(input) || typeof input !== "object") throw new Error("\u684C\u9762\u8F93\u5165\u5FC5\u987B\u662F\u5BF9\u8C61");
      if (args.action === "page" || args.action === "review") await assertImageCapableRoute(ctx, exec, "PDF \u9875\u9762");
      const result = await call(args.action, { ...input, session: exec.agent?.session.id }, `${exec.agent?.session.id ?? "direct"}:${exec.callId}`, exec.signal, true);
      if (result.images) {
        await assertImageCapableRoute(ctx, exec, "\u684C\u9762\u56FE\u50CF");
        if (!ctx.get("attachments")) throw new Error("\u9875\u9762\u56FE\u50CF\u670D\u52A1\u4E0D\u53EF\u7528");
        result.images = await admitEncodedImages(ctx.attachments, result.images.map((img) => ({ data: img.data, mediaType: img.mimeType })));
      }
      return result;
    }
  })));
  ctx.effect(() => ctx.systemPrompt.section({
    name: "desktop",
    order: 1,
    interpolate: false,
    text: (config.host ? "\u5B66\u4E60\u6587\u4EF6\u7531 study_desktop \u4FDD\u5B58\uFF0C\u8BFE\u7A0B\u6210\u679C\u53D1\u5E03\u5230\u5BBF\u4E3B\u5DE5\u4F5C\u533A\u3002" : "\u684C\u9762\u6587\u4EF6\u7531 study_desktop \u4E0E\u7F51\u9875\u5171\u540C\u4FDD\u5B58\u3002") + "\u7528\u6237\u5173\u8054\u7684\u662F\u6587\u4EF6\u8EAB\u4EFD\u548C\u5F53\u524D\u54C8\u5E0C\uFF0C\u5FC5\u987B read \u5B9E\u9645\u5185\u5BB9\uFF1B\u9009\u533A\u4E0E\u4F5C\u7B54\u662F\u7528\u6237\u63D0\u4F9B\u7684\u539F\u6587\uFF0C\u4E0D\u66FF\u4EE3\u5B8C\u6574\u9898\u76EE\u3002\u751F\u6210\u7EC3\u4E60\u987B\u4F9D\u636E\u5DF2\u8BFB\u6750\u6599\uFF0C\u4E0D\u4F7F\u7528\u56FA\u5B9A\u793A\u4F8B\u5145\u5F53\u771F\u5B9E\u51FA\u9898\uFF1B\u7ED3\u679C\u5FC5\u987B create/write \u4FDD\u5B58\uFF0C\u4E0D\u80FD\u53EA\u5728\u56DE\u590D\u58F0\u79F0\u5B8C\u6210\u3002\u4FEE\u6539 LaTeX \u540E compile\uFF0C\u518D\u9010\u9875 page \u770B\u5B9E\u9645\u56FE\u50CF\uFF0C\u6838\u5BF9\u6765\u6E90\u548C\u7248\u5F0F\u540E\u624D review\uFF1B\u5931\u8D25\u5148\u6839\u636E\u65E5\u5FD7\u4FEE\u6B63\u3002\u7B14\u8BB0\u4E0E\u7EC3\u4E60\u5206\u522B\u9075\u5B88 study-notes\u3001study-worksheets \u7684\u8D28\u91CF\u8981\u6C42\uFF1B\u7531\u4F60\u8D1F\u8D23\u5199\u4F5C\u3001\u7F16\u8BD1\u4E0E\u6838\u9A8C\uFF0C\u4E0D\u56E0\u6587\u6863\u7C7B\u578B\u5F3A\u5236\u7528\u6237\u9605\u8BFB\u6216\u7EC3\u4E60\u987A\u5E8F\u3002\u4EFB\u52A1\u5B8C\u6210\u3001\u6A21\u578B\u6279\u6539\u3001\u77E5\u8BC6\u638C\u63E1\u4ECD\u9700\u73B0\u6709 study \u670D\u52A1\u7684\u771F\u5B9E\u8BC1\u636E\uFF1B\u684C\u9762\u52FE\u9009\u4EC5\u662F\u5B66\u751F\u786E\u8BA4\u5B8C\u6210\u3002"
  }));
  const supplied = /* @__PURE__ */ new Map();
  ctx.effect(() => () => supplied.clear());
  if (config.host) return;
  ctx.on("agent/pre-step", async ({ agent, signal }, next) => {
    const decision = await next();
    if (decision.kind === "reject" || signal.aborted) return decision;
    const state = await call("read", { session: agent.session.id }, "", signal);
    const context = { pending: state.pending ?? [], memory: state.memory?.content ?? "", recovery: state.recovery_warnings ?? [], agreements: state.agreements?.references ?? [] };
    if (!context.pending.length && !context.memory && !context.recovery.length && !context.agreements.length) return decision;
    const content = JSON.stringify(context);
    if (supplied.get(agent.session.id) === content) return decision;
    supplied.set(agent.session.id, content);
    return { ...decision, messages: [...decision.messages, createUserMessage({
      source: { kind: "study" },
      content: [{ type: "text", text: `\u5F53\u524D\u5B66\u4E60\u6D41\u7A0B\uFF08\u5185\u90E8\u4E0A\u4E0B\u6587\uFF09\uFF1A${content}` }]
    })] };
  });
}

// src/projects.ts
import { readFileSync as readFileSync2, realpathSync, existsSync } from "node:fs";
import { dirname, isAbsolute, relative, resolve } from "node:path";
var BINDING_FILE = ".study-plugin.json";
function inside(root, target) {
  const path = relative(root, target);
  return path === "" || !path.startsWith(".." + (process.platform === "win32" ? "\\" : "/")) && path !== ".." && !isAbsolute(path);
}
function projectFor(cwd) {
  if (!cwd || !isAbsolute(cwd)) return;
  let root = realpathSync(cwd);
  for (; ; ) {
    const file = resolve(root, BINDING_FILE);
    if (existsSync(file)) {
      const config = JSON.parse(readFileSync2(file, "utf8"));
      if (config.version !== 1 || typeof config.enabled !== "boolean") throw new Error("\u5B66\u4E60\u9879\u76EE\u914D\u7F6E\u683C\u5F0F\u65E0\u6548");
      if (!config.enabled) return;
      if (typeof config.data !== "string" || isAbsolute(config.data)) throw new Error("\u5B66\u4E60\u6570\u636E\u76EE\u5F55\u5FC5\u987B\u662F\u9879\u76EE\u5185\u7684\u76F8\u5BF9\u8DEF\u5F84");
      const requested = resolve(root, config.data);
      if (!inside(root, requested)) throw new Error("\u5B66\u4E60\u6570\u636E\u76EE\u5F55\u4E0D\u80FD\u4F4D\u4E8E\u9879\u76EE\u4E4B\u5916");
      let ancestor = requested;
      while (!existsSync(ancestor)) ancestor = dirname(ancestor);
      if (!inside(root, realpathSync(ancestor))) throw new Error("\u5B66\u4E60\u6570\u636E\u76EE\u5F55\u4E0D\u80FD\u901A\u8FC7\u7B26\u53F7\u94FE\u63A5\u6307\u5411\u9879\u76EE\u4E4B\u5916");
      const data = existsSync(requested) ? realpathSync(requested) : requested;
      return { root, data, calendar: config.calendar };
    }
    const parent = dirname(root);
    if (parent === root) return;
    root = parent;
  }
}

// src/process.ts
import { execFile as execFile2 } from "node:child_process";
function jsonProcess(python, script2, root, request, signal, env = process.env, timeout = 12e4) {
  return new Promise((resolve3, reject) => {
    const child = execFile2(python, [script2, root], { signal, timeout, maxBuffer: 32 * 1024 * 1024, encoding: "utf8", env: { ...env, PYTHONUTF8: "1", PYTHONDONTWRITEBYTECODE: "1" } }, (error, stdout, stderr) => {
      if (signal?.aborted) {
        reject(signal.reason);
        return;
      }
      let response;
      try {
        response = JSON.parse(stdout);
      } catch {
        reject(new Error(stderr.trim() || error?.message || "\u5B66\u4E60\u540E\u53F0\u8FD4\u56DE\u4E86\u65E0\u6548JSON"));
        return;
      }
      if (!response.ok) {
        reject(new Error(response.error));
        return;
      }
      if (error) {
        reject(error);
        return;
      }
      resolve3(response.result);
    });
    child.stdin.on("error", () => {
    });
    child.stdin.end(JSON.stringify(request));
  });
}

// src/transcription-host.ts
import { spawn } from "node:child_process";
import { existsSync as existsSync2, mkdirSync, readFileSync as readFileSync3, openSync, closeSync } from "node:fs";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
var TranscriptionHosts = class {
  constructor(python, script2) {
    this.python = python;
    this.script = script2;
  }
  python;
  script;
  hosts = /* @__PURE__ */ new Map();
  config(data) {
    return this.hosts.get(data)?.config;
  }
  async start(data, signal) {
    const python = await this.python();
    const current = this.hosts.get(data);
    if (current) return current.ready;
    const directory = join(data, ".transcription");
    mkdirSync(directory, { recursive: true });
    const config = join(directory, `connection-${randomUUID()}.json`);
    const log = join(directory, "host.log");
    const fd = openSync(log, "a", 384);
    const child = spawn(python, [this.script, "--bind", "127.0.0.1", "--port", "0", "--data", join(directory, "host"), "--config", config], { stdio: ["ignore", fd, fd], env: { ...process.env, PYTHONUTF8: "1" } });
    closeSync(fd);
    let failure;
    child.on("error", (error) => {
      failure = error;
    });
    const ready = (async () => {
      for (let attempt = 0; attempt < 200; attempt++) {
        signal?.throwIfAborted();
        if (existsSync2(config)) {
          const value = JSON.parse(readFileSync3(config, "utf8"));
          if (!value.url?.startsWith("http://127.0.0.1:")) throw new Error("\u8F6C\u5199\u540E\u53F0\u6CA1\u6709\u7ED1\u5B9A\u672C\u673A\u56DE\u73AF\u5730\u5740");
          return config;
        }
        if (failure || child.exitCode !== null) throw new Error(`\u8F6C\u5199\u540E\u53F0\u672A\u542F\u52A8\uFF1B\u8BF7\u67E5\u770B ${log}\uFF08\u9700\u8981 Python 3.12 \u548C\u53D7\u652F\u6301 GPU\uFF09`);
        await new Promise((resolve3) => setTimeout(resolve3, 50));
      }
      throw new Error(`\u8F6C\u5199\u540E\u53F0\u542F\u52A8\u8D85\u65F6\uFF1B\u8BF7\u67E5\u770B ${log}`);
    })().catch((error) => {
      child.kill();
      this.hosts.delete(data);
      throw error;
    });
    this.hosts.set(data, { process: child, config, ready });
    child.on("exit", () => {
      if (this.hosts.get(data)?.process === child) this.hosts.delete(data);
    });
    return ready;
  }
  async dispose() {
    const children = [...this.hosts.values()].map((v) => v.process);
    this.hosts.clear();
    await Promise.all(children.map((child) => new Promise((resolve3) => {
      if (child.exitCode !== null) {
        resolve3();
        return;
      }
      const timer = setTimeout(() => {
        child.kill("SIGKILL");
        resolve3();
      }, 4e3);
      child.once("exit", () => {
        clearTimeout(timer);
        resolve3();
      });
      child.kill("SIGTERM");
    })));
  }
};

// src/environment.ts
import { existsSync as existsSync3 } from "node:fs";
import { join as join2 } from "node:path";
import { homedir } from "node:os";
import { fileURLToPath as fileURLToPath2 } from "node:url";
var script = fileURLToPath2(new URL("../packages/agent/plugin_dependencies.py", import.meta.url));
function installedHarnessPython() {
  const root = join2(process.env.DSH_HOME || join2(homedir(), ".dsh"), "dsh-runtimes", "dsh-primary-runtime", "dependencies", "python");
  const python = process.platform === "win32" ? join2(root, "python.exe") : join2(root, "bin", "python3");
  return existsSync3(python) ? python : void 0;
}
var PythonEnvironment = class {
  constructor(configured) {
    this.configured = configured;
  }
  configured;
  python;
  preparation;
  selection;
  get current() {
    return this.python;
  }
  async resolve(load) {
    if (load && (!this.configured || this.configured === "auto")) {
      this.selection ??= load().then((python) => {
        this.python = python;
        this.preparation = void 0;
        return python;
      }).catch((error) => {
        this.selection = void 0;
        throw error;
      });
      return this.selection;
    }
    if (this.python) return this.python;
    const selected = this.configured && this.configured !== "auto" ? this.configured : installedHarnessPython() ?? (process.platform === "win32" ? "python" : "python3");
    this.python = selected;
    return selected;
  }
  async check(signal) {
    return jsonProcess(await this.resolve(), script, ".", { action: "check" }, signal);
  }
  async ensure(signal) {
    if (!this.preparation) {
      this.preparation = (async () => jsonProcess(await this.resolve(), script, ".", { action: "install" }, signal, process.env, 36e4))().catch((error) => {
        this.preparation = void 0;
        throw error;
      });
    }
    return this.preparation;
  }
};

// src/index.ts
var name = "study-projects";
var inject = ["agents", "tools", "systemPrompt", "sandboxPolicy"];
var Config = z.object({ python: z.string().default("auto") });
var bridge = fileURLToPath3(new URL("../packages/agent/plugin_bridge.py", import.meta.url));
var host = fileURLToPath3(new URL("../packages/transcription/host.py", import.meta.url));
var output = { schema: { type: "object", additionalProperties: true }, render: (_args, value) => [{ type: "text", text: JSON.stringify(value) }] };
function apply2(ctx, config = {}) {
  const environment = new PythonEnvironment(config.python);
  const hosts = new TranscriptionHosts(() => environment.resolve(), host);
  const mounted = /* @__PURE__ */ new Map();
  async function selectPython(exec) {
    return environment.resolve(ctx.tools.get("load_workspace_dependencies", exec.agent) ? async () => {
      const result = await ctx.tools.execute({
        name: "load_workspace_dependencies",
        arguments: {},
        agent: exec.agent,
        parent: exec.token,
        rootCallId: exec.rootCallId,
        callId: ToolCallId(`${exec.callId}:python`),
        signal: exec.signal
      });
      if (result.isError) throw new Error(result.content.map((block) => block.type === "text" ? block.text : "").join("\n"));
      const value = result.value;
      if (!value.python || !isAbsolute2(value.python) || !existsSync4(value.python)) throw new Error("Harness \u6CA1\u6709\u8FD4\u56DE\u53EF\u7528\u7684 Python \u89E3\u91CA\u5668");
      return value.python;
    } : void 0);
  }
  async function authorize(exec) {
    const policy = ctx.sandboxPolicy.resolve(exec.agent ? { session: exec.agent.session } : {});
    await approveEscalation(
      {
        requestedMode: "danger-full-access",
        effectiveMode: policy.mode,
        subject: "\u5B66\u4E60\u5DE5\u5177",
        justification: `${exec.name} \u4F7F\u7528\u672C\u673A Python \u5904\u7406\u5B66\u4E60\u6570\u636E\uFF1B\u5199\u5165\u3001\u7F16\u8BD1\u3001\u65E5\u5386\u548C\u8F6C\u5199\u9075\u5B88\u672C\u6B21\u5BBF\u4E3B\u5BA1\u6279\u3002`
      },
      { approver: ctx.get("approval"), agent: exec.agent, callId: exec.callId, toolName: exec.name, signal: exec.signal }
    );
    exec.signal.throwIfAborted();
  }
  async function mount(agent) {
    const project = projectFor(agent.session.header.cwd);
    const old = mounted.get(agent);
    if (old && JSON.stringify(old.project) === JSON.stringify(project)) return;
    if (old) {
      mounted.delete(agent);
      await old.scope.dispose();
    }
    if (!project) return;
    const scope = createScope(ctx, agent);
    mounted.set(agent, { project, scope });
    async function call(action, args, id, signal, desktop = false) {
      const python = await environment.resolve();
      const input = args;
      let connection = hosts.config(project.data);
      if (action === "transcribe" && input.action === "start") {
        const check = await jsonProcess(python, bridge, project.data, { action: "transcribe", args: { action: "check", session: agent.session.id } }, signal, { ...process.env, STUDY_PDFIUM: "1", STUDY_TRANSCRIBE_CONFIG: connection ?? "" });
        if (!check.eligible) return { blocked: true, resources: check };
        connection = await hosts.start(project.data, signal);
      }
      return jsonProcess(
        python,
        bridge,
        project.data,
        { action, args: { ...input, session: agent.session.id }, id, desktop, project: project.root, calendar: project.calendar },
        signal,
        { ...process.env, STUDY_PDFIUM: "1", STUDY_TRANSCRIBE_CONFIG: connection ?? "" }
      );
    }
    apply(scope.ctx, { data: project.data, bridge: call, host: true });
    scope.ctx.on("tools/execute", async (exec, next) => {
      if (exec.name.startsWith("study_") && exec.name !== "study_project") {
        await authorize(exec);
        await selectPython(exec);
        if (exec.name !== "study_environment") await environment.ensure(exec.signal);
      }
      return next();
    });
    const extraTools = [
      { name: "study_environment", description: "check \u67E5\u770B\u5B66\u4E60\u63D2\u4EF6\u4F9D\u8D56\u548C\u6B63\u5728\u4F7F\u7528\u7684 Python\uFF1Binstall \u590D\u7528 Harness \u7684 Python\uFF0C\u53EA\u5B89\u88C5\u7F3A\u5C11\u7684\u4F9D\u8D56\uFF0C\u5DF2\u6709\u5305\u4E0D\u5347\u7EA7\u3002\u6B63\u5E38\u4F7F\u7528\u5B66\u4E60\u5DE5\u5177\u65F6\u4E5F\u4F1A\u81EA\u52A8\u8865\u88C5\u3002\u6B64\u64CD\u4F5C\u4E0D\u4E0B\u8F7D\u5F55\u97F3\u6A21\u578B\u3002", action: "environment", parameters: { action: { type: "string", enum: ["check", "install"], required: true } } },
      { name: "study_import", description: "\u5BFC\u5165\u672C\u9879\u76EE\u5DF2\u6709\u7684\u8BFE\u7A0B\u6750\u6599\u6216\u5F55\u97F3\uFF0C\u4FDD\u7559\u539F\u4EF6\u3001\u6587\u4EF6\u8EAB\u4EFD\u548C\u7248\u672C\u3002\u540C\u4E00\u6587\u4EF6\u540C\u4E00\u7248\u672C\u91CD\u590D\u5BFC\u5165\u590D\u7528\u8BB0\u5F55\u3002path \u662F\u9879\u76EE\u5185\u8DEF\u5F84\uFF1B\u4E5F\u53EF\u4F20\u5F53\u524D\u5BF9\u8BDD\u6587\u4EF6\u9644\u4EF6\u7684 attachment_id\u3002expected \u53EF\u6838\u5BF9\u6E90\u6587\u4EF6 sha256\u3002", action: "import", parameters: { path: { type: "string" }, attachment_id: { type: "string" }, expected: { type: "string" } } },
      { name: "study_context", description: "\u8BFB\u53D6\u672C\u9879\u76EE\u5171\u4EAB\u72B6\u6001\uFF0C\u6216 remember \u4FDD\u5B58\u5176\u4ED6\u5BF9\u8BDD\u4E5F\u80FD\u8BFB\u5230\u7684\u6750\u6599\u6458\u8981\u3002summary \u4EC5\u4FDD\u5B58\u5B9E\u9645\u8BFB\u8FC7\u7684\u5185\u5BB9\uFF1Bsources:[{path,sha256}] \u4FDD\u7559\u5B66\u671F\u5185\u6765\u6E90\uFF0C\u6E90\u6587\u4EF6\u53D8\u5316\u65F6\u6458\u8981\u6807\u4E3A\u8FC7\u671F\u3002\u4FDD\u5B58\u987B\u4F20\u8BFB\u53D6\u7ED3\u679C\u7684 expected \u7248\u672C\u3002\u5B66\u4E60\u7EA6\u5B9A\u8BF7\u7528 study_desktop agreement\u3002", action: "context", parameters: { action: { type: "string", enum: ["read", "remember"], required: true }, input: { type: "string", required: true, description: "JSON\u5BF9\u8C61\u3002read:{}\uFF1Bremember:{key,summary,sources:[{path,sha256}],expected}\u3002" } } },
      { name: "study_calendar", description: "\u67E5\u8BE2\u6216\u7BA1\u7406\u5B9E\u9645\u5916\u90E8\u65E5\u5386\u3002\u5148 status/calendars \u6838\u5BF9\u8FDE\u63A5\uFF0Clist/read \u6838\u5BF9\u771F\u5B9E\u4E8B\u4EF6\uFF1Bcreate/update/delete \u53EA\u4F9D\u7528\u6237\u8981\u6C42\u6267\u884C\u3002\u5199\u64CD\u4F5C\u4E0D\u8BC1\u660E\u5B66\u4E60\u4EFB\u52A1\u5B8C\u6210\u3002update/delete \u5FC5\u987B\u4F7F\u7528\u8BFB\u53D6\u7ED3\u679C\u7684 expected\uFF1B\u4EC5\u4FEE\u6539\u6307\u5B9A\u4E8B\u4EF6\uFF0C\u91CD\u590D\u7CFB\u5217\u4E0D\u652F\u6301\u6279\u91CF\u6539\u5199\u3002\u672A\u914D\u7F6E\u6216\u8BBF\u95EE\u5931\u8D25\u8981\u5982\u5B9E\u8BF4\u660E\u3002", action: "calendar", parameters: { action: { type: "string", enum: ["status", "calendars", "list", "read", "create", "update", "delete"], required: true }, input: { type: "string", required: true, description: "JSON\u5BF9\u8C61\u3002list:{start,end}\uFF08\u5E26\u65F6\u533AISO8601\uFF09\uFF1Bread:{id}\uFF1Bcreate:{title,start,end,all_day?,notes?,location?,task?}\uFF1Bupdate:{id,expected,title,start,end,all_day?,notes?,location?}\uFF1Bdelete:{id,expected}\u3002\u5168\u5929\u4E8B\u4EF6\u4F7F\u7528YYYY-MM-DD\uFF0Cend\u4E0D\u542B\u8FB9\u754C\u3002" } } }
    ];
    for (const tool of extraTools) {
      scope.ctx.effect(() => scope.ctx.tools.register(defineTool2({
        name: tool.name,
        description: tool.description,
        parameters: tool.parameters,
        output,
        async execute(args, exec) {
          if (tool.action === "environment") return args.action === "install" ? environment.ensure(exec.signal) : environment.check(exec.signal);
          if (tool.action === "import" && args.attachment_id) {
            if (args.path) throw new Error("\u8BF7\u63D0\u4F9B\u6587\u4EF6\u8DEF\u5F84\u6216\u9644\u4EF6\u8EAB\u4EFD\u5176\u4E2D\u4E4B\u4E00");
            const ref = agent.session.deriveMessages().flatMap((message) => message.role === "user" ? message.content : []).find((block) => block.type === "file" && block.attachment.attachmentId === args.attachment_id);
            if (!ref || ref.type !== "file") throw new Error("\u6B64\u9644\u4EF6\u4E0D\u5C5E\u4E8E\u5F53\u524D\u5BF9\u8BDD\uFF0C\u8BF7\u4F7F\u7528\u5B9E\u9645\u4E0A\u4F20\u7684\u6587\u4EF6\u8EAB\u4EFD");
            const store = ctx.get("attachments");
            if (!store) throw new Error("\u5BBF\u4E3B\u9644\u4EF6\u670D\u52A1\u4E0D\u53EF\u7528");
            if (ref.attachment.bytes > 200 * 1024 * 1024) throw new Error("\u9644\u4EF6\u8D85\u8FC7\u652F\u6301\u7684\u5927\u5C0F");
            const chunks = [];
            let size = 0;
            for await (const chunk of store.readFileStream(ref.attachment, exec.signal)) {
              size += chunk.byteLength;
              if (size > 200 * 1024 * 1024) throw new Error("\u9644\u4EF6\u8D85\u8FC7\u652F\u6301\u7684\u5927\u5C0F");
              chunks.push(Buffer.from(chunk));
            }
            return call("attachment_import", { name: ref.attachment.name, attachment_id: ref.attachment.attachmentId, data: Buffer.concat(chunks).toString("base64"), expected: args.expected }, `${agent.session.id}:${exec.callId}`, exec.signal);
          }
          const input = "input" in args ? JSON.parse(args.input) : args;
          if (!input || Array.isArray(input) || typeof input !== "object") throw new Error("\u5DE5\u5177\u8F93\u5165\u5FC5\u987B\u662FJSON\u5BF9\u8C61");
          const action = tool.action === "context" ? args.action === "remember" ? "remember" : "context" : tool.action;
          return call(action, { ...input, ...tool.action === "calendar" ? { action: args.action } : {} }, `${agent.session.id}:${exec.callId}`, exec.signal);
        }
      })));
    }
  }
  ctx.effect(() => ctx.tools.register(defineTool2({
    name: "study_project",
    description: "\u67E5\u770B\u5F53\u524D\u9879\u76EE\u7684\u5B66\u4E60\u63D2\u4EF6\u7ED1\u5B9A\uFF0C\u6216\u6309\u7528\u6237\u660E\u786E\u8981\u6C42 bind/unbind\u3002\u7ED1\u5B9A\u4E00\u6B21\u540E\uFF0C\u6B64\u9879\u76EE\u6BCF\u4E2A\u5BF9\u8BDD\u81EA\u52A8\u83B7\u5F97\u5B66\u4E60\u5DE5\u5177\u4E0E\u5171\u4EAB\u72B6\u6001\u3002\u4E0D\u4F1A\u6539\u53D8\u5176\u4ED6\u9879\u76EE\u6216\u5BBF\u4E3B\u9ED8\u8BA4\u9884\u8BBE\u3002",
    parameters: { action: { type: "string", required: true, enum: ["status", "bind", "unbind"] }, reason: { type: "string", description: "\u7ED1\u5B9A\u6216\u89E3\u7ED1\u7684\u7528\u6237\u8981\u6C42" } },
    output,
    async execute(args, exec) {
      const cwd = exec.agent?.session.header.cwd;
      if (!cwd) throw new Error("\u5F53\u524D\u4F1A\u8BDD\u6CA1\u6709\u9879\u76EE\u76EE\u5F55");
      const project = projectFor(cwd);
      if (args.action === "status") return { bound: !!project, project };
      if (!args.reason?.trim()) throw new Error("\u8BF7\u4FDD\u7559\u7528\u6237\u7684\u7ED1\u5B9A\u6216\u89E3\u7ED1\u8981\u6C42");
      await authorize(exec);
      const root = args.action === "unbind" && project ? project.root : realpathSync2(cwd);
      const path = resolve2(root, BINDING_FILE);
      const temporary = `${path}.${randomUUID2()}.tmp`;
      const previous = existsSync4(path) ? JSON.parse(readFileSync4(path, "utf8")) : { version: 1, data: "." };
      const value = { ...previous, version: 1, enabled: args.action === "bind" };
      await writeFile(temporary, JSON.stringify(value, null, 2) + "\n", { mode: 384, flag: "wx" });
      await rename(temporary, path);
      await mount(exec.agent);
      return { bound: args.action === "bind", root };
    }
  })));
  ctx.on("agent/created", async ({ agent }) => {
    await mount(agent);
  });
  ctx.on("agent/disposed", ({ agent }) => {
    const entry = mounted.get(agent);
    mounted.delete(agent);
    if (entry) void entry.scope.dispose().catch((error) => ctx.logger.warn(String(error)));
  });
  ctx.on("agent/pre-step", async ({ agent, signal }, next) => {
    await mount(agent);
    const decision = await next();
    const entry = mounted.get(agent);
    if (!entry || decision.kind === "reject" || signal.aborted) return decision;
    if (!environment.current && (!config.python || config.python === "auto") && ctx.tools.get("load_workspace_dependencies", agent)) {
      const installed = installedHarnessPython();
      if (!installed) return decision;
      await environment.resolve();
    }
    const value = await jsonProcess(await environment.resolve(), bridge, entry.project.data, { action: "context", args: { session: agent.session.id } }, signal);
    const text = JSON.stringify(value);
    const generation = agent.session.surface.contentGeneration;
    if (entry.supplied === text && entry.generation === generation) return decision;
    entry.supplied = text;
    entry.generation = generation;
    return { ...decision, messages: [...decision.messages, createUserMessage2({ source: { kind: "study" }, content: [{ type: "text", text: `\u5F53\u524D\u9879\u76EE\u5B66\u4E60\u72B6\u6001\uFF08\u8BB0\u5F55\u662F\u53C2\u8003\uFF0C\u8FC7\u671F\u6458\u8981\u987B\u91CD\u65B0\u6838\u5BF9\u539F\u6587\uFF09\uFF1A${text}` }] })] };
  });
  ctx.effect(() => async () => {
    await Promise.all([...mounted.values()].map((v) => v.scope.dispose()));
    mounted.clear();
    await hosts.dispose();
  });
}
export {
  Config,
  apply2 as apply,
  inject,
  name
};
//# sourceMappingURL=index.js.map
