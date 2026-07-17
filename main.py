"""
Tiny YouTube stream-URL resolver for Lebanese Black Dude, deployed on Render's free
tier. Public cobalt/Invidious/Piped instances all hit YouTube's "sign in to confirm
you're not a bot" wall as of 2026-07 — yt-dlp's own extractor (actively maintained,
handles signature ciphers + proof-of-origin tokens internally) works with ZERO
cookies for a handful of extremely popular/cached videos, but shared cloud-host IPs
(Render, GCP, AWS, etc.) are IP-reputation flagged by YouTube for everything else —
confirmed by direct testing 2026-07-17, every non-mega-viral video hit the bot wall.
Cookies from a real logged-in session are the standard, yt-dlp-recommended fix for
this exact error. Set YOUTUBE_COOKIES as a Render env var — either the raw Netscape
cookies.txt content, OR that same content base64-encoded (auto-detected below), to
survive any dashboard text field that might mangle embedded newlines.

Even with valid cookies, a session used from a datacenter IP far from where it was
issued can get served a different (sometimes url-less, SABR-only) format list than
the same account gets from a residential IP — confirmed 2026-07-17: identical cookies
resolved fine from a residential test but returned "Requested format is not
available" from this Render instance. There's no clean fix for that (it's Google's
own fraud heuristic, not something client-side flags control), so resolve() below
tries several player_client / format-selector combinations in sequence and only
gives up once all of them fail, to maximize the chance one gets a servable format.

It does not download or proxy the actual audio/video bytes — it just resolves a
YouTube video ID to a direct googlevideo.com URL, exactly like cobalt's tunnel URLs
did. The Cloudflare Worker (or the client directly) fetches that URL itself. This
keeps the service fast (a few seconds per request) and cheap.
"""
import base64
import os
import re
import tempfile
from flask import Flask, request, jsonify
import yt_dlp

app = Flask(__name__)

SHARED_SECRET = os.environ.get("SHARED_SECRET", "")
VIDEO_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")

COOKIES_PATH = os.path.join(tempfile.gettempdir(), "lbd_cookies.txt")


def check_secret() -> bool:
    if not SHARED_SECRET:
        return True  # not configured — allow (set SHARED_SECRET in prod to require it)
    return request.args.get("secret") == SHARED_SECRET


def _load_cookies() -> bool:
    """Accepts YOUTUBE_COOKIES as either raw Netscape cookies.txt text or that same
    text base64-encoded (some dashboard text fields mangle embedded newlines, which
    base64 survives unconditionally). Returns True if a cookie file was written."""
    raw = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if not raw:
        return False
    content = raw
    if "\n" not in raw and "\t" not in raw:
        # No literal newlines/tabs survived — either it's base64, or the dashboard
        # collapsed the real thing into one line (unrecoverable, but try decoding
        # first since that's the far more common case with a one-line value).
        try:
            decoded = base64.b64decode(raw, validate=True).decode("utf-8")
            if "youtube.com" in decoded or "Netscape" in decoded:
                content = decoded
        except Exception:
            pass
    with open(COOKIES_PATH, "w", encoding="utf-8") as f:
        f.write(content.rstrip("\n") + "\n")
    return True


_have_cookies = _load_cookies()

# Each strategy is tried in order until one yields a directly playable URL. Confirmed
# by direct testing 2026-07-17: with cookies active, the 'android'/'web'/combined
# clients get throttled down to a storyboard-thumbnails-only format list (4-5
# entries, nothing playable), while 'tv' alone still returns the full ~30-format
# list and resolves normally — so it's tried first, with the others kept only as
# a fallback in case some future video/account behaves differently.
_STRATEGIES = [
    {"player_client": ["tv"]},
    {"player_client": ["android", "ios", "web"]},
    {"player_client": ["web"]},
    {"player_client": ["android"]},
]
# itag 18 (360p H.264/AAC combined) is YouTube's oldest, most universally-available
# progressive format — kept for compatibility across every client for 15+ years.
# When cookies+datacenter-IP causes YouTube to serve a restricted/manifest-only
# format list to everything else, 18 is the one concrete format worth trying by
# exact itag rather than a selector that depends on what's actually offered.
_FORMAT_FALLBACKS = {
    "audio": ["bestaudio/best", "18"],
    "video": ["best[acodec!=none][vcodec!=none]/best", "18"],
}


def _extract_direct_url(info: dict, kind: str):
    if info.get("url"):
        return info["url"]
    # Some format selections resolve to a merged pair instead of one direct url —
    # fall back to picking the best single combined (audio+video) format manually.
    formats = info.get("formats") or []
    candidates = [f for f in formats if f.get("url") and f.get("acodec") not in (None, "none")
                  and (kind == "audio" or f.get("vcodec") not in (None, "none"))]
    if not candidates:
        return None
    candidates.sort(key=lambda f: (f.get("abr") or 0) + (f.get("tbr") or 0), reverse=True)
    return candidates[0]["url"]


def resolve(video_id: str, kind: str):
    url = f"https://www.youtube.com/watch?v={video_id}"
    errors = []
    # A cookie session can itself end up flagged/restricted (confirmed 2026-07-17 —
    # even a mega-viral video that resolved fine with zero cookies returned nothing
    # but storyboard thumbnails once cookies were added), so cookie-using attempts
    # are tried first, then the exact same strategy list is retried with cookies
    # off entirely as a last resort rather than letting a poisoned session block
    # every request.
    cookie_modes = [True, False] if _have_cookies else [False]
    for use_cookies in cookie_modes:
        for strategy in _STRATEGIES:
            for fmt in _FORMAT_FALLBACKS[kind]:
                ydl_opts = {
                    "format": fmt,
                    "quiet": True,
                    "no_warnings": True,
                    "noplaylist": True,
                    "skip_download": True,
                    "extractor_args": {"youtube": strategy},
                }
                if use_cookies:
                    ydl_opts["cookiefile"] = COOKIES_PATH
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=False)
                    stream_url = _extract_direct_url(info, kind)
                    if stream_url:
                        return stream_url, info.get("title")
                    errors.append(f"cookies={use_cookies}/{strategy['player_client']}/{fmt}: no url in formats")
                except Exception as e:
                    errors.append(f"cookies={use_cookies}/{strategy['player_client']}/{fmt}: {str(e)[:150]}")
    # Server-side only — helps diagnose which strategies failed without exposing
    # internals to the client response.
    print(f"resolve({video_id}, {kind}) exhausted all strategies: {errors}")
    return None, None


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


@app.route("/debug")
def debug_route():
    if not check_secret():
        return jsonify({"error": "unauthorized"}), 401
    video_id = request.args.get("id", "")
    if not VIDEO_ID_RE.match(video_id):
        return jsonify({"error": "bad id"}), 400
    use_cookies = _have_cookies and request.args.get("nocookies") != "1"
    client = request.args.get("client", "android,ios,web").split(",")
    ydl_opts = {
        "format": "all",  # a selector that matches everything — can't itself fail,
                          # unlike the default 'best' selector this is diagnosing.
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "extractor_args": {"youtube": {"player_client": client}},
    }
    if use_cookies:
        ydl_opts["cookiefile"] = COOKIES_PATH
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    except Exception as e:
        return jsonify({"error": str(e)[:400]}), 502
    formats = info.get("formats") or []
    summary = [{
        "format_id": f.get("format_id"), "protocol": f.get("protocol"),
        "acodec": f.get("acodec"), "vcodec": f.get("vcodec"),
        "has_url": bool(f.get("url")), "has_manifest_url": bool(f.get("manifest_url")),
        "ext": f.get("ext"),
    } for f in formats]
    return jsonify({
        "used_cookies": use_cookies, "client": client,
        "top_level_url": bool(info.get("url")), "format_count": len(formats), "formats": summary,
    })


@app.route("/health")
def health():
    return jsonify({"ok": True, "cookies_loaded": _have_cookies})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
