//: Stored assistant events → chat bubbles.
//:
//: A conversation lives on the server as an ordered list of events (see
//: labtris_api/assistant_sessions.py). This turns them into what ChatPane
//: draws, so a conversation reads the same whether it streamed live into this
//: tab or was read back after a reload, a sign-out, or on another device.
//:
//: Run: node src/lib/aiSessions.test.mjs

function attachNote(attachments) {
  if (!attachments?.length) return "";
  const names = attachments.map((a) => a.filename).filter(Boolean).join(", ");
  return `(${attachments.length} attachment${attachments.length === 1 ? "" : "s"}${names ? `: ${names}` : ""})`;
}

//: One stored event as a bubble, or null when it draws nothing by itself.
export function eventToBubble(ev) {
  switch (ev.kind) {
    case "user": {
      const note = attachNote(ev.attachments);
      return { role: "you", text: [ev.message, note].filter(Boolean).join("\n") };
    }
    case "turn": {
      const applied = Array.isArray(ev.applied) ? ev.applied : [];
      if (!ev.text && !applied.length) return null;
      return { role: "labtris", text: ev.text || "", applied };
    }
    case "confirm":
      return { role: "labtris", text: `Asked to run ${ev.tool}; waiting for your approval.` };
    case "answer":
      return { role: "labtris", text: ev.allow ? "You approved it." : "You denied it." };
    case "cancelled":
      return { role: "labtris", text: "(stopped)" };
    case "interrupted":
      return { role: "labtris", text: "(cut off: the Labtris server restarted during this turn)" };
    case "error":
      return { role: "labtris", text: ev.message || "error", error: true };
    default:
      return null;
  }
}

//: The whole conversation as bubbles, plus what the UI needs to pick up
//: where it left off: the last seq seen, and an approval still waiting.
export function eventsToChat(events) {
  const chat = [];
  let lastSeq = 0;
  const open = new Map();
  for (const ev of events) {
    if (typeof ev.seq === "number" && ev.seq > lastSeq) lastSeq = ev.seq;
    if (ev.kind === "confirm") open.set(ev.id, { id: ev.id, tool: ev.tool, args: ev.args || {} });
    if (ev.kind === "answer") open.delete(ev.id);
    //: A turn's end settles anything it was still asking.
    if (ev.kind === "end") open.clear();
    const b = eventToBubble(ev);
    if (b) chat.push(b);
  }
  const pending = [...open.values()];
  return { chat, lastSeq, pendingConfirm: pending.length ? pending[pending.length - 1] : null };
}

//: Which conversation to show for a lab: the one this browser last had open,
//: if it still exists, otherwise the newest.
export function pickSession(sessions, rememberedId) {
  if (!sessions?.length) return null;
  return sessions.find((s) => s.id === rememberedId) || sessions[0];
}
