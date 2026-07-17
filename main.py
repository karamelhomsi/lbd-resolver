"""
Tiny YouTube stream-URL resolver for Lebanese Black Dude, deployed on Google Cloud
Run's always-free tier. Public cobalt/Invidious/Piped instances all hit YouTube's
"sign in to confirm you're not a bot" wall as of 2026-07 — yt-dlp's own extractor
(actively maintained, handles signature ciphers + proof-of-origin tokens internally)
still works with ZERO cookies/login, confirmed by direct testing. This service does
NOT store any personal account data — it's a stateless resolver, nothing to leak.

It does not download or proxy the actual audio/video bytes — it just resolves a
YouTube video ID to a direct googlevideo.com URL, exactly like cobalt's tunnel URLs
did. The Cloudflare Worker (or the client directly) fetches that URL itself. This
keeps the service fast (a few seconds per request) and cheap (well within Cloud
Run's always-free 2M requests/month), since it never has to stream large files.
"""
import os
import re
from flask import Flask, request, jsonify
import yt_dlp

app = Flask(__name__)

SHARED_SECRET = os.environ.get("SHARED_SECRET", "")
VIDEO_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")


def check_secret() -> bool:
    if not SHARED_SECRET:
        return True  # not configured — allow (set SHARED_SECRET in prod to require it)
    return request.args.get("secret") == SHARED_SECRET


def resolve(video_id: str, kind: str):
    url = f"https://www.youtube.com/watch?v={video_id}"
    fmt = "bestaudio/best" if kind == "audio" else "best[acodec!=none][vcodec!=none]/best"
    ydl_opts = {
        "format": fmt,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "extractor_args": {"youtube": {"player_client": ["android", "ios", "web"]}},
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if info.get("url"):
        return info["url"], info.get("title")

    # Some format selections resolve to a merged pair instead of one direct url —
    # fall back to picking the best single combined (audio+video) format manually.
    formats = info.get("formats") or []
    candidates = [f for f in formats if f.get("url") and f.get("acodec") not in (None, "none")
                  and (kind == "audio" or f.get("vcodec") not in (None, "none"))]
    if not candidates:
        return None, None
    candidates.sort(key=lambda f: (f.get("abr") or 0) + (f.get("tbr") or 0), reverse=True)
    return candidates[0]["url"], info.get("title")


@app.route("/resolve")
def resolve_route():
    if not check_secret():
        return jsonify({"error": "unauthorized"}), 401

    video_id = request.args.get("id", "")
    kind = request.args.get("type", "audio")
    if not VIDEO_ID_RE.match(video_id):
        return jsonify({"error": "bad id"}), 400
    if kind not in ("audio", "video"):
        return jsonify({"error": "bad type"}), 400

    try:
        stream_url, title = resolve(video_id, kind)
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 502

    if not stream_url:
        return jsonify({"error": "no playable format found"}), 502

    return jsonify({"url": stream_url, "title": title})


@app.route("/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
