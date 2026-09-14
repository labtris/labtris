// A ring buffer of everything the browser already knew and would otherwise
// throw away. Installed at module load, before the app renders, because the
// errors worth having are usually the ones from startup.
const MAX = 40;
const MAX_EVENTS = 300;

export const errors = [];
export const failedCalls = [];

//: The same material the annotate tool attaches to a report, kept as a stream
//: the UI can show directly. "Why didn't it start" should be answerable in the
//: app, not only by filing a report and waiting.
export const events = [];
const listeners = new Set();

/** Subscribe to the event stream. Returns an unsubscribe function. */
export function onEvent(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function push(list, entry) {
  list.push(entry);
  if (list.length > MAX) list.shift();
}

/**
 * Record something worth showing. `level` is "error" | "warn" | "info",
 * `source` names where it came from ("browser", "api", "node", "task").
 */
export function noteEvent(level, source, text, meta = {}) {
  const entry = { at: new Date(), level, source, text: String(text).slice(0, 400), ...meta };
  events.push(entry);
  if (events.length > MAX_EVENTS) events.shift();
  for (const fn of listeners) {
    try {
      fn(entry);
    } catch {
      // A broken listener must not swallow the event for everyone else.
    }
  }
  return entry;
}

export function noteError(text) {
  push(errors, `${new Date().toISOString().slice(11, 19)} ${String(text).slice(0, 300)}`);
  noteEvent("error", "browser", text);
}

export function noteFailedCall(method, path, status, message) {
  push(failedCalls, `${method} ${path} -> ${status} ${String(message ?? "").slice(0, 160)}`);
  noteEvent("error", "api", `${method} ${path} → ${status} ${message ?? ""}`.trim(), {
    status,
    path,
  });
}

if (typeof window !== "undefined") {
  window.addEventListener("error", (e) =>
    noteError(`${e.message} @ ${e.filename?.split("/").pop()}:${e.lineno}`),
  );
  window.addEventListener("unhandledrejection", (e) =>
    noteError(`unhandled rejection: ${e.reason?.message ?? e.reason}`),
  );
  // console.error too: a caught-and-logged failure is still a symptom, and is
  // exactly what does not reach window.onerror.
  const original = console.error;
  console.error = (...args) => {
    noteError(args.map((a) => (a instanceof Error ? a.message : String(a))).join(" "));
    original.apply(console, args);
  };
}

/** A short, readable path to an element — enough to find it in the source. */
export function describe(el) {
  if (!el || el === document.body) return "body";
  const parts = [];
  let node = el;
  for (let depth = 0; node && node.nodeType === 1 && depth < 4; depth++) {
    let part = node.tagName.toLowerCase();
    const cls = [...(node.classList || [])].filter((c) => !c.startsWith("svelte-"));
    if (cls.length) part += "." + cls.slice(0, 3).join(".");
    parts.unshift(part);
    node = node.parentElement;
  }
  return parts.join(" > ");
}
