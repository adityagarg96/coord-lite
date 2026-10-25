/**
 * Handoff instead of compaction, for the coordinating agent.
 *
 *   idle ──(context ≥ threshold)──► briefing ──(brief_write succeeded)──► briefed ──(turn ends)──► compacting
 *     ▲    queue HANDOFF prompt               (or 2 turns end without it)          session.compact     │
 *     └────────────────────────────── compaction hook: summary := boot packet from the store ◄────────┘
 *
 * The compaction hook sets `result`, which skips OpenCode's model-written summary entirely: the session keeps
 * its id and title (peers keep addressing it), and its context restarts from the brief + the live plan.
 * A compaction OpenCode starts on its own (or `/compact`) takes the same path, with a stale-brief warning.
 * Every host call goes through `Deps`, so this file runs under `node --test` with a fake host.
 */

export interface Tokens {
  input: number
  output: number
  reasoning?: number
  cache?: { read: number; write: number }
}

export interface CompactionResult {
  summary: string
  tokens?: { input: number; output: number; reasoning: number; cache: { read: number; write: number } }
}

export interface Options {
  /** Agent whose sessions hand off instead of compacting. */
  agent: string
  /** Store scope the brief is written under. */
  scope: string
  /** Context size (tokens) that triggers a handoff. Set to ~60–70% of the model's window. */
  thresholdTokens: number
}

export interface Deps {
  agentOf(sessionID: string): Promise<string | undefined>
  prompt(sessionID: string, text: string): Promise<void>
  compact(sessionID: string): Promise<void>
  /** Record the handoff in the store and return the boot packet. */
  handoff(sessionID: string, tokens: number, since: string): Promise<string>
  now(): string
  log(message: string): void
}

type Phase = "idle" | "briefing" | "briefed" | "compacting" | "disabled"

interface State {
  phase: Phase
  tokens: number
  since?: string
  turnsWhileBriefing: number
  /** The first step after a handoff measures the fresh context; if that is already over the threshold, stop. */
  fresh?: boolean
}

export const handoffText = (scope: string) =>
  `HANDOFF NOW: this session's context is nearly full and will be replaced by a fresh start. ` +
  `Call the store tool brief_write (actor "${scope}", scope "${scope}") with all five slots REWRITTEN: ` +
  `purpose · happened · open · next · ruled_out. Write it so your successor can continue without asking anyone: ` +
  `what you were in the middle of, which sessions own what, what you promised whom, and what you ruled out. ` +
  `Make sure every decision is already in the store as nodes, and journal_write anything learned that isn't. ` +
  `Then reply with one line.`

/** The context the last model call actually read and wrote. Over-counting only hands off early. */
export function contextTokens(t: Tokens): number {
  return (t.input || 0) + (t.output || 0) + (t.cache?.read || 0) + (t.cache?.write || 0)
}

export function createHandoff(opts: Options, deps: Deps) {
  const states = new Map<string, State>()
  const agents = new Map<string, string | undefined>()

  const state = (id: string): State => {
    let s = states.get(id)
    if (!s) states.set(id, (s = { phase: "idle", tokens: 0, turnsWhileBriefing: 0 }))
    return s
  }

  async function isCoordinator(sessionID: string, agent?: string): Promise<boolean> {
    if (agent !== undefined) return agent === opts.agent
    if (!agents.has(sessionID)) agents.set(sessionID, await deps.agentOf(sessionID).catch(() => undefined))
    return agents.get(sessionID) === opts.agent
  }

  async function compactNow(sessionID: string, s: State) {
    s.phase = "compacting"
    try {
      await deps.compact(sessionID)
    } catch (e) {
      s.phase = "idle"
      deps.log(`compact failed for ${sessionID}: ${String(e)}`)
    }
  }

  return {
    states,

    onAgentSelected(sessionID: string, agent: string) {
      agents.set(sessionID, agent)
    },

    async onStepEnded(sessionID: string, tokens: Tokens) {
      if (!(await isCoordinator(sessionID))) return
      const s = state(sessionID)
      s.tokens = contextTokens(tokens)
      if (s.fresh) {
        s.fresh = false
        if (s.tokens >= opts.thresholdTokens) {
          s.phase = "disabled"
          deps.log(`${sessionID} starts at ${s.tokens} tokens right after a handoff, over the ${opts.thresholdTokens} threshold: ` +
            `handoffs paused for this session (raise thresholdTokens) so it does not loop`)
          return
        }
      }
      if (s.phase !== "idle" || s.tokens < opts.thresholdTokens) return
      s.phase = "briefing"
      s.since = deps.now()
      s.turnsWhileBriefing = 0
      deps.log(`${sessionID} at ${s.tokens} tokens ≥ ${opts.thresholdTokens}: asking for a brief`)
      try {
        await deps.prompt(sessionID, handoffText(opts.scope))
      } catch (e) {
        s.phase = "idle"
        deps.log(`could not queue the handoff prompt: ${String(e)}`)
      }
    },

    onToolDone(sessionID: string, tool: string, ok: boolean) {
      const s = states.get(sessionID)
      if (s?.phase === "briefing" && ok && /brief_write$/.test(tool)) s.phase = "briefed"
    },

    async onTurnEnded(sessionID: string) {
      const s = states.get(sessionID)
      if (!s) return
      if (s.phase === "briefed") return compactNow(sessionID, s)
      // The turn that crossed the threshold ends first; the queued briefing turn is the second.
      // A model that never calls brief_write must not hold the session hostage.
      if (s.phase === "briefing" && ++s.turnsWhileBriefing >= 2) {
        deps.log(`${sessionID} ended two turns without brief_write; handing off with the old brief`)
        return compactNow(sessionID, s)
      }
    },

    /** Mutates the hook input: setting `result` skips the model-written summary. Fails open. */
    async onCompaction(input: { sessionID: string; agent?: string; result?: CompactionResult }) {
      if (!(await isCoordinator(input.sessionID, input.agent))) return
      const s = state(input.sessionID)
      try {
        const boot = await deps.handoff(input.sessionID, s.tokens, s.since ?? deps.now())
        // tokens is optional in the 2.0.29 types, but a result without it left the session unable to start its
        // next turn (probed live); OpenCode's own compactions always carry it. ~4 chars per token.
        const est = Math.ceil(boot.length / 4)
        input.result = { summary: boot, tokens: { input: est, output: 0, reasoning: 0, cache: { read: 0, write: 0 } } }
        deps.log(`${input.sessionID} handed off (${s.phase === "compacting" ? "planned" : "unplanned compaction"})`)
      } catch (e) {
        deps.log(`handoff failed, falling back to OpenCode's own compaction: ${String(e)}`)
      } finally {
        states.set(input.sessionID, { phase: "idle", tokens: 0, turnsWhileBriefing: 0, fresh: true })
      }
    },
  }
}
