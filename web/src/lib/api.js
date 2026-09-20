export const KIND = [
  { id: "alpine", label: "Alpine Linux", image: "alpine:3.20", cmd: ["sleep", "3600"], color: "#3ee0c5", glyph: "▲" },
  { id: "nginx", label: "Nginx", image: "nginx:alpine", cmd: null, color: "#6ee7b7", glyph: "⬢" },
  { id: "redis", label: "Redis", image: "redis:alpine", cmd: null, color: "#f87171", glyph: "◆" },
  { id: "frr", label: "FRRouting", image: "frrouting/frr:v8.4.0", cmd: null, color: "#60a5fa", glyph: "⬡" },
  // bmv2 — the P4 software switch. Ships with a `/p4` mount defaulted to
  // basic_switch.p4; pick another built-in or upload a custom .p4 from
  // the node inspector (or `labtris node p4 …`). Distinct glyph so a
  // programmable-data-plane node is visually different from a plain
  // container on the canvas at a glance.
  {
    id: "bmv2",
    label: "P4 switch (bmv2)",
    image: "p4lang/behavioral-model:latest",
    cmd: null,
    color: "#a78bfa",
    glyph: "⌥",
    boot: 5,
    note: "Programmable data plane. Default program: basic_switch.p4 (L2 forwarding). Swap for ecmp / ecn / trim or upload a custom .p4.",
  },
  { id: "haproxy", label: "HAProxy", image: "haproxy:alpine", cmd: null, color: "#fbbf24", glyph: "▣" },
  { id: "ubuntu", label: "Ubuntu", image: "ubuntu:24.04", cmd: ["sleep", "infinity"], color: "#fb923c", glyph: "●" },
  { id: "busybox", label: "BusyBox", image: "busybox:1.36", cmd: ["sleep", "3600"], color: "#a78bfa", glyph: "■" },
  // A real vendor NOS, and the only one that needs no account — Nokia publish
  // it publicly. It has to run privileged to boot at all, which is host-level
  // trust, so the palette offers it to admins only. `boot` is the wait to a
  // usable CLI: without it the node looks hung for the best part of a minute.
  {
    id: "srlinux",
    label: "Nokia SR Linux",
    image: "ghcr.io/nokia/srlinux:latest",
    cmd: null,
    color: "#c084fc",
    glyph: "◉",
    privileged: true,
    boot: 45,
    note: "Runs privileged — admins only. Emulates a 7220 IXR-D3L. Login admin / NokiaSrl1!",
  },
];

import { noteFailedCall } from "./diagnostics.js";

async function req(path, opts = {}) {
  const r = await fetch(path, {
    // The session is an httpOnly cookie, so it has to be sent explicitly and
    // cannot be read (or stolen) by script.
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  if (r.status === 204) return null;
  const text = await r.text();
  // Not every response is JSON, however much the API intends to be: a proxy
  // can return HTML, and an unhandled server error used to return the bare
  // string "Internal Server Error". Parsing eagerly turned those into
  // "Unexpected token 'I'", which reported the parser's problem instead of
  // the server's and hid the actual failure completely.
  let data = null;
  let unparsed = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      unparsed = text.slice(0, 300);
    }
  }
  if (!r.ok) {
    const msg = data?.error?.message || unparsed || r.statusText || `HTTP ${r.status}`;
    // Recorded before it is thrown, so a report made after the fact still has
    // the call that failed.
    noteFailedCall(opts.method || "GET", path, r.status, msg);
    // A 401 anywhere in the app means the session is gone or was never
    // there — either the cookie expired, or the tab has stale in-memory
    // state from before the last auth deploy and never actually had a
    // session. Broadcast it so the app can fall back to the sign-in
    // screen instead of rendering under a session it doesn't have.
    // The /auth/me and /auth/state calls that legitimately probe for
    // "am I signed in?" opt out via {expected401: true} in opts.
    if (r.status === 401 && !opts.expected401 && typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent("labtris:unauthorized", { detail: { path } }));
    }
    throw new Error(msg);
  }
  if (unparsed !== null) {
    // A 2xx that is not JSON is a bug on the server side, but the caller still
    // deserves to be told which call, not to be handed a parse error.
    noteFailedCall(opts.method || "GET", path, r.status, "response was not JSON");
    throw new Error(`${path} returned ${r.status} with a non-JSON body: ${unparsed}`);
  }
  return data;
}

export const api = {
  health: () => req("/api/v1/health"),
  authState: () => req("/api/v1/auth/state"),
  //: A pre-auth probe. Expected to 401 for anyone not signed in — that's
  //: the whole point of calling it — so opt out of the app-wide 401
  //: broadcast that would otherwise loop the sign-in fallback.
  me: () => req("/api/v1/auth/me", { expected401: true }),
  login: (username, password) =>
    req("/api/v1/auth/login", { method: "POST", body: JSON.stringify({ username, password }) }),
  setup: (username, password, display_name) =>
    req("/api/v1/auth/setup", {
      method: "POST",
      body: JSON.stringify({ username, password, display_name }),
    }),
  logout: () => req("/api/v1/auth/logout", { method: "POST", body: "{}" }),
  users: () => req("/api/v1/users"),
  addUser: (body) => req("/api/v1/users", { method: "POST", body: JSON.stringify(body) }),
  deleteUser: (id) => req(`/api/v1/users/${id}`, { method: "DELETE" }),
  catalog: () => req("/api/v1/catalog"),
  imagePull: (image) =>
    req("/api/v1/images/pull", {
      method: "POST",
      body: JSON.stringify({ image }),
    }),
  imageStatus: (image) =>
    req(`/api/v1/images/status?image=${encodeURIComponent(image)}`),
  labs: () => req("/api/v1/labs"),
  labAddresses: (id) => req(`/api/v1/labs/${id}/addresses`),
  patchNetwork: (id, body) =>
    req(`/api/v1/networks/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  netLeases: (id) => req(`/api/v1/networks/${id}/leases`),
  netSessions: (id) => req(`/api/v1/networks/${id}/sessions`),
  agentTools: () => req("/api/v1/agent/tools"),
  agentTool: (body) =>
    req("/api/v1/agent/tool", { method: "POST", body: JSON.stringify(body) }),
  setPortReservation: (ifaceId, body) =>
    req(`/api/v1/interfaces/${ifaceId}/reservation`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  setPortVlan: (ifaceId, body) =>
    req(`/api/v1/interfaces/${ifaceId}/vlan`, { method: "PATCH", body: JSON.stringify(body) }),
  createLab: (name, description = "", folder = "") =>
    req("/api/v1/labs", {
      method: "POST",
      body: JSON.stringify({ name, description, folder }),
    }),
  folders: () => req("/api/v1/folders"),
  configsets: (id) => req(`/api/v1/labs/${id}/configsets`),
  captureConfigset: (id, name) =>
    req(`/api/v1/labs/${id}/configsets/${encodeURIComponent(name)}/capture`, { method: "POST" }),
  applyConfigset: (id, name) =>
    req(`/api/v1/labs/${id}/configsets/${encodeURIComponent(name)}/apply`, { method: "POST" }),
  deleteConfigset: (id, name) =>
    req(`/api/v1/labs/${id}/configsets/${encodeURIComponent(name)}`, { method: "DELETE" }),
  cloneLab: (id, body = {}) =>
    req(`/api/v1/labs/${id}/clone`, { method: "POST", body: JSON.stringify(body) }),
  renameFolder: (from, to) =>
    req("/api/v1/folders/rename", { method: "POST", body: JSON.stringify({ from, to }) }),
  lab: (id) => req(`/api/v1/labs/${id}`),
  exportLab: (id) => req(`/api/v1/labs/${id}/export`),
  deleteLab: (id) => req(`/api/v1/labs/${id}`, { method: "DELETE" }),
  // Ready hooks (Phase B of the LocalStack-inspired work). YAML is the
  // canonical form the user writes; the server parses + caches.
  hooksGet: (id) => req(`/api/v1/labs/${id}/hooks`),
  hooksApply: (id, source) =>
    req(`/api/v1/labs/${id}/hooks`, { method: "PUT", body: JSON.stringify({ source }) }),
  hooksRun: (id) => req(`/api/v1/labs/${id}/hooks/run`, { method: "POST" }),
  hooksClear: (id) => req(`/api/v1/labs/${id}/hooks`, { method: "DELETE" }),
  // P4 (bmv2) programmable switch — per-node program shape (Phase E1).
  // The /p4 endpoints only make sense against a bmv2 node; the server
  // refuses if the node's image is something else.
  p4Builtins: () => req(`/api/v1/p4/builtins`),
  nodeP4Get: (id) => req(`/api/v1/nodes/${id}/p4`),
  nodeP4SetBuiltin: (id, builtin) =>
    req(`/api/v1/nodes/${id}/p4`, { method: "PUT", body: JSON.stringify({ builtin }) }),
  //: Multipart because a .p4 program can be dozens of KB — small, but
  //: worth streaming through the same shape uploadCompanion uses. req()
  //: hard-codes Content-Type: application/json which would kill the
  //: multipart parse, so fetch directly.
  nodeP4Upload: async (id, file) => {
    const fd = new FormData();
    fd.append("file", file);
    const r = await fetch(`/api/v1/nodes/${id}/p4`, {
      method: "POST",
      credentials: "same-origin",
      body: fd,
    });
    const text = await r.text();
    if (!r.ok) {
      let msg = text;
      try { msg = JSON.parse(text).error?.message || text; } catch {}
      throw new Error(msg || `HTTP ${r.status}`);
    }
    return text ? JSON.parse(text) : null;
  },
  nodeP4Clear: (id) => req(`/api/v1/nodes/${id}/p4`, { method: "DELETE" }),
  // Per-link live traffic stats (Phase E2). Batched: one call for the
  // whole lab, one netd round-trip on the server side. Server caches
  // last few snapshots per interface and derives bps/pps.
  linkStats: (id) => req(`/api/v1/links/${id}/stats`),
  labLinkStats: (id) => req(`/api/v1/labs/${id}/link-stats`),
  geometry: (id) => req(`/api/v1/labs/${id}/geometry`),
  saveGeometry: (id, data) =>
    req(`/api/v1/labs/${id}/geometry`, { method: "PUT", body: JSON.stringify({ data }) }),
  addNode: (labId, body) =>
    req(`/api/v1/labs/${labId}/nodes`, { method: "POST", body: JSON.stringify(body) }),
  node: (id) => req(`/api/v1/nodes/${id}`),
  start: (id) => req(`/api/v1/nodes/${id}/start`, { method: "POST", body: "{}" }),
  wipe: (id) => req(`/api/v1/nodes/${id}/wipe`, { method: "POST", body: "{}" }),
  qemuOptions: () => req("/api/v1/qemu/options"),
  setQemuOptions: (id, opts) =>
    req(`/api/v1/nodes/${id}/qemu-options`, { method: "PATCH", body: JSON.stringify(opts) }),
  stop: (id, mode = "graceful") =>
    req(`/api/v1/nodes/${id}/stop`, { method: "POST", body: JSON.stringify({ mode }) }),
  deleteNode: (id) => req(`/api/v1/nodes/${id}`, { method: "DELETE" }),
  addIface: (id, name) =>
    req(`/api/v1/nodes/${id}/interfaces`, { method: "POST", body: JSON.stringify({ name }) }),
  link: (labId, a, b) =>
    req(`/api/v1/labs/${labId}/links`, {
      method: "POST",
      body: JSON.stringify({ a_iface_id: a, b_iface_id: b }),
    }),
  deleteLink: (id) => req(`/api/v1/links/${id}`, { method: "DELETE" }),
  patchLink: (id, body) => req(`/api/v1/links/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  networks: (labId, body) =>
    req(`/api/v1/labs/${labId}/networks`, { method: "POST", body: JSON.stringify(body) }),
  deleteNetwork: (id) => req(`/api/v1/networks/${id}`, { method: "DELETE" }),
  networkHosts: (id) => req(`/api/v1/networks/${id}/hosts`),
  netCaptureStart: (id, bpf = "") =>
    req(`/api/v1/networks/${id}/capture/start`, { method: "POST", body: JSON.stringify({ bpf }) }),
  netCaptureStop: (id) => req(`/api/v1/networks/${id}/capture/stop`, { method: "POST" }),
  netCaptureRead: (id) => req(`/api/v1/networks/${id}/capture`),
  feedback: (note, context) =>
    req("/api/v1/feedback", { method: "POST", body: JSON.stringify({ note, context }) }),
  feedbackList: (status) => req(`/api/v1/feedback${status ? `?status=${status}` : ""}`),
  feedbackStatus: (id, status) =>
    req(`/api/v1/feedback/${id}`, { method: "PATCH", body: JSON.stringify({ status }) }),
  settings: () => req("/api/v1/settings"),
  saveSettings: (values) =>
    req("/api/v1/settings", { method: "PATCH", body: JSON.stringify({ values }) }),
  gdriveStatus: () => req("/api/v1/backup/gdrive/status"),
  gdriveCredentials: (body) =>
    req("/api/v1/backup/gdrive/credentials", { method: "POST", body: JSON.stringify(body) }),
  gdriveLink: () => req("/api/v1/backup/gdrive/link", { method: "POST", body: "{}" }),
  gdriveLinkComplete: (device_code) =>
    req("/api/v1/backup/gdrive/link/complete", {
      method: "POST",
      body: JSON.stringify({ device_code }),
    }),
  gdrivePush: () => req("/api/v1/backup/gdrive/push", { method: "POST", body: "{}" }),
  gdriveList: () => req("/api/v1/backup/gdrive/list"),
  gdrivePull: (id) => req(`/api/v1/backup/gdrive/pull/${id}`, { method: "POST", body: "{}" }),
  wiresharkStart: (body) =>
    req("/api/v1/wireshark/start", { method: "POST", body: JSON.stringify(body) }),
  wiresharkStop: (id) => req(`/api/v1/wireshark/${id}/stop`, { method: "POST" }),
  hostInterfaces: () => req("/api/v1/system/host-interfaces"),
  setIfaceNetwork: (ifaceId, networkId) =>
    req(`/api/v1/interfaces/${ifaceId}`, {
      method: "PATCH",
      body: JSON.stringify({ network_id: networkId }),
    }),
  presets: () => req("/api/v1/impair/presets"),
  captureStart: (ifaceId, bpf = "") =>
    req(`/api/v1/interfaces/${ifaceId}/capture/start`, {
      method: "POST",
      body: JSON.stringify({ bpf }),
    }),
  captureStop: (ifaceId) => req(`/api/v1/interfaces/${ifaceId}/capture/stop`, { method: "POST" }),
  captureRead: (ifaceId) => req(`/api/v1/interfaces/${ifaceId}/capture`),
  linkCaptureStart: (linkId, bpf = "") =>
    req(`/api/v1/links/${linkId}/capture/start`, { method: "POST", body: JSON.stringify({ bpf }) }),
  linkCaptureStop: (linkId) => req(`/api/v1/links/${linkId}/capture/stop`, { method: "POST" }),
  linkCaptureRead: (linkId) => req(`/api/v1/links/${linkId}/capture`),
  tuning: () => req("/api/v1/system/tuning"),
  diagnostics: () => req("/api/v1/system/diagnostics?fmt=json"),
  applyTuning: () => req("/api/v1/system/tuning", { method: "POST" }),
  managementNetwork: () => req("/api/v1/system/management-network"),
  applyManagementNetwork: (body) =>
    req("/api/v1/system/management-network", { method: "POST", body: JSON.stringify(body) }),
  uplinkBridges: () => req("/api/v1/system/uplink-bridges"),
  applyUplinkBridges: (body) =>
    req("/api/v1/system/uplink-bridges", { method: "POST", body: JSON.stringify(body) }),
  aiStatus: () => req("/api/v1/ai/status"),
  ai: (labId, message) =>
    req(`/api/v1/labs/${labId}/ai`, { method: "POST", body: JSON.stringify({ message }) }),

  renameLab: (id, body) => req(`/api/v1/labs/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  lockLab: (id) => req(`/api/v1/labs/${id}/lock`, { method: "POST" }),
  unlockLab: (id) => req(`/api/v1/labs/${id}/unlock`, { method: "POST" }),
  importLab: (payload) =>
    req("/api/v1/labs/import", { method: "POST", body: JSON.stringify(payload) }),
  importTopology: (content, filename, name) =>
    req("/api/v1/labs/import/topology", {
      method: "POST",
      body: JSON.stringify({ content, filename, name }),
    }),
  importUnl: (xml, name) =>
    req("/api/v1/labs/import/unl", { method: "POST", body: JSON.stringify({ xml, name }) }),

  nodeStyle: (id, body) =>
    req(`/api/v1/nodes/${id}/style`, { method: "PATCH", body: JSON.stringify(body) }),
  nodeResources: (id, body) =>
    req(`/api/v1/nodes/${id}/resources`, { method: "PATCH", body: JSON.stringify(body) }),
  nodeConsole: (id, body) =>
    req(`/api/v1/nodes/${id}/console`, { method: "PATCH", body: JSON.stringify(body) }),
  readConfig: (id) => req(`/api/v1/nodes/${id}/config`),
  saveConfig: (id, content) =>
    req(`/api/v1/nodes/${id}/config`, { method: "PUT", body: JSON.stringify({ content }) }),
  pushConfig: (id) => req(`/api/v1/nodes/${id}/config/push`, { method: "POST" }),
  nodeLogs: (id, lines = 200, pattern = "") =>
    req(`/api/v1/nodes/${id}/logs?lines=${lines}&pattern=${encodeURIComponent(pattern)}`),
  suspend: (id) => req(`/api/v1/nodes/${id}/suspend`, { method: "POST" }),
  resume: (id) => req(`/api/v1/nodes/${id}/resume`, { method: "POST" }),
  exportTemplate: (id, name, icon) =>
    req(`/api/v1/nodes/${id}/export`, { method: "POST", body: JSON.stringify({ name, icon }) }),
  templates: () => req("/api/v1/templates"),
  updateTemplate: (id, body) =>
    req(`/api/v1/templates/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteTemplate: (id) => req(`/api/v1/templates/${id}`, { method: "DELETE" }),
  //: Companion files for a template (BIOS blob or CD-ROM ISO). Multipart
  //: because the file can be gigabytes; JSON body would base64-inflate.
  //: Returns {kind, path, sha256, size}; caller stores `path` in the
  //: template's spec via updateTemplate.
  uploadCompanion: async (kind, file) => {
    const fd = new FormData();
    fd.append("kind", kind);
    fd.append("file", file);
    const r = await fetch("/api/v1/images/companion", {
      method: "POST",
      credentials: "same-origin",
      body: fd,
    });
    const text = await r.text();
    if (!r.ok) {
      let msg = text;
      try { msg = JSON.parse(text).error?.message || text; } catch {}
      throw new Error(msg || `HTTP ${r.status}`);
    }
    return JSON.parse(text);
  },

  saveSnapshot: (id, name) =>
    req(`/api/v1/nodes/${id}/snapshot`, { method: "POST", body: JSON.stringify({ name }) }),
  listSnapshots: (id) => req(`/api/v1/nodes/${id}/snapshots`),
  restoreSnapshot: (id, name) =>
    req(`/api/v1/nodes/${id}/snapshot/${encodeURIComponent(name)}/restore`, { method: "POST" }),

  createTask: (labId, kind) =>
    req(`/api/v1/labs/${labId}/tasks`, { method: "POST", body: JSON.stringify({ kind }) }),
  task: (id) => req(`/api/v1/tasks/${id}`),

  hosts: () => req("/api/v1/hosts"),
  registerHost: (body) => req("/api/v1/hosts", { method: "POST", body: JSON.stringify(body) }),
  patchHost: (id, body) =>
    req(`/api/v1/hosts/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteHost: (id) => req(`/api/v1/hosts/${id}`, { method: "DELETE" }),
  hostCapabilities: (id) => req(`/api/v1/hosts/${id}/capabilities`),
};
