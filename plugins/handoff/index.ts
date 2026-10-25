/**
 * OpenCode v2 plugin: the coordinating agent hands off instead of compacting (see logic.ts).
 * This file is wiring only: host events and hooks in, `Deps` calls out. Host imports are type-only,
 * so the plugin ships with no dependencies.
 *
 * Options (opencode.json → plugins[].options):
 *   agent            default "master"   which agent's sessions hand off
 *   scope            default = agent    store scope for the brief
 *   thresholdTokens  default 600000     set to ~60–70% of the model's context window
 *   store            default ["uv","run","--project","<repo>/store","python","-m","coordlite"]
 *   env              extra environment for the store CLI (COORD_DB): must match the MCP server's, or the
 *                    handoff would read and write a different store than the sessions do
 */
import { execFile } from "node:child_process"
import { dirname, resolve } from "node:path"
import { fileURLToPath } from "node:url"

import type { Plugin } from "@opencode/plugin"

import { createHandoff, type Tokens } from "./logic.ts"

const ID = "coord-handoff"
const STORE_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "../../store")

function run(cmd: string[], args: string[], env: Record<string, string>): Promise<string> {
  return new Promise((ok, fail) => {
    const opts = { timeout: 30_000, maxBuffer: 4 * 1024 * 1024, env: { ...process.env, ...env } }
    execFile(cmd[0], [...cmd.slice(1), ...args], opts, (err, stdout, stderr) =>
      err ? fail(new Error(`${err.message}: ${String(stderr).slice(0, 300)}`)) : ok(String(stdout)))
  })
}

export default {
  id: ID,

  async setup(context) {
    const o = context.options ?? {}
    const agent = typeof o.agent === "string" ? o.agent : "master"
    const scope = typeof o.scope === "string" ? o.scope : agent
    const thresholdTokens = Number.isFinite(o.thresholdTokens) ? Number(o.thresholdTokens) : 600_000
    const store: string[] = Array.isArray(o.store) ? o.store : ["uv", "run", "--project", STORE_DIR, "python", "-m", "coordlite"]
    const env: Record<string, string> = o.env && typeof o.env === "object" ? o.env : {}
    const log = (m: string) => console.warn(`[${ID}] ${m}`)

    const h = createHandoff({ agent, scope, thresholdTokens }, {
      agentOf: async (sessionID) => (await context.session.get({ sessionID })).agent,
      prompt: async (sessionID, text) => { await context.session.prompt({ sessionID, text, delivery: "queue" }) },
      compact: async (sessionID) => { await context.session.compact({ sessionID, delivery: "queue" }) },
      handoff: (sessionID, tokens, since) =>
        run(store, ["handoff", "--scope", scope, "--session", sessionID, "--tokens", String(tokens), "--since", since], env),
      now: () => new Date().toISOString(),
      log,
    })

    const compaction = await context.session.hook("compaction", (input) => h.onCompaction(input))
    const toolDone = await context.tool.hook("execute.after", (input) =>
      h.onToolDone(input.sessionID, input.tool, input.status === "completed"))

    const controller = new AbortController()
    void (async () => {
      for (let delay = 1000; !controller.signal.aborted; delay = Math.min(delay * 2, 30_000)) {
        try {
          for await (const ev of context.event.subscribe({ signal: controller.signal })) {
            delay = 1000
            const e = ev as { type: string; data?: { sessionID?: string; tokens?: Tokens; agent?: string } }
            const sid = e.data?.sessionID
            if (!sid) continue
            if (e.type === "session.step.ended" && e.data?.tokens) await h.onStepEnded(sid, e.data.tokens)
            else if (e.type === "session.agent.selected" && e.data?.agent) h.onAgentSelected(sid, e.data.agent)
            else if (e.type === "session.execution.succeeded" || e.type === "session.execution.failed"
              || e.type === "session.execution.interrupted") await h.onTurnEnded(sid)
          }
        } catch (err) {
          if (controller.signal.aborted) return
          log(`event stream dropped (${String(err)}); retrying in ${delay} ms`)
        }
        await new Promise((r) => setTimeout(r, delay))
      }
    })()

    log(`loaded: agent=${agent} scope=${scope} threshold=${thresholdTokens} store=${store.join(" ")}`)
    return async () => {
      controller.abort()
      await compaction.dispose()
      await toolDone.dispose()
    }
  },
} satisfies Plugin.Plugin
