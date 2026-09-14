<script>
  import Guacamole from "guacamole-common-js";
  import { onDestroy } from "svelte";

  // One Guacamole session, scaled to whatever the window is. Owning the client
  // here rather than in App means several screens can be open at once, and a
  // closed window actually disconnects instead of leaking a tunnel.
  let { node, protocol = "vnc", onstatus } = $props();

  let holder = $state(null);
  let client = null;
  let keyboard = null;
  let mouse = null;
  let display = null;
  let observer = null;

  const STATES = ["idle", "connecting", "waiting", "connected", "disconnecting", "disconnected"];

  function fit() {
    if (!display || !holder) return;
    const w = display.getWidth();
    const h = display.getHeight();
    if (!w || !h) return;
    // Never scale up past 1:1 — a magnified framebuffer is just blurry.
    const scale = Math.min(holder.clientWidth / w, holder.clientHeight / h, 1);
    display.scale(scale);
  }

  function connect() {
    disconnect();
    onstatus?.("connecting");
    const rect = holder.getBoundingClientRect();
    const q = new URLSearchParams({
      width: String(Math.max(640, Math.round(rect.width))),
      height: String(Math.max(480, Math.round(rect.height))),
      dpi: String(Math.round(96 * (window.devicePixelRatio || 1))),
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "",
    });
    const proto = location.protocol === "https:" ? "wss" : "ws";
    // A Wireshark session is not a node, so it hangs off its own path; the
    // tunnel either side of it is identical.
    const path =
      protocol === "wireshark"
        ? `/api/v1/wireshark/${node.id}/ws`
        : `/api/v1/nodes/${node.id}/${protocol}/ws`;
    const tunnel = new Guacamole.WebSocketTunnel(`${proto}://${location.host}${path}?${q}`);
    tunnel.onerror = (s) => onstatus?.(`tunnel error: ${s?.message || s?.code || "unknown"}`);

    client = new Guacamole.Client(tunnel);
    client.onstatechange = (s) => {
      onstatus?.(STATES[s] || String(s));
      if (STATES[s] === "connected") requestAnimationFrame(fit);
    };
    client.onerror = (e) => onstatus?.(`error: ${e?.message || "unknown"}`);

    display = client.getDisplay();
    holder.innerHTML = "";
    holder.appendChild(display.getElement());
    // The framebuffer size is only known once the guest announces it, and it
    // changes if the guest re-resolutions mid-session.
    display.onresize = fit;
    client.connect("");

    mouse = new Guacamole.Mouse(display.getElement());
    // The second argument is applyDisplayScale. Guacamole.Mouse reports
    // positions as `clientX - element.offsetLeft`, which is layout
    // arithmetic — and Display.scale() scales by CSS transform, which
    // layout offsets do not see. Sending the raw state put the guest
    // pointer at browser coordinates on a framebuffer drawn at a
    // different size, so it tracked the real cursor with an error that
    // grew the further you moved from the top-left corner.
    const send = (e) => client.sendMouseState(e.state ?? e, true);
    mouse.onmousedown = mouse.onmouseup = mouse.onmousemove = send;

    keyboard = new Guacamole.Keyboard(holder);
    keyboard.onkeydown = (k) => client.sendKeyEvent(1, k);
    keyboard.onkeyup = (k) => client.sendKeyEvent(0, k);

    observer = new ResizeObserver(fit);
    observer.observe(holder);
  }

  function disconnect() {
    observer?.disconnect();
    observer = null;
    if (mouse) {
      mouse.onmousedown = mouse.onmouseup = mouse.onmousemove = null;
      mouse = null;
    }
    if (keyboard) {
      keyboard.onkeydown = keyboard.onkeyup = null;
      keyboard = null;
    }
    try {
      client?.disconnect();
    } catch {
      /* already gone */
    }
    client = null;
    display = null;
  }

  //: X11 keysyms. The browser keeps Ctrl+Alt+F2 for itself — the guest never
  //: sees it — so a VTY switch is unreachable without a way to synthesise the
  //: chord and send it down the wire directly.
  const CTRL = 0xffe3;
  const ALT = 0xffe9;
  const SHIFT = 0xffe1;
  const KEYS = {
    Del: 0xffff,
    Esc: 0xff1b,
    Tab: 0xff09,
    Enter: 0xff0d,
    ...Object.fromEntries(Array.from({ length: 12 }, (_, i) => [`F${i + 1}`, 0xffbe + i])),
  };

  function chord(...keysyms) {
    if (!client) return;
    for (const k of keysyms) client.sendKeyEvent(1, k);
    for (const k of [...keysyms].reverse()) client.sendKeyEvent(0, k);
    holder?.focus();
  }

  let showKeys = $state(false);
  //: The handful worth a button. Everything else is typeable.
  const COMBOS = [
    { label: "Ctrl+Alt+Del", keys: [CTRL, ALT, KEYS.Del] },
    ...Array.from({ length: 6 }, (_, i) => ({
      label: `C+A+F${i + 1}`,
      keys: [CTRL, ALT, KEYS[`F${i + 1}`]],
      title: `switch to virtual terminal ${i + 1}`,
    })),
    { label: "Esc", keys: [KEYS.Esc] },
    { label: "Ctrl+Alt+T", keys: [CTRL, ALT, 0x0074], title: "GNOME: open a terminal" },
    { label: "Alt+Tab", keys: [ALT, KEYS.Tab] },
    { label: "Ctrl+Shift+Esc", keys: [CTRL, SHIFT, KEYS.Esc] },
  ];

  $effect(() => {
    if (holder && node?.id) connect();
  });

  onDestroy(disconnect);
</script>

<div class="vncwrap">
  <div class="keybar">
    <button
      class="keytoggle"
      title="send key combinations the browser intercepts"
      onclick={() => (showKeys = !showKeys)}>⌨</button
    >
    {#if showKeys}
      {#each COMBOS as c}
        <button title={c.title || `send ${c.label}`} onclick={() => chord(...c.keys)}>{c.label}</button>
      {/each}
    {/if}
  </div>
  <!-- tabindex so the keyboard handler can actually receive keys -->
  <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
  <div class="vncpane" tabindex="0" bind:this={holder} onclick={() => holder?.focus()}></div>
</div>

<style>
  .vncwrap { flex: 1; min-width: 0; display: flex; flex-direction: column; }
  .keybar {
    display: flex; flex-wrap: wrap; gap: 4px; padding: 4px 6px;
    border-bottom: 1px solid var(--stroke); background: var(--panel);
  }
  .keybar button {
    padding: 2px 7px; font-size: 10px; border-radius: 5px;
    border: 1px solid var(--stroke); background: transparent; color: var(--muted);
    cursor: pointer; white-space: nowrap;
  }
  .keybar button:hover { color: var(--accent); border-color: var(--accent); }
  .keybar .keytoggle { font-size: 12px; }
  .vncpane {
    flex: 1;
    min-width: 0;
    background: var(--screen);
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    outline: none;
  }
  .vncpane :global(canvas) {
    image-rendering: pixelated;
    cursor: crosshair;
  }
  .vncpane:focus-within {
    box-shadow: inset 0 0 0 1px var(--accent);
  }
</style>
