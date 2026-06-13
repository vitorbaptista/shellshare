# Mobile room-viewer design

**Date:** 2026-06-13
**Status:** Approved design, ready for implementation plan
**Scope:** `templates/room.html` (viewer script) and `public/stylesheet/room.css`. No
server or CLI changes.

## Problem

The room viewer (`/r/:room`) is built around a fixed-grid terminal: the broadcaster's
`cols × rows` (typically 80 × 24) is authoritative and the page renders it with xterm.js.
On a portrait phone this creates a poor default experience, verified on a Pixel 9 (Chrome
and Brave, ~426 CSS-px wide) against a live broadcast over `adb reverse`:

1. **Horizontal clipping (core issue).** The default, non-fullscreen view renders 80 cols
   at the 14px base font ≈ 670px wide on a ~426px screen. The right ~25 columns are cut
   off and the viewer must scroll sideways to read full lines.
2. **Wasted vertical space.** That view pins the 24-row terminal to the top third of the
   screen with a large empty gap below; portrait height goes unused.
3. **Fullscreen portrait is unreadable.** The existing fullscreen font-fit *does* fit all
   80 cols, but only by shrinking the font to ~5px.
4. **Landscape fullscreen is genuinely good** — readable font, full width, full height —
   but it is gated behind two non-obvious manual steps: physically rotate the device *and*
   find the corner fullscreen toggle (50% opacity on touch).
5. **Browser + page chrome** (address bar, header, footer) consume scarce portrait height.

**There is no rendering or encryption bug on mobile.** Early blank-screen observations
during investigation were test-harness artifacts (a dropped `adb reverse` tunnel,
broadcasters exiting, and room TTL eviction). With a live broadcaster and the tunnel up,
both plaintext and encrypted rooms render crisply on the device.

The product already contains a good mobile experience (landscape-fullscreen). The design
goal is to make that the **obvious default** instead of something the viewer must discover.

## Direction

**Lean into landscape** (lowest-risk): treat landscape-fullscreen as the intended mobile
viewing mode and guide viewers into it, rather than re-engineering portrait terminal
rendering. Explicitly **out of scope**: pinch-to-zoom/pan, CSS content-rotation,
follow-the-cursor auto-pan, header/footer copy redesign.

## Platform constraints (the reason the design looks the way it does)

- Browsers **cannot** enter fullscreen or lock orientation on page load; both require a
  user gesture. "Automatic immersive" is therefore impossible — entry must be one tap.
- `screen.orientation.lock('landscape')` works on **Android Chrome** (only while
  fullscreen) but is **unsupported on iOS Safari**, which also has no element Fullscreen
  API. The viewer already ships a class-based fill-the-page fullscreen fallback for iOS;
  this design builds on it.

## Detection

Touch devices get the new behavior, gated on `window.matchMedia('(pointer: coarse)')`.
Fine-pointer (mouse/keyboard) devices — including touch laptops — keep today's behavior
unchanged: hover-reveal corner toggle, `f` to toggle fullscreen, native size, horizontal
scroll. The detection is re-evaluated on `fullscreenchange` and `resize` so a device that
never matches simply never shows the affordance.

## Behavior

### 1. Portrait landing — fit-to-width live preview + overlay (Variant A)

When a coarse-pointer device is **not** in fullscreen:

- **Fit-to-width preview.** The font auto-fits so all `cols` fit the viewport *width*
  (no horizontal scroll). This is a new branch of the existing `fitTerminal()` logic,
  which today scales only in fullscreen and otherwise pins `BASE_FONT_PX`. The not-
  fullscreen coarse-pointer branch fits to width only (height is allowed to overflow /
  scroll vertically), reusing the same proportional-scale + integer-px + overflow-nudge
  convergence the fullscreen path already uses, clamped at `MIN_FONT_PX`.
- **Full-terminal overlay.** A single tap target covering the terminal, layered above
  xterm's pointer-capturing overlays (the same z-index lesson as the corner toggle —
  park it far above xterm's internal layers). Contents, centered:
  - the expand icon (reuse the existing `.icon-expand` SVG),
  - label **"Tap to watch fullscreen"**,
  - sub-hint **"↻ rotates to landscape for readable text"**.
- The overlay is a real focusable control (`role="button"`, `aria-label`, keyboard
  activatable) for accessibility, even though touch is the primary path.
- The corner fullscreen toggle is **hidden** in this state; the overlay is the entry point.
- **Offline/idle broadcaster:** the overlay label is unchanged ("Tap to watch fullscreen")
  — one code path regardless of broadcaster state. The existing broadcast-status dot and
  user count continue to convey live/offline.

### 2. The tap → immersive

On activation, within the user-gesture handler, in order:

1. Enter fullscreen: native `requestFullscreen()` / `webkitRequestFullscreen()` where
   available, else add the `fullscreen` class (existing iOS fallback). This reuses the
   current `enterFullscreen()`.
2. Attempt `screen.orientation.lock('landscape')`; `.catch()` is a silent no-op (iOS,
   older Android, permission failure).
3. The existing fullscreen font-fit (`fitTerminal()` in its current fullscreen branch)
   produces the readable landscape view.

### 3. Rotate nudge (fallback for un-lockable platforms)

While in fullscreen **and** the orientation is still portrait (iOS, or Android where the
lock did not take), show a small dismissible-on-rotate nudge **"Rotate your phone ↻"**.
Driven by `@media (orientation: portrait)` scoped to the fullscreen state, so it auto-hides
the instant the device reports landscape. No JS polling of orientation required for the
hide; it is CSS-driven.

### 4. Exit

Exit via the OS back gesture, `Esc`, or the corner compress icon (shown while immersive,
as today). On exit:

- `screen.orientation.unlock()` (no-op where unsupported).
- Remove the `fullscreen` class (existing `exitFullscreen()` / `onFullscreenChange()`).
- The portrait preview + overlay return.

The existing `onFullscreenChange()` remains the single source of truth so OS/browser-driven
exits (swipe-down, Esc, browser UI) stay in sync; orientation unlock and overlay
restoration hang off the same path.

### 5. Screen Wake Lock (should-have, in scope)

On entering immersive, request `navigator.wakeLock.request('screen')` so the screen does
not sleep during a live broadcast. Release on exit. Re-acquire on `visibilitychange` when
the page returns to visible and is still immersive (wake locks are auto-released when a tab
is hidden). All calls guarded for absence of the API and wrapped so a rejection is a silent
no-op.

### 6. Desktop

Unchanged. None of the above triggers under a fine pointer.

## Affected code & boundaries

- `templates/room.html` (inline viewer script):
  - New: coarse-pointer detection helper; overlay element + activation handler; orientation
    lock/unlock; wake-lock acquire/release; fit-to-width-when-not-fullscreen branch in the
    fit logic.
  - Reused unchanged: WebSocket transport, decryption, theme, history replay, the
    `enterFullscreen`/`exitFullscreen`/`onFullscreenChange`/`fitTerminal` skeleton.
  - The overlay markup lives in the static template alongside `#fullscreen-toggle`;
    visibility is class/media-driven.
- `public/stylesheet/room.css`:
  - New: overlay styling, rotate-nudge (orientation media query within fullscreen),
    coarse-pointer/portrait visibility rules, hiding the corner toggle in the preview state.
  - The existing fullscreen layout block and `(hover: none)` toggle rules stay; the overlay
    rules sit beside them.

These are additive: the desktop and existing fullscreen paths keep their current structure,
and the new behavior is gated behind media queries and the coarse-pointer check.

## Testing

E2E is the source of truth (no Rust unit tests, per project convention). Add Playwright
coverage using mobile device emulation (portrait viewport + `hasTouch`):

- Portrait preview fits to width (no horizontal overflow; font < base).
- The tap-to-watch overlay is present and is the active tap target on a coarse-pointer
  viewport, and absent on a desktop viewport.
- Activating the overlay adds the `fullscreen` class and triggers the fullscreen font-fit
  (native fullscreen + orientation lock are not assertable headless; assert the class-based
  fallback path and that lock is *attempted* — e.g. via a stub/now-throws-caught).
- Rotate-nudge visibility flips with the emulated orientation while fullscreen.
- Existing desktop viewer tests stay green (regression guard for the unchanged path).

Manual on-device verification on the Pixel 9 over `adb reverse` during implementation:
real native fullscreen, real `orientation.lock`, real wake lock, and the portrait→tap→
landscape flow end to end.

## Risks & mitigations

- **`orientation.lock` rejects without fullscreen / on unsupported platforms** → always
  `.catch()` to no-op; the rotate nudge covers the un-lockable case.
- **Overlay losing the hit-test to xterm's layers** → place it above xterm's internal
  z-indexes (the `#147`/`#148` lesson), and e2e-assert it receives the tap.
- **Fit-to-width changing layout for unexpected viewports** → gate strictly on
  `(pointer: coarse)` and re-evaluate on resize; desktop path untouched.
- **Wake lock auto-released on tab hide** → re-acquire on `visibilitychange`.
