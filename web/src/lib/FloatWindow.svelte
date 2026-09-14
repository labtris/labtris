<script>
  // Draggable, resizable window chrome. One of these per open console, screen
  // or capture, so several nodes can be watched at once — the single shared
  // dock could only ever show one, and showed it in a strip too short to see
  // a 1280x800 desktop in.
  let { win, onclose, onfocus, children } = $props();

  let el = $state(null);
  let drag = null;

  function begin(e, mode) {
    if (e.button !== 0) return;
    // The minimise and close buttons live in the drag bar. preventDefault on
    // pointerdown suppresses the compatibility click that follows, so without
    // this the buttons are inert.
    if (e.target.closest?.("button")) return;
    e.preventDefault();
    onfocus?.(win.id);
    drag = {
      mode,
      px: e.clientX,
      py: e.clientY,
      x: win.x,
      y: win.y,
      w: win.w,
      h: win.h,
    };
    try {
      e.currentTarget.setPointerCapture?.(e.pointerId);
    } catch {
      /* capture unsupported */
    }
  }

  function move(e) {
    if (!drag) return;
    const dx = e.clientX - drag.px;
    const dy = e.clientY - drag.py;
    if (drag.mode === "move") {
      // Keep the title bar reachable: a window dragged off the top edge can
      // never be grabbed again.
      win.x = Math.max(0, drag.x + dx);
      win.y = Math.max(0, drag.y + dy);
    } else {
      win.w = Math.max(320, drag.w + dx);
      win.h = Math.max(180, drag.h + dy);
    }
  }

  function end() {
    drag = null;
  }
</script>

<svelte:window onpointermove={move} onpointerup={end} onpointercancel={end} />

<div
  bind:this={el}
  class="floatwin"
  class:min={win.min}
  style={`left:${win.x}px; top:${win.y}px; width:${win.w}px; height:${win.min ? 34 : win.h}px; z-index:${win.z}`}
  onpointerdown={() => onfocus?.(win.id)}
>
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="fw-bar" onpointerdown={(e) => begin(e, "move")}>
    <span class="fw-kind">{win.kind}</span>
    <span class="fw-title">{win.title}</span>
    <span class="fw-status">{win.status ?? ""}</span>
    <button class="fw-btn" title="minimise" onclick={() => (win.min = !win.min)}>
      {win.min ? "▣" : "—"}
    </button>
    <button class="fw-btn" title="close" onclick={() => onclose?.(win.id)}>✕</button>
  </div>
  {#if !win.min}
    <div class="fw-body">{@render children?.()}</div>
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="fw-grip" onpointerdown={(e) => begin(e, "size")}></div>
  {/if}
</div>

<style>
  .floatwin {
    position: absolute;
    display: flex;
    flex-direction: column;
    background: var(--panel);
    border: 1px solid var(--stroke);
    border-radius: 12px;
    box-shadow: var(--shadow);
    overflow: hidden;
    min-width: 320px;
  }
  .fw-bar {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 8px;
    background: rgba(255, 255, 255, 0.04);
    border-bottom: 1px solid var(--stroke);
    cursor: move;
    user-select: none;
    flex: 0 0 auto;
  }
  .fw-kind {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--accent);
  }
  .fw-title {
    font-size: 12px;
    font-weight: 600;
    flex: 1;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .fw-status {
    font-size: 10px;
    color: var(--muted);
  }
  .fw-btn {
    padding: 0 6px;
    font-size: 11px;
    line-height: 18px;
    border-radius: 5px;
  }
  .fw-body {
    flex: 1;
    min-height: 0;
    display: flex;
    overflow: hidden;
  }
  .fw-grip {
    position: absolute;
    right: 0;
    bottom: 0;
    width: 16px;
    height: 16px;
    cursor: nwse-resize;
    /* The VNC canvas fills the body and would otherwise swallow the grab. */
    z-index: 5;
    background: linear-gradient(
      135deg,
      transparent 0 50%,
      var(--stroke) 50% 60%,
      transparent 60% 75%,
      var(--stroke) 75% 85%,
      transparent 85%
    );
  }
</style>
