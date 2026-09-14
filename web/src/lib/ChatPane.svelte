<script>
  // The assistant conversation, extracted from App.svelte so the same DOM
  // renders both inside a dock (historical) and inside a FloatWindow
  // (current). Nothing knows about layout here — the parent decides where
  // and how big the pane is; this component only paints the messages and
  // the input.
  let {
    chat = [],
    chatIn = $bindable(""),
    aiStatus = null,
    aiStep = "",
    stepCount = 0,
    busy = false,
    //: While the current turn is streaming, `liveTurn` is `{text,
    //: reasoning}` and grows as deltas arrive. Null when nothing is
    //: in flight or between the "turn" event and the next stream.
    liveTurn = null,
    attachments = [],
    onattach = (_files) => {},
    onremoveattach = (_idx) => {},
    labId = null,
    onsend = () => {},
    //: Called when the user clicks Stop while a turn is in flight. The
    //: parent should send `{kind: "cancel"}` on the assistant WebSocket;
    //: the server's `cancelled` event is what finalises the UI.
    onstop = () => {},
    //: Called when the user clicks the Clear button in the header. The
    //: parent resets the persisted chat + the history threaded through
    //: subsequent turns; this component is stateless.
    onclear = () => {},
  } = $props();

  let fileInput;

  function askClear() {
    if (chat.length === 0 && !liveTurn) return;
    if (confirm("Clear the assistant conversation? The lab itself is untouched.")) {
      onclear();
    }
  }
</script>

<div class="ai">
  <!-- Slim header row: just a Clear button, top-right, and only when
       there's something to clear. Not shown in the empty state so a
       fresh chat doesn't have visual noise above the "Ask it to build
       something" hint. -->
  {#if chat.length > 0 || liveTurn}
    <div class="chat-hd">
      <button
        type="button"
        class="clear"
        title="clear the conversation (the lab itself is untouched)"
        aria-label="clear conversation"
        disabled={busy}
        onclick={askClear}
      >🗑 Clear</button>
    </div>
  {/if}
  {#if busy}
    <!-- The whole-turn "working" indicator. Shows for the full duration
         of an in-flight request, not just while a tool call is running,
         so the model's thinking-between-calls doesn't look like nothing
         is happening. Names the last tool call and the running count so
         it's obvious the loop is progressing. -->
    <div class="strip-run">
      <span class="pulse"></span>
      <span>Assistant is working</span>
      {#if aiStep}
        <span class="dim">— running <span class="mono">{aiStep}</span></span>
      {/if}
      {#if stepCount > 0}
        <span class="dim">· {stepCount} tool{stepCount === 1 ? "" : "s"} used</span>
      {/if}
    </div>
  {/if}
  <div class="chat">
    {#each chat as m}
      <div class="bubble" class:you={m.role === "you"} class:error={m.error}>
        <strong>{m.role}</strong>
        {#if m.reasoning}
          <!-- Reasoning content the model exposed, folded by default so
               it does not dominate the reply. Only some models emit
               anything here; when they do it is often the most useful
               half of the answer. -->
          <details class="thinking">
            <summary>thinking</summary>
            <pre>{m.reasoning}</pre>
          </details>
        {/if}
        <pre>{m.text}</pre>
        {#if m.applied?.length}
          <ul class="applied">
            {#each m.applied as a}
              <li class="mono tiny">{a}</li>
            {/each}
          </ul>
        {/if}
      </div>
    {:else}
      {#if aiStatus && !aiStatus.reachable}
        <p class="hint">
          No model reachable at <code>{aiStatus.base_url}</code>. Point
          <code>LABTRIS_LLM_BASE_URL</code> at your LiteLLM (and
          <code>LABTRIS_LLM_API_KEY</code> / <code>LABTRIS_LLM_MODEL</code>) and the
          assistant can drive the lab with the {aiStatus.tools.length} tools it has.
          Until then only a few built-in patterns work.
        </p>
      {:else if aiStatus}
        <p class="hint">
          {aiStatus.model} via {aiStatus.base_url} · {aiStatus.tools.length} tools.
          Ask it to build something: "3 alpine in a triangle", "put r1 and r2 on a
          bridge", "give the link 100ms one way".
        </p>
      {/if}
    {/each}
    {#if liveTurn}
      <!-- The current in-flight turn, growing as tokens arrive. Rendered
           with a blinking caret so it is visually distinct from a
           finalized bubble; committed to `chat` by the parent when the
           turn's terminator event arrives. -->
      <div class="bubble live">
        <strong>labtris</strong>
        {#if liveTurn.reasoning}
          <details class="thinking" open>
            <summary>thinking</summary>
            <pre>{liveTurn.reasoning}</pre>
          </details>
        {/if}
        <pre>{liveTurn.text || ""}<span class="caret"></span></pre>
      </div>
    {/if}
  </div>
  {#if attachments.length}
    <!-- Chip list of things that will go with the next Send. Each chip
         has a remove-button so a mis-picked file does not need a whole
         redo. Images/PDFs are shown by name only; the transcript below
         doesn't render the pixels to keep it scannable. -->
    <div class="attachments">
      {#each attachments as a, i}
        <span class="attachment-chip mono tiny">
          {a.filename}
          <button class="chip-x" onclick={() => onremoveattach(i)} title="remove">×</button>
        </span>
      {/each}
    </div>
  {/if}
  <form
    onsubmit={(e) => {
      e.preventDefault();
      if (busy) onstop();
      else onsend();
    }}
  >
    <!-- Attach: hidden native input driven by a paperclip button so we
         can style consistently. Vision models take images (.png/.jpg/
         .jpeg/.webp/.gif); PDFs are converted server-side. -->
    <input
      type="file"
      bind:this={fileInput}
      multiple
      accept="image/png,image/jpeg,image/webp,image/gif,application/pdf"
      style="display:none"
      onchange={(e) => { onattach(e.currentTarget.files); e.currentTarget.value = ""; }}
    />
    <button type="button" class="attach" title="attach an image or PDF" disabled={busy} onclick={() => fileInput?.click()}>📎</button>
    <!-- Textarea, not input: a real prompt is usually a couple of lines
         (a topology description, a snippet of config to paste). Grows
         with the content up to a cap; then it scrolls internally. Enter
         sends, Shift-Enter inserts a newline — the convention every
         chat app has settled on. -->
    <textarea
      bind:value={chatIn}
      placeholder="talk to the lab… (Enter sends, Shift-Enter for a new line)"
      disabled={busy}
      rows="1"
      onkeydown={(e) => {
        if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
          e.preventDefault();
          if (busy) onstop();
          else onsend();
        }
      }}
    ></textarea>
    {#if busy}
      <!-- Stop swaps for Send while a turn is in flight. Clicking it
           sends `{kind:"cancel"}` on the assistant WS; the server's
           cancelled/done pair cleans up state. -->
      <button class="stop" type="submit" onclick={(e) => { e.preventDefault(); onstop(); }}>Stop</button>
    {:else}
      <button class="primary" type="submit" disabled={!labId}>Send</button>
    {/if}
  </form>
</div>

<style>
  /* Fill whatever container the parent gives us — inside a FloatWindow,
     that's the content area under the title bar; inside a dock, the tab
     body. */
  .ai { display: flex; flex-direction: column; height: 100%; }
  .chat-hd {
    display: flex; justify-content: flex-end;
    padding: 6px 8px 0; gap: 6px;
  }
  .clear {
    background: transparent; border: 1px solid var(--stroke);
    color: var(--muted); padding: 2px 8px; border-radius: 4px;
    font-size: 11px; cursor: pointer;
  }
  .clear:hover:not(:disabled) { color: var(--danger, #dc2626); border-color: var(--danger, #dc2626); }
  .clear:disabled { opacity: 0.4; cursor: not-allowed; }
  .strip-run {
    display: flex; align-items: center; gap: 6px;
    padding: 8px 12px; font-size: 12.5px;
    background: color-mix(in srgb, var(--accent, #22c55e) 14%, transparent);
    border-bottom: 1px solid color-mix(in srgb, var(--accent, #22c55e) 35%, transparent);
    color: var(--fg);
  }
  .strip-run .dim { color: var(--muted); }
  .strip-run .mono { font-family: "IBM Plex Mono", monospace; font-size: 12px; }
  /* Pulsing dot — it moves, so a stall (server hung, socket died) is
     visible without the user having to guess whether "no bubble" means
     "still thinking" or "already broken". */
  .pulse {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--accent, #22c55e);
    animation: pulse 1.2s ease-in-out infinite;
  }
  @keyframes pulse {
    0%, 100% { transform: scale(1); opacity: 0.7; }
    50% { transform: scale(1.4); opacity: 1; }
  }
  .cdot { display: inline-block; width: 6px; height: 6px; border-radius: 50%;
          background: var(--accent, #22c55e); margin-right: 6px; }
  .chat { flex: 1; overflow-y: auto; padding: 8px; }
  .bubble { margin: 6px 0; padding: 6px 8px; border-radius: 8px;
            background: color-mix(in srgb, var(--fg, #000) 4%, transparent); }
  .bubble.you { background: color-mix(in srgb, var(--accent, #22c55e) 12%, transparent); }
  .bubble.live { border-left: 2px solid var(--accent, #22c55e); }
  /* A refused tool call is the single most useful thing to see, so its
     bubble is visually distinct — a red left border + subtle tint. The
     text and applied list render the same way; only the framing changes,
     so a wall of orange doesn't drown out normal turns. */
  .bubble.error {
    background: color-mix(in srgb, var(--danger, #dc2626) 8%, transparent);
    border-left: 2px solid var(--danger, #dc2626);
  }
  .caret {
    display: inline-block; width: 6px; height: 12px; margin-left: 1px;
    vertical-align: text-bottom; background: var(--accent, #22c55e);
    animation: caret 1s steps(1) infinite;
  }
  @keyframes caret { 50% { opacity: 0; } }
  .thinking { margin: 4px 0; font-size: 11px; color: var(--muted); }
  .thinking summary { cursor: pointer; user-select: none; }
  .thinking pre {
    margin: 4px 0 0; padding: 4px 6px;
    background: color-mix(in srgb, var(--fg, #000) 3%, transparent);
    border-radius: 4px; font-size: 11px;
    white-space: pre-wrap; word-break: break-word;
    font-family: "IBM Plex Mono", monospace;
  }
  .bubble strong { font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em;
                    color: var(--muted); }
  .bubble pre { margin: 4px 0 0; white-space: pre-wrap; word-break: break-word;
                font-family: "IBM Plex Mono", monospace; font-size: 12px; }
  .applied { margin: 6px 0 0; padding-left: 16px; color: var(--muted); }
  .applied li { margin: 1px 0; }
  /* align-items: flex-end so a growing textarea stays visually anchored
     to Send/Stop rather than pushing them upward. */
  form { display: flex; gap: 6px; padding: 8px; border-top: 1px solid var(--stroke); align-items: flex-end; }
  form input { flex: 1; }
  form textarea {
    flex: 1;
    resize: vertical;
    min-height: 32px;
    max-height: 220px;
    font-family: inherit;
    font-size: inherit;
    line-height: 1.4;
    padding: 6px 8px;
    /* field-sizing: content lets the textarea grow with its content up
       to max-height without JS. Fallback on older browsers is the
       explicit min-height + user resize handle. */
    field-sizing: content;
  }
  .stop {
    background: var(--danger, #dc2626);
    color: white;
    border: 1px solid var(--danger, #dc2626);
    padding: 4px 12px;
    border-radius: 4px;
    cursor: pointer;
  }
  .attach {
    background: transparent; border: 1px solid var(--stroke);
    padding: 4px 8px; border-radius: 4px; cursor: pointer;
    font-size: 14px;
  }
  .attach:disabled { opacity: 0.5; cursor: not-allowed; }
  .attachments {
    display: flex; flex-wrap: wrap; gap: 4px;
    padding: 4px 8px; border-top: 1px solid var(--stroke);
    background: color-mix(in srgb, var(--fg, #000) 3%, transparent);
  }
  .attachment-chip {
    display: inline-flex; align-items: center; gap: 4px;
    padding: 2px 4px 2px 6px; border-radius: 3px;
    background: color-mix(in srgb, var(--accent, #22c55e) 10%, transparent);
  }
  .chip-x {
    background: none; border: none; padding: 0 4px;
    color: var(--muted); cursor: pointer; font-size: 14px;
  }
  .chip-x:hover { color: var(--danger, #dc2626); }
  .hint { color: var(--muted); font-size: 12px; padding: 8px; }
  .mono { font-family: "IBM Plex Mono", monospace; }
  .tiny { font-size: 11px; }
</style>
