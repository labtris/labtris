<script>
  //: Two states, one form. Before any account exists this creates the first
  //: administrator; afterwards it signs in. Shipping a default password would
  //: be worse than asking, and an instance nobody can administer is bricked
  //: rather than secure — so the first visit is allowed to make the account.
  import { api } from "./api.js";

  let { setupRequired = false, onsignedin } = $props();

  let username = $state("");
  let password = $state("");
  let displayName = $state("");
  let busy = $state(false);
  let error = $state("");

  async function submit(e) {
    e.preventDefault();
    if (!username.trim() || !password) return;
    busy = true;
    error = "";
    try {
      const out = setupRequired
        ? await api.setup(username.trim(), password, displayName.trim())
        : await api.login(username.trim(), password);
      onsignedin?.(out.user);
    } catch (err) {
      error = err.message;
      password = "";
    } finally {
      busy = false;
    }
  }
</script>

<div class="signin">
  <form onsubmit={submit}>
    <h1>Labtris</h1>
    <p class="lead">
      {setupRequired
        ? "No account exists yet. Create the administrator."
        : "Sign in to continue."}
    </p>

    <label>
      <span>Username</span>
      <!-- svelte-ignore a11y_autofocus -->
      <input bind:value={username} autocomplete="username" autofocus disabled={busy} />
    </label>

    {#if setupRequired}
      <label>
        <span>Display name</span>
        <input bind:value={displayName} placeholder="optional" disabled={busy} />
      </label>
    {/if}

    <label>
      <span>Password</span>
      <input
        type="password"
        bind:value={password}
        autocomplete={setupRequired ? "new-password" : "current-password"}
        disabled={busy}
      />
    </label>

    {#if error}<p class="err">{error}</p>{/if}

    <button class="primary" type="submit" disabled={busy || !username.trim() || !password}>
      {busy ? "…" : setupRequired ? "Create administrator" : "Sign in"}
    </button>

    {#if setupRequired}
      <p class="note">
        This is the only time setup is open. Everyone else is added from the Users panel.
      </p>
    {/if}
  </form>
</div>

<style>
  .signin {
    position: fixed;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--bg);
    z-index: 100;
  }
  form {
    width: 320px;
    padding: 26px 24px;
    border: 1px solid var(--stroke);
    border-radius: 14px;
    background: var(--panel);
    box-shadow: var(--shadow);
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  h1 { margin: 0; font-size: 20px; letter-spacing: -0.01em; }
  .lead { margin: 0 0 4px; color: var(--muted); font-size: 12px; }
  label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; }
  label span { color: var(--muted); }
  input { padding: 7px 9px; border-radius: 8px; border: 1px solid var(--stroke);
          background: var(--bg-2); color: var(--text); font-size: 13px; }
  input:focus { outline: none; border-color: var(--accent); }
  button { padding: 8px; border-radius: 8px; border: 1px solid var(--stroke);
           background: var(--accent); color: #04121a; font-weight: 600; cursor: pointer; }
  button:disabled { opacity: 0.55; cursor: default; }
  .err { margin: 0; color: var(--danger); font-size: 12px; }
  .note { margin: 0; color: var(--muted); font-size: 11px; line-height: 1.45; }
</style>
