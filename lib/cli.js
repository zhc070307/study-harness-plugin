#!/usr/bin/env node

// src/cli.ts
import { readFileSync as readFileSync2, existsSync as existsSync3, realpathSync as realpathSync2 } from "node:fs";
import { writeFile, rename } from "node:fs/promises";
import { resolve as resolve2 } from "node:path";
import { fileURLToPath as fileURLToPath2 } from "node:url";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";

// src/projects.ts
import { readFileSync, realpathSync, existsSync } from "node:fs";
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
      const config = JSON.parse(readFileSync(file, "utf8"));
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

// src/environment.ts
import { existsSync as existsSync2 } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";
import { fileURLToPath } from "node:url";

// src/process.ts
import { execFile } from "node:child_process";
function jsonProcess(python, script2, root, request, signal, env = process.env, timeout = 12e4) {
  return new Promise((resolve3, reject) => {
    const child = execFile(python, [script2, root], { signal, timeout, maxBuffer: 32 * 1024 * 1024, encoding: "utf8", env: { ...env, PYTHONUTF8: "1", PYTHONDONTWRITEBYTECODE: "1" } }, (error, stdout, stderr) => {
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

// src/environment.ts
var script = fileURLToPath(new URL("../packages/agent/plugin_dependencies.py", import.meta.url));
function installedHarnessPython() {
  const root = join(process.env.DSH_HOME || join(homedir(), ".dsh"), "dsh-runtimes", "dsh-primary-runtime", "dependencies", "python");
  const python = process.platform === "win32" ? join(root, "python.exe") : join(root, "bin", "python3");
  return existsSync2(python) ? python : void 0;
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

// src/cli.ts
var [action, directory = ".", extra] = process.argv.slice(2);
try {
  if (action === "check-dependencies" || action === "setup") {
    const environment = new PythonEnvironment(process.argv[3]);
    console.log(JSON.stringify(action === "setup" ? await environment.ensure() : await environment.check(), null, 2));
  } else {
    const root = realpathSync2(resolve2(directory));
    const file = resolve2(root, BINDING_FILE);
    if (action === "bind" || action === "unbind") {
      const previous = existsSync3(file) ? JSON.parse(readFileSync2(file, "utf8")) : { version: 1, data: "." };
      const temporary = `${file}.${randomUUID()}.tmp`;
      await writeFile(temporary, JSON.stringify({ ...previous, version: 1, enabled: action === "bind" }, null, 2) + "\n", { mode: 384, flag: "wx" });
      await rename(temporary, file);
      projectFor(root);
      console.log(action === "bind" ? `\u5DF2\u7ED1\u5B9A\u5B66\u4E60\u9879\u76EE\uFF1A${root}` : `\u5DF2\u505C\u7528\u5B66\u4E60\u9879\u76EE\uFF1A${root}`);
    } else if (action === "status") {
      console.log(JSON.stringify({ project: projectFor(root) ?? null }, null, 2));
    } else if (action === "doctor") {
      const python = await new PythonEnvironment(extra).resolve();
      const script2 = fileURLToPath2(new URL("../packages/agent/plugin_doctor.py", import.meta.url));
      const result = spawnSync(python, [script2], { stdio: "inherit" });
      if (result.error) throw result.error;
      process.exitCode = result.status ?? 1;
    } else {
      console.log("study-plugin bind|unbind|status PROJECT\nstudy-plugin doctor PROJECT [PYTHON]\nstudy-plugin check-dependencies|setup [PYTHON]\n\u5B89\u88C5\u63D2\u4EF6\u540E\uFF0C\u7ED1\u5B9A\u9879\u76EE\u4E2D\u7684\u65B0\u5BF9\u8BDD\u81EA\u52A8\u542F\u7528\u5B66\u4E60\u5DE5\u5177\u3002");
      process.exitCode = action === void 0 || action === "--help" ? 0 : 1;
    }
  }
} catch (error) {
  console.error(String(error));
  process.exitCode = 1;
}
//# sourceMappingURL=cli.js.map
