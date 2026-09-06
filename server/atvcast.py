#!/usr/bin/env python3
"""atvcast - remux-only MKV -> Apple TV friendly MP4 server.

Stdlib only. Requires ffmpeg/ffprobe on PATH.

    python3 atvcast.py --media ~/Videos --port 8080
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

# --------------------------------------------------------------------------
# Constants / decision tables
# --------------------------------------------------------------------------

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".mov", ".avi", ".ts", ".m2ts", ".webm", ".wmv", ".mpg", ".mpeg"}
SRT_EXTS = {".srt"}

AUDIO_COPY_CODECS = {"ac3", "eac3", "aac"}
SUB_TEXT_CODECS = {"subrip", "ass", "ssa", "webvtt", "mov_text", "text", "srt"}
SUB_DROP_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub", "hdmv_text_subtitle"}

EAC3_MAX_CHANNELS = 6
EAC3_BITRATE = "768k"

HI10P_PROFILES = {"high10", "high10intra"}

# ISO 639-1 -> 639-2/B used by Apple. Only the codes that actually turn up in
# consumer rips; anything already 3 letters is passed through untouched.
ISO1_TO_ISO2 = {
    "en": "eng", "ja": "jpn", "de": "deu", "fr": "fra", "es": "spa", "it": "ita",
    "pt": "por", "ru": "rus", "zh": "zho", "ko": "kor", "nl": "nld", "sv": "swe",
    "no": "nor", "da": "dan", "fi": "fin", "pl": "pol", "cs": "ces", "hu": "hun",
    "tr": "tur", "ar": "ara", "he": "heb", "hi": "hin", "th": "tha", "vi": "vie",
    "el": "ell", "uk": "ukr", "ro": "ron", "bg": "bul", "hr": "hrv", "sr": "srp",
    "sk": "slk", "sl": "slv", "ca": "cat", "id": "ind", "ms": "msa", "fa": "fas",
}

# Fallback: infer a language tag from a free-text track title.
TITLE_TO_ISO2 = {
    "english": "eng", "eng": "eng", "japanese": "jpn", "jpn": "jpn", "german": "deu",
    "deutsch": "deu", "ger": "deu", "french": "fra", "francais": "fra", "fre": "fra",
    "spanish": "spa", "espanol": "spa", "italian": "ita", "portuguese": "por",
    "brazilian": "por", "russian": "rus", "chinese": "zho", "mandarin": "zho",
    "cantonese": "yue", "korean": "kor", "dutch": "nld", "swedish": "swe",
    "norwegian": "nor", "danish": "dan", "finnish": "fin", "polish": "pol",
    "czech": "ces", "hungarian": "hun", "turkish": "tur", "arabic": "ara",
    "hebrew": "heb", "hindi": "hin", "thai": "tha", "vietnamese": "vie",
    "greek": "ell", "ukrainian": "ukr", "romanian": "ron", "commentary": None,
}

# Python codec -> the name ffmpeg's -sub_charenc (iconv) expects.
CHARENC_FFMPEG = {"utf-8": "UTF-8", "utf-8-sig": "UTF-8", "cp1252": "CP1252", "latin-1": "ISO-8859-1"}
SRT_ENCODING_ORDER = ["utf-8", "cp1252", "latin-1"]

CHUNK = 256 * 1024


# --------------------------------------------------------------------------
# ffprobe / decision logic  (pure functions - exercised by --selftest)
# --------------------------------------------------------------------------


def ffprobe(path: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


def norm_lang(raw) -> str | None:
    if not raw:
        return None
    v = str(raw).strip().lower().split("-")[0]
    if not v or v in ("und", "unknown", "none"):
        return None
    if len(v) == 2:
        return ISO1_TO_ISO2.get(v)
    if len(v) == 3:
        return v
    return TITLE_TO_ISO2.get(v)


def lang_from_title(title) -> str | None:
    if not title:
        return None
    for word in re.split(r"[^A-Za-z]+", str(title).lower()):
        if word in TITLE_TO_ISO2 and TITLE_TO_ISO2[word]:
            return TITLE_TO_ISO2[word]
    return None


def resolve_track_identity(stream: dict, ordinal: int) -> tuple[str | None, str]:
    """(iso639-2 language or None, display label). Untagged -> 'Track N'."""
    tags = {k.lower(): v for k, v in (stream.get("tags") or {}).items()}
    title = tags.get("title")
    lang = norm_lang(tags.get("language"))
    if lang is None:
        lang = lang_from_title(title)
    label = str(title).strip() if title and str(title).strip() else "Track %d" % (ordinal + 1)
    return lang, label


def pick_video(streams: list) -> tuple[dict | None, str | None]:
    """First real video stream + rejection reason for the whole file, if any."""
    vids = [s for s in streams
            if s.get("codec_type") == "video"
            and (s.get("disposition") or {}).get("attached_pic", 0) != 1
            and s.get("codec_name") not in ("mjpeg", "png", "bmp", "gif")]
    if not vids:
        return None, "No decodable video stream found"
    v = vids[0]
    profile = re.sub(r"\s+", "", str(v.get("profile") or "")).lower()
    if v.get("codec_name") == "h264" and profile in HI10P_PROFILES:
        return v, "Hi10P not supported by Apple TV"
    return v, None


def plan_audio(streams: list, selected: set | None = None) -> list:
    """Per-track copy/transcode decision. selected = set of source stream indices."""
    plan = []
    for ordinal, s in enumerate(a for a in streams if a.get("codec_type") == "audio"):
        codec = (s.get("codec_name") or "").lower()
        channels = int(s.get("channels") or 2)
        lang, label = resolve_track_identity(s, ordinal)
        copyable = codec in AUDIO_COPY_CODECS
        target = min(channels, EAC3_MAX_CHANNELS)  # eac3 encoder tops out at 6
        plan.append({
            "index": int(s["index"]),
            "ordinal": ordinal,
            "codec": codec,
            "channels": channels,
            "language": lang,
            "label": label,
            "selected": True if selected is None else int(s["index"]) in selected,
            "action": "copy" if copyable else "transcode",
            "target_channels": channels if copyable else target,
            "note": "" if copyable else "%s -> eac3 %dch%s" % (
                codec or "unknown", target,
                " (downmixed from %d, eac3 caps at 6)" % channels if channels > EAC3_MAX_CHANNELS else ""),
        })
    return plan


def plan_subs(streams: list, selected: set | None = None) -> tuple[list, list]:
    """(keepable text subtitle plan, dropped bitmap subtitle report)."""
    keep, dropped = [], []
    for ordinal, s in enumerate(x for x in streams if x.get("codec_type") == "subtitle"):
        codec = (s.get("codec_name") or "").lower()
        lang, label = resolve_track_identity(s, ordinal)
        entry = {"index": int(s["index"]), "ordinal": ordinal, "codec": codec,
                 "language": lang, "label": label}
        if codec in SUB_TEXT_CODECS:
            entry["selected"] = True if selected is None else int(s["index"]) in selected
            keep.append(entry)
        else:
            reason = ("bitmap subtitles (%s) cannot be converted to mov_text" % codec
                      if codec in SUB_DROP_CODECS else "unsupported subtitle codec '%s'" % codec)
            entry["reason"] = reason
            dropped.append(entry)
    return keep, dropped


def analyze(probe: dict, audio_sel=None, sub_sel=None) -> dict:
    streams = probe.get("streams") or []
    fmt = probe.get("format") or {}
    video, reject = pick_video(streams)
    audio = plan_audio(streams, audio_sel)
    subs, dropped = plan_subs(streams, sub_sel)
    transcode_n = sum(1 for a in audio if a["selected"] and a["action"] == "transcode")
    warnings = []
    if transcode_n > 1:
        warnings.append("%d audio tracks need transcoding - this is the slow part, "
                        "expect a long job." % transcode_n)
    elif transcode_n == 1:
        warnings.append("1 audio track needs transcoding (eac3); video is still copied.")
    if not audio:
        warnings.append("File has no audio streams.")
    return {
        "reject": reject,
        "duration": float(fmt.get("duration") or 0.0),
        "video": None if video is None else {
            "index": int(video["index"]),
            "codec": video.get("codec_name"),
            "profile": video.get("profile"),
            "width": video.get("width"),
            "height": video.get("height"),
            "color": color_tags(video),
        },
        "audio": audio,
        "subs": subs,
        "dropped_subs": dropped,
        "warnings": warnings,
    }


def color_tags(stream: dict) -> dict:
    out = {}
    for k in ("color_transfer", "color_primaries", "color_space", "color_range"):
        v = stream.get(k)
        if v and str(v).lower() not in ("unknown", "unspecified", "reserved"):
            out[k] = str(v)
    return out


def is_hdr(tags: dict) -> bool:
    return tags.get("color_transfer", "").lower() in ("smpte2084", "arib-std-b67")


# --------------------------------------------------------------------------
# External SRT handling
# --------------------------------------------------------------------------


def sniff_srt(path: str, workdir: str) -> tuple[str, str, str]:
    """(path to feed ffmpeg, ffmpeg charenc name, note).

    Tries utf-8, cp1252, latin-1 in order. Any BOM is stripped into a copy so
    ffmpeg never sees it - a BOM in the first cue is either mojibake or a hard
    demuxer failure.
    """
    with open(path, "rb") as fh:
        raw = fh.read()
    bom = ""
    if raw.startswith(b"\xef\xbb\xbf"):
        raw, bom = raw[3:], "UTF-8 BOM"
    elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        enc = "utf-16"
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        clean = os.path.join(workdir, "sub_%s.srt" % uuid.uuid4().hex[:8])
        with open(clean, "w", encoding="utf-8", newline="") as fh:
            fh.write(text.lstrip("﻿"))
        return clean, "UTF-8", "re-encoded from UTF-16 to UTF-8"

    chosen = None
    for enc in SRT_ENCODING_ORDER:
        try:
            raw.decode(enc)
            chosen = enc
            break
        except UnicodeDecodeError:
            continue
    chosen = chosen or "latin-1"  # latin-1 never fails, but be explicit
    ffenc = CHARENC_FFMPEG[chosen]

    src = path
    note = "decoded as %s" % ffenc
    if bom:
        src = os.path.join(workdir, "sub_%s.srt" % uuid.uuid4().hex[:8])
        with open(src, "wb") as fh:
            fh.write(raw)
        note += " (%s stripped)" % bom
    return src, ffenc, note


# --------------------------------------------------------------------------
# ffmpeg command construction
# --------------------------------------------------------------------------


def build_command(source: str, plan: dict, out_path: str, ext_subs: list,
                  color_override: dict | None = None) -> list:
    """Remux-only command. Video is always copied; audio is per-track."""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error", "-progress", "pipe:1",
           "-i", source]

    for e in ext_subs:
        if e.get("offset"):
            cmd += ["-itsoffset", str(e["offset"])]
        if e.get("charenc"):
            cmd += ["-sub_charenc", e["charenc"]]
        cmd += ["-i", e["path"]]

    audio = [a for a in plan["audio"] if a["selected"]]
    subs = [s for s in plan["subs"] if s.get("selected")]

    cmd += ["-map", "0:%d" % plan["video"]["index"]]
    for a in audio:
        cmd += ["-map", "0:%d" % a["index"]]
    for s in subs:
        cmd += ["-map", "0:%d" % s["index"]]
    for i, _e in enumerate(ext_subs):
        cmd += ["-map", "%d:0" % (i + 1)]

    cmd += ["-c:v", "copy"]

    for oi, a in enumerate(audio):
        if a["action"] == "copy":
            cmd += ["-c:a:%d" % oi, "copy"]
        else:
            cmd += ["-c:a:%d" % oi, "eac3",
                    "-b:a:%d" % oi, EAC3_BITRATE,
                    "-ac:a:%d" % oi, str(min(a["target_channels"], EAC3_MAX_CHANNELS))]
        cmd += ["-metadata:s:a:%d" % oi, "language=%s" % (a["language"] or "und")]
        cmd += ["-metadata:s:a:%d" % oi, "title=%s" % a["label"]]
        cmd += ["-disposition:a:%d" % oi, "default" if oi == 0 else "0"]

    all_subs = [{"language": s["language"], "label": s["label"]} for s in subs]
    all_subs += [{"language": e.get("lang"), "label": e.get("label") or "External"} for e in ext_subs]
    if all_subs:
        cmd += ["-c:s", "mov_text"]
        for oi, s in enumerate(all_subs):
            cmd += ["-metadata:s:s:%d" % oi, "language=%s" % (s["language"] or "und")]
            cmd += ["-metadata:s:s:%d" % oi, "title=%s" % s["label"]]

    if color_override:
        # Metadata only - pixels are untouched. Without these the Apple TV falls
        # back to BT.709 and HDR grades look washed out.
        if color_override.get("color_transfer"):
            cmd += ["-color_trc", color_override["color_transfer"]]
        if color_override.get("color_primaries"):
            cmd += ["-color_primaries", color_override["color_primaries"]]
        if color_override.get("color_space"):
            cmd += ["-colorspace", color_override["color_space"]]
        if color_override.get("color_range"):
            cmd += ["-color_range", color_override["color_range"]]

    cmd += ["-movflags", "+faststart", out_path]
    return cmd


# --------------------------------------------------------------------------
# Job store
# --------------------------------------------------------------------------


class Store:
    def __init__(self, cache_dir: str):
        self.cache = cache_dir
        self.work = os.path.join(cache_dir, "work")
        os.makedirs(self.work, exist_ok=True)
        self.index_path = os.path.join(cache_dir, "index.json")
        self.lock = threading.RLock()
        self.items: dict = {}
        self._load()

    def _load(self):
        try:
            with open(self.index_path, "r", encoding="utf-8") as fh:
                for it in json.load(fh):
                    if it.get("status") == "running":
                        it["status"] = "error"
                        it.setdefault("notes", []).append("Interrupted by server restart")
                    self.items[it["id"]] = it
        except (OSError, ValueError):
            self.items = {}

    def save(self):
        with self.lock:
            tmp = self.index_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(list(self.items.values()), fh, indent=2)
            os.replace(tmp, self.index_path)

    def put(self, item: dict):
        with self.lock:
            self.items[item["id"]] = item
            self.save()

    def update(self, item_id: str, **kw):
        with self.lock:
            it = self.items.get(item_id)
            if not it:
                return
            it.update(kw)
            self.save()

    def note(self, item_id: str, text: str):
        with self.lock:
            it = self.items.get(item_id)
            if it and text not in it.setdefault("notes", []):
                it["notes"].append(text)
                self.save()

    def all(self) -> list:
        with self.lock:
            return sorted(self.items.values(), key=lambda i: i.get("created", 0), reverse=True)

    def ready(self) -> list:
        return [i for i in self.all()
                if i.get("status") == "ready" and i.get("output") and os.path.exists(i["output"])]

    def get(self, item_id: str) -> dict | None:
        with self.lock:
            return self.items.get(item_id)


class Converter(threading.Thread):
    """Single serial worker - transcodes are CPU bound, running them in
    parallel just makes every job slower."""

    def __init__(self, store: Store):
        super().__init__(daemon=True)
        self.store = store
        self.queue: list = []
        self.cv = threading.Condition()
        self.proc: subprocess.Popen | None = None

    def submit(self, job: dict):
        with self.cv:
            self.queue.append(job)
            self.cv.notify()

    def run(self):
        while True:
            with self.cv:
                while not self.queue:
                    self.cv.wait()
                job = self.queue.pop(0)
            try:
                self._run_job(job)
            except Exception as exc:  # keep the worker alive whatever happens
                self.store.update(job["id"], status="error", progress=0)
                self.store.note(job["id"], "Internal error: %s" % exc)

    def _exec(self, item_id: str, cmd: list, duration: float) -> tuple[int, str]:
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, bufsize=1)
        err_tail: list = []

        def drain_err():
            for line in self.proc.stderr:
                err_tail.append(line.rstrip())
                del err_tail[:-40]

        t = threading.Thread(target=drain_err, daemon=True)
        t.start()

        for line in self.proc.stdout:
            line = line.strip()
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            if key in ("out_time_us", "out_time_ms") and duration > 0:
                try:
                    secs = int(val) / 1_000_000.0  # ffmpeg reports us for both keys
                except ValueError:
                    continue
                pct = max(0.0, min(99.0, secs / duration * 100.0))
                self.store.update(item_id, progress=round(pct, 1))
            elif key == "progress" and val == "end":
                self.store.update(item_id, progress=99.0)

        rc = self.proc.wait()
        t.join(timeout=2)
        self.proc = None
        return rc, "\n".join(err_tail[-12:])

    def _run_job(self, job: dict):
        item_id = job["id"]
        self.store.update(item_id, status="running", progress=0.0)
        cmd = build_command(job["source"], job["plan"], job["output"], job["ext_subs"])
        self.store.update(item_id, command=" ".join(cmd))
        rc, err = self._exec(item_id, cmd, job["duration"])
        if rc != 0:
            self.store.update(item_id, status="error", progress=0.0)
            self.store.note(item_id, "ffmpeg failed (exit %d): %s" % (rc, err or "no stderr"))
            return

        # HDR metadata survival check.
        src_color = job["plan"]["video"].get("color") or {}
        if src_color:
            try:
                out_color = color_tags(next(s for s in ffprobe(job["output"])["streams"]
                                            if s.get("codec_type") == "video"))
            except Exception:
                out_color = {}
            missing = {k: v for k, v in src_color.items() if out_color.get(k) != v}
            if missing:
                self.store.note(item_id, "Color metadata %s lost in the MP4 - re-muxing with "
                                         "explicit color tags" % ",".join(sorted(missing)))
                cmd2 = build_command(job["source"], job["plan"], job["output"],
                                     job["ext_subs"], color_override=src_color)
                self.store.update(item_id, status="running", progress=0.0,
                                  command=" ".join(cmd2))
                rc2, err2 = self._exec(item_id, cmd2, job["duration"])
                if rc2 != 0:
                    self.store.update(item_id, status="error", progress=0.0)
                    self.store.note(item_id, "Color re-mux failed (exit %d): %s" % (rc2, err2))
                    return
                if is_hdr(src_color):
                    self.store.note(item_id, "HDR transfer %s re-applied; no tone mapping "
                                             "performed" % src_color.get("color_transfer"))
            elif is_hdr(src_color):
                self.store.note(item_id, "HDR metadata (%s) survived the mux" %
                                src_color.get("color_transfer"))

        size = os.path.getsize(job["output"]) if os.path.exists(job["output"]) else 0
        self.store.update(item_id, status="ready", progress=100.0, size=size)


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)", re.I)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # AVPlayer needs keep-alive + real 206s
    server_version = "atvcast/1.0"

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body: bytes, ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD" and body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode("utf-8"))

    def _err(self, code, msg):
        self._json({"error": msg}, code)

    # -- path safety ------------------------------------------------------
    def _safe(self, raw: str | None) -> str | None:
        root = self.server.media_root
        if not raw:
            return root
        p = os.path.realpath(os.path.join(root, os.path.expanduser(raw)))
        if p == root or p.startswith(root + os.sep):
            return p
        return None

    # -- routing ----------------------------------------------------------
    def do_GET(self):
        self._route()

    def do_HEAD(self):
        self._route()

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/convert":
            return self.api_convert()
        return self._err(404, "not found")

    def _route(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = u.path
        try:
            if path == "/":
                return self._send(200, UI_HTML.encode("utf-8"), "text/html; charset=utf-8")
            if path == "/api/browse":
                return self.api_browse(q)
            if path == "/api/probe":
                return self.api_probe(q)
            if path == "/api/jobs":
                return self._json({"items": self.server.store.all()})
            if path == "/library":
                return self.api_library()
            if path.startswith("/media/"):
                return self.api_media(path)
            return self._err(404, "not found")
        except BrokenPipeError:
            pass
        except Exception as exc:
            try:
                self._err(500, str(exc))
            except Exception:
                pass

    # -- api --------------------------------------------------------------
    def api_browse(self, q):
        target = self._safe((q.get("path") or [None])[0])
        if not target or not os.path.isdir(target):
            return self._err(400, "not a directory under --media")
        dirs, files = [], []
        for name in sorted(os.listdir(target), key=str.lower):
            if name.startswith("."):
                continue
            full = os.path.join(target, name)
            ext = os.path.splitext(name)[1].lower()
            if os.path.isdir(full):
                dirs.append({"name": name, "path": os.path.relpath(full, self.server.media_root)})
            elif ext in VIDEO_EXTS or ext in SRT_EXTS:
                files.append({
                    "name": name,
                    "path": os.path.relpath(full, self.server.media_root),
                    "kind": "srt" if ext in SRT_EXTS else "video",
                    "size": os.path.getsize(full),
                })
        rel = os.path.relpath(target, self.server.media_root)
        parent = None if target == self.server.media_root else os.path.dirname(rel if rel != "." else "")
        return self._json({"path": "" if rel == "." else rel, "parent": parent,
                           "dirs": dirs, "files": files})

    def api_probe(self, q):
        src = self._safe((q.get("path") or [None])[0])
        if not src or not os.path.isfile(src):
            return self._err(400, "no such file under --media")
        try:
            probe = ffprobe(src)
        except subprocess.CalledProcessError as exc:
            return self._err(400, "ffprobe failed: %s" % (exc.stderr or "").strip()[:400])
        info = analyze(probe)
        info["path"] = os.path.relpath(src, self.server.media_root)
        info["title"] = os.path.splitext(os.path.basename(src))[0]
        return self._json(info)

    def api_convert(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._err(400, "bad json")

        src = self._safe(req.get("source"))
        if not src or not os.path.isfile(src):
            return self._err(400, "no such source file")

        store = self.server.store
        title = req.get("title") or os.path.splitext(os.path.basename(src))[0]
        item_id = uuid.uuid4().hex[:12]

        try:
            probe = ffprobe(src)
        except subprocess.CalledProcessError as exc:
            return self._err(400, "ffprobe failed: %s" % (exc.stderr or "").strip()[:400])

        audio_sel = set(int(i) for i in (req.get("audio") or []))
        sub_sel = set(int(i) for i in (req.get("subs") or []))
        plan = analyze(probe, audio_sel or None, sub_sel if req.get("subs") is not None else None)

        item = {"id": item_id, "title": title,
                "source": os.path.relpath(src, self.server.media_root),
                "output": None, "duration": plan["duration"], "status": "queued",
                "progress": 0.0, "created": time.time(), "notes": []}

        if plan["reject"]:
            item["status"] = "rejected"
            item["notes"].append(plan["reject"])
            store.put(item)
            return self._json({"id": item_id, "status": "rejected", "reason": plan["reject"]})

        if not any(a["selected"] for a in plan["audio"]) and plan["audio"]:
            item["status"] = "rejected"
            item["notes"].append("No audio tracks selected")
            store.put(item)
            return self._json({"id": item_id, "status": "rejected", "reason": "no audio selected"})

        for d in plan["dropped_subs"]:
            item["notes"].append("Dropped subtitle #%d (%s, %s): %s" %
                                 (d["index"], d["codec"], d["language"] or "und", d["reason"]))
        for a in plan["audio"]:
            if a["selected"] and a["action"] == "transcode":
                item["notes"].append("Audio #%d %s" % (a["index"], a["note"]))
            if a["selected"] and not a["language"]:
                item["notes"].append("Audio #%d has no language tag - shows as Unknown "
                                     "on the Apple TV" % a["index"])

        ext_subs = []
        for e in (req.get("external") or []):
            sp = self._safe(e.get("path"))
            if not sp or not os.path.isfile(sp):
                item["notes"].append("External subtitle skipped (not found): %s" % e.get("path"))
                continue
            try:
                fed, charenc, note = sniff_srt(sp, store.work)
            except OSError as exc:
                item["notes"].append("External subtitle unreadable: %s (%s)" % (e.get("path"), exc))
                continue
            lang = norm_lang(e.get("lang")) or "und"
            try:
                offset = float(e.get("offset") or 0)
            except (TypeError, ValueError):
                offset = 0.0
            ext_subs.append({"path": fed, "charenc": charenc, "offset": offset,
                             "lang": lang, "label": os.path.basename(sp)})
            item["notes"].append("External SRT %s: %s, lang=%s, offset=%+gs" %
                                 (os.path.basename(sp), note, lang, offset))

        item["output"] = os.path.join(store.cache, "%s.mp4" % item_id)
        store.put(item)
        self.server.worker.submit({"id": item_id, "source": src, "plan": plan,
                                   "output": item["output"], "ext_subs": ext_subs,
                                   "duration": plan["duration"]})
        return self._json({"id": item_id, "status": "queued"})

    def api_library(self):
        out = [{"id": i["id"], "title": i["title"],
                "duration": i.get("duration") or 0,
                "size": i.get("size") or 0,
                "url": "/media/%s.mp4" % i["id"]} for i in self.server.store.ready()]
        return self._json(out)

    # -- ranged media -----------------------------------------------------
    def api_media(self, path: str):
        name = unquote(path[len("/media/"):])
        item_id = name[:-4] if name.endswith(".mp4") else name
        item = self.server.store.get(item_id)
        if not item or not item.get("output") or not os.path.exists(item["output"]):
            return self._err(404, "unknown media id")
        self._serve_range(item["output"])

    def _serve_range(self, filepath: str):
        size = os.path.getsize(filepath)
        rng = self.headers.get("Range")
        start, end, partial = 0, size - 1, False

        if rng:
            m = RANGE_RE.match(rng.strip())
            if not m:
                return self._send(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, b"",
                                  "text/plain", {"Content-Range": "bytes */%d" % size})
            first, last = m.group(1), m.group(2)
            if first == "":
                if last == "":
                    return self._send(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, b"",
                                      "text/plain", {"Content-Range": "bytes */%d" % size})
                start = max(0, size - int(last))  # suffix range
            else:
                start = int(first)
                if last != "":
                    end = min(int(last), size - 1)
            if start >= size or start > end:
                return self._send(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, b"",
                                  "text/plain", {"Content-Range": "bytes */%d" % size})
            partial = True

        length = end - start + 1
        self.send_response(HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        if self.command == "HEAD":
            return

        try:
            with open(filepath, "rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    buf = fh.read(min(CHUNK, remaining))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    remaining -= len(buf)
        except (BrokenPipeError, ConnectionResetError):
            pass  # player seeked away; perfectly normal


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

UI_HTML = r"""<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>atvcast</title>
<style>
:root{--bg:#111418;--fg:#e6e8ea;--mut:#8b949e;--acc:#2f81f7;--ok:#3fb950;--bad:#f85149;--warn:#d29922;--card:#191d23;--line:#2a3038}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}
header{padding:14px 20px;border-bottom:1px solid var(--line);font-weight:600}
.wrap{display:grid;grid-template-columns:340px 1fr;gap:18px;padding:18px;align-items:start}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px;margin-bottom:16px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--mut);margin:0 0 10px}
ul{list-style:none;margin:0;padding:0}
li.row{padding:6px 8px;border-radius:5px;cursor:pointer}
li.row:hover{background:#222831}
li.row.sel{background:#1f3350}
.dir::before{content:"📁 "}.vid::before{content:"🎬 "}.srt::before{content:"💬 "}
.mut{color:var(--mut)}.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}
button{background:var(--acc);color:#fff;border:0;border-radius:6px;padding:8px 16px;font-weight:600;cursor:pointer}
button.sec{background:#2a3038}button:disabled{opacity:.45;cursor:not-allowed}
input,select{background:#0d1117;color:var(--fg);border:1px solid var(--line);border-radius:5px;padding:5px 7px}
table{width:100%;border-collapse:collapse}td,th{padding:5px 6px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:500;font-size:12px}
.bar{height:6px;background:#0d1117;border-radius:3px;overflow:hidden}.bar>i{display:block;height:100%;background:var(--acc)}
.pill{font-size:11px;padding:1px 7px;border-radius:10px;background:#2a3038}
code{font:12px ui-monospace,monospace;color:var(--mut);word-break:break-all}
.note{font-size:12px;color:var(--mut)}
</style>
<header>atvcast <span class="mut">— remux only, never re-encodes video</span></header>
<div class="wrap">
  <div>
    <div class="card">
      <h2>Library folder</h2>
      <div id="crumb" class="mut" style="margin-bottom:8px"></div>
      <ul id="tree"></ul>
    </div>
  </div>
  <div>
    <div class="card" id="detail"><span class="mut">Select a video on the left.</span></div>
    <div class="card"><h2>Jobs &amp; report</h2><div id="jobs"></div></div>
  </div>
</div>
<script>
const $ = s => document.querySelector(s);
let cur = {path:"", probe:null, ext:[]};

const fmtDur = s => { s=Math.round(s||0); const h=(s/3600|0),m=((s%3600)/60|0);
  return (h?h+"h ":"")+m+"m "+(s%60)+"s"; };

async function browse(p){
  const r = await fetch("/api/browse?path="+encodeURIComponent(p||"")); const d = await r.json();
  if(d.error) return alert(d.error);
  cur.dir = d.path;
  $("#crumb").textContent = "/"+(d.path||"");
  const t = $("#tree"); t.innerHTML = "";
  if(d.parent !== null && d.parent !== undefined){
    const li=document.createElement("li"); li.className="row dir"; li.textContent="..";
    li.onclick=()=>browse(d.parent); t.appendChild(li);
  }
  d.dirs.forEach(x=>{const li=document.createElement("li");li.className="row dir";li.textContent=x.name;
    li.onclick=()=>browse(x.path);t.appendChild(li);});
  d.files.forEach(x=>{const li=document.createElement("li");li.className="row "+(x.kind==="srt"?"srt":"vid");
    li.textContent=x.name;
    if(x.kind==="video"){ li.onclick=()=>{document.querySelectorAll("#tree .sel").forEach(e=>e.classList.remove("sel"));
      li.classList.add("sel"); probe(x.path);} } else { li.style.opacity=.6; li.style.cursor="default"; }
    t.appendChild(li);});
  cur.srts = d.files.filter(f=>f.kind==="srt");
}

async function probe(path){
  $("#detail").innerHTML = '<span class="mut">Probing…</span>';
  const r = await fetch("/api/probe?path="+encodeURIComponent(path)); const d = await r.json();
  if(d.error){ $("#detail").innerHTML = '<span class="bad">'+d.error+'</span>'; return; }
  cur = Object.assign(cur, {path:path, probe:d, ext:[]});
  render();
}

function render(){
  const d = cur.probe, el = $("#detail");
  if(d.reject){
    el.innerHTML = '<h2>'+esc(d.title)+'</h2><p class="bad">REJECTED — '+esc(d.reject)+'</p>'+
      '<p class="note">The whole file is rejected; nothing is written.</p>';
    return;
  }
  const v = d.video;
  let h = '<h2>'+esc(d.title)+'</h2>';
  h += '<p class="note">'+esc(v.codec)+' '+(v.profile?esc(v.profile)+" ":"")+v.width+'×'+v.height+
       ' · '+fmtDur(d.duration)+' · video is copied, never re-encoded'+
       (Object.keys(v.color).length? ' · color: '+esc(Object.values(v.color).join(", ")) : ' · no color tags')+'</p>';
  d.warnings.forEach(w=> h += '<p class="warn">⚠ '+esc(w)+'</p>');

  h += '<h2 style="margin-top:14px">Audio</h2><table><tr><th></th><th>#</th><th>Codec</th><th>Ch</th><th>Lang</th><th>Title</th><th>Action</th></tr>';
  d.audio.forEach(a=>{
    h += '<tr><td><input type="checkbox" class="ain" data-i="'+a.index+'" '+(a.selected?"checked":"")+'></td>'+
      '<td>'+a.index+'</td><td>'+esc(a.codec)+'</td><td>'+a.channels+'</td>'+
      '<td>'+(a.language?esc(a.language):'<span class="warn">untagged</span>')+'</td><td>'+esc(a.label)+'</td>'+
      '<td>'+(a.action==="copy"?'<span class="ok">copy</span>':'<span class="warn">'+esc(a.note)+'</span>')+'</td></tr>';
  });
  if(!d.audio.length) h += '<tr><td colspan="7" class="mut">none</td></tr>';
  h += '</table><p class="note">First checked track is marked default. Untagged tracks show as Unknown on the Apple TV.</p>';

  h += '<h2 style="margin-top:14px">Subtitles</h2><table><tr><th></th><th>#</th><th>Codec</th><th>Lang</th><th>Title</th><th></th></tr>';
  d.subs.forEach(s=>{
    h += '<tr><td><input type="checkbox" class="sin" data-i="'+s.index+'" '+(s.selected?"checked":"")+'></td>'+
      '<td>'+s.index+'</td><td>'+esc(s.codec)+'</td><td>'+(s.language?esc(s.language):'und')+'</td>'+
      '<td>'+esc(s.label)+'</td><td class="ok">→ mov_text</td></tr>';
  });
  d.dropped_subs.forEach(s=>{
    h += '<tr><td>—</td><td>'+s.index+'</td><td>'+esc(s.codec)+'</td><td>'+(s.language?esc(s.language):'und')+
      '</td><td>'+esc(s.label)+'</td><td class="bad">dropped: '+esc(s.reason)+'</td></tr>';
  });
  if(!d.subs.length && !d.dropped_subs.length) h += '<tr><td colspan="6" class="mut">none</td></tr>';
  h += '</table>';

  h += '<h2 style="margin-top:14px">External SRT</h2><div id="ext"></div>'+
       '<button class="sec" onclick="addExt()">+ Add .srt</button>';
  h += '<p style="margin-top:16px"><button id="go" onclick="convert()">Convert</button></p>';
  el.innerHTML = h;
  drawExt();
}

function addExt(){ cur.ext.push({path:(cur.srts&&cur.srts[0]?cur.srts[0].path:""),lang:"eng",offset:0}); drawExt(); }
function drawExt(){
  const box = $("#ext"); if(!box) return;
  if(!cur.ext.length){ box.innerHTML = '<span class="note">External SRTs are muxed alongside the embedded text tracks. Encoding is auto-detected (utf-8 → cp1252 → latin-1).</span>'; return; }
  let h = '<table><tr><th>File (under --media)</th><th>Lang</th><th>Offset s</th><th></th></tr>';
  cur.ext.forEach((e,i)=>{
    const opts = (cur.srts||[]).map(s=>'<option '+(s.path===e.path?'selected':'')+' value="'+esc(s.path)+'">'+esc(s.name)+'</option>').join("");
    h += '<tr><td><select onchange="cur.ext['+i+'].path=this.value">'+
         (opts||'<option value="">no .srt in this folder</option>')+'</select> '+
         '<input style="width:180px" placeholder="or path" value="'+esc(e.path||"")+'" oninput="cur.ext['+i+'].path=this.value"></td>'+
         '<td><input style="width:60px" value="'+esc(e.lang)+'" oninput="cur.ext['+i+'].lang=this.value"></td>'+
         '<td><input style="width:70px" type="number" step="0.1" value="'+e.offset+'" oninput="cur.ext['+i+'].offset=this.value"></td>'+
         '<td><button class="sec" onclick="cur.ext.splice('+i+',1);drawExt()">×</button></td></tr>';
  });
  box.innerHTML = h+'</table>';
}

async function convert(){
  $("#go").disabled = true;
  const audio = [...document.querySelectorAll(".ain:checked")].map(x=>+x.dataset.i);
  const subs  = [...document.querySelectorAll(".sin:checked")].map(x=>+x.dataset.i);
  const r = await fetch("/api/convert",{method:"POST",headers:{"Content-Type":"application/json"},
    body: JSON.stringify({source:cur.path,title:cur.probe.title,audio,subs,external:cur.ext})});
  const d = await r.json();
  $("#go").disabled = false;
  if(d.error) alert(d.error);
  poll();
}

async function poll(){
  try{
    const d = await (await fetch("/api/jobs")).json();
    const items = d.items||[];
    let h = '<table><tr><th>Title</th><th>Status</th><th style="width:180px">Progress</th><th>Notes</th></tr>';
    if(!items.length) h += '<tr><td colspan="4" class="mut">nothing converted yet</td></tr>';
    items.forEach(i=>{
      const cls = i.status==="ready"?"ok":(i.status==="error"||i.status==="rejected")?"bad":"warn";
      h += '<tr><td>'+esc(i.title)+'<div class="note">'+esc(i.source||"")+'</div></td>'+
        '<td><span class="pill '+cls+'">'+i.status+'</span></td>'+
        '<td>'+(i.status==="running"?'<div class="bar"><i style="width:'+(i.progress||0)+'%"></i></div>'+(i.progress||0)+'%'
              : i.status==="ready"? '<a class="mut" href="/media/'+i.id+'.mp4">/media/'+i.id+'.mp4</a>' : '<span class="mut">—</span>')+'</td>'+
        '<td class="note">'+(i.notes||[]).map(esc).join("<br>")+'</td></tr>';
    });
    $("#jobs").innerHTML = h+'</table>';
  }catch(e){}
}
function esc(s){ return String(s==null?"":s).replace(/[&<>"]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
browse(""); poll(); setInterval(poll, 1000);
</script>
"""


# --------------------------------------------------------------------------
# Self-check
# --------------------------------------------------------------------------

FIXTURE = {
    "format": {"duration": "1234.5"},
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "hevc", "profile": "Main 10",
         "width": 3840, "height": 2160, "color_transfer": "smpte2084",
         "color_primaries": "bt2020", "color_space": "bt2020nc", "color_range": "tv"},
        {"index": 1, "codec_type": "audio", "codec_name": "dts", "channels": 8,
         "tags": {"language": "eng", "title": "DTS-HD MA 7.1"}},
        {"index": 2, "codec_type": "audio", "codec_name": "ac3", "channels": 6,
         "tags": {"language": "jpn"}},
        {"index": 3, "codec_type": "audio", "codec_name": "flac", "channels": 2, "tags": {}},
        {"index": 4, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng"}},
        {"index": 5, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle",
         "tags": {"language": "eng"}},
        {"index": 6, "codec_type": "video", "codec_name": "mjpeg",
         "disposition": {"attached_pic": 1}},
    ],
}

HI10P_FIXTURE = {
    "format": {"duration": "600"},
    "streams": [{"index": 0, "codec_type": "video", "codec_name": "h264", "profile": "High 10",
                 "width": 1920, "height": 1080},
                {"index": 1, "codec_type": "audio", "codec_name": "flac", "channels": 2}],
}


def selftest(sample: str | None = None) -> int:
    fails = []

    def check(cond, msg):
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            fails.append(msg)

    print("decision logic (synthetic fixture):")
    p = analyze(FIXTURE)
    check(p["reject"] is None, "hevc Main 10 is accepted")
    check(p["video"]["index"] == 0, "attached_pic mjpeg is not picked as the video stream")
    check(p["video"]["color"]["color_transfer"] == "smpte2084", "HDR transfer detected")
    check([a["action"] for a in p["audio"]] == ["transcode", "copy", "transcode"],
          "dts->eac3, ac3 copy, flac->eac3")
    check(p["audio"][0]["target_channels"] == 6, "7.1 dts clamped to 6ch (eac3 cap)")
    check(p["audio"][2]["language"] is None, "untagged flac reports no language")
    check(p["audio"][2]["label"] == "Track 3", "untagged flac falls back to 'Track 3'")
    check([s["index"] for s in p["subs"]] == [4], "subrip kept")
    check([s["index"] for s in p["dropped_subs"]] == [5], "PGS dropped")
    check(any("2 audio tracks need transcoding" in w for w in p["warnings"]),
          "multi-transcode warning raised")

    r = analyze(HI10P_FIXTURE)
    check(r["reject"] == "Hi10P not supported by Apple TV", "Hi10P rejected")

    cmd = build_command("/in.mkv", p, "/out.mp4",
                        [{"path": "/x.srt", "charenc": "CP1252", "offset": 2.5, "lang": "spa",
                          "label": "x.srt"}])
    s = " ".join(cmd)
    print("cmd: %s" % s)
    check("-c:v copy" in s, "video copied")
    check("-c:a:0 eac3 -b:a:0 768k -ac:a:0 6" in s, "eac3 768k 6ch on track 0")
    check("-c:a:1 copy" in s, "ac3 copied on track 1")
    check("-disposition:a:0 default" in s and "-disposition:a:1 0" in s, "default disposition")
    check("-metadata:s:a:2 language=und" in s, "untagged audio muxed as und")
    check("-itsoffset 2.5 -sub_charenc CP1252 -i /x.srt" in s, "srt offset+charenc precede input")
    check("-map 1:0" in s and "-c:s mov_text" in s, "external srt mapped as mov_text")
    check("-metadata:s:s:1 language=spa" in s, "external srt language tagged")
    check("-movflags +faststart" in s, "faststart")
    check("-ac:a:0 8" not in s, "never asks eac3 for 7.1")

    c2 = " ".join(build_command("/in.mkv", p, "/out.mp4", [], color_override=p["video"]["color"]))
    check("-color_trc smpte2084 -color_primaries bt2020 -colorspace bt2020nc" in c2,
          "color re-tag flags")

    if sample and os.path.isfile(sample) and shutil.which("ffprobe"):
        print("real sample: %s" % sample)
        try:
            real = analyze(ffprobe(sample))
            print(json.dumps({k: real[k] for k in ("reject", "video", "audio", "subs",
                                                   "dropped_subs", "warnings")}, indent=2)[:2000])
            check(real["video"] is not None or real["reject"] is not None,
                  "sample produced a decision")
        except Exception as exc:
            check(False, "ffprobe on sample: %s" % exc)

    print("\n%d check(s) failed" % len(fails) if fails else "\nall checks passed")
    return 1 if fails else 0


# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="Remux MKVs for Apple TV and serve them over HTTP.")
    ap.add_argument("--media", default=os.path.expanduser("~/Videos"), help="media root")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--cache", default=os.path.expanduser("~/.cache/atvcast"))
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--selftest", action="store_true",
                    help="run the ffprobe decision-logic sanity check and exit")
    ap.add_argument("--sample", help="optional real media file for --selftest")
    args = ap.parse_args()

    if args.selftest:
        return selftest(args.sample)

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit("error: %s not found on PATH" % tool)

    media = os.path.realpath(os.path.expanduser(args.media))
    if not os.path.isdir(media):
        sys.exit("error: --media %s is not a directory" % media)
    cache = os.path.realpath(os.path.expanduser(args.cache))
    os.makedirs(cache, exist_ok=True)

    store = Store(cache)
    worker = Converter(store)
    worker.start()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    httpd.media_root = media
    httpd.store = store
    httpd.worker = worker

    print("atvcast: media=%s cache=%s" % (media, cache))
    print("         http://%s:%d/   (library: /library, media: /media/{id}.mp4)" %
          (args.host, args.port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
