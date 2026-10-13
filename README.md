# live_photo_skill

A series of WorkBuddy skills that turn still inputs into **native Apple Live
Photos** — real paired resources (HEIC/JPEG + MOV sharing one content
identifier), verified through PhotoKit, not a video file with a photo sitting
next to it.

## Skills in this repo

| Skill | Input | Output |
|---|---|---|
| [`handheld-crayon-live-photo`](handheld-crayon-live-photo/) | one photo of a hand holding a drink | a Live Photo where a chunky crayon version of the drink is drawn on stroke by stroke, then settles into the real photo |

Each skill folder is self-contained: `SKILL.md` plus `references/` and
`scripts/`. Nothing in this repo is tied to a sample photo — no example images
or finished outputs are committed.

## What "native Live Photo" means here

Three markers have to be present, or Photos stores a picture next to a video:

1. still image → `MakerApple[17]` = asset identifier
2. MOV → `com.apple.quicktime.content.identifier` = the same identifier
3. MOV → timed `com.apple.quicktime.still-image-time` track

And the only accepted proof is PhotoKit: `mediaSubtypes` contains `.photoLive`,
with `.photo` and `.pairedVideo` resources on the same asset. Details in
`handheld-crayon-live-photo/references/live-photo-spec.md`.

## Using a skill

Copy the skill folder into your WorkBuddy skills directory
(`~/.workbuddy/skills/` for user-level, or `<project>/.workbuddy/skills/` for
project-level), then invoke it by name.

## Requirements

- macOS (stage 4 uses ImageIO, AVFoundation and PhotoKit through PyObjC).
- Python 3 with `pillow numpy imageio imageio-ffmpeg` and
  `pyobjc-framework-Cocoa AVFoundation Photos Quartz UniformTypeIdentifiers`.
- No Xcode, no Command Line Tools, no `ffmpeg` needed.
- Image generation costs credits — two generations per Live Photo.

## Adding another skill to the series

Create a sibling folder with the same shape:

```
<skill-name>/
├── SKILL.md
├── references/
└── scripts/
```

Reuse `handheld-crayon-live-photo/scripts/make_livephoto.py` and
`make_verifier_app.py` unchanged — they take any clip and turn it into a
verified Live Photo pair. Only the content-generation stages differ between
skills.
