---
name: handheld-crayon-live-photo
description: 'From one photo of a hand holding a drink (or any held object), produce a native Apple Live Photo: the hand starts empty, a chunky crayon version of the drink is drawn on stroke by stroke, then it settles into the real photo. Static state shows the real photo; long-press plays the drawing. Covers colour-block crayon keyframe prompting, deriving the empty hand without pose drift, the deterministic stroke-by-stroke compositing script, and packaging the result as a genuine paired Live Photo (HEIC/JPEG + MOV sharing one content identifier, with a still-image-time track) verified through PhotoKit. Trigger phrases include live 图, live photo, 实况照片, 手绘风 live, 蜡笔画出来, 涂鸦动画, 会动的涂鸦, 手持饮料, crayon live photo.'
agent_created: true
---

# Handheld Crayon Live Photo

Input: **one photo of a hand holding a drink** (or any handheld object).
Output: **a native Apple Live Photo** in which the hand starts empty, a chunky
crayon version of the drink is coloured in stroke by stroke, and the drawing then
settles into the real photo.

At rest the Live Photo shows the **finished crayon drawing** (the cover frame).
Long-press (or the lock-screen press) plays the whole sequence: empty hand →
stroke-by-stroke drawing → morph → the real photo.

## Non-negotiable output contract

Deliver a **paired resource set**, never a loose file:

| File | Role |
|---|---|
| `*.HEIC` (or `*.JPG`) | the still image — carries `MakerApple[17]` = the asset identifier |
| `*.MOV` | the paired video — carries `com.apple.quicktime.content.identifier` (same value) **and** a timed `com.apple.quicktime.still-image-time` track |

All three markers must be present. **A plain MP4, GIF, JPEG or an unpaired
image+video is not a Live Photo and must not be described as one.** The only
accepted proof is PhotoKit reporting `mediaSubtypes` containing `.photoLive`
together with a `.photo` + `.pairedVideo` resource pair.

## Method

Four stages. Stages 1–2 are image-generation calls; 3–4 are deterministic
scripts with no model calls.

### Stage 1 — crayon keyframe (image-to-image, 1 generation)

Input: the source photo. Rewrite the template in
`references/methodology.md`, keeping these rules:

- replace the drink with a **crayon version of the same drink**;
- **match the drink's own colour regions** — bottle/cap/label/liquid each keep
  their own colour. Never flatten the object into one colour;
- **large flat colour blocks, thick chunky strokes, deliberately drop small
  detail** (no fine print, no highlights, no thin linework). A cartoonish,
  low-detail render is what makes the reveal read as *colouring*; a faithful
  render reads as a slow dissolve;
- wobbly edges overflowing the silhouette, dry waxy texture, no black outlines;
- hand, arm, background, lighting and camera angle unchanged.

`input_fidelity: high`; drop to `medium` if the model refuses to simplify.

### Stage 2 — empty keyframe (image-to-image, 1 generation)

Input: **the crayon keyframe**, never the source photo. Ask for the crayon
object to be erased and the background filled back in, framed as a **pure
object-removal inpainting task**: every finger must stay exactly where it is
in the input (same position, length, direction, separation and bend), and the
prompt must **not name any pose**. Never write "fingers curled" or "do not
open the palm" — grips differ (a narrow bottle is gripped with curled fingers,
a wide cup is held with the fingers fanned flat across its front), and
prescribing a canonical grip makes the model invent a fist or an open palm
that never existed in the source. Deriving from the crayon keyframe (not the
source) keeps framing and scale identical to the frame it will be diffed
against, so only the object region needs to change.

### Stage 3 — compose the animation

```bash
python3 scripts/compose_live.py \
    --source  <real photo> \
    --crayon  <crayon keyframe> \
    --empty   <empty-hand keyframe> \
    --out live.mp4 --gif live.gif \
    --max-seconds 4.6
```

`--max-seconds` is **required for Live Photo output**: it scales every stage
proportionally so the clip fits inside the ~5 s Live Photo ceiling (4.6 s is a
safe budget — the packager trims again as a backstop, but a clip that was
already budgeted keeps its pacing).

The script handles, with no manual tuning: fit-crop to a shared canvas,
watermark patch, background-only alignment of the two generated frames,
auto-location of the object centre, silhouette extraction by connected
component, a boustrophedon pen path, an arc-length "order field", the
stroke-by-stroke reveal, and the timeline
empty hold → draw → crayon hold → morph → real hold. The crayon hold is
**still by default**; `--boiling` adds hand-drawn wobble.

Read the printed object centre and silhouette size before moving on — the
script warns when the silhouette looks implausible.

It also prints the **cover frame** — the complete crayon drawing, taken from the
crayon keyframe — and writes it as `<out stem>-cover.png`:

```
cover frame 78 @ 2.6167s  ->  stage 4: --still-time 2.6167   (complete drawing, residual 0.000)
cover png -> live-cover.png
```

That timestamp is the one stage 4 needs. `--cover-png` moves the file,
`--no-cover-png` skips writing it (the time is still printed).

The cover comes from the **keyframe, not from the last frame of the reveal**:
the reveal is a soft-edged ramp, and pixels the brush never crosses can only
reach half opacity there — on a still image that reads as missing colour
blocks. `residual` is the mean `|cover − crayon|`; expect 0.000.

### Stage 4 — package as a native Live Photo

```bash
python3 scripts/make_livephoto.py \
    --video live.mp4 --out-dir pair/ --jpeg \
    --still-time <cover time printed by stage 3>
```

1. Rewraps the clip into a `.mov` (lossless passthrough, no re-encode), capped
   at `--max-duration` (default 4.9 s).
2. Writes the movie-level content identifier.
3. Appends a timed metadata track carrying `still-image-time` at the **cover
   time** — the drawing-complete frame the still image represents.
4. Extracts that exact frame out of the MOV and writes it as HEIC (and
   optionally JPEG) with `MakerApple[17]` set to the same identifier, so the
   still and the video can never disagree.
5. Reads the pair back with AVFoundation and prints the identifier, the still
   time and the track list.
6. Creates a `PHAsset` from the two resources and reports
   `is_photoLive` / `playbackStyle` / the resource list.

`RESULT: PASS` requires: identifiers match on both sides, the still-image-time
track is present with a real timestamp, duration ≤ 5 s, and PhotoKit reports
`.photoLive` with a `.pairedVideo` resource.

#### PhotoKit authorisation (macOS) — read this before running stage 4

PhotoKit is gated by TCC and fails in two silent ways:

- **running under a host without `NSPhotoLibraryUsageDescription`** →
  `requestAuthorization` returns Denied immediately, no dialog;
- **running as a bare CLI process** → the reply is queued on the main queue
  nobody drains, so the call never returns.

So run stage 4 inside a generated app bundle:

```bash
python3 scripts/make_verifier_app.py --out /tmp/LPVerify.app --run -- \
    python3 scripts/make_livephoto.py --video live.mp4 --out-dir pair/
open -a /tmp/LPVerify.app          # grant access in the dialog
tail -f /tmp/lpverify.log          # watch the result
```

**Use absolute paths in the wrapped command** — an app bundle's working
directory is `/`, so relative paths fail. Once granted, the decision sticks for
that bundle, so later runs need no dialog. `--no-import` skips PhotoKit
entirely and only builds and verifies the files.

**Re-running with different arguments: rebuild the bundle.** `open -a
LPVerify.app --args ...` does **not** replace the command embedded at build
time — the app runs what it was built with, so a changed `--still-time`,
`--out-dir` or any other flag silently does not apply (and the log's first
line, which echoes the command, is the only tell). Re-run
`make_verifier_app.py --out <new.app> --run -- <full command>` instead.

## Requirements

- macOS. Python 3 with `pillow numpy imageio imageio-ffmpeg` (stage 3) and
  `pyobjc-framework-Cocoa AVFoundation Photos Quartz UniformTypeIdentifiers`
  (stage 4). There is no separate `pyobjc-framework-ImageIO` package — ImageIO
  lives inside `Quartz`.
- `ffmpeg`, Xcode and the Command Line Tools are **not** required. If
  Command Line Tools are missing, `swift`/`clang`/`git` are unusable
  (`xcrun: invalid active developer path`) — the PyObjC route still works.
- Image generation costs credits: **two** generations per Live Photo.

## Extensibility

- **Any held object.** Only the Stage 1 colour regions change. Nothing in the
  scripts knows what a drink is — the silhouette is "where the two frames
  differ", and the object centre is detected at runtime.
- **Any aspect ratio.** `--canvas-ratio 9:16` (default), `1:1`, `4:5`, `16:9`
  plus `--canvas-w`. Keep portrait sources at `9:16`; a portrait source at
  `16:9` would be cropped away.
- **Any pacing.** `--draw`, `--hold-empty`, `--hold-crayon`, `--morph`,
  `--hold-real` are frame counts; `--fps` the rate. Re-budget with
  `--max-seconds` instead of hand-tuning.
- **Any stroke density.** `--step-y` (row spacing) and `--brush-r`.
- **Other drawing styles.** Swap the Stage 1 wording for marker pen, oil
  pastel, chalk or flat vector; stages 2–4 are unchanged.
- **Still image choice (the cover).** The cover is the **drawing-complete
  frame**, not the last frame. At rest the asset should show the finished
  crayon drawing — that is the payoff of the effect, and a resting frame of the
  plain photo hides it. Stage 3 prints the exact `--still-time`; leave it at
  the default last frame only if you deliberately want the plain photo as the
  cover (`--still-png` overrides the still source entirely).
- **Escape hatches.** `--seed-x/--seed-y` override the auto object centre,
  `--diff-thresh` the silhouette threshold, `--no-align` skips alignment.
  Only reach for them when the printed warnings say the auto values failed.

## Pitfalls

- **Duration over 5 s.** Photos ignores over-long paired video. Budget with
  `--max-seconds` at stage 3; the packager trims as a backstop but trimming
  changes the ending.
- **Saving the two files separately on iOS.** A Live Photo is only created when
  both resources are submitted in one write. Saving the image and the video as
  two separate items yields a static photo plus an unrelated video. Transferring
  an already paired Live Photo (AirDrop from the Photos app, or iCloud Photos
  sync) preserves it; transferring two bare files may not.
- **Re-encoding or renaming away the markers.** Third-party apps that re-save
  the image strip `MakerApple[17]` and break the pair.
- **Background colour drift** between the two generations leaks into the
  silhouette; raise `--diff-thresh` if the "silhouette very large" warning fires.
- **Over-detailed crayon keyframe** turns the reveal into a slow desaturation.
  Force large flat blocks.
- **Watermark** burned into generated frames, flush to the bottom-right corner.
  Patched by default; eyeball one crayon-hold frame before delivering.
- **Canonical-pose drift in the empty hand.** Naming a pose in Stage 2 (any
  pose, including "fingers curled" / "do not open the palm") makes the model
  substitute a stereotyped "holding" hand — usually a fist — that is not the
  grip in the source. Keep Stage 2 pose-agnostic and lock per-finger geometry
  instead. Derive from the crayon keyframe, never the source.

## Bundled resources

- `scripts/compose_live.py` — stage 3, the deterministic compositor.
- `scripts/make_livephoto.py` — stage 4, native Live Photo packaging +
  verification.
- `scripts/make_verifier_app.py` — builds the app bundle needed for PhotoKit
  authorisation.
- `references/methodology.md` — prompt templates, composition algorithm,
  parameter recipe, troubleshooting.
- `references/live-photo-spec.md` — exactly which bytes make a pair a Live Photo,
  and how each is verified.
