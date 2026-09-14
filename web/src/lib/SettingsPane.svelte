<script>
  import { api } from "./api.js";

  // Everything the settings and backup endpoints can do, in one place. The
  // backend has had all of this for a while with nothing able to reach it.
  let { onstatus, prefs = null, onprefs = null, themes = [], theme = "", ontheme = null,
        llm = null, onllm = null, initialSection = "appearance",
        //: Passed through from App.svelte so the Users pane knows whether
        //: to show itself (admin-only) and which row is the caller (can't
        //: delete yourself). Non-admins get a Settings without a Users tab.
        currentUser = null } = $props();

  //: One long scroll made everything look equally important — the LiteLLM key
  //: sat beside the QEMU state directory beside a restore-from-file button
  //: that wipes what you have. Sections put a wall between "change a URL" and
  //: "replace this instance's data".
  //:
  //: initialSection lets a caller open Settings on a specific pane — the
  //: host meter's click goes straight to "host" instead of leaving the user
  //: to hunt through the nav.
  let section = $state(initialSection);
  let diag = $state(null);

  let rows = $state([]);
  let edits = $state({});
  let saving = $state(false);
  let note = $state("");
  let err = $state("");

  let drive = $state(null);
  let creds = $state({ client_id: "", client_secret: "", folder_id: "" });
  let link = $state(null);
  let files = $state(null);
  let restoring = $state(false);

  //: Management network: what the host is currently on and what the admin is
  //: about to change it to. Loaded on demand when the pane first opens.
  let mgmt = $state(null);          // {interfaces, current} from the server
  let mgmtForm = $state(null);      // editable copy of `current`
  let mgmtConfirm = $state(false);  // second-click confirmation before Apply
  let mgmtApplying = $state(false);
  let mgmtNote = $state("");

  //: Uplink bridges: sibling of the management pane. Adds extra `brN` on
  //: unused NICs, each of which becomes a Cloud choice in the network picker.
  let uplinks = $state(null);       // {eligible_nics, existing_bridges}
  let uplinkPicked = $state(new Set()); // NICs the user has ticked to add
  let uplinkConfirm = $state(false);
  let uplinkApplying = $state(false);
  let uplinkNote = $state("");

  //: Users pane. Admin-only — an operator on a shared box needs to be
  //: able to see and manage accounts without dropping to curl. Users
  //: array is loaded on first open; addForm is non-null while the modal
  //: is up.
  let users = $state(null);
  let addForm = $state(null);
  let usersErr = $state("");
  let usersBusy = $state(false);

  const groups = $derived([
    { id: "appearance", label: "Appearance" },
    ...sections,
    { id: "management-network", label: "Management network" },
    { id: "uplink-bridges", label: "Uplink bridges" },
    //: The /users endpoint refuses non-admins anyway (require_admin gate);
    //: hiding the nav item for non-admins keeps the UI honest — they don't
    //: see a tab that would just say "forbidden" if they clicked it.
    ...(currentUser?.is_admin ? [{ id: "users", label: "Users" }] : []),
    { id: "host", label: "Host & diagnostics", badge: diagFailures },
    { id: "backup", label: "Backup & restore" },
  ]);

  //: The diagnostics endpoint returns facts, not a pass/fail list, so the
  //: badge is derived from the conditions that actually mean something is
  //: wrong. Counting an invented "checks" array would have shown 0 forever.
  const diagProblems = $derived.by(() => {
    const d = diag;
    if (!d) return [];
    const out = [];
    if (d.accel_setting === "kvm" && !d.kvm?.device) {
      out.push("set to kvm, but this host has no /dev/kvm");
    }
    if (d.kvm?.device && !d.kvm?.writable) out.push("/dev/kvm is not writable by this user");
    if (d.netd && !d.netd.reachable) out.push("netd is not reachable — no networking can be built");
    if (d.docker && !d.docker.reachable) out.push("docker is not reachable");
    if (d.database && !d.database.reachable) out.push("the database is not reachable");
    if (d.ksm?.available && !d.ksm?.enabled) out.push("KSM is available but switched off");
    for (const disk of d.disks ?? []) {
      if (disk.free_gb != null && disk.free_gb < 2) {
        out.push(`${disk.path} has ${disk.free_gb} GB free`);
      }
    }
    return out;
  });
  const diagFailures = $derived(diagProblems.length);

  function rowsIn(id) {
    return rows.filter((r) => (r.section ?? "general") === id);
  }

  let sections = $state([]);

  async function load() {
    try {
      const got = await api.settings();
      rows = got.settings;
      sections = got.sections ?? [{ id: "general", label: "General" }];
      edits = {};
      drive = await api.gdriveStatus();
      //: A count of what is wrong, on the nav item, so a broken host is
      //: visible without opening the pane that would tell you.
      try {
        diag = await api.diagnostics();
      } catch {
        diag = null;
      }
      err = "";
    } catch (e) {
      err = e.message;
    }
  }

  $effect(() => {
    load();
  });

  async function loadMgmt() {
    mgmtNote = "";
    try {
      mgmt = await api.managementNetwork();
      const cur = mgmt.current || {};
      // The form starts as an editable copy of what's on the box; edits
      // don't touch `mgmt.current` so we can show "was → will be" if we
      // want to.
      // Default the target to whichever interface currently carries the
      // default route (that's where the management IP lives today), else
      // the first candidate.
      const defaultNic = mgmt.interfaces.find((n) => n.carries_default_route)
        || mgmt.interfaces[0]
        || { name: "ens160", kind: "nic" };
      const picked = mgmt.interfaces.find((n) => n.name === cur.interface) || defaultNic;
      mgmtForm = {
        mode: cur.mode === "manual" ? "manual" : "dhcp",
        interface: picked.name,
        kind: picked.kind || "nic",
        address: cur.address || "",
        gateway: cur.gateway || "",
        dns: (cur.dns || []).join(", "),
      };
    } catch (e) {
      err = `management network: ${e.message}`;
    }
  }

  async function applyMgmt() {
    mgmtApplying = true;
    mgmtNote = "";
    try {
      // When the user changes the picked interface, its `kind` may change
      // too (br0 is a bridge, ens160 is a nic). Re-derive from the current
      // interfaces list at submit time so the payload matches the pick.
      const picked = mgmt.interfaces.find((n) => n.name === mgmtForm.interface);
      const kind = picked?.kind || "nic";
      const body = {
        mode: mgmtForm.mode,
        interface: mgmtForm.interface,
        kind,
      };
      if (mgmtForm.mode === "manual") {
        body.address = mgmtForm.address.trim();
        body.gateway = mgmtForm.gateway.trim();
        body.dns = mgmtForm.dns
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean);
      }
      const out = await api.applyManagementNetwork(body);
      mgmtNote = `Applied. If the address changed, the browser needs a manual reload at the new URL.`;
      mgmt = { ...mgmt, current: out.applied };
      mgmtConfirm = false;
    } catch (e) {
      mgmtNote = "";
      err = `apply failed: ${e.message}`;
    } finally {
      mgmtApplying = false;
    }
  }

  // Load management data the first time this pane opens, not on every
  // Settings open — a settings roundtrip doesn't need to also pay for a
  // netd probe.
  $effect(() => {
    if (section === "management-network" && mgmt === null) {
      loadMgmt();
    }
  });

  async function loadUplinks() {
    uplinkNote = "";
    try {
      uplinks = await api.uplinkBridges();
      // Any existing bridges are already applied — start the picker
      // with them ticked so an admin adding one more doesn't accidentally
      // wipe what's there.
      uplinkPicked = new Set((uplinks.existing_bridges || []).map((b) => b.nic).filter(Boolean));
    } catch (e) {
      err = `uplink bridges: ${e.message}`;
    }
  }

  async function applyUplinks() {
    uplinkApplying = true;
    uplinkNote = "";
    try {
      const pairs = [...uplinkPicked].map((nic) => ({ nic }));
      const out = await api.applyUplinkBridges({ pairs });
      uplinkNote = `Applied. ${out.applied?.length || 0} uplink bridge(s) now defined.`;
      uplinkConfirm = false;
      await loadUplinks();
    } catch (e) {
      err = `apply failed: ${e.message}`;
    } finally {
      uplinkApplying = false;
    }
  }

  $effect(() => {
    if (section === "uplink-bridges" && uplinks === null) {
      loadUplinks();
    }
  });

  async function loadUsers() {
    usersErr = "";
    try {
      const r = await api.users();
      users = r.users || [];
    } catch (e) {
      usersErr = e.message;
    }
  }

  function openAddUser() {
    addForm = {
      username: "",
      display_name: "",
      password: "",
      role: "user",
      error: "",
    };
  }

  async function submitAddUser() {
    if (!addForm.username || !addForm.password) {
      addForm.error = "username and password are required";
      return;
    }
    usersBusy = true;
    addForm.error = "";
    try {
      await api.addUser({
        username: addForm.username.trim().toLowerCase(),
        display_name: addForm.display_name || null,
        password: addForm.password,
        role: addForm.role,
      });
      addForm = null;
      await loadUsers();
    } catch (e) {
      try {
        addForm.error = JSON.parse(e.message).error?.message || e.message;
      } catch {
        addForm.error = e.message;
      }
    } finally {
      usersBusy = false;
    }
  }

  async function removeUser(u) {
    if (!confirm(`Remove ${u.username}? Their labs are kept (ownership goes null).`)) return;
    usersBusy = true;
    usersErr = "";
    try {
      await api.deleteUser(u.id);
      await loadUsers();
    } catch (e) {
      try {
        usersErr = JSON.parse(e.message).error?.message || e.message;
      } catch {
        usersErr = e.message;
      }
    } finally {
      usersBusy = false;
    }
  }

  $effect(() => {
    if (section === "users" && users === null) {
      loadUsers();
    }
  });

  const dirty = $derived(Object.keys(edits).length > 0);

  async function save() {
    saving = true;
    try {
      const out = await api.saveSettings(edits);
      note = out.restart_required.length
        ? `Saved. ${out.restart_required.join(", ")} take effect on the next API restart.`
        : "Saved.";
      if (out.refused_pinned_by_env?.length) {
        note += ` Refused (pinned by env): ${out.refused_pinned_by_env.join(", ")}.`;
      }
      await load();
    } catch (e) {
      err = e.message;
    } finally {
      saving = false;
    }
  }

  function download() {
    // A plain link, so the browser saves it rather than us buffering a blob.
    window.location.href = "/api/v1/backup";
  }

  async function restore(event) {
    const file = event.target.files?.[0];
    if (!file) return;
    restoring = true;
    note = "";
    try {
      const form = new FormData();
      form.append("file", file);
      const r = await fetch("/api/v1/backup/restore", { method: "POST", body: form });
      const out = await r.json();
      if (!r.ok) throw new Error(out?.error?.message || r.statusText);
      note = `Restored ${out.restored.length} lab${out.restored.length === 1 ? "" : "s"}${
        out.failed.length ? `, ${out.failed.length} failed: ${out.failed[0]}` : ""
      }`;
    } catch (e) {
      err = e.message;
    } finally {
      restoring = false;
      event.target.value = "";
    }
  }

  async function saveCreds() {
    try {
      await api.gdriveCredentials(creds);
      drive = await api.gdriveStatus();
      note = "Google client saved.";
    } catch (e) {
      err = e.message;
    }
  }

  async function startLink() {
    try {
      link = await api.gdriveLink();
    } catch (e) {
      err = e.message;
    }
  }

  async function finishLink() {
    try {
      const out = await api.gdriveLinkComplete(link.device_code);
      if (out.pending) {
        note = "Still waiting for you to approve it at Google.";
        return;
      }
      link = null;
      drive = await api.gdriveStatus();
      note = "Linked to Google Drive.";
    } catch (e) {
      err = e.message;
    }
  }

  async function push() {
    try {
      const out = await api.gdrivePush();
      note = `Uploaded ${out.uploaded} (${Math.round(out.bytes / 1024)} KB).`;
      files = null;
    } catch (e) {
      err = e.message;
    }
  }

  async function listFiles() {
    try {
      files = (await api.gdriveList()).files;
    } catch (e) {
      err = e.message;
    }
  }

  async function pull(id) {
    try {
      const out = await api.gdrivePull(id);
      note = `Restored ${out.restored.length} lab${out.restored.length === 1 ? "" : "s"} from Drive.`;
    } catch (e) {
      err = e.message;
    }
  }
</script>

<div class="setwrap">
  <!-- Nav on the left, one pane at a time on the right. Everything below was
       previously a single scroll, which put the LiteLLM key beside a
       restore-from-file button that replaces this instance's data. -->
  <nav class="setnav">
    {#each groups as g}
      <button class:on={section === g.id} onclick={() => (section = g.id)}>
        <span>{g.label}</span>
        {#if g.badge}<span class="navbadge">{g.badge}</span>{/if}
      </button>
    {/each}
  </nav>

  <div class="setpane">
    {#if err}<p class="bad">{err}</p>{/if}
    {#if note}<p class="good">{note}</p>{/if}

    {#if section === "appearance"}
      <h4>Theme</h4>
      <div class="themegrid">
        {#each themes as g}
          <div class="themegroup">
            <div class="tiny hintline">{g.group}</div>
            {#each g.themes as t}
              <button class="themebtn" class:on={theme === t.id} onclick={() => ontheme?.(t.id)}>
                {t.label}
              </button>
            {/each}
          </div>
        {/each}
      </div>

      <h4>Canvas</h4>
      <label class="opt">
        <input type="checkbox" checked={prefs?.grid ?? true}
          onchange={(e) => onprefs?.({ grid: e.currentTarget.checked })} />
        <span>Show grid</span>
      </label>
      <label class="opt">
        <input type="checkbox" checked={prefs?.snap ?? true}
          onchange={(e) => onprefs?.({ snap: e.currentTarget.checked })} />
        <span>Snap nodes to grid</span>
      </label>
      <label class="opt">
        <span>Node label</span>
        <select value={prefs?.nodeLabel ?? "image"}
          onchange={(e) => onprefs?.({ nodeLabel: e.currentTarget.value })}>
          <option value="image">Name and image</option>
          <option value="name">Name only</option>
        </select>
      </label>

      <h4>Drawer</h4>
      <label class="opt">
        <span>Open on launch</span>
        <select value={prefs?.dock ?? "console"}
          onchange={(e) => onprefs?.({ dock: e.currentTarget.value })}>
          <option value="console">Terminals</option>
          <option value="ai">Assistant</option>
          <option value="logs">Logs</option>
          <option value="events">Events</option>
        </select>
      </label>
      <p class="tiny hintline">
        The Events tab still opens itself when an error arrives, whatever this is set to.
      </p>
      <p class="tiny hintline">Changes apply immediately.</p>

    {:else if section === "assistant" && llm}
      <h4>Your model</h4>
      <p class="tiny hintline">
        These stay in this browser. Labtris never receives the key — the request
        goes straight from here to your provider, and the server is asked only to
        run the tools the model chooses, as you.
      </p>
      <table class="setgrid">
        <tbody>
          <tr>
            <td class="lbl">API base URL</td>
            <td>
              <input class="mono" value={llm.baseUrl}
                onchange={(e) => onllm?.({ baseUrl: e.currentTarget.value.trim() })} />
              <div class="tiny hintline">Anything OpenAI-compatible: OpenAI, a local vLLM, LiteLLM, Ollama.</div>
            </td>
          </tr>
          <tr>
            <td class="lbl">Model</td>
            <td>
              <input class="mono" value={llm.model} placeholder="gpt-4o-mini, llama3.1:70b…"
                onchange={(e) => onllm?.({ model: e.currentTarget.value.trim() })} />
            </td>
          </tr>
          <tr>
            <td class="lbl">API key</td>
            <td>
              <input class="mono" type="password" value={llm.apiKey}
                placeholder={llm.apiKey ? "•••• stored in this browser" : "not set"}
                onchange={(e) => onllm?.({ apiKey: e.currentTarget.value.trim() })} />
              <div class="tiny hintline">
                Kept in this browser's local storage. Clearing site data removes it.
              </div>
            </td>
          </tr>
        </tbody>
      </table>
      <div class="acts">
        <button onclick={() => onllm?.({ apiKey: "" })} disabled={!llm.apiKey}>Forget the key</button>
      </div>
      <p class="tiny hintline">
        Your provider must allow browser requests. A key that works from a
        terminal but fails here with a CORS error is the provider refusing the
        origin, not Labtris.
      </p>

      <h4>Server-side fallback</h4>
      <p class="tiny hintline">
        Used only when no key is set above. An instance that supplies its own
        model — a shared classroom box, an air-gapped install with a local
        vLLM — configures it here instead, and then everyone gets it without
        bringing anything.
      </p>
      {#if rowsIn("assistant").length}
        <table class="setgrid">
          <tbody>
            {#each rowsIn("assistant") as row}
              <tr>
                <td class="lbl">
                  {row.label}
                  {#if !row.live}<span class="tag">restart</span>{/if}
                  {#if row.source === "env"}<span class="tag env">env</span>{/if}
                </td>
                <td>
                  {#if !row.editable}
                    <span class="mono tiny fixed">pinned by LABTRIS_{row.key.toUpperCase()}</span>
                  {:else}
                    <input
                      class="mono"
                      type={row.kind === "secret" ? "password" : row.kind === "int" ? "number" : "text"}
                      placeholder={row.kind === "secret" ? (row.set ? "•••• set" : "not set") : ""}
                      value={edits[row.key] ?? row.value}
                      oninput={(e) => (edits = { ...edits, [row.key]: e.currentTarget.value })}
                    />
                  {/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
        <div class="acts">
          <button class="primary" onclick={save} disabled={!dirty || saving}>
            {saving ? "Saving…" : "Save"}
          </button>
          <button onclick={load} disabled={!dirty}>Discard</button>
        </div>
      {/if}

    {:else if section === "management-network"}
      <h4>Management network</h4>
      <p class="tiny hintline">
        How this box gets its own address. DHCP is the installer default and moves
        when the lease expires or the DHCP server hands out a different address.
        Manual pins it — and pins the gateway and DNS with it, so a lab server has
        a fixed URL that survives reboots.
      </p>
      {#if !mgmt}
        <p class="tiny hintline">Loading…</p>
      {:else if mgmtForm}
        {#if mgmt.current}
          <p class="tiny hintline">
            <strong>Now:</strong>
            {mgmt.current.mode === "manual" ? "Manual" : "DHCP"}
            on <code>{mgmt.current.interface || "?"}</code> —
            <code>{mgmt.current.address || "no address"}</code>
            {#if mgmt.current.gateway}via <code>{mgmt.current.gateway}</code>{/if}
          </p>
        {/if}

        <label class="opt">
          <input
            type="radio"
            name="mgmt-mode"
            value="dhcp"
            bind:group={mgmtForm.mode}
          />
          <span>DHCP</span>
        </label>
        <label class="opt">
          <input
            type="radio"
            name="mgmt-mode"
            value="manual"
            bind:group={mgmtForm.mode}
          />
          <span>Manual</span>
        </label>

        <label class="opt">
          <span>Interface</span>
          <select bind:value={mgmtForm.interface}>
            {#each mgmt.interfaces as nic}
              <option value={nic.name}>
                {nic.name}{nic.kind === "bridge" ? " (host bridge)" : ""}{nic.addresses?.length
                  ? ` — ${nic.addresses[0]}`
                  : ""}
              </option>
            {/each}
          </select>
        </label>

        {#if mgmtForm.mode === "manual"}
          <label class="opt">
            <span>Address (CIDR)</span>
            <input class="mono" bind:value={mgmtForm.address} placeholder="10.124.133.91/24" />
          </label>
          <label class="opt">
            <span>Gateway</span>
            <input class="mono" bind:value={mgmtForm.gateway} placeholder="10.124.133.1" />
          </label>
          <label class="opt">
            <span>DNS servers (comma-separated)</span>
            <input class="mono" bind:value={mgmtForm.dns} placeholder="8.8.8.8, 1.1.1.1" />
          </label>
        {/if}

        <div class="mgmt-warn">
          <strong>Read before Apply.</strong>
          The new config is applied immediately. If the address changes, this
          browser session ends the moment it takes effect — reconnect at the
          new URL. If the new config is wrong (bad gateway, subnet mismatch),
          the box drops off the network and only the physical / vSphere
          console can recover it. Labtris does not auto-revert.
        </div>

        {#if mgmtNote}<p class="good">{mgmtNote}</p>{/if}

        {#if !mgmtConfirm}
          <button
            class="primary"
            disabled={mgmtApplying}
            onclick={() => (mgmtConfirm = true)}
          >
            Apply…
          </button>
        {:else}
          <p class="tiny hintline">
            Applying <strong>{mgmtForm.mode}</strong>
            {#if mgmtForm.mode === "manual"}
              — <code>{mgmtForm.address}</code> via
              <code>{mgmtForm.gateway}</code>
            {/if}
            on <code>{mgmtForm.interface}</code>. Sure?
          </p>
          <button disabled={mgmtApplying} onclick={() => (mgmtConfirm = false)}>Cancel</button>
          <button class="primary danger" disabled={mgmtApplying} onclick={applyMgmt}>
            {mgmtApplying ? "Applying…" : "Yes, apply"}
          </button>
        {/if}
      {/if}

    {:else if section === "uplink-bridges"}
      <h4>Uplink bridges</h4>
      <p class="tiny hintline">
        Turn extra host NICs into Cloud choices. One NIC per bridge, one Cloud per
        bridge. The management NIC is not offered; make its uplink from a
        different NIC.
      </p>
      {#if !uplinks}
        <p class="tiny hintline">Loading…</p>
      {:else}
        {#if uplinks.existing_bridges?.length}
          <h5>Existing</h5>
          <table class="setgrid">
            <tbody>
              {#each uplinks.existing_bridges as br}
                <tr>
                  <td class="lbl mono">{br.name}</td>
                  <td class="mono tiny">{br.nic || "(no NIC)"}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        {:else}
          <p class="tiny hintline">No uplink bridges configured.</p>
        {/if}

        <h5>Add / remove</h5>
        {#if uplinks.eligible_nics?.length || uplinks.existing_bridges?.length}
          <p class="tiny hintline">
            Tick the NICs to include. Any NIC unticked will lose its bridge on
            Apply. Bridge names are allocated as br1, br2, br3… in the order below.
          </p>
          <div class="uplink-nics">
            {#each [...(uplinks.existing_bridges || []).map((b) => b.nic).filter(Boolean), ...(uplinks.eligible_nics || []).map((n) => n.name)] as nic (nic)}
              <label class="opt">
                <input
                  type="checkbox"
                  checked={uplinkPicked.has(nic)}
                  onchange={(e) => {
                    if (e.currentTarget.checked) uplinkPicked = new Set([...uplinkPicked, nic]);
                    else { const s = new Set(uplinkPicked); s.delete(nic); uplinkPicked = s; }
                  }}
                />
                <span class="mono">{nic}</span>
              </label>
            {/each}
          </div>
        {:else}
          <p class="tiny hintline">
            No free NICs to add. Adding a NIC to the VM/host and rebooting will
            make it appear here.
          </p>
        {/if}

        <div class="mgmt-warn">
          <strong>Read before Apply.</strong>
          Applying rewrites <code>/etc/netplan/60-labtris-cloud.yaml</code> and
          runs <code>netplan apply</code>. A NIC whose config accidentally
          shadows the management NIC would take the box off the network.
          Recovery is at the physical / vSphere console. Labtris does not
          auto-revert.
        </div>

        {#if uplinkNote}<p class="good">{uplinkNote}</p>{/if}

        {#if !uplinkConfirm}
          <button
            class="primary"
            disabled={uplinkApplying}
            onclick={() => (uplinkConfirm = true)}
          >
            Apply…
          </button>
        {:else}
          <p class="tiny hintline">
            Applying: <strong>{uplinkPicked.size}</strong> uplink bridge(s) —
            {[...uplinkPicked].join(", ") || "(none)"}. Sure?
          </p>
          <button disabled={uplinkApplying} onclick={() => (uplinkConfirm = false)}>Cancel</button>
          <button class="primary danger" disabled={uplinkApplying} onclick={applyUplinks}>
            {uplinkApplying ? "Applying…" : "Yes, apply"}
          </button>
        {/if}
      {/if}

    {:else if section === "users"}
      <div class="users-hd">
        <h4>Users</h4>
        <button class="primary" onclick={openAddUser}>Add user</button>
      </div>
      <p class="hint tiny">
        Every request on this instance runs as the user who signed in.
        Admins can add, remove, and see everyone; a regular user can build
        in their own labs and cannot delete other people's.
      </p>
      {#if usersErr}
        <p class="hint tiny danger-text">{usersErr}</p>
      {/if}
      {#if users === null}
        <p class="hint tiny">loading…</p>
      {:else if users.length === 0}
        <p class="hint tiny">no accounts yet — that would only happen mid-migration.</p>
      {:else}
        <table class="users">
          <thead>
            <tr><th>username</th><th>display name</th><th>role</th><th></th></tr>
          </thead>
          <tbody>
            {#each users as u}
              <tr>
                <td class="mono">{u.username}{u.id === currentUser?.id ? " (you)" : ""}</td>
                <td>{u.display_name || ""}</td>
                <td>
                  {u.role}{u.is_admin ? " · admin" : ""}
                </td>
                <td class="row-actions">
                  {#if u.id !== currentUser?.id}
                    <button class="ghost" disabled={usersBusy} onclick={() => removeUser(u)}>Remove</button>
                  {/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}

      {#if addForm}
        <!-- Inline modal: same pattern the palette Upload dialog uses. -->
        <div class="modal-back fixed" onpointerdown={() => (usersBusy ? null : (addForm = null))}>
          <!-- svelte-ignore a11y_no_static_element_interactions -->
          <div class="modal" onpointerdown={(e) => e.stopPropagation()}>
            <h3>Add user</h3>
            <label class="pick">
              <span>username</span>
              <input bind:value={addForm.username} placeholder="alice" autofocus />
            </label>
            <label class="pick">
              <span>display name</span>
              <input bind:value={addForm.display_name} placeholder="Alice (optional)" />
            </label>
            <label class="pick">
              <span>password</span>
              <input type="password" bind:value={addForm.password} autocomplete="new-password" />
            </label>
            <label class="pick">
              <span>role</span>
              <select bind:value={addForm.role}>
                <option value="user">user — build in own labs</option>
                <option value="admin">admin — full access, can manage users</option>
              </select>
            </label>
            {#if addForm.error}
              <p class="hint tiny danger-text">{addForm.error}</p>
            {/if}
            <div class="modal-actions">
              <button disabled={usersBusy} onclick={() => (addForm = null)}>Cancel</button>
              <button
                class="primary"
                disabled={usersBusy || !addForm.username || !addForm.password}
                onclick={submitAddUser}
              >
                {usersBusy ? "Adding…" : "Add"}
              </button>
            </div>
          </div>
        </div>
      {/if}

    {:else if section === "host"}
      <h4>Host</h4>
      {#if diagProblems.length}
        <ul class="problems">
          {#each diagProblems as p}<li>{p}</li>{/each}
        </ul>
      {:else if diag}
        <p class="tiny hintline">Nothing looks wrong with this host.</p>
      {/if}
      {#if diag}
        <table class="setgrid">
          <tbody>
            <tr><td class="lbl">version</td><td class="mono tiny">{diag.labtris_version}</td></tr>
            <tr><td class="lbl">platform</td><td class="mono tiny">{diag.platform}</td></tr>
            <tr><td class="lbl">cpu</td><td class="mono tiny">{diag.cpu?.model} · {diag.cpu?.cores} cores</td></tr>
            <tr><td class="lbl">memory</td><td class="mono tiny">{diag.memory_mb?.MemAvailable} of {diag.memory_mb?.MemTotal} MB free</td></tr>
            <tr><td class="lbl">acceleration</td><td class="mono tiny">{diag.accel_setting} · {diag.kvm?.note}</td></tr>
            <tr><td class="lbl">qemu</td><td class="mono tiny">{diag.qemu}</td></tr>
            <tr><td class="lbl">docker</td><td class="mono tiny">{diag.docker?.version ?? "unreachable"}</td></tr>
            <tr><td class="lbl">database</td><td class="mono tiny">migration {diag.database?.migration} · {diag.database?.labs} labs, {diag.database?.nodes} nodes</td></tr>
            <tr><td class="lbl">ksm</td><td class="mono tiny">{diag.ksm?.enabled ? "on" : "off"} · scan {diag.ksm?.pages_to_scan}</td></tr>
          </tbody>
        </table>
      {/if}

    {:else if section === "backup"}
    <h4>Backup</h4>
    <p class="tiny hintline">
      Every lab's topology plus these settings. Configuration only — QEMU images are
      0.7–6&nbsp;GB copies of something with a known download URL, so they are left out and
      re-fetched on first start. Restore is additive.
    </p>
    <div class="acts">
      <button onclick={download}>Download archive</button>
      <label class="filebtn">
        {restoring ? "Restoring…" : "Restore from file"}
        <input type="file" accept=".gz,.tar.gz,application/gzip" onchange={restore} />
      </label>
    </div>
  
    <h4>Google Drive</h4>
    {#if drive && !drive.configured}
      <p class="tiny hintline">
        Needs your own OAuth client — Cloud Console → enable the Drive API → Credentials →
        OAuth client ID → <strong>Desktop app</strong>. Scope requested is
        <code>drive.file</code>, which is files this app created, not your Drive.
      </p>
      <div class="acts">
        <input class="mono" placeholder="client id" bind:value={creds.client_id} />
        <input class="mono" type="password" placeholder="client secret" bind:value={creds.client_secret} />
        <button onclick={saveCreds} disabled={!creds.client_id || !creds.client_secret}>Save</button>
      </div>
    {:else if drive && !drive.linked}
      {#if link}
        <p class="tiny hintline">
          Open <a href={link.verification_url} target="_blank" rel="noreferrer">
            {link.verification_url}</a
          >
          and enter <strong class="mono">{link.user_code}</strong>, then press Done. A lab host is
          usually headless, which is why this is a code rather than a redirect.
        </p>
        <div class="acts"><button class="primary" onclick={finishLink}>Done</button></div>
      {:else}
        <div class="acts"><button class="primary" onclick={startLink}>Link a Google account</button></div>
      {/if}
    {:else if drive}
      <div class="acts">
        <button class="primary" onclick={push}>Back up to Drive</button>
        <button onclick={listFiles}>List backups</button>
      </div>
      {#if files}
        {#if files.length}
          <ul class="applied">
            {#each files as f}
              <li class="tiny">
                <button class="linky" onclick={() => pull(f.id)}>restore</button>
                <span class="mono">{f.name}</span>
                <span class="muted">{f.createdTime?.slice(0, 16).replace("T", " ")}</span>
              </li>
            {/each}
          </ul>
        {:else}
          <p class="tiny hintline">No backups in Drive yet.</p>
        {/if}
      {/if}
    {/if}

    {:else}
      <!-- Every remaining section is config rows, filtered to that section. -->
      {#if rowsIn(section).length}
      <table class="setgrid">
        <tbody>
          {#each rowsIn(section) as row}
            <tr>
              <td class="lbl">
                {row.label}
                {#if !row.live}<span class="tag">restart</span>{/if}
                {#if row.source === "env"}<span class="tag env">env</span>{/if}
              </td>
              <td>
                {#if !row.editable}
                  <span class="mono tiny fixed">{row.value} — pinned by LABTRIS_{row.key.toUpperCase()}</span>
                {:else if row.kind === "choice"}
                  <select
                    value={edits[row.key] ?? row.value}
                    onchange={(e) => (edits = { ...edits, [row.key]: e.currentTarget.value })}
                  >
                    {#each row.choices as c}<option value={c}>{c}</option>{/each}
                  </select>
                {:else}
                  <input
                    class="mono"
                    type={row.kind === "secret" ? "password" : row.kind === "int" ? "number" : "text"}
                    placeholder={row.kind === "secret" ? (row.set ? "•••• set" : "not set") : ""}
                    value={edits[row.key] ?? row.value}
                    oninput={(e) => (edits = { ...edits, [row.key]: e.currentTarget.value })}
                  />
                {/if}
                {#if row.note}<div class="tiny hintline">{row.note}</div>{/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
      <div class="acts">
        <button class="primary" onclick={save} disabled={!dirty || saving}>
          {saving ? "Saving…" : "Save"}
        </button>
        <button onclick={load} disabled={!dirty}>Discard</button>
      </div>
    
        <div class="acts">
          <button class="primary" onclick={save} disabled={!dirty || saving}>
            {saving ? "Saving…" : "Save"}
          </button>
          <button onclick={load} disabled={!dirty}>Discard</button>
        </div>
      {:else}
        <p class="tiny hintline">Nothing configurable here yet.</p>
      {/if}
    {/if}
  </div>
</div>
<style>
  .setwrap { flex: 1; display: flex; min-height: 0; min-width: 0; }
  .setnav {
    flex: 0 0 168px; display: flex; flex-direction: column; gap: 2px;
    padding: 10px 8px; border-right: 1px solid var(--stroke); overflow: auto;
  }
  .setnav button {
    display: flex; align-items: center; gap: 8px; width: 100%;
    min-height: 30px; padding: 0 10px; border-radius: 8px;
    background: transparent; border-color: transparent; color: var(--muted);
    font-size: 12.5px; font-weight: 500; text-align: left;
  }
  .setnav button span:first-child { flex: 1; }
  .setnav button:hover { color: var(--text); }
  .setnav button.on { background: var(--bg-2); color: var(--text); }
  .navbadge {
    font-size: 10px; padding: 1px 6px; border-radius: 999px;
    background: color-mix(in srgb, var(--warn) 20%, transparent); color: var(--warn);
  }
  .setpane { flex: 1; min-width: 0; overflow: auto; padding: 12px 14px; }
  .users-hd { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
  .users-hd h4 { margin: 0; }
  table.users { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 13px; }
  table.users th, table.users td {
    padding: 6px 8px; text-align: left;
    border-bottom: 1px solid var(--stroke);
  }
  table.users th { color: var(--muted); font-weight: 500; font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; }
  table.users .row-actions { text-align: right; }
  table.users .row-actions .ghost {
    background: transparent; border: 1px solid var(--stroke);
    color: var(--muted); padding: 2px 8px; border-radius: 4px;
    font-size: 11px; cursor: pointer;
  }
  table.users .row-actions .ghost:hover:not(:disabled) { color: var(--danger, #dc2626); border-color: var(--danger, #dc2626); }
  .themegrid { display: flex; flex-wrap: wrap; gap: 14px; }
  .themegroup { display: flex; flex-direction: column; gap: 3px; min-width: 130px; }
  .themebtn {
    min-height: 26px; padding: 0 9px; font-size: 12px; text-align: left;
    background: transparent; border-color: transparent; color: var(--muted);
    border-radius: 7px;
  }
  .themebtn:hover { color: var(--text); background: var(--raise); }
  .themebtn.on { color: var(--accent); background: var(--raise); }
  .opt { display: flex; align-items: center; gap: 9px; margin: 7px 0; font-size: 12.5px; }
  .opt input[type="checkbox"] { min-height: 0; }
  .problems { margin: 4px 0 12px; padding-left: 18px; font-size: 12.5px; color: var(--warn); }
  .problems li { margin: 3px 0; }
  h4 { margin: 16px 0 6px; font-size: 11px; text-transform: uppercase;
       letter-spacing: .06em; color: var(--muted); }
  h4:first-of-type { margin-top: 0; }
  .setgrid { width: 100%; border-collapse: collapse; }
  .setgrid td { padding: 3px 4px; vertical-align: top; }
  .setgrid .lbl { width: 45%; font-size: 12px; padding-top: 8px; }
  .setgrid input, .setgrid select { width: 100%; }
  .tag { font-size: 9px; padding: 1px 4px; border-radius: 4px; margin-left: 5px;
         border: 1px solid var(--stroke); color: var(--muted); }
  .tag.env { border-color: var(--warn); color: var(--warn); }
  .fixed { opacity: .65; display: inline-block; padding-top: 7px; }
  .hintline { color: var(--muted); margin: 2px 0 0; line-height: 1.4; }
  .acts { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; align-items: center; }
  .acts input { flex: 1; min-width: 120px; }
  .filebtn { font-size: 12px; padding: 5px 10px; border: 1px solid var(--stroke);
             border-radius: 8px; cursor: pointer; }
  .filebtn input { display: none; }
  .linky { padding: 0 4px; color: var(--accent); background: none; border: 0; font-size: 11px; }
  .bad { color: var(--danger); font-size: 12px; margin: 0 0 8px; }
  .good { color: var(--accent); font-size: 12px; margin: 0 0 8px; }
  .muted { color: var(--muted); }
  .mgmt-warn {
    margin: 12px 0;
    padding: 8px 10px;
    border: 1px solid var(--danger, #dc2626);
    border-radius: 6px;
    font-size: 12px;
    color: var(--danger, #dc2626);
    background: color-mix(in srgb, var(--danger, #dc2626) 8%, transparent);
  }
  .primary.danger { background: var(--danger, #dc2626); color: white; }
  .uplink-nics { display: flex; flex-direction: column; gap: 4px; margin: 8px 0; }
</style>
