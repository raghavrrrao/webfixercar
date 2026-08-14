# Renderer decision: Canvas 2D pseudo-3D, not Three.js

Phase 2B asked for a proper racing presentation and for the choice to be
argued rather than assumed. This is the argument.

## The decision

**Keep Canvas 2D, and replace the flat top-down view with a projected
pseudo-3D road** — a pinhole camera behind the car, segments of road projected
to a horizon, and every vehicle, sign and pickup drawn as a cached sprite
scaled by its distance.

## Why not Three.js

Three.js would be the right call for a game whose *gameplay* is
three-dimensional. This one's is not: the car moves along one axis and across
four lanes. Everything the brief asks for visually — depth, a road that
recedes, traffic that grows as it approaches, roadside scenery, a readable
racing camera — is exactly what a projected road gives, and it gives it
without:

* **a ~600 KB dependency vendored into `static/`.** There is no bundler in
  this project; three.js would have to be committed as a static file and
  served to every PC in the room on event day.
* **an asset pipeline.** GLB models need a loader, a fetch per model, and a
  decode. Every one of those is a thing that can fail on an event machine
  with a cold cache and a shared network.
* **GPU risk.** College lab PCs run integrated graphics. A 2D canvas blitting
  ~40 cached sprites per frame is safe on anything; a WebGL scene with
  shadow-mapped lights is not, and the failure mode is a participant's race
  running at 12fps during their one official attempt.
* **a rewrite of the game layer.** The physics, collision, repair and server
  code are tested and working. A 3D migration touches all of it.

The brief itself says not to migrate "simply because it sounds impressive",
and the honest reading is that the visual gap is a *rendering* gap, not a
dimensionality gap. The road looked like a dark rectangle because it was drawn
as a dark rectangle, not because it was drawn in 2D.

## Why the assets are procedural

The brief lists Kenney and Quaternius (both CC0) as preferred sources, and
also says not to download anything silently. This environment has no
guaranteed network access, so shipping a game that depends on files I could
not fetch and verify would be worse than shipping one that needs none.

Every sprite is therefore **drawn once into an offscreen canvas at load and
blitted thereafter** — cars, signs, billboards, pickups, lamp posts. That
costs a few milliseconds at startup, adds nothing to the download, cannot 404
on event day, and carries no licence question at all.

`static/game/` is laid out for real assets anyway, and
`static/game/README.md` documents exactly which Kenney packs to drop in and
where, if you would rather have them later. The sprite builders are isolated
in one section of `wf-race.js` so swapping a `drawImage` source is a local
change.

## What this buys, concretely

| Before | After |
| --- | --- |
| flat dark rectangle, top-down | projected road to a horizon, sky, scenery |
| cars as coloured bars | rear-view car sprites: body, glass, wheels, lights, shadow |
| one car shape | player car + six traffic types in varied colours |
| labels on rectangles | CSS billboards, gantries, road signs, barriers, lamps |
| pickups as bars | holographic CSS chips with glow, bob and a collect burst |
| no depth cues | distance scaling, horizon haze, parallax skyline |

## Performance budget

One `requestAnimationFrame` loop. Per frame: ~70 road quads, one sky/skyline
blit, and ~40 cached-sprite `drawImage` calls, all culled by draw distance.
No per-frame allocation in the hot path, no layout: the HUD is DOM but every
value is compared before it is written. Device pixel ratio capped at 2.
