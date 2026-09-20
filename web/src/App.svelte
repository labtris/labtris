<script>
  import { onDestroy, onMount, tick, untrack } from "svelte";
  import Guacamole from "guacamole-common-js";
  import Hint from "./lib/Hint.svelte";
  import Menu from "./lib/Menu.svelte";
  import SignIn from "./lib/SignIn.svelte";
  import { api, KIND } from "./lib/api.js";
  import { describe, errors, failedCalls, events, noteEvent, onEvent } from "./lib/diagnostics.js";
  import FloatWindow from "./lib/FloatWindow.svelte";
  import VncPane from "./lib/VncPane.svelte";
  import Terminal from "./lib/Terminal.svelte";
  import { loadConfig as loadLlm, saveConfig as saveLlm, runTurn } from "./lib/assistant.js";
  import CapturePane from "./lib/CapturePane.svelte";
  import WiresharkPane from "./lib/WiresharkPane.svelte";
  import SettingsPane from "./lib/SettingsPane.svelte";
  import HooksPane from "./lib/HooksPane.svelte";
  import ChatPane from "./lib/ChatPane.svelte";

  let labs = $state([]);
  let lab = $state(null);
  let geometry = $state({ nodes: {}, nets: {}, links: {}, view: { x: 0, y: 0, k: 1 } });
  let health = $state(null);
  let selected = $state(null);
  let selectedLink = $state(null);
  //: A selected network segment — clicking a port that joins a bridge (rather
  //: than a p2p link) parks the id here so we can highlight every other node
  //: on the same segment. Not exposed in the inspector yet; it's used purely
  //: to compute `hilitedNodeIds`.
  let selectedNet = $state(null);

  //: Nodes that a selected link or segment reaches — used to soft-highlight
  //: the endpoints so "what does this wire connect to" is visually obvious
  //: without opening the inspector. A Set for cheap membership.
  const hilitedNodeIds = $derived.by(() => {
    const out = new Set();
    if (selectedLink && lab) {
      const link = (lab.links || []).find((l) => l.id === selectedLink);
      if (link) {
        for (const n of lab.nodes || []) {
          if (n.interfaces?.some(
            (i) => i.id === link.a_iface_id || i.id === link.b_iface_id,
          )) {
            out.add(n.id);
          }
        }
      }
    }
    if (selectedNet && lab) {
      for (const n of lab.nodes || []) {
        if (n.interfaces?.some((i) => i.network_id === selectedNet)) {
          out.add(n.id);
        }
      }
    }
    return out;
  });
  let error = $state("");
  //: A non-error message that still needs saying — "this will take minutes",
  //: "it is saved, here is where it went". Rendered in the same slot as an
  //: error so there is one place the eye learns to look.
  let note = $state("");
  let creating = $state(false);
  let pan = $state({ x: 80, y: 40, k: 1 });
  let dragging = $state(null);
  let panning = $state(null);
  //: `selected` stays the one the inspector describes; this is everything a
  //: marquee or ctrl-click has gathered, and it always contains `selected`.
  let selectedIds = $state([]);
  let marquee = $state(null);
  let spaceHeld = $state(false);
  let wiring = $state(null);
  //: Which ports are already wired. Derived once rather than per port: this is
  //: read for every port on every node while a drag is in flight.
  const usedIfaces = $derived(
    new Set((lab?.links ?? []).flatMap((l) => [l.a_iface_id, l.b_iface_id])),
  );

  //: A port is a legal drop target if it is free and on another node. Saying so
  //: while the drag is happening is the only thing that teaches that ports are
  //: drag handles at all — the previous UI gave no hint, so people dragged
  //: nodes around and never found the wiring.
  function wireTarget(iface, node) {
    if (!wiring?.from) return false;
    if (wiring.from.id === iface.id) return false;
    if (wiring.from.node_id === iface.node_id) return false;
    return !usedIfaces.has(iface.id);
  }
  let picker = $state(null);
  let canvasEl = $state(null);

  //: Grouped because a flat list of thirteen is a wall. The editor palettes
  //: use each project's published hex values rather than approximations.
  const THEME_GROUPS = [
    {
      group: "Labtris",
      themes: [
        { id: "dark", label: "Dark" },
        { id: "light", label: "Light" },
        { id: "contrast", label: "High contrast" },
      ],
    },
    {
      group: "Editor palettes",
      themes: [
        { id: "gruvbox", label: "Gruvbox" },
        { id: "nord", label: "Nord" },
        { id: "dracula", label: "Dracula" },
        { id: "tokyonight", label: "Tokyo Night" },
        { id: "solarized-dark", label: "Solarized Dark" },
        { id: "solarized-light", label: "Solarized Light" },
        { id: "catppuccin-mocha", label: "Catppuccin Mocha" },
        { id: "catppuccin-latte", label: "Catppuccin Latte" },
        { id: "rose-pine", label: "Rosé Pine" },
        { id: "rose-pine-dawn", label: "Rosé Pine Dawn" },
        { id: "everforest", label: "Everforest" },
        { id: "kanagawa", label: "Kanagawa" },
        { id: "onedark", label: "One Dark" },
        { id: "monokai", label: "Monokai" },
        { id: "ayu-mirage", label: "Ayu Mirage" },
      ],
    },
    {
      group: "Design systems",
      themes: [
        { id: "material-dark", label: "Material Dark" },
        { id: "material-light", label: "Material Light" },
        { id: "carbon", label: "IBM Carbon" },
        { id: "github-dark", label: "GitHub Dark" },
        { id: "github-light", label: "GitHub Light" },
      ],
    },
    {
      group: "Muted",
      themes: [
        { id: "morandi", label: "Morandi" },
        { id: "morandi-dark", label: "Morandi Dark" },
        { id: "zenburn", label: "Zenburn" },
        { id: "sepia", label: "Sepia" },
      ],
    },
    {
      group: "Familiar",
      themes: [
        { id: "cobalt", label: "Cobalt" },
        { id: "cobalt-dark", label: "Cobalt Dark" },
      ],
    },
    {
      group: "Terminal",
      themes: [{ id: "amber", label: "Amber phosphor" }],
    },
  ];
  //: Two themes were named after another product. The ids are persisted, so
  //: rename them in place rather than silently dropping someone's choice.
  const THEME_ALIASES = { "eve-ng": "cobalt", "eve-ng-dark": "cobalt-dark" };

  //: Remembered per browser; first visit follows the OS rather than assuming
  //: everyone wants a dark IDE.
  //: Per-browser preferences. Not on the server: they are about this screen,
  //: and a shared instance where changing your grid changed everyone's would
  //: be a bug rather than a feature.
  let prefs = $state(
    (() => {
      const fallback = { grid: true, snap: true, nodeLabel: "image", dock: "console" };
      try {
        return { ...fallback, ...JSON.parse(localStorage.getItem("labtris.prefs") || "{}") };
      } catch {
        return fallback;
      }
    })(),
  );

  function savePrefs(patch) {
    prefs = { ...prefs, ...patch };
    try {
      localStorage.setItem("labtris.prefs", JSON.stringify(prefs));
    } catch {
      //: A browser with storage disabled still gets the setting for this
      //: session; losing it on reload beats refusing to change it.
    }
  }

  let theme = $state(
    (() => {
      const saved = localStorage.getItem("labtris.theme");
      if (saved) return THEME_ALIASES[saved] || saved;
      return window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
    })(),
  );

  $effect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("labtris.theme", theme);
  });
  let windows = $state([]);
  let zTop = 20;
  let mouse = $state({ x: 0, y: 0 });
  let labWs = null;
  let busy = $state(false);
  //: Which drawer opens with the app, from this browser's preferences.
  let dock = $state(prefs.dock ?? "console");
  //: Chat survives a page reload. The assistant conversation used to
  //: evaporate on refresh, which was surprising — every other panel
  //: (canvas geometry, palette state, theme, terminal tabs) persists,
  //: so the assistant felt uniquely fragile. localStorage per browser
  //: is enough: this key is small (text bubbles + tool call lists;
  //: attachments are dropped from the persisted copy), and there's
  //: one chat across labs which matches the on-screen behaviour.
  let chat = $state(loadChatFromStorage());
  //: Cap the persisted chat at 200 bubbles so localStorage (5 MB per
  //: origin) never fills from an assistant-heavy session. The visible
  //: chat isn't trimmed — only what's serialized to disk — so a
  //: rollover feels like a background operation, not a data loss.
  $effect(() => {
    try {
      const persisted = chat.length > 200 ? chat.slice(-200) : chat;
      localStorage.setItem("labtris.chat", JSON.stringify(persisted));
    } catch {
      // QuotaExceeded — most likely someone pasted a huge attachment
      // filename or the browser's storage is tight. Drop the oldest
      // half rather than throw.
      try {
        const kept = chat.slice(-Math.floor(chat.length / 2));
        localStorage.setItem("labtris.chat", JSON.stringify(kept));
      } catch {
        localStorage.removeItem("labtris.chat");
      }
    }
  });

  function loadChatFromStorage() {
    try {
      const raw = localStorage.getItem("labtris.chat");
      if (!raw) return [];
      const arr = JSON.parse(raw);
      return Array.isArray(arr) ? arr : [];
    } catch {
      return [];
    }
  }

  //: Clears the visible chat + the server-facing history the browser-side
  //: path threads through subsequent turns. The Stop button covers "cancel
  //: this turn"; this covers "start fresh, forget everything above."
  function clearChat() {
    chat = [];
    aiHistory = [];
    liveTurn = null;
    localStorage.removeItem("labtris.chat");
  }
  //: Provider settings for this browser. The key is never sent to Labtris.
  let llm = $state(loadLlm());
  const llmConfigured = () => !!(llm.apiKey && llm.model);
  function setLlm(patch) {
    llm = saveLlm(patch);
  }
  let chatIn = $state("create 2 alpine and wire them");
  let packets = $state([]);
  let capturing = $state(false);
  let bpf = $state("");
  let presets = $state({});
  let tuning = $state(null);
  let customImage = $state("alpine:3.20");
  let customName = $state("");
  let palQ = $state("");
  let poller = null;
  //: Top-bar host meter. Nulls mean "not polled yet"; a fetch failure
  //: leaves the last-known values on screen rather than clearing them
  //: (a briefly-slow API should not flash "…" across the header).
  let hostMeter = $state({ load_pct: null, mem_used_gb: null, mem_total_gb: null });
  let hostMeterTimer = null;
  let templates = $state([]);
  let qemuImages = $state([]);
  let ifaceSchemes = $state([]);
  //: Bring-your-own image upload. Non-null while the modal is open; carries
  //: form state plus, once the upload starts, the XHR progress fraction.
  let uploadForm = $state(null);
  let uploadPct = $state(0);
  //: Editing an existing template — a saved image's iface-scheme, nic-model,
  //: disk-bus, ram/cpus, name/description. Non-null while the modal is open;
  //: carries the template id and the mutable form fields (the source template
  //: row is not touched until Save).
  let editTemplateForm = $state(null);
  //: Which release is showing for each family, so picking Ubuntu Server and
  //: picking 26.04 are one entry rather than two competing rows.
  let pickedVersion = $state({});

  //: One row per OS, newest-first inside it, defaulting to whichever release
  //: the catalog marks — the LTS that is proven, not merely the highest number.
  let qemuFamilies = $derived.by(() => {
    const byFamily = new Map();
    for (const q of qemuImages) {
      const key = q.family || q.id;
      if (!byFamily.has(key)) byFamily.set(key, []);
      byFamily.get(key).push(q);
    }
    return [...byFamily.entries()].map(([key, images]) => {
      images.sort((a, b) => String(b.version || "").localeCompare(String(a.version || "")));
      const fallback = images.find((i) => i.default_version) || images[0];
      const chosen = images.find((i) => i.id === pickedVersion[key]) || fallback;
      return { key, label: images[0].family_label || images[0].label, images, chosen };
    });
  });

  function familyMatches(fam) {
    return (
      palMatch(fam.label, fam.key) ||
      fam.images.some((i) => palMatch(i.label, i.id, i.image, i.version))
    );
  }
  let configText = $state("");
  let configSets = $state({ summary: [], active: null });
  let newSetName = $state("");
  let logsText = $state([]);
  let logsPattern = $state("");
  let fileInput = $state(null);
  let taskProgress = $state(null);
  let snapshots = $state([]);
  let snapshotName = $state("checkpoint1");
  let hosts = $state([]);
  let hostForm = $state({ name: "", endpoint: "", token: "", underlay_ip: "" });
  let vncStatus = $state("idle");
  let vncProtocol = $state("vnc");
  let vncContainerEl = null;
  let vncClient = null;
  let vncKeyboard = null;
  let vncMouse = null;
  let consoleForm = $state({ hostname: "", port: "", username: "", password: "" });
  let hostCaps = $state({});
  //: Both side panels collapse. With the actions on right-click the inspector
  //: is often just taking canvas width, and a topology is the thing you came
  //: to look at. Remembered per browser.
  let paletteOpen = $state(localStorage.getItem("labtris.palette") !== "0");
  let dockMin = $state(localStorage.getItem("labtris.dock.min") === "1");
  $effect(() => localStorage.setItem("labtris.palette", paletteOpen ? "1" : "0"));
  $effect(() => localStorage.setItem("labtris.dock.min", dockMin ? "1" : "0"));

  //: Nothing else loads until we know who is asking — every other call would
  //: 401 and paint a broken app behind the sign-in form.
  let currentUser = $state(null);

  //: The switcher groups by folder rather than showing a tree, because it is a
  //: <select>: optgroups nest exactly one level, which is all a dropdown can
  //: usefully show. Deeper paths still read correctly as their full string.
  //: Networks count too — a lab holding only a bridge is not a blank page,
  //: and the first-run guidance would be wrong over the top of it.
  const runningCount = $derived(
    (lab?.nodes ?? []).filter((n) => n.state === "running").length,
  );
  const stoppedCount = $derived((lab?.nodes ?? []).length - runningCount);

  //: Inspector tab used to inline the whole context — "Node: pan-ha-a" or
  //: "Lab (4 nodes)" — which stretched the tab wider than every sibling
  //: and pushed the row around. The pane's own header already carries the
  //: full name; the tab just needs to say what pane it is. A small chip
  //: next to the label carries the tiny bit of state a person wants to
  //: see without opening the tab: which kind of thing is selected.
  const inspectorContextChip = $derived(
    selectedNode ? "node" : linkObj ? "link" : lab ? `${(lab.nodes || []).length}` : "",
  );

  const labIsEmpty = $derived(
    !!lab && (lab.nodes?.length ?? 0) === 0 && (lab.networks?.length ?? 0) === 0,
  );

  const labsByFolder = $derived.by(() => {
    const groups = new Map();
    for (const l of labs) {
      const key = l.folder || "";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(l);
    }
    return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b));
  });

  //: "ens3" looks like a typo until you know Ubuntu-on-QEMU names ports by PCI
  //: slot. Only worth saying when it is not the eth0 everyone expects.
  const ifaceSchemeLabel = $derived.by(() => {
    const id = selectedNode?.iface_scheme;
    if (!id || id === "eth") return "";
    return ifaceSchemes.find((s) => s.id === id)?.label || "";
  });

  //: A privileged image is host-level trust, so it is not offered to someone
  //: who would only be refused at create time. Hidden, not disabled: a greyed
  //: row invites a support question that has no good answer for a student.
  const palKinds = $derived(
    KIND.filter(
      (k) => palMatch(k.label, k.image) && (!k.privileged || currentUser?.is_admin),
    ),
  );
  let setupRequired = $state(false);
  let authChecked = $state(false);

  async function checkAuth() {
    try {
      currentUser = await api.me();
      setupRequired = false;
    } catch {
      currentUser = null;
      try {
        setupRequired = (await api.authState()).setup_required;
      } catch {
        setupRequired = false;
      }
    } finally {
      authChecked = true;
    }
  }

  //: Any 401 anywhere in the app — a background poll, a WebSocket that
  //: closed because the session lapsed, a click on VNC from a tab that
  //: never actually signed in — clears the in-memory session and forces
  //: the sign-in screen. Without this, a tab with stale currentUser
  //: state (or a bundle that predates the auth-required deploy) would
  //: keep rendering the canvas over 401 responses forever, and the user
  //: would see mysterious "tunnel 519" errors instead of "sign in".
  if (typeof window !== "undefined") {
    window.addEventListener("labtris:unauthorized", async () => {
      if (currentUser === null) return;
      currentUser = null;
      lab = null;
      labs = [];
      try {
        setupRequired = (await api.authState()).setup_required;
      } catch {
        setupRequired = false;
      }
      authChecked = true;
    });
  }

  async function signedIn(user) {
    currentUser = user;
    setupRequired = false;
    await boot();
  }

  async function signOut() {
    try {
      await api.logout();
    } catch {
      /* the cookie is gone either way */
    }
    currentUser = null;
    lab = null;
    labs = [];
  }

  let hostIfaces = $state([]);
  //: A row of green chips saying everything is fine is noise on every render.
  //: Show one dot when it is, and name only the subsystem that broke.
  const HEALTH_PARTS = [
    ["db", "database"],
    ["netd", "netd"],
    ["docker", "docker"],
  ];
  let healthDown = $derived(
    health ? HEALTH_PARTS.filter(([k]) => !health[k]).map(([, label]) => label) : [],
  );
  let healthTitle = $derived(
    healthDown.length
      ? `not responding: ${healthDown.join(", ")}`
      : "database, netd and docker all responding",
  );
  let nicModels = $state([]);
  let aiStatus = $state(null);
  let importReport = $state(null);
  let annotating = $state(false);
  let report = $state(null);
  let reports = $state([]);

  //: The mark, inline rather than an <img>, so it inherits nothing from the
  //: theme and cannot flash in late on a page that has just repainted.
  const LOGO_MARK = `<svg viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Labtris"> <!-- The small variant, for the header chip and the favicon. The full mark has eight nodes and nine edges, which at 20px is a grey smudge; this keeps three blocks and three nodes, at weights that survive being drawn 16 pixels wide. It sits on its own dark tile rather than on the page. That is not decoration: the graph is white because the blocks are saturated, and on the twenty light themes a white node over a pale background is invisible. The tile is the logo's own darkest blue, so the mark carries its contrast with it onto any of the thirty. --> <rect width="64" height="64" rx="15" fill="#0b2f57"/> <g> <path d="M9 14 h12 v20 h11 v9 H9 Z" fill="#0a5ca8"/> <path d="M27 11 h28 v12 H45 v10 H34 V23 h-7 Z" fill="#fd820b"/> <path d="M23 39 h15 v-7 h13 v14 H36 v7 H23 Z" fill="#00b4b9" opacity="0.9"/> </g> <g fill="none" stroke="#fff" stroke-width="3.2" stroke-linecap="round" opacity="0.96"> <path d="M17 21 L30 32 M30 32 L47 19 M30 32 L33 46"/> </g> <g fill="#fff"> <circle cx="17" cy="21" r="5"/> <circle cx="47" cy="19" r="5"/> <circle cx="33" cy="46" r="5"/> <circle cx="30" cy="32" r="5.6"/> </g></svg>`;
  let netForm = $state(null);
  let tcEdit = $state(null);
  let sizeForm = $state(null);

  const W = 176;
  const H = 92;
  const GRID = 24;

  //: The palette is now networks + 11 QEMU images + the Docker catalog +
  //: templates, which is a lot of scrolling to reach one image.
  const palMatch = $derived.by(() => {
    const q = palQ.trim().toLowerCase();
    if (!q) return () => true;
    return (...fields) => fields.filter(Boolean).join(" ").toLowerCase().includes(q);
  });

  function kindFor(image) {
    return KIND.find((k) => k.image === image) || { ...KIND[0], image, label: image, glyph: "◇", color: "#94a3b8" };
  }

  //: A node stores its image as an id — a catalog id like "ubuntu-24.04", or a
  //: content-addressed "custom:<sha>" for a saved or uploaded image. The sha
  //: tells a person nothing, so resolve it back to the template's own name;
  //: a card and the inspector then read "Palo Alto PAN-OS 13.0", not
  //: "custom:869d87f3…". Falls back to the raw id when nothing matches.
  function imageLabel(image) {
    if (!image) return image;
    const t = templates.find((x) => x.image === image);
    if (t) return t.name;
    const q = qemuImages.find((x) => x.image === image || x.id === image);
    if (q) return q.label;
    return image;
  }

  function pos(id, i = 0) {
    const p = geometry.nodes?.[id];
    if (p) return p;
    const col = i % 4;
    const row = Math.floor(i / 4);
    return { x: 80 + col * 240, y: 80 + row * 160 };
  }

  function portPos(node, iface, index, iNode, towards) {
    // Leave the node on the side the other end actually is. Fixing the side by
    // interface index meant a wire to a node on the left still exited right and
    // looped back over its own box.
    const p = pos(node.id, iNode);
    const n = Math.max(node.interfaces.length, 1);
    const y = p.y + 28 + (index * (H - 40)) / n;
    const left = towards === undefined ? iface.idx % 2 === 0 : towards < p.x + W / 2;
    return { x: left ? p.x : p.x + W, y, left };
  }

  function labelAt(end) {
    // Sit the name just outside its own port, on the side the wire leaves, so
    // each label reads as belonging to the device it touches rather than
    // floating in the middle of the link.
    return {
      x: end.x + (end.left ? -8 : 8),
      y: end.y - 5,
      anchor: end.left ? "end" : "start",
    };
  }

  function wirePath(a, b) {
    // Control points pushed out along each end's own exit direction, scaled to
    // the gap, so the curve leaves horizontally and arrives horizontally
    // without the big loop a fixed midpoint produced for stacked nodes.
    const dx = Math.abs(b.x - a.x);
    const dy = Math.abs(b.y - a.y);
    const reach = Math.max(48, Math.min(200, dx * 0.5 + dy * 0.25));
    const c1 = a.x + (a.left ? -reach : reach);
    const c2 = b.x + (b.left ? -reach : reach);
    return `M ${a.x} ${a.y} C ${c1} ${a.y}, ${c2} ${b.y}, ${b.x} ${b.y}`;
  }

  const NET_W = 150;
  const NET_H = 38;

  function netAnchor(net, ni, towards) {
    const p = netPos(net.id, ni);
    const left = towards < p.x + NET_W / 2;
    return { x: left ? p.x : p.x + NET_W, y: p.y + NET_H / 2, left };
  }

  //: Every interface that sits on a named segment, paired with that segment —
  //: joining a bridge draws no Link row, so these would otherwise be invisible.
  const segmentWires = $derived.by(() => {
    if (!lab) return [];
    const named = (lab.networks || []).filter(
      (n) => n.kind !== "vxlan" && !n.name.startsWith("lnk-"),
    );
    const out = [];
    for (const node of lab.nodes) {
      for (const iface of node.interfaces) {
        const ni = named.findIndex((n) => n.id === iface.network_id);
        if (ni < 0) continue;
        out.push({ node, iface, net: named[ni], ni, key: `${iface.id}` });
      }
    }
    return out;
  });

  function snap(v) {
    //: Snapping off means the value passes through: the grid is still drawn
    //: (or not) independently, because "I want to see it" and "I want to be
    //: held to it" are different preferences.
    return prefs.snap ? Math.round(v / GRID) * GRID : Math.round(v);
  }

  async function refreshLabs() {
    labs = await api.labs();
  }

  function geoData() {
    return {
      nodes: { ...(geometry.nodes || {}) },
      nets: { ...(geometry.nets || {}) },
      links: { ...(geometry.links || {}) },
      view: { x: pan.x, y: pan.y, k: pan.k },
    };
  }

  //: diagnostics.js owns the buffer (it starts filling before this component
  //: exists); this is the reactive mirror the dock renders.
  let eventLog = $state([...events]);
  let eventsSeen = $state(events.length);
  let unseenErrors = $derived(
    eventLog.slice(eventsSeen).filter((e) => e.level === "error").length,
  );
  $effect(() => onEvent((entry) => (eventLog = [...eventLog, entry])));

  let eventsErrorsOnly = $state(false);
  //: Newest first: the thing you came to look at just happened.
  let shownEvents = $derived(
    (eventsErrorsOnly ? eventLog.filter((e) => e.level === "error") : eventLog).slice().reverse(),
  );

  function clearEvents() {
    events.length = 0;
    eventLog = [];
    eventsSeen = 0;
  }

  let refreshTimer = null;

  function connectLabWs(id) {
    labWs?.close();
    const proto = location.protocol === "https:" ? "wss" : "ws";
    labWs = new WebSocket(`${proto}://${location.host}/api/v1/labs/${id}/ws`);
    labWs.onmessage = (ev) => {
      if (lab?.id !== id) return;
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === "node") {
          const who = msg.name || msg.id?.slice(-6) || "node";
          if (msg.state === "failed") {
            noteEvent("error", "node", `${who} failed to start: ${msg.error || "no reason given"}`, {
              nodeId: msg.id,
            });
          } else if (msg.state) {
            noteEvent("info", "node", `${who} is ${msg.state}`, { nodeId: msg.id });
          }
        } else if (msg.type === "task") {
          noteEvent(
            msg.status === "failed" ? "error" : "info",
            "task",
            `${msg.kind || "task"}: ${msg.status}${msg.message ? " — " + msg.message : ""}`,
          );
        }
      } catch {
        // Not every frame is JSON we understand; the refresh below still runs.
      }
      // Bursts are the norm here — "start all" publishes per node. Refetching
      // the lab per message cost a round trip each, which is a second apiece
      // from far away. Coalesce them.
      clearTimeout(refreshTimer);
      refreshTimer = setTimeout(() => loadLab(id, true), 250);
    };
  }

  async function loadLab(id, quiet = false) {
    // Never let a background refresh yank a gesture out from under the user.
    if (quiet && (dragging || panning)) return;
    if (!quiet) error = "";
    lab = await api.lab(id);
    if (!quiet) {
      //: Otherwise switching labs while the Config tab is open leaves the
      //: previous lab's sets on screen, with working Apply buttons.
      if (dock === "config") loadConfigSets();
    }
    // Layout is client-owned while a lab is open — we are the only writer. Only
    // an explicit open needs to read it back, which halves the requests a
    // background refresh makes.
    if (quiet) return;
    const g = await api.geometry(id);
    const incoming = g.data || {};
    geometry = {
      nodes: { ...(incoming.nodes || {}) },
      nets: { ...(incoming.nets || {}) },
      links: { ...(incoming.links || {}) },
      view: incoming.view || { x: 80, y: 40, k: 1 },
    };
    pan = geometry.view || pan;
    connectLabWs(id);
  }

  let geoTimer = null;

  async function persistGeo() {
    if (!lab) return;
    const data = geoData();
    geometry = data;
    // Nudging five nodes into place should not be five round trips.
    clearTimeout(geoTimer);
    const labId = lab.id;
    geoTimer = setTimeout(() => api.saveGeometry(labId, data).catch(() => {}), 400);
  }

  async function dropKind(kind, clientX, clientY, canvasEl) {
    if (!lab) {
      error = "Create or open a lab first";
      return;
    }
    const rect = canvasEl.getBoundingClientRect();
    const x = snap((clientX - rect.left - pan.x) / pan.k);
    const y = snap((clientY - rect.top - pan.y) / pan.k);
    await spawnNode(kind.image, kind.cmd, x, y, kind.id);
  }

  async function spawnNode(image, cmd, x, y, prefix, runtime = "docker") {
    busy = true;
    const labId = lab.id;
    try {
      const node = await api.addNode(labId, {
        name: `${prefix || image.split(":")[0].replace("/", "-")}-${Math.random().toString(36).slice(2, 6)}`,
        runtime,
        image,
        cmd: cmd || undefined,
        interfaces: runtime === "qemu" ? [] : [{ name: "eth1" }],
      });
      const nodes = { ...(geometry.nodes || {}), [node.id]: { x, y } };
      geometry = { ...geoData(), nodes };
      await persistGeo();
      await loadLab(labId, true);
      selected = node.id;
      selectedLink = null;
      selectedNet = null;
    linkPop = null;
    } catch (e) {
      error = e.message;
    } finally {
      busy = false;
    }
  }

  async function dropCustom(clientX, clientY, canvasEl) {
    const image = customImage.trim();
    if (!image) return;
    const rect = canvasEl.getBoundingClientRect();
    const x = snap((clientX - rect.left - pan.x) / pan.k);
    const y = snap((clientY - rect.top - pan.y) / pan.k);
    const cmd = image.includes("alpine") || image.includes("ubuntu") || image.includes("busybox")
      ? ["sleep", "3600"]
      : undefined;
    await spawnNode(image, cmd, x, y, customName.trim() || image.split(":")[0]);
  }


  //: Start any node by id — the console pane offers "start and attach" for the
  //: node it is showing, which is not necessarily the selected one.
  async function startNode(id) {
    busy = true;
    try {
      await api.start(id);
      await loadLab(lab.id, true);
    } catch (e) {
      error = e.message;
    } finally {
      busy = false;
    }
  }

  async function startSelected() {
    if (!selected) return;
    busy = true;
    try {
      await api.start(selected);
      await loadLab(lab.id, true);
    } catch (e) {
      error = e.message;
    } finally {
      busy = false;
    }
  }

  async function stopSelected() {
    if (!selected) return;
    busy = true;
    try {
      await api.stop(selected);
      await loadLab(lab.id, true);
    } catch (e) {
      error = e.message;
    } finally {
      busy = false;
    }
  }

  async function startAll() {
    if (!lab) return;
    busy = true;
    try {
      for (const n of lab.nodes) await api.start(n.id);
      await loadLab(lab.id, true);
    } catch (e) {
      error = e.message;
    } finally {
      busy = false;
    }
  }

  async function stopAll() {
    if (!lab) return;
    busy = true;
    try {
      for (const n of lab.nodes) await api.stop(n.id, "force");
      await loadLab(lab.id, true);
    } catch (e) {
      error = e.message;
    } finally {
      busy = false;
    }
  }

  async function removeSelected() {
    if (!selected || !lab) return;
    busy = true;
    try {
      await api.deleteNode(selected);
      selected = null;
      await loadLab(lab.id, true);
    } catch (e) {
      error = e.message;
    } finally {
      busy = false;
    }
  }

  async function addPort() {
    if (!selected) return;
    //: No name: the server knows which scheme this node's guest follows, and
    //: guessing `eth<n>` here was both wrong for SR Linux and off by one.
    try {
      await api.addIface(selected, null);
      await loadLab(lab.id, true);
    } catch (e) {
      error = e.message;
    }
  }

  function onNetDown(e, net, i) {
    if (e.button !== 0 || e.target.closest?.("button")) return;
    e.stopPropagation();
    try {
      e.currentTarget.setPointerCapture?.(e.pointerId);
    } catch {
      /* capture unsupported */
    }
    const p = netPos(net.id, i);
    dragging = {
      id: net.id,
      kind: "net",
      dx: e.clientX - pan.x - p.x * pan.k,
      dy: e.clientY - pan.y - p.y * pan.k,
    };
  }

  function onNodeDown(e, node, i) {
    if (e.button !== 0) return;
    e.stopPropagation();
    // Pointer capture keeps the gesture bound to this element even once the
    // pointer leaves it, which is what makes a fast drag survive crossing the
    // inspector or the palette.
    try {
      e.currentTarget.setPointerCapture?.(e.pointerId);
    } catch {
      /* not a pointer event, or capture unsupported */
    }
    if (e.ctrlKey || e.metaKey || e.shiftKey) {
      selectedIds = selectedIds.includes(node.id)
        ? selectedIds.filter((id) => id !== node.id)
        : [...selectedIds, node.id];
    } else if (!selectedIds.includes(node.id)) {
      selectedIds = [node.id];
    }
    selected = node.id;
    selectedLink = null;
    selectedNet = null;
    linkPop = null;
    snapshots = [];
    if (node.runtime === "qemu") loadSnapshots();
    const p = pos(node.id, i);
    const group =
      selectedIds.length > 1 && selectedIds.includes(node.id)
        ? selectedIds
            .filter((id) => id !== node.id)
            .map((id) => {
              const idx = (lab?.nodes || []).findIndex((n) => n.id === id);
              const q = pos(id, idx < 0 ? 0 : idx);
              return { id, x0: q.x, y0: q.y };
            })
        : [];
    dragging = {
      id: node.id,
      dx: e.clientX - pan.x - p.x * pan.k,
      dy: e.clientY - pan.y - p.y * pan.k,
      originX: p.x,
      originY: p.y,
      group,
    };
  }

  function onCanvasMove(e) {
    mouse = { x: e.clientX, y: e.clientY };
    if (panning) {
      pan = { ...pan, x: e.clientX - panning.dx, y: e.clientY - panning.dy };
      return;
    }
    if (marquee) {
      const box = canvasBox();
      marquee = {
        ...marquee,
        x1: (e.clientX - box.left - pan.x) / pan.k,
        y1: (e.clientY - box.top - pan.y) / pan.k,
      };
      return;
    }
    if (dragging && lab) {
      const x = snap((e.clientX - pan.x - dragging.dx) / pan.k);
      const y = snap((e.clientY - pan.y - dragging.dy) / pan.k);
      const bucket = dragging.kind === "net" ? "nets" : "nodes";
      const moved = { ...(geometry[bucket] || {}), [dragging.id]: { x, y } };
      // Dragging one of several selected nodes moves the group: the whole
      // point of gathering them was to treat them as one thing.
      if (dragging.group?.length) {
        const dx = x - dragging.originX;
        const dy = y - dragging.originY;
        for (const { id, x0, y0 } of dragging.group) {
          moved[id] = { x: snap(x0 + dx), y: snap(y0 + dy) };
        }
      }
      geometry = { ...geoData(), [bucket]: moved };
    }
  }

  async function onCanvasUp() {
    if (panning) panning = null;
    if (marquee) {
      const hit = idsUnderMarquee();
      selectedIds = marquee.add ? [...new Set([...selectedIds, ...hit])] : hit;
      // A marquee round exactly one node should leave the inspector on it,
      // the same as clicking it would.
      if (selectedIds.length === 1) selected = selectedIds[0];
      marquee = null;
    }
    if (wiring) wiring = null;
    if (dragging) {
      dragging = null;
      await persistGeo();
    }
  }

  function onCanvasDown(e) {
    if (e.target !== e.currentTarget && !e.target.classList?.contains("grid") && !e.target.classList?.contains("wires")) {
      return;
    }
    // Dragging empty canvas used to pan, so an attempt to rubber-band a group
    // looked like every device sliding sideways at once. Marquee is what this
    // gesture means everywhere else; pan moved to the middle button, space, or
    // shift — the same three every canvas tool uses.
    if (e.button === 1 || e.button === 2 || spaceHeld) {
      panning = { dx: e.clientX - pan.x, dy: e.clientY - pan.y };
      return;
    }
    if (e.button !== 0) return;
    const add = e.ctrlKey || e.metaKey || e.shiftKey;
    selected = null;
    selectedLink = null;
    selectedNet = null;
    linkPop = null;
    if (!add) selectedIds = [];
    const box = canvasBox();
    const at = {
      x: (e.clientX - box.left - pan.x) / pan.k,
      y: (e.clientY - box.top - pan.y) / pan.k,
    };
    marquee = { x0: at.x, y0: at.y, x1: at.x, y1: at.y, add };
  }

  //: Space-to-pan, the convention everywhere from Figma to GNS3. Ignored while
  //: typing so it does not eat spaces in the console or the AI box.
  function onKeyDown(e) {
    const el = e.target;
    const typing =
      el?.tagName === "INPUT" || el?.tagName === "TEXTAREA" || el?.isContentEditable;
    if (e.key === " " && !typing) {
      spaceHeld = true;
      return;
    }
    //: ⌘K is deliberately above the `typing` guard: a palette you cannot open
    //: without first clicking out of a text field is a palette people stop
    //: reaching for.
    if ((e.key === "k" || e.key === "K") && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      openPalette();
      return;
    }
    if (typing) return;
    //: F2 renames the open lab, the convention everywhere from Explorer to
    //: Blender. The menu names the shortcut so it is discoverable rather than
    //: folklore.
    if (e.key === "F2" && lab && !lab.locked) {
      e.preventDefault();
      startEdit("rename");
      return;
    }
    if (e.key === "Escape") {
      marquee = null;
      selectedIds = [];
    }
    if ((e.key === "Delete" || e.key === "Backspace") && selectedIds.length > 1) {
      e.preventDefault();
      deleteSelection();
    }
    if ((e.key === "a" || e.key === "A") && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      selectedIds = (lab?.nodes || []).map((n) => n.id);
    }
  }

  function onKeyUp(e) {
    if (e.key === " ") spaceHeld = false;
  }

  function marqueeRect() {
    if (!marquee) return null;
    return {
      x: Math.min(marquee.x0, marquee.x1),
      y: Math.min(marquee.y0, marquee.y1),
      w: Math.abs(marquee.x1 - marquee.x0),
      h: Math.abs(marquee.y1 - marquee.y0),
    };
  }

  //: Anything the box touches, not only what it fully contains — a partial
  //: sweep across a row of nodes is the usual way people select them.
  //:
  //: Measured from the rendered elements rather than from a constant. A node
  //: is `min-height`, not `height`: it grows with every interface it carries,
  //: so a fixed 176x92 under-measured the tall ones and a sweep that visibly
  //: crossed them selected nothing. offsetLeft/offsetTop are relative to
  //: `.grid`, the positioned ancestor, and are unaffected by its CSS
  //: transform — the same space the marquee rectangle is computed in.
  function idsUnderMarquee() {
    const r = marqueeRect();
    if (!r || !lab) return [];
    const hits = [];
    for (const el of document.querySelectorAll(".grid .node[data-id]")) {
      const x = el.offsetLeft;
      const y = el.offsetTop;
      if (
        x < r.x + r.w &&
        x + el.offsetWidth > r.x &&
        y < r.y + r.h &&
        y + el.offsetHeight > r.y
      ) {
        hits.push(el.dataset.id);
      }
    }
    return hits;
  }

  let qemuOptSchema = $state(null);
  let qemuOptForm = $state(null);

  async function openQemuOptions() {
    if (!selectedNode) return;
    try {
      if (!qemuOptSchema) qemuOptSchema = await api.qemuOptions();
      const defaults = Object.fromEntries(
        Object.entries(qemuOptSchema.options).map(([k, v]) => [k, v.default]),
      );
      qemuOptForm = { ...defaults, ...(selectedNode.qemu_opts || {}) };
    } catch (e) {
      error = e.message;
    }
  }

  async function saveQemuOptions() {
    if (!selectedNode || !qemuOptForm) return;
    busy = true;
    try {
      await api.setQemuOptions(selectedNode.id, qemuOptForm);
      qemuOptForm = null;
      await loadLab(lab.id, true);
    } catch (e) {
      error = e.message;
    } finally {
      busy = false;
    }
  }

  async function wipeNodes(ids) {
    if (!ids.length || !lab) return;
    const what = ids.length === 1 ? "this node's disk" : `${ids.length} nodes' disks`;
    if (!confirm(`Wipe ${what}? The topology stays; the guest goes back to a fresh image.`)) {
      return;
    }
    busy = true;
    try {
      for (const id of ids) await api.wipe(id);
      await loadLab(lab.id, true);
    } catch (e) {
      error = e.message;
    } finally {
      busy = false;
    }
  }

  async function deleteSelection() {
    if (!selectedIds.length || !lab) return;
    busy = true;
    try {
      for (const id of selectedIds) await api.deleteNode(id);
      selectedIds = [];
      selected = null;
      await loadLab(lab.id, true);
    } catch (e) {
      error = e.message;
    } finally {
      busy = false;
    }
  }

  async function startSelection(start) {
    if (!selectedIds.length || !lab) return;
    busy = true;
    try {
      for (const id of selectedIds) {
        await (start ? api.start(id) : api.stop(id));
      }
      await loadLab(lab.id, true);
    } catch (e) {
      error = e.message;
    } finally {
      busy = false;
    }
  }

  function onWheel(e) {
    e.preventDefault();
    // Wheel and two-finger scroll pan; ctrl/cmd-wheel zooms. That is the
    // convention every canvas tool uses, and browsers already report a
    // trackpad pinch as ctrl+wheel, so pinch-to-zoom comes along with it.
    // Zooming on a bare wheel left no way to move sideways at all.
    if (!e.ctrlKey && !e.metaKey) {
      // Shift makes a vertical-only wheel scroll horizontally, which is what
      // a mouse without a tilt wheel needs.
      const dx = e.shiftKey ? e.deltaY : e.deltaX;
      const dy = e.shiftKey ? 0 : e.deltaY;
      pan = { ...pan, x: pan.x - dx, y: pan.y - dy };
      return;
    }
    const factor = e.deltaY < 0 ? 1.08 : 0.92;
    const k = Math.min(2.2, Math.max(0.25, pan.k * factor));
    // Keep the point under the cursor still. Zooming about the origin makes
    // whatever you were looking at slide off the screen.
    const box = canvasBox();
    const mx = e.clientX - box.left;
    const my = e.clientY - box.top;
    const wx = (mx - pan.x) / pan.k;
    const wy = (my - pan.y) / pan.k;
    pan = { x: mx - wx * k, y: my - wy * k, k };
  }

  function beginWire(e, iface) {
    e.stopPropagation();
    // If the port is already wired, a "beginWire" would only ever fail
    // (the existing link stops any second one from taking hold) — and
    // while wiring is armed, every other port on every other node
    // lights up as wire-ok/wire-no, which reads as "everything is
    // selected." Instead: on click of a wired port, select the link
    // (or the network) so the two endpoints highlight and the link
    // inspector shows the qos/admin controls.
    if (iface.network_id) {
      const link = (lab?.links ?? []).find(
        (l) => l.a_iface_id === iface.id || l.b_iface_id === iface.id,
      );
      if (link) {
        selectedLink = link.id;
        selectedNet = null;
      } else {
        selectedNet = iface.network_id;
        selectedLink = null;
      }
      selected = null;
      selectedIds = [];
      return;
    }
    wiring = { from: iface };
  }

  async function endWire(e, iface) {
    e.stopPropagation();
    if (!wiring || !lab) return;
    if (wiring.from.id === iface.id) {
      wiring = null;
      return;
    }
    busy = true;
    try {
      await api.link(lab.id, wiring.from.id, iface.id);
      await loadLab(lab.id, true);
    } catch (err) {
      error = err.message;
    } finally {
      wiring = null;
      busy = false;
    }
  }

  function beginNodeWire(e, node) {
    e.stopPropagation();
    // Deliberately no setPointerCapture here: capture would deliver the
    // release back to this handle, and the whole point is to learn which
    // *other* node the pointer was let go over.
    const rect = e.currentTarget.getBoundingClientRect();
    wiring = {
      fromNode: node,
      // Where the gesture started, in canvas coordinates, so the rubber-band
      // stays anchored to the handle while the canvas is panned or zoomed.
      from: {
        x: (rect.left + rect.width / 2 - canvasBox().left - pan.x) / pan.k,
        y: (rect.top + rect.height / 2 - canvasBox().top - pan.y) / pan.k,
      },
    };
  }

  function canvasBox() {
    return canvasEl?.getBoundingClientRect() ?? { left: 0, top: 0 };
  }

  //: The cursor in canvas coordinates — the loose end of the rubber-band.
  const wireEnd = $derived.by(() => {
    const box = canvasBox();
    return {
      x: (mouse.x - box.left - pan.x) / pan.k,
      y: (mouse.y - box.top - pan.y) / pan.k,
    };
  });

  function freeIfaces(node) {
    // An interface already carrying a link can't take another.
    const used = new Set((lab?.links || []).flatMap((l) => [l.a_iface_id, l.b_iface_id]));
    return (node?.interfaces || []).filter((i) => !used.has(i.id));
  }

  function endNodeWire(e, node) {
    if (!wiring?.fromNode || wiring.fromNode.id === node.id) {
      wiring = null;
      return;
    }
    e.stopPropagation();
    const a = wiring.fromNode;
    wiring = null;
    // Ports are 30px pills; with eight interfaces on a node they are unusable.
    // Ask instead — and offer a new interface, since that is the usual answer.
    picker = {
      a,
      b: node,
      aFree: freeIfaces(a),
      bFree: freeIfaces(node),
      aIface: freeIfaces(a)[0]?.id || "__new",
      bIface: freeIfaces(node)[0]?.id || "__new",
    };
  }

  async function confirmPicker() {
    if (!picker || !lab) return;
    busy = true;
    const { a, b, aIface, bIface } = picker;
    picker = null;
    try {
      const aId = aIface === "__new" ? (await api.addIface(a.id, null)).id : aIface;
      const bId = bIface === "__new" ? (await api.addIface(b.id, null)).id : bIface;
      await api.link(lab.id, aId, bId);
      await loadLab(lab.id, true);
    } catch (err) {
      error = err.message;
    } finally {
      busy = false;
    }
  }

  // Networks are canvas objects too: an internal bridge joins nodes to each
  // other, a cloud enslaves one of the host's own NICs so the lab can reach
  // the outside world (EVE-NG calls this pnet).
  function netPos(id, i = 0) {
    return geometry.nets?.[id] || { x: 120 + i * 240, y: 420 };
  }

  async function startNetwork(kind, clientX, clientY, canvasEl) {
    if (!lab) {
      error = "Create or open a lab first";
      return;
    }
    const rect = canvasEl.getBoundingClientRect();
    const at = {
      x: snap((clientX - rect.left - pan.x) / pan.k),
      y: snap((clientY - rect.top - pan.y) / pan.k),
    };
    if (kind === "cloud") {
      try {
        hostIfaces = (await api.hostInterfaces()).interfaces || [];
      } catch (e) {
        error = e.message;
        return;
      }
      const usable = hostIfaces.filter((i) => i.usable !== false);
      netForm = {
        kind,
        at,
        name: `cloud-${Math.random().toString(36).slice(2, 5)}`,
        cloud_ref: usable.find((i) => !i.default_route)?.name || usable[0]?.name || "",
        allow_default_route: false,
      };
    } else {
      netForm = {
        kind,
        at,
        name: `${kind === "nat" ? "nat" : "net"}-${Math.random().toString(36).slice(2, 5)}`,
        cloud_ref: "",
        //: Blank means "pick me a free /24". Filling it in is for the lab that
        //: has to be on a particular prefix, which is the rarer case.
        subnet: "",
        dhcp: true,
        vlan_aware: false,
        vlan_proto: "802.1Q",
      };
    }
  }

  async function confirmNetwork() {
    if (!netForm || !lab) return;
    const { kind, at, name, cloud_ref, allow_default_route, subnet, dhcp, vlan_aware, vlan_proto } =
      netForm;
    netForm = null;
    busy = true;
    try {
      const net = await api.networks(lab.id, {
        name,
        kind,
        cloud_ref: kind === "cloud" ? cloud_ref : null,
        allow_default_route: !!allow_default_route,
        //: Empty string means "choose one for me" — sending it as null keeps
        //: that decision on the server, where it can see every other subnet.
        subnet: kind === "nat" ? (subnet || "").trim() || null : null,
        dhcp: kind === "nat" ? !!dhcp : false,
        vlan_aware: !!vlan_aware,
        vlan_proto: vlan_proto || "802.1Q",
      });
      geometry = { ...geoData(), nets: { ...(geometry.nets || {}), [net.id]: at } };
      await persistGeo();
      await loadLab(lab.id, true);
    } catch (e) {
      error = e.message;
    } finally {
      busy = false;
    }
  }

  async function removeNetwork(net) {
    busy = true;
    try {
      await api.deleteNetwork(net.id);
      await loadLab(lab.id, true);
    } catch (e) {
      error = e.message;
    } finally {
      busy = false;
    }
  }

  function endNetWire(e, net) {
    if (!wiring?.fromNode) return;
    e.stopPropagation();
    const a = wiring.fromNode;
    wiring = null;
    picker = {
      a,
      net,
      aFree: freeIfaces(a),
      aIface: freeIfaces(a)[0]?.id || "__new",
    };
  }

  async function joinNetwork() {
    if (!picker?.net || !lab) return;
    busy = true;
    const { a, net, aIface } = picker;
    picker = null;
    try {
      const id = aIface === "__new" ? (await api.addIface(a.id, null)).id : aIface;
      await api.setIfaceNetwork(id, net.id);
      await loadLab(lab.id, true);
    } catch (e) {
      error = e.message;
    } finally {
      busy = false;
    }
  }

  //: Point at the thing that is wrong instead of describing it. The note is
  //: the easy half; the value is everything captured alongside it, because
  //: "the canvas is not draggable" and "which element, which theme, which
  //: viewport, and what the console had already logged" are very different
  //: bug reports.
  function startAnnotate() {
    annotating = true;
    report = null;
  }

  function captureAt(e) {
    if (!annotating) return;
    e.preventDefault();
    e.stopPropagation();
    // The veil is what the pointer actually hit, so ask what is under it —
    // otherwise every report says the problem is with the reporting overlay.
    const veil = document.querySelector(".annotate-veil");
    const previous = veil?.style.pointerEvents;
    if (veil) veil.style.pointerEvents = "none";
    const el = document.elementFromPoint(e.clientX, e.clientY);
    if (veil) veil.style.pointerEvents = previous ?? "";
    const rect = el?.getBoundingClientRect();
    annotating = false;
    report = {
      note: "",
      x: e.clientX,
      y: e.clientY,
      context: {
        element: describe(el),
        label: (el?.innerText || "").trim().slice(0, 80),
        rect: rect && {
          x: Math.round(rect.x),
          y: Math.round(rect.y),
          w: Math.round(rect.width),
          h: Math.round(rect.height),
        },
        theme,
        viewport: `${window.innerWidth}x${window.innerHeight}`,
        zoom: pan.k.toFixed(2),
        lab: lab?.name || null,
        selected: selectedNode?.name || (selectedLink ? "a link" : null),
        dock,
        windows: windows.map((w) => w.kind),
        errors: [...errors],
        failedCalls: [...failedCalls],
        userAgent: navigator.userAgent,
      },
    };
  }

  async function sendReport() {
    if (!report?.note.trim()) return;
    const payload = report;
    report = null;
    try {
      await api.feedback(payload.note.trim(), payload.context);
      await loadReports();
    } catch (e) {
      error = e.message;
    }
  }

  async function loadReports() {
    try {
      reports = await api.feedbackList("open");
    } catch {
      reports = [];
    }
  }

  async function resolveReport(id) {
    try {
      await api.feedbackStatus(id, "done");
      await loadReports();
    } catch (e) {
      error = e.message;
    }
  }

  function openWindow(kind, key, title, extra = {}) {
    // One window per (kind, subject): clicking Console twice on the same node
    // should raise the window you already have, not stack a second socket on
    // top of it.
    const id = `${kind}:${key}`;
    const existing = windows.find((w) => w.id === id);
    if (existing) {
      existing.min = false;
      focusWindow(id);
      return;
    }
    const big = kind === "vnc" || kind === "rdp";
    const want = { w: big ? 900 : 620, h: big ? 620 : 340, ...extra };
    // A window wider than the screen is not openable and a cascade that walks
    // off the edge hides the title bar you need to drag it back. Fit first,
    // then place, then clamp.
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const w = Math.min(want.w, vw - 40);
    const h = Math.min(want.h, vh - 80);
    const offset = windows.length % 6;
    const x = Math.max(12, Math.min(260 + offset * 28, vw - w - 12));
    const y = Math.max(56, Math.min(90 + offset * 26, vh - h - 12));
    windows = [
      ...windows,
      { id, kind, title, status: "", z: ++zTop, min: false, ...extra, x, y, w, h },
    ];
  }

  //: Windows hold the node they were opened on. That row goes stale the moment
  //: the node changes state, so panes are given the current one where we still
  //: have it, and fall back to the snapshot for a node that has been deleted.
  function liveNode(snapshot) {
    const live = (lab?.nodes || []).find((n) => n.id === snapshot?.id);
    if (live) return live;
    //: Not in the lab on screen — the lab was switched, or the node deleted.
    //: The snapshot still holds whatever state it had when the window opened,
    //: and believing it means a pane redialling the console of a guest that
    //: stopped hours ago, every few seconds, for as long as the tab is open.
    //: Not running is the only thing we actually know here.
    return snapshot ? { ...snapshot, state: "stopped" } : snapshot;
  }

  function focusWindow(id) {
    const w = windows.find((x) => x.id === id);
    if (w) w.z = ++zTop;
  }

  function closeWindow(id) {
    windows = windows.filter((w) => w.id !== id);
  }

  function setWinStatus(id, status) {
    const w = windows.find((x) => x.id === id);
    if (w) w.status = status;
  }

  //: Where the link popover sits, in screen coordinates. Selecting a link used
  //: to change the inspector on the far side of the window; the two things you
  //: actually want to do with a link now appear on the link.
  let linkPop = $state(null);

  function pickLink(e, link) {
    e.stopPropagation();
    selectedLink = link.id;
    selectedNet = null;
    selected = null;
    linkPop = { id: link.id, x: e.clientX, y: e.clientY };
  }

  //: Right-click menus. Every action here already existed in the inspector —
  //: the complaint was that reaching them meant selecting a thing and then
  //: crossing the window. Opening the menu performs the selection, so each
  //: item can call the same selectedNode/selectedLink function unchanged.
  let menu = $state(null);
  const closeMenu = () => (menu = null);

  //: The console you want is decided by how the node was spawned: a graphical
  //: QEMU guest is useless over serial, and a container has no VNC at all.
  function defaultConsole(node) {
    if (node.runtime === "qemu" || node.console?.vnc?.hostname) return "vnc";
    if (node.console?.rdp?.hostname) return "rdp";
    return "console";
  }

  function openDefaultConsole(node) {
    const kind = defaultConsole(node);
    //: A stopped node's terminal is worth opening: the pane says it is stopped
    //: and offers to start it, which is more use than an error banner telling
    //: you the same thing with nothing to press. A framebuffer is different —
    //: there is no display to attach to until the guest is drawing one.
    if (kind !== "console" && node.state !== "running") {
      error = `${node.name} is not running — start it to open its ${kind.toUpperCase()} display`;
      return;
    }
    if (kind === "console") openConsole(node);
    else openWindow(kind, node.id, node.name, { node });
  }

  function nodeMenu(e, node) {
    e.preventDefault();
    e.stopPropagation();
    // Right-clicking inside a selection acts on the selection; right-clicking
    // outside one is a fresh single-node gesture.
    if (!selectedIds.includes(node.id)) selectedIds = [node.id];
    selected = node.id;
    selectedLink = null;
    selectedNet = null;
    linkPop = null;
    if (selectedIds.length > 1) {
      const n = selectedIds.length;
      menu = {
        x: e.clientX,
        y: e.clientY,
        title: `${n} nodes`,
        items: [
          { label: `Start ${n} nodes`, glyph: "▶", run: () => startSelection(true) },
          { label: `Stop ${n} nodes`, glyph: "■", run: () => startSelection(false) },
          { sep: true },
          { label: "Select none", glyph: "○", run: () => (selectedIds = []) },
          { sep: true },
          {
            label: `Wipe ${n} disks`,
            glyph: "⌫",
            run: () => wipeNodes(selectedIds),
            danger: true,
          },
          {
            label: `Delete ${n} nodes`,
            glyph: "✕",
            run: deleteSelection,
            danger: true,
          },
        ],
      };
      return;
    }
    const running = node.state === "running";
    const hasVnc = node.runtime === "qemu" || !!node.console?.vnc?.hostname;
    menu = {
      x: e.clientX,
      y: e.clientY,
      title: node.name,
      items: [
        running
          ? { label: "Stop", glyph: "■", run: stopSelected }
          : { label: "Start", glyph: "▶", run: startSelected },
        node.paused
          ? { label: "Resume", glyph: "⏵", run: doResume }
          : {
              label: "Suspend",
              glyph: "⏸",
              run: doSuspend,
              disabled: !running,
              hint: running ? "" : "the node is not running",
            },
        { sep: true },
        { label: "Console", glyph: "▚", run: () => openConsole(node) },
        ...(hasVnc ? [{ label: "VNC", glyph: "🖥", run: openVnc }] : []),
        ...(node.console?.rdp?.hostname ? [{ label: "RDP", glyph: "🖳", run: openRdp }] : []),
        { sep: true },
        {
          label: "Capture",
          glyph: "◉",
          run: openCapture,
          disabled: !node.interfaces?.length,
          hint: node.interfaces?.length ? "" : "the node has no interface yet",
        },
        {
          label: "Wireshark",
          glyph: "🦈",
          run: openNodeWireshark,
          disabled: !node.interfaces?.length,
          hint: node.interfaces?.length ? "" : "the node has no interface yet",
        },
        { sep: true },
        { label: "Config", glyph: "≡", run: openConfig },
        { label: "Logs", glyph: "☰", run: openLogs },
        { label: "RAM / vCPU…", glyph: "⚙", run: editSize },
        ...(node.runtime === "qemu"
          ? [{ label: "Machine options…", glyph: "🖧", run: openQemuOptions }]
          : []),
        { label: "Add interface", glyph: "+", run: addPort },
        { sep: true },
        {
          label: "Wipe disk",
          glyph: "⌫",
          run: () => wipeNodes([node.id]),
          danger: true,
          hint: "back to a fresh image; keeps the node, its links and its place",
        },
        { label: "Delete node", glyph: "✕", run: removeSelected, danger: true },
      ],
    };
  }

  function linkMenu(e, link) {
    e.preventDefault();
    e.stopPropagation();
    selectedLink = link.id;
    selectedNet = null;
    selected = null;
    const up = link.admin_up !== false;
    menu = {
      x: e.clientX,
      y: e.clientY,
      title: linkTitle(link),
      items: [
        { label: "Capture", glyph: "◉", run: openCapture },
        { label: "Wireshark", glyph: "🦈", run: openNodeWireshark },
        { sep: true },
        up
          ? { label: "Admin down", glyph: "↓", run: () => setAdmin(false) }
          : { label: "Admin up", glyph: "↑", run: () => setAdmin(true) },
        { label: "Shape (tc)…", glyph: "⚙", run: editTc },
        // One exclusive choice, so say so. Listed flat they read as five
        // independent things you could switch on, which is what they are not.
        { header: activePreset === null ? "Profile — custom" : "Profile" },
        { label: "none", glyph: activePreset === "" ? "●" : "○", run: () => applyPreset(null) },
        ...realPresets.map((name) => ({
          label: name,
          glyph: activePreset === name ? "●" : "○",
          run: () => applyPreset(name),
        })),
        { sep: true },
        { label: "Delete link", glyph: "✕", run: deleteSelectedLink, danger: true },
      ],
    };
  }

  async function toggleDhcp(net) {
    busy = true;
    try {
      await api.patchNetwork(net.id, { dhcp: !net.dhcp_first });
      await loadLab(lab.id, true);
    } catch (e) {
      error = e.message;
    } finally {
      busy = false;
    }
  }

  async function toggleVlan(net) {
    busy = true;
    try {
      await api.patchNetwork(net.id, { vlan_aware: !net.vlan_aware });
      await loadLab(lab.id, true);
    } catch (e) {
      error = e.message;
    } finally {
      busy = false;
    }
  }

  //: Straight from dnsmasq's lease file. Shown rather than tracked separately
  //: because the DHCP server is the thing that knows, and a second record
  //: would drift from it.
  //: Leases, reservations and live translations in one window. They are three
  //: answers to the same question — "what address does this thing have, and is
  //: it talking" — and having them in three places is how you end up with two
  //: of them open and the third forgotten.
  async function showLeases(net) {
    try {
      const [{ leases }, sessions] = await Promise.all([
        api.netLeases(net.id),
        api.netSessions(net.id).catch(() => ({ sessions: [] })),
      ]);
      leaseView = {
        net,
        name: net.name,
        tab: "leases",
        leases: leases ?? [],
        sessions: sessions.sessions ?? [],
        note: sessions.note ?? "",
      };
    } catch (e) {
      error = e.message;
    }
  }

  async function refreshLeaseView() {
    if (leaseView?.net) await showLeases(leaseView.net);
  }

  //: Every port on this segment, so a reservation is picked from what is
  //: actually there rather than typed from memory.
  function portsOn(net) {
    return (lab?.nodes ?? []).flatMap((n) =>
      (n.interfaces ?? [])
        .filter((i) => i.network_id === net?.id)
        .map((i) => ({ node: n, iface: i })),
    );
  }

  async function saveReservation(iface, value) {
    try {
      await api.setPortReservation(iface.id, { reserved_ip: value || null });
      await loadLab(lab.id, true);
      await refreshLeaseView();
    } catch (e) {
      error = e.message;
    }
  }

  let leaseView = $state(null);
  let vlanForm = $state(null);

  async function commitVlan() {
    if (!vlanForm) return;
    const { iface, mode, vid, trunk } = vlanForm;
    vlanForm = null;
    busy = true;
    try {
      await api.setPortVlan(iface.id, {
        vlan_mode: mode,
        vlan_id: mode === "access" ? Number(vid) : null,
        trunk_vids:
          mode === "trunk"
            ? String(trunk).split(/[,\s]+/).filter(Boolean).map(Number)
            : [],
      });
      await loadLab(lab.id, true);
    } catch (e) {
      error = e.message;
    } finally {
      busy = false;
    }
  }

  function netMenu(e, net) {
    e.preventDefault();
    e.stopPropagation();
    menu = {
      x: e.clientX,
      y: e.clientY,
      title: net.name,
      items: [
        //: What this segment is, before what you can do to it. A NAT network
        //: with no gateway shown is indistinguishable from a plain bridge.
        {
          header:
            net.kind === "nat"
              ? `NAT · ${net.subnet ?? "?"} · gw ${net.gateway ?? "?"}`
              : net.kind === "cloud"
                ? `cloud · ${net.cloud_ref}`
                : "internal bridge",
        },
        ...(net.kind === "nat"
          ? [
              {
                label: net.dhcp_first ? "Stop handing out addresses" : "Hand out addresses",
                glyph: "⇩",
                hint: net.dhcp_first ? `${net.dhcp_first}–${net.dhcp_last}` : "DHCP off",
                run: () => toggleDhcp(net),
              },
              { label: "Show leases", glyph: "≡", run: () => showLeases(net) },
            ]
          : []),
        ...(net.kind === "bridge" || net.kind === "nat"
          ? [
              {
                label: net.vlan_aware ? "Turn off VLAN filtering" : "Turn on VLAN filtering",
                glyph: "⑃",
                hint: net.vlan_aware ? net.vlan_proto : "forwards tags without reading them",
                run: () => toggleVlan(net),
              },
            ]
          : []),
        { sep: true },
        { label: "Capture", glyph: "◉", run: () => openNetCapture(net) },
        {
          label: "Wireshark",
          glyph: "🦈",
          run: () =>
            openWireshark(
              { kind: "network", id: net.id, label: net.name },
              `${net.name} · wireshark`,
            ),
        },
        { sep: true },
        { label: "Delete segment", glyph: "✕", run: () => removeNetwork(net), danger: true },
      ],
    };
  }

  function canvasMenu(e) {
    if (!lab) return;
    e.preventDefault();
    menu = {
      x: e.clientX,
      y: e.clientY,
      title: lab.name,
      items: [
        { label: "Start all", glyph: "▶", run: startAll },
        { label: "Stop all", glyph: "■", run: stopAll },
      ],
    };
  }

  //: Names the two ends, since a link has no name of its own.
  function linkTitle(link) {
    const a = lab?.nodes?.find((n) => n.interfaces?.some((i) => i.id === link.a_iface_id));
    const b = lab?.nodes?.find((n) => n.interfaces?.some((i) => i.id === link.b_iface_id));
    return a && b ? `${a.name} ↔ ${b.name}` : "link";
  }

  //: Terminals live in the dock, one tab per node, because a lab is worked on
  //: by talking to several nodes at once — the float window made that a pile
  //: of overlapping windows you had to arrange by hand. The float window is
  //: still there, reached with the pop-out button, for a second screen.
  let termTabs = $state([]);
  let termA = $state(null);
  let termB = $state(null);
  let paneA = $state(null);
  let paneB = $state(null);
  let runOn = $state("a");
  let cmdLine = $state("");
  let history = $state({});
  let histIdx = $state(-1);

  const termNodes = $derived(
    termTabs.map((id) => (lab?.nodes ?? []).find((n) => n.id === id)).filter(Boolean),
  );
  const nodeA = $derived((lab?.nodes ?? []).find((n) => n.id === termA) ?? null);
  const nodeB = $derived((lab?.nodes ?? []).find((n) => n.id === termB) ?? null);
  const target = $derived(runOn === "b" && nodeB ? nodeB : nodeA);

  //: What each guest says its own addresses are, keyed by node then port.
  //: Deliberately behind a toggle: it is a round of exec calls into every
  //: running container, which is not something to do on a timer for a view
  //: nobody has asked for.
  let showAddressing = $state(false);
  let addresses = $state({});
  let addrTimer = null;

  async function refreshAddresses() {
    if (!lab || !showAddressing) return;
    try {
      addresses = await api.labAddresses(lab.id);
    } catch {
      addresses = {};
    }
  }

  function toggleAddressing() {
    showAddressing = !showAddressing;
    clearInterval(addrTimer);
    if (showAddressing) {
      refreshAddresses();
      //: Slow on purpose. Addresses change when someone configures them, not
      //: continuously, and each poll is an exec into every running container.
      addrTimer = setInterval(refreshAddresses, 15000);
    } else {
      addresses = {};
    }
  }

  function addrOf(node, iface) {
    const a = addresses[node.id]?.[iface.name];
    return a?.length ? a[0] : null;
  }

  //: The first address of whichever port faces the other end of this link —
  //: what you would actually ping to test it.
  function linkEnds(link) {
    const nodes = lab?.nodes ?? [];
    const side = (ifaceId) => {
      const node = nodes.find((n) => n.interfaces?.some((i) => i.id === ifaceId));
      if (!node) return null;
      const iface = node.interfaces.find((i) => i.id === ifaceId);
      return { node, iface, addr: addrOf(node, iface) };
    };
    return { a: side(link.a_iface_id), b: side(link.b_iface_id) };
  }

  function pingAcross(link) {
    const { a, b } = linkEnds(link);
    if (!a || !b) return;
    const from = a.node.state === "running" ? a : b;
    const to = from === a ? b : a;
    if (!to.addr) {
      error = `${to.node.name} has no address on ${to.iface.name} — turn Addressing on, or give it one`;
      return;
    }
    openConsole(from.node);
    //: The pane has to exist and have a socket before it can be typed into.
    setTimeout(() => runCommand(`ping -c 3 ${to.addr.split("/")[0]}`), 400);
  }

  function openConsole(node) {
    if (!termTabs.includes(node.id)) termTabs = [...termTabs, node.id];
    //: Opening a second terminal while split fills the empty half rather than
    //: replacing what you are already looking at.
    if (termB !== null && !nodeB) termB = node.id;
    else termA = node.id;
    switchDock("console");
  }

  function closeTerm(id) {
    termTabs = termTabs.filter((t) => t !== id);
    if (termA === id) termA = termTabs[0] ?? null;
    if (termB === id) termB = termTabs.find((t) => t !== termA) ?? null;
  }

  //: Whether a second pane is possible at all — two nodes to put in it. The
  //: button says so rather than being pressable and doing nothing.
  const canSplit = $derived((lab?.nodes ?? []).length >= 2);

  function toggleSplit() {
    if (termB !== null) {
      termB = null;
      runOn = "a";
      return;
    }
    const nodes = lab?.nodes ?? [];
    //: Splitting from a panel that is not the terminals is still a request for
    //: terminals; without this the button worked and appeared not to.
    switchDock("console");

    if (!termA) termA = termTabs[0] ?? nodes[0]?.id ?? null;
    if (termA && !termTabs.includes(termA)) termTabs = [...termTabs, termA];

    //: Prefer a terminal that is already open, then any other node in the lab.
    //: Not "any other *running* node": that was the bug. A stopped node in a
    //: pane is a useful thing — the pane says it is stopped and offers to
    //: start it — and filtering them out meant Split silently did nothing in
    //: a lab nobody had started yet, which is most labs when you open them.
    let second = termTabs.find((t) => t !== termA);
    if (!second) second = nodes.find((n) => n.id !== termA)?.id;
    if (!second) return;

    if (!termTabs.includes(second)) termTabs = [...termTabs, second];
    termB = second;
  }

  function popOutTerm() {
    if (nodeA) openWindow("console", nodeA.id, nodeA.name, { node: nodeA });
  }

  //: Four commands worth one click, built from what this node actually has:
  //: its own ports, and the neighbour it is wired to. Generic chips ("ls")
  //: would be decoration; these are the first things anyone types in a lab.
  function quickCommands(node) {
    if (!node) return [];
    const first = node.interfaces?.[0]?.name;
    const peer = node.interfaces?.map(peerOf).find(Boolean);
    const out = ["ip -br a", "ip route"];
    if (peer) out.push(`ping -c 3 ${peer.name}`);
    if (first) out.push(`tcpdump -ni ${first}`);
    return out;
  }

  function runCommand(text) {
    const line = (text ?? cmdLine).trim();
    if (!line || !target) return;
    const pane = runOn === "b" ? paneB : paneA;
    if (!pane?.send(line)) {
      error = `${target.name}'s console is not connected`;
      return;
    }
    const prev = history[target.id] ?? [];
    history = { ...history, [target.id]: [...prev.filter((c) => c !== line), line].slice(-50) };
    histIdx = -1;
    cmdLine = "";
  }

  function historyKey(e) {
    const list = history[target?.id] ?? [];
    if (e.key === "ArrowUp" && list.length) {
      e.preventDefault();
      histIdx = histIdx < 0 ? list.length - 1 : Math.max(0, histIdx - 1);
      cmdLine = list[histIdx];
    } else if (e.key === "ArrowDown" && histIdx >= 0) {
      e.preventDefault();
      histIdx += 1;
      cmdLine = histIdx >= list.length ? ((histIdx = -1), "") : list[histIdx];
    }
  }


  async function applyPreset(name) {
    if (!selectedLink) return;
    busy = true;
    try {
      // A preset is not a mode the link remembers — the server expands it into
      // per-direction netem parameters. "none" is therefore the same call with
      // every parameter cleared, not a separate concept.
      await api.patchLink(
        selectedLink,
        name ? { preset: name } : { impair_ab: {}, impair_ba: {} },
      );
      await loadLab(lab.id, true);
    } catch (e) {
      error = e.message;
    } finally {
      busy = false;
    }
  }

  //: Since presets are stored expanded, the active one is whichever preset's
  //: parameters the link currently carries — they are mutually exclusive by
  //: construction, and the control should say so instead of offering five
  //: buttons that look independently toggleable.
  function sameSpec(a, b) {
    const keys = new Set([...Object.keys(a || {}), ...Object.keys(b || {})]);
    for (const k of keys) {
      const x = (a || {})[k] ?? 0;
      const y = (b || {})[k] ?? 0;
      if (Number(x) !== Number(y)) return false;
    }
    return true;
  }

  //: Presets that impair nothing are the "none" position under another name.
  let realPresets = $derived(
    Object.entries(presets)
      .filter(([, spec]) => Object.values(spec || {}).some((v) => Number(v)))
      .map(([name]) => name),
  );

  let activePreset = $derived.by(() => {
    const link = lab?.links?.find((l) => l.id === selectedLink);
    if (!link) return null;
    const ab = link.impair_ab || {};
    if (!Object.values(ab).some((v) => Number(v))) return "";
    for (const [name, spec] of Object.entries(presets)) {
      if (sameSpec(ab, spec) && sameSpec(link.impair_ba || {}, spec)) return name;
    }
    return null; // custom parameters — no preset describes them
  });

  // Every knob netd's tc.set understands, per direction. Presets alone meant
  // the other five parameters were unreachable from the UI even though the
  // daemon has always applied them.
  const TC_FIELDS = [
    { key: "delay_ms", label: "delay", unit: "ms", step: 1 },
    { key: "jitter_ms", label: "jitter", unit: "ms", step: 1 },
    { key: "loss_pct", label: "loss", unit: "%", step: 0.1 },
    { key: "rate_kbit", label: "rate", unit: "kbit", step: 64 },
    { key: "duplicate_pct", label: "duplicate", unit: "%", step: 0.1 },
    { key: "reorder_pct", label: "reorder", unit: "%", step: 0.1 },
    { key: "corrupt_pct", label: "corrupt", unit: "%", step: 0.1 },
  ];

  function editTc() {
    if (!linkObj) return;
    const grab = (spec) =>
      Object.fromEntries(TC_FIELDS.map((f) => [f.key, spec?.[f.key] ?? ""]));
    tcEdit = {
      ab: grab(linkObj.impair_ab),
      ba: grab(linkObj.impair_ba),
      mirror: JSON.stringify(linkObj.impair_ab || {}) === JSON.stringify(linkObj.impair_ba || {}),
    };
  }

  function tcSpec(side) {
    // Blank means "not set", which is different from zero: netd clears the
    // qdisc entirely when the spec is empty.
    const out = {};
    for (const f of TC_FIELDS) {
      const v = tcEdit[side][f.key];
      if (v !== "" && v !== null && Number(v) > 0) out[f.key] = Number(v);
    }
    return out;
  }

  async function applyTc() {
    if (!selectedLink || !tcEdit) return;
    busy = true;
    const ab = tcSpec("ab");
    const ba = tcEdit.mirror ? ab : tcSpec("ba");
    tcEdit = null;
    try {
      await api.patchLink(selectedLink, { impair_ab: ab, impair_ba: ba });
      await loadLab(lab.id, true);
    } catch (e) {
      error = e.message;
    } finally {
      busy = false;
    }
  }

  function tcSummary(spec) {
    const parts = [];
    for (const f of TC_FIELDS) {
      const v = spec?.[f.key];
      if (v) parts.push(`${f.label} ${v}${f.unit}`);
    }
    return parts.length ? parts.join(" · ") : "clear";
  }

  async function setAdmin(up) {
    if (!selectedLink) return;
    try {
      await api.patchLink(selectedLink, { admin_up: up });
      await loadLab(lab.id, true);
    } catch (e) {
      error = e.message;
    }
  }

  async function deleteSelectedLink() {
    if (!selectedLink) return;
    try {
      await api.deleteLink(selectedLink);
      selectedLink = null;
      selectedNet = null;
    linkPop = null;
      await loadLab(lab.id, true);
    } catch (e) {
      error = e.message;
    }
  }

  async function startCapture() {
    packets = [];
    capturing = true;
    dock = "packets";
    try {
      if (selectedLink) await api.linkCaptureStart(selectedLink, bpf);
      else if (selectedNode?.interfaces[0]) await api.captureStart(selectedNode.interfaces[0].id, bpf);
      else throw new Error("select a running node or a link first");
    } catch (e) {
      capturing = false;
      error = e.message;
    }
  }

  async function stopCapture() {
    capturing = false;
    try {
      if (selectedLink) await api.linkCaptureStop(selectedLink);
      else if (selectedNode?.interfaces[0]) await api.captureStop(selectedNode.interfaces[0].id);
    } catch (e) {
      error = e.message;
    }
  }

  async function tickCapture() {
    if (!capturing) return;
    try {
      let got;
      if (selectedLink) got = await api.linkCaptureRead(selectedLink);
      else if (selectedNode?.interfaces[0]) got = await api.captureRead(selectedNode.interfaces[0].id);
      if (got?.lines?.length) packets = [...packets, ...got.lines].slice(-400);
    } catch {
      /* keep polling */
    }
  }

  //: History kept out of `chat`, which is what is drawn. The model needs the
  //: tool calls and their results; the person does not want to read them.
  let aiHistory = $state([]);
  let aiStep = $state("");
  //: How many tools the assistant has called on this turn so far. The
  //: working-strip in ChatPane uses this alongside aiStep to make it
  //: obvious the loop is progressing even between visible bubbles.
  let aiStepCount = $state(0);
  //: The turn the model is currently streaming — a growing bubble at the
  //: end of the chat that grows token-by-token as `text`/`reasoning`
  //: deltas arrive. Committed to `chat` when the turn finalizes; nulled
  //: at Send and on any terminator so a fresh prompt starts blank.
  let liveTurn = $state(null);
  //: The in-flight assistant WebSocket, so the Stop button has something
  //: to send `{kind:"cancel"}` on. Null between turns.
  let aiWs = $state(null);
  //: Pending destructive-tool confirmation. Non-null while a modal is
  //: on-screen asking the user to allow or deny; the user's click sends
  //: `{kind:"answer",id,allow}` on the WS. Only one is ever pending
  //: because the server waits for the answer before firing the tool.
  let aiConfirm = $state(null);
  //: Attachments the user has picked to include with the next Send.
  //: Each is `{filename, mime, size, data_url}` — base64 in-memory
  //: until the WS send point, then dropped. No server-side storage.
  let aiAttachments = $state([]);

  async function sendAi() {
    if (!lab) return;
    const msg = chatIn.trim();
    if (!msg && !aiAttachments.length) return;
    //: Show what the user sent, including a note about attachments —
    //: the images themselves are not rendered in the chat log to keep
    //: the transcript readable.
    const shown = msg + (aiAttachments.length
      ? (msg ? "\n" : "") + `(${aiAttachments.length} attachment${aiAttachments.length === 1 ? "" : "s"}: ${aiAttachments.map(a => a.filename).join(", ")})`
      : "");
    chat = [...chat, { role: "you", text: shown }];
    chatIn = "";
    busy = true;
    aiStep = "";
    aiStepCount = 0;
    liveTurn = null;
    //: The key lives in this browser and the call goes straight to the
    //: provider. If none is set, fall back to the server-side agent, which
    //: is how an instance with its own key configured still works.
    if (llmConfigured()) {
      try {
        const r = await runTurn({
          labId: lab.id,
          message: msg,
          history: aiHistory,
          onStep: ({ name }) => {
            aiStep = name;
            aiStepCount += 1;
          },
          //: One bubble per model turn instead of one at the end. The old
          //: shape hid every intermediate reasoning step and every tool
          //: outcome behind a single closing bubble; if the model went
          //: through 20 tool calls before quitting, none of that showed
          //: until it stopped — you had no idea what the plan was, what
          //: worked, or what failed. Now each turn's text + its own
          //: applied list appears as it happens, and refused calls carry
          //: the error message inline (not just "toolname refused").
          onTurn: ({ text, applied }) => {
            if (text || applied.length) {
              chat = [...chat, { role: "labtris", text, applied }];
            }
          },
          //: Refused tools also get their own explicit error bubble so a
          //: mid-loop failure is visually distinct from a normal step —
          //: otherwise a red-shaped "failed" event blends into the
          //: applied list under an otherwise-benign turn bubble.
          onError: ({ name, args, error }) => {
            const argStr = Object.entries(args).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", ");
            chat = [...chat, {
              role: "labtris",
              text: `${name}(${argStr}) refused:\n${error}`,
              error: true,
            }];
          },
        });
        aiHistory = r.messages.slice(1).slice(-24);
        //: If the loop hit the runaway backstop, r.text is the summary
        //: message from assistant.js — surface it as its own bubble.
        //: Otherwise the natural end (a turn with no tool calls) was
        //: already emitted by onTurn above, so don't duplicate it.
        if (r.truncated) {
          chat = [...chat, { role: "labtris", text: r.text, applied: [] }];
        }
        await loadLab(lab.id, true);
      } catch (e) {
        chat = [...chat, { role: "labtris", text: e.message, error: true }];
      } finally {
        busy = false;
        aiStep = "";
        aiStepCount = 0;
      }
      return;
    }
    //: Server-side agent path: streams turns over WebSocket so each bubble
    //: appears as the model produces it, plus a per-tool-call "step" ping
    //: that keeps the working-strip live between turns. The old batch POST
    //: at /api/v1/labs/{id}/ai is still there for MCP and tests.
    const proto = location.protocol === "https:" ? "wss" : "ws";
    aiWs = new WebSocket(`${proto}://${location.host}/api/v1/labs/${lab.id}/ai/ws`);
    const pendingAttachments = aiAttachments;
    aiAttachments = [];
    aiWs.onopen = () => aiWs.send(JSON.stringify({
      message: msg,
      attachments: pendingAttachments.map(({ filename, mime, data_url }) => ({ filename, mime, data_url })),
    }));
    aiWs.onmessage = (ev) => {
      let m;
      try {
        m = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (m.kind === "text") {
        //: Token delta — extend the live bubble. Create it on the first
        //: delta of a turn so the growing-cursor UI has something to
        //: anchor on.
        liveTurn = {
          ...(liveTurn || { text: "", reasoning: "" }),
          text: (liveTurn?.text || "") + (m.delta || ""),
        };
      } else if (m.kind === "reasoning") {
        liveTurn = {
          ...(liveTurn || { text: "", reasoning: "" }),
          reasoning: (liveTurn?.reasoning || "") + (m.delta || ""),
        };
      } else if (m.kind === "step") {
        aiStep = m.tool || "";
        aiStepCount += 1;
      } else if (m.kind === "confirm") {
        //: Destructive tool wants approval. Freeze the strip in an
        //: "awaiting your decision" state and pop the modal. The click
        //: handlers on the modal send `answer` back on the WS.
        aiConfirm = { id: m.id, tool: m.tool, args: m.args || {} };
      } else if (m.kind === "reset") {
        //: The stream broke mid-turn and the server is auto-retrying —
        //: discard the growing bubble so the retry does not append to
        //: half-generated text.
        liveTurn = null;
      } else if (m.kind === "turn") {
        //: The turn has finalized. Commit the live bubble (or the
        //: server-supplied fallback text) as a permanent one, reset the
        //: growing state so the next turn starts fresh.
        const text = m.text || liveTurn?.text || "";
        const reasoning = liveTurn?.reasoning || "";
        const applied = Array.isArray(m.applied) ? m.applied : [];
        if (text || applied.length || reasoning) {
          const entry = { role: "labtris", text, applied };
          if (reasoning) entry.reasoning = reasoning;
          chat = [...chat, entry];
        }
        liveTurn = null;
      } else if (m.kind === "cancelled") {
        //: User hit Stop. Discard the partial bubble and log it faintly
        //: so the user knows the turn was cut short, not lost silently.
        liveTurn = null;
        chat = [...chat, { role: "labtris", text: "(stopped)" }];
      } else if (m.kind === "error") {
        chat = [...chat, { role: "labtris", text: m.message || "error" }];
        liveTurn = null;
      } else if (m.kind === "done") {
        //: Server signalled clean end. The onclose that follows also
        //: clears busy — this is belt-and-braces so a slow close doesn't
        //: leave the input disabled.
        aiStep = "";
        aiStepCount = 0;
        busy = false;
        liveTurn = null;
        aiConfirm = null;
        loadLab(lab.id, true);
      }
    };
    aiWs.onerror = () => {
      chat = [...chat, { role: "labtris", text: "AI assistant connection lost" }];
    };
    aiWs.onclose = () => {
      aiStep = "";
      aiStepCount = 0;
      liveTurn = null;
      aiConfirm = null;
      busy = false;
      aiWs = null;
    };
  }

  function answerConfirm(allow) {
    if (!aiWs || !aiConfirm) return;
    try {
      aiWs.send(JSON.stringify({ kind: "answer", id: aiConfirm.id, allow }));
    } catch {}
    aiConfirm = null;
  }

  //: Read a file into a base64 data URL. Rejects anything over the size
  //: cap so a stray 200MB screenshot does not silently kill the WS with
  //: a message-size limit. The cap matches most vision models' input
  //: budget (they refuse or truncate above ~20MB anyway).
  const ATTACHMENT_MAX_BYTES = 20 * 1024 * 1024;
  async function addAttachments(fileList) {
    for (const file of Array.from(fileList || [])) {
      if (file.size > ATTACHMENT_MAX_BYTES) {
        chat = [...chat, { role: "labtris", text: `${file.name} is ${(file.size / 1024 / 1024).toFixed(1)}MB — the cap is 20MB.` }];
        continue;
      }
      const reader = new FileReader();
      const done = new Promise((resolve, reject) => {
        reader.onload = () => resolve(reader.result);
        reader.onerror = reject;
      });
      reader.readAsDataURL(file);
      const dataUrl = await done;
      aiAttachments = [...aiAttachments, {
        filename: file.name,
        mime: file.type || "application/octet-stream",
        size: file.size,
        data_url: dataUrl,
      }];
    }
  }

  function removeAttachment(idx) {
    aiAttachments = aiAttachments.filter((_, i) => i !== idx);
  }

  //: Manual stop. Sends a cancel to the server; the loop bails out at
  //: its next boundary and returns a `cancelled` event, which finalises
  //: the UI. Also handles the case where the WS has not yet opened.
  function stopAi() {
    if (!aiWs) return;
    try {
      if (aiWs.readyState === WebSocket.OPEN) {
        aiWs.send(JSON.stringify({ kind: "cancel" }));
      } else {
        aiWs.close();
      }
    } catch {
      // WebSocket already dead; onclose handler will clean up state.
    }
  }

  async function loadTuning() {
    try {
      tuning = await api.tuning();
    } catch (e) {
      error = e.message;
    }
  }

  //: Only the settings that actually differ. Offering "apply" on a host that
  //: already matches is a button that does nothing, every time you look at it.
  let tuningDrift = $derived(
    Object.entries(tuning?.wanted || {})
      .map(([k, want]) => [k, want, tuning?.values?.[k]])
      .filter(([, want, have]) => String(have ?? "") !== String(want)),
  );

  let diag = $state(null);
  let diagText = $derived(diag ? renderDiag(diag) : "");

  async function loadDiag() {
    try {
      diag = await api.diagnostics();
    } catch (e) {
      error = e.message;
    }
  }

  //: The same shape the CLI prints, so what someone pastes from the browser
  //: and what they paste from a terminal are the same document.
  function renderDiag(d) {
    const kvs = [
      ["labtris", d.labtris_version],
      ["platform", d.platform],
      ["cpu", `${d.cpu?.cores} x ${d.cpu?.model}`],
      ["memory", `${d.memory_mb?.MemTotal ?? "?"} MB total, ${d.memory_mb?.MemAvailable ?? "?"} MB free`],
      ["kvm", d.kvm?.note],
      ["qemu", d.qemu || "not found"],
      ["guacd", d.guacd],
      ...(d.disks || []).map((x) => [
        x.path?.split("/").slice(-2).join("/") || "disk",
        x.error || `${x.free_gb} GB free of ${x.total_gb} GB (${x.used_pct}% used)`,
      ]),
      ["image cache", `${d.image_cache_gb} GB`],
      ["vm overlays", `${d.vm_dir_gb} GB`],
      ["netd", d.netd?.reachable ? "reachable" : `DOWN — ${d.netd?.error}`],
      ["docker", d.docker?.reachable ? d.docker.version : `DOWN — ${d.docker?.error}`],
      ["database", d.database?.reachable ? `migration ${d.database.migration}` : `DOWN — ${d.database?.error}`],
      ["contents", `${d.database?.labs} labs, ${d.database?.nodes} nodes, ${d.database?.networks} networks`],
      ["running", `${d.database?.nodes_running} node(s), ${d.qemu_processes} qemu process(es)`],
    ];
    return kvs.map(([k, v]) => `${k.padEnd(14)} ${v}`).join("\n");
  }

  async function applyTuning() {
    busy = true;
    try {
      await api.applyTuning();
      await loadTuning();
    } catch (e) {
      error = e.message;
    } finally {
      busy = false;
    }
  }

  async function doExport() {
    if (!lab) return;
    const data = await api.exportLab(lab.id);
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${lab.name}.labtris.json`;
    a.click();
  }

  //: One list for "where is that node" and "where is that lab" — from a
  //: user's point of view both are "take me to the thing called X", and
  //: splitting them into two boxes made you decide which kind of thing you
  //: were looking for before you were allowed to look for it.
  function openPalette() {
    const items = [];
    if (lab?.nodes?.length) {
      items.push({ header: `in ${lab.name}` });
      for (const n of lab.nodes) {
        items.push({
          label: n.name,
          glyph: n.runtime === "qemu" ? "🖥" : "▪",
          hint: n.state === "running" ? "running" : n.state,
          run: () => jumpToNode(n),
        });
      }
    }
    const others = labs.filter((l) => l.id !== lab?.id);
    if (others.length) {
      items.push({ sep: true });
      items.push({ header: "labs" });
      for (const l of others) {
        const c = l.nodes ?? 0;
        items.push({
          label: l.name,
          glyph: "🗂",
          hint: l.folder || (c ? `${c} node${c === 1 ? "" : "s"}` : "empty"),
          run: () => loadLab(l.id),
        });
      }
    }
    items.push({ sep: true });
    items.push({
      label: "Addressing",
      glyph: "⊞",
      hint: "every port and its address",
      run: () => {
        if (!showAddressing) toggleAddressing();
        openWindow("addressing", "lab", "Lab addressing", { w: 720, h: 420 });
      },
    });
    if (items.length <= 2) items.unshift({ header: "nothing to jump to yet" });
    //: Anchored under the header rather than at the pointer: it is opened from
    //: the keyboard, and a menu at the last mouse position would appear
    //: wherever the pointer happened to be resting.
    headerMenu = { x: Math.max(12, window.innerWidth / 2 - 150), y: 64, items, filter: "Jump to node or lab…" };
  }

  function jumpToNode(node) {
    const p = pos(node.id, lab.nodes.indexOf(node));
    pan = { ...pan, x: 200 - p.x * pan.k, y: 160 - p.y * pan.k };
    selected = node.id;
    selectedLink = null;
    selectedNet = null;
    linkPop = null;
  }


  function linkImpaired(link) {
    const has = (s) => s && Object.values(s).some((v) => Number(v) > 0);
    return has(link.impair_ab) || has(link.impair_ba);
  }

  let catalogTimer = null;

  function trackImageDownloads() {
    // Only poll while something is actually being fetched — a 2 GB image takes
    // ~15 minutes, and sitting on "starting" with no feedback is why a node
    // that is working looks broken.
    clearTimeout(catalogTimer);
    if (!qemuImages.some((q) => q.phase)) return;
    catalogTimer = setTimeout(async () => {
      try {
        qemuImages = (await api.catalog()).qemu_images || [];
      } catch {
        /* transient */
      }
      trackImageDownloads();
    }, 3000);
  }

  //: One-shot POST /images/pull from the palette. Marks the image row as
  //: `phase: "starting"` locally so `trackImageDownloads()` kicks its poll
  //: loop off immediately; the next poll will overwrite with real
  //: {phase, done, total, percent} from the server. Errors bubble to the
  //: toast; the row stays in "starting" briefly before the poll clears it.
  async function pullImage(id) {
    const i = qemuImages.findIndex((q) => q.id === id);
    if (i < 0) return;
    // Optimistic local flip so the progress bar shows up in the same tick.
    qemuImages = qemuImages.map((q, k) =>
      k === i ? { ...q, phase: "starting", percent: 0 } : q,
    );
    trackImageDownloads();
    try {
      await api.imagePull(id);
    } catch (e) {
      qemuImages = qemuImages.map((q, k) =>
        k === i ? { ...q, phase: null, percent: null } : q,
      );
      note = `Could not start pull: ${e.message || e}`;
    }
  }

  function openUpload() {
    uploadForm = {
      file: null,
      name: "",
      ram_mb: 1024,
      cpus: 1,
      nic_model: "virtio-net-pci",
      disk_bus: "virtio",
      iface_scheme: "ens",
      graphical: false,
      description: "",
      submitting: false,
      error: "",
    };
    uploadPct = 0;
  }

  async function submitUpload() {
    if (!uploadForm.file || !uploadForm.name) {
      uploadForm.error = "pick a file and give it a name";
      return;
    }
    uploadForm.submitting = true;
    uploadForm.error = "";
    // XHR rather than fetch so we can render upload progress — fetch doesn't
    // expose it. Multi-GB uploads on a lab LAN are otherwise a silent 30-second
    // freeze.
    const fd = new FormData();
    fd.append("file", uploadForm.file);
    fd.append("name", uploadForm.name);
    fd.append("ram_mb", String(uploadForm.ram_mb));
    fd.append("cpus", String(uploadForm.cpus));
    fd.append("nic_model", uploadForm.nic_model);
    fd.append("disk_bus", uploadForm.disk_bus);
    fd.append("iface_scheme", uploadForm.iface_scheme);
    fd.append("graphical", String(uploadForm.graphical));
    if (uploadForm.description) fd.append("description", uploadForm.description);
    const done = new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/v1/images");
      xhr.withCredentials = true;
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) uploadPct = Math.round((100 * e.loaded) / e.total);
      };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) resolve(xhr.responseText);
        else reject(new Error(xhr.responseText || `HTTP ${xhr.status}`));
      };
      xhr.onerror = () => reject(new Error("network error"));
      xhr.send(fd);
    });
    try {
      await done;
      uploadForm = null;
      await loadTemplates();
      note = `Uploaded. It is in the palette under Your templates.`;
    } catch (e) {
      try {
        uploadForm.error = JSON.parse(e.message).error?.message || e.message;
      } catch {
        uploadForm.error = e.message;
      }
    } finally {
      if (uploadForm) uploadForm.submitting = false;
    }
  }

  function openEditTemplate(t) {
    // Prefill from the current template row (name/description at the top
    // level, everything else nested under spec). PATCH sends only the
    // fields that actually change, so pre-filling with the row's current
    // values is safe — a Save with no edits is a no-op.
    const s = t.spec || {};
    editTemplateForm = {
      id: t.id,
      name: t.name || "",
      description: t.description || "",
      ram_mb: s.ram_mb ?? 1024,
      cpus: s.cpus ?? 1,
      nic_model: s.nic_model || "virtio-net-pci",
      disk_bus: s.disk_bus || "virtio",
      iface_scheme: s.iface_scheme || "ens",
      graphical: !!s.graphical,
      // qemu_opts.cpu is what a guest with a modern glibc (PAN-OS, RHEL
      // 9+) needs: qemu64 is pre-2010 and lacks sse4.2, so glibc panics
      // at Fatal glibc error: CPU does not support x86-64-v2. "host"
      // needs KVM (present on any modern lab box) and passes everything
      // through; "max" is the software-only equivalent.
      cpu: (s.qemu_opts || {}).cpu || "qemu64",
      // Companion files. String path or null. Displayed as the
      // basename with an "unlink" X; an "upload…" button opens a
      // file picker and calls /images/companion.
      bios: s.bios || null,
      cdrom: s.cdrom || null,
      // Extra qemu args as a newline-joined string in the textarea;
      // split back to a list on Save. Only what an admin explicitly
      // types goes here — empty stays empty.
      qemu_extra_args_text: (s.qemu_extra_args || []).join("\n"),
      // Bootstrap block — {step_timeout_s, steps: [{wait_for, type}]}
      // typed into the guest over the serial console on first boot.
      // Full docs + example recipes: packaging/recipes/bootstrap/
      // Stored on the row as JSON; edited here as a text blob so the
      // regex quoting stays legible.
      bootstrap_text: s.bootstrap
        ? JSON.stringify(s.bootstrap, null, 2)
        : "",
      uploading: null,  // "bios" | "cdrom" | null while an upload is in flight
      submitting: false,
      error: "",
    };
  }

  async function uploadCompanion(kind, fileList) {
    if (!fileList?.length || !editTemplateForm) return;
    editTemplateForm.uploading = kind;
    editTemplateForm.error = "";
    try {
      const out = await api.uploadCompanion(kind, fileList[0]);
      editTemplateForm[kind] = out.path;
    } catch (e) {
      try {
        editTemplateForm.error = JSON.parse(e.message).error?.message || e.message;
      } catch {
        editTemplateForm.error = e.message;
      }
    } finally {
      if (editTemplateForm) editTemplateForm.uploading = null;
    }
  }

  async function submitEditTemplate() {
    if (!editTemplateForm.name) {
      editTemplateForm.error = "give it a name";
      return;
    }
    editTemplateForm.submitting = true;
    editTemplateForm.error = "";
    try {
      const extraArgs = editTemplateForm.qemu_extra_args_text
        .split("\n")
        .map((s) => s.trim())
        .filter(Boolean);
      // Bootstrap: empty string = no block (clear it). Non-empty must
      // parse as JSON. Refuse to save on a syntax error rather than
      // silently dropping the field.
      let bootstrap = null;
      const btext = (editTemplateForm.bootstrap_text || "").trim();
      if (btext) {
        try {
          bootstrap = JSON.parse(btext);
        } catch (e) {
          editTemplateForm.error = `Bootstrap JSON is not valid: ${e.message}`;
          editTemplateForm.submitting = false;
          return;
        }
      }
      await api.updateTemplate(editTemplateForm.id, {
        name: editTemplateForm.name,
        description: editTemplateForm.description || null,
        ram_mb: editTemplateForm.ram_mb,
        cpus: editTemplateForm.cpus,
        nic_model: editTemplateForm.nic_model,
        disk_bus: editTemplateForm.disk_bus,
        iface_scheme: editTemplateForm.iface_scheme,
        graphical: editTemplateForm.graphical,
        qemu_opts: { cpu: editTemplateForm.cpu },
        // Companion files. null means "clear the reference"; a path
        // means "point here". Delete-refcount on the server unlinks
        // the file when no template references it anymore.
        bios: editTemplateForm.bios,
        cdrom: editTemplateForm.cdrom,
        qemu_extra_args: extraArgs,
        // null clears the block entirely; an object sets it.
        bootstrap: bootstrap,
      });
      editTemplateForm = null;
      await loadTemplates();
      note = "Template updated.";
    } catch (e) {
      try {
        editTemplateForm.error = JSON.parse(e.message).error?.message || e.message;
      } catch {
        editTemplateForm.error = e.message;
      }
    } finally {
      if (editTemplateForm) editTemplateForm.submitting = false;
    }
  }

  // The assistant lives in a FloatWindow rather than a dock tab. Click
  // once to open, again to raise (openWindow's dedup logic handles it);
  // the × on the window closes it. State (chat, chatIn) lives at App
  // level so a close-and-reopen preserves the conversation.
  function toggleChat() {
    openWindow("chat", "instance", "✦ AI assistant", { w: 420, h: 700 });
  }

  // Host meter click: open Settings directly on the Host & diagnostics
  // pane. `section` is a per-window field the SettingsPane render reads.
  function openHostDiag() {
    openWindow("settings", "instance", "Settings & backup", {
      w: 620,
      h: 620,
      section: "host",
    });
  }

  async function pollHostMeter() {
    try {
      const d = await api.diagnostics();
      const total = d?.memory_mb?.MemTotal;
      const avail = d?.memory_mb?.MemAvailable;
      const load1 = d?.loadavg?.load1;
      const cores = d?.cpu?.cores || 1;
      hostMeter = {
        load_pct: load1 !== undefined ? Math.round((load1 / cores) * 100) : hostMeter.load_pct,
        mem_used_gb:
          total !== undefined && avail !== undefined
            ? ((total - avail) / 1024).toFixed(1)
            : hostMeter.mem_used_gb,
        mem_total_gb: total !== undefined ? (total / 1024).toFixed(1) : hostMeter.mem_total_gb,
      };
    } catch {
      // Leave the last known values on screen; a briefly-slow API should
      // not flash "…" across the top bar every 3 seconds.
    }
  }

  // Visibility-aware polling: 3s when the tab is focused, back off to
  // 15s when hidden. `document.visibilityState` change fires the effect
  // via a listener that toggles the interval.
  $effect(() => {
    let interval = 3000;
    const arm = () => {
      if (hostMeterTimer) clearInterval(hostMeterTimer);
      interval = document.visibilityState === "visible" ? 3000 : 15000;
      hostMeterTimer = setInterval(pollHostMeter, interval);
    };
    const onVis = () => arm();
    document.addEventListener("visibilitychange", onVis);
    pollHostMeter();
    arm();
    return () => {
      document.removeEventListener("visibilitychange", onVis);
      if (hostMeterTimer) clearInterval(hostMeterTimer);
      hostMeterTimer = null;
    };
  });

  async function loadTemplates() {
    try {
      templates = await api.templates();
    } catch {
      templates = [];
    }
    try {
      const cat = await api.catalog();
      qemuImages = cat.qemu_images || [];
      nicModels = cat.nic_models || [];
      ifaceSchemes = cat.iface_schemes || [];
      trackImageDownloads();
    } catch {
      qemuImages = [];
    }
  }

  async function dropQemu(img, clientX, clientY, canvasEl) {
    const rect = canvasEl.getBoundingClientRect();
    const x = snap((clientX - rect.left - pan.x) / pan.k);
    const y = snap((clientY - rect.top - pan.y) / pan.k);
    await spawnNode(img.image, null, x, y, img.id, "qemu");
  }

  async function toggleLock() {
    if (!lab) return;
    try {
      if (lab.locked) await api.unlockLab(lab.id);
      else await api.lockLab(lab.id);
      await loadLab(lab.id, true);
      await refreshLabs();
    } catch (e) {
      error = e.message;
    }
  }


  //: What is being named right now, or null. Replaces two permanent text
  //: boxes that four different buttons silently read from.
  let edit = $state(null);
  let headerMenu = $state(null);

  function openHeaderMenu(e, items, filter = "") {
    const r = e.currentTarget.getBoundingClientRect();
    headerMenu = { x: r.left, y: r.bottom + 4, items, filter };
  }

  //: Every lab on the instance, grouped the way the folders are, each one
  //: labelled with what is actually in it. "demo" and "demo" tell you nothing;
  //: "demo · 3 running" and "demo · empty" tell you which one you meant.
  function labSwitcherMenu() {
    const items = [];
    for (const [folder, group] of labsByFolder) {
      items.push({ header: folder || "Labs" });
      for (const l of group) {
        const n = l.nodes ?? 0;
        items.push({
          label: l.name,
          glyph: l.id === lab?.id ? "•" : "",
          hint: l.running ? `${l.running} running` : n ? `${n} node${n === 1 ? "" : "s"}` : "empty",
          run: () => loadLab(l.id),
        });
      }
    }
    if (!items.length) items.push({ header: "no labs yet" });
    items.push({ sep: true });
    items.push({
      label: lab?.folder ? `New lab in ${lab.folder}…` : "New lab…",
      glyph: "＋",
      run: () => startEdit("create"),
    });
    return items;
  }

  function startEdit(mode) {
    const modes = {
      rename: { prompt: "Rename to", value: lab?.name ?? "", placeholder: "lab name" },
      move: { prompt: "Move to folder", value: lab?.folder ?? "", placeholder: "CCNA/Week 1 — empty for the root" },
      clone: { prompt: "Duplicate as", value: `${lab?.name ?? "lab"} copy`, placeholder: "name for the copy" },
      create: { prompt: "New lab", value: "", placeholder: "lab name" },
    };
    edit = { mode, ...modes[mode] };
  }

  async function commitEdit() {
    if (!edit) return;
    const { mode, value } = edit;
    const name = value.trim();
    edit = null;
    try {
      if (mode === "rename" && name) {
        await api.renameLab(lab.id, { name });
        await refreshLabs();
        await loadLab(lab.id, true);
      } else if (mode === "move") {
        //: "" is a real destination — the root — so it is sent, not skipped.
        await api.renameLab(lab.id, { folder: value.trim() });
        await refreshLabs();
        await loadLab(lab.id, true);
      } else if (mode === "clone") {
        const copy = await api.cloneLab(lab.id, name ? { name } : {});
        await refreshLabs();
        await loadLab(copy.id);
      } else if (mode === "create") {
        creating = true;
        const created = await api.createLab(name || "lab", "", lab?.folder ?? "");
        await refreshLabs();
        await loadLab(created.id);
      }
    } catch (e) {
      error = e.message;
    } finally {
      creating = false;
    }
  }

  async function deleteCurrentLab() {
    if (!lab) return;
    //: Typed confirmation, not an OK button: this destroys every node,
    //: interface and saved config in the lab, and the name is the one thing
    //: that proves you meant this lab and not the one you had open before.
    const typed = window.prompt(
      `Delete "${lab.name}" and everything in it? This cannot be undone.\n\nType the lab name to confirm:`,
    );
    if (typed?.trim() !== lab.name) return;
    try {
      await api.deleteLab(lab.id);
      lab = null;
      selected = null;
      selectedIds = [];
      await refreshLabs();
    } catch (e) {
      error = e.message;
    }
  }

  //: Everything the header used to show as its own button. These are not
  //: frequent enough to earn permanent space, and having them there is what
  //: made the header a control panel — but they are all still one click away
  //: and named, rather than hidden behind an icon you have to learn.
  function overflowMenu() {
    const items = lab ? labMenu() : [{ header: "no lab open" }];
    return [
      ...items,
      { sep: true },
      { header: "instance" },
      {
        label: "Settings & backup",
        glyph: "⚙",
        run: () => openWindow("settings", "instance", "Settings & backup", { w: 620, h: 620 }),
      },
      {
        label: "Report a problem",
        glyph: "⚑",
        hint: reports.length ? `${reports.length} open` : "",
        run: startAnnotate,
      },
    ];
  }

  function userMenu() {
    //: Theme groups used to live here; they moved to a top-bar <select>
    //: because a preference seen every second belongs somewhere visible.
    //: Sign-out is also a top-bar button now — this menu has no remaining
    //: callers, but the function is kept in case something else lands here.
    return [
      { header: currentUser?.username ?? "signed in" },
      { sep: true },
      { label: "Sign out", glyph: "⇥", run: signOut },
    ];
  }

  function newMenu() {
    return [
      { label: "Blank lab", glyph: "＋", run: () => startEdit("create") },
      { sep: true },
      { label: "Import lab file…", glyph: "⇩", run: triggerImport },
    ];
  }

  function labMenu() {
    const locked = !!lab?.locked;
    return [
      { header: lab?.name ?? "lab" },
      { label: "Rename…", glyph: "✎", hint: locked ? "Locked" : "F2", disabled: locked, run: () => startEdit("rename") },
      { label: "Move to folder…", glyph: "🗂", hint: locked ? "Locked" : "", disabled: locked, run: () => startEdit("move") },
      { label: "Duplicate topology", glyph: "⧉", hint: "topology only", run: () => startEdit("clone") },
      { sep: true },
      { label: "Export lab file", glyph: "⇧", run: doExport },
      { label: "Pre-pull images", glyph: "⇩", hint: "fetch now", run: () => runBulkTask("pull_images") },
      { sep: true },
      {
        label: locked ? "Unlock topology" : "Lock topology",
        glyph: locked ? "🔓" : "🔒",
        hint: locked ? "" : "blocks edits",
        run: toggleLock,
      },
      { sep: true },
      {
        label: "Delete lab…",
        glyph: "🗑",
        danger: true,
        hint: locked ? "Locked" : "",
        disabled: locked,
        run: deleteCurrentLab,
      },
    ];
  }



  async function loadConfigSets() {
    if (!lab) return;
    try {
      configSets = await api.configsets(lab.id);
    } catch {
      configSets = { summary: [], active: null };
    }
  }

  async function captureSet() {
    const name = newSetName.trim();
    if (!lab || !name) return;
    try {
      await api.captureConfigset(lab.id, name);
      newSetName = "";
      await loadConfigSets();
      await loadLab(lab.id, true);
    } catch (e) {
      error = e.message;
    }
  }

  async function applySet(name) {
    if (!lab) return;
    try {
      const r = await api.applyConfigset(lab.id, name);
      //: A set that silently configures three of four nodes is how a lab comes
      //: up half-right, so a short count is surfaced rather than swallowed.
      if (r.missing?.length) {
        error = `${name}: ${r.missing.length} node(s) in this set no longer exist`;
      }
      await loadConfigSets();
      await loadLab(lab.id, true);
      if (selected) await openConfig();
    } catch (e) {
      error = e.message;
    }
  }

  async function deleteSet(name) {
    if (!lab) return;
    try {
      await api.deleteConfigset(lab.id, name);
      await loadConfigSets();
      await loadLab(lab.id, true);
    } catch (e) {
      error = e.message;
    }
  }

  function triggerImport() {
    fileInput?.click();
  }

  async function onImportFile(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    const text = await file.text();
    busy = true;
    try {
      let created;
      if (text.trim().startsWith("{")) {
        // our own export
        created = await api.importLab(JSON.parse(text));
      } else {
        // containerlab .clab.yml or EVE-NG .unl, detected from the content
        const name = file.name.replace(/\.(clab\.)?(unl|ya?ml)$/i, "");
        const out = await api.importTopology(text, file.name, name);
        created = out.lab;
        importReport = {
          name: created.name,
          source: out.source,
          ...out.imported,
          warnings: out.warnings || [],
        };
      }
      await refreshLabs();
      await loadLab(created.id);
    } catch (err) {
      error = err.message;
    } finally {
      busy = false;
      e.target.value = "";
    }
  }

  async function setStyle(icon, color) {
    if (!selected) return;
    try {
      await api.nodeStyle(selected, { icon, color });
      await loadLab(lab.id, true);
    } catch (e) {
      error = e.message;
    }
  }

  async function doSuspend() {
    if (!selected) return;
    try {
      await api.suspend(selected);
      await loadLab(lab.id, true);
    } catch (e) {
      error = e.message;
    }
  }

  async function doResume() {
    if (!selected) return;
    try {
      await api.resume(selected);
      await loadLab(lab.id, true);
    } catch (e) {
      error = e.message;
    }
  }

  async function doExportTemplate() {
    if (!selectedNode) return;
    const isVm = selectedNode.runtime === "qemu";
    // Saving a VM copies its whole disk. Saying so before the prompt is the
    // difference between waiting and assuming it has hung.
    if (isVm && selectedNode.state === "running") {
      error = `Stop ${selectedNode.name} first — a disk being written by a running guest cannot be copied consistently.`;
      return;
    }
    const name = prompt("Template name", `${selectedNode.name}-tmpl`);
    if (!name) return;
    busy = true;
    if (isVm) note = `Copying ${selectedNode.name}'s disk — this can take a few minutes.`;
    try {
      await api.exportTemplate(selectedNode.id, name);
      await loadTemplates();
      note = `Saved "${name}". It is in the palette under Your templates.`;
    } catch (e) {
      error = e.message;
      note = "";
    } finally {
      busy = false;
    }
  }

  async function openConfig() {
    if (!selected) return;
    dock = "config";
    try {
      const r = await api.readConfig(selected);
      configText = r.content || "";
    } catch (e) {
      error = e.message;
    }
  }

  async function saveConfig() {
    if (!selected) return;
    try {
      await api.saveConfig(selected, configText);
    } catch (e) {
      error = e.message;
    }
  }

  async function pushConfig() {
    if (!selected) return;
    busy = true;
    try {
      await saveConfig();
      await api.pushConfig(selected);
    } catch (e) {
      error = e.message;
    } finally {
      busy = false;
    }
  }

  async function openLogs() {
    if (!selected) return;
    dock = "logs";
    await refreshLogs();
  }

  async function refreshLogs() {
    if (!selected) return;
    try {
      const r = await api.nodeLogs(selected, 300, logsPattern);
      logsText = r.lines || [];
    } catch (e) {
      error = e.message;
    }
  }

  async function runBulkTask(kind) {
    if (!lab) return;
    busy = true;
    taskProgress = { kind, progress: 0, total: 0, status: "pending" };
    try {
      const task = await api.createTask(lab.id, kind);
      for (let i = 0; i < 100; i++) {
        const got = await api.task(task.id);
        taskProgress = got;
        if (got.status === "done" || got.status === "failed") break;
        await new Promise((r) => setTimeout(r, 400));
      }
      await loadLab(lab.id, true);
    } catch (e) {
      error = e.message;
    } finally {
      busy = false;
      setTimeout(() => (taskProgress = null), 2500);
    }
  }

  async function loadSnapshots() {
    if (!selected) return;
    try {
      const r = await api.listSnapshots(selected);
      snapshots = r.snapshots || [];
    } catch {
      snapshots = [];
    }
  }

  async function doSaveSnapshot() {
    if (!selected || !snapshotName.trim()) return;
    busy = true;
    try {
      await api.saveSnapshot(selected, snapshotName.trim());
      await loadSnapshots();
    } catch (e) {
      error = e.message;
    } finally {
      busy = false;
    }
  }

  async function doRestoreSnapshot(name) {
    if (!selected) return;
    busy = true;
    try {
      await api.restoreSnapshot(selected, name);
      await loadLab(lab.id, true);
    } catch (e) {
      error = e.message;
    } finally {
      busy = false;
    }
  }

  async function loadHosts() {
    try {
      hosts = await api.hosts();
    } catch (e) {
      error = e.message;
    }
  }

  async function registerHost() {
    if (!hostForm.name.trim() || !hostForm.endpoint.trim()) return;
    busy = true;
    try {
      await api.registerHost({
        name: hostForm.name.trim(),
        endpoint: hostForm.endpoint.trim(),
        token: hostForm.token.trim() || undefined,
        underlay_ip: hostForm.underlay_ip.trim() || undefined,
      });
      hostForm = { name: "", endpoint: "", token: "", underlay_ip: "" };
      await loadHosts();
      await loadReports();
      try {
        aiStatus = await api.aiStatus();
      } catch {
        aiStatus = null;
      }
    } catch (e) {
      error = e.message;
    } finally {
      busy = false;
    }
  }

  async function setHostUnderlay(host, ip) {
    try {
      await api.patchHost(host.id, { underlay_ip: ip });
      await loadHosts();
    } catch (e) {
      error = e.message;
    }
  }

  async function removeHost(host) {
    try {
      await api.deleteHost(host.id);
      await loadHosts();
    } catch (e) {
      error = e.message;
    }
  }

  function closeVnc() {
    if (vncMouse) {
      vncMouse.onmousedown = vncMouse.onmouseup = vncMouse.onmousemove = null;
      vncMouse = null;
    }
    if (vncKeyboard) {
      vncKeyboard.onkeydown = null;
      vncKeyboard.onkeyup = null;
      vncKeyboard = null;
    }
    if (vncClient) {
      try {
        vncClient.disconnect();
      } catch {
        /* already gone */
      }
      vncClient = null;
    }
    vncStatus = "idle";
  }

  function switchDock(name) {
    if (dock === "vnc" && name !== "vnc") closeVnc();
    dock = name;
    if (name === "events") eventsSeen = eventLog.length;
    if (name === "config") loadConfigSets();
  }

  // Clicking a tab in the strip: if the dock is minimised, expand and
  // switch to that tab (otherwise the click lands on nothing visible and
  // looks broken). If the tab is ALREADY the active one AND the dock is
  // already expanded, minimise — so a tab acts as a real toggle. If the
  // tab is a different one, just switch.
  function switchDockAndExpand(name) {
    if (dockMin) {
      dockMin = false;
      switchDock(name);
    } else if (dock === name) {
      dockMin = true;
    } else {
      switchDock(name);
    }
  }

  function toggleDockMin() {
    dockMin = !dockMin;
  }

  // Auto-switch the dock to the Inspector tab when a node or link is
  // selected — the details of what you just clicked belong on screen. We
  // do NOT auto-expand a minimised dock: a click-to-select shouldn't
  // force UI visible, and the tab strip already shows the label change.
  //
  // The `dock` read is inside untrack() so this effect only re-runs when
  // the selection changes — not when the user switches tabs. Without
  // that, clicking Logs while a node is selected snapped the dock right
  // back to Inspector.
  $effect(() => {
    const _dep = selected || selectedLink;  // depend on selection only
    if (_dep) {
      untrack(() => {
        if (dock !== "inspector" && dock !== "console") dock = "inspector";
      });
    }
  });

  const GUAC_STATES = [
    "idle",
    "connecting",
    "waiting",
    "connected",
    "disconnecting",
    "disconnected",
  ];

  async function openGuac(protocol) {
    if (!selectedNode) return;
    vncProtocol = protocol;
    dock = "vnc";
    vncStatus = "connecting";
    await tick();
    // Close-and-flush: closeVnc() calls vncClient.disconnect() which
    // triggers a WebSocket close frame, but the frame is queued — a
    // fresh WebSocketTunnel opened one line later can race the close
    // and both sockets end up open at the same time. QEMU's -vnc used
    // to be single-client and would refuse the second, surfacing to
    // the browser as "tunnel 519". force-shared on the QEMU side fixes
    // the root cause; this small delay makes the same-tab reconnect
    // clean either way.
    closeVnc();
    await new Promise((r) => setTimeout(r, 50));
    vncStatus = "connecting";
    if (!vncContainerEl) return;

    // Hand guacd the real viewport so the guest gets a sensible desktop size
    // instead of guacd's 1024x768 fallback.
    const rect = vncContainerEl.getBoundingClientRect();
    const q = new URLSearchParams({
      width: String(Math.max(320, Math.round(rect.width || 1024))),
      height: String(Math.max(240, Math.round(rect.height || 768))),
      dpi: String(Math.round(96 * (window.devicePixelRatio || 1))),
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "",
    });
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${location.host}/api/v1/nodes/${selectedNode.id}/${protocol}/ws?${q}`;

    const tunnel = new Guacamole.WebSocketTunnel(url);
    // Guacamole.Client never wires tunnel.onerror itself, so a tunnel that
    // dies during the handshake leaves the client sitting in "waiting" with
    // nothing on screen and nothing in the console. Wire it up ourselves.
    tunnel.onerror = (status) =>
      (vncStatus = `tunnel error: ${status?.message || status?.code || "unknown"}`);
    tunnel.onstatechange = (state) => {
      if (state === Guacamole.Tunnel.State.CLOSED && !/error/.test(vncStatus)) {
        vncStatus = "tunnel closed";
      }
    };

    const client = new Guacamole.Client(tunnel);
    vncClient = client;
    client.onstatechange = (s) => (vncStatus = GUAC_STATES[s] || String(s));
    client.onerror = (e) => (vncStatus = `error: ${e?.message || "unknown"}`);

    const display = client.getDisplay();
    vncContainerEl.innerHTML = "";
    vncContainerEl.appendChild(display.getElement());
    client.connect("");

    const mouse = new Guacamole.Mouse(display.getElement());
    // The second argument is applyDisplayScale. Guacamole.Mouse reports
    // positions as `clientX - element.offsetLeft`, which is layout
    // arithmetic — and Display.scale() scales by CSS transform, which
    // layout offsets do not see. Sending the raw state put the guest
    // pointer at browser coordinates on a framebuffer drawn at a
    // different size, so it tracked the real cursor with an error that
    // grew the further you moved from the top-left corner.
    const sendMouse = (e) => client.sendMouseState(e.state ?? e, true);
    mouse.onmousedown = sendMouse;
    mouse.onmouseup = sendMouse;
    mouse.onmousemove = sendMouse;
    vncMouse = mouse;

    // Keyboard events only reach the div if it can hold focus (tabindex) and
    // actually has it.
    const keyboard = new Guacamole.Keyboard(vncContainerEl);
    keyboard.onkeydown = (keysym) => client.sendKeyEvent(1, keysym);
    keyboard.onkeyup = (keysym) => client.sendKeyEvent(0, keysym);
    vncKeyboard = keyboard;
    vncContainerEl.focus();
  }

  const openVnc = () => selectedNode && openWindow("vnc", selectedNode.id, selectedNode.name, { node: selectedNode });
  const openRdp = () => selectedNode && openWindow("rdp", selectedNode.id, selectedNode.name, { node: selectedNode });

  function openWireshark(target, title) {
    openWindow("wireshark", target.id, title, {
      target,
      w: 1180,
      h: 760,
    });
  }

  function openNetCapture(net) {
    openWindow("capture", `net-${net.id}`, `${net.name} · segment`, {
      target: { kind: "network", id: net.id, label: net.name },
    });
  }

  function openNodeWireshark() {
    if (selectedLink) {
      openWireshark({ kind: "link", id: selectedLink, label: "link" }, "link · wireshark");
    } else if (selectedNode?.interfaces?.length) {
      const iface = selectedNode.interfaces[0];
      openWireshark(
        { kind: "iface", id: iface.id, label: iface.name },
        `${selectedNode.name} · ${iface.name} · wireshark`,
      );
    } else {
      error = "select a running node with an interface, or a link";
    }
  }

  function openCapture() {
    if (selectedLink) {
      openWindow("capture", `link-${selectedLink}`, "link capture", {
        target: { kind: "link", id: selectedLink, label: "link" },
      });
    } else if (selectedNode?.interfaces?.length) {
      const iface = selectedNode.interfaces[0];
      openWindow("capture", iface.id, `${selectedNode.name} · ${iface.name}`, {
        target: { kind: "iface", id: iface.id, label: `${selectedNode.name}-${iface.name}` },
      });
    } else {
      error = "select a node with an interface, or a link, to capture";
    }
  }

  function editSize() {
    if (!selectedNode) return;
    sizeForm = {
      ram_mb: selectedNode.ram_mb ?? "",
      cpu_limit: selectedNode.cpu_limit ?? "",
      nic_model: selectedNode.nic_model ?? "",
    };
  }

  async function saveSize() {
    if (!selectedNode || !sizeForm) return;
    busy = true;
    const body = {};
    if (sizeForm.ram_mb !== "") body.ram_mb = Number(sizeForm.ram_mb);
    if (sizeForm.cpu_limit !== "") body.cpu_limit = Number(sizeForm.cpu_limit);
    if (sizeForm.nic_model) body.nic_model = sizeForm.nic_model;
    sizeForm = null;
    try {
      await api.nodeResources(selectedNode.id, body);
      await loadLab(lab.id, true);
    } catch (e) {
      error = e.message;
    } finally {
      busy = false;
    }
  }

  async function saveConsoleTarget() {
    if (!selectedNode) return;
    try {
      await api.nodeConsole(selectedNode.id, {
        protocol: "rdp",
        hostname: consoleForm.hostname,
        port: consoleForm.port ? Number(consoleForm.port) : null,
        username: consoleForm.username,
        password: consoleForm.password,
      });
      await loadLab(lab.id, true);
    } catch (e) {
      error = e.message;
    }
  }

  async function loadHostCaps(host) {
    try {
      hostCaps = { ...hostCaps, [host.id]: await api.hostCapabilities(host.id) };
    } catch (e) {
      hostCaps = { ...hostCaps, [host.id]: { error: e.message } };
    }
  }

  async function dropTemplate(tmpl, clientX, clientY, canvasEl) {
    const rect = canvasEl.getBoundingClientRect();
    const x = snap((clientX - rect.left - pan.x) / pan.k);
    const y = snap((clientY - rect.top - pan.y) / pan.k);
    // The template's own runtime, not the spawnNode default. Without this a
    // saved QEMU appliance was placed as a Docker node pointing at a disk
    // image, which fails at start with an error about a missing registry.
    await spawnNode(tmpl.image, tmpl.cmd, x, y, tmpl.label, tmpl.runtime || "docker");
  }

  onMount(async () => {
    await checkAuth();
    if (!currentUser) return;
    await boot();
  });

  async function boot() {
    try {
      health = await api.health();
    } catch {
      health = { status: "down", db: false, netd: false, docker: false };
    }
    try {
      const p = await api.presets();
      presets = p.presets || {};
    } catch {
      presets = {};
    }
    // Labs first. Everything below is decoration for panels that may not even
    // be open, and one of them — the multi-host list — probes remote netds,
    // so a single unreachable satellite used to leave the app with no labs
    // for as long as that took to time out.
    try {
      await refreshLabs();
      if (labs[0]) await loadLab(labs[0].id);
    } catch (e) {
      error = e.message;
    }
    await loadTuning();
    await loadTemplates();
    // Not awaited: the Hosts tab reloads this when it is opened anyway.
    loadHosts();
    poller = setInterval(tickCapture, 800);
    // The canvas element is not the whole window: dragging a node up into the
    // toolbar, or releasing over the inspector, used to strand the gesture
    // because move/up never reached it. Tracking on window fixes that, and
    // pointer events cover trackpads, pens and touch as well as a mouse.
    window.addEventListener("pointermove", onCanvasMove);
    window.addEventListener("pointerup", onCanvasUp);
    window.addEventListener("pointercancel", onCanvasUp);
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
    // A held space with the window unfocused would otherwise stay "held".
    window.addEventListener("blur", () => (spaceHeld = false));
  }

  onDestroy(() => {
    window.removeEventListener("pointermove", onCanvasMove);
    window.removeEventListener("pointerup", onCanvasUp);
    window.removeEventListener("pointercancel", onCanvasUp);
    window.removeEventListener("keydown", onKeyDown);
    window.removeEventListener("keyup", onKeyUp);
    clearInterval(poller);
    ws?.close();
    labWs?.close();
    closeVnc();
  });

  const selectedNode = $derived(lab?.nodes?.find((n) => n.id === selected));

  //: The catalog row for a node's image, where one exists. QEMU images are a
  //: known set with known credentials; a Docker image is whatever string was
  //: typed, so there is nothing to look up and nothing is claimed.
  const selectedImage = $derived(
    selectedNode ? (qemuImages.find((q) => q.id === selectedNode.image) ?? null) : null,
  );

  //: Every distinct image the lab needs. Only the QEMU catalog tracks whether
  //: a disk is already fetched — whether a Docker tag is present is the
  //: engine's business, so those rows say nothing rather than guess.
  const labImages = $derived.by(() => {
    const seen = new Map();
    for (const n of lab?.nodes ?? []) {
      if (seen.has(n.image)) continue;
      const q = qemuImages.find((x) => x.id === n.image);
      seen.set(n.image, {
        image: n.image,
        status: q ? (q.cached ? "on disk" : "not downloaded") : "",
        missing: !!q && !q.cached,
      });
    }
    return [...seen.values()];
  });

  //: What a port is wired to: the node at the other end of its link, or the
  //: segment it sits on. "eth1" on its own does not tell you anything.
  function peerOf(iface) {
    const link = (lab?.links ?? []).find(
      (l) => l.a_iface_id === iface.id || l.b_iface_id === iface.id,
    );
    if (link) {
      const otherId = link.a_iface_id === iface.id ? link.b_iface_id : link.a_iface_id;
      const node = (lab?.nodes ?? []).find((n) => n.interfaces?.some((i) => i.id === otherId));
      if (node) {
        return { name: node.name, port: node.interfaces.find((i) => i.id === otherId)?.name };
      }
    }
    const net = (lab?.networks ?? []).find((n) => n.id === iface.network_id);
    return net ? { name: net.name, port: null } : null;
  }

  //: What can be opened on this node, and for anything that cannot, why. A
  //: dimmed row with its reason beats a window that opens only to say "start
  //: the node first" — which is what Console used to do.
  function openActions(node) {
    const why = node.state === "running" ? "" : "start the node first";
    // The two are equivalent in shape (a terminal in the dock), so call
    // both "Console" — QEMU's is a serial stream, Docker's is a real PTY
    // over `docker exec -it`. Two different labels made the container
    // console feel like a different feature people had to hunt for.
    const list = [{ label: "Console", run: () => openConsole(node), why }];
    if (node.runtime === "qemu" || node.console?.vnc?.hostname) {
      list.push({ label: "VNC display", run: openVnc, why });
    }
    if (node.console?.rdp?.hostname) list.push({ label: "RDP", run: openRdp, why });
    //: Logs and the startup config are readable on a stopped node — the last
    //: run's output is often exactly why you are looking.
    list.push({ label: "Logs", run: openLogs, why: "" });
    list.push({ label: "Startup config", run: openConfig, why: "" });
    list.push({ label: "Capture packets", run: openCapture, why });
    list.push({ label: "Wireshark", run: openNodeWireshark, why });
    return list;
  }

  const openActs = $derived(selectedNode ? openActions(selectedNode) : []);
  const openBlocked = $derived(openActs.find((a) => a.why)?.why ?? "");

  //: Bulk start/stop can run inline or as a background task. That was four
  //: buttons; it is one action and a checkbox, because "queued" is a property
  //: of how you start, not a different thing to start.
  let queuedBulk = $state(false);

  async function restartSelected() {
    if (!selected) return;
    busy = true;
    try {
      await api.stop(selected);
      await api.start(selected);
      await loadLab(lab.id, true);
    } catch (e) {
      error = e.message;
    } finally {
      busy = false;
    }
  }
  const linkObj = $derived(lab?.links?.find((l) => l.id === selectedLink));
</script>

{#if authChecked && !currentUser}
  <SignIn {setupRequired} onsignedin={signedIn} />
{/if}

<!--
  The shell only renders when there is a signed-in user. Before the auth
  guards on WebSockets landed, this div was always drawn and the sign-in
  overlay sat on top; every poll and effect inside kept running, and a
  click on VNC opened an unauthenticated tunnel that failed with a
  cryptic tunnel-519. Guarding the whole shell means no stale requests,
  and no clickable actions that would 401 anyway.
-->
{#if currentUser}
<div class="shell">
  <header class="top">
    <div class="brand">
      <div class="logo">{@html LOGO_MARK}</div>
      <div class="wordmark">
        <div class="title">Labtris</div>
        <div class="subtitle">the LLM-native network lab</div>
      </div>
    </div>

    <!-- Identity, then state, then the one thing you type into. Everything that
         is not one of those three moved behind the overflow: the header used to
         carry sixteen controls, which is a control panel, not a header. -->
    <button class="crumb" onclick={(e) => openHeaderMenu(e, labSwitcherMenu(), "Find a lab…")}>
      {#if lab}
        {#if lab.folder}<span class="crumb-folder">{lab.folder}</span><span class="crumb-sep">/</span>{/if}
        <span class="crumb-name">{lab.name}</span>
      {:else}
        <span class="crumb-name dim">Open a lab</span>
      {/if}
      <span class="crumb-caret">▾</span>
    </button>
    <button class="ghost dots" title="lab actions and settings" onclick={(e) => openHeaderMenu(e, overflowMenu())}>•••</button>

    {#if lab && !labIsEmpty}
      <span class="chips">
        {#if runningCount}<span class="chip ok"><i class="cdot"></i>{runningCount} running</span>{/if}
        {#if stoppedCount}<span class="chip"><i class="cdot"></i>{stoppedCount} stopped</span>{/if}
      </span>
    {/if}
    <!-- Health is only worth space when something is wrong; a green dot that is
         always green teaches you to stop looking at it. -->
    {#if showAddressing && unaddressed}
      <!-- Only while the addressing view is on: outside it we have not asked the
           guests, and a count from stale data would be a guess. -->
      <span class="chip warn" title="linked ports with no address — this is what breaks a ping">
        <i class="cdot"></i>{unaddressed} unaddressed
      </span>
    {/if}
    {#if healthDown.length}
      <span class="chip warn" title={healthTitle}>
        <i class="cdot"></i>{healthDown.join(", ")} down
      </span>
    {/if}

    <div class="grow"></div>

    {#if edit}
      <!-- One editor, and only while something is being named. It says what it
           will do, so "Rename" is no longer a verb aimed at a box elsewhere. -->
      <div class="cmdwrap">
        <span class="edit-what">{edit.prompt}</span>
        <!-- svelte-ignore a11y_autofocus -->
        <input
          class="edit-box mono"
          autofocus
          bind:value={edit.value}
          placeholder={edit.placeholder}
          onkeydown={(e) => {
            if (e.key === "Enter") { e.preventDefault(); commitEdit(); }
            else if (e.key === "Escape") { e.preventDefault(); edit = null; }
          }}
        />
        <span class="edit-keys">Enter to save · Esc to cancel</span>
      </div>
    {:else}
      <button class="cmdbar" onclick={openPalette}>
        <span class="cmd-icon" aria-hidden="true">⌕</span>
        <span class="cmd-text">Jump, or type a command…</span>
        <kbd>⌘K</kbd>
      </button>
    {/if}

    <div class="grow"></div>

    {#if lab}
      <button
        class="lock-state"
        class:locked={lab.locked}
        onclick={toggleLock}
        title={lab.locked
          ? "Topology is locked — nodes can run but cannot be moved, linked or deleted. Click to unlock."
          : "Click to lock the topology against accidental edits."}
      >
        {lab.locked ? "🔒 Locked" : "🔓 Unlocked"}
      </button>
    {/if}
    <button class="primary newbtn" onclick={(e) => openHeaderMenu(e, newMenu())}>+ New ▾</button>

    <!-- Assistant toggles a floating window rather than sharing the bottom
         dock with six other tabs — the chat is the one dock resident that
         benefits from being genuinely resizable and closable. -->
    <button class="ghost topbar-btn" onclick={toggleChat} title="open the AI assistant">
      ✦ AI assistant
    </button>

    <!-- Theme picker in the top bar rather than buried in an avatar menu:
         it is a preference set once but seen every second. A single select
         handles 30 themes without dominating the bar. -->
    <select class="topbar-theme" bind:value={theme} title="theme">
      {#each THEME_GROUPS as g}
        <optgroup label={g.group}>
          {#each g.themes as t}
            <option value={t.id}>{t.label}</option>
          {/each}
        </optgroup>
      {/each}
    </select>

    <!-- Host meter — CPU load and RAM at a glance, so "can I start another
         VM here?" is answerable without opening Settings. Clicks straight
         through to the diagnostics pane. -->
    <button
      class="host-meter"
      onclick={openHostDiag}
      title="host load — click for full diagnostics"
    >
      {#if hostMeter.load_pct !== null}
        <span class="mono">CPU {hostMeter.load_pct}%</span>
      {:else}
        <span class="mono muted">CPU …</span>
      {/if}
      ·
      {#if hostMeter.mem_total_gb}
        <span class="mono">RAM {hostMeter.mem_used_gb}/{hostMeter.mem_total_gb} GB</span>
      {:else}
        <span class="mono muted">RAM …</span>
      {/if}
    </button>

    {#if currentUser}
      <span class="who" title={`signed in as ${currentUser.username}`}>
        {currentUser.display_name || currentUser.username}
      </span>
      <button class="ghost topbar-btn" onclick={signOut} title="sign out">⇥</button>
    {/if}

    <input
      type="file"
      accept=".json,.unl,.yml,.yaml,application/json,text/xml,text/yaml"
      style="display:none"
      bind:this={fileInput}
      onchange={onImportFile}
    />
  </header>

  {#if taskProgress}
    <div class="task-banner">
      task {taskProgress.kind}: {taskProgress.status} — {taskProgress.progress}/{taskProgress.total} {taskProgress.message || ""}
    </div>
  {/if}

  {#if error}
    <div class="banner">{error} <button onclick={() => (error = "")}>dismiss</button></div>
  {/if}

  {#if note}
    <div class="banner ok">{note} <button onclick={() => (note = "")}>dismiss</button></div>
  {/if}

  <div class="body">
    {#if !paletteOpen}
      <button class="rail left" title="show the palette" onclick={() => (paletteOpen = true)}>›</button>
    {/if}
    <aside class="palette" hidden={!paletteOpen}>
      <button class="collapse" title="hide the palette" onclick={() => (paletteOpen = false)}>‹</button>
      <input
        class="palfilter mono"
        bind:value={palQ}
        placeholder="filter images and networks…"
      />
      {#if palMatch("internal bridge cloud network segment host nic")}
        <h3>
          Networks
          <Hint
            text="Drop a segment on the canvas, then drag a node's link handle onto it. A bridge joins nodes to each other; a cloud enslaves one of this host's NICs so the lab can reach outside. One NIC can back only one cloud."
          />
        </h3>
      <div
        class="kind"
        draggable="true"
        style="--c:var(--seg)"
        ondragstart={(e) => e.dataTransfer.setData("net", "bridge")}
      >
        <span class="glyph">▭</span>
        <div>
          <strong>Internal bridge</strong>
          <div class="mono tiny">L2 segment, lab-only</div>
        </div>
      </div>
      <!-- NAT sits above cloud deliberately. "These nodes need to reach the
           internet" is what almost everyone reaching for cloud actually wants,
           and cloud puts the lab on a real wire, which is the answer with
           consequences. -->
      <div
        class="kind"
        draggable="true"
        style="--c:var(--accent)"
        ondragstart={(e) => e.dataTransfer.setData("net", "nat")}
      >
        <span class="glyph">⇄</span>
        <div>
          <strong>NAT</strong>
          <div class="mono tiny">a way out, with DHCP · no host NIC touched</div>
        </div>
      </div>
      <div
        class="kind"
        draggable="true"
        style="--c:var(--cloud)"
        ondragstart={(e) => e.dataTransfer.setData("net", "cloud")}
      >
        <span class="glyph">☁</span>
        <div>
          <strong>Cloud (host NIC)</strong>
          <div class="mono tiny">bridges to a real interface</div>
        </div>
      </div>
      {/if}
      {#if qemuFamilies.filter(familyMatches).length}
        <h3>
          QEMU images
          <Hint
            text="Real VMs, software-emulated (TCG) on this host. Where an OS ships several releases, pick the version from the dropdown before dragging. Cloud images are a tenth the size of a desktop install, boot in seconds and already have a serial console; desktop images are multi-GB and want VNC."
          />
        </h3>
        {#each qemuFamilies.filter(familyMatches) as fam (fam.key)}
          {@const q = fam.chosen}
          <div
            class="kind"
            draggable="true"
            style="--c:#fb923c"
            ondragstart={(e) => e.dataTransfer.setData("qemuimg", q.id)}
          >
            <span class="glyph">{q.graphical ? "🖥" : "⌨"}</span>
            <div>
              <strong>{fam.images.length > 1 ? fam.label : q.label}</strong>
              {#if fam.images.length > 1}
                <select
                  class="verpick mono tiny"
                  value={q.id}
                  onpointerdown={(e) => e.stopPropagation()}
                  onchange={(e) => (pickedVersion = { ...pickedVersion, [fam.key]: e.currentTarget.value })}
                >
                  {#each fam.images as v}
                    <option value={v.id}>{v.version || v.label}{v.cached ? " ✓" : ""}</option>
                  {/each}
                </select>
              {/if}
              <div class="mono tiny">
                {q.ram_mb} MB · {q.cpus} vCPU · {q.graphical ? "VNC" : "serial"}
              </div>
              {#if q.phase === "downloading"}
                <div class="mono tiny dl">
                  downloading {q.percent}%
                  {#if q.total}({Math.round(q.done / 1048576)}/{Math.round(q.total / 1048576)} MB){/if}
                </div>
                <div class="bar"><i style={`width:${q.percent || 0}%`}></i></div>
              {:else if q.phase}
                <div class="mono tiny dl">{q.phase}…</div>
                <div class="bar indet"><i></i></div>
              {:else if q.cached}
                <div class="mono tiny ready">✓ on disk — starts immediately</div>
              {:else}
                <div class="mono tiny cold">
                  not downloaded — first start fetches it
                  <button
                    type="button"
                    class="pull mono tiny"
                    onclick={(e) => {
                      e.stopPropagation();
                      pullImage(q.id);
                    }}
                    onpointerdown={(e) => e.stopPropagation()}
                    title="Download the image now so the first node start is instant"
                  >
                    Pull now
                  </button>
                </div>
              {/if}
              {#if q.credentials}
                <div class="mono tiny">login {q.credentials}</div>
              {/if}
            </div>
          </div>
        {/each}
      {/if}
      {#if palKinds.length}
        <h3>Node catalog</h3>
        <p class="hint">Drag any image onto the canvas. No templates required.</p>
      {/if}
      {#each palKinds as k}
        <div
          class="kind"
          draggable="true"
          style={`--c:${k.color}`}
          ondragstart={(e) => e.dataTransfer.setData("kind", k.id)}
        >
          <span class="glyph">{k.glyph}</span>
          <div>
            <strong>{k.label}</strong>
            <div class="mono tiny">{k.image}</div>
            {#if k.note}
              <div class="mono tiny cold">{k.note}</div>
            {/if}
            {#if k.boot}
              <div class="mono tiny cold">~{k.boot}s to a usable CLI</div>
            {/if}
          </div>
        </div>
      {/each}
      <h3>Any image</h3>
      <input bind:value={customImage} placeholder="nginx:alpine" class="mono" />
      <input bind:value={customName} placeholder="optional name prefix" />
      <p class="hint tiny">Drag the chip onto the canvas.</p>
      <div
        class="kind"
        draggable="true"
        style="--c:#94a3b8"
        ondragstart={(e) => e.dataTransfer.setData("kind", "__custom")}
      >
        <span class="glyph">◇</span>
        <div>
          <strong>Custom</strong>
          <div class="mono tiny">{customImage || "set an image"}</div>
        </div>
      </div>
      <div class="templates-header">
        <h3>Your templates</h3>
        <button class="tiny" onclick={openUpload}>Upload image…</button>
      </div>
      {#if templates.filter((t) => palMatch(t.name, t.image)).length}
        <p class="hint">Saved via "Export as template" or uploaded from disk.</p>
        {#each templates.filter((t) => palMatch(t.name, t.image)) as t}
          <div
            class="kind"
            draggable="true"
            style="--c:var(--cloud)"
            ondragstart={(e) => e.dataTransfer.setData("template", t.id)}
          >
            <span class="glyph">{t.icon || "★"}</span>
            <div>
              <strong>{t.name}</strong>
              <button
                class="tmpl-edit"
                title="edit template"
                aria-label={`edit template ${t.name}`}
                onclick={(e) => { e.stopPropagation(); openEditTemplate(t); }}
              >✎</button>
              <!-- A saved QEMU image is addressed by content hash, so its
                   reference says nothing to a person. Show what it was made
                   from and how big it is instead (sizing lives in spec). -->
              <div class="mono tiny">
                {#if t.spec?.from_image}
                  {t.spec.from_image}{t.spec?.ram_mb ? ` · ${t.spec.ram_mb} MB` : ""}{t.spec?.bytes
                    ? ` · ${(t.spec.bytes / 1e9).toFixed(1)} GB`
                    : ""}
                {:else if t.spec?.ram_mb || t.spec?.bytes}
                  {t.spec?.ram_mb ? `${t.spec.ram_mb} MB` : ""}{t.spec?.ram_mb && t.spec?.bytes ? " · " : ""}{t.spec?.bytes
                    ? `${(t.spec.bytes / 1e9).toFixed(1)} GB`
                    : ""}
                {:else}
                  {t.image}
                {/if}
              </div>
              {#if t.description}<div class="tiny dim">{t.description}</div>{/if}
            </div>
          </div>
        {/each}
      {/if}
      <div class="legend">
        <div><i class="dot run"></i> running</div>
        <div><i class="dot def"></i> defined</div>
        <div><i class="dot fail"></i> failed</div>
        <div><i class="dot impair"></i> impaired link</div>
      </div>
    </aside>

    <main
      class="canvas"
      bind:this={canvasEl}
      class:panning={spaceHeld || panning}
      class:nogrid={!prefs.grid}
      onpointerdown={onCanvasDown}
      onwheel={onWheel}
      oncontextmenu={canvasMenu}
      ondragover={(e) => e.preventDefault()}
      ondrop={(e) => {
        e.preventDefault();
        const id = e.dataTransfer.getData("kind");
        const tmplId = e.dataTransfer.getData("template");
        const qemuId = e.dataTransfer.getData("qemuimg");
        const netKind = e.dataTransfer.getData("net");
        if (netKind) {
          startNetwork(netKind, e.clientX, e.clientY, e.currentTarget);
          return;
        }
        if (id === "__custom") {
          dropCustom(e.clientX, e.clientY, e.currentTarget);
          return;
        }
        if (tmplId) {
          const t = templates.find((x) => x.id === tmplId);
          if (t) dropTemplate(t, e.clientX, e.clientY, e.currentTarget);
          return;
        }
        if (qemuId) {
          const img = qemuImages.find((x) => x.id === qemuId);
          if (img) dropQemu(img, e.clientX, e.clientY, e.currentTarget);
          return;
        }
        const k = KIND.find((x) => x.id === id);
        if (k) dropKind(k, e.clientX, e.clientY, e.currentTarget);
      }}
    >
      <div class="grid" style={`transform: translate(${pan.x}px, ${pan.y}px) scale(${pan.k})`}>
        <svg class="wires" width="4000" height="3000">
          <defs>
            <marker
              id="wirehead"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
            >
              <path d="M0 0 L10 5 L0 10 z" fill="var(--accent)" />
            </marker>
          </defs>
          {#if wiring?.from}
            <path
              class="rubber"
              marker-end="url(#wirehead)"
              d={`M ${wiring.from.x} ${wiring.from.y} L ${wireEnd.x} ${wireEnd.y}`}
            />
          {/if}
          {#if marquee}
            {@const r = marqueeRect()}
            <rect class="marquee" x={r.x} y={r.y} width={r.w} height={r.h} />
          {/if}
          {#if lab}
            {#each segmentWires as w (w.key)}
              {@const np = netPos(w.net.id, w.ni)}
              {@const a = portPos(w.node, w.iface, w.iface.idx, lab.nodes.indexOf(w.node), np.x)}
              {@const b = netAnchor(w.net, w.ni, pos(w.node.id, lab.nodes.indexOf(w.node)).x)}
              <path d={wirePath(a, b)} class="link seg" />
              {#if pan.k >= 0.7}
                {@const la = labelAt(a)}
                <text class="iflabel" x={la.x} y={la.y} text-anchor={la.anchor}>{w.iface.name}</text>
              {/if}
            {/each}
            {#each lab.links as link}
              {@const aNode = lab.nodes.find((n) => n.interfaces.some((i) => i.id === link.a_iface_id))}
              {@const bNode = lab.nodes.find((n) => n.interfaces.some((i) => i.id === link.b_iface_id))}
              {@const aIf = aNode?.interfaces.find((i) => i.id === link.a_iface_id)}
              {@const bIf = bNode?.interfaces.find((i) => i.id === link.b_iface_id)}
              {#if aNode && bNode && aIf && bIf}
                {@const ai = lab.nodes.indexOf(aNode)}
                {@const bi = lab.nodes.indexOf(bNode)}
                {@const a = portPos(aNode, aIf, aIf.idx, ai, pos(bNode.id, bi).x)}
                {@const b = portPos(bNode, bIf, bIf.idx, bi, pos(aNode.id, ai).x)}
                {@const d = wirePath(a, b)}
                <!-- A 2px curve is a miserable click target; this invisible
                     one is what you actually hit. -->
                <path
                  {d}
                  class="link-hit"
                  onpointerdown={(e) => pickLink(e, link)}
                  oncontextmenu={(e) => linkMenu(e, link)}
                />
                <path
                  {d}
                  class="link"
                  class:sel={selectedLink === link.id}
                  class:impaired={linkImpaired(link)}
                  class:down={link.admin_up === false}
                />
                <!-- Only worth reading when you are close enough to read it:
                     at 100 nodes zoomed out these would be a grey haze. -->
                {#if pan.k >= 0.7}
                  {@const la = labelAt(a)}
                  {@const lb = labelAt(b)}
                  <text class="iflabel" x={la.x} y={la.y} text-anchor={la.anchor}>{aIf.name}</text>
                  <text class="iflabel" x={lb.x} y={lb.y} text-anchor={lb.anchor}>{bIf.name}</text>
                {/if}
              {/if}
            {/each}
          {/if}
        </svg>
        {#if lab}
          {#each (lab.networks || []).filter((n) => n.kind !== "vxlan" && !n.name.startsWith("lnk-")) as net, ni}
            {@const np = netPos(net.id, ni)}
            <div
              class="netobj"
              class:cloud={net.kind === "cloud"}
              class:hilite={selectedNet === net.id}
              class:wire-target={wiring?.fromNode}
              style={`left:${np.x}px; top:${np.y}px`}
              onpointerdown={(e) => onNetDown(e, net, ni)}
              onpointerup={(e) => wiring?.fromNode && endNetWire(e, net)}
              oncontextmenu={(e) => netMenu(e, net)}
            >
              <span class="glyph">{net.kind === "cloud" ? "☁" : net.kind === "nat" ? "⇄" : "▭"}</span>
              <div class="netname">
                <strong>{net.name}</strong>
                <div class="mono tiny">
                  <!-- What the segment is, in the terms that matter while looking at
                       the canvas: for NAT that is the subnet, not the word "nat". -->
                  {net.kind === "cloud"
                    ? `host ${net.cloud_ref}`
                    : net.kind === "nat"
                      ? `${net.subnet ?? ""}${net.dhcp_first ? " · dhcp" : ""}`
                      : "internal bridge"}
                  {#if net.vlan_aware}· vlan{/if}
                  {#if net.host_ifname}· {net.host_ifname}{/if}
                </div>
              </div>
              <button
                class="netcap"
                title="tcpdump on this segment"
                onclick={() => openNetCapture(net)}>◉</button
              >
              <button
                class="netcap"
                title="open Wireshark on this segment"
                onclick={() =>
                  openWireshark(
                    { kind: "network", id: net.id, label: net.name },
                    `${net.name} · wireshark`,
                  )}>🦈</button
              >
              <button class="netdel" title="delete segment" onclick={() => removeNetwork(net)}>✕</button>
            </div>
          {/each}
          {#each lab.nodes as node, i}
            {@const p = pos(node.id, i)}
            {@const k = node.runtime === "qemu" ? { color: "#fb923c", glyph: "🖥" } : kindFor(node.image)}
            {@const color = node.style?.color || k.color}
            {@const glyph = node.style?.icon || k.glyph}
            <div
              class="node"
              data-id={node.id}
              class:sel={selected === node.id || selectedIds.includes(node.id)}
              class:hilite={hilitedNodeIds.has(node.id) && selected !== node.id && !selectedIds.includes(node.id)}
              class:multi={selectedIds.length > 1 && selectedIds.includes(node.id)}
              class:running={node.state === "running"}
              class:failed={node.state === "failed"}
              class:paused={node.paused}
              style={`left:${p.x}px;top:${p.y}px;--c:${color}`}
              class:wire-target={wiring?.fromNode && wiring.fromNode.id !== node.id}
              onpointerdown={(e) => onNodeDown(e, node, i)}
              onpointerup={(e) => wiring?.fromNode && endNodeWire(e, node)}
              oncontextmenu={(e) => nodeMenu(e, node)}
              ondblclick={() => openDefaultConsole(node)}
            >
              <div class="node-hd">
                <button
                  class="wire-handle"
                  aria-label="connect"
                  title="drag onto another node or a segment to connect"
                  onpointerdown={(e) => beginNodeWire(e, node)}
                >
                  <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
                    <path
                      d="M6.2 9.8 9.8 6.2M5.6 8.4 4.2 9.8a2.4 2.4 0 1 0 3.4 3.4l1.4-1.4M10.4 7.6l1.4-1.4a2.4 2.4 0 1 0-3.4-3.4L7 4.2"
                      fill="none"
                      stroke="currentColor"
                      stroke-width="1.6"
                      stroke-linecap="round"
                    />
                  </svg>
                </button>
                <!-- A status dot, not a coloured glow around the whole card.
                     It reads at any zoom and does not repaint the diagram. -->
                <i
                  class="ndot"
                  class:on={node.state === "running" && !node.paused}
                  class:fail={node.state === "failed"}
                ></i>
                {#if node.style?.icon}<span class="glyph">{glyph}</span>{/if}
                <div class="node-id">
                  <strong>{node.name}</strong>
                  <!-- The image is what you actually need to tell two nodes
                       apart; "docker" you can see from the shape of the card. -->
                  <div class="tiny mono meta" hidden={prefs.nodeLabel === "name"}>
                    {imageLabel(node.image)}{node.paused ? " · paused" : node.state === "running" ? "" : ` · ${node.state}`}
                  </div>
                </div>
                <button
                  class="node-console"
                  aria-label="open console"
                  title="open this node's console"
                  onpointerdown={(e) => e.stopPropagation()}
                  onclick={(e) => { e.stopPropagation(); openDefaultConsole(node); }}
                >&gt;_</button>
              </div>
              <div class="ports">
                {#each node.interfaces as iface}
                  <button
                    class="port"
                    class:left={iface.idx % 2 === 0}
                    class:wire-src={wiring?.from?.id === iface.id}
                    class:wire-ok={wireTarget(iface, node)}
                    class:wire-no={wiring?.from && !wireTarget(iface, node) && wiring.from.id !== iface.id}
                    title={iface.name}
                    onpointerdown={(e) => beginWire(e, iface)}
                    onpointerup={(e) => endWire(e, iface)}
                    onpointerenter={(e) => wiring && endWire(e, iface)}
                  >
                    {iface.name}
                  </button>
                  {#if showAddressing}
                    {@const addr = addrOf(node, iface)}
                    <!-- "no address" is a finding, not a blank: an unaddressed
                         port is the usual reason a lab does not ping. -->
                    <span class="addr" class:none={!addr}>{addr ?? "no address"}</span>
                  {/if}
                {/each}
              </div>
            </div>
          {/each}
        {:else}
          <div class="empty">Create a lab, then drag Alpine onto the canvas — or ask the AI tab.</div>
        {/if}
      </div>
      {#if picker}
        <div class="modal-back" onpointerdown={() => (picker = null)}>
          <!-- svelte-ignore a11y_no_static_element_interactions -->
          <div class="modal" onpointerdown={(e) => e.stopPropagation()}>
            <h3>
              Connect {picker.a.name} to {picker.net ? picker.net.name : picker.b.name}
            </h3>
            <p class="hint tiny">
              Pick the interface on each end. "New interface" adds the next free port —
              a node that has run out of ports would otherwise be unconnectable.
            </p>
            <label class="pick">
              <span>{picker.a.name}</span>
              <select bind:value={picker.aIface}>
                {#each picker.aFree as i}
                  <option value={i.id}>{i.name}</option>
                {/each}
                <option value="__new">+ new interface</option>
              </select>
            </label>
            {#if picker.net}
              <label class="pick">
                <span>segment</span>
                <span class="mono tiny"
                  >{picker.net.kind === "cloud"
                    ? `cloud · host ${picker.net.cloud_ref}`
                    : "internal bridge"}</span
                >
              </label>
            {:else}
              <label class="pick">
                <span>{picker.b.name}</span>
                <select bind:value={picker.bIface}>
                  {#each picker.bFree as i}
                    <option value={i.id}>{i.name}</option>
                  {/each}
                  <option value="__new">+ new interface</option>
                </select>
              </label>
            {/if}
            <div class="modal-actions">
              <button onclick={() => (picker = null)}>Cancel</button>
              <button class="primary" onclick={picker.net ? joinNetwork : confirmPicker}>
                Connect
              </button>
            </div>
          </div>
        </div>
      {/if}
      <!-- Floating over the canvas rather than in the header: they change what
           the canvas shows, so they belong on it. -->
      {#if linkPop && linkObj}
        {@const ends = linkEnds(linkObj)}
        <div class="linkpop" style={`left:${linkPop.x}px; top:${linkPop.y}px`}>
          <div class="linkpop-hd">
            <span>{ends.a?.node.name}</span>
            <span class="port static">{ends.a?.iface.name}</span>
            <span class="dim">↔</span>
            <span>{ends.b?.node.name}</span>
            <span class="port static">{ends.b?.iface.name}</span>
          </div>
          <div class="mono tiny dim">
            {#if ends.a?.addr && ends.b?.addr}
              {ends.a.addr.split("/")[0]} ↔ {ends.b.addr.split("/")[0]} · {ends.a.addr.split("/")[1] ? `/${ends.a.addr.split("/")[1]}` : ""}
            {:else if showAddressing}
              no addresses on this link yet
            {:else}
              turn on Addressing to see what is configured
            {/if}
          </div>
          {#if linkImpaired(linkObj) || linkObj.admin_up === false}
            <div class="tiny warn-t">
              {linkObj.admin_up === false ? "administratively down" : tcSummary(linkObj.impair_ab)}
            </div>
          {/if}
          <div class="linkpop-acts">
            <button onclick={() => pingAcross(linkObj)}>Ping across</button>
            <button onclick={() => { openCapture(); linkPop = null; }}>Capture here</button>
            <button class="ghost" aria-label="close" onclick={() => (linkPop = null)}>✕</button>
          </div>
        </div>
      {/if}

      <div class="canvas-ctl">
        <button
          class:on={showAddressing}
          onclick={toggleAddressing}
          title="ask each running guest what addresses it has, and show them on its ports"
        >⊞ Addressing</button>
        {#if showAddressing}
          <button
            title="every port, its peer and its address, in one table"
            onclick={() => openWindow("addressing", "lab", "Lab addressing", { w: 720, h: 420 })}
          >Table</button>
        {/if}
        <button
          title={paletteOpen || !dockMin ? "hide the panels and use the whole window" : "show the panels again"}
          aria-label="toggle full canvas"
          onclick={() => {
            const full = paletteOpen || !dockMin;
            paletteOpen = !full;
            dockMin = full;
          }}
        >{paletteOpen || !dockMin ? "⤢" : "⤡"}</button>
      </div>

      {#if lab && labIsEmpty}
        <!-- A blank grid tells a first-time user nothing. This names the ways
             something can get onto the canvas, and each one is the real control,
             not a picture of it. The assistant card from the design is held back
             until there is an assistant behind it. -->
        <div class="empty-lab">
          <div class="empty-head">
            <div class="empty-title">{lab.name} is empty</div>
            <div class="empty-sub">Two ways to put something on the canvas.</div>
          </div>
          <div class="empty-cards">
            <div class="empty-card">
              <div class="empty-glyph">⬚</div>
              <div class="empty-card-title">Drag an image in</div>
              <div class="empty-card-body">
                Pick a container or VM from the palette on the left and drop it here.
                Drag between ports to link them.
              </div>
              {#if !paletteOpen}
                <button onclick={() => (paletteOpen = true)}>Show the palette</button>
              {/if}
            </div>
            <div class="empty-card">
              <div class="empty-glyph">⇩</div>
              <div class="empty-card-title">Import a topology</div>
              <div class="empty-card-body">
                Load a lab file exported from Labtris, or start from a copy of a lab
                you already have.
              </div>
              <div class="empty-actions">
                <button onclick={triggerImport}>Import file</button>
                <button onclick={() => startEdit("clone")}>Clone this lab</button>
              </div>
            </div>
          </div>
        </div>
      {/if}
      {#if busy}<div class="busy">working…</div>{/if}
    </main>

  </div>

  {#if annotating}
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div
      class="annotate-veil"
      onpointerdowncapture={captureAt}
      onkeydown={(e) => e.key === "Escape" && (annotating = false)}
      role="button"
      tabindex="0"
    >
      <div class="annotate-hint">Click the thing that is wrong · Esc to cancel</div>
    </div>
  {/if}

  {#if report}
    <div class="pin" style={`left:${report.x}px; top:${report.y}px`}></div>
    <div
      class="composer"
      style={`left:${Math.min(report.x + 14, window.innerWidth - 360)}px; top:${Math.min(
        report.y + 14,
        window.innerHeight - 220,
      )}px`}
    >
      <div class="mono tiny composer-el">{report.context.element}</div>
      <!-- svelte-ignore a11y_autofocus -->
      <textarea
        autofocus
        rows="3"
        bind:value={report.note}
        placeholder="What should have happened?"
        onkeydown={(e) => {
          if (e.key === "Escape") report = null;
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) sendReport();
        }}
      ></textarea>
      <div class="tiny composer-ctx">
        captures {report.context.theme} · {report.context.viewport}
        {#if report.context.errors.length}· {report.context.errors.length} console error{report
            .context.errors.length === 1
            ? ""
            : "s"}{/if}
        {#if report.context.failedCalls.length}· {report.context.failedCalls.length} failed
          call{report.context.failedCalls.length === 1 ? "" : "s"}{/if}
      </div>
      <div class="modal-actions">
        <button onclick={() => (report = null)}>Cancel</button>
        <button class="primary" onclick={sendReport} disabled={!report.note.trim()}>Report</button>
      </div>
    </div>
  {/if}

  {#if importReport}
    <div class="modal-back fixed" onpointerdown={() => (importReport = null)}>
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <div class="modal wide" onpointerdown={(e) => e.stopPropagation()}>
        <h3>Imported {importReport.name}</h3>
        <p class="hint tiny">
          From {importReport.source === "clab" ? "a containerlab topology" : "an EVE-NG .unl"}:
          {importReport.nodes} nodes, {importReport.links} links, {importReport.networks} segments.
        </p>
        {#if importReport.warnings.length}
          <p class="hint tiny danger-text">
            {importReport.warnings.length} thing{importReport.warnings.length === 1 ? "" : "s"}
            could not be carried over exactly:
          </p>
          <ul class="applied">
            {#each importReport.warnings as w}
              <li class="tiny">{w}</li>
            {/each}
          </ul>
        {:else}
          <p class="hint tiny">Everything carried over.</p>
        {/if}
        <div class="modal-actions">
          <button class="primary" onclick={() => (importReport = null)}>Close</button>
        </div>
      </div>
    </div>
  {/if}

  {#if vlanForm}
    <div class="modal-back fixed" onpointerdown={() => (vlanForm = null)}>
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <div class="modal" onpointerdown={(e) => e.stopPropagation()}>
        <h3>{vlanForm.iface.name} · VLAN</h3>
        <label class="pick">
          <span>mode</span>
          <select bind:value={vlanForm.mode}>
            <option value="access">access — one VLAN, untagged</option>
            <option value="trunk">trunk — several, tagged</option>
          </select>
        </label>
        {#if vlanForm.mode === "access"}
          <label class="pick">
            <span>VLAN</span>
            <input class="mono" type="number" min="1" max="4094" bind:value={vlanForm.vid} />
          </label>
          <p class="hint tiny">
            Frames leave this port untagged and arrive tagged with this VLAN. This is
            what a host plugs into.
          </p>
        {:else}
          <label class="pick">
            <span>VLANs</span>
            <input class="mono" bind:value={vlanForm.trunk} placeholder="10, 20, 30" />
          </label>
          <p class="hint tiny">
            Frames keep their tags in both directions. This is what goes between two
            switches — a node on the other end has to understand tags to use it.
          </p>
        {/if}
        <div class="modal-actions">
          <button onclick={() => (vlanForm = null)}>Cancel</button>
          <button class="primary" onclick={commitVlan}>Apply</button>
        </div>
      </div>
    </div>
  {/if}

  {#if leaseView}
    <div class="modal-back fixed" onpointerdown={() => (leaseView = null)}>
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <div class="modal wide" onpointerdown={(e) => e.stopPropagation()}>
        <h3>{leaseView.name}</h3>
        <div class="tabs subtabs">
          <button class:on={leaseView.tab === "leases"} onclick={() => (leaseView.tab = "leases")}>
            Leases{#if leaseView.leases.length}<sup class="badge">{leaseView.leases.length}</sup>{/if}
          </button>
          <button class:on={leaseView.tab === "reserved"} onclick={() => (leaseView.tab = "reserved")}>
            Reserved
          </button>
          <button class:on={leaseView.tab === "sessions"} onclick={() => (leaseView.tab = "sessions")}>
            NAT sessions{#if leaseView.sessions.length}<sup class="badge">{leaseView.sessions.length}</sup>{/if}
          </button>
          <div class="grow"></div>
          <button class="tiny" onclick={refreshLeaseView}>Refresh</button>
        </div>

        {#if leaseView.tab === "leases"}
          {#if leaseView.leases.length}
            <table class="leases">
              <thead><tr><th>address</th><th>MAC</th><th>name</th></tr></thead>
              <tbody>
                {#each leaseView.leases as l}
                  <tr>
                    <td class="mono">{l.ip}</td>
                    <td class="mono tiny">{l.mac}</td>
                    <td class="tiny">{l.name === "*" ? "" : l.name}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          {:else}
            <p class="hint tiny">
              Nothing has asked for an address yet. A guest needs a DHCP client running on
              the port that is on this segment — <code>udhcpc -i eth1</code> on Alpine,
              <code>dhclient</code> elsewhere.
            </p>
          {/if}

        {:else if leaseView.tab === "reserved"}
          <p class="hint tiny">
            Pin a port to an address so it gets the same one every boot. Keyed on the MAC
            Labtris already assigned. Must be inside {leaseView.net?.subnet} and outside the
            pool ({leaseView.net?.dhcp_first}–{leaseView.net?.dhcp_last}).
          </p>
          <table class="leases">
            <thead><tr><th>node</th><th>port</th><th>MAC</th><th>reserved</th></tr></thead>
            <tbody>
              {#each portsOn(leaseView.net) as p}
                <tr>
                  <td>{p.node.name}</td>
                  <td class="mono tiny">{p.iface.name}</td>
                  <td class="mono tiny">{p.iface.mac}</td>
                  <td>
                    <input
                      class="mono resv"
                      value={p.iface.reserved_ip ?? ""}
                      placeholder="none"
                      onchange={(e) => saveReservation(p.iface, e.currentTarget.value.trim())}
                    />
                  </td>
                </tr>
              {:else}
                <tr><td colspan="4" class="tiny hint">No ports on this segment yet.</td></tr>
              {/each}
            </tbody>
          </table>

        {:else}
          {#if leaseView.sessions.length}
            <table class="leases">
              <thead>
                <tr><th>proto</th><th>from</th><th>to</th><th>seen as</th><th>state</th></tr>
              </thead>
              <tbody>
                {#each leaseView.sessions as s}
                  <tr>
                    <td class="tiny">{s.proto}</td>
                    <td class="mono tiny">{s.src}:{s.sport}</td>
                    <td class="mono tiny">{s.dst}:{s.dport}</td>
                    <!-- The part a capture inside the lab cannot show you. -->
                    <td class="mono tiny">{s.translated}</td>
                    <td class="tiny dim">{s.state}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          {:else}
            <p class="hint tiny">
              {leaseView.note || "No flows are being translated right now. conntrack only shows a connection while it is alive."}
            </p>
          {/if}
        {/if}

        <div class="modal-actions">
          <button onclick={() => (leaseView = null)}>Close</button>
        </div>
      </div>
    </div>
  {/if}

  {#if aiConfirm}
    <!-- The assistant wants to call a destructive tool. Ask the user
         before it fires. The overlay is not click-to-dismiss because
         either answer needs to be explicit — a stray click should not
         quietly deny (frustrating) or allow (dangerous). -->
    <div class="modal-back fixed">
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <div class="modal">
        <h3>The AI assistant wants your permission</h3>
        <p>
          It is about to run <code class="mono">{aiConfirm.tool}</code> with:
        </p>
        <pre class="mono tiny">{JSON.stringify(aiConfirm.args, null, 2)}</pre>
        <p class="hint tiny">
          This tool changes state that is hard or impossible to undo. Approve if
          this is what you want; deny to have the AI assistant explain what it was
          trying to do without doing it.
        </p>
        <div class="modal-actions">
          <button onclick={() => answerConfirm(false)}>Deny</button>
          <button class="primary danger" onclick={() => answerConfirm(true)}>Allow</button>
        </div>
      </div>
    </div>
  {/if}

  {#if netForm}
    <div class="modal-back fixed" onpointerdown={() => (netForm = null)}>
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <div class="modal" onpointerdown={(e) => e.stopPropagation()}>
        <h3>
          New {netForm.kind === "cloud"
            ? "cloud"
            : netForm.kind === "nat"
              ? "NAT network"
              : "internal bridge"}
        </h3>
        <label class="pick">
          <span>name</span>
          <input class="mono" bind:value={netForm.name} />
        </label>
        {#if netForm.kind === "nat"}
          <p class="hint tiny">
            A bridge with a gateway on it and masquerading behind it. Nodes reach the
            outside without the lab being put on one of the host's own interfaces —
            which is what a cloud does, and why a cloud can take the host off the
            network.
          </p>
          <label class="pick">
            <span>subnet</span>
            <input class="mono" bind:value={netForm.subnet} placeholder="pick one for me" />
          </label>
          <p class="hint tiny">
            Left blank, a free /24 is chosen from 10.200.0.0/16. The first address
            becomes the gateway.
          </p>
          <label class="pick">
            <span>hand out addresses (DHCP)</span>
            <input type="checkbox" bind:checked={netForm.dhcp} />
          </label>
        {/if}
        {#if netForm.kind === "bridge" || netForm.kind === "nat"}
          <label class="pick">
            <span>VLAN filtering</span>
            <input type="checkbox" bind:checked={netForm.vlan_aware} />
          </label>
          {#if netForm.vlan_aware}
            <p class="hint tiny">
              Makes this bridge an actual switch: ports carry a PVID and tags are
              enforced. Without it the kernel forwards tagged frames without reading
              them, so a trunk appears to work and an access port does not exist.
            </p>
            <label class="pick">
              <span>tag protocol</span>
              <select bind:value={netForm.vlan_proto}>
                <option value="802.1Q">802.1Q — one tag</option>
                <option value="802.1ad">802.1ad — QinQ, outer tag</option>
              </select>
            </label>
          {/if}
        {/if}
        {#if netForm.kind === "cloud"}
          <label class="pick">
            <span>host interface</span>
            <select bind:value={netForm.cloud_ref}>
              {#each hostIfaces.filter((i) => i.usable !== false) as i}
                <option value={i.name}>
                  {i.name}{i.reusable ? " (host bridge — reused as-is)" : ""}{i.addresses?.length
                    ? ` — ${i.addresses[0]}`
                    : ""}{i.default_route || i.has_default_route_addr ? "  ⚠ default route" : ""}
                </option>
              {/each}
              {#each hostIfaces.filter((i) => i.usable === false) as i}
                <option value={i.name} disabled>
                  {i.name} — {i.unusable_reason}
                </option>
              {/each}
            </select>
          </label>
          {#if !hostIfaces.some((i) => i.usable !== false)}
            <p class="hint tiny danger-text">
              This host has no interface a cloud can use. A cloud needs a physical NIC, a veth,
              or a host-owned bridge (like a netplan br0 or EVE-NG's pnet0).
            </p>
          {/if}
          {#if (() => { const p = hostIfaces.find((i) => i.name === netForm.cloud_ref); return p?.default_route || p?.has_default_route_addr; })()}
            {#if hostIfaces.find((i) => i.name === netForm.cloud_ref)?.reusable}
              <p class="hint tiny">
                This bridge already holds the host's default-route address. Reusing it is safe
                (the address stays put — lab veths just join the bridge), but confirm below.
              </p>
            {:else}
              <p class="hint tiny danger-text">
                That NIC carries this host's default route. Enslaving it to a lab bridge moves
                its traffic onto the bridge and will take the host off the network — including
                this session. netd refuses it unless you tick this.
              </p>
            {/if}
            <label class="pick">
              <span>I understand</span>
              <input type="checkbox" bind:checked={netForm.allow_default_route} />
            </label>
          {/if}
        {/if}
        <div class="modal-actions">
          <button onclick={() => (netForm = null)}>Cancel</button>
          <button class="primary" onclick={confirmNetwork}>Create</button>
        </div>
      </div>
    </div>
  {/if}

  {#if uploadForm}
    <div class="modal-back fixed" onpointerdown={() => (uploadForm.submitting ? null : (uploadForm = null))}>
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <div class="modal" onpointerdown={(e) => e.stopPropagation()}>
        <h3>Upload an image</h3>
        <p class="hint tiny">
          Register a qcow2 (or other disk image; will be converted) as a template.
          Appears in the palette under Your templates.
        </p>
        <label class="pick">
          <span>file</span>
          <input
            type="file"
            accept=".qcow2,.img,.vmdk,.vdi,.vhd,.raw"
            onchange={(e) => (uploadForm.file = e.target.files?.[0] || null)}
          />
        </label>
        <label class="pick">
          <span>name</span>
          <input bind:value={uploadForm.name} placeholder="pfSense 24.03" />
        </label>
        <label class="pick">
          <span>RAM (MB)</span>
          <input type="number" min="64" bind:value={uploadForm.ram_mb} />
        </label>
        <label class="pick">
          <span>CPUs</span>
          <input type="number" min="1" max="32" bind:value={uploadForm.cpus} />
        </label>
        <label class="pick">
          <span>NIC model</span>
          <select bind:value={uploadForm.nic_model}>
            <option value="virtio-net-pci">virtio-net-pci (modern Linux)</option>
            <option value="e1000">e1000 (old kernels, safest fallback)</option>
            <option value="e1000e">e1000e</option>
            <option value="rtl8139">rtl8139</option>
            <option value="vmxnet3">vmxnet3</option>
          </select>
        </label>
        <label class="pick">
          <span>disk bus</span>
          <select bind:value={uploadForm.disk_bus}>
            <option value="virtio">virtio (modern Linux)</option>
            <option value="ide">ide (no virtio-blk driver in guest)</option>
            <option value="scsi">scsi</option>
            <option value="sata">sata</option>
          </select>
        </label>
        <label class="pick">
          <span>iface scheme</span>
          <select bind:value={uploadForm.iface_scheme}>
            <option value="ens">ens3, ens4 — systemd + virtio</option>
            <option value="eth">eth0, eth1 — busybox, older Linux, cirros</option>
            <option value="enp">enp0s3, enp0s4</option>
            <option value="vmware">ens192, ens224 — VMware VMXNET3</option>
            <option value="srl">Nokia SR Linux</option>
            <option value="ios">Cisco IOS</option>
            <option value="paloalto">mgmt, eth1/1, eth1/2 — Palo Alto PAN-OS</option>
            <option value="nxos">Mgmt0, E1/1, E1/2 — Cisco Nexus 9000v</option>
          </select>
        </label>
        <label class="pick">
          <span>graphical (VNC)</span>
          <input type="checkbox" bind:checked={uploadForm.graphical} />
        </label>
        <label class="pick">
          <span>description</span>
          <input bind:value={uploadForm.description} placeholder="optional" />
        </label>
        {#if uploadForm.submitting}
          <p class="hint tiny">Uploading… {uploadPct}%</p>
        {/if}
        {#if uploadForm.error}
          <p class="hint tiny danger-text">{uploadForm.error}</p>
        {/if}
        <div class="modal-actions">
          <button disabled={uploadForm.submitting} onclick={() => (uploadForm = null)}>
            Cancel
          </button>
          <button
            class="primary"
            disabled={uploadForm.submitting || !uploadForm.file || !uploadForm.name}
            onclick={submitUpload}
          >
            {uploadForm.submitting ? "Uploading…" : "Upload"}
          </button>
        </div>
      </div>
    </div>
  {/if}

  {#if editTemplateForm}
    <div class="modal-back fixed" onpointerdown={() => (editTemplateForm.submitting ? null : (editTemplateForm = null))}>
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <div class="modal" onpointerdown={(e) => e.stopPropagation()}>
        <h3>Edit template</h3>
        <p class="hint tiny">
          Only affects new nodes and, for iface-scheme, only interfaces added after
          this change. Existing nodes keep their current sizing and port names.
        </p>
        <label class="pick">
          <span>name</span>
          <input bind:value={editTemplateForm.name} />
        </label>
        <label class="pick">
          <span>RAM (MB)</span>
          <input type="number" min="64" bind:value={editTemplateForm.ram_mb} />
        </label>
        <label class="pick">
          <span>CPUs</span>
          <input type="number" min="1" max="32" bind:value={editTemplateForm.cpus} />
        </label>
        <label class="pick">
          <span>CPU model</span>
          <select bind:value={editTemplateForm.cpu}>
            <option value="qemu64">qemu64 (pre-2010, no SSE4.2 — safest but modern glibc rejects it)</option>
            <option value="host">host (KVM: pass every CPU feature through; needed by PAN-OS/RHEL 9+)</option>
            <option value="max">max (software-only equivalent of host; slower)</option>
          </select>
        </label>
        <label class="pick">
          <span>NIC model</span>
          <select bind:value={editTemplateForm.nic_model}>
            <option value="virtio-net-pci">virtio-net-pci (modern Linux)</option>
            <option value="e1000">e1000 (old kernels, safest fallback)</option>
            <option value="e1000e">e1000e</option>
            <option value="rtl8139">rtl8139</option>
            <option value="vmxnet3">vmxnet3</option>
          </select>
        </label>
        <label class="pick">
          <span>disk bus</span>
          <select bind:value={editTemplateForm.disk_bus}>
            <option value="virtio">virtio (modern Linux)</option>
            <option value="ide">ide (no virtio-blk driver in guest)</option>
            <option value="scsi">scsi</option>
            <option value="sata">sata</option>
          </select>
        </label>
        <label class="pick">
          <span>iface scheme</span>
          <select bind:value={editTemplateForm.iface_scheme}>
            <option value="ens">ens3, ens4 — systemd + virtio</option>
            <option value="eth">eth0, eth1 — busybox, older Linux, cirros</option>
            <option value="enp">enp0s3, enp0s4</option>
            <option value="vmware">ens192, ens224 — VMware VMXNET3</option>
            <option value="srl">Nokia SR Linux</option>
            <option value="ios">Cisco IOS</option>
            <option value="paloalto">mgmt, eth1/1, eth1/2 — Palo Alto PAN-OS</option>
            <option value="nxos">Mgmt0, E1/1, E1/2 — Cisco Nexus 9000v</option>
          </select>
        </label>
        <label class="pick">
          <span>graphical (VNC)</span>
          <input type="checkbox" bind:checked={editTemplateForm.graphical} />
        </label>
        <label class="pick">
          <span>description</span>
          <input bind:value={editTemplateForm.description} placeholder="optional" />
        </label>

        <!-- Companion files: a BIOS blob and/or a CD-ROM ISO that some
             appliances need at every boot (NX-OSv 9000's config schema
             CD, vjunosevoefi's OVMF-sata firmware). Each row shows the
             current file's basename with an X to unlink, and an "Upload"
             button that runs a hidden file input. Empty rows say "no
             file — click Upload to add one" so the pane doesn't render
             blank next to labels you don't recognise. -->
        <label class="pick">
          <span>companion BIOS</span>
          <div class="companion-row">
            {#if editTemplateForm.bios}
              <span class="companion-chip mono tiny" title={editTemplateForm.bios}>
                {editTemplateForm.bios.split("/").pop()}
                <button class="chip-x" onclick={() => (editTemplateForm.bios = null)} title="unlink">×</button>
              </span>
            {:else}
              <span class="hint tiny">no BIOS attached</span>
            {/if}
            <input type="file" id="edit-bios-input" style="display:none"
                   onchange={(e) => { uploadCompanion("bios", e.currentTarget.files); e.currentTarget.value = ""; }} />
            <button class="tiny"
                    disabled={editTemplateForm.uploading === "bios"}
                    onclick={() => document.getElementById("edit-bios-input").click()}>
              {editTemplateForm.uploading === "bios" ? "Uploading…" : "Upload…"}
            </button>
          </div>
        </label>

        <label class="pick">
          <span>companion CD-ROM</span>
          <div class="companion-row">
            {#if editTemplateForm.cdrom}
              <span class="companion-chip mono tiny" title={editTemplateForm.cdrom}>
                {editTemplateForm.cdrom.split("/").pop()}
                <button class="chip-x" onclick={() => (editTemplateForm.cdrom = null)} title="unlink">×</button>
              </span>
            {:else}
              <span class="hint tiny">no CD-ROM attached</span>
            {/if}
            <input type="file" id="edit-cdrom-input" style="display:none"
                   onchange={(e) => { uploadCompanion("cdrom", e.currentTarget.files); e.currentTarget.value = ""; }} />
            <button class="tiny"
                    disabled={editTemplateForm.uploading === "cdrom"}
                    onclick={() => document.getElementById("edit-cdrom-input").click()}>
              {editTemplateForm.uploading === "cdrom" ? "Uploading…" : "Upload…"}
            </button>
          </div>
        </label>

        <label class="pick pick-tall">
          <span>extra qemu args</span>
          <textarea rows="3" placeholder="one per line, e.g. -smbios type=1,manufacturer=Cisco"
                    bind:value={editTemplateForm.qemu_extra_args_text}></textarea>
        </label>

        <label class="pick pick-tall">
          <span>bootstrap
            <Hint text="Optional first-boot config sequence typed into the serial console. JSON: {'{'}step_timeout_s, steps: [{'{'}wait_for, type{'}'}, ...]{'}'}. `wait_for` is a regex, `type` is what to send. Substitutes {'{'}name{'}'} and {'{'}node_id{'}'}. Recipes in packaging/recipes/bootstrap/. Runs once per node; watch progress at /nodes/{'{'}id{'}'}/bootstrap." />
          </span>
          <textarea
            rows="10"
            class="mono tiny"
            placeholder={'{"steps":[\n  {"wait_for":"login:", "type":"admin\\r\\n"},\n  {"wait_for":"Password:", "type":"admin\\r\\n"},\n  {"wait_for":"# *$", "type":"hostname {name}\\r\\n"}\n]}'}
            bind:value={editTemplateForm.bootstrap_text}></textarea>
        </label>

        {#if editTemplateForm.error}
          <p class="hint tiny danger-text">{editTemplateForm.error}</p>
        {/if}
        <div class="modal-actions">
          <button disabled={editTemplateForm.submitting} onclick={() => (editTemplateForm = null)}>
            Cancel
          </button>
          <button
            class="primary"
            disabled={editTemplateForm.submitting || !editTemplateForm.name}
            onclick={submitEditTemplate}
          >
            {editTemplateForm.submitting ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </div>
  {/if}

  <!-- Windows live above the whole app: inside .canvas they were cropped by
       its overflow:hidden, which quietly ate the resize grip. -->
  <div class="winlayer">
    {#each windows as win (win.id)}
      <FloatWindow {win} onclose={closeWindow} onfocus={focusWindow}>
        {#if win.kind === "console"}
          <!-- The live row, not the snapshot taken when the window opened: a
               node stopped since then still reported itself as running, which
               is exactly the state the console needs to tell you about. -->
          <Terminal
            node={liveNode(win.node)}
            onstart={() => startNode(win.node.id)}
            onstatus={(t) => setWinStatus(win.id, t)}
          />
        {:else if win.kind === "vnc" || win.kind === "rdp"}
          <VncPane node={liveNode(win.node)} protocol={win.kind} onstatus={(t) => setWinStatus(win.id, t)} />
        {:else if win.kind === "capture"}
          <CapturePane target={win.target} onstatus={(t) => setWinStatus(win.id, t)} />
        {:else if win.kind === "wireshark"}
          <WiresharkPane target={win.target} onstatus={(t) => setWinStatus(win.id, t)} />
        {:else if win.kind === "addressing"}
          <div class="addrtable">
            {#if !showAddressing}
              <p class="hint tiny">Turn on Addressing to fill this in.</p>
            {/if}
            <table>
              <thead>
                <tr><th>node</th><th>port</th><th>peer</th><th>address</th><th>state</th></tr>
              </thead>
              <tbody>
                {#each addressTable as r}
                  <tr class:warn-row={r.peer && r.state === "none"}>
                    <td>{r.node.name}</td>
                    <td class="mono tiny">{r.iface.name}</td>
                    <td class="tiny">
                      {#if r.peer}
                        {r.peer.name}{#if r.peer.port}<span class="mono dim"> {r.peer.port}</span>{/if}
                      {:else}
                        <span class="dim">not linked</span>
                      {/if}
                    </td>
                    <td class="mono tiny">{r.addr ?? "—"}</td>
                    <td class="tiny">
                      <!-- "we did not ask" is not "it has none", and conflating
                           them sends people debugging a working node. -->
                      {r.state === "stopped"
                        ? "stopped"
                        : r.state === "unknown"
                          ? "no agent to ask"
                          : r.state === "none"
                            ? "no address"
                            : "addressed"}
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
            {#if !addressTable.length}
              <p class="hint tiny">This lab has no interfaces yet.</p>
            {/if}
          </div>
        {:else if win.kind === "settings"}
          <SettingsPane
            onstatus={(t) => setWinStatus(win.id, t)}
            themes={THEME_GROUPS}
            {theme}
            ontheme={(id) => (theme = id)}
            {prefs}
            onprefs={savePrefs}
            {llm}
            onllm={setLlm}
            initialSection={win.section ?? "appearance"}
            {currentUser}
          />
        {:else if win.kind === "chat"}
          <ChatPane
            {chat}
            bind:chatIn
            {aiStatus}
            {aiStep}
            stepCount={aiStepCount}
            {busy}
            {liveTurn}
            attachments={aiAttachments}
            onattach={addAttachments}
            onremoveattach={removeAttachment}
            labId={lab?.id}
            onsend={sendAi}
            onstop={stopAi}
            onclear={clearChat}
          />
        {/if}
      </FloatWindow>
    {/each}
  </div>

  <div class="docks" class:min={dockMin}>
    <div class="tabs">
      <!-- Left: which node you are talking to. Right: everything that is not a
           terminal. They were one flat row of eight, so "Console" sat beside
           "Events" as though picking a node and picking a panel were the same
           kind of choice. -->
      <div class="termtabs">
        {#each termNodes as n}
          <button
            class="termtab"
            class:on={dock === "console" && (termA === n.id || termB === n.id)}
            onclick={() => { termA = n.id; switchDock("console"); }}
          >
            <i class="ndot" class:on={n.state === "running"} class:fail={n.state === "failed"}></i>
            <span>{n.name}</span>
            {#if n.state !== "running"}<span class="tbadge">{n.state}</span>{/if}
            <span
              class="tclose"
              role="button"
              tabindex="0"
              aria-label={`close ${n.name}`}
              onclick={(e) => { e.stopPropagation(); closeTerm(n.id); }}
              onkeydown={(e) => e.key === "Enter" && closeTerm(n.id)}
            >✕</span>
          </button>
        {/each}
        <button
          class="termtab add"
          title="open a terminal on another node"
          onclick={(e) =>
            openHeaderMenu(
              e,
              (lab?.nodes ?? [])
                .filter((n) => !termTabs.includes(n.id))
                .map((n) => ({
                  label: n.name,
                  glyph: n.runtime === "qemu" ? "🖥" : "▪",
                  hint: n.state === "running" ? "" : n.state,
                  run: () => openConsole(n),
                }))
                .concat((lab?.nodes ?? []).every((n) => termTabs.includes(n.id))
                  ? [{ header: "every node is open" }]
                  : []),
            )}
        >+</button>
      </div>

      <div class="grow"></div>

      <button
    class:on={dock === "console" && termB !== null}
    onclick={toggleSplit}
    disabled={!canSplit}
    title={canSplit
      ? "show two terminals side by side"
      : "needs a second node in the lab"}
  >⊞ Split</button>
      <button
        class:on={dock === "inspector"}
        onclick={() => switchDockAndExpand("inspector")}
        title={selectedNode
          ? `Inspector — node ${selectedNode.name}`
          : linkObj
            ? "Inspector — link"
            : lab
              ? `Inspector — lab (${(lab.nodes || []).length} nodes)`
              : "Inspector"}
      >Inspector{#if inspectorContextChip}<sup class="ctx-chip">{inspectorContextChip}</sup>{/if}</button>
      <button class:on={dock === "logs"} onclick={() => switchDockAndExpand("logs")}>Logs</button>
      <button class:on={dock === "packets"} onclick={() => switchDockAndExpand("packets")}>Packets</button>
      <button class:on={dock === "config"} onclick={() => switchDockAndExpand("config")}>Config</button>
      <button class:on={dock === "hosts"} onclick={() => { switchDockAndExpand("hosts"); loadHosts(); }}>Hosts</button>
      <button class:on={dock === "hooks"} onclick={() => switchDockAndExpand("hooks")} disabled={!lab}>Hooks</button>
      <button class:on={dock === "events"} onclick={() => switchDockAndExpand("events")}>
        Events{#if unseenErrors}<sup class="badge">{unseenErrors}</sup>{/if}
      </button>
      {#if dock === "vnc"}
        <button class="on" onclick={() => switchDockAndExpand("console")}>{vncProtocol.toUpperCase()}</button>
      {/if}
      <div class="grow"></div>
      <button class="ghost" title="open this terminal in its own window" onclick={popOutTerm} disabled={!nodeA}>↗</button>
      <button
        class="ghost dock-min"
        title={dockMin ? "expand the dock" : "minimise the dock"}
        aria-label={dockMin ? "expand dock" : "minimise dock"}
        onclick={toggleDockMin}
      >{dockMin ? "▴" : "▾"}</button>
    </div>
    {#if dock === "inspector"}
      <div class="inspector inspector-in-dock">
        {#if selectedNode}
          <div class="insp-head">
            <h3 class="insp-name">{selectedNode.name}</h3>
            <span
              class="state-chip"
              class:on={selectedNode.state === "running" && !selectedNode.paused}
              class:bad={selectedNode.state === "failed"}
            >
              {selectedNode.paused
                ? "Paused"
                : selectedNode.state === "running"
                  ? "Running"
                  : selectedNode.state === "failed"
                    ? "Failed"
                    : "Stopped"}
            </span>
          </div>
          <p class="mono tiny dim">{selectedNode.id}</p>
          {#if selectedNode.last_error}
            <p class="err tiny">{selectedNode.last_error}</p>
          {/if}

          <!-- The primary action is whatever this node's state makes it. A stopped node
               is not offered Stop or Suspend even dimmed: those are not blocked actions
               with a reason, they are meaningless on a node that is not running. -->
          <div class="acts">
            {#if selectedNode.state === "running"}
              <button class="primary" onclick={stopSelected}>Stop</button>
              <button onclick={restartSelected}>Restart</button>
              {#if selectedNode.paused}
                <button onclick={doResume}>Resume</button>
              {:else}
                <button onclick={doSuspend}>Suspend</button>
              {/if}
            {:else}
              <button class="primary" onclick={startSelected}>Start</button>
            {/if}
          </div>

          <h3>Open</h3>
          <div class="acts">
            {#each openActs as a}
              <button onclick={a.run} disabled={!!a.why} title={a.why}>{a.label}</button>
            {/each}
          </div>
          {#if openBlocked}
            <p class="why">{openBlocked} — these need a running guest to talk to.</p>
          {/if}

          <h3>Details</h3>
          <dl>
            <dt>image</dt>
            <dd>
              {imageLabel(selectedNode.image)}
              {#if imageLabel(selectedNode.image) !== selectedNode.image}
                <span class="tiny dim mono"> · {selectedNode.image}</span>
              {/if}
            </dd>
            <dt>type</dt>
            <dd>
              {selectedNode.runtime === "qemu"
                ? `QEMU · ${selectedImage?.graphical ? "VNC" : "serial"}`
                : "Docker"}
            </dd>
            <dt>memory</dt>
            <dd>
              {selectedNode.ram_mb ?? selectedImage?.ram_mb ?? "default"} MB ·
              {selectedNode.cpu_limit ?? selectedImage?.cpus ?? "default"} vCPU
            </dd>
            {#if selectedNode.runtime !== "qemu" && selectedNode.runtime_ref}
              <dt>container</dt><dd class="mono tiny">{selectedNode.runtime_ref.slice(0, 12)}</dd>
            {/if}
            {#if selectedImage?.credentials}
              <dt>login</dt><dd class="mono">{selectedImage.credentials}</dd>
            {/if}
          </dl>

          <h3>
            Interfaces
            <button class="tiny" onclick={addPort}>+ Add</button>
          </h3>
          {#if ifaceSchemeLabel}
            <p class="hint tiny">{ifaceSchemeLabel}</p>
          {/if}
          <ul class="ifaces">
            {#each selectedNode.interfaces as i}
              {@const peer = peerOf(i)}
              {@const seg = (lab?.networks ?? []).find((n) => n.id === i.network_id)}
              <li>
                <span class="mono">{i.name}</span>
                {#if peer}
                  <span class="dim">→</span>
                  <span class="peer">{peer.name}</span>
                  {#if peer.port}<span class="mono dim">{peer.port}</span>{/if}
                {:else}
                  <span class="dim">not connected</span>
                {/if}
                {#if showAddressing}
                  {@const addr = addrOf(selectedNode, i)}
                  {#if addr}<span class="mono dim">{addr}</span>{/if}
                {/if}
                {#if seg?.vlan_aware}
                  <button
                    class="vlan-tag"
                    title="set this port's VLAN"
                    onclick={() =>
                      (vlanForm = {
                        iface: i,
                        mode: i.vlan_mode ?? "access",
                        vid: i.vlan_id ?? 1,
                        trunk: i.trunk_vids ?? "",
                      })}
                  >
                    {i.vlan_mode === "trunk"
                      ? `trunk ${i.trunk_vids}`
                      : i.vlan_mode === "access"
                        ? `vlan ${i.vlan_id}`
                        : "set vlan"}
                  </button>
                {/if}
              </li>
            {/each}
          </ul>

          <div class="acts">
            <button onclick={doExportTemplate}>Export as template</button>
            <button class="danger" onclick={removeSelected}>Delete node</button>
          </div>

          <h3>Style</h3>
          <div class="style-row">
            <input
              class="mono"
              placeholder="icon glyph"
              value={selectedNode.style?.icon || ""}
              onchange={(e) => setStyle(e.currentTarget.value, selectedNode.style?.color || null)}
            />
            <input
              type="color"
              value={selectedNode.style?.color || "#3ee0c5"}
              onchange={(e) => setStyle(selectedNode.style?.icon || null, e.currentTarget.value)}
            />
          </div>
          <h3>Resources</h3>
          {#if sizeForm}
            <p class="hint tiny">
              Neither can change on a live guest — this is what the node boots with next
              time it starts.
            </p>
            <div class="style-row">
              <input class="mono" type="number" min="16" step="128" placeholder="RAM MB" bind:value={sizeForm.ram_mb} />
              <input class="mono" type="number" min="1" max="8" step="1" placeholder="vCPU" bind:value={sizeForm.cpu_limit} />
            </div>
            {#if selectedNode.runtime === "qemu" && nicModels.length}
              <p class="hint tiny">
                virtio is fastest but invisible to a guest without virtio drivers — that
                boots with no network and nothing to explain why. e1000 is the safe answer.
              </p>
              <label class="pick">
                <span>NIC</span>
                <select bind:value={sizeForm.nic_model}>
                  <option value="">image default</option>
                  {#each nicModels as m}
                    <option value={m.id}>{m.label}</option>
                  {/each}
                </select>
              </label>
            {/if}
            <div class="style-row">
              <button class="primary" onclick={saveSize}>Save</button>
              <button onclick={() => (sizeForm = null)}>Cancel</button>
            </div>
          {:else}
            <div class="style-row">
              <span class="mono tiny">
                {selectedNode.ram_mb ?? "default"} MB · {selectedNode.cpu_limit ?? "default"} vCPU{selectedNode.runtime === "qemu"
                  ? ` · ${selectedNode.nic_model ?? "virtio"}`
                  : ""}
              </span>
              <button onclick={editSize}>Change…</button>
            </div>
          {/if}
          <h3>Remote console target</h3>
          <p class="hint tiny">
            Where the RDP tunnel dials — the guest's own address on a lab network, or a
            hostfwd on this host. A QEMU node already has a VNC display without this.
            {#if selectedNode.console?.rdp?.hostname}
              Currently
              <code>{selectedNode.console.rdp.hostname}:{selectedNode.console.rdp.port || 3389}</code>.
            {/if}
          </p>
          <div class="style-row">
            <input class="mono" placeholder="hostname" bind:value={consoleForm.hostname} />
            <input class="mono" style="width:70px" placeholder="port" bind:value={consoleForm.port} />
          </div>
          <div class="style-row">
            <input class="mono" placeholder="username" bind:value={consoleForm.username} />
            <input class="mono" type="password" placeholder="password" bind:value={consoleForm.password} />
            <button onclick={saveConsoleTarget}>Save</button>
          </div>
          {#if selectedNode.runtime === "qemu"}
            <h3>Snapshots</h3>
            <p class="hint tiny">Real QEMU `savevm`/`loadvm` — point-in-time VM state, not a Docker approximation.</p>
            <div class="style-row">
              <input bind:value={snapshotName} placeholder="snapshot name" />
              <button onclick={doSaveSnapshot} disabled={selectedNode.state !== "running"}>Save</button>
            </div>
            {#each snapshots as name}
              <div class="kind" style="--c:#fb923c">
                <span class="glyph">📷</span>
                <div style="flex:1"><strong>{name}</strong></div>
                <button onclick={() => doRestoreSnapshot(name)}>Restore</button>
              </div>
            {/each}
          {/if}
        {:else if linkObj}
          <h3>Link</h3>
          <p class="mono tiny">{linkObj.id}</p>
          <dl>
            <dt>admin</dt><dd>{linkObj.admin_up === false ? "DOWN" : "UP"}</dd>
            <dt>A→B</dt><dd class="mono tiny">{tcSummary(linkObj.impair_ab)}</dd>
            <dt>B→A</dt><dd class="mono tiny">{tcSummary(linkObj.impair_ba)}</dd>
          </dl>
          <h3>Impairment</h3>
          {#if tcEdit}
            <p class="hint tiny">
              netem on each direction's own tap. Blank or zero leaves that knob unset;
              clearing every field removes the qdisc.
            </p>
            <label class="pick">
              <span>same both ways</span>
              <input type="checkbox" bind:checked={tcEdit.mirror} />
            </label>
            <table class="tc">
              <thead>
                <tr><th></th><th>A→B</th><th class:dim={tcEdit.mirror}>B→A</th></tr>
              </thead>
              <tbody>
                {#each TC_FIELDS as f}
                  <tr>
                    <td class="tc-lbl">{f.label}<span class="tiny"> {f.unit}</span></td>
                    <td>
                      <input class="mono" type="number" min="0" step={f.step} placeholder="—" bind:value={tcEdit.ab[f.key]} />
                    </td>
                    <td>
                      <input class="mono" type="number" min="0" step={f.step} placeholder={tcEdit.mirror ? "=" : "—"} disabled={tcEdit.mirror} bind:value={tcEdit.ba[f.key]} />
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
            <div class="acts">
              <button class="primary" onclick={applyTc}>Apply</button>
              <button onclick={() => (tcEdit = null)}>Cancel</button>
            </div>
          {:else}
            <div class="seg" role="radiogroup" aria-label="link profile">
              <button role="radio" aria-checked={activePreset === ""} class:on={activePreset === ""} onclick={() => applyPreset(null)}>none</button>
              {#each realPresets as name}
                <button role="radio" aria-checked={activePreset === name} class:on={activePreset === name} onclick={() => applyPreset(name)}>{name}</button>
              {/each}
            </div>
            {#if activePreset === null}
              <p class="hint tiny">Custom parameters — no profile matches.</p>
            {/if}
            <div class="acts">
              <button onclick={editTc}>Edit parameters…</button>
            </div>
          {/if}
          <h3>Link</h3>
          <div class="acts">
            <button onclick={() => setAdmin(false)}>Admin down</button>
            <button onclick={() => setAdmin(true)}>Admin up</button>
            <button onclick={openCapture}>Capture</button>
            <button class="danger" onclick={deleteSelectedLink}>Delete link</button>
          </div>
        {:else}
          <div class="insp-head">
            <h3 class="insp-name">{lab ? lab.name : "No lab open"}</h3>
            <Hint text="Drag a node's link handle onto another node or a segment to wire it. Click a link to apply tc, or right-click one for capture and shaping." />
          </div>
          {#if lab}
            <p class="tiny dim">
              {lab.nodes.length} node{lab.nodes.length === 1 ? "" : "s"} · {runningCount} running{lab.folder ? ` · ${lab.folder}` : ""}
            </p>
          {:else}
            <p class="hint tiny">Open a lab from the switcher, or make one with New.</p>
          {/if}
          {#if selectedIds.length > 1}
            <h3>{selectedIds.length} nodes selected</h3>
            <p class="hint tiny">
              Drag any one of them to move the group; right-click for actions on all of them.
            </p>
            <div class="acts">
              <button class="primary" onclick={() => startSelection(true)}>Start them</button>
              <button onclick={() => startSelection(false)}>Stop them</button>
              <button onclick={() => (selectedIds = [])}>Select none</button>
              <button class="danger" onclick={() => wipeNodes(selectedIds)}>Wipe disks</button>
              <button class="danger" onclick={deleteSelection}>Delete them</button>
            </div>
          {/if}

          {#if lab}
            <div class="acts">
              <button
                class="primary"
                onclick={() => (queuedBulk ? runBulkTask("start_all") : startAll())}
                disabled={labIsEmpty || !stoppedCount}
              >
                {stoppedCount ? `Start ${stoppedCount} stopped` : "Everything is running"}
              </button>
              <button
                onclick={() => (queuedBulk ? runBulkTask("stop_all") : stopAll())}
                disabled={!runningCount}
              >
                Stop all
              </button>
            </div>
            {#if labIsEmpty}
              <p class="why">Add a node to enable the lab controls.</p>
            {/if}
            <label class="opt">
              <input type="checkbox" bind:checked={queuedBulk} />
              <span>Start one at a time (queued)</span>
            </label>
            <p class="hint tiny">
              Queued runs in the background with a progress bar, which is what you want
              once a lab is large enough that starting it takes a while.
            </p>

            <h3>
              Images
              <Hint text="Fetches what this lab needs now, so the first start is not also a download. Only the QEMU catalog knows what is already on disk; whether a Docker tag is present is the engine's business." />
            </h3>
            {#if labImages.length}
              <ul class="imglist">
                {#each labImages as im}
                  <li>
                    <span class="mono tiny">{imageLabel(im.image)}</span>
                    {#if im.status}
                      <span class="tiny" class:dim={!im.missing} class:cold={im.missing}>{im.status}</span>
                    {/if}
                  </li>
                {/each}
              </ul>
            {:else}
              <p class="hint tiny">No images yet — this lab has no nodes.</p>
            {/if}
            <div class="acts">
              <button onclick={() => runBulkTask("pull_images")} disabled={labIsEmpty}>
                Pre-pull images
              </button>
            </div>

            {#if lab.nodes.length}
              <h3>Nodes</h3>
              <ul class="nodelist">
                {#each lab.nodes as n}
                  <li>
                    <button class="node-row" onclick={() => (selected = n.id)}>
                      <i class="dot" class:on={n.state === "running"} class:fail={n.state === "failed"}></i>
                      <span class="grow">{n.name}</span>
                      <span class="tiny dim">{n.runtime}</span>
                    </button>
                  </li>
                {/each}
              </ul>
            {/if}
          {/if}
        {/if}
      </div>
    {:else if dock === "packets"}
      <div class="packets">
        <div class="pkt-bar">
          <input class="mono" bind:value={bpf} placeholder="optional BPF, e.g. icmp" />
          {#if capturing}
            <button onclick={stopCapture}>Stop capture</button>
          {:else}
            <button class="primary" onclick={startCapture}>Start capture</button>
          {/if}
          <span class="tiny">{capturing ? "live" : "idle"} · {packets.length} frames</span>
        </div>
        <pre>{packets.join("\n") || "tcpdump on the host veth — start nodes, then capture."}</pre>
      </div>
    {:else if dock === "console"}
      {#if termNodes.length}
        <div class="termgrid" class:split={nodeB}>
          <div class="termpane">
            <div class="termhd">
              <i class="ndot" class:on={nodeA?.state === "running"}></i>
              <strong>{nodeA?.name}</strong>
              <span class="mono tiny dim">{nodeA?.runtime === "qemu" ? "serial" : "sh"}</span>
              <div class="grow"></div>
              <button class="ghost tiny" title="close this terminal" onclick={() => closeTerm(termA)}>✕</button>
            </div>
            {#if nodeA}
              <Terminal
                bind:this={paneA}
                node={nodeA}
                onstart={() => startNode(nodeA.id)}
                onstatus={() => {}}
              />
            {/if}
          </div>
          {#if nodeB}
            <div class="termpane">
              <div class="termhd">
                <i class="ndot" class:on={nodeB.state === "running"}></i>
                <strong>{nodeB.name}</strong>
                <span class="mono tiny dim">{nodeB.runtime === "qemu" ? "serial" : "sh"}</span>
                <div class="grow"></div>
                <button class="ghost tiny" title="close this terminal" onclick={() => closeTerm(termB)}>✕</button>
              </div>
              <Terminal
                bind:this={paneB}
                node={nodeB}
                onstart={() => startNode(nodeB.id)}
                onstatus={() => {}}
              />
            </div>
          {/if}
        </div>

        <!-- One command bar for both panes, with the target named on it. Typing
             into a split without saying which half you are typing into is how you
             run a command on the wrong router. -->
        <div class="cmdrow">
          <button
            class="runon"
            disabled={!nodeB}
            title={nodeB ? "choose which pane this runs on" : "only one terminal is open"}
            onclick={() => (runOn = runOn === "a" ? "b" : "a")}
          >
            Run on <strong>{target?.name ?? "—"}</strong>{#if nodeB}<span class="dim"> ▾</span>{/if}
          </button>
          <form class="cmdform" onsubmit={(e) => { e.preventDefault(); runCommand(); }}>
            <span class="prompt mono">$</span>
            <input class="mono" bind:value={cmdLine} onkeydown={historyKey} placeholder={target ? `run on ${target.name}` : "no terminal open"} />
            {#if (history[target?.id] ?? []).length}
              <span class="keyhint">↑ history</span>
            {/if}
          </form>
          <div class="quick">
            {#each quickCommands(target) as q}
              <button class="qchip mono" onclick={() => runCommand(q)}>{q}</button>
            {/each}
          </div>
        </div>
      {:else}
        <div class="termempty">
          <p>No terminal open.</p>
          <p class="hint tiny">
            Open one from a node's <span class="mono">&gt;_</span>, or with + on the left.
            Double-clicking a node opens its default console.
          </p>
        </div>
      {/if}
    {:else if dock === "config"}
      <div class="console">
        <div class="console-hd">
          <span>startup-config · {selectedNode?.name || "select a node"}</span>
          <div>
            <button onclick={saveConfig} disabled={!selected}>Save</button>
            <button class="primary" onclick={pushConfig} disabled={!selected}>Save &amp; push</button>
          </div>
        </div>
        <div class="sets">
          <div class="sets-hd">
            <strong>Config sets</strong>
            <Hint
              text="A named snapshot of every node's startup-config. Build the lab once, save it as `solution`, then flip the whole topology back to it in one action. Applying a set writes it to every node and pushes it live to any that are running."
            />
            <span class="grow"></span>
            <input
              class="mono"
              bind:value={newSetName}
              placeholder="name this state"
              onkeydown={(e) => e.key === "Enter" && captureSet()}
            />
            <button onclick={captureSet} disabled={!lab || !newSetName.trim()}
              >Capture current</button
            >
          </div>
          {#if configSets.summary?.length}
            <div class="set-list">
              {#each configSets.summary as st}
                <div class="set" class:active={configSets.active === st.name}>
                  <strong>{st.name}</strong>
                  <span class="tiny mono">
                    {st.nodes} node{st.nodes === 1 ? "" : "s"}
                    {#if st.missing}· {st.missing} missing{/if}
                  </span>
                  {#if configSets.active === st.name}<span class="tiny ready">applied</span>{/if}
                  <span class="grow"></span>
                  <button onclick={() => applySet(st.name)}>Apply</button>
                  <button class="danger" onclick={() => deleteSet(st.name)}>Delete</button>
                </div>
              {/each}
            </div>
          {:else}
            <p class="hint tiny">
              No saved states yet. Configure the nodes, then capture them under a name.
            </p>
          {/if}
        </div>
        <textarea
          class="mono config-edit"
          bind:value={configText}
          placeholder={"interface eth0\n no shutdown\n"}
        ></textarea>
      </div>
    {:else if dock === "logs"}
      <div class="console">
        <div class="console-hd">
          <span>logs · {selectedNode?.name || "select a node"}</span>
          <div>
            <input
              class="mono"
              style="width:160px"
              bind:value={logsPattern}
              placeholder="filter pattern"
            />
            <button onclick={refreshLogs} disabled={!selected}>Refresh</button>
          </div>
        </div>
        <pre>{logsText.join("\n") || "docker logs, tail 300, optional regex filter"}</pre>
      </div>
    {:else if dock === "vnc"}
      <div class="vnc">
        <div class="console-hd">
          <span
            >{vncProtocol} · {selectedNode?.name || "node"} ·
            <span class="tiny">{vncStatus}</span></span
          >
          <button class="tiny" onclick={() => openGuac(vncProtocol)}>Reconnect</button>
        </div>
        <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
        <div class="vnc-canvas" tabindex="0" bind:this={vncContainerEl}></div>
      </div>
    {:else if dock === "hooks"}
      {#if lab}
        <HooksPane labId={lab.id} onstatus={(s) => noteEvent({ kind: "info", text: `hooks: ${s}` })} />
      {:else}
        <div class="hint tiny">Open a lab to configure its ready hooks.</div>
      {/if}
    {:else if dock === "events"}
      <div class="events">
        <div class="console-hd">
          <span>
            {eventLog.length} event{eventLog.length === 1 ? "" : "s"} this session
            {#if lab}· {lab.name}{/if}
          </span>
          <div>
            <label class="tiny"
              ><input type="checkbox" bind:checked={eventsErrorsOnly} /> errors only</label
            >
            <button onclick={clearEvents} disabled={!eventLog.length}>Clear</button>
          </div>
        </div>
        <div class="event-list">
          {#if !shownEvents.length}
            <p class="hint tiny">
              {eventLog.length
                ? "No errors — untick “errors only” to see everything."
                : "Nothing has gone wrong yet. Node failures, failed API calls and browser errors land here as they happen."}
            </p>
          {/if}
          {#each shownEvents as e}
            <div class="event {e.level}">
              <span class="mono tiny at">{e.at.toTimeString().slice(0, 8)}</span>
              <span class="src tiny">{e.source}</span>
              {#if e.nodeId && lab?.nodes?.some((n) => n.id === e.nodeId)}
                <button
                  class="linky"
                  onclick={() => {
                    selected = e.nodeId;
                    selectedLink = null;
                    selectedNet = null;
    linkPop = null;
                  }}>{e.text}</button
                >
              {:else}
                <span class="txt">{e.text}</span>
              {/if}
            </div>
          {/each}
        </div>
      </div>
    {:else if dock === "hosts"}
      <div class="hosts">
        <h3>
          Diagnostics
          <Hint
            text="Everything anyone would ask you to paste into a bug report. The same document is available as `labtris-doctor` on the command line, or GET /api/v1/system/diagnostics?fmt=text."
          />
        </h3>
        {#if !diag}
          <button onclick={loadDiag}>Collect diagnostics</button>
        {:else}
          <pre class="diag mono">{diagText}</pre>
          <div class="acts">
            <button onclick={loadDiag}>Refresh</button>
            <button onclick={() => navigator.clipboard?.writeText(diagText)}>Copy</button>
          </div>
        {/if}
        <h3>Host tuning</h3>
        {#if !tuning}
          <p class="hint tiny">Reading this host's sysctls…</p>
        {:else if tuningDrift.length}
          <p class="hint tiny">
            {tuningDrift.length === 1
              ? "One sysctl on this host differs"
              : `${tuningDrift.length} sysctls on this host differ`} from what a busy lab wants:
          </p>
          <dl class="tune">
            {#each tuningDrift as [k, want, have]}
              <dt title={k}>{k.split(".").slice(-2).join(".")}</dt>
              <dd class="mono">{have ?? "?"} → {want}</dd>
            {/each}
          </dl>
          <button onclick={applyTuning}>Apply recommended sysctls</button>
        {:else}
          <p class="hint tiny">
            This host already matches the recommended sysctls — nothing to apply.
          </p>
        {/if}
        <p class="hint">
          Multi-host control plane (F6) — register a second labtris-netd over TCP+token. A
          `vxlan`-kind network can then span the participating hosts' underlay IPs. See
          <code>make netd-hostb</code> for a local demo of a second host in its own netns.
        </p>
        <table class="host-table">
          <thead>
            <tr
              ><th>name</th><th>endpoint</th><th>underlay ip</th><th>status</th><th>vxlan</th><th
              ></th></tr
            >
          </thead>
          <tbody>
            {#each hosts as h}
              <tr>
                <td>{h.name}{h.is_local ? " (local)" : ""}</td>
                <td class="mono tiny">{h.endpoint}</td>
                <td>
                  <input
                    class="mono tiny"
                    value={h.underlay_ip || ""}
                    placeholder="10.x.x.x"
                    onchange={(e) => setHostUnderlay(h, e.currentTarget.value)}
                  />
                </td>
                <td><span class:ok={h.reachable}>{h.reachable ? "up" : "down"}</span></td>
                <td>
                  {#if hostCaps[h.id]?.links?.vxlan}
                    <span class:ok={hostCaps[h.id].links.vxlan.supported}>
                      {hostCaps[h.id].links.vxlan.supported ? "supported" : "ENOTSUP"}
                    </span>
                  {:else if hostCaps[h.id]?.error}
                    <span class="tiny">{hostCaps[h.id].error}</span>
                  {:else}
                    <button class="tiny" onclick={() => loadHostCaps(h)}>probe</button>
                  {/if}
                </td>
                <td>
                  {#if !h.is_local}
                    <button class="danger" onclick={() => removeHost(h)}>remove</button>
                  {/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
        <form
          class="host-form"
          onsubmit={(e) => {
            e.preventDefault();
            registerHost();
          }}
        >
          <input bind:value={hostForm.name} placeholder="name (host-b)" />
          <input
            class="mono"
            bind:value={hostForm.endpoint}
            placeholder="tcp://10.201.0.2:9601"
          />
          <input class="mono" bind:value={hostForm.token} placeholder="token" />
          <input
            class="mono"
            bind:value={hostForm.underlay_ip}
            placeholder="underlay ip 10.201.0.2"
          />
          <button class="primary" type="submit">Register host</button>
        </form>
      </div>
    {/if}
  </div>
</div>
{/if}

{#if qemuOptForm && qemuOptSchema && selectedNode}
  <div class="modal-back" onpointerdown={() => (qemuOptForm = null)}>
    <div class="modal wide" onpointerdown={(e) => e.stopPropagation()}>
      <h3>Machine options · {selectedNode.name}</h3>
      <p class="hint tiny">
        How the machine is built, not what runs inside it. None of these can change on a
        live guest — they take effect on the next start.
      </p>
      {#each Object.entries(qemuOptSchema.options) as [key, opt]}
        {@const disabled = key === "extra_args" && !qemuOptSchema.extra_args_enabled}
        <label class="pick optrow" class:dim={disabled}>
          <span>{opt.label}</span>
          {#if opt.type === "bool"}
            <input type="checkbox" bind:checked={qemuOptForm[key]} {disabled} />
          {:else if opt.type === "choice"}
            <select bind:value={qemuOptForm[key]} {disabled}>
              {#each opt.choices as c}<option value={c}>{c}</option>{/each}
            </select>
          {:else if opt.type === "int"}
            <input
              class="mono"
              type="number"
              min={opt.min}
              max={opt.max}
              bind:value={qemuOptForm[key]}
              {disabled}
            />
          {:else}
            <input
              class="mono"
              placeholder={disabled ? "disabled on this install" : "-device foo -device bar"}
              value={(qemuOptForm[key] || []).join(" ")}
              {disabled}
              onchange={(e) =>
                (qemuOptForm[key] = e.currentTarget.value.split(/\s+/).filter(Boolean))}
            />
          {/if}
        </label>
        <p class="hint tiny opthelp">{opt.help}</p>
      {/each}
      <div class="modal-actions">
        <button onclick={() => (qemuOptForm = null)}>Cancel</button>
        <button class="primary" onclick={saveQemuOptions}>Save</button>
      </div>
    </div>
  </div>
{/if}

{#if menu}
  <Menu x={menu.x} y={menu.y} title={menu.title} items={menu.items} onclose={closeMenu} />
{/if}

{#if headerMenu}
  <Menu
    x={headerMenu.x}
    y={headerMenu.y}
    items={headerMenu.items}
    filter={headerMenu.filter || ""}
    onclose={() => (headerMenu = null)}
  />
{/if}

<style>
  .shell { height: 100%; display: flex; flex-direction: column; background:
    radial-gradient(1200px 600px at 10% -10%, color-mix(in srgb, var(--accent) 8%, transparent), transparent 50%),
    radial-gradient(900px 500px at 100% 0%, color-mix(in srgb, var(--accent-2) 10%, transparent), transparent 45%),
    var(--bg); }
  .top { display: flex; align-items: center; gap: 10px; padding: 10px 16px; border-bottom: 1px solid var(--stroke); }
  .brand { display: flex; gap: 10px; align-items: center; }
  .logo { width: 30px; height: 30px; flex: 0 0 30px; line-height: 0; }
  .logo :global(svg) { width: 100%; height: 100%; display: block; }
  .wordmark { display: flex; flex-direction: column; line-height: 1.15; }
  .title { font-weight: 600; font-size: 16px; letter-spacing: 0.01em; }
  /* Subtitle is the same string as the landing-page H1 so anyone who
     ever wondered "what is this thing?" sees the answer next to the
     logo — no click, no docs trip. Kept small and dim so it does not
     fight the crumb next to it for attention. Hidden below the width
     the crumb starts to crowd the title — the crumb is more useful
     than the tagline once you are actually in a lab. */
  .subtitle { font-size: 10.5px; color: var(--muted); letter-spacing: 0.02em; margin-top: 1px; }
  @media (max-width: 900px) { .subtitle { display: none; } }
  .dots { min-height: 30px; padding: 0 8px; color: var(--muted); letter-spacing: 0.05em; }
  /* The command bar is the header's one large target, centred between two
     flexible gaps so it stays put as the chips on either side change width. */
  .cmdwrap, .cmdbar {
    display: flex; align-items: center; gap: 9px;
    width: 330px; max-width: 34vw; min-height: var(--ctl-h);
    padding: 0 12px; border-radius: var(--r-ctl);
    border: 1px solid var(--stroke); background: var(--bg);
  }
  .cmdbar { color: var(--muted); font-weight: 400; }
  .cmdbar:hover { border-color: color-mix(in srgb, var(--accent) 40%, var(--stroke)); }
  .cmd-icon { font-size: 14px; opacity: 0.8; }
  .cmd-text { flex: 1; text-align: left; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .cmdbar kbd {
    font-family: inherit; font-size: 10.5px; padding: 1px 5px;
    border-radius: 5px; border: 1px solid var(--stroke); color: var(--muted);
  }
  .cmdwrap { width: 420px; max-width: 42vw; background: var(--bg-2); }
  .cmdwrap .edit-box { flex: 1; min-width: 0; border: 0; background: transparent; padding: 0; }
  /* Chips carry a dot because the colour alone is the whole signal otherwise,
     and colour alone is not a signal everyone can read. */
  .chips { display: flex; gap: 6px; }
  .chip {
    display: inline-flex; align-items: center; gap: 6px; height: 22px;
    padding: 0 9px; border-radius: 999px; font-size: 11.5px; font-weight: 500;
    color: var(--muted); background: var(--raise);
  }
  .chip .cdot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
  .chip.ok { color: var(--accent); }
  .chip.warn { color: var(--warn); }
  .newbtn { font-weight: 600; }
  .whoami {
    display: flex; align-items: center; gap: 8px; padding: 0 10px 0 6px;
    background: transparent; border-color: var(--stroke); font-weight: 500;
  }
  .who { font-size: 12.5px; color: var(--muted); padding: 0 4px; }
  .topbar-btn {
    background: transparent; border: 1px solid var(--stroke);
    padding: 2px 8px; font-size: 12px; border-radius: 4px;
  }
  .topbar-theme {
    background: transparent; border: 1px solid var(--stroke);
    padding: 2px 6px; font-size: 12px; border-radius: 4px;
    max-width: 160px;
  }
  .host-meter {
    background: transparent; border: 1px solid var(--stroke);
    padding: 2px 8px; font-size: 12px; border-radius: 4px;
    cursor: pointer; color: var(--fg);
  }
  .host-meter .mono { font-family: "IBM Plex Mono", monospace; font-size: 11.5px; }
  .host-meter .muted { color: var(--muted); }
  .avatar {
    display: inline-flex; align-items: center; justify-content: center;
    width: 22px; height: 22px; border-radius: 50%;
    background: var(--accent); color: #0b1418; font-size: 11px; font-weight: 600;
  }
  .sub { color: var(--muted); font-size: 12px; }
  .lab-switch { display: flex; gap: 8px; flex: 1; flex-wrap: wrap; }
  .sets { border-bottom: 1px solid var(--stroke); padding: 8px 10px; }
  .sets-hd { display: flex; align-items: center; gap: 8px; }
  .sets-hd input { width: 150px; }
  .set-list { margin-top: 6px; display: flex; flex-direction: column; gap: 4px; }
  .set { display: flex; align-items: center; gap: 8px; padding: 4px 6px; border-radius: 6px; }
  .set.active { background: color-mix(in srgb, var(--ok, #3ee0c5) 12%, transparent); }
  .grow { flex: 1; }
  /* The inline editor is deliberately wide and labelled: it is modal in
     effect, so it should not read as one more control in a row. */
  .edit-box { min-width: 230px; }
  .edit-what { font-size: 11px; opacity: 0.75; white-space: nowrap; }
  .edit-keys { font-size: 10.5px; opacity: 0.5; white-space: nowrap; }
  /* Sits over the canvas, but must not swallow a drop: the whole point is
     that you drag an image onto it. Only the cards themselves take pointer
     events, so the surrounding area still drops through to the canvas. */
  .empty-lab {
    position: absolute; inset: 0; display: flex; flex-direction: column;
    align-items: center; justify-content: center; gap: 22px;
    pointer-events: none; padding: 24px; text-align: center;
  }
  .empty-head { display: flex; flex-direction: column; gap: 6px; }
  .empty-title { font-size: 20px; font-weight: 600; }
  .empty-sub { color: var(--muted); font-size: 13px; }
  .empty-cards {
    display: flex; flex-wrap: wrap; gap: 14px; justify-content: center;
    max-width: 720px;
  }
  .empty-card {
    pointer-events: auto; width: 300px; text-align: left;
    display: flex; flex-direction: column; gap: 7px;
    padding: 16px; border-radius: 12px;
    border: 1px solid var(--stroke); background: var(--panel);
  }
  .empty-glyph { font-size: 17px; opacity: 0.7; }
  .empty-card-title { font-weight: 600; font-size: 13.5px; }
  .empty-card-body { font-size: 12.5px; color: var(--muted); line-height: 1.45; }
  .why { margin: 2px 0 0; font-size: 11.5px; color: var(--muted); }
  .addrtable { flex: 1; overflow: auto; padding: 10px 12px; }
  .addrtable table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .addrtable th {
    position: sticky; top: 0; background: var(--panel); text-align: left;
    font-weight: 500; color: var(--muted); font-size: 10.5px;
    text-transform: uppercase; letter-spacing: 0.06em;
    padding: 5px 10px 5px 0; border-bottom: 1px solid var(--stroke);
  }
  .addrtable td { padding: 5px 10px 5px 0; border-bottom: 1px solid var(--stroke); }
  .addrtable tr.warn-row td { color: var(--warn); }
  .subtabs { padding: 0 0 8px; border-bottom: 1px solid var(--stroke); margin-bottom: 10px; }
  .resv { min-height: 26px; padding: 0 8px; font-size: 11.5px; width: 130px; }
  .modal.wide .leases td { vertical-align: middle; }
  .leases { width: 100%; border-collapse: collapse; font-size: 12px; }
  .leases th {
    text-align: left; font-weight: 500; color: var(--muted); font-size: 10.5px;
    text-transform: uppercase; letter-spacing: 0.06em; padding: 4px 8px 4px 0;
    border-bottom: 1px solid var(--stroke);
  }
  .leases td { padding: 5px 8px 5px 0; border-bottom: 1px solid var(--stroke); }
  .vlan-tag {
    font-family: "IBM Plex Mono", ui-monospace, monospace;
    font-size: 9px; padding: 1px 5px; border-radius: 4px;
    background: color-mix(in srgb, var(--seg) 18%, transparent); color: var(--seg);
  }
  .empty-actions { display: flex; gap: 8px; margin-top: 3px; }
  .empty-card button { font-size: 12px; padding: 6px 10px; }
  .palette-btn {
    display: flex; align-items: center; gap: 10px; min-width: 200px;
    font-size: 12.5px; color: var(--muted); background: var(--bg-2);
  }
  .palette-btn kbd {
    margin-left: auto; font-size: 10.5px; font-family: inherit;
    padding: 1px 5px; border-radius: 5px; border: 1px solid var(--stroke);
  }
  .crumb {
    display: flex; align-items: center; gap: 6px; max-width: 340px;
    font-size: 13px; padding: 7px 10px;
  }
  .crumb-folder { color: var(--muted); }
  .crumb-sep { color: var(--muted); opacity: 0.6; }
  .crumb-name { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .crumb-name.muted { font-weight: 400; color: var(--muted); }
  .crumb-caret { color: var(--muted); font-size: 10px; }
  .counts { display: flex; gap: 6px; }
  .count {
    font-size: 11px; padding: 3px 8px; border-radius: 999px;
    border: 1px solid var(--stroke); color: var(--muted); white-space: nowrap;
  }
  .count.on { color: var(--ok, #3ee0c5); border-color: color-mix(in srgb, var(--ok, #3ee0c5) 40%, transparent); }
  .lock-state {
    font-size: 11px; padding: 3px 8px; border-radius: 999px;
    border: 1px solid var(--stroke); white-space: nowrap; opacity: 0.85;
  }
  .lock-state { background: transparent; color: var(--muted); cursor: pointer; }
  .lock-state.locked {
    color: var(--warn, #f5b74e);
    border-color: color-mix(in srgb, var(--warn, #f5b74e) 45%, transparent);
  }
  /* Fixed height, so a row of controls lines up instead of each one sizing
     itself from its own text. Straight from the redesign's .btn. */
  select, input, button {
    background: var(--bg-2); color: var(--text);
    border: 1px solid var(--stroke); border-radius: var(--r-ctl);
    min-height: var(--ctl-h); padding: 0 12px;
    font-size: 13.33px; font-weight: 500;
  }
  textarea { border-radius: var(--r-ctl); }
  button { display: inline-flex; align-items: center; justify-content: center; gap: 6px; white-space: nowrap; }
  button.primary { background: var(--accent); color: #0b1418; border-color: var(--accent); font-weight: 600; }
  button:disabled { opacity: 0.38; }
  button.ghost { background: transparent; border-color: transparent; color: var(--muted); }
  button.danger { background: transparent; border-color: color-mix(in srgb, var(--danger) 40%, transparent); color: var(--danger); }
  .marquee { fill: color-mix(in srgb, var(--accent) 12%, transparent); stroke: var(--accent); stroke-width: 1; stroke-dasharray: 4 3; }
  .node.multi { box-shadow: 0 0 0 2px var(--accent), var(--shadow); }
  .modal.wide { width: 520px; max-height: 82vh; overflow: auto; }
  .optrow { align-items: center; }
  .optrow.dim { opacity: 0.5; }
  .opthelp { margin: 0 0 10px; }
  .verpick { margin: 3px 0; padding: 1px 4px; max-width: 100%; }
  .diag { font-size: 11px; line-height: 1.5; white-space: pre; overflow: auto; max-height: 320px;
    padding: 10px 12px; border: 1px solid var(--stroke); border-radius: 8px; background: var(--bg-2); }
  .events { display: flex; flex-direction: column; min-height: 0; height: 100%; }
  .event-list { overflow: auto; padding: 6px 12px 12px; }
  .event { display: flex; gap: 10px; align-items: baseline; padding: 3px 0; font-size: 12px; border-bottom: 1px solid color-mix(in srgb, var(--stroke) 55%, transparent); }
  .event .at { color: var(--muted); flex: 0 0 auto; }
  .event .src { flex: 0 0 52px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; font-size: 10px; }
  .event .txt, .event .linky { flex: 1; word-break: break-word; }
  .event.error .txt, .event.error .linky { color: var(--danger); }
  .event.warn .txt { color: var(--warn); }
  .event .linky { background: none; border: 0; padding: 0; text-align: left; color: inherit; text-decoration: underline dotted; cursor: pointer; font-size: 12px; }
  .badge { background: var(--danger); color: #fff; border-radius: 999px; padding: 0 5px; font-size: 9px; margin-left: 3px; }
  .seg { display: flex; flex-wrap: wrap; gap: 0; margin: 8px 0; border: 1px solid var(--stroke); border-radius: 8px; overflow: hidden; }
  .seg button { flex: 1 0 auto; border: 0; border-radius: 0; background: transparent; padding: 6px 10px; font-size: 11px; }
  .seg button + button { border-left: 1px solid var(--stroke); }
  .seg button.on { background: color-mix(in srgb, var(--accent) 22%, transparent); color: var(--accent); font-weight: 600; }
  .palette { position: relative; }
  .palette h3 { position: relative; }
  .health { display: flex; gap: 6px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); }
  .health span.ok { color: var(--accent); font-size: 9px; cursor: default; }
  .health span.down { padding: 4px 8px; border-radius: 999px; color: var(--danger); border: 1px solid color-mix(in srgb, var(--danger) 45%, transparent); }
  .banner { margin: 8px 16px; padding: 8px 12px; border-radius: 10px; background: color-mix(in srgb, var(--danger) 12%, transparent); display: flex; justify-content: space-between; }
  /* Same shape, different meaning: a notice is not a failure. */
  .banner.ok { background: color-mix(in srgb, var(--ok, #2e7d61) 12%, transparent); }
  .task-banner { margin: 0 16px 8px; padding: 6px 12px; border-radius: 10px; background: color-mix(in srgb, var(--accent-2) 12%, transparent); font-size: 12px; color: var(--accent-2); }
  .style-row { display: flex; gap: 8px; }
  .style-row input[type="color"] { padding: 2px; width: 44px; }
  .config-edit { flex: 1; margin: 8px; border-radius: 10px; resize: none; font-size: 12px; }
  .hosts { padding: 8px 12px; overflow: auto; }
  .host-table { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 8px; }
  .host-table th { text-align: left; color: var(--muted); font-weight: 500; padding: 4px 8px; }
  .host-table td { padding: 4px 8px; border-top: 1px solid var(--stroke); }
  .host-table span.ok { color: var(--accent); }
  .host-table span:not(.ok) { color: var(--danger); }
  .host-form { display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap; }
  .host-form input { flex: 1; min-width: 120px; }
  .modal.wide { width: 560px; max-height: 70vh; overflow: auto; }
  .modal-back.fixed { position: fixed; z-index: 60; }
  /* Small, dim and non-interactive: readable next to its port, invisible as a
     mass when the canvas is busy. The halo keeps it legible over a wire. */
  .rubber { fill: none; stroke: var(--accent); stroke-width: 2; stroke-dasharray: 5 4;
    pointer-events: none; opacity: .9; }
  .iflabel { font: 9px ui-monospace, SFMono-Regular, Menlo, monospace; fill: var(--muted);
    paint-order: stroke; stroke: var(--halo); stroke-width: 3px; stroke-linejoin: round;
    pointer-events: none; user-select: none; }
  .applied { margin: 6px 0 0; padding-left: 16px; color: var(--muted); }
  .applied li { margin: 1px 0; }
  .gear { font-size: 13px; padding: 4px 8px; line-height: 1; }
  .flag.on { color: var(--danger); border-color: var(--danger); }
  .flag sup { font-size: 9px; margin-left: 2px; color: var(--warn); }
  .annotate-veil { position: fixed; inset: 0; z-index: 90; cursor: crosshair;
    background: color-mix(in srgb, var(--accent) 6%, transparent); }
  .annotate-hint { position: fixed; left: 50%; top: 14px; transform: translateX(-50%);
    background: var(--panel-solid); border: 1px solid var(--accent); color: var(--text);
    padding: 6px 12px; border-radius: 999px; font-size: 12px; box-shadow: var(--shadow); }
  .pin { position: fixed; width: 12px; height: 12px; margin: -6px 0 0 -6px; z-index: 91;
    border-radius: 50%; background: var(--danger); box-shadow: 0 0 0 3px var(--panel-solid); }
  .composer { position: fixed; z-index: 92; width: 330px; padding: 10px;
    background: var(--panel-solid); border: 1px solid var(--stroke);
    border-radius: 12px; box-shadow: var(--shadow); }
  .composer textarea { width: 100%; resize: vertical; font: inherit; font-size: 12px;
    padding: 6px 8px; }
  .composer-el { color: var(--muted); margin-bottom: 5px; word-break: break-all; }
  .composer-ctx { color: var(--muted); margin: 5px 0; line-height: 1.4; }
  .themepick { font-size: 11px; padding: 4px 6px; }
  .palfilter { width: 100%; margin-bottom: 10px; font-size: 12px; }
  table.tc { width: 100%; border-collapse: collapse; margin: 6px 0; font-size: 11px; }
  table.tc th { color: var(--muted); font-weight: 500; text-align: right; padding: 2px 4px; }
  table.tc th.dim { opacity: .4; }
  table.tc td { padding: 2px 4px; }
  table.tc .tc-lbl { color: var(--muted); white-space: nowrap; }
  table.tc input { width: 100%; padding: 3px 6px; text-align: right; }
  table.tc input:disabled { opacity: .35; }
  .netobj { touch-action: none; position: absolute; display: flex; align-items: center; gap: 8px;
    padding: 8px 10px; border-radius: 999px; border: 1px dashed var(--seg);
    background: linear-gradient(180deg, var(--node-a), var(--node-b));
    box-shadow: var(--shadow); user-select: none; min-width: 150px; }
  .netobj.cloud { border-color: var(--cloud); border-style: solid; }
  .netobj.wire-target { outline: 2px dashed var(--accent); outline-offset: 3px; }
  .netname strong { font-size: 12px; }
  .netcap { margin-left: auto; padding: 0 5px; font-size: 10px; opacity: .5; }
  .netcap:hover { opacity: 1; color: var(--accent); }
  .netdel { padding: 0 5px; font-size: 10px; opacity: .5; }
  .netdel:hover { opacity: 1; }
  .danger-text { color: var(--danger); }
  .winlayer { position: fixed; inset: 0; pointer-events: none; z-index: 30; }
  .winlayer :global(.floatwin) { pointer-events: auto; }
  .dl { color: var(--accent); }
  .ready { color: var(--accent); opacity: .8; }
  .cold { color: var(--muted); }
  .cold .pull {
    margin-left: 6px;
    padding: 1px 6px;
    border: 1px solid var(--stroke);
    border-radius: 3px;
    background: transparent;
    color: var(--accent);
    cursor: pointer;
  }
  .cold .pull:hover { background: var(--stroke); color: var(--fg); }
  .bar { height: 3px; border-radius: 2px; background: var(--stroke); margin-top: 4px; overflow: hidden; }
  .bar i { display: block; height: 100%; background: var(--accent); transition: width .4s linear; }
  .bar.indet i { width: 40%; animation: slide 1.2s ease-in-out infinite; }
  @keyframes slide { 0% { margin-left: -40%; } 100% { margin-left: 100%; } }
  .wire-handle { display: inline-flex; align-items: center; line-height: 1; padding: 2px 4px; border-radius: 6px; cursor: crosshair; opacity: .55; color: var(--accent); }
  .wire-handle:hover { opacity: 1; }
  .node.wire-target { outline: 2px dashed var(--accent); outline-offset: 3px; }
  /* While a wire is in flight: where it came from, where it may go, and what
     is not available. Dimming the rest is what makes the legal ones read as a
     set rather than as the same chips they were a moment ago. */
  .port.wire-src { background: var(--accent); color: var(--panel-solid); border-color: var(--accent); }
  .port.wire-ok {
    border-color: var(--accent); color: var(--accent);
    background: color-mix(in srgb, var(--accent) 15%, transparent);
  }
  .port.wire-no { opacity: 0.35; }
  .modal-back { position: absolute; inset: 0; background: var(--scrim); display: flex; align-items: center; justify-content: center; z-index: 40; }
  .modal { background: var(--panel); border: 1px solid var(--stroke); border-radius: 14px; padding: 18px 20px; width: 380px; box-shadow: var(--shadow); }
  .modal h3 { margin: 0 0 6px; }
  .pick { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin: 10px 0; }
  .pick span { font-size: 12px; color: var(--muted); }
  .pick select { flex: 1; max-width: 200px; }
  /* pick-tall: for a multi-line field (textarea). The label sits above
     instead of alongside so the textarea gets full width. */
  .pick.pick-tall { flex-direction: column; align-items: stretch; }
  .pick.pick-tall > span { margin-bottom: 4px; }
  .pick textarea { width: 100%; font-family: "IBM Plex Mono", monospace; font-size: 12px; padding: 6px; resize: vertical; }
  /* companion-row: current-file chip on the left, Upload button on the
     right. Chip has an inline unlink X. */
  .companion-row { display: flex; align-items: center; gap: 8px; flex: 1; justify-content: flex-end; }
  .companion-chip {
    display: inline-flex; align-items: center; gap: 4px;
    padding: 2px 4px 2px 6px; border-radius: 3px;
    background: color-mix(in srgb, var(--accent, #22c55e) 10%, transparent);
    max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .modal-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 14px; }
  .vnc { display: flex; flex-direction: column; height: 100%; }
  .vnc-canvas { flex: 1; overflow: auto; background: var(--screen); display: flex; align-items: center; justify-content: center; }
  .vnc-canvas :global(canvas) { image-rendering: pixelated; cursor: crosshair; }
  .vnc-canvas:focus { outline: 1px solid var(--accent); }
  .console-hd button.tiny { padding: 2px 8px; }
  /* Flex, not grid: a hidden panel simply stops taking space. With
     grid-template-columns the column count is fixed, so display:none on the
     palette dropped <main> into the leftover zero-width column. */
  .body { flex: 1; display: flex; min-height: 0; position: relative; }
  .body > .palette { flex: 0 0 240px; }
  .body > .inspector { flex: 0 0 300px; }
  .body > main.canvas { flex: 1 1 auto; min-width: 0; }
  aside[hidden] { display: none; }
  .collapse {
    position: absolute; top: 6px; z-index: 5; width: 18px; height: 20px; padding: 0;
    border: 1px solid var(--stroke); border-radius: 5px; background: var(--panel);
    color: var(--muted); font-size: 11px; line-height: 1; cursor: pointer;
  }
  .palette .collapse { right: 6px; }
  .collapse.right { left: 6px; right: auto; }
  .collapse:hover { color: var(--accent); border-color: var(--accent); }
  .rail {
    position: absolute; top: 8px; z-index: 6; width: 16px; height: 34px; padding: 0;
    border: 1px solid var(--stroke); background: var(--panel); color: var(--muted);
    font-size: 12px; cursor: pointer;
  }
  .rail.left { left: 0; border-radius: 0 6px 6px 0; }
  .rail.right { right: 0; border-radius: 6px 0 0 6px; }
  .rail:hover { color: var(--accent); border-color: var(--accent); }
  .palette, .inspector { padding: 16px; border-right: 1px solid var(--stroke); background: var(--panel); overflow: auto; min-width: 0; }
  .inspector { border-right: 0; border-left: 1px solid var(--stroke); }
  .insp-head { display: flex; align-items: center; gap: 8px; }
  .insp-name {
    margin: 0; font-size: 15px; font-weight: 600; text-transform: none;
    letter-spacing: 0; color: var(--text); flex: 1;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  /* State is a chip, so it reads as a fact about the node rather than one
     more thing you could press. */
  .state-chip {
    display: inline-flex; align-items: center; height: 22px; padding: 0 8px;
    border-radius: 999px; font-size: 11.5px; font-weight: 500;
    color: var(--muted); border: 1px solid var(--stroke);
  }
  .state-chip.on {
    color: var(--accent);
    border-color: color-mix(in srgb, var(--accent) 45%, transparent);
  }
  .state-chip.bad {
    color: var(--danger);
    border-color: color-mix(in srgb, var(--danger) 45%, transparent);
  }
  .dim { color: var(--muted); }
  .cold { color: var(--warn); }
  .ifaces, .nodelist, .imglist { list-style: none; margin: 6px 0; padding: 0; }
  .ifaces li, .imglist li {
    display: flex; align-items: center; gap: 7px;
    min-height: 30px; padding: 0 8px; border-radius: var(--r-tab);
    font-size: 12px;
  }
  .ifaces li:hover, .imglist li:hover { background: var(--raise); }
  .imglist li { justify-content: space-between; }
  .peer { font-weight: 500; }
  /* 38px rows, the design's list rhythm — big enough to be a target, small
     enough that eight nodes fit without scrolling. */
  .node-row {
    display: flex; align-items: center; gap: 9px; width: 100%;
    min-height: 38px; padding: 0 10px; border-radius: var(--r-tab);
    background: transparent; border-color: transparent; text-align: left;
    font-weight: 400;
  }
  .node-row:hover { background: var(--raise); }
  .node-row .dot {
    width: 7px; height: 7px; border-radius: 50%; flex: 0 0 7px;
    background: var(--muted);
  }
  .node-row .dot.on { background: var(--accent); }
  .node-row .dot.fail { background: var(--danger); }
  .opt { display: flex; align-items: center; gap: 8px; margin: 8px 0 2px; font-size: 12px; }
  .opt input { min-height: 0; }
  h3 { margin: 16px 0 8px; font-size: 11px; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); }
  h3:first-child { margin-top: 0; }
  .hint { color: var(--muted); font-size: 13px; }
  .kind { display: flex; gap: 10px; align-items: center; padding: 10px; margin: 8px 0; border: 1px solid var(--stroke); border-radius: 12px; background: var(--raise); cursor: grab; position: relative; }
  .kind:hover { border-color: var(--c); }
  .tmpl-edit {
    position: absolute; top: 6px; right: 6px;
    background: transparent; border: none; color: var(--muted);
    padding: 2px 6px; border-radius: 6px; font-size: 12px; cursor: pointer;
    opacity: 0; transition: opacity 120ms ease;
  }
  .kind:hover .tmpl-edit { opacity: 1; }
  .tmpl-edit:hover { color: var(--text); background: var(--bg-2); }
  .glyph { width: 32px; height: 32px; display: grid; place-items: center; border-radius: 8px; background: color-mix(in srgb, var(--c) 20%, transparent); color: var(--c); }
  .tiny { font-size: 11px; color: var(--muted); }
  .legend { margin-top: 18px; color: var(--muted); font-size: 12px; display: grid; gap: 6px; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 99px; margin-right: 6px; background: var(--muted); }
  .dot.run { background: var(--accent); }
  .dot.fail { background: var(--danger); }
  .dot.impair { background: var(--warn); }
  .canvas.panning { cursor: grab; }
  .canvas { touch-action: none; position: relative; overflow: hidden; background-image:
    linear-gradient(var(--grid) 1px, transparent 1px),
    linear-gradient(90deg, var(--grid) 1px, transparent 1px);
    background-size: 24px 24px; }
  .canvas.nogrid { background-image: none; }
  .grid { position: absolute; transform-origin: 0 0; width: 4000px; height: 3000px; }
  .wires { position: absolute; inset: 0; }
  /* Thinner and calmer: a 3px glowing cable per link turned a dense lab into
     soup. The glow is kept for the selected one, where it means something. */
  .link { fill: none; stroke: var(--wire); stroke-width: 1.75; stroke-linecap: round; opacity: .85; pointer-events: none; transition: stroke .15s, opacity .15s; }
  .link.seg { stroke: var(--seg); stroke-width: 1.5; opacity: .6; stroke-dasharray: 1 5; }
  .link-hit { fill: none; stroke: transparent; stroke-width: 14; cursor: pointer; }
  .link-hit:hover + .link { opacity: 1; stroke-width: 2.5; }
  .link.sel { stroke: var(--text); stroke-width: 2.75; opacity: 1; filter: drop-shadow(0 0 6px var(--accent)); }
  .link.impaired { stroke: var(--warn); }
  .link.down { stroke-dasharray: 6 6; stroke: var(--danger); }
  /* A flat card, not a gradient with a glow around it. The old treatment —
     176px, 16px radius, a 24px coloured halo on every running node — is most
     of why a dense lab looked like a light show rather than a diagram. */
  .node {
    touch-action: none; position: absolute; width: 150px; min-height: 76px;
    border-radius: var(--r-node); border: 1px solid var(--stroke);
    background: var(--node-a); padding: 10px 12px; user-select: none;
  }
  .node.sel { border-color: var(--c); box-shadow: 0 0 0 1px var(--c); }
  /* Softer treatment than .sel — a link's endpoints get a coloured
     outline (not a full "you selected me" chrome swap). Enough to
     read "these are the two ends" at a glance, not so much that it
     competes with a real primary selection. */
  .node.hilite { box-shadow: 0 0 0 2px var(--accent); }
  .netobj.hilite { box-shadow: 0 0 0 2px var(--accent); }
  /* Running is shown by the status dot, which is already there. */
  .node.running { border-color: color-mix(in srgb, var(--c) 55%, var(--stroke)); }
  .node.failed { border-color: var(--danger); }
  .node.paused { opacity: 0.6; filter: grayscale(0.4); }
  /* Floats over the canvas, top right, clear of the node the user is most
     likely dragging (which is wherever they last dropped one). */
  .canvas-ctl { position: absolute; right: 14px; top: 14px; display: flex; gap: 6px; z-index: 5; }
  .canvas-ctl button {
    min-height: 30px; padding: 0 10px; font-size: 12px;
    background: var(--panel); color: var(--muted);
  }
  .canvas-ctl button.on { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 45%, transparent); }
  .addr {
    font-family: "IBM Plex Mono", ui-monospace, monospace;
    font-size: 9px; padding: 1px 5px; border-radius: 4px;
    background: color-mix(in srgb, var(--accent) 14%, transparent);
    color: var(--accent);
  }
  .addr.none { background: var(--raise); color: var(--muted); }
  /* Anchored at the pointer, because it is about the thing under it. */
  .linkpop {
    position: fixed; z-index: 40; transform: translate(-50%, 10px);
    display: flex; flex-direction: column; gap: 7px;
    min-width: 260px; padding: 12px 14px;
    border: 1px solid var(--stroke); border-radius: var(--r-card);
    background: var(--panel); box-shadow: var(--shadow);
  }
  .linkpop-hd { display: flex; align-items: center; gap: 6px; font-size: 12.5px; font-weight: 600; }
  .port.static { pointer-events: none; }
  .linkpop-acts { display: flex; gap: 7px; margin-top: 2px; }
  .linkpop-acts button { min-height: 30px; padding: 0 11px; font-size: 12px; }
  .warn-t { color: var(--warn); }
  .node-hd { display: flex; gap: 7px; align-items: center; }
  .node-id { min-width: 0; flex: 1; }
  .node-id strong { font-size: 12.5px; font-weight: 600; }
  .node .meta {
    color: var(--muted); font-size: 10px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .ndot { width: 7px; height: 7px; border-radius: 50%; flex: 0 0 7px; background: var(--muted); }
  .ndot.on { background: var(--accent); }
  .ndot.fail { background: var(--danger); }
  .node-console {
    min-height: 0; padding: 2px 5px; font-size: 10px; line-height: 1;
    background: transparent; border-color: transparent; color: var(--muted);
    font-family: "IBM Plex Mono", ui-monospace, monospace;
  }
  .node-console:hover { color: var(--accent); border-color: var(--stroke); }
  .ports { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 8px; }
  .port {
    font-family: "IBM Plex Mono", ui-monospace, monospace;
    font-size: 9px; padding: 1px 5px; border-radius: 4px;
    background: var(--bg); color: var(--muted);
    border: 1px solid color-mix(in srgb, var(--stroke) 70%, transparent);
  }
  .empty { position: absolute; left: 80px; top: 80px; color: var(--muted); }
  .busy { position: absolute; right: 16px; bottom: 16px; background: var(--panel); padding: 8px 12px; border-radius: 10px; }
  dl { display: grid; grid-template-columns: 70px 1fr; gap: 6px 10px; font-size: 13px; }
  dl.tune { grid-template-columns: 90px 1fr; font-size: 11px; }
  dt { color: var(--muted); overflow: hidden; text-overflow: ellipsis; }
  .acts { display: grid; gap: 8px; margin-top: 16px; }
  .err { color: var(--danger); }
  .docks { height: 300px; border-top: 1px solid var(--stroke); background: var(--bg); display: flex; flex-direction: column; overflow: hidden; transition: height 120ms ease; }
  .docks.min { height: 34px; }
  .dock-min { padding: 0 8px; font-size: 14px; }
  .inspector-in-dock { overflow-y: auto; padding: 8px 12px; flex: 1; }
  .termtabs { display: flex; gap: 4px; align-items: center; min-width: 0; overflow-x: auto; }
  .termtab {
    display: flex; align-items: center; gap: 7px; min-height: 28px;
    padding: 0 9px; border-radius: var(--r-tab); border-color: transparent;
    background: transparent; color: var(--muted); font-size: 12.5px; font-weight: 500;
  }
  .termtab.on { background: var(--bg-2); color: var(--text); }
  .termtab.add { padding: 0 10px; font-size: 15px; }
  .tbadge {
    font-size: 10px; padding: 1px 6px; border-radius: 999px;
    background: var(--raise); color: var(--muted);
  }
  .tclose { opacity: 0; font-size: 10px; cursor: pointer; }
  .termtab:hover .tclose { opacity: 0.65; }
  .tclose:hover { opacity: 1; color: var(--danger); }
  /* Two panes are the useful maximum: the point is to watch one node while
     driving another, and a third column makes both too narrow to read. */
  .termgrid { flex: 1; display: grid; grid-template-columns: 1fr; min-height: 0; gap: 8px; padding: 8px; }
  .termgrid.split { grid-template-columns: 1fr 1fr; }
  .termpane {
    display: flex; flex-direction: column; min-width: 0; min-height: 0;
    border: 1px solid var(--stroke); border-radius: var(--r-card);
    background: var(--panel); overflow: hidden;
  }
  .termhd {
    display: flex; align-items: center; gap: 8px; padding: 0 10px;
    min-height: 30px; border-bottom: 1px solid var(--stroke); font-size: 12px;
  }
  .termhd strong { font-weight: 600; }
  .termhd .tiny { font-size: 10.5px; }
  .cmdrow {
    display: flex; align-items: center; gap: 8px;
    padding: 8px; border-top: 1px solid var(--stroke); min-height: 50px;
  }
  .runon { font-weight: 400; color: var(--muted); white-space: nowrap; }
  .runon strong { color: var(--text); font-weight: 600; }
  .cmdform {
    flex: 1; display: flex; align-items: center; gap: 8px; min-width: 120px;
    min-height: var(--ctl-h); padding: 0 12px;
    border: 1px solid var(--stroke); border-radius: var(--r-ctl); background: var(--bg);
  }
  .cmdform input { flex: 1; min-width: 0; border: 0; background: transparent; padding: 0; }
  .prompt { color: var(--accent); }
  .keyhint {
    font-size: 10.5px; color: var(--muted); white-space: nowrap;
    padding: 1px 6px; border: 1px solid var(--stroke); border-radius: 5px;
  }
  .quick { display: flex; gap: 6px; overflow-x: auto; }
  .qchip {
    min-height: 26px; padding: 0 9px; font-size: 11px; font-weight: 400;
    color: var(--muted); background: var(--raise); border-color: transparent;
    white-space: nowrap;
  }
  .qchip:hover { color: var(--text); border-color: var(--stroke); }
  .termempty { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 4px; }
  .tabs { display: flex; gap: 4px; padding: 6px 10px; border-bottom: 1px solid var(--stroke); }
  /* An active tab is a raised surface, the way a tab behaves physically —
     the old accent-tinted fill made every dock look like it held an alert. */
  .tabs button {
    min-height: 28px; padding: 0 12px; font-size: 12.5px; font-weight: 500;
    border-radius: var(--r-tab); border-color: transparent;
    background: transparent; color: var(--muted);
  }
  .tabs button:hover { color: var(--text); }
  .tabs button.on { background: var(--bg-2); color: var(--text); }
  /* Compact context tag next to a tab label (Inspector's "node" / "link" /
     node-count chip; also the pattern Events' error count uses). Never
     grows the tab beyond its natural label width. */
  .tabs button .ctx-chip {
    display: inline-block; margin-left: 6px;
    font-size: 10px; line-height: 1; padding: 2px 6px;
    border-radius: 999px;
    background: color-mix(in srgb, var(--muted) 20%, transparent);
    color: var(--muted);
    vertical-align: middle;
  }
  .tabs button.on .ctx-chip {
    background: color-mix(in srgb, var(--accent, #22c55e) 20%, transparent);
    color: var(--accent, #22c55e);
  }
  .ai, .packets, .console { flex: 1; display: flex; flex-direction: column; min-height: 0; }
  .chat { flex: 1; overflow: auto; padding: 8px 12px; }
  .strip-run {
    display: flex; align-items: center; gap: 7px;
    padding: 5px 12px; font-size: 11.5px; color: var(--accent);
    border-bottom: 1px solid var(--stroke);
  }
  .strip-run .cdot {
    width: 6px; height: 6px; border-radius: 50%; background: currentColor;
    animation: pulse 1.1s ease-in-out infinite;
  }
  @keyframes pulse { 50% { opacity: 0.25; } }
  .bubble { margin: 8px 0; }
  .bubble.you { color: var(--accent); }
  .bubble pre { white-space: pre-wrap; margin: 4px 0 0; font-size: 12px; font-family: "IBM Plex Mono", monospace; }
  .ai form, .console form, .pkt-bar { display: flex; gap: 8px; padding: 8px; }
  .ai form input, .pkt-bar input, .console input { flex: 1; }
  .console-hd { display: flex; justify-content: space-between; padding: 8px 12px; color: var(--muted); font-size: 12px; }
  pre { flex: 1; margin: 0; padding: 8px 12px; overflow: auto; font-family: "IBM Plex Mono", monospace; font-size: 12px; white-space: pre-wrap; }
  .templates-header { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
  .templates-header h3 { margin: 0; }
  .templates-header button.tiny { font-size: 11px; padding: 2px 6px; }
</style>
