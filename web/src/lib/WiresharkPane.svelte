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
    } catch (e) {
      error = e.message;
      onstatus?.("failed");
    }
  }

  $effect(() => {
    if (target?.id && !session && !error) begin();
  });

  onDestroy(() => {
    // Each session is a whole X server and Wireshark process; closing the
    // window has to actually reclaim it.
    if (session) api.wiresharkStop(session.id).catch(() => {});
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
{:else}
  <div class="wsmsg"><p class="tiny">starting Wireshark on the lab host…</p></div>
{/if}

<style>
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
