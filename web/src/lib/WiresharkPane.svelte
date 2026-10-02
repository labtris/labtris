<script>
  import { onDestroy } from "svelte";
  import { api } from "./api.js";
  import VncPane from "./VncPane.svelte";

  // The real Wireshark, running on the lab host against a lab interface and
  // shown over the same Guacamole path as the QEMU consoles. Not a pcap
  // download and not a reimplementation: the actual binary, dissectors and
  // filter bar included.
  let { target, onstatus } = $props();

  let session = $state(null);
  let error = $state("");

  //: Wireshark runs on the lab host, so File → Save As writes to the server.
  //: Left at that the file is stranded: it sits in a directory nobody can
  //: reach and nothing cleans up, and the person who clicked Save gets no
  //: hint that it did not land on their machine. So the pane watches the
  //: session's save directory and pulls anything new down itself — a save
  //: arrives in Downloads with no second step.
  let delivered = $state(new Set());
  let lastSaved = $state("");
  let poller = null;

  const POLL_MS = 2000;

  async function deliverNewSaves() {
    if (!session) return;
    let files;
    try {
      ({ files } = await api.wiresharkFiles(session.id));
    } catch {
      return; // session gone, or a blip — the next tick tries again
    }
    for (const f of files) {
      if (delivered.has(f.name)) continue;
      // Marked before the fetch, not after: two ticks can overlap on a slow
      // download and the file would otherwise be handed over twice.
      delivered.add(f.name);
      delivered = delivered;
      try {
        const r = await fetch(api.wiresharkFileUrl(session.id, f.name), {
          credentials: "same-origin",
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const url = URL.createObjectURL(await r.blob());
        const a = document.createElement("a");
        a.href = url;
        a.download = f.name;
        document.body.appendChild(a);
        a.click();
        a.remove();
        // Revoked on a tick, not immediately: Firefox cancels an in-flight
        // download if the object URL goes away under it.
        setTimeout(() => URL.revokeObjectURL(url), 10000);
        lastSaved = f.name;
      } catch {
        delivered.delete(f.name);   // let the next tick retry
        delivered = delivered;
      }
    }
  }

  async function begin() {
    onstatus?.("starting");
    try {
      const body = {};
      body[
        target.kind === "network"
          ? "network_id"
          : target.kind === "link"
            ? "link_id"
            : "interface_id"
      ] = target.id;
      session = await api.wiresharkStart(body);
      onstatus?.(`on ${session.ifname}`);
      poller = setInterval(deliverNewSaves, POLL_MS);
    } catch (e) {
      error = e.message;
      onstatus?.("failed");
    }
  }

  $effect(() => {
    if (target?.id && !session && !error) begin();
  });

  onDestroy(() => {
    if (poller) clearInterval(poller);
    // Each session is a whole X server and Wireshark process; closing the
    // window has to actually reclaim it. One last sweep first, because
    // stopping removes the save directory and a capture saved in the last
    // couple of seconds would go with it.
    const s = session;
    if (!s) return;
    deliverNewSaves()
      .catch(() => {})
      .finally(() => api.wiresharkStop(s.id).catch(() => {}));
  });
</script>

{#if error}
  <div class="wsmsg">
    <p>{error}</p>
    <p class="tiny">
      Needs wireshark, xvfb and x11vnc on the lab host, and dumpcap allowed to capture
      without root (<code>dpkg-reconfigure wireshark-common</code>).
    </p>
  </div>
{:else if session}
  <VncPane
    node={{ id: session.id }}
    protocol="wireshark"
    onstatus={(t) => onstatus?.(t === "connected" ? `on ${session.ifname}` : t)}
  />
  {#if lastSaved}
    <!-- Said out loud because the download happens on its own: without a
         line here, a file quietly appearing in Downloads is a mystery. -->
    <p class="wssaved tiny">Saved <code>{lastSaved}</code> → your downloads</p>
  {/if}
{:else}
  <div class="wsmsg"><p class="tiny">starting Wireshark on the lab host…</p></div>
{/if}

<style>
  .wssaved {
    margin: 0;
    padding: 4px 8px;
    color: var(--muted);
    border-top: 1px solid var(--stroke);
  }
  .wsmsg {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 6px;
    padding: 16px;
    color: var(--muted);
    text-align: center;
  }
</style>
