#!/usr/bin/env python3
"""Compose a crayon "live image": empty hand -> crayon drawn stroke-by-stroke -> real.

Inputs (the three keyframes must be prepared beforehand, e.g. with the ImageGen
tool — see SKILL.md / references/methodology.md):
  --source  the REAL photo (hand holding the object)
  --crayon  crayon-scribble keyframe (object rendered as crayon, hand pose intact)
  --empty   empty-hand keyframe (object removed, hand pose identical to --crayon)

Everything that used to be hand-tuned for one specific photo is now derived at
runtime, so the script works unchanged on any new keyframe triple:

  * object centre      -- auto-located (blurred difference-map peak) unless
                          --seed-x / --seed-y is given explicitly
  * frame alignment    -- coarse-to-fine integer shift search on the background
                          (object region excluded), fixes the "floating object"
                          morph jump for any pair of generations
  * pen geometry       -- row spacing / brush radius scale with the canvas, so
                          512-tall and 2048-tall outputs behave the same
  * watermark patch    -- covers the bottom-right corner flush to the edge (the
                          built-in AI watermark sits in the last ~6% of height)

Method:
  * object silhouette from |crayon - empty| colour difference, kept as the 4-connected
    component around the object centre (robust to background colour drift).
  * a boustrophedon (snake) pen path inside the silhouette; each pixel stores the
    arc-fraction at which the brush (radius R) reaches it (the "order field").
  * reveal = threshold the order field over time -> the brush travels the path and
    colours stroke-by-stroke in real pen order.
  * empty hold -> stroke-by-stroke draw -> crayon hold (optionally with a
    "boiling line" jitter, off by default) -> crossfade morph into the real
    photo (+settle scale) -> real hold.
  * exports MP4 (libx264), a downscaled looping GIF, and the **cover frame**
    (the drawing-complete frame) as a PNG plus its exact timestamp — feed that
    timestamp to stage 4 via `--still-time` so the Live Photo rests on the
    finished drawing instead of the plain photo.

Required packages: pillow numpy imageio imageio-ffmpeg (the imageio-ffmpeg encoder
supplies the codec, so `ffmpeg` itself is not required).
"""
import argparse
import os

import numpy as np
from PIL import Image, ImageFilter
import imageio.v3 as iio
import imageio

# Width of the soft edge of the reveal, as a fraction of the draw progress. It
# also sets the overshoot the draw stage runs past 1: pixels the brush never
# crosses keep an order-field value of 1 and would otherwise stall at mask 0.5
# (see the draw loop and the cover frame below).
SOFT = 0.012


# --------------------------------------------------------------------- helpers
def load_fit(path, w, h):
    """Load, centre-crop to the target aspect, resize."""
    img = Image.open(path).convert("RGB")
    tw, th = img.size
    ratio = w / h
    if tw / th > ratio:
        nw = int(th * ratio)
        img = img.crop(((tw - nw) // 2, 0, (tw - nw) // 2 + nw, th))
    else:
        nh = int(tw / ratio)
        img = img.crop((0, (th - nh) // 2, tw, (th - nh) // 2 + nh))
    return img.resize((w, h), Image.LANCZOS)


def patch_watermark(img, w, h, fx0=0.78, fy0=0.94):
    """Cover the bottom-right AI watermark by feather-copying the band above it.

    The burned-in mark sits flush against the bottom-right corner (roughly the last
    6% of height), so the patch band must reach the bottom edge — copying from the
    band immediately above keeps the local colour seamless.
    """
    arr = np.array(img).astype(np.float32)
    x0, x1 = int(w * fx0), w
    y0, y1 = int(h * fy0), h
    bw, bh = x1 - x0, y1 - y0
    if bw <= 0 or bh <= 0 or y0 - bh - 4 < 0:
        return img
    src = arr[y0 - bh - 4:y0 - 4, x0:x1].copy()
    fy, fx = min(26, bh), min(22, bw)
    alpha = np.ones((bh, bw), dtype=np.float32)
    alpha[:fy, :] *= np.linspace(0.0, 1.0, fy)[:, None]
    alpha[:, :fx] *= np.linspace(0.0, 1.0, fx)[None, :]
    alpha = alpha[..., None]
    arr[y0:y1, x0:x1] = arr[y0:y1, x0:x1] * (1 - alpha) + src * alpha
    return Image.fromarray(arr.astype(np.uint8))


def _boxblur(a, r):
    """Separable box blur (edge-padded) via cumulative sums. a: 2-D float."""
    def blur1d(x, r, axis):
        pad = [(0, 0)] * x.ndim
        pad[axis] = (r, r)
        xp = np.pad(x, pad, mode="edge")
        lead = np.zeros_like(np.take(xp, [0], axis=axis))
        c = np.concatenate([lead, np.cumsum(xp, axis=axis)], axis=axis)
        n = x.shape[axis]
        k = 2 * r + 1
        hi = np.take(c, np.arange(k, n + 2 * r + 1), axis=axis)
        lo = np.take(c, np.arange(0, n), axis=axis)
        return (hi - lo) / k
    return blur1d(blur1d(a, r, 0), r, 1)


def _block_reduce(a, s, how="mean"):
    """Downsample a 2-D array by integer factor s (mean or max pooling)."""
    H, W = a.shape
    h, w = H // s * s, W // s * s
    b = a[:h, :w].reshape(h // s, s, w // s, s)
    return b.max(axis=(1, 3)) if how == "max" else b.mean(axis=(1, 3))


def auto_seed(diff, top=0.04, bottom=0.90, side=0.05):
    """Locate the object centre as the peak of a blurred difference map.

    Background drift between the two generated frames is diffuse and low
    amplitude; the object is a dense, high-amplitude blob. Blurring with a
    kernel a few percent of the frame therefore peaks inside the object.
    The bands near the top / bottom / side edges are excluded so that the
    watermark patch region and frame borders can never win the argmax.
    """
    r = max(4, int(min(diff.shape) * 0.05))
    b = _boxblur(diff.astype(np.float64), r)
    H, W = b.shape
    mask = np.ones_like(b, dtype=bool)
    mask[:int(H * top), :] = False
    mask[int(H * bottom):, :] = False
    mask[:, :int(W * side)] = False
    mask[:, W - int(W * side):] = False
    if not mask.any():
        mask = np.ones_like(b, dtype=bool)
    b = np.where(mask, b, -1.0)
    y, x = np.unravel_index(int(np.argmax(b)), b.shape)
    return float(x) / W, float(y) / H


def _mad(a, b, ex, oy, ox):
    """Mean |a - b| over the overlap, ignoring pixels where `ex` is True."""
    H, W = a.shape
    y0, y1 = max(0, oy), min(H, H + oy)
    x0, x1 = max(0, ox), min(W, W + ox)
    if y1 <= y0 or x1 <= x0:
        return float("inf")
    av = a[y0:y1, x0:x1]
    bv = b[y0 - oy:y1 - oy, x0 - ox:x1 - ox]
    ev = ex[y0:y1, x0:x1]
    keep = ~ev
    n = int(keep.sum())
    if n < 500:
        return float("inf")
    return float(np.abs(av - bv)[keep].mean())


def estimate_shift(real_g, other_g, exclude, max_shift=32):
    """Coarse-to-fine integer translation aligning `other` onto `real`.

    Only the background is compared (`exclude` = the object mask, dilated), so
    the result is driven by the static parts of the frame rather than by the
    object that is supposed to change.
    """
    shift_y, shift_x = 0, 0
    for scale, radius in ((8, max(1, max_shift // 8)), (2, 2)):
        rg = _block_reduce(real_g, scale)
        og = _block_reduce(other_g, scale)
        ex = _block_reduce(exclude.astype(np.float64), scale, how="max") > 0.25
        best = None
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                oy = shift_y // scale + dy
                ox = shift_x // scale + dx
                cost = _mad(rg, og, ex, oy, ox)
                if best is None or cost < best[0]:
                    best = (cost, oy, ox)
        if best is None:
            return 0, 0
        shift_y, shift_x = best[1] * scale, best[2] * scale
    return shift_y, shift_x


def shift_arr(a, dy, dx):
    """Translate (H,W,...) by (dy,dx), clamping at the edges (no wrap-around)."""
    if dy == 0 and dx == 0:
        return a
    H, W = a.shape[:2]
    sy = np.clip(np.arange(H) - dy, 0, H - 1)
    sx = np.clip(np.arange(W) - dx, 0, W - 1)
    return a[sy[:, None], sx[None, :]]


def component_at(mask, sy, sx):
    """4-connected component of boolean `mask` containing row `sy`, col `sx`.

    Exact flood fill (DFS with an explicit stack). Costs one pass over the
    component's own pixels only, so it stays fast at full resolution.
    """
    H, W = mask.shape
    sy = int(np.clip(sy, 0, H - 1))
    sx = int(np.clip(sx, 0, W - 1))
    out = np.zeros((H, W), dtype=bool)
    if not mask[sy, sx]:
        return out
    seen = bytearray(H * W)
    stack = [(sy, sx)]
    seen[sy * W + sx] = 1
    while stack:
        y, x = stack.pop()
        if not out[y, x]:
            out[y, x] = True
        for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
            if 0 <= ny < H and 0 <= nx < W:
                i = ny * W + nx
                if not seen[i] and mask[ny, nx]:
                    seen[i] = 1
                    stack.append((ny, nx))
    return out


def build_path(mask, step_y, pt_step, rng):
    """Boustrophedon (snake) pen path: fill the mask row by row, alternating direction."""
    rows = np.where(mask.any(axis=1))[0]
    if len(rows) == 0:
        raise SystemExit("未检测到物体轮廓，请检查关键帧或调低 --diff-thresh")
    H, W = mask.shape
    path, row = [], 0
    y = int(rows.min())
    while y <= int(rows.max()):
        xs = np.where(mask[y])[0]
        if len(xs) > 8:
            splits = np.where(np.diff(xs) > 1)[0]
            run = max(np.split(xs, splits + 1), key=len)
            x0, x1 = max(0, run[0] - 4), min(W - 1, run[-1] + 4)
            xs_row = np.arange(x0, x1 + 1, pt_step)
            if row % 2 == 1:
                xs_row = xs_row[::-1]
            for x in xs_row:
                path.append((float(x), float(y) + rng.uniform(-1.5, 1.5)))
            row += 1
        y += step_y
    return np.array(path, dtype=np.float32)


def order_field(path, frac, H, W, R):
    """Arc-fraction at which a brush of radius R first reaches each pixel."""
    OF = np.ones((H, W), dtype=np.float32)
    for (xi, yi), f in zip(path, frac):
        xi, yi = int(xi), int(yi)
        x0, x1 = max(0, xi - R), min(W, xi + R + 1)
        y0, y1 = max(0, yi - R), min(H, yi + R + 1)
        if x0 >= x1 or y0 >= y1:
            continue
        yy, xx = np.mgrid[y0:y1, x0:x1]
        d = (xx - xi) ** 2 + (yy - yi) ** 2 <= R * R
        region = OF[y0:y1, x0:x1]
        region[d] = np.minimum(region[d], f)
        OF[y0:y1, x0:x1] = region
    return OF


# ------------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description="Compose a crayon live image.")
    ap.add_argument("--source", required=True, help="real photo (hand holding object)")
    ap.add_argument("--crayon", required=True, help="crayon keyframe")
    ap.add_argument("--empty", required=True, help="empty-hand keyframe")
    ap.add_argument("--out", default="live.mp4", help="output mp4 path")
    ap.add_argument("--gif", default="live.gif", help="output gif path")
    ap.add_argument("--canvas-w", type=int, default=864, help="canvas width in px")
    ap.add_argument("--canvas-ratio", default="9:16",
                    help="canvas aspect as W:H (e.g. 9:16, 1:1, 3:4, 16:9)")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--step-y", type=int, default=None,
                    help="pen row spacing in px (default: canvas_h/70)")
    ap.add_argument("--brush-r", type=int, default=None,
                    help="brush radius in px (default: 0.6 * step-y; keep >= step-y/2)")
    ap.add_argument("--pt-step", type=int, default=5, help="path sampling step in px")
    ap.add_argument("--diff-thresh", type=int, default=70,
                    help="silhouette difference threshold (raise if background leaks)")
    ap.add_argument("--seed-y", type=float, default=None, help="object centre y (0..1)")
    ap.add_argument("--seed-x", type=float, default=None, help="object centre x (0..1)")
    ap.add_argument("--no-watermark", action="store_true",
                    help="inputs carry no burned-in watermark")
    ap.add_argument("--wm-x0", type=float, default=0.78, help="watermark band start x (0..1)")
    ap.add_argument("--wm-y0", type=float, default=0.94, help="watermark band start y (0..1)")
    ap.add_argument("--no-align", action="store_true",
                    help="skip the background alignment search")
    ap.add_argument("--align-max", type=int, default=32, help="max alignment shift in px")
    ap.add_argument("--boiling", dest="no_boiling", action="store_false", default=True,
                    help="enable the 'boiling line' jitter during the crayon hold "
                         "(default: off — the drawing holds still, then morphs)")
    ap.add_argument("--hold-empty", type=int, default=21)
    ap.add_argument("--draw", type=int, default=66)
    ap.add_argument("--hold-crayon", type=int, default=6)
    ap.add_argument("--morph", type=int, default=36)
    ap.add_argument("--hold-real", type=int, default=24)
    ap.add_argument("--settle", type=float, default=0.05, help="morph settle scale (0 = off)")
    ap.add_argument("--cover-png", default=None,
                    help="where to write the cover frame, i.e. the drawing-complete "
                         "frame (default: <out stem>-cover.png)")
    ap.add_argument("--no-cover-png", action="store_true",
                    help="do not write the cover png (the cover time is still printed)")
    ap.add_argument("--gif-scale", type=int, default=2, help="gif downscale factor")
    ap.add_argument("--max-seconds", type=float, default=None,
                    help="budget the whole clip into <= N seconds by scaling every "
                         "stage proportionally (Live Photos cap at ~5s; 4.6 is safe)")

    a = ap.parse_args()

    if a.max_seconds:
        stages = ("hold_empty", "draw", "hold_crayon", "morph", "hold_real")
        cur = {k: getattr(a, k) for k in stages}
        total = sum(cur.values())
        target = int(a.fps * a.max_seconds)
        if total > target:
            k = target / total
            for name in stages:
                setattr(a, name, max(1, int(round(cur[name] * k))))
            print("clip budget: %d -> %d frames (%.2fs @ %dfps)"
                  % (total, sum(getattr(a, n) for n in stages),
                     sum(getattr(a, n) for n in stages) / float(a.fps), a.fps))

    rw, rh = (int(v) for v in a.canvas_ratio.split(":"))
    W = a.canvas_w
    H = int(round(W * rh / rw))

    real = load_fit(a.source, W, H)
    crayon = load_fit(a.crayon, W, H)
    empty = load_fit(a.empty, W, H)
    if not a.no_watermark:
        crayon = patch_watermark(crayon, W, H, a.wm_x0, a.wm_y0)
        empty = patch_watermark(empty, W, H, a.wm_x0, a.wm_y0)
    real_a = np.array(real).astype(np.float32)
    crayon_a = np.array(crayon).astype(np.float32)
    empty_a = np.array(empty).astype(np.float32)

    # ---- rough object mask, used only to exclude the object while aligning ----
    diff0 = np.abs(crayon_a - empty_a).sum(axis=2)
    rough = diff0 > max(a.diff_thresh * 1.5, 120)
    excl = np.array(Image.fromarray(rough.astype(np.uint8) * 255, "L")
                    .filter(ImageFilter.MaxFilter(9))
                    .filter(ImageFilter.MaxFilter(9))) > 127

    # ---- background alignment: fix the "floating object" morph jump ----
    if not a.no_align:
        rg = np.array(real.convert("L")).astype(np.float32)
        cg = np.array(crayon.convert("L")).astype(np.float32)
        dy, dx = estimate_shift(rg, cg, excl, a.align_max)
        if (dy, dx) != (0, 0):
            print(f"alignment shift dy={dy} dx={dx}")
            crayon_a = shift_arr(crayon_a, dy, dx)
            empty_a = shift_arr(empty_a, dy, dx)

    # ---- object silhouette from |crayon - empty| ----
    diff = np.abs(crayon_a - empty_a).sum(axis=2)
    M = (diff > a.diff_thresh).astype(np.uint8) * 255
    m = Image.fromarray(M, "L").filter(ImageFilter.MaxFilter(7)).filter(ImageFilter.MinFilter(5))
    Mfull = np.array(m) > 127

    if a.seed_x is None or a.seed_y is None:
        sx, sy = auto_seed(diff)
        if a.seed_x is not None:
            sx = a.seed_x
        if a.seed_y is not None:
            sy = a.seed_y
        print(f"object centre auto: x={sx:.3f} y={sy:.3f}")
    else:
        sx, sy = a.seed_x, a.seed_y
    comp = component_at(Mfull, sy * (H - 1), sx * (W - 1))
    M = Mfull & comp
    M = np.array(Image.fromarray(M.astype(np.uint8) * 255)
                 .filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))) > 127

    frac_px = float(M.sum()) / float(W * H)
    print(f"silhouette rows {int(np.where(M.any(axis=1))[0].min())}.."
          f"{int(np.where(M.any(axis=1))[0].max())}, px {int(M.sum())} ({frac_px:.1%} of canvas)")
    if frac_px < 0.004:
        print("warning: silhouette is very small — the object may not have been "
              "detected; try lowering --diff-thresh or setting --seed-x/--seed-y")
    elif frac_px > 0.35:
        print("warning: silhouette is very large — background may be leaking in; "
              "try raising --diff-thresh")

    # ---- pen geometry scales with the canvas, so any output size behaves alike ----
    step_y = a.step_y if a.step_y is not None else max(6, int(round(H / 70)))
    brush_r = a.brush_r if a.brush_r is not None else max(4, int(round(step_y * 0.6)))
    if brush_r * 2 < step_y:
        print(f"warning: brush radius {brush_r} is smaller than half the row step "
              f"{step_y}; strokes may leave gaps")

    rng = np.random.default_rng(3)
    path = build_path(M, step_y, a.pt_step, rng)
    seg = np.sqrt(((np.diff(path, axis=0)) ** 2).sum(axis=1))
    arc = np.concatenate([[0.0], np.cumsum(seg)])
    frac = (arc / arc[-1]).astype(np.float32)
    print(f"pen path points {len(path)}, length {arc[-1]:.0f}px")

    OF = order_field(path, frac, H, W, brush_r)

    def stroke_frame(p):
        mask = np.clip((p - OF) / SOFT + 0.5, 0.0, 1.0)
        return empty_a * (1 - mask[..., None]) + crayon_a * mask[..., None]

    def ease(t):
        return 0.5 - 0.5 * np.cos(np.pi * np.clip(t, 0, 1))

    frames = []
    rng2 = np.random.default_rng(11)
    frames += [empty_a.copy() for _ in range(a.hold_empty)]
    for i in range(a.draw):
        # Overshoot the progress past 1. Pixels the brush never crosses keep
        # OF = 1, so at p = 1 exactly they land on mask = (1-1)/SOFT + 0.5 = 0.5
        # -- a half-drawn ghost, i.e. the missing colour blocks. Running the
        # progress to 1 + 2*SOFT pushes the tail of the ramp to a full 1.
        frames.append(stroke_frame(ease((i + 1) / a.draw) * (1.0 + 2.0 * SOFT)))
    for _ in range(a.hold_crayon):
        if a.no_boiling:
            frames.append(crayon_a.copy())
        else:
            dx, dy = int(rng2.integers(-2, 3)), int(rng2.integers(-2, 3))
            frames.append(np.roll(np.roll(crayon_a, dy, axis=0), dx, axis=1).copy())
    for i in range(a.morph):
        t = ease((i + 1) / a.morph)
        s = 1.0 + a.settle * (1 - t)
        mw, mh = int(round(W * s)), int(round(H * s))
        scaled = np.array(real.resize((mw, mh), Image.LANCZOS).crop(
            ((mw - W) // 2, (mh - H) // 2, (mw - W) // 2 + W, (mh - H) // 2 + H))).astype(np.float32)
        frames.append(crayon_a * (1 - t) + scaled * t)
    frames += [real_a.copy() for _ in range(a.hold_real)]
    print(f"frames {len(frames)}, duration {len(frames)/a.fps:.2f}s")

    # ---- cover frame = the complete crayon drawing ---------------------------
    # The Live Photo should rest on the finished drawing, not on the plain photo:
    # the drawing is the payoff. The cover is taken from the crayon keyframe
    # itself, not from whatever the soft-edged reveal produced on its last
    # frame — the reveal is a ramp over the "order field", and any pixel the
    # brush never crosses can only ever reach mask 0.5 there, which shows up as
    # missing / washed-out colour blocks on the cover. The keyframe is by
    # definition complete, so the cover is exact by construction.
    cover_i = min(a.hold_empty + a.draw - 1, len(frames) - 1)
    frames[cover_i] = crayon_a.copy()
    cover_t = (cover_i + 0.5) / float(a.fps)
    resid = float(np.abs(frames[cover_i] - crayon_a).mean())
    print(f"cover frame {cover_i} @ {cover_t:.4f}s  ->  stage 4: --still-time {cover_t:.4f}"
          f"   (complete drawing, residual {resid:.3f})")
    if not a.no_cover_png:
        cover_path = a.cover_png or (os.path.splitext(a.out)[0] + "-cover.png")
        Image.fromarray(frames[cover_i].astype(np.uint8)).save(cover_path)
        print(f"cover png -> {cover_path}")

    iio.imwrite(a.out, [f.astype(np.uint8) for f in frames],
                macro_block_size=8, fps=a.fps, codec="libx264", quality=8)
    every = max(1, int(round(a.fps / 12.0)))
    gw, gh = max(1, W // a.gif_scale), max(1, H // a.gif_scale)
    gif_frames = [np.array(Image.fromarray(f.astype(np.uint8)).resize((gw, gh), Image.LANCZOS))
                  for f in frames[::every]]
    imageio.mimsave(a.gif, gif_frames, duration=every / float(a.fps), loop=0)
    print(f"wrote {a.out} and {a.gif}")


if __name__ == "__main__":
    main()
