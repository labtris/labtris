//: Stored assistant events → chat bubbles, without a server.
//:
//: Run: node src/lib/aiSessions.test.mjs

import assert from "node:assert/strict";
import { eventsToChat, eventToBubble, pickSession } from "./aiSessions.js";

const ev = (seq, kind, rest = {}) => ({ seq, kind, ...rest });

{
  const { chat, lastSeq, pendingConfirm } = eventsToChat([
    ev(1, "user", { message: "build a triangle", attachments: [] }),
    ev(2, "turn", { text: "Adding nodes.", applied: ["add_node(r1)"] }),
    ev(3, "turn", { text: "", applied: [] }),
    ev(4, "end"),
  ]);
  assert.deepEqual(chat, [
    { role: "you", text: "build a triangle" },
    { role: "labtris", text: "Adding nodes.", applied: ["add_node(r1)"] },
  ]);
  assert.equal(lastSeq, 4);
  assert.equal(pendingConfirm, null);
}

// An approval still open after a reload comes back as the pending one.
{
  const { pendingConfirm, chat } = eventsToChat([
    ev(1, "user", { message: "delete it" }),
    ev(2, "confirm", { id: "c1", tool: "delete_lab", args: { lab_id: "L" } }),
  ]);
  assert.deepEqual(pendingConfirm, { id: "c1", tool: "delete_lab", args: { lab_id: "L" } });
  assert.match(chat[1].text, /delete_lab/);
}

// Answered, or settled by the end of the turn: nothing pending.
{
  assert.equal(eventsToChat([
    ev(1, "confirm", { id: "c1", tool: "stop_node" }),
    ev(2, "answer", { id: "c1", allow: false }),
  ]).pendingConfirm, null);
  assert.equal(eventsToChat([
    ev(1, "confirm", { id: "c1", tool: "stop_node" }),
    ev(2, "cancelled"),
    ev(3, "end"),
  ]).pendingConfirm, null);
}

// Attachments are named, not shown.
assert.deepEqual(
  eventToBubble(ev(1, "user", { message: "look", attachments: [{ filename: "a.png" }, { filename: "b.pdf" }] })),
  { role: "you", text: "look\n(2 attachments: a.png, b.pdf)" },
);
assert.equal(eventToBubble(ev(1, "interrupted")).text.includes("restarted"), true);
assert.equal(eventToBubble(ev(1, "error", { message: "boom" })).error, true);
assert.equal(eventToBubble(ev(1, "end")), null);

// Remembered session wins if it still exists; otherwise the newest.
{
  const s = [{ id: "new" }, { id: "old" }];
  assert.equal(pickSession(s, "old").id, "old");
  assert.equal(pickSession(s, "gone").id, "new");
  assert.equal(pickSession([], "x"), null);
}

console.log("aiSessions: ok");
