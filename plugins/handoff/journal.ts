/**
 * Capture → compile → index, adapted from coleam00/claude-memory-compiler (https://github.com/coleam00/claude-memory-compiler):
 *
 *   flush       every `everyTokens` of context growth, once the turn ends, the session's OWN model (session.generate,
 *               which sees the session's history and adds no turn to it) writes a short structured extract to the
 *               journal. Nobody ever re-reads a whole transcript.
 *   precompact  when a session compacts with un-journaled work since the last flush, a raw excerpt of its recent
 *               turns goes to the journal first (the safety net for compactions the flush didn't anticipate).
 *   compile     when the backlog reaches `compileMin`, or once a day after `compileAfterHour`, the coordinator is
 *               asked to run the knowledge-compile skill (in a background subagent) over the journal.
 *   index       every session of a journaling agent gets the knowledge index in its system prompt, snapshotted once
 *               per session (stable, so it caches) and refreshed after each compaction.
 */
import { contextTokens, type Tokens } from "./logic.ts"

export interface JournalOptions {
  agents: string[]
  coordinator: string
  everyTokens: number
  minRawTokens: number
  compileMin: number
  compileAfterHour: number
}

export interface JournalDeps {
  agentOf(sessionID: string): Promise<string | undefined>
  generate(sessionID: string, prompt: string): Promise<string>
  write(entry: { sessionID: string; agent: string; kind: "flush" | "precompact"; body: string; tokens: number }): Promise<void>
  stats(): Promise<{ pending: number; last_compiled?: string | null }>
  prompt(sessionID: string, text: string): Promise<void>
  index(): Promise<string>
  now(): Date
  log(message: string): void
}

export const NOTHING = "NOTHING_TO_SAVE"

export const FLUSH_PROMPT =
  `Write a short journal entry about the work in this session since your last journal entry, for a later pass that ` +
  `folds it into the team's knowledge graph. Use only these sections, and only the ones with real content:\n\n` +
  `**Context:** one line on what was being worked on.\n` +
  `**Decisions:** only choices that were actually made or agreed, each with its reason and who made it.\n` +
  `**Proposals:** options suggested but not agreed (yours or anyone's), each marked as a suggestion.\n` +
  `**Lessons:** observed facts: gotchas, approaches that failed and why, things that surprised you about the code.\n` +
  `**Action items:** follow-ups that are not done yet.\n\n` +
  `Never promote a suggestion, a guess or a plan to a decision or a fact. ` +
  `Name files, functions, node ids (#id) and PRs where they matter. Leave out routine tool calls, file reads, ` +
  `back-and-forth and anything already obvious from the code. Do not call tools. If nothing is worth keeping, ` +
  `reply with exactly ${NOTHING}.`

export const compileText = (pending: number) =>
  `KNOWLEDGE COMPILE: ${pending} journal entries are waiting. When you reach a natural pause, load the ` +
  `knowledge-compile skill and run it in a background subagent so your own context stays small; then carry on.`

interface State {
  tokens: number
  lastJournal: number
  busy: boolean
  index?: string
}

export function createJournal(opts: JournalOptions, deps: JournalDeps) {
  const states = new Map<string, State>()
  const agents = new Map<string, string | undefined>()
  let coordinatorSession: string | undefined
  let lastCompilePrompt = 0

  const state = (id: string) => {
    let s = states.get(id)
    if (!s) states.set(id, (s = { tokens: 0, lastJournal: 0, busy: false }))
    return s
  }

  async function agentOf(sessionID: string, known?: string) {
    if (known !== undefined) agents.set(sessionID, known)
    if (!agents.has(sessionID)) agents.set(sessionID, await deps.agentOf(sessionID).catch(() => undefined))
    const a = agents.get(sessionID)
    if (a === opts.coordinator) coordinatorSession = sessionID
    return a
  }
  const journaling = async (sessionID: string, known?: string) => {
    const a = await agentOf(sessionID, known)
    return a !== undefined && opts.agents.includes(a) ? a : undefined
  }

  async function maybeCompile() {
    if (!coordinatorSession) return
    const now = deps.now()
    if (now.getTime() - lastCompilePrompt < 2 * 3600_000) return
    const st = await deps.stats().catch(() => undefined)
    if (!st || st.pending === 0) return
    const today = now.toISOString().slice(0, 10)
    const daily = now.getHours() >= opts.compileAfterHour && !(st.last_compiled ?? "").startsWith(today)
    if (st.pending < opts.compileMin && !daily) return
    lastCompilePrompt = now.getTime()
    await deps.prompt(coordinatorSession, compileText(st.pending))
      .catch((e) => deps.log(`could not ask for a compile: ${String(e)}`))
  }

  return {
    states,

    onAgentSelected(sessionID: string, agent: string) {
      agents.set(sessionID, agent)
      if (agent === opts.coordinator) coordinatorSession = sessionID
    },

    async onStepEnded(sessionID: string, tokens: Tokens) {
      if (!(await journaling(sessionID))) return
      const s = state(sessionID)
      if (!s.busy) s.tokens = contextTokens(tokens)   // a flush's own generate call must not move the needle
    },

    async onTurnEnded(sessionID: string) {
      const agent = await journaling(sessionID)
      const s = states.get(sessionID)
      if (!agent || !s || s.busy || s.tokens - s.lastJournal < opts.everyTokens) return
      s.busy = true
      try {
        const text = (await deps.generate(sessionID, FLUSH_PROMPT)).trim()
        const keep = text && !text.startsWith(NOTHING)
        if (keep) await deps.write({ sessionID, agent, kind: "flush", body: text, tokens: s.tokens })
        deps.log(`${sessionID} flush at ${s.tokens} tokens: ${keep ? `journaled ${text.length} chars` : "nothing worth saving"}`)
        s.lastJournal = s.tokens
      } catch (e) {
        deps.log(`flush failed for ${sessionID}: ${String(e)}`)
      } finally {
        s.busy = false
      }
      await maybeCompile()
    },

    /** Runs before the handoff's compaction hook. `excerpt` is the recent turns as text, built by the caller. */
    async onCompaction(input: { sessionID: string; agent?: string }, excerpt: () => string) {
      const agent = await journaling(input.sessionID, input.agent)
      if (!agent) return
      const s = state(input.sessionID)
      if (s.tokens - s.lastJournal >= opts.minRawTokens) {
        const body = excerpt().trim()
        if (body) await deps.write({ sessionID: input.sessionID, agent, kind: "precompact", body, tokens: s.tokens })
          .catch((e) => deps.log(`precompact capture failed: ${String(e)}`))
      }
      states.set(input.sessionID, { tokens: 0, lastJournal: 0, busy: false })   // fresh context, fresh index snapshot
    },

    /** The text to append to a journaling agent's system prompt, or undefined. */
    async systemFor(sessionID: string, agent?: string): Promise<string | undefined> {
      if (!(await journaling(sessionID, agent))) return undefined
      const s = state(sessionID)
      if (s.index === undefined) {
        s.index = await deps.index().catch(() => "")
      }
      return s.index ? `# What the team knows (snapshot at session start; search the store for more)\n${s.index}` : undefined
    },
  }
}

/** Recent turns as plain text, newest last, capped at `maxChars` (the precompact safety net). */
export function excerpt(messages: readonly unknown[], maxTurns = 30, maxChars = 15_000): string {
  const out: string[] = []
  for (const m of messages.slice(-maxTurns) as { role?: string; content?: unknown }[]) {
    const parts = typeof m.content === "string" ? [m.content]
      : Array.isArray(m.content) ? m.content.map((p: { type?: string; text?: string }) => (p?.type === "text" ? p.text ?? "" : "")) : []
    const text = parts.join(" ").trim()
    if (text) out.push(`[${m.role ?? "?"}]: ${text}`)
  }
  const all = out.join("\n\n")
  return all.length > maxChars ? all.slice(-maxChars) : all
}
