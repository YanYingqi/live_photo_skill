#!/usr/bin/env python3
"""Turn a short clip into a *native* Apple Live Photo pair.

A Live Photo is not "an image plus a video". It is two resources that Photos
only pairs when both sides carry the same asset identifier:

  still side (HEIC/JPEG) -> MakerApple dictionary key "17"        (ImageIO)
  video side (.mov)      -> com.apple.quicktime.content.identifier (AVFoundation)
  plus a timed metadata track -> com.apple.quicktime.still-image-time,
  marking which video frame the still image represents.

Anything that lacks any one of those three is a plain picture next to a plain
video, however it is named. This script writes all three through Apple's own
frameworks (PyObjC) and then proves the result through PhotoKit:

  * build the .mov, capped at the Live Photo duration limit
  * pull the still frame out of that .mov so the two always agree
  * write HEIC (and optionally JPEG) carrying the asset identifier
  * read the pair back with AVFoundation
  * create a PHAsset from the two resources and confirm
    mediaSubtypes contains .photoLive and the resource list holds
    .photo + .pairedVideo

Usage
    python3 make_livephoto.py --video live.mp4 --out-dir pair/
    python3 make_livephoto.py --video live.mp4 --out-dir pair/ --jpeg

PhotoKit needs to run inside a GUI app bundle (see make_verifier_app.py);
the --no-import mode builds and verifies the files only.
"""
import argparse
import json
import os
import sys
import threading
import time
import uuid

from Foundation import NSURL, NSNumber, NSRunLoop, NSDate

import AVFoundation as AVF
import CoreMedia as CM
import Quartz
import Photos

CONTENT_IDENTIFIER_KEY = "com.apple.quicktime.content.identifier"
STILL_IMAGE_TIME_KEY = "com.apple.quicktime.still-image-time"
MAKER_APPLE_ASSET_ID = "17"

# Photos rejects/ignores paired video longer than ~5s; stay under it.
DEFAULT_MAX_DURATION = 4.9
TIMESCALE = 600
AUTH_WAIT = 120.0

TYPE_NAMES = {
    1: "photo", 2: "video", 3: "audio", 4: "alternatePhoto", 5: "fullSizePhoto",
    6: "fullSizeVideo", 7: "adjustmentData", 8: "adjustmentBasePhoto",
    9: "pairedVideo", 10: "fullSizePairedVideo", 11: "adjustmentBaseVideo",
}


# --------------------------------------------------------------------------- #
# still side
# --------------------------------------------------------------------------- #
def write_still(cg_image, dst_url, uttype, asset_id):
    dest = Quartz.CGImageDestinationCreateWithURL(dst_url, uttype, 1, None)
    if dest is None:
        raise RuntimeError("CGImageDestination unavailable for " + uttype)
    # kCGImagePropertyMakerAppleDictionary is "{MakerApple}" -- key "17" in that
    # dictionary is the Live Photo asset identifier.
    props = {
        Quartz.kCGImagePropertyMakerAppleDictionary: {MAKER_APPLE_ASSET_ID: asset_id},
        Quartz.kCGImagePropertyOrientation: 1,
    }
    Quartz.CGImageDestinationAddImage(dest, cg_image, props)
    if not Quartz.CGImageDestinationFinalize(dest):
        raise RuntimeError("failed to finalise still image: " + str(dst_url))


def load_cg_image(path_url):
    src = Quartz.CGImageSourceCreateWithURL(path_url, None)
    if src is None:
        raise RuntimeError("cannot open still source image")
    return Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)


def read_still_asset_id(url):
    src = Quartz.CGImageSourceCreateWithURL(url, None)
    props = Quartz.CGImageSourceCopyPropertiesAtIndex(src, 0, None)
    maker = (props or {}).get(Quartz.kCGImagePropertyMakerAppleDictionary) or {}
    return maker.get(MAKER_APPLE_ASSET_ID)


# --------------------------------------------------------------------------- #
# video side
# --------------------------------------------------------------------------- #
def build_mov(src_url, dst_url, asset_id, max_duration=DEFAULT_MAX_DURATION,
              reencode=False, still_time=None):
    """Rewrap src into a QuickTime .mov carrying the two Live Photo markers.

    `still_time` places the still-image-time metadata at that second (the cover
    frame); None keeps the historical behaviour of marking the last frame.
    Returns (frame_count, duration_seconds, still_time_seconds).
    """
    asset = AVF.AVURLAsset.assetWithURL_(src_url)
    vtracks = asset.tracksWithMediaType_(AVF.AVMediaTypeVideo)
    if not vtracks:
        raise RuntimeError("source clip has no video track")
    vtrack = vtracks[0]

    total_s = float(asset.duration().value) / float(asset.duration().timescale)
    keep_s = total_s if not max_duration else min(total_s, max_duration)
    if max_duration and total_s > max_duration + 1e-6:
        print("      [trim] %.3fs -> %.3fs (Live Photo limit)" % (total_s, keep_s))

    reader, rerr = AVF.AVAssetReader.assetReaderWithAsset_error_(asset, None)
    if reader is None:
        raise RuntimeError("AVAssetReader failed: %s" % rerr)
    if max_duration:
        reader.setTimeRange_(CM.CMTimeRangeMake(
            CM.CMTimeMake(0, TIMESCALE), CM.CMTimeMakeWithSeconds(keep_s, TIMESCALE)))
    # nil output settings -> samples stay compressed, which is what the writer
    # needs for a lossless rewrap (frames identical to the source clip)
    pix = ({"PixelFormatType": 0x34323076}      # 420v, only when re-encoding
           if reencode else None)
    r_out = AVF.AVAssetReaderTrackOutput.assetReaderTrackOutputWithTrack_outputSettings_(
        vtrack, pix)
    reader.addOutput_(r_out)

    writer, werr = AVF.AVAssetWriter.assetWriterWithURL_fileType_error_(
        dst_url, AVF.AVFileTypeQuickTimeMovie, None)
    if writer is None:
        raise RuntimeError("AVAssetWriter failed: %s" % werr)

    enc = {
        AVF.AVVideoCodecKey: AVF.AVVideoCodecTypeH264,
        AVF.AVVideoWidthKey: int(vtrack.naturalSize().width),
        AVF.AVVideoHeightKey: int(vtrack.naturalSize().height),
        AVF.AVVideoCompressionPropertiesKey: {
            AVF.AVVideoAverageBitRateKey: 9_000_000,
            AVF.AVVideoProfileLevelKey: AVF.AVVideoProfileLevelH264HighAutoLevel,
        },
    }
    w_in = AVF.AVAssetWriterInput.assetWriterInputWithMediaType_outputSettings_(
        AVF.AVMediaTypeVideo, enc if reencode else None)
    w_in.setExpectsMediaDataInRealTime_(False)
    if reencode:
        w_in.setTransform_(vtrack.preferredTransform())
    writer.addInput_(w_in)

    # movie level: content identifier -- must equal the still's
    cid = AVF.AVMutableMetadataItem.metadataItem()
    cid.setKey_(CONTENT_IDENTIFIER_KEY)
    cid.setKeySpace_(AVF.AVMetadataKeySpaceQuickTimeMetadata)
    cid.setValue_(asset_id)
    cid.setDataType_("com.apple.metadata.datatype.UTF-8")
    writer.setMetadata_([cid])

    # timed metadata track: still image time
    # AVAssetWriterInputMetadataAdaptor refuses to build unless the input
    # carries a metadata format description as its source format hint.
    spec = {
        "MetadataIdentifier": "mdta/" + STILL_IMAGE_TIME_KEY,
        "MetadataDataType": "com.apple.metadata.datatype.int8",
    }
    status, fmt_desc = CM.CMMetadataFormatDescriptionCreateWithMetadataSpecifications(
        None, CM.kCMMetadataFormatType_Boxed, [spec], None)
    if status != 0:
        raise RuntimeError("CMMetadataFormatDescriptionCreate failed: %s" % status)
    st_in = AVF.AVAssetWriterInput.assetWriterInputWithMediaType_outputSettings_sourceFormatHint_(
        AVF.AVMediaTypeMetadata, None, fmt_desc)
    st_in.setExpectsMediaDataInRealTime_(False)
    writer.addInput_(st_in)
    adaptor = AVF.AVAssetWriterInputMetadataAdaptor.assetWriterInputMetadataAdaptorWithAssetWriterInput_(
        st_in)

    if not writer.startWriting():
        raise RuntimeError("startWriting failed: %s" % writer.error())
    writer.startSessionAtSourceTime_(CM.CMTimeMake(0, TIMESCALE))
    if not reader.startReading():
        raise RuntimeError("startReading failed: %s" % reader.error())

    n = 0
    last_pts = None
    while True:
        sb = r_out.copyNextSampleBuffer()
        if sb is None:
            break
        pts = CM.CMSampleBufferGetOutputPresentationTimeStamp(sb)
        if pts.timescale:
            last_pts = pts
        while not w_in.isReadyForMoreMediaData():
            time.sleep(0.005)
        if not w_in.appendSampleBuffer_(sb):
            print("[warn] appendSampleBuffer returned False at frame %d" % n,
                  file=sys.stderr)
        n += 1
    if last_pts is None:
        raise RuntimeError("the reader produced no samples")

    # the still-image-time marks the frame the still image represents.
    # Default: the very last frame we actually wrote. `still_time` overrides it
    # (e.g. the cover frame printed by compose_live.py) and is clamped into the
    # clip, so the metadata and the extracted still can never disagree.
    if still_time is not None:
        still_s = max(0.0, min(float(still_time),
                               float(last_pts.value) / float(last_pts.timescale)))
    else:
        still_s = float(last_pts.value) / float(last_pts.timescale)
    sit = AVF.AVMutableMetadataItem.metadataItem()
    sit.setKey_(STILL_IMAGE_TIME_KEY)
    sit.setKeySpace_(AVF.AVMetadataKeySpaceQuickTimeMetadata)
    sit.setValue_(NSNumber.numberWithInt_(0))
    sit.setDataType_("com.apple.metadata.datatype.int8")
    rng = CM.CMTimeRangeMake(
        CM.CMTimeMakeWithSeconds(still_s, TIMESCALE), CM.CMTimeMake(1, 1000))
    group = AVF.AVTimedMetadataGroup.alloc().initWithItems_timeRange_([sit], rng)
    if not adaptor.appendTimedMetadataGroup_(group):
        print("[warn] appendTimedMetadataGroup returned False", file=sys.stderr)

    w_in.markAsFinished()
    st_in.markAsFinished()
    ev = threading.Event()
    writer.finishWritingWithCompletionHandler_(lambda: ev.set())
    if not ev.wait(180):
        raise RuntimeError("finishWriting timed out")
    if writer.status() != AVF.AVAssetWriterStatusCompleted:
        raise RuntimeError("writer status %s error=%s" % (writer.status(),
                                                          writer.error()))
    return n, keep_s, still_s


def extract_still_cg_image(mov_url, still_s):
    """Pull the frame at still_s straight out of the .mov we just wrote."""
    asset = AVF.AVURLAsset.assetWithURL_(mov_url)
    gen = AVF.AVAssetImageGenerator.assetImageGeneratorWithAsset_(asset)
    gen.setAppliesPreferredTrackTransform_(True)
    gen.setRequestedTimeToleranceBefore_(CM.CMTimeMake(1, TIMESCALE))
    gen.setRequestedTimeToleranceAfter_(CM.CMTimeMake(1, TIMESCALE))
    t = CM.CMTimeMakeWithSeconds(still_s, TIMESCALE)
    img = gen.copyCGImageAtTime_actualTime_error_(t, None, None)
    if isinstance(img, tuple):
        img = img[0]
    if img is None:
        raise RuntimeError("could not extract the still frame at %.4fs" % still_s)
    return img


def verify_mov(url):
    out = {}
    a = AVF.AVURLAsset.assetWithURL_(url)
    dur = a.duration()
    out["duration_sec"] = round(float(dur.value) / float(dur.timescale), 4) if dur.timescale else None
    out["tracks"] = [{
        "mediaType": str(t.mediaType()),
        "naturalSize": (int(t.naturalSize().width), int(t.naturalSize().height)),
        "nominalFrameRate": float(t.nominalFrameRate()),
    } for t in a.tracks()]

    qmd = a.metadataForFormat_(AVF.AVMetadataFormatQuickTimeMetadata) or []
    ids = [str(it.value()) for it in qmd if str(it.key()) == CONTENT_IDENTIFIER_KEY]
    out["content_identifier"] = ids[0] if ids else None

    mt = a.tracksWithMediaType_(AVF.AVMediaTypeMetadata)
    out["has_metadata_track"] = bool(mt)
    if mt:
        # the adaptor must be attached before the reader starts producing
        r2, _ = AVF.AVAssetReader.assetReaderWithAsset_error_(a, None)
        ro2 = AVF.AVAssetReaderTrackOutput.assetReaderTrackOutputWithTrack_outputSettings_(mt[0], None)
        r2.addOutput_(ro2)
        ada = AVF.AVAssetReaderOutputMetadataAdaptor.assetReaderOutputMetadataAdaptorWithAssetReaderTrackOutput_(ro2)
        if r2.startReading():
            while True:
                grp = ada.nextTimedMetadataGroup()
                if grp is None:
                    break
                st = grp.timeRange().start
                if st.timescale:
                    out.setdefault("metadata_sample_times", []).append(
                        round(float(st.value) / float(st.timescale), 4))
                for it in grp.items():
                    if str(it.key()) == STILL_IMAGE_TIME_KEY:
                        out["still_image_time_sec"] = (
                            round(float(st.value) / float(st.timescale), 4)
                            if st.timescale else None)
                        out["still_image_time_value"] = str(it.value())
                        out["still_image_time_keyspace"] = str(it.keySpace())

    with open(str(url.path()), "rb") as fh:
        blob = fh.read()
    out["raw_has_still_image_time_key"] = STILL_IMAGE_TIME_KEY.encode() in blob
    out["raw_has_content_identifier_key"] = CONTENT_IDENTIFIER_KEY.encode() in blob
    out["bytes"] = len(blob)
    return out


# --------------------------------------------------------------------------- #
# PhotoKit
# --------------------------------------------------------------------------- #
def request_photos_authorisation(res):
    status = Photos.PHPhotoLibrary.authorizationStatus()
    res["auth_status_initial"] = int(status)
    if status == Photos.PHAuthorizationStatusNotDetermined:
        # the reply is delivered on the main queue: a process with no run loop
        # would wait forever, so pump the main run loop while waiting
        ev = threading.Event()
        seen = {}

        def handler(s):
            seen["s"] = int(s)
            ev.set()

        Photos.PHPhotoLibrary.requestAuthorization_(handler)
        deadline = time.time() + AUTH_WAIT
        while not ev.is_set() and time.time() < deadline:
            NSRunLoop.mainRunLoop().runUntilDate_(
                NSDate.dateWithTimeIntervalSinceNow_(0.25))
        res["auth_callback_fired"] = ev.is_set()
        res["auth_callback"] = seen.get("s")
        status = Photos.PHPhotoLibrary.authorizationStatus()
    res["auth_status"] = int(status)
    if status not in (Photos.PHAuthorizationStatusAuthorized,
                      Photos.PHAuthorizationStatusLimited):
        res["error"] = "Photos authorisation not granted (status=%s)" % status
        return False
    return True


def describe_asset(asset):
    d = {
        "asset_local_identifier": str(asset.localIdentifier()),
        "mediaType": int(asset.mediaType()),
        "mediaSubtypes_raw": int(asset.mediaSubtypes()),
        "is_photoLive": bool(int(asset.mediaSubtypes())
                             & Photos.PHAssetMediaSubtypePhotoLive),
        "pixelWidth": int(asset.pixelWidth()),
        "pixelHeight": int(asset.pixelHeight()),
        "duration": round(float(asset.duration()), 3),
    }
    try:
        d["playbackStyle"] = int(asset.playbackStyle())
    except Exception:                                        # noqa: BLE001
        pass
    d["resources"] = [{
        "type": int(r.type()),
        "typeName": TYPE_NAMES.get(int(r.type()), "?"),
        "uti": str(r.uniformTypeIdentifier()),
        "filename": str(r.originalFilename()),
    } for r in Photos.PHAssetResource.assetResourcesForAsset_(asset)]
    d["resource_types"] = sorted(x["type"] for x in d["resources"])
    d["has_paired_video_resource"] = (
        Photos.PHAssetResourceTypePairedVideo in d["resource_types"])
    return d


def photokit_import_and_verify(still_url, mov_url, delete_after=False):
    res = {}
    if not request_photos_authorisation(res):
        res["imported"] = False
        return res
    lib = Photos.PHPhotoLibrary.sharedPhotoLibrary()
    holder = {}

    def changes():
        req = Photos.PHAssetCreationRequest.creationRequestForAsset()
        o1 = Photos.PHAssetResourceCreationOptions.alloc().init()
        o1.setShouldMoveFile_(False)
        req.addResourceWithType_fileURL_options_(
            Photos.PHAssetResourceTypePhoto, still_url, o1)
        o2 = Photos.PHAssetResourceCreationOptions.alloc().init()
        o2.setShouldMoveFile_(False)
        req.addResourceWithType_fileURL_options_(
            Photos.PHAssetResourceTypePairedVideo, mov_url, o2)
        holder["ph"] = req.placeholderForCreatedAsset()

    try:
        ok = lib.performChangesAndWait_error_(changes, None)
    except Exception as e:                                   # noqa: BLE001
        res["imported"] = False
        res["error"] = "performChangesAndWait raised: %s" % e
        return res
    if not ok:
        res["imported"] = False
        res["error"] = "performChangesAndWait returned False"
        return res

    fetch = Photos.PHAsset.fetchAssetsWithLocalIdentifiers_options_(
        [holder["ph"].localIdentifier()], None)
    asset = fetch.firstObject()
    if asset is None:
        res["imported"] = False
        res["error"] = "asset not found after import"
        return res
    res["imported"] = True
    res.update(describe_asset(asset))
    if delete_after:
        def delete_block():
            Photos.PHAssetChangeRequest.deleteAssets_([asset])
        lib.performChangesAndWait_error_(delete_block, None)
        res["deleted_after_verify"] = True
    return res


def photokit_verify_existing(local_id):
    res = {}
    if not request_photos_authorisation(res):
        return res
    fetch = Photos.PHAsset.fetchAssetsWithLocalIdentifiers_options_([local_id], None)
    asset = fetch.firstObject()
    if asset is None:
        res["error"] = "no asset with local identifier " + local_id
        return res
    res["imported"] = True
    res.update(describe_asset(asset))
    return res


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True, help="the composed clip")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--still-png", default=None,
                    help="override the still image (default: last frame of the clip)")
    ap.add_argument("--still-time", type=float, default=None,
                    help="seconds into the clip the still represents (default: last frame)")
    ap.add_argument("--max-duration", type=float, default=DEFAULT_MAX_DURATION,
                    help="hard cap for the paired video (Live Photo limit is ~5s)")
    ap.add_argument("--asset-id", default=None, help="reuse an existing identifier")
    ap.add_argument("--jpeg", action="store_true", help="also emit a JPEG still")
    ap.add_argument("--reencode", action="store_true",
                    help="re-encode instead of the lossless passthrough rewrap")
    ap.add_argument("--no-import", action="store_true",
                    help="build and verify the files only")
    ap.add_argument("--verify-asset", default=None,
                    help="verify an already imported PHAsset by local identifier")
    ap.add_argument("--delete-after", action="store_true",
                    help="remove the asset from Photos after verifying")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    asset_id = args.asset_id or str(uuid.uuid4()).upper()
    out = os.path.abspath(args.out_dir)
    for stale in ("live-photo.HEIC", "live-photo.MOV", "live-photo.JPG"):
        p = os.path.join(out, stale)
        if os.path.exists(p):
            os.remove(p)

    video = NSURL.fileURLWithPath_(os.path.abspath(args.video))
    heic_url = NSURL.fileURLWithPath_(os.path.join(out, "live-photo.HEIC"))
    mov_url = NSURL.fileURLWithPath_(os.path.join(out, "live-photo.MOV"))

    print("asset identifier :", asset_id)

    print("[1/3] building MOV with content identifier + still-image-time track ...")
    n, dur_s, still_s = build_mov(video, mov_url, asset_id,
                                  max_duration=args.max_duration,
                                  reencode=args.reencode,
                                  still_time=args.still_time)
    if args.still_time is not None:
        still_s = args.still_time
    print("      ->", mov_url.path(), os.path.getsize(mov_url.path()),
          "bytes / %d frames / %.4fs / still at %.4fs" % (n, dur_s, still_s))

    print("[2/3] writing still image with MakerApple[17] ...")
    if args.still_png:
        cg = load_cg_image(NSURL.fileURLWithPath_(os.path.abspath(args.still_png)))
        still_src = "override: " + args.still_png
    else:
        cg = extract_still_cg_image(mov_url, still_s)
        still_src = "frame @ %.4fs of the MOV" % still_s
    print("      still source:", still_src)
    write_still(cg, heic_url, "public.heic", asset_id)
    heic_id = read_still_asset_id(heic_url)
    print("      HEIC ->", heic_url.path(), os.path.getsize(heic_url.path()),
          "bytes, identifier", heic_id)

    jpeg_url = None
    if args.jpeg:
        jpeg_url = NSURL.fileURLWithPath_(os.path.join(out, "live-photo.JPG"))
        write_still(cg, jpeg_url, "public.jpeg", asset_id)
        print("      JPEG ->", jpeg_url.path(), os.path.getsize(jpeg_url.path()),
              "bytes, identifier", read_still_asset_id(jpeg_url))

    vres = verify_mov(mov_url)
    print("      MOV readback:", json.dumps(vres, ensure_ascii=False))

    report = {
        "asset_identifier": asset_id,
        "still_heic": str(heic_url.path()),
        "still_heic_identifier_readback": heic_id,
        "still_source": still_src,
        "mov": str(mov_url.path()),
        "mov_verification": vres,
        "identifier_match": vres.get("content_identifier") == asset_id == heic_id,
    }
    if jpeg_url is not None:
        report["still_jpeg"] = str(jpeg_url.path())
        report["still_jpeg_identifier_readback"] = read_still_asset_id(jpeg_url)

    print("[3/3] PhotoKit ...")
    if args.verify_asset:
        pk = photokit_verify_existing(args.verify_asset)
        pk["mode"] = "verify-existing"
    elif not args.no_import:
        pk = photokit_import_and_verify(heic_url, mov_url,
                                        delete_after=args.delete_after)
        pk["mode"] = "import-and-verify"
    else:
        pk = {"mode": "skipped"}
    report["photokit"] = pk
    print("      " + json.dumps(pk, indent=2, ensure_ascii=False).replace(
        "\n", "\n      "))

    with open(os.path.join(out, "report.json"), "w") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)

    ok = (report["identifier_match"]
          and vres.get("raw_has_still_image_time_key")
          and vres.get("still_image_time_sec") is not None
          and (vres.get("duration_sec") or 99) <= 5.0)
    if pk.get("mode") != "skipped":
        ok = ok and pk.get("is_photoLive") and pk.get("has_paired_video_resource")
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
