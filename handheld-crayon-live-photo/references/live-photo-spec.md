# What actually makes a Live Photo

A Live Photo is **two** files that Photos agrees to pair. The suffix tells you
nothing — a `.HEIC` on its own is just a photo, and a `.MOV` on its own is just
a video. Pairing happens only when all three markers below are present.

Apple never embeds the video inside the still (that is Google's Motion Photo
format, which iOS does not import as a Live Photo). It is always a resource
pair.

## The three markers

| # | Where | Key | Value |
|---|---|---|---|
| 1 | still image (HEIC/JPEG) | `MakerApple` dictionary, key `"17"` | the asset identifier (UUID string) |
| 2 | video (.mov), movie level | `com.apple.quicktime.content.identifier` | **the same** identifier |
| 3 | video (.mov), timed metadata track | `com.apple.quicktime.still-image-time` | `0`, datatype `com.apple.metadata.datatype.int8`, presented at the frame the still shows |

Miss any one and Photos stores a picture next to a video.

## How each is written (PyObjC)

### 1. Still side — ImageIO

```python
props = {Quartz.kCGImagePropertyMakerAppleDictionary: {"17": asset_id},
         Quartz.kCGImagePropertyOrientation: 1}
dest = Quartz.CGImageDestinationCreateWithURL(url, "public.heic", 1, None)
Quartz.CGImageDestinationAddImage(dest, cg_image, props)
Quartz.CGImageDestinationFinalize(dest)
```

`kCGImagePropertyMakerAppleDictionary` is the string `"{MakerApple}"` — with
braces. Looking properties up with the bare `"MakerApple"` returns nothing and
looks like the write failed.

### 2 & 3. Video side — AVFoundation

Movie-level identifier:

```python
item = AVF.AVMutableMetadataItem.metadataItem()
item.setKey_("com.apple.quicktime.content.identifier")
item.setKeySpace_(AVF.AVMetadataKeySpaceQuickTimeMetadata)
item.setValue_(asset_id)
item.setDataType_("com.apple.metadata.datatype.UTF-8")
writer.setMetadata_([item])
```

Still image time, on its own metadata track:

```python
spec = {"MetadataIdentifier": "mdta/com.apple.quicktime.still-image-time",
        "MetadataDataType": "com.apple.metadata.datatype.int8"}
status, fmt = CM.CMMetadataFormatDescriptionCreateWithMetadataSpecifications(
    None, CM.kCMMetadataFormatType_Boxed, [spec], None)
st_in = AVF.AVAssetWriterInput.assetWriterInputWithMediaType_outputSettings_sourceFormatHint_(
    AVF.AVMediaTypeMetadata, None, fmt)
adaptor = AVF.AVAssetWriterInputMetadataAdaptor.assetWriterInputMetadataAdaptorWithAssetWriterInput_(st_in)
...
group = AVF.AVTimedMetadataGroup.alloc().initWithItems_timeRange_([item], rng)
adaptor.appendTimedMetadataGroup_(group)
```

The **source format hint is mandatory**. Without it the adaptor raises
`Cannot create a new metadata adaptor with an asset writer that does not carry
a source format hint`.

## The traps

- **Passthrough vs re-encode.** `AVAssetWriterInput(outputSettings=nil)` copies
  compressed samples verbatim. If you pass an encoding dictionary instead, the
  reader must hand over uncompressed pixels
  (`{"PixelFormatType": 0x34323076}`), or you get
  *"Input buffer must be in an uncompressed format when outputSettings is not nil"*.
  Prefer passthrough — frames stay identical to the composed clip.
- **Reading the still time back.** Attach
  `AVAssetReaderOutputMetadataAdaptor` **before** `startReading`, and use
  `nextTimedMetadataGroup()` (there is no `copyNextTimedMetadataGroup`).
  Reading the metadata track's first sample buffer instead returns a 0-valued
  priming sample and looks like the write failed.
- **Duration.** Keep the paired video at or under ~5 s. Photos ignores
  anything longer.
- **Still must be a real frame of the video.** Pull it out of the finished MOV
  with `AVAssetImageGenerator` at the still time rather than re-using a source
  image, so the two can never disagree.

## Creating and verifying the asset

```python
req = Photos.PHAssetCreationRequest.creationRequestForAsset()
req.addResourceWithType_fileURL_options_(Photos.PHAssetResourceTypePhoto, still_url, opts)
req.addResourceWithType_fileURL_options_(Photos.PHAssetResourceTypePairedVideo, mov_url, opts)
lib.performChangesAndWait_error_(changes, None)
```

Both resources must go into **one** request. Two separate writes produce two
unrelated assets — this is exactly what happens when a user saves the image and
the video one at a time from an iOS app.

Verification reads the created asset back:

```python
subs = int(asset.mediaSubtypes())
is_live = bool(subs & Photos.PHAssetMediaSubtypePhotoLive)   # 1 << 3 == 8
resources = Photos.PHAssetResource.assetResourcesForAsset_(asset)
# expect types 1 (.photo) and 9 (.pairedVideo)
```

`playbackStyle == 3` is `.livePhoto`. `duration` on the asset reads 0 — Live
Photos are `mediaType == .image`, so the duration lives on the paired video
resource, not the asset.

## Authorisation

`PHPhotoLibrary` is TCC-gated. Two silent failure modes:

1. **No `NSPhotoLibraryUsageDescription` in the host** → `requestAuthorization`
   returns `Denied` (2) instantly, no dialog. This is what happens when a script
   runs under a host app that never declared it.
2. **Bare CLI process** → the completion handler is dispatched to the main
   queue, which nothing is draining, so the call never returns. Pump the main
   run loop while waiting, or — better — run inside an app bundle.

Workable recipe: generate a minimal `.app` whose Info.plist declares
`NSPhotoLibraryUsageDescription` and `NSPhotoLibraryAddUsageDescription`,
`xattr -cr` it, ad-hoc sign with `/usr/bin/codesign --force --deep -s -`, then
`open -a`. macOS shows the normal dialog and remembers the decision for that
bundle identifier. `scripts/make_verifier_app.py` does this.

Note: the app's working directory is `/`. Always pass absolute paths in the
wrapped command.

## Moving a Live Photo around

| Route | Pairing survives |
|---|---|
| AirDrop the Live Photo entry from the Photos app | yes |
| iCloud Photos sync | yes |
| AirDrop / save the two bare files together | often, but iOS-version dependent |
| Save the two bare files one at a time | **no** — a photo plus an unrelated video |
| Any app that re-encodes the image | no — `MakerApple[17]` is stripped |
