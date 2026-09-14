<script>
  //: A right-click menu over the canvas. It holds no knowledge of what the
  //: items do — the caller has already resolved them against the thing that
  //: was clicked, so this only has to place itself on screen and stay there
  //: until something dismisses it.
  //: `filter` turns the menu into a picker: a list long enough to need
  //: searching (every lab on the instance) is still a menu, not a dialog.
  let { x = 0, y = 0, title = "", items = [], filter = "", onclose } = $props();

  let needle = $state("");
  const shown = $derived.by(() => {
    const q = needle.trim().toLowerCase();
    if (!q) return items;
    //: Headers and separators only earn their place if something under them
    //: survived the filter, so they are re-derived rather than filtered.
    const kept = items.filter((i) => !i.sep && !i.header && i.label?.toLowerCase().includes(q));
    return kept.length ? kept : [{ header: "no match" }];
  });

  let el = $state(null);
  let inputEl = $state(null);
  let pos = $state({ x, y });

  //: Opened near the right or bottom edge, a menu that runs off-screen is
  //: unusable — flip it back inside once its real size is known.
  $effect(() => {
    if (!el) return;
    //: Re-measuring on every filter keystroke would make the menu walk up
    //: the screen as it shrinks. Position is decided once, when it opens.
    if (pos.settled) return;
    const r = el.getBoundingClientRect();
    const nx = x + r.width > window.innerWidth - 8 ? Math.max(8, x - r.width) : x;
    const ny = y + r.height > window.innerHeight - 8 ? Math.max(8, y - r.height) : y;
    pos = { x: nx, y: ny, settled: true };
    (inputEl ?? el)?.focus();
  });

  function run(item) {
    if (item.disabled) return;
    onclose?.();
    item.run?.();
  }
</script>

<svelte:window
  onkeydown={(e) => e.key === "Escape" && onclose?.()}
  onresize={() => onclose?.()}
/>
<!-- A click anywhere outside dismisses it, including on the canvas beneath. -->
<div
  class="menu-scrim"
  onpointerdown={(e) => {
    e.preventDefault();
    e.stopPropagation();
    onclose?.();
  }}
  oncontextmenu={(e) => {
    e.preventDefault();
    onclose?.();
  }}
></div>
<div
  class="menu"
  bind:this={el}
  tabindex="-1"
  role="menu"
  style={`left:${pos.x}px; top:${pos.y}px`}
  onpointerdown={(e) => e.stopPropagation()}
  oncontextmenu={(e) => e.preventDefault()}
>
  {#if title}<div class="menu-title mono">{title}</div>{/if}
  {#if filter}
    <input
      class="menu-filter"
      bind:this={inputEl}
      bind:value={needle}
      placeholder={filter}
      onkeydown={(e) => {
        if (e.key === "Escape") { e.stopPropagation(); onclose?.(); }
        //: Enter takes the only remaining match — the whole point of typing.
        if (e.key === "Enter") {
          const only = shown.filter((i) => !i.sep && !i.header);
          if (only.length === 1) run(only[0]);
        }
      }}
    />
  {/if}
  {#each shown as item}
    {#if item.sep}
      <div class="menu-sep"></div>
    {:else if item.header}
      <div class="menu-head">{item.header}</div>
    {:else}
      <button
        role="menuitem"
        class:danger={item.danger}
        disabled={item.disabled}
        title={item.hint || ""}
        onclick={() => run(item)}
      >
        <span class="menu-glyph">{item.glyph || ""}</span>
        <span>{item.label}</span>
        <!-- Shortcuts and the reason an item is dimmed both belong on the
             row, not in a tooltip you have to hover to discover. -->
        {#if item.hint}<span class="menu-hint">{item.hint}</span>{/if}
      </button>
    {/if}
  {/each}
</div>

<style>
  .menu-scrim { position: fixed; inset: 0; z-index: 900; }
  .menu {
    position: fixed; z-index: 901; min-width: 186px; max-height: 70vh; overflow-y: auto; padding: 5px;
    border-radius: 10px; border: 1px solid var(--stroke);
    background: var(--panel-solid, var(--panel));
    box-shadow: 0 14px 40px rgba(0, 0, 0, 0.34);
    outline: none;
  }
  .menu-title {
    padding: 5px 9px 7px; font-size: 10px; color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.08em;
    border-bottom: 1px solid var(--stroke); margin-bottom: 4px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .menu button {
    display: flex; align-items: center; gap: 9px; width: 100%;
    padding: 6px 9px; border: 0; border-radius: 6px; background: transparent;
    color: var(--text); font-size: 12px; text-align: left; cursor: pointer;
  }
  .menu button:hover:not(:disabled) { background: color-mix(in srgb, var(--accent) 18%, transparent); }
  .menu button:disabled { color: var(--muted); opacity: 0.55; cursor: default; }
  .menu button.danger { color: var(--danger); }
  .menu button.danger:hover:not(:disabled) { background: color-mix(in srgb, var(--danger) 16%, transparent); }
  .menu-glyph { width: 14px; text-align: center; font-size: 12px; opacity: 0.85; }
  .menu-hint {
    margin-left: auto; padding-left: 14px; font-size: 10.5px;
    color: var(--muted); white-space: nowrap;
  }
  .menu-sep { height: 1px; margin: 4px 6px; background: var(--stroke); }
  .menu-filter {
    width: 100%; margin-bottom: 4px; padding: 6px 9px; font-size: 12px;
    color: var(--text); background: var(--bg-2);
    border: 1px solid var(--stroke); border-radius: 7px;
  }
  .menu-head {
    padding: 6px 9px 3px; font-size: 9px; color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.08em;
  }
</style>
