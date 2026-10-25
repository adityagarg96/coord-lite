import assert from "node:assert/strict"
import { test } from "node:test"

import { compileText, createJournal, excerpt, FLUSH_PROMPT, NOTHING, type JournalDeps } from "./journal.ts"

function fake(over: Partial<JournalDeps> = {}, hour = 10) {
  const calls: string[] = []
  const agents: Record<string, string> = { m: "master", w: "worker", b: "build" }
  let pending = 0
  const deps: JournalDeps = {
    agentOf: async (id) => agents[id],
    generate: async (id, p) => { calls.push(`generate ${id}`); assert.equal(p, FLUSH_PROMPT); return "**Context:** auth work" },
    write: async (e) => { calls.push(`write ${e.kind} ${e.sessionID} ${e.tokens}`); pending++ },
    stats: async () => ({ pending, last_compiled: null }),
    prompt: async (id, text) => { calls.push(`prompt ${id} ${text.slice(0, 18)}`) },
    index: async () => { calls.push("index"); return "- #1 concept: Auth" },
    now: () => new Date(2026, 9, 24, hour),
    log: () => {},
    ...over,
  }
  const j = createJournal({ agents: ["master", "worker"], coordinator: "master", everyTokens: 1000, minRawTokens: 300,
    compileMin: 2, compileAfterHour: 18 }, deps)
  return { calls, j, setPending: (n: number) => { pending = n } }
}
const t = (n: number) => ({ input: n, output: 0, cache: { read: 0, write: 0 } })

test("a flush happens at the end of the turn that crossed the growth step, then not again until the next step", async () => {
  const { calls, j } = fake()
  await j.onStepEnded("w", t(900)); await j.onTurnEnded("w")
  assert.deepEqual(calls, [])
  await j.onStepEnded("w", t(1100)); await j.onTurnEnded("w")
  assert.deepEqual(calls, ["generate w", "write flush w 1100"])
  await j.onStepEnded("w", t(1900)); await j.onTurnEnded("w")
  assert.equal(calls.length, 2)
  await j.onStepEnded("w", t(2200)); await j.onTurnEnded("w")
  assert.deepEqual(calls.slice(2), ["generate w", "write flush w 2200"])
})

test("nothing worth saving writes nothing but still resets the step; other agents are ignored", async () => {
  const { calls, j } = fake({ generate: async () => `${NOTHING}` })
  await j.onStepEnded("w", t(1500)); await j.onTurnEnded("w")
  await j.onStepEnded("b", t(9000)); await j.onTurnEnded("b")
  await j.onStepEnded("w", t(1600)); await j.onTurnEnded("w")
  assert.equal(calls.filter((c) => c.startsWith("write")).length, 0)
  assert.equal(j.states.get("w")?.lastJournal, 1500)
  assert.equal(j.states.has("b"), false)
})

test("compaction captures a raw excerpt only when enough un-journaled work exists, then resets", async () => {
  const { calls, j } = fake()
  await j.onStepEnded("w", t(200))
  await j.onCompaction({ sessionID: "w", agent: "worker" }, () => "[user]: hi")
  assert.deepEqual(calls, [])                                   // 200 < minRawTokens
  await j.onStepEnded("w", t(500))
  await j.onCompaction({ sessionID: "w", agent: "worker" }, () => "[user]: build it")
  assert.deepEqual(calls, ["write precompact w 500"])
  assert.equal(j.states.get("w")?.tokens, 0)
})

test("the coordinator is asked to compile once the backlog reaches the minimum, at most every two hours", async () => {
  const { calls, j } = fake()
  await j.onStepEnded("m", t(10))                                // the master has been seen
  await j.onStepEnded("w", t(1100)); await j.onTurnEnded("w")   // pending 1 < 2
  assert.equal(calls.some((c) => c.startsWith("prompt")), false)
  await j.onStepEnded("w", t(2200)); await j.onTurnEnded("w")   // pending 2
  assert.equal(calls.at(-1), `prompt m ${compileText(2).slice(0, 18)}`)
  await j.onStepEnded("w", t(3300)); await j.onTurnEnded("w")
  assert.equal(calls.filter((c) => c.startsWith("prompt")).length, 1)
})

test("after the compile hour, any backlog not compiled today triggers a compile", async () => {
  const { calls, j } = fake({}, 19)
  await j.onStepEnded("m", t(10))
  await j.onStepEnded("w", t(1100)); await j.onTurnEnded("w")   // pending 1, but it's 19:00
  assert.equal(calls.at(-1)?.startsWith("prompt m"), true)
})

test("the index is fetched once per session and refreshed after compaction; non-journaling agents get none", async () => {
  const { calls, j } = fake()
  assert.match((await j.systemFor("w", "worker")) ?? "", /#1 concept: Auth/)
  await j.systemFor("w", "worker")
  assert.equal(calls.filter((c) => c === "index").length, 1)
  await j.onCompaction({ sessionID: "w", agent: "worker" }, () => "")
  await j.systemFor("w", "worker")
  assert.equal(calls.filter((c) => c === "index").length, 2)
  assert.equal(await j.systemFor("b", "build"), undefined)
})

test("excerpt keeps the newest turns as text and caps the size", () => {
  const msgs = [{ role: "user", content: "old" }, { role: "assistant", content: [{ type: "text", text: "middle" }, { type: "tool" }] },
    { role: "user", content: "x".repeat(50) }]
  assert.equal(excerpt(msgs, 2), `[assistant]: middle\n\n[user]: ${"x".repeat(50)}`)
  assert.equal(excerpt(msgs, 30, 20).length, 20)
})
