<script>
  import { onMount, onDestroy } from "svelte";
  import { api } from "./api.js";

  //: Ready-hooks tab.
  //:
  //: The lab-level analogue of the template's bootstrap block. Users write
  //: a small YAML doc naming checks to run once the lab is up — ping tests,
  //: HTTP probes, serial-prompt regexes, exec commands inside Docker nodes.
  //: The server parses + persists (`Lab.hooks_source` / `Lab.hooks`), and
  //: the per-lab `HookRunner` auto-fires them once every node is running.

  let { labId, onstatus } = $props();

  let source = $state("");
  let parsed = $state(null);
  let state = $state(null);
  let watcherRunning = $state(false);
  let loading = $state(true);
  let saving = $state(false);
  let running = $state(false);
  let err = $state("");

  const EXAMPLE = `# hooks.yml — fires once every node in the lab is running.
# 'labtris lab hooks apply <lab_id> hooks.yml' does the same thing.
ready_when: all_nodes_running

hooks:
  # - name: "wait for BGP up on R1"
  #   kind: serial_wait
  #   node: R1
  #   wait_for: "% BGP session established"
  #   timeout_s: 60

  # - name: "ping between DC1 and DC2"
  #   kind: ping
  #   from_node: dc1-host
  #   to: 10.20.0.1
  #   count: 3
  #   timeout_s: 10

  # - name: "validate app health"
  #   kind: http
  #   from_node: dc1-host
  #   url: "http://web.dc1:8080/health"
  #   expect_status: 200

  # - name: "check BGP"
  #   kind: command   # Docker only. Use serial_wait for QEMU.
  #   node: dc1-host
  #   command: "curl -s http://192.168.1.1/status"
  #   expect_rc: 0
  #   expect_stdout_regex: "OK"
`;

  async function load() {
    loading = true;
    err = "";
    try {
      const got = await api.hooksGet(labId);
      source = got?.source || "";
      parsed = got?.parsed || null;
      state = got?.state || null;
      watcherRunning = !!got?.watcher_running;
    } catch (e) {
      err = e?.message || String(e);
    } finally {
      loading = false;
    }
  }

  async function apply() {
    saving = true;
    err = "";
    try {
      const got = await api.hooksApply(labId, source);
      parsed = got?.parsed || null;
      state = got?.state || null;
      watcherRunning = !!got?.watcher_running;
      onstatus?.("hooks applied");
    } catch (e) {
      err = e?.message || String(e);
    } finally {
      saving = false;
    }
  }

  async function run() {
    running = true;
    err = "";
    try {
      const got = await api.hooksRun(labId);
      state = got?.state || null;
      onstatus?.("hooks run complete");
    } catch (e) {
      err = e?.message || String(e);
    } finally {
      running = false;
    }
  }

  async function clear() {
    if (!confirm("Drop hooks and per-run state for this lab?")) return;
    err = "";
    try {
      await api.hooksClear(labId);
      source = "";
      parsed = null;
      state = null;
      watcherRunning = false;
    } catch (e) {
      err = e?.message || String(e);
    }
  }

  function insertExample() {
    if (source.trim()) return;
    source = EXAMPLE;
  }

  onMount(load);
</script>

<div class="hooks-pane">
  <div class="hp-head">
    <h3>Ready hooks</h3>
    <span class="hp-note">
      YAML canonical. Auto-fires when every node reaches running.
      {#if watcherRunning}
        <span class="chip on">watcher active</span>
      {/if}
    </span>
    <div class="hp-actions">
      <button onclick={insertExample} disabled={!!source.trim()}>Example</button>
      <button class="primary" onclick={apply} disabled={saving || loading}>
        {saving ? "Saving…" : "Apply"}
      </button>
      <button onclick={run} disabled={running || !parsed}>{running ? "Running…" : "Run now"}</button>
      <button class="danger" onclick={clear} disabled={!source}>Clear</button>
    </div>
  </div>

  {#if loading}
    <div class="hp-loading">loading…</div>
  {:else}
    {#if err}
      <div class="hp-err">{err}</div>
    {/if}

    <textarea
      class="hp-src"
      bind:value={source}
      spellcheck="false"
      placeholder="# Paste hooks.yml here, then click Apply."
      rows="18"
    ></textarea>

    {#if state?.runs?.length}
      <div class="hp-runs">
        <h4>Last run</h4>
        <table>
          <thead>
            <tr><th>Hook</th><th>Phase</th><th>Output / error</th></tr>
          </thead>
          <tbody>
            {#each state.runs as r}
              <tr>
                <td>{r.name}</td>
                <td class="phase-{r.phase}">{r.phase}</td>
                <td>
                  <pre>{r.error || r.output || ""}</pre>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  {/if}
</div>

<style>
  .hooks-pane { padding: 8px; overflow: auto; height: 100%; }
  .hp-head { display: flex; align-items: baseline; gap: 12px; margin-bottom: 8px; }
  .hp-head h3 { margin: 0; font-size: 14px; }
  .hp-note { font-size: 12px; color: #888; }
  .hp-actions { margin-left: auto; display: flex; gap: 6px; }
  .hp-src {
    width: 100%; box-sizing: border-box;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px; line-height: 1.4;
  }
  .hp-runs { margin-top: 12px; }
  .hp-runs table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .hp-runs th, .hp-runs td { padding: 4px 6px; border-bottom: 1px solid #333; text-align: left; }
  .hp-runs pre { margin: 0; white-space: pre-wrap; max-height: 6em; overflow: auto; }
  .phase-passed { color: #6ee7b7; }
  .phase-failed { color: #f87171; }
  .phase-running { color: #fbbf24; }
  .phase-pending { color: #888; }
  .hp-err { background: rgba(248,113,113,.15); color: #fca5a5; padding: 6px; margin-bottom: 6px; font-size: 12px; }
  .hp-loading { color: #888; padding: 8px; }
  .chip { display: inline-block; padding: 1px 6px; margin-left: 6px; border-radius: 3px; font-size: 11px; background: #333; }
  .chip.on { background: #14532d; color: #86efac; }
</style>
