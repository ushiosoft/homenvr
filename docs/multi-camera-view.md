# Multi-camera view — decisions

homenvr works with any number of cameras, from one up. This records how the Cameras page lays them
out and why, so the choices can be reviewed and changed in one place
(`layoutGrid()` and the `.camgrid` styles in `app/static/index.html`).

## Backend

Nothing assumed three cameras: cameras are a list, NVR stream paths come from the
channel number (channel 12 -> `/Streaming/Channels/1201`), and every API iterates the
list. `config.load()` now refuses a config file with no cameras.

## Grid layout (desktop)

| Cameras | Columns | Why |
| --- | --- | --- |
| 1 | 1 | the camera at full width |
| 2–3 | one row | side by side, as before |
| 4+ | `ceil(sqrt(n))` | the smallest square that holds them: 4 -> 2x2, 5–9 -> 3 wide, 10–16 -> 4 wide, 17–25 -> 5 wide |

- The square rule keeps the grid about as tall as it is wide whatever the count (16
  cameras: 4 rows, ~550 px, instead of 6 rows / 1100 px with the old 300 px auto-fit).
- **Portrait cameras** (rotation 90/270) take two rows of their column, sized to exactly
  two landscape cells plus the gap, so rows keep the landscape height; the picture is
  pillarboxed inside. `grid-auto-flow: dense` fills the holes this leaves.
- **One portrait + two landscape** keeps its dedicated arrangement (portrait column
  9fr : landscape column 14fr, heights lining up) — it looked right already.
- **All portrait:** tall tiles side by side, up to six across.

## Focus layout (desktop)

- Up to 3 other cameras: unchanged — the selected camera large, the others in a 300 px
  column beside it.
- 4 or more others: a **strip** — the selected camera full width, the others below it,
  up to six across, wrapping. (A side column of 4+ tiles grows taller than the main
  picture; at 7 others it was ~1200 px.) Strip tiles hide their controls and badge;
  clicking one selects it.
- With one camera the Grid/Focus switch is hidden (both would look the same).

## Phone

Always the focus arrangement: the selected camera full width, the others as chips
below it, **up to four per row, wrapping** onto more rows. (Before, all chips shared one
row: with five or more cameras they were 1 px wide.)

## Wall display

Unchanged rule (`ceil(sqrt(n))` columns, rows filling the screen); portrait cameras
span two rows as in the grid.

## Elsewhere

- Incident cards show up to three camera snapshots, then a **+N** chip for the rest.
- Saved clips of several cameras: up to three side by side as before; four or more as a
  grid of 16:9 thumbnails (four -> 2x2, more -> three across).
- The saved-clip player's camera picker wraps onto more rows.
- The review sync chip is hidden with one camera ("1 cameras in sync" said nothing).

## Not done yet (open)

- **Many cameras and load.** Every tile streams all the time: live, one WebRTC/HLS
  stream per camera in the browser; review, one playback request per camera (the
  "Compatible" quality transcodes on the server per camera). Fine for a handful of
  cameras; for 9+ it would be worth streaming only the tiles on screen
  (IntersectionObserver) or paging the grid (e.g. 9 per page).
- **Timeline** has one lane per camera; with many cameras it simply gets taller.
  Collapsing lanes without events would be the next step if that gets unwieldy.
- **Camera order** is the order in `config.yml`; there's no drag-to-reorder.

## How this was checked

A local harness ran the app with N fake cameras (ffmpeg test-pattern HLS per camera)
and headless Edge measured tile positions and took screenshots at 1440×900 and
390×844: 1, 2, 3 (incl. 1 portrait + 2 landscape), 4, 5, 6 (with 2 portrait),
4 all-portrait, 8 (grid and focus), 16, the wall display at 3/5/16, and the incident,
saved and player views with 6 cameras.
