<script>
  import { onDestroy } from "svelte";
  import { api } from "./api.js";

  // Live tcpdump for one interface or link, in its own window so a capture can
  // keep running while you work on the rest of the lab.
  let { target, onstatus } = $props();

  let lines = $state([]);
  let bpf = $state("");
  let running = $state(false);
  let pre = $state(null);
  let timer = null;

  //: interface | link | network — a network capture watches the whole bridge,
  //: which is every frame the segment floods rather than one port's view.
  const beginCap = $derived(
    { link: api.linkCaptureStart, network: api.netCaptureStart }[target.kind] ??
      api.captureStart,
  );
  const readCap = $derived(
    { link: api.linkCaptureRead, network: api.netCaptureRead }[target.kind] ?? api.captureRead,
  );
  const stopCap = $derived(
    { link: api.linkCaptureStop, network: api.netCaptureStop }[target.kind] ?? api.captureStop,
  );

  async function start() {
    lines = [];
    try {
      await beginCap(target.id, bpf);
      running = true;
      onstatus?.("capturing");
      timer = setInterval(tick, 800);
    } catch (e) {
      onstatus?.(e.message);
    }
  }

  async function tick() {
    try {
      const got = await readCap(target.id);
      if (got?.lines?.length) {
        lines = [...lines, ...got.lines].slice(-600);
        if (pre) pre.scrollTop = pre.scrollHeight;
      }
    } catch {
      /* keep polling */
    }
  }

  async function stop() {
    clearInterval(timer);
    timer = null;
    running = false;
    onstatus?.("stopped");
    try {
      await stopCap(target.id);
    } catch {
      /* already stopped */
    }
  }

  function save() {
    // The pcap itself lives on the host; this is the decoded summary, which is
    // what you actually read here.
    const blob = new Blob([lines.join("\n")], { type: "text/plain" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${target.label || "capture"}.txt`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  onDestroy(() => {
    if (running) stop();
    clearInterval(timer);
  });
</script>

<div class="cap">
  <div class="cap-bar">
    <input class="mono" bind:value={bpf} placeholder="bpf filter, e.g. icmp or tcp port 179" />
    {#if running}
      <button onclick={stop}>Stop</button>
    {:else}
      <button class="primary" onclick={start}>Start</button>
    {/if}
    <button onclick={save} disabled={!lines.length}>Save</button>
  </div>
  <pre bind:this={pre}>{lines.join("\n") || "tcpdump -nn -e -tt on " + (target.label || target.id)}</pre>
</div>

<style>
  .cap {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-width: 0;
  }
  .cap-bar {
    display: flex;
    gap: 6px;
    padding: 6px;
    border-bottom: 1px solid var(--stroke);
  }
  .cap-bar input {
    flex: 1;
  }
  pre {
    flex: 1;
    margin: 0;
    padding: 8px 10px;
    overflow: auto;
    font-size: 11px;
    line-height: 1.4;
    white-space: pre-wrap;
    word-break: break-all;
  }
</style>
