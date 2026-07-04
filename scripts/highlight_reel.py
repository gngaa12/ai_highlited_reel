#!/usr/bin/env python3
"""
AI Highlight Reel Generator
Free stack:
  - yt-dlp          -> download the source video
  - faster-whisper   -> free transcription with per-sentence timestamps
  - pydub            -> measures audio loudness/energy per segment (free,
                         uses ffmpeg under the hood)
  - keyword scoring   -> spots excitement in the transcript ("wow", "no
                         way", laughter, etc.)
  - ffmpeg            -> cuts the top-scoring moments and stitches them
                         into one highlight reel, then burns captions
  - Facebook Reels API -> publishes the result

How "AI picks the best moments" actually works here (no black box):
  score = loudness_spike + excitement_keyword_count
  -> top N highest-scoring moments get kept, back in chronological order

Triggered manually with:
  VIDEO_URL      link to the long source video
  MAX_HIGHLIGHTS how many top moments to include (default 6)

Env vars required (GitHub Actions secrets):
  FB_PAGE_ID
  FB_PAGE_ACCESS_TOKEN
"""

import os
import subprocess
import sys
import time

import requests
from faster_whisper import WhisperModel
from pydub import AudioSegment

VIDEO_URL = os.environ["VIDEO_URL"]
MAX_HIGHLIGHTS = int(os.environ.get("MAX_HIGHLIGHTS", "6"))
POST_MODE = os.environ.get("POST_MODE", "combined")  # "combined" or "separate"
CLIP_SECONDS = float(os.environ.get("CLIP_SECONDS", "35"))  # target length per highlight
FB_PAGE_ID = os.environ["FB_PAGE_ID"]
FB_PAGE_TOKEN = os.environ["FB_PAGE_ACCESS_TOKEN"]
GRAPH_VERSION = "v19.0"

WORKDIR = "work"
ORIGINAL_VIDEO = f"{WORKDIR}/original.mp4"
AUDIO_WAV = f"{WORKDIR}/audio.wav"
RAW_HIGHLIGHTS = f"{WORKDIR}/highlights_raw.mp4"
CAPTIONS_SRT = f"{WORKDIR}/captions.srt"
FINAL_VIDEO = f"{WORKDIR}/final_highlight_reel.mp4"
UPLOAD_DELAY_SECONDS = 20  # gap between separate uploads

PADDING_SECONDS = 0.4

EXCITEMENT_KEYWORDS = [
    "wow", "amazing", "insane", "unbelievable", "no way", "haha", "lol",
    "omg", "incredible", "let's go", "yes!", "what the", "crazy", "wild",
    "best", "worst", "never seen", "shocking", "oh my god", "whoa",
]


def run(cmd):
    print("RUN:", " ".join(cmd))
    subprocess.run(cmd, check=True)


def download_video():
    os.makedirs(WORKDIR, exist_ok=True)
    run(["yt-dlp", "-f", "mp4", "-o", ORIGINAL_VIDEO, VIDEO_URL])


def extract_audio():
    run([
        "ffmpeg", "-y", "-i", ORIGINAL_VIDEO,
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        AUDIO_WAV,
    ])


def transcribe():
    print("Loading Whisper model (first run downloads it, ~150MB)...")
    model = WhisperModel("small", device="cpu", compute_type="int8")
    segments, info = model.transcribe(AUDIO_WAV, word_timestamps=False)
    segments = list(segments)
    print(f"Detected language: {info.language}, {len(segments)} segments found")
    return segments


def keyword_score(text):
    t = text.lower()
    return sum(1 for kw in EXCITEMENT_KEYWORDS if kw in t)


def score_segments(segments):
    audio = AudioSegment.from_wav(AUDIO_WAV)
    overall_rms = audio.rms or 1

    scored = []
    for seg in segments:
        start_ms = int(seg.start * 1000)
        end_ms = int(seg.end * 1000)
        clip = audio[start_ms:end_ms]
        clip_rms = clip.rms if len(clip) > 0 else 0
        energy_score = clip_rms / overall_rms

        kw_score = keyword_score(seg.text)
        total_score = energy_score + (kw_score * 0.5)

        scored.append({
            "start": seg.start,
            "end": seg.end,
            "text": seg.text.strip(),
            "energy_score": round(energy_score, 3),
            "keyword_score": kw_score,
            "total_score": round(total_score, 3),
        })
    return scored


def pick_top_highlights(scored_segments):
    ranked = sorted(scored_segments, key=lambda s: s["total_score"], reverse=True)
    top = ranked[:MAX_HIGHLIGHTS]

    print("\n=== Top scoring moments (peak points, before expanding) ===")
    for s in top:
        print(f"[{s['start']:.1f}s-{s['end']:.1f}s] score={s['total_score']} "
              f"(energy={s['energy_score']}, keywords={s['keyword_score']}): {s['text']}")

    top.sort(key=lambda s: s["start"])
    return top


def expand_and_merge_windows(highlights, video_duration, target_seconds):
    """
    Whisper segments are only a few seconds long (one sentence).
    This expands each highlight into a real ~target_seconds-long window
    centered on the exciting moment, then merges any windows that end up
    overlapping so the same footage doesn't get used twice.
    """
    half = target_seconds / 2.0
    windows = []
    for h in highlights:
        center = (h["start"] + h["end"]) / 2.0
        start = max(0.0, center - half)
        end = min(video_duration, center + half)
        # if we hit the start/end of the video, extend the other side to
        # still try to hit the target length where possible
        actual_len = end - start
        if actual_len < target_seconds:
            if start == 0.0:
                end = min(video_duration, target_seconds)
            elif end == video_duration:
                start = max(0.0, video_duration - target_seconds)
        windows.append({
            "start": start,
            "end": end,
            "text": h["text"],
            "total_score": h["total_score"],
        })

    windows.sort(key=lambda w: w["start"])
    merged = []
    for w in windows:
        if merged and w["start"] <= merged[-1]["end"] + 2.0:
            merged[-1]["end"] = max(merged[-1]["end"], w["end"])
            if w["text"] not in merged[-1]["text"]:
                merged[-1]["text"] = merged[-1]["text"] + " " + w["text"]
        else:
            merged.append(dict(w))

    print(f"\n=== Expanded to {len(merged)} window(s) of ~{target_seconds:.0f}s each ===")
    for w in merged:
        print(f"[{w['start']:.1f}s-{w['end']:.1f}s] ({w['end']-w['start']:.1f}s long): {w['text']}")

    return merged


def cut_and_stitch_clips(highlights, video_duration):
    clip_paths = []
    for i, h in enumerate(highlights):
        start = h["start"]
        end = h["end"]
        clip_path = f"{WORKDIR}/clip_{i}.mp4"
        run([
            "ffmpeg", "-y", "-i", ORIGINAL_VIDEO,
            "-ss", str(start), "-to", str(end),
            "-c:v", "libx264", "-c:a", "aac",
            clip_path,
        ])
        clip_paths.append(clip_path)

    list_file = f"{WORKDIR}/clip_list.txt"
    with open(list_file, "w") as f:
        for p in clip_paths:
            f.write(f"file '{os.path.basename(p)}'\n")

    run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", list_file,
        "-c", "copy",
        RAW_HIGHLIGHTS,
    ])


def get_video_duration(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def format_srt_timestamp(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def caption_the_reel():
    """Re-transcribe the finished reel so captions line up with the new timeline."""
    model = WhisperModel("small", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(RAW_HIGHLIGHTS, word_timestamps=False)

    srt_lines = []
    for i, seg in enumerate(segments, start=1):
        start = format_srt_timestamp(seg.start)
        end = format_srt_timestamp(seg.end)
        text = seg.text.strip()
        srt_lines.append(f"{i}\n{start} --> {end}\n{text}\n")

    if not srt_lines:
        return False

    with open(CAPTIONS_SRT, "w", encoding="utf-8") as f:
        f.write("\n".join(srt_lines))
    return True


def burn_captions():
    style = (
        "FontName=Arial,FontSize=14,Bold=1,"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
        "BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV=40"
    )
    run([
        "ffmpeg", "-y", "-i", RAW_HIGHLIGHTS,
        "-vf", f"subtitles={CAPTIONS_SRT}:force_style='{style}'",
        "-c:a", "copy",
        FINAL_VIDEO,
    ])


def caption_individual_clip(clip_path, index):
    """Transcribe and burn captions onto a single standalone clip."""
    model = WhisperModel("small", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(clip_path, word_timestamps=False)

    srt_path = f"{WORKDIR}/captions_{index}.srt"
    srt_lines = []
    for i, seg in enumerate(segments, start=1):
        start = format_srt_timestamp(seg.start)
        end = format_srt_timestamp(seg.end)
        text = seg.text.strip()
        srt_lines.append(f"{i}\n{start} --> {end}\n{text}\n")

    captioned_path = f"{WORKDIR}/captioned_clip_{index}.mp4"
    if not srt_lines:
        os.replace(clip_path, captioned_path)
        return captioned_path

    with open(srt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(srt_lines))

    style = (
        "FontName=Arial,FontSize=14,Bold=1,"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
        "BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV=40"
    )
    run([
        "ffmpeg", "-y", "-i", clip_path,
        "-vf", f"subtitles={srt_path}:force_style='{style}'",
        "-c:a", "copy",
        captioned_path,
    ])
    return captioned_path


def post_highlights_separately(highlights, video_duration):
    for i, h in enumerate(highlights):
        start = h["start"]
        end = h["end"]
        raw_clip_path = f"{WORKDIR}/clip_{i}.mp4"

        run([
            "ffmpeg", "-y", "-i", ORIGINAL_VIDEO,
            "-ss", str(start), "-to", str(end),
            "-c:v", "libx264", "-c:a", "aac",
            raw_clip_path,
        ])

        captioned_path = caption_individual_clip(raw_clip_path, i)

        caption_text = h["text"][:200] if h["text"] else "AI-picked highlight 🔥"
        print(f"\n=== Posting highlight {i+1}/{len(highlights)}: {caption_text}")
        upload_reel_to_facebook(captioned_path, caption_text)

        if i < len(highlights) - 1:
            time.sleep(UPLOAD_DELAY_SECONDS)


def upload_reel_to_facebook(video_path, caption):
    start_url = f"https://graph.facebook.com/{GRAPH_VERSION}/{FB_PAGE_ID}/video_reels"
    start_resp = requests.post(start_url, params={
        "upload_phase": "start",
        "access_token": FB_PAGE_TOKEN,
    })
    if start_resp.status_code != 200:
        print("REELS START ERROR:", start_resp.text)
    start_resp.raise_for_status()
    start_data = start_resp.json()
    video_id = start_data["video_id"]
    upload_url = start_data["upload_url"]
    print("Reel upload session started:", start_data)

    file_size = os.path.getsize(video_path)
    with open(video_path, "rb") as f:
        video_bytes = f.read()

    upload_headers = {
        "Authorization": f"OAuth {FB_PAGE_TOKEN}",
        "file_size": str(file_size),
        "offset": "0",
        "Content-Type": "application/octet-stream",
    }
    upload_resp = requests.post(upload_url, headers=upload_headers, data=video_bytes, timeout=600)
    if upload_resp.status_code != 200:
        print("REELS UPLOAD ERROR:", upload_resp.text)
    upload_resp.raise_for_status()
    print("Upload response:", upload_resp.json())

    finish_url = f"https://graph.facebook.com/{GRAPH_VERSION}/{FB_PAGE_ID}/video_reels"
    finish_resp = requests.post(finish_url, params={
        "access_token": FB_PAGE_TOKEN,
        "video_id": video_id,
        "upload_phase": "finish",
        "video_state": "PUBLISHED",
        "description": caption,
    })
    if finish_resp.status_code != 200:
        print("REELS PUBLISH ERROR:", finish_resp.text)
    finish_resp.raise_for_status()
    print("Facebook Reel publish response:", finish_resp.json())


def main():
    print(f"=== Building AI highlight reel from {VIDEO_URL} ===")
    download_video()
    extract_audio()
    video_duration = get_video_duration(ORIGINAL_VIDEO)
    print(f"Source video duration: {video_duration:.1f}s")

    segments = transcribe()
    if not segments:
        print("No speech detected, cannot score highlights.", file=sys.stderr)
        sys.exit(1)

    scored = score_segments(segments)
    highlights = pick_top_highlights(scored)
    if not highlights:
        print("No highlights selected.", file=sys.stderr)
        sys.exit(1)

    highlights = expand_and_merge_windows(highlights, video_duration, CLIP_SECONDS)

    if POST_MODE == "separate":
        post_highlights_separately(highlights, video_duration)
        print(f"=== Done! {len(highlights)} separate highlight clips posted to Facebook. ===")
        return

    cut_and_stitch_clips(highlights, video_duration)

    has_captions = caption_the_reel()
    if has_captions:
        burn_captions()
    else:
        os.replace(RAW_HIGHLIGHTS, FINAL_VIDEO)

    upload_reel_to_facebook(FINAL_VIDEO, "AI-picked highlights 🔥")
    print("=== Done! Combined highlight reel posted to Facebook. ===")


if __name__ == "__main__":
    main()
