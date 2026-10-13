# Crayon Live Image — Methodology

This reference documents the method behind the `handheld-crayon-live-photo` skill:
the exact generation prompt templates, the composition algorithm, the parameter
recipe, the native Live Photo packaging step, and the pitfalls discovered while
building it.

The deliverable is a **native Live Photo pair**, not a video file. Packaging and
verification are specified in `references/live-photo-spec.md`.

It is deliberately **file-agnostic and subject-agnostic**. The three images are
always referred to as "the real photo", "the crayon keyframe" and "the empty-hand
keyframe"; the object is always `<OBJECT>`. Nothing here assumes a particular
drink, colour, prop or camera angle — substitute for the photo in hand and the
pipeline is unchanged.

## The three keyframes

| Frame | What it is | How to make it |
|-------|-----------|----------------|
| real  | original photo, hand holding `<OBJECT>` | the source photo |
| crayon | same hand, `<OBJECT>` rendered as crayon colour blocks | image-to-image: real photo (+ optional style ref) |
| empty | same hand, `<OBJECT>` removed, every finger in its exact input position | image-to-image: **the crayon keyframe** + "erase the object" (pose-agnostic inpainting) |

### Why the empty hand is derived from the crayon keyframe (not the source)

The empty keyframe is an **object-removal inpainting task, not a re-posing
task**. Deriving it from the crayon keyframe (rather than the source) keeps
framing, background and hand scale identical to the frame it will be diffed
against, and reduces the model's job to refilling the object region only — the
fewer pixels that change, the less likely the hand is to move.

The cardinal rule: **never prescribe a pose**. Hands hold objects in many
ways — a narrow bottle is gripped with curled fingers, a wide cup or plate is
held with the fingers fanned flat across its front, a phone is pinched. If the
prompt says "fingers stay curled" or "do not open the palm", the model obeys
the pose words and substitutes a canonical gripping hand (typically a fist)
that never existed in the source — anatomically wrong for a fanned grip, and
visibly discontinuous when empty → crayon → real plays. Instead, lock the
**geometry**: every finger keeps its input-image position, length, direction,
separation and bend, whatever grip that happens to be.

## Stage 1 prompt template (crayon keyframe, colour-matched, blocky)

Fill in the angle brackets from the photo. The two bracketed lines are the ones
that matter most: enumerate *this* object's own colour regions, and demand
simplification.

```
Edit the first image. The first image is a photo of a hand holding <OBJECT> in
front of <BACKGROUND DESCRIPTION>.
Replace the real <OBJECT> with the same <OBJECT> rendered as a very simple, chunky
CARTOON crayon drawing made of large flat colour blocks.
Use only a few big regions: <COLOUR REGION 1 — e.g. "one solid <colour> block for
the cap">, <COLOUR REGION 2 — e.g. "one solid <colour> block for the body">,
<COLOUR REGION 3 — e.g. "a <colour> block for the label">.
CRITICAL: match the <OBJECT>'s OWN original colour regions — do NOT paint the whole
<OBJECT> one solid colour.
Deliberately IGNORE fine detail: no small printed text, no fine label typography,
no highlights, no fine linework; simplify any small printed graphic down to a couple
of crude scribbled shapes.
Use very THICK, chunky, blunt crayon strokes with a dry grainy waxy texture, visible
streaky pressure streaks, wobbly edges that clearly overflow the <OBJECT> silhouette,
hand-drawn children's colouring-book feel.
NO black outlines, NO line art, NO smooth shading gradients.
Keep the hand, fingers, skin tone, nails, sleeve, background, lighting and camera
angle EXACTLY the same. The hand and background stay photorealistic.
```

Notes

- **Second input (optional)**: a reference image that only demonstrates the crayon
  *look*. The style reference never contributes content — say so in the prompt, or
  the model copies its subject.
- `input_fidelity: high` keeps the hand and background faithful. Use `medium` if
  the model is being too literal and refusing to simplify.
- The blocky, low-detail instruction is not a compromise — it is what makes the
  reveal read as *colouring* rather than as a slow dissolve. A faithful, busy
  crayon render reveals fine detail progressively and loses the effect.
- Generate at the largest sensible size (portrait sources: 2:3 or 9:16) so the
  empty keyframe and the alignment search have pixels to work with.

## Stage 2 prompt template (empty keyframe, pose-agnostic inpainting)

Input: the **crayon keyframe**. Keep the object description to a minimum so the
model does not invent a replacement object. Do not name any hand pose —
describe the lock geometrically instead.

```
Edit the first image. Erase only the crayon-drawn <OBJECT>, refilling those
pixels with the existing background. This is pure object removal, not a re-pose.
The hand must not change in any way. Every finger stays exactly where it is in
the input image: same position, same length, same direction, same gaps between
the fingers, same knuckle bend, same skin folds and shading. Whatever grip the
hand has in the input is the grip it keeps.
Do NOT substitute a generic "holding something" pose: do NOT curl the fingers
up into a fist or closed grip, do NOT straighten them into a flat open palm,
do NOT spread them wider or squeeze them together. Fingers that lie fanned out
across the object stay fanned out; fingers that are curled stay curled.
Keep the arm, wrist, sleeve, nails, ring, background, lighting, camera angle
and framing exactly the same. Photorealistic hand and background.
Leave no drawn, sketchy, crayon or scribble residue anywhere in the image.
```

Notes

- **Never write "fingers curled" / "gently curled" / "do not open the palm" as
  the target pose.** Earlier versions of this template did, and it caused a
  real failure: on a wide cup held with the fingers fanned flat across its
  front, the model obeyed the pose words and curled the hand into a fist —
  anatomically impossible for that grip and clearly discontinuous with the
  real photo. The only correct pose is the one in the input image; state the
  lock per finger, never by pose name.
- The "do NOT curl / do NOT straighten" lines are *guardrails against both
  canonical poses* (fist and open palm), not a description of the desired
  pose. Keep both halves; dropping the fist half reintroduces the failure.
- If the model still re-poses the hand, add the observable facts of *this*
  grip as observations, not instructions — e.g. "the four fingers lie fanned
  out across where the object was, fingertips apart, thumb vertical on the
  left". Describing what is visible is safe; prescribing what the pose should
  be is not.

## Composition method (the bundled script)

Everything below is automatic; the parameter names are listed only so the tuning
knobs and warnings are understandable.

1. **Fit-crop** all three inputs to a shared canvas (`--canvas-w`, `--canvas-ratio`).
2. **Watermark patch**. Generated images burn a mark into the bottom-right corner,
   flush to the edge (roughly the last 6% of height). Patch it by feather-copying
   the vertical band immediately above it, which keeps local colour seamless.
   `--no-watermark` when inputs are clean; `--wm-x0` / `--wm-y0` move the band.
3. **Background alignment**. Two independent generations rarely agree on framing.
   A coarse-to-fine integer translation search (1/8 scale, then 1/2 scale) minimises
   the mean absolute difference over the background only — the object region,
   located by a rough high threshold, is excluded so the search is driven by the
   static parts of the frame. The winning shift is applied to the crayon and empty
   frames. `--no-align` disables; `--align-max` bounds the search.
4. **Silhouette.** `diff = sum(|crayon - empty|, axis=2)`; threshold at
   `--diff-thresh`; dilate to cover crayon overflow then erode back. The object
   centre is auto-located as the peak of a box-blurred diff map (edge bands
   excluded so the watermark patch region can never win), then only the
   **4-connected component containing that centre** is kept — this is what makes
   the method robust to background colour drift, and why no per-photo seed is needed.
5. **Pen path.** For each row inside the silhouette (step = `--step-y`), take the
   longest contiguous x-run, alternate direction (boustrophedon), sample every
   `--pt-step` pixels. Result: a snake that fills the object like real colouring.
6. **Order field.** Initialise `OF = 1`. For each path point with arc-fraction `f`,
   paint a disk of radius `--brush-r` and set `OF[disk] = min(OF[disk], f)`. Every
   pixel now holds *when* the brush reaches it.
7. **Reveal.** At progress `p`, `mask = clip((p - OF)/soft + 0.5, 0, 1)`. As `p`
   grows the brush travels the snake and colours stroke-by-stroke.
8. **Timeline.** empty hold → stroke draw (the longest phase) → short crayon hold →
   crossfade morph to the real photo with a settle scale → real hold.
   The crayon hold is still by default; `--boiling` restores the ±1–2 px hand-drawn
   wobble for users who want it.
9. **Export.** libx264 MP4 + a looping GIF (`--gif-scale`, sampled to ≈12 fps).
10. **Cover frame.** The frame the still image should show is the
    **drawing-complete frame** — the last frame of the draw stage, i.e. the
    finished crayon drawing before the morph. The script computes its index
    (`hold_empty + draw - 1`), prints the timestamp to pass on, and writes the
    frame as `<out stem>-cover.png` for eyeballing:

    ```
    cover frame 87 @ 2.9167s  ->  stage 4: --still-time 2.9167
    ```

    The timestamp is the **centre** of that frame (`(i + 0.5) / fps`) so the
    AVAssetImageGenerator in stage 4 cannot snap to the neighbouring frame.
    `--cover-png` moves the file; `--no-cover-png` skips it.

    Why not the last frame: the last frame is the plain real photo, so the
    asset at rest looks like an ordinary picture and the drawing — the whole
    point of the effect — is invisible until someone presses. The drawing is
    the payoff; it belongs on the cover.

To replay on a fresh photo, only the two generation calls change; the script is
fully deterministic given the three frames.

11. **Duration budget.** When the result is going into a Live Photo, pass
    `--max-seconds` (4.6 is a safe budget). Every stage is scaled by the same
    factor, so the pacing stays in proportion while the total drops under the
    ~5 s Live Photo ceiling. Without it the defaults run ≈5.1 s and the
    packager has to trim the ending.

## Stage 4 — packaging as a native Live Photo

```bash
python3 scripts/make_livephoto.py --video live.mp4 --out-dir pair/ --jpeg \
    --still-time <cover time printed by stage 3>
```

The composed MP4 is not the deliverable. The packager

1. rewraps it into `.mov` by **passthrough** (no re-encode, frames identical),
   capped at `--max-duration` (default 4.9 s);
2. writes the movie-level `com.apple.quicktime.content.identifier`;
3. appends a timed `com.apple.quicktime.still-image-time` sample at the **cover
   time** — the drawing-complete frame the still shows;
4. extracts that frame from the finished MOV and writes it as HEIC (optionally
   also JPEG) carrying `MakerApple[17]` with the same identifier. Deriving the
   still from the MOV rather than re-using a source frame is deliberate: the two
   halves then cannot disagree;
5. reads the pair back with AVFoundation;
6. creates one `PHAsset` from the two resources and reports `is_photoLive`,
   `playbackStyle` and the resource list.

Because the still is the cover frame, the finished Live Photo **rests on the
finished crayon drawing** and long-press plays the whole sequence through to the
real photo — which is the intended reading of the effect. Omit `--still-time`
only if you deliberately want the plain photo as the cover (the packager then
defaults to the last frame); `--still-png` replaces the still source entirely,
at the cost of losing the "cannot disagree" guarantee of point 4.

PhotoKit needs to run inside an app bundle that declares the photo usage
strings; see `references/live-photo-spec.md` and `scripts/make_verifier_app.py`.

## Parameter recipe

| Goal | Parameter | Value |
|------|-----------|-------|
| slower / clearer stroke-by-stroke | `--draw` | larger (e.g. 90–120) |
| longer crayon strokes, fewer rows | `--step-y` | larger (≈ canvas_h/50) |
| thicker crayon coverage (no gaps) | `--brush-r` | ≳ half the row step |
| tighter silhouette (no background leak) | `--diff-thresh` | higher (e.g. 90–120) |
| silhouette detected in the wrong place | `--seed-x` / `--seed-y` | object centre, 0..1 |
| object sits lower / higher in frame | `--seed-y` | adjust |
| square / feed-friendly output | `--canvas-ratio` | `1:1`, `4:5`, `9:16` |
| no watermark in inputs | `--no-watermark` | flag |
| hand-drawn wobble on the finished drawing | `--boiling` | flag |
| longer, smoother morph | `--morph` | 36–48 |

Pacing defaults that have held up across subjects: `--hold-empty 21`,
`--draw 66`, `--hold-crayon 6`, `--morph 36`, `--hold-real 24`, `--fps 30`
(≈5.1 s at a 9:16 canvas — fine for a standalone video, too long for a Live
Photo). For Live Photo output add `--max-seconds 4.6`, which rescales to
≈4.6 s without changing the feel.

## Extending to new subject types

| New input | What changes | What does not |
|---|---|---|
| A different drink / food / prop | Stage 1 prompt colour regions only | everything else |
| A fanned/spread grip (wide cup, plate, book) | nothing — Stage 2 is pose-agnostic; do NOT add "keep fingers curled" | everything else |
| An object with many fine regions (a printed package) | state the 2–3 dominant blocks and explicitly tell the model to drop the rest | component / path / reveal logic |
| Two hands, or an object spanning both | nothing structural; the silhouette is still "where the two frames differ" | — |
| A landscape photo | `--canvas-ratio 16:9` so the fit-crop does not throw the frame away | — |
| A style other than crayon (marker, pastel, chalk) | Stage 1 style wording only — the reveal needs only that the two frames differ on the object | — |
| A very large or very small object | nothing; pen geometry scales with the canvas | — |

## Troubleshooting

- **Silhouette spans the whole frame / the drawing never completes.** Background
  drifted between the two generations. Raise `--diff-thresh`; the connected-component
  filter usually handles the rest.
- **"silhouette is very small" warning.** The object was not detected — either the
  threshold is too high for a low-saturation object, or the empty keyframe still
  contains a ghost of the object. Regenerate Stage 2 or set `--seed-x/--seed-y`.
- **Empty hand becomes a fist (or an open palm).** The prompt named a pose and
  the model substituted a canonical grip — most often a fist on wide objects
  that are actually held with a fanned grip. Remove every pose word from the
  Stage 2 prompt, restate it as pure object removal with a per-finger position
  lock (see the keyframe section), and regenerate.
- **Watermark ghost.** The band may not match the generator's placement; adjust
  `--wm-y0` (and `--wm-x0`) after eyeballing a generated frame.
- **Hand / object alignment jump on morph.** The built-in alignment search fixes
  translation. Residual size or perspective differences mean the two generations
  genuinely disagree — regenerate the crayon keyframe, or composite the crayon
  object onto the real hand before morphing.
- **Gaps between strokes.** Brush radius much smaller than the row step; the script
  warns. Raise `--brush-r` or lower `--step-y`.

## Tooling notes

- Python packages for stages 1–3: `pillow numpy imageio imageio-ffmpeg`.
  `ffmpeg` itself is not required — the imageio-ffmpeg encoder supplies the codec.
- Python packages for stage 4: `pyobjc-framework-Cocoa`,
  `pyobjc-framework-AVFoundation`, `pyobjc-framework-Photos`,
  `pyobjc-framework-Quartz`, `pyobjc-framework-UniformTypeIdentifiers`.
  There is **no** `pyobjc-framework-ImageIO` package — ImageIO ships inside
  `Quartz`.
- Xcode and the Command Line Tools are **not** required. If they are missing,
  `swift`, `clang` and `git` all fail with
  `xcrun: error: invalid active developer path`; the PyObjC route is unaffected.
- Image generation consumes credits per image; plan for **two** keyframe
  generations per live image (one crayon, one empty hand).
- Preview before delivery: extract the empty-hold frame, one mid-draw frame, the
  crayon-hold frame and the final frame into a contact sheet, and confirm the
  silhouette matches the object, the watermark is gone, and the crayon hold is
  static.
- Preview the *Live Photo* by opening it in Photos and long-pressing. A contact
  sheet cannot show whether pairing worked — only the PhotoKit report can.
- **File systems are case-insensitive on macOS by default.** A working directory
  named `livephoto` is deleted by `rm -rf LivePhoto`. Do not create siblings
  that differ only in case.
