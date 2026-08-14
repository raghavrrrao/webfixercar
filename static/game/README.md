# Game assets

**The game currently ships with no asset files, and needs none.** Every
sprite — the cars, the CSS hazards, the repair chips, the road signs, the
street furniture, the skyline — is drawn once into an offscreen canvas at
load by `static/js/wf-race.js` and blitted from then on.

That is a deliberate choice, argued in `static/js/RENDERER-DECISION.md`:
nothing to download on event day, nothing to 404 on a cold cache, no licence
question, and no decode cost. The whole sprite set builds in a few
milliseconds.

This directory exists so that swapping in real artwork later is a drop-in
rather than a refactor.

## If you want to use real artwork

The sprite builders live in one section of `wf-race.js` (`// 4. sprites`).
Each one returns a canvas, and everything downstream only ever calls
`ctx.drawImage(...)` on it. To use images instead, replace the body of
`buildVehicleSprites()` / `buildPropSprites()` / `buildHazardSprites()` with
loaded `Image` objects and keep the same keys in the `sprites` map:

```
sprites.player          sprites.sedan   sprites.hatch   sprites.sports
sprites.van             sprites.taxi    sprites.truck
sprites['<name>:brake'] – brake-light variant of each vehicle
sprites['pickup:<repair id>']           – one per CSS repair
sprites['hazard:<section index>']       – one per course section, 0-6
sprites['sign:<section>:<n>']  sprites.lamp  sprites.barrier  sprites.block
```

Load them before `booted = true` is set, or the first frames will draw
nothing. Vehicle sprites are drawn rear-view (the camera is behind the car)
and should be **wider than they are tall** — roughly 170 × 140 — or the car
renders like a bus standing on its end.

## Recommended sources — all CC0

| Pack | URL | Use for |
| --- | --- | --- |
| Kenney Car Kit | <https://kenney.nl/assets/car-kit> | player and traffic vehicles |
| Kenney Racing Kit | <https://kenney.nl/assets/racing-kit> | barriers, cones, gantries |
| Kenney Road Textures | <https://kenney.nl/assets/road-textures> | tarmac and lane markings |
| Kenney 3D Road Tiles | <https://kenney.nl/assets/3d-road-tiles> | road furniture |
| Quaternius Cars | <https://quaternius.com/packs/cars.html> | vehicles |

All of the above are **CC0 / public domain** — no attribution required,
though crediting Kenney and Quaternius is good manners.

**Do not** commit assets from any other source without checking the licence.
Nothing in this repository should carry a commercial game licence.

## Where to put them

```
static/game/
  cars/          player.png, sedan.png, hatch.png, sports.png, van.png,
                 taxi.png, truck.png  (+ *-brake.png)
  roads/         tarmac.png, rumble.png, markings.png
  environment/   lamp.png, barrier.png, building.png, sign-*.png
  pickups/       responsive.png, display.png, margin.png, padding.png,
                 flexbox.png, position.png, grid.png
  audio/         (see below)
```

Reference them through `{% static %}` in the template and pass the URLs into
`wf-race-config`, the same way the course is passed — the script should not
hardcode paths.

## Audio

Also fully procedural: engine, collision, repair chime, countdown and finish
are synthesised with the Web Audio API in `wf-race.js`, with a mute toggle in
the HUD that persists to `localStorage`. There is no external audio service
and no file to download. If you later want recorded audio, put it in
`static/game/audio/` and use CC0 sources such as
<https://kenney.nl/assets/interface-sounds> or <https://freesound.org> —
checking each individual licence.
