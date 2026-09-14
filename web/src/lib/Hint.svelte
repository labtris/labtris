<script>
  //: A section heading carries a `?` rather than a paragraph of prose. The
  //: explanation is still there for whoever wants it, but it costs a hover
  //: instead of permanent vertical space in a narrow palette.
  let { text } = $props();
  let open = $state(false);
</script>

<button
  class="hint-dot"
  type="button"
  aria-label="What is this?"
  onpointerenter={() => (open = true)}
  onpointerleave={() => (open = false)}
  onfocus={() => (open = true)}
  onblur={() => (open = false)}
  onclick={() => (open = !open)}
>?</button>
{#if open}
  <span class="hint-pop" role="tooltip">{text}</span>
{/if}

<style>
  .hint-dot {
    width: 15px; height: 15px; padding: 0; margin-left: 6px;
    border-radius: 999px; border: 1px solid var(--stroke);
    background: transparent; color: var(--muted);
    font-size: 10px; line-height: 1; cursor: help; vertical-align: middle;
  }
  .hint-dot:hover, .hint-dot:focus-visible { color: var(--accent); border-color: var(--accent); }
  .hint-pop {
    position: absolute; z-index: 60; left: 8px; right: 8px; margin-top: 18px;
    padding: 8px 10px; border-radius: 8px;
    border: 1px solid var(--stroke); background: var(--panel);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.28);
    font-size: 11px; font-weight: 400; line-height: 1.45;
    color: var(--muted); text-transform: none; letter-spacing: normal;
  }
</style>
