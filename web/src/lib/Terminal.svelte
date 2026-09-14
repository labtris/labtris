<script>
  //: A real terminal, not a <pre> with text appended to it.
  //:
  //: The previous pane concatenated bytes into a text node, which works right
  //: up until something repaints: vi, top, a curses installer, or a shell that
  //: colours its own prompt. Those arrive as escape sequences, and a <pre>
  //: renders them as the literal characters "\x1b[31m". xterm.js is the
  //: emulator that turns them back into a screen.
  //:
  //: It also brings the things people expect and notice the absence of:
  //: selection, copy on select, a scrollback you can search, and a cursor that
  //: is where the guest thinks it is.
  import { onDestroy, onMount, tick } from "svelte";
  import { Terminal } from "@xterm/xterm";
  import { FitAddon } from "@xterm/addon-fit";
  import "@xterm/xterm/css/xterm.css";

  let { node, onstatus, onstart } = $props();

  const running = $derived(node?.state === "running");

  let host = $state(null);
  let term = null;
  let fit = null;
  let ws = null;
  let attachedTo = null;
  let resizeObs = null;

  //: "connecting" | "open" | "dropped" | "refused" | "stopped"
  let status = $state("connecting");
  let refusal = $state("");
  let retryIn = $state(0);
  let retryTimer = null;
  let attempts = 0;
  let openedAt = 0;

  const MAX_TRIES = 5;

  //: Colours come from the page's own tokens, so the terminal belongs to the
  //: theme rather than being a black rectangle sitting in it.
  function palette() {
    const css = getComputedStyle(document.documentElement);
    const pick = (name, fallback) => (css.getPropertyValue(name) || fallback).trim();
    return {
      background: pick("--bg", "#12181b"),
      foreground: pick("--text", "#e4e9ec"),
      cursor: pick("--accent", "#00c0ef"),
      selectionBackground: "rgba(0,192,239,.25)",
    };
  }

  function closeSocket() {
    clearInterval(retryTimer);
    retryTimer = null;
    retryIn = 0;
    attempts = 0;
    refusal = "";
    const old = ws;
    ws = null;
    attachedTo = null;
    //: Cleared before close, or onclose reads its own teardown as the guest
    //: going away and starts reconnecting to a node we are leaving.
    old?.close();
  }

  function sendResize() {
    if (!term || !ws || ws.readyState !== 1) return;
    ws.send(JSON.stringify({ resize: true, cols: term.cols, rows: term.rows }));
  }

  function connect(id) {
    closeSocket();
    attachedTo = id;
    status = "connecting";
    onstatus?.("connecting");
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const sock = new WebSocket(`${proto}://${location.host}/api/v1/nodes/${id}/console/ws`);
    sock.binaryType = "arraybuffer";
    ws = sock;

    sock.onopen = () => {
      if (ws !== sock) return;
      openedAt = Date.now();
      status = "open";
      onstatus?.("connected");
      //: Tell the PTY our size immediately: it starts at the server's default,
      //: and a shell that thinks it is 80 columns wraps every longer line.
      sendResize();
      term?.focus();
    };
    sock.onmessage = (ev) => {
      if (ws !== sock) return;
      attempts = 0;
      term?.write(
        typeof ev.data === "string" ? ev.data : new Uint8Array(ev.data),
      );
    };
    sock.onclose = (ev) => {
      if (ws !== sock) return;
      onstatus?.("closed");
      const lived = openedAt ? Date.now() - openedAt : 0;
      //: 4404 is the server saying there is nothing to attach to. Anything that
      //: dies within a second and a half never carried a session either, and
      //: retrying a refusal is a loop that changes nothing.
      if (ev?.code === 4404 || lived < 1500) {
        refused(ev?.reason || `${node?.name} has no console to attach to yet`);
        return;
      }
      dropped();
    };
  }

  function refused(why) {
    ws = null;
    attachedTo = null;
    clearInterval(retryTimer);
    retryTimer = null;
    retryIn = 0;
    refusal = why;
    status = running ? "refused" : "stopped";
  }

  function dropped() {
    ws = null;
    attachedTo = null;
    if (!running) {
      status = "stopped";
      return;
    }
    status = "dropped";
    const at = new Date().toTimeString().slice(0, 8);
    //: Written into the scrollback rather than shown beside it, so the output
    //: above stays readable as a record of what happened before the break.
    term?.write(`\r\n\x1b[33m── connection dropped ${at} · output above is kept ──\x1b[0m\r\n`);
    if (attempts >= MAX_TRIES) {
      refused("gave up after 5 attempts — the console keeps closing");
      return;
    }
    retryIn = Math.min(30, 3 * 2 ** attempts);
    retryTimer = setInterval(() => {
      retryIn -= 1;
      if (retryIn <= 0) reconnect();
    }, 1000);
  }

  function reconnect() {
    attempts += 1;
    if (node?.id) connect(node.id);
  }

  export function retryNow() {
    attempts = 0;
    refusal = "";
    reconnect();
  }

  export function send(text) {
    if (!ws || ws.readyState !== 1) return false;
    //: A carriage return, not a newline: that is what Enter sends on a tty, and
    //: a shell reading \n alone waits for the rest of the line forever.
    ws.send(text + "\r");
    return true;
  }

  export function focus() {
    term?.focus();
  }

  onMount(async () => {
    term = new Terminal({
      fontFamily: '"IBM Plex Mono", ui-monospace, monospace',
      fontSize: 12.5,
      lineHeight: 1.25,
      cursorBlink: true,
      scrollback: 5000,
      theme: palette(),
      //: Copy on select is what people expect from a terminal and notice the
      //: absence of within a minute.
      rightClickSelectsWord: true,
    });
    fit = new FitAddon();
    term.loadAddon(fit);
    await tick();
    term.open(host);
    fit.fit();

    //: Straight down the socket, unechoed. What appears is the guest's
    //: decision — which is how a password prompt is supposed to behave.
    term.onData((d) => {
      if (ws && ws.readyState === 1) ws.send(d);
      else if (status === "dropped" || status === "refused") retryNow();
    });

    resizeObs = new ResizeObserver(() => {
      try {
        fit.fit();
        sendResize();
      } catch {
        //: Fitting a pane that is momentarily zero-sized throws; the next
        //: observation will be a real one.
      }
    });
    resizeObs.observe(host);
  });

  $effect(() => {
    const id = node?.id;
    const up = running;
    if (!id || !term) return;
    if (attachedTo && attachedTo !== id) {
      closeSocket();
      term.reset();
    }
    if (up && attachedTo !== id) connect(id);
    if (!up) {
      closeSocket();
      status = "stopped";
    }
  });

  onDestroy(() => {
    closeSocket();
    resizeObs?.disconnect();
    term?.dispose();
  });
</script>

<div class="tpane">
  {#if status === "stopped"}
    <div class="state">
      <div class="state-title">{node?.name} is not running.</div>
      <div class="state-body">
        {#if refusal}
          {refusal}
        {:else}
          Nothing is attached to its console. Starting it here attaches as soon
          as the guest writes its first line.
        {/if}
      </div>
      <div class="state-acts">
        <button class="primary" onclick={() => onstart?.()}>Start and attach</button>
      </div>
    </div>
  {:else}
    {#if status === "connecting"}
      <div class="strip">
        Connecting to {node?.name} — the console attaches as soon as the guest
        writes to it, which for a booting VM is its first kernel line.
      </div>
    {:else if status === "dropped"}
      <div class="strip warn">
        Connection lost. Reconnecting in {retryIn}s — or press any key.
        <button class="tiny" onclick={retryNow}>Reconnect now</button>
      </div>
    {:else if status === "refused"}
      <div class="strip warn">
        {refusal}
        <button class="tiny" onclick={retryNow}>Try again</button>
      </div>
    {/if}
    <div class="screen" bind:this={host}></div>
  {/if}
</div>

<style>
  .tpane { flex: 1; display: flex; flex-direction: column; min-width: 0; min-height: 0; }
  .screen { flex: 1; min-height: 0; padding: 6px 8px; overflow: hidden; }
  .screen :global(.xterm) { height: 100%; }
  .screen :global(.xterm-viewport) { background: transparent !important; }
  .strip {
    display: flex; align-items: center; gap: 8px;
    padding: 6px 9px; font-size: 11px; color: var(--muted);
    border-bottom: 1px solid var(--stroke);
  }
  .strip.warn { color: var(--warn, #f5b74e); }
  .strip button { margin-left: auto; }
  .state {
    flex: 1; display: flex; flex-direction: column;
    align-items: center; justify-content: center; gap: 9px;
    padding: 20px; text-align: center;
  }
  .state-title { font-size: 14px; font-weight: 600; }
  .state-body { font-size: 12px; color: var(--muted); max-width: 340px; line-height: 1.45; }
  .state-acts { display: flex; gap: 8px; margin-top: 3px; }
</style>
