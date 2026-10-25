import assert from "node:assert/strict"
import { test } from "node:test"

import { contextTokens, createHandoff, type CompactionResult, type Deps } from "./logic.ts"

function fake(agents: Record<string, string> = { m: "master", w: "worker" }) {
  const calls: string[] = []
  const deps: Deps = {
    agentOf: async (id) => agents[id],
    prompt: async (id) => { calls.push(`prompt ${id}`) },
    compact: async (id) => { calls.push(`compact ${id}`) },
    handoff: async (id, tokens, since) => { calls.push(`handoff ${id} ${tokens} ${since}`); return "# HANDOFF boot" },
    now: () => "T0",
    log: () => {},
  }
  return { calls, deps, h: createHandoff({ agent: "master", scope: "master", thresholdTokens: 1000 }, deps) }
}
const t = (n: number) => ({ input: n, output: 0, cache: { read: 0, write: 0 } })

test("context counts input, output and cache", () => {
  assert.equal(contextTokens({ input: 10, output: 5, cache: { read: 100, write: 1 } }), 116)
})

test("below threshold, or a worker, never hands off", async () => {
  const { calls, h } = fake()
  await h.onStepEnded("m", t(999))
  await h.onStepEnded("w", t(5000))
  await h.onTurnEnded("m"); await h.onTurnEnded("w")
  assert.deepEqual(calls, [])
})

test("planned handoff: prompt once, compact after brief_write's turn, boot packet replaces the summary", async () => {
  const { calls, h } = fake()
  await h.onStepEnded("m", t(1200))
  await h.onStepEnded("m", t(1300))                  // still in the same turn: no second prompt
  assert.deepEqual(calls, ["prompt m"])
  await h.onTurnEnded("m")                            // the turn that crossed the threshold
  assert.deepEqual(calls, ["prompt m"])
  h.onToolDone("m", "store_brief_write", true)
  await h.onTurnEnded("m")                            // the briefing turn
  assert.deepEqual(calls, ["prompt m", "compact m"])
  const input: { sessionID: string; agent?: string; result?: CompactionResult } = { sessionID: "m", agent: "master" }
  await h.onCompaction(input)
  assert.equal(input.result?.summary, "# HANDOFF boot")
  assert.equal(calls[2], "handoff m 1300 T0")
  assert.equal(h.states.get("m")?.phase, "idle")
})

test("a model that never writes the brief still hands off after two turns", async () => {
  const { calls, h } = fake()
  await h.onStepEnded("m", t(1200))
  h.onToolDone("m", "store_brief_write", false)       // a failed call does not count
  await h.onTurnEnded("m")
  await h.onTurnEnded("m")
  assert.deepEqual(calls, ["prompt m", "compact m"])
})

test("an unplanned compaction of the master is still replaced; a worker's is left to OpenCode", async () => {
  const { calls, h } = fake()
  const m: { sessionID: string; agent?: string; result?: CompactionResult } = { sessionID: "m", agent: "master" }
  const w: { sessionID: string; agent?: string; result?: CompactionResult } = { sessionID: "w", agent: "worker" }
  await h.onCompaction(m); await h.onCompaction(w)
  assert.equal(m.result?.summary, "# HANDOFF boot")
  assert.equal(w.result, undefined)
  assert.deepEqual(calls, ["handoff m 0 T0"])
})

test("a failing store leaves OpenCode's own compaction in place", async () => {
  const { deps } = fake()
  const h = createHandoff({ agent: "master", scope: "master", thresholdTokens: 1000 },
    { ...deps, handoff: async () => { throw new Error("uv missing") } })
  const input: { sessionID: string; agent?: string; result?: CompactionResult } = { sessionID: "m", agent: "master" }
  await h.onCompaction(input)
  assert.equal(input.result, undefined)
})

test("switching a session to the master agent makes it hand off", async () => {
  const { calls, h } = fake()
  await h.onStepEnded("w", t(5000))
  h.onAgentSelected("w", "master")
  await h.onStepEnded("w", t(5000))
  assert.deepEqual(calls, ["prompt w"])
})

test("the injected result carries a token estimate (a result without one stalled the session live)", async () => {
  const { h } = fake()
  const input: { sessionID: string; agent?: string; result?: CompactionResult } = { sessionID: "m", agent: "master" }
  await h.onCompaction(input)
  assert.equal(input.result?.tokens?.input, Math.ceil("# HANDOFF boot".length / 4))
})

test("a threshold below the fresh context pauses handoffs instead of looping", async () => {
  const { calls, h } = fake()
  await h.onCompaction({ sessionID: "m", agent: "master" })
  await h.onStepEnded("m", t(5000))            // first step after the handoff is already over 1000
  await h.onStepEnded("m", t(6000))
  assert.deepEqual(calls, ["handoff m 0 T0"])
  assert.equal(h.states.get("m")?.phase, "disabled")
})

test("after a normal handoff the next crossing hands off again", async () => {
  const { calls, h } = fake()
  await h.onCompaction({ sessionID: "m", agent: "master" })
  await h.onStepEnded("m", t(200))             // fresh context is small
  await h.onStepEnded("m", t(1500))
  assert.deepEqual(calls, ["handoff m 0 T0", "prompt m"])
})
