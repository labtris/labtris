//: The assistant loop, without a provider and without a server.
//:
//: What is worth testing here is not "does OpenAI reply" — it is what happens
//: between the model and Labtris: that a refusal is fed back rather than
//: thrown, that a turn with no tool calls ends, that the step cap stops a loop
//: and says so, and that history is carried. None of that needs a real model,
//: and all of it is where a tool-calling loop actually goes wrong.
//:
//: Run: node src/lib/assistant.test.mjs

import assert from "node:assert/strict";
import { runTurn } from "./assistant.js";

const CONFIG = { baseUrl: "https://stub.invalid/v1", model: "stub", apiKey: "k" };
const TOOLS = { tools: [{ type: "function", function: { name: "create_lab" } }], system: "sys" };

function say(content) {
  return { choices: [{ message: { role: "assistant", content } }] };
}
function callTool(name, args) {
  return {
    choices: [{
      message: {
        role: "assistant",
        content: null,
        tool_calls: [{ id: "c1", function: { name, arguments: JSON.stringify(args) } }],
      },
    }],
  };
}

const tests = {
  async "a plain answer ends the turn"() {
    let calls = 0;
    const r = await runTurn({
      labId: "L", message: "hello",
      deps: {
        config: CONFIG, fetchTools: async () => TOOLS,
        chat: async () => { calls++; return say("nothing to do"); },
        execTool: async () => assert.fail("should not run a tool"),
      },
    });
    assert.equal(r.text, "nothing to do");
    assert.equal(calls, 1, "one round trip, not a loop");
    assert.deepEqual(r.applied, []);
  },

  async "a tool call runs, then the answer ends it"() {
    const seen = [];
    let turn = 0;
    const r = await runTurn({
      labId: "L42", message: "make a lab",
      deps: {
        config: CONFIG, fetchTools: async () => TOOLS,
        chat: async () => (turn++ === 0 ? callTool("create_lab", { name: "x" }) : say("done")),
        execTool: async (body) => { seen.push(body); return { ok: true, result: { id: "1" } }; },
      },
    });
    assert.equal(r.text, "done");
    assert.equal(seen.length, 1);
    assert.equal(seen[0].name, "create_lab");
    //: The lab the user is looking at is passed through, so the model does not
    //: have to be told which lab it is working on in prose.
    assert.equal(seen[0].lab_id, "L42");
    assert.match(r.applied[0], /create_lab\(name=x\)/);
  },

  async "a refusal is fed back to the model, not thrown"() {
    //: The whole reason /agent/tool returns ok:false instead of a 4xx. A locked
    //: lab is something the model should read and work around; an exception
    //: would end the turn and tell the user their assistant crashed.
    let turn = 0;
    const r = await runTurn({
      labId: "L", message: "delete it",
      deps: {
        config: CONFIG, fetchTools: async () => TOOLS,
        chat: async (_c, messages) => {
          if (turn++ === 0) return callTool("create_lab", { name: "x" });
          const last = messages[messages.length - 1];
          assert.equal(last.role, "tool", "the refusal must reach the model as a tool result");
          assert.match(last.content, /locked/);
          return say("that lab is locked, so I stopped");
        },
        execTool: async () => ({ ok: false, error: "409: lab is locked" }),
      },
    });
    assert.match(r.applied[0], /refused/);
    assert.match(r.text, /locked/);
  },

  async "the step cap stops a runaway and says so"() {
    let runs = 0;
    const r = await runTurn({
      labId: "L", message: "loop forever", maxSteps: 3,
      deps: {
        config: CONFIG, fetchTools: async () => TOOLS,
        chat: async () => callTool("create_lab", { name: "again" }),
        execTool: async () => { runs++; return { ok: true, result: {} }; },
      },
    });
    assert.equal(runs, 3, "stopped at the cap");
    assert.equal(r.truncated, true);
    //: Presenting a half-finished job as finished is the failure mode worth
    //: guarding: the user would believe the lab was built.
    assert.match(r.text, /Stopped after 3 steps/);
  },

  async "no key is refused before any request is made"() {
    await assert.rejects(
      runTurn({ labId: "L", message: "x",
        deps: { config: { ...CONFIG, apiKey: "" },
                chat: async () => assert.fail("must not call the provider") } }),
      /No API key/,
    );
  },

  async "malformed tool arguments do not end the turn"() {
    //: Models emit invalid JSON in `arguments` often enough that treating it as
    //: fatal would make the assistant feel broken rather than imperfect.
    let turn = 0;
    const r = await runTurn({
      labId: "L", message: "x",
      deps: {
        config: CONFIG, fetchTools: async () => TOOLS,
        chat: async () => (turn++ === 0
          ? { choices: [{ message: { role: "assistant", tool_calls: [
              { id: "c1", function: { name: "create_lab", arguments: "{not json" } }] } }] }
          : say("recovered")),
        execTool: async (body) => { assert.deepEqual(body.arguments, {}); return { ok: true, result: {} }; },
      },
    });
    assert.equal(r.text, "recovered");
  },
};

let pass = 0, fail = 0;
for (const [name, fn] of Object.entries(tests)) {
  try {
    await fn();
    pass++;
    console.log(`  ok   ${name}`);
  } catch (e) {
    fail++;
    console.log(`  FAIL ${name}\n       ${e.message}`);
  }
}
console.log(`  --- ${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
