/**
 * OpenCode v2 plugin: the coordinating agent hands off instead of compacting (logic.ts), and every master/worker
 * session journals what it learns, gets the knowledge index, and feeds the compile loop (journal.ts).
 * This file is wiring only: host events and hooks in, `Deps` calls out. Host imports are type-only,
 * so the plugin ships with no dependencies.
 *
 * Options (opencode.json → plugins[].options):
 *   agent             default "master"   which agent's sessions hand off (and are asked to compile)
 *   scope             default = agent    store scope for the brief
 *   thresholdTokens   default 600000     handoff point; ~60–70% of the model's context window
 *   journalAgents     default [agent, "worker"]   agents whose sessions journal and get the index
 *   journalEveryTokens default 150000    context growth between flushes
 *   minRawTokens      default 20000      un-journaled growth that earns a raw excerpt at compaction
 *   compileMin        default 8          backlog that triggers a compile request
 *   compileAfterHour  default 18         after this local hour, any backlog not compiled today triggers one
 *   store             default ["uv","run","--project","<repo>/store","python","-m","coordlite"]
 *   env               extra environment for the store CLI (COORD_DB): must match the MCP server's, or the
 *                     plugin would read and write a different store than the sessions do
 */
import { execFile } from "node:child_process"
import { dirname, resolve } from "node:path"
import { fileURLToPath } from "node:url"

import type { Plugin } from "@opencode/plugin"

import { createJournal, excerpt } from "./journal.ts"
import { createHandoff, type Tokens } from "./logic.ts"

const ID = "coord-handoff"
const STORE_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "../../store")

function run(cmd: string[], args: string[], env: Record<string, string>, input?: string): Promise<string> {
  return new Promise((ok, fail) => {
    const opts = { timeout: 30_000, maxBuffer: 4 * 1024 * 1024, env: { ...process.env, ...env } }
    const child = execFile(cmd[0], [...cmd.slice(1), ...args], opts, (err, stdout, stderr) =>
      err ? fail(new Error(`${err.message}: ${String(stderr).slice(0, 300)}`)) : ok(String(stdout)))
    if (input !== undefined) child.stdin?.end(input)
  })
}

const num = (v: unknown, d: number) => (Number.isFinite(v) ? Number(v) : d)

export default {
  id: ID,

  async setup(context) {
    const o = context.options ?? {}
    const agent = typeof o.agent === "string" ? o.agent : "master"
    const scope = typeof o.scope === "string" ? o.scope : agent
    const thresholdTokens = num(o.thresholdTokens, 600_000)
    const store: string[] = Array.isArray(o.store) ? o.store : ["uv", "run", "--project", STORE_DIR, "python", "-m", "coordlite"]
    const env: Record<string, string> = o.env && typeof o.env === "object" ? o.env : {}
    const log = (m: string) => console.warn(`[${ID}] ${m}`)
    const agentOf = async (sessionID: string) => (await context.session.get({ sessionID })).agent
    const prompt = async (sessionID: string, text: string) => { await context.session.prompt({ sessionID, text, delivery: "queue" }) }

    const h = createHandoff({ agent, scope, thresholdTokens }, {
      agentOf,
      prompt,
      compact: async (sessionID) => { await context.session.compact({ sessionID, delivery: "queue" }) },
      handoff: (sessionID, tokens, since) =>
        run(store, ["handoff", "--scope", scope, "--session", sessionID, "--tokens", String(tokens), "--since", since], env),
      now: () => new Date().toISOString(),
      log,
    })

    const j = createJournal({
      agents: Array.isArray(o.journalAgents) ? o.journalAgents : [agent, "worker"],
      coordinator: agent,
      everyTokens: num(o.journalEveryTokens, 150_000),
      minRawTokens: num(o.minRawTokens, 20_000),
      compileMin: num(o.compileMin, 8),
      compileAfterHour: num(o.compileAfterHour, 18),
    }, {
      agentOf,
      generate: async (sessionID, p) => (await context.session.generate({ sessionID, prompt: p })).text,
      write: async (e) => {
        await run(store, ["journal", "--actor", e.agent, "--session", e.sessionID, "--agent", e.agent, "--kind", e.kind,
          "--tokens", String(e.tokens)], env, e.body)
      },
      stats: async () => JSON.parse(await run(store, ["journal-stats"], env)),
      prompt,
      index: () => run(store, ["index"], env),
      now: () => new Date(),
      log,
    })

    const compaction = await context.session.hook("compaction", async (input) => {
      await j.onCompaction(input, () => excerpt(input.messages))   // capture first: the handoff replaces the context
      await h.onCompaction(input)
    })
    const sessionContext = await context.session.hook("context", async (input) => {
      const text = await j.systemFor(input.sessionID, input.agent)
      if (text) input.system.push({ type: "text", text })
    })
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
            if (e.type === "session.step.ended" && e.data?.tokens) {
              await j.onStepEnded(sid, e.data.tokens)
              await h.onStepEnded(sid, e.data.tokens)
            } else if (e.type === "session.agent.selected" && e.data?.agent) {
              j.onAgentSelected(sid, e.data.agent)
              h.onAgentSelected(sid, e.data.agent)
            } else if (e.type === "session.execution.succeeded" || e.type === "session.execution.failed"
              || e.type === "session.execution.interrupted") {
              await j.onTurnEnded(sid)                                // flush before a possible handoff compaction
              await h.onTurnEnded(sid)
            }
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
      await sessionContext.dispose()
      await toolDone.dispose()
    }
  },
} satisfies Plugin.Plugin
