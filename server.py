#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RAX YT Downloader (web)
=======================
The small download server behind the website in the "website" folder.

Windows:  double-click START.bat  (it installs everything and starts this file)
Manual:   python server.py        then open http://localhost:5000
"""

# ----------------------------------------------------------------- settings
PORT = 5000                    # the site opens at http://localhost:5000
ALLOW_PHONES_ON_WIFI = True    # let phones and laptops on the same Wi-Fi use the site
MAX_PARALLEL_DOWNLOADS = 2     # how many videos download at the same time
KEEP_FILES_MINUTES = 30        # finished files are removed from temp_downloads after this
MAX_PLAYLIST_VIDEOS = 500      # biggest playlist the site will list
OPEN_BROWSER_ON_START = True   # open the site in your browser when the server starts
# ---------------------------------------------------------------------------

import io
import ipaddress
import json
import logging
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor

APP_NAME = 'RAX YT Downloader'
APP_VERSION = '1.0'
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, 'website')
TEMP_DIR = os.path.join(BASE_DIR, 'temp_downloads')
COOKIES_FILE = os.path.join(BASE_DIR, 'cookies.txt')
DEBUG = os.environ.get('RAX_DEBUG') == '1'
YOUTUBE_ONLY = True
USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/131.0 Safari/537.36')

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass


def say(text=''):
    print(text, flush=True)


try:
    import flask.cli
    from flask import Flask, jsonify, request, send_file, send_from_directory
    from werkzeug.security import safe_join
except ImportError:
    say('Flask is not installed. Run START.bat, or: python -m pip install -r requirements.txt')
    sys.exit(1)

try:
    import yt_dlp
    import yt_dlp.utils
except ImportError:
    say('yt-dlp is not installed. Run START.bat, or: python -m pip install -U "yt-dlp[default]"')
    sys.exit(1)

try:
    from yt_dlp.version import __version__ as YTDLP_VERSION
except Exception:
    YTDLP_VERSION = 'unknown'

DownloadCancelled = getattr(yt_dlp.utils, 'DownloadCancelled', None)
if DownloadCancelled is None:  # very old yt-dlp
    class DownloadCancelled(Exception):
        pass

# Windows sometimes maps .js/.css to the wrong type in the registry; browsers then refuse them.
mimetypes.add_type('text/javascript', '.js')
mimetypes.add_type('text/css', '.css')
mimetypes.add_type('image/svg+xml', '.svg')


# ===================================================================== tools

def find_ffmpeg():
    """FFmpeg joins HD video with its sound and makes MP3s."""
    names = ('ffmpeg.exe', 'ffmpeg') if os.name == 'nt' else ('ffmpeg',)
    for sub in ('ffmpeg', os.path.join('ffmpeg', 'bin')):
        for name in names:
            path = os.path.join(BASE_DIR, sub, name)
            if os.path.isfile(path):
                return path, 'ffmpeg folder'
    path = shutil.which('ffmpeg')
    if path:
        return path, 'installed on this computer'
    try:
        import imageio_ffmpeg
        path = imageio_ffmpeg.get_ffmpeg_exe()
        if path and os.path.isfile(path):
            return path, 'imageio-ffmpeg package'
    except Exception:
        pass
    return None, None


def _node_is_new_enough(path):
    try:
        out = subprocess.run([path, '--version'], capture_output=True, text=True, timeout=8).stdout
        return int(out.strip().lstrip('v').split('.')[0]) >= 20
    except Exception:
        return False


def find_js_runtimes():
    """YouTube now needs a JavaScript runtime (Deno or Node) to unlock video links."""
    runtimes = {}
    deno_path = None
    try:
        import deno as deno_pkg  # the official "deno" package from PyPI
        candidate = deno_pkg.find_deno_bin()
        if candidate and os.path.isfile(str(candidate)):
            deno_path = str(candidate)
    except Exception:
        pass
    if not deno_path:
        for name in (('deno.exe', 'deno') if os.name == 'nt' else ('deno',)):
            candidate = os.path.join(BASE_DIR, name)
            if os.path.isfile(candidate):
                deno_path = candidate
                break
    if not deno_path:
        deno_path = shutil.which('deno')
    if deno_path:
        runtimes['deno'] = {'path': deno_path}
    node = shutil.which('node')
    if node and _node_is_new_enough(node):
        runtimes['node'] = {'path': node}
    return runtimes


def lan_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(('10.255.255.255', 1))  # no packet is sent; this only picks the Wi-Fi/LAN adapter
        ip = sock.getsockname()[0]
        return None if ip.startswith('127.') else ip
    except Exception:
        return None
    finally:
        sock.close()


FFMPEG, FFMPEG_SOURCE = find_ffmpeg()
JS_RUNTIMES = find_js_runtimes()
JS_RUNTIME_NAME = next(iter(JS_RUNTIMES), None)
ACTIVE_PORT = PORT


# ================================================================ links & text

ID_RE = re.compile(r'^[A-Za-z0-9_-]{11}$')
FID_RE = re.compile(r'^[A-Za-z0-9_-]{1,24}$')
LANG_RE = re.compile(r'^[A-Za-z0-9_-]{1,24}$')
JOB_RE = re.compile(r'^[a-f0-9]{12}$')
URL_RE = re.compile(r'https?://[^\s<>"\']+', re.I)
BARE_RE = re.compile(r'^(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)/\S*$', re.I)
YT_HOSTS = ('youtube.com', 'youtu.be', 'youtube-nocookie.com')
ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')


def clean_url(raw):
    """Turn whatever was pasted into a tidy YouTube link, or None."""
    raw = str(raw or '').strip()
    if not raw:
        return None
    if ID_RE.match(raw):
        return 'https://www.youtube.com/watch?v=' + raw
    match = URL_RE.search(raw)
    if match:
        url = match.group(0)
    elif BARE_RE.match(raw):
        url = 'https://' + raw
    else:
        return None
    url = url.rstrip('.,;)]}>')
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or '').lower()
    if YOUTUBE_ONLY and not any(host == h or host.endswith('.' + h) for h in YT_HOSTS):
        return None
    path = parts.path.rstrip('/')
    # A channel's home page lists tabs, not videos. Send it to its Videos tab instead.
    if re.fullmatch(r'/(@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)', path):
        url = urllib.parse.urlunsplit(('https', parts.netloc, path + '/videos', parts.query, ''))
    return url


def video_id_from(url):
    if not url:
        return None
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or '').lower()
    if host.endswith('youtu.be'):
        first = parts.path.strip('/').split('/')[0]
        return first if ID_RE.match(first) else None
    query = urllib.parse.parse_qs(parts.query)
    vid = (query.get('v') or [''])[0]
    if ID_RE.match(vid):
        return vid
    match = re.match(r'^/(?:shorts|live|embed|v|e)/([A-Za-z0-9_-]{11})', parts.path)
    return match.group(1) if match else None


def safe_filename(text, limit=120):
    text = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]+', ' ', str(text or ''))
    text = re.sub(r'\s+', ' ', text).strip().strip('.')
    if len(text) > limit:
        cut = text[:limit]
        space = cut.rfind(' ')
        text = (cut[:space] if space > limit * 0.6 else cut).rstrip(' .,;-')
    return text or 'video'


def strip_ansi(text):
    return ANSI_RE.sub('', str(text or ''))


ERROR_HINTS = [
    ('private video', 'This video is private, so only its owner can watch it.'),
    ('sign in to confirm your age', 'This video is age-restricted. Put a cookies.txt file from your '
                                    'browser in the app folder (see HOW TO USE.txt), then try again.'),
    ('not a bot', 'YouTube wants to check that you are not a bot. Wait a few minutes and try again, '
                  'or add a cookies.txt file (see HOW TO USE.txt).'),
    ('members-only', 'This video is only for members of the channel.'),
    ("channel's members", 'This video is only for members of the channel.'),
    ('premieres in', "This video hasn't premiered yet. Try again after it starts."),
    ('live event will begin', "This live stream hasn't started yet."),
    ('not available in your country', 'This video is blocked in your country.'),
    ('made this video available in your country', 'This video is blocked in your country.'),
    ('copyright', 'This video was taken down because of a copyright claim.'),
    ('account associated with this video has been terminated', 'This video is gone because its channel was closed.'),
    ('video unavailable', 'This video is unavailable. It may have been removed, made private, or blocked in your country.'),
    ('playlist does not exist', "This playlist doesn't exist or is private."),
    ('playlist type is unviewable', "YouTube doesn't let anyone open this kind of playlist."),
    ('requested format is not available', "That quality isn't available for this video. Pick another one."),
    ('ffmpeg', 'FFmpeg is needed for this. Close the downloader and run START.bat again so it can install FFmpeg.'),
    ('javascript runtime', 'YouTube needs Deno to unlock this video. Close the downloader and run START.bat again to install it.'),
    ('challenge', 'YouTube changed something. Close the downloader and run START.bat again; it updates the download engine.'),
    ('signature', 'YouTube changed something. Close the downloader and run START.bat again; it updates the download engine.'),
    ('http error 403', 'YouTube refused the download. Close the downloader and run START.bat again; it updates the '
                       'download engine. If it keeps happening, wait a while and try again.'),
    ('http error 429', 'YouTube is getting too many requests from this network. Wait a few minutes and try again.'),
    ('incomplete youtube id', 'That link looks cut off. Copy the whole link and paste it again.'),
    ('unsupported url', "That link doesn't point to a YouTube video or playlist."),
    ('getaddrinfo', "Can't reach YouTube. Check that this computer is online."),
    ('unable to download webpage', "Can't reach YouTube. Check that this computer is online."),
    ('timed out', 'YouTube took too long to answer. Try again.'),
    ('no space left', 'The disk is full. Free some space and try again.'),
]


def friendly_error(exc):
    text = strip_ansi(exc)
    low = text.lower()
    for needle, hint in ERROR_HINTS:
        if needle in low:
            return hint
    text = re.sub(r'^(?:ERROR:\s*)?(?:\[[^\]]+\]\s*)?(?:[A-Za-z0-9_-]{6,}:\s+)?', '', text.strip())
    return text[:300] or 'The download stopped for an unknown reason. Try again.'


class QuietLogger:
    """Keeps yt-dlp quiet in the black window; errors still show up there."""

    def debug(self, msg):
        if DEBUG:
            say(strip_ansi(msg))

    info = debug

    def warning(self, msg):
        if DEBUG:
            say('[warning] ' + strip_ansi(msg))

    def error(self, msg):
        say('[problem] ' + strip_ansi(msg)[:400])


def base_options():
    opts = {
        'quiet': True,
        'no_warnings': True,
        'noprogress': True,
        'logger': QuietLogger(),
        'socket_timeout': 20,
        'retries': 5,
        'fragment_retries': 10,
        'extractor_retries': 2,
        'concurrent_fragment_downloads': 4,
    }
    if FFMPEG:
        opts['ffmpeg_location'] = FFMPEG
    if JS_RUNTIMES:
        opts['js_runtimes'] = JS_RUNTIMES
    if os.path.isfile(COOKIES_FILE):
        opts['cookiefile'] = COOKIES_FILE
    return opts


# ================================================================ video info

LADDER = (4320, 2160, 1440, 1080, 720, 480, 360, 240, 144)
QUALITY_TAGS = {4320: '8K', 2160: '4K Ultra HD', 1440: '2K', 1080: 'Full HD', 720: 'HD',
                480: 'Standard', 360: 'Small file', 240: 'Very small', 144: 'Tiny'}
AUDIO_CHOICES = {'mp3-320': ('mp3', '320'), 'mp3-192': ('mp3', '192'),
                 'mp3-128': ('mp3', '128'), 'm4a': ('m4a', None)}


def _has_video(f):
    return (f.get('vcodec') or 'none') != 'none' and bool(f.get('height') or f.get('width'))


def _has_audio(f):
    return (f.get('acodec') or 'none') != 'none'


def _usable(f):
    if f.get('has_drm'):
        return False
    if (f.get('ext') or '') == 'mhtml' or 'mhtml' in (f.get('protocol') or ''):
        return False  # storyboard images, not video
    return True


def _size(f, duration):
    if not f:
        return None
    size = f.get('filesize') or f.get('filesize_approx')
    if not size and f.get('tbr') and duration:
        size = f['tbr'] * 125 * duration  # kbit/s x seconds -> bytes
    return int(size) if size else None


def _quality(f):
    match = re.search(r'(\d{3,4})p', f.get('format_note') or '')
    if match:
        q = int(match.group(1))
    else:
        w, h = f.get('width'), f.get('height')
        q = min(w, h) if (w and h) else (h or 0)
    if not q:
        return None
    return min(LADDER, key=lambda rung: abs(rung - q))


def _video_rank(f):
    vcodec = (f.get('vcodec') or '').lower()
    if vcodec.startswith(('avc', 'h264')):
        codec = 3  # plays everywhere
    elif vcodec.startswith(('vp9', 'vp09')):
        codec = 2
    elif vcodec.startswith('av01'):
        codec = 1
    else:
        codec = 0
    direct = 0 if 'm3u8' in (f.get('protocol') or '') else 1
    sdr = 1 if (f.get('dynamic_range') or 'SDR') == 'SDR' else 0
    return (direct, sdr, codec, f.get('fps') or 0, 1 if _has_audio(f) else 0, f.get('tbr') or 0)


def _audio_rank(f):
    return (1 if f.get('ext') == 'm4a' else 0,
            f.get('language_preference') or 0,
            0 if 'drc' in str(f.get('format_id')) else 1,
            f.get('abr') or f.get('tbr') or 0)


def _live_message(info):
    status = info.get('live_status')
    if info.get('is_live') or status == 'is_live':
        return 'This video is live right now. You can download it after the stream ends.'
    if status == 'is_upcoming':
        return "This video hasn't started yet. Come back after it premieres."
    return None


def build_video(info, source_url):
    duration = info.get('duration') or 0
    formats = [f for f in (info.get('formats') or []) if _usable(f)]
    audios = [f for f in formats if _has_audio(f) and not _has_video(f)]
    best_audio = max(audios, key=_audio_rank) if audios else None
    audio_size = _size(best_audio, duration)

    best_per_quality = {}
    for f in formats:
        if not _has_video(f):
            continue
        if not FFMPEG and not _has_audio(f):
            continue  # without FFmpeg only files that already contain sound work
        q = _quality(f)
        if q and (q not in best_per_quality or _video_rank(f) > _video_rank(best_per_quality[q])):
            best_per_quality[q] = f

    video_options = []
    for q in sorted(best_per_quality, reverse=True):
        f = best_per_quality[q]
        progressive = _has_audio(f)
        vsize = _size(f, duration)
        total = (vsize or 0) + (0 if progressive else (audio_size or 0))
        video_options.append({
            'q': q,
            'tag': QUALITY_TAGS.get(q, ''),
            'fps': int(f.get('fps') or 0) or None,
            'hdr': (f.get('dynamic_range') or 'SDR') != 'SDR',
            'fid': str(f.get('format_id')),
            'progressive': progressive,
            'size': total or None,
            'vsize': vsize,
            'asize': None if progressive else audio_size,
        })

    audio_options = []
    if FFMPEG and (audios or best_per_quality):
        for bitrate in (320, 192, 128):
            audio_options.append({'id': 'mp3-%d' % bitrate, 'name': 'MP3', 'detail': '%d kbps' % bitrate,
                                  'size': int(duration * bitrate * 125) if duration else None})
        m4a = [f for f in audios if f.get('ext') == 'm4a']
        audio_options.append({'id': 'm4a', 'name': 'M4A', 'detail': 'Original quality, no conversion',
                              'size': _size(max(m4a, key=_audio_rank), duration) if m4a else audio_size})
    elif best_audio:
        audio_options.append({'id': 'm4a', 'name': (best_audio.get('ext') or 'm4a').upper(),
                              'detail': 'Original quality', 'size': audio_size})

    subtitles = []
    for code, tracks in (info.get('subtitles') or {}).items():
        if code in ('live_chat', 'rechat') or not tracks:
            continue
        subtitles.append({'code': code, 'name': tracks[0].get('name') or code, 'auto': False})
    autos = info.get('automatic_captions') or {}
    for code, tracks in autos.items():
        if code.endswith('-orig') and tracks:
            base = re.sub(r'\s*\((?:original|auto-generated)\)\s*', ' ', tracks[0].get('name') or code, flags=re.I)
            subtitles.append({'code': code, 'name': base.strip() + ' (auto-generated)', 'auto': True})
    if 'en' in autos and not any(s['code'].split('-')[0] == 'en' for s in subtitles):
        subtitles.append({'code': 'en', 'name': 'English (auto-translated)', 'auto': True})
    subtitles.sort(key=lambda s: (s['auto'], s['name'].lower()))

    vid = info.get('id') or ''
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(source_url).query)
    playlist_id = (query.get('list') or [''])[0]
    return {
        'type': 'video',
        'id': vid,
        'url': 'https://www.youtube.com/watch?v=' + vid if ID_RE.match(vid) else (info.get('webpage_url') or source_url),
        'source_url': source_url,
        'title': info.get('title') or 'Untitled video',
        'channel': info.get('channel') or info.get('uploader'),
        'duration': duration or None,
        'views': info.get('view_count'),
        'upload_date': info.get('upload_date'),
        'thumbnail': info.get('thumbnail') or ('https://i.ytimg.com/vi/%s/hqdefault.jpg' % vid if vid else None),
        'live': _live_message(info),
        'playlist_hint': bool(playlist_id) and playlist_id not in ('WL', 'LL'),
        'video_options': video_options,
        'audio_options': audio_options,
        'subtitles': subtitles,
    }


def build_playlist(info, source_url):
    entries = []
    for entry in info.get('entries') or []:
        if not entry:
            continue
        vid = entry.get('id') or ''
        if not ID_RE.match(vid):
            continue  # skip channel tabs and other non-video items
        title = entry.get('title') or vid
        entries.append({
            'id': vid,
            'url': 'https://www.youtube.com/watch?v=' + vid,
            'title': title,
            'duration': entry.get('duration'),
            'thumb': 'https://i.ytimg.com/vi/%s/mqdefault.jpg' % vid,
            'unavailable': title in ('[Private video]', '[Deleted video]', '[Unavailable video]'),
        })
        if len(entries) >= MAX_PLAYLIST_VIDEOS:
            break
    return {
        'type': 'playlist',
        'id': info.get('id'),
        'title': info.get('title') or 'Playlist',
        'channel': info.get('channel') or info.get('uploader'),
        'count': len(entries),
        'total': info.get('playlist_count'),
        'entries': entries,
        'source_url': source_url,
    }


# ===================================================================== jobs

class Job:
    ACTIVE = ('queued', 'downloading', 'processing')

    def __init__(self, params):
        self.id = uuid.uuid4().hex[:12]
        self.params = params
        self.state = 'queued'
        self.stage = 'Waiting in line'
        self.error = None
        self.file = None
        self.name = None
        self.size = None
        self.cancel = False
        self.created = time.time()
        self.ended = None
        self.served = None
        self.info = None
        self.last_pp = ''
        self.dir = os.path.join(TEMP_DIR, self.id)
        self.reset_progress()

    def reset_progress(self):
        p = self.params
        if p['kind'] == 'video' and FFMPEG and not p.get('progressive'):
            vsize, asize = p.get('vsize'), p.get('asize')
            weight = vsize / float(vsize + asize) if (vsize and asize) else 0.9
            self.weights = [weight, 1.0 - weight]   # video file, then its sound
            self.part_sizes = [vsize, asize]
        else:
            self.weights = [1.0]
            self.part_sizes = [None]
        self.parts = []
        self.part_done = {}
        self.percent = 0.0
        self.done_bytes = 0
        self.total_bytes = None
        self.speed = None
        self.eta = None

    def public(self):
        return {
            'id': self.id, 'state': self.state, 'stage': self.stage,
            'percent': round(self.percent, 1), 'speed': self.speed, 'eta': self.eta,
            'done_bytes': self.done_bytes, 'total_bytes': self.total_bytes,
            'name': self.name, 'size': self.size, 'error': self.error,
        }

    # yt-dlp calls this many times a second while a file downloads
    def on_progress(self, d):
        if self.cancel:
            raise DownloadCancelled('Cancelled')
        status = d.get('status')
        name = os.path.basename(d.get('filename') or d.get('tmpfilename') or 'file')
        if name not in self.parts:
            self.parts.append(name)
        idx = min(self.parts.index(name), len(self.weights) - 1)
        if status == 'downloading':
            done = d.get('downloaded_bytes') or 0
            total = d.get('total_bytes') or d.get('total_bytes_estimate')
            frac = max(0.0, min(1.0, done / float(total))) if total else 0.0
            pct = (sum(self.weights[:idx]) + self.weights[idx] * frac) * 100
            self.percent = max(self.percent, min(99.0, pct))
            before = sum(self.part_done.get(n, 0) for n in self.parts[:idx])
            self.done_bytes = int(before + done)
            if len(self.weights) == 2 and all(self.part_sizes):
                self.total_bytes = int(sum(self.part_sizes))
            else:
                self.total_bytes = int(before + total) if total else None
            self.speed = d.get('speed')
            eta = d.get('eta')
            if eta is not None and self.speed and len(self.weights) == 2 and idx == 0 and self.part_sizes[1]:
                eta += self.part_sizes[1] / self.speed  # the sound still has to come after the video
            self.eta = eta
            kind = self.params['kind']
            if len(self.weights) == 2:
                self.stage = 'Downloading video' if idx == 0 else 'Downloading sound'
            elif kind == 'audio':
                self.stage = 'Downloading audio'
            elif kind == 'subs':
                self.stage = 'Downloading subtitles'
            else:
                self.stage = 'Downloading'
            self.state = 'downloading'
        elif status == 'finished':
            self.part_done[name] = d.get('total_bytes') or d.get('downloaded_bytes') or 0
            self.percent = max(self.percent, min(99.0, sum(self.weights[:idx + 1]) * 100))

    # ...and this when FFmpeg joins, converts or tags the file
    def on_postprocess(self, d):
        pp = d.get('postprocessor') or ''
        if d.get('status') != 'started' or 'MoveFiles' in pp:
            return
        self.last_pp = pp
        self.state = 'processing'
        self.speed = self.eta = None
        audio_fmt = AUDIO_CHOICES.get(self.params.get('audio') or '', ('mp3', None))[0]
        if 'Merger' in pp:
            self.stage = 'Joining video and sound'
        elif 'ExtractAudio' in pp:
            self.stage = 'Converting to MP3' if audio_fmt == 'mp3' else 'Saving as M4A'
        elif 'Metadata' in pp:
            self.stage = 'Adding title and artist'
        elif 'Thumbnail' in pp:
            self.stage = 'Adding cover art'
        elif 'Subtitles' in pp:
            self.stage = 'Converting subtitles'
        else:
            self.stage = 'Finishing up'


JOBS = {}
JOBS_LOCK = threading.Lock()
EXECUTOR = ThreadPoolExecutor(max_workers=max(1, int(MAX_PARALLEL_DOWNLOADS)))


def job_options(job, safe=False):
    p = job.params
    opts = base_options()
    opts.update({
        'paths': {'home': job.dir, 'temp': job.dir},
        'outtmpl': 'media.%(ext)s',   # short name on disk; the real title is used when you save it
        'noplaylist': True,
        'overwrites': True,
        'progress_hooks': [job.on_progress],
        'postprocessor_hooks': [job.on_postprocess],
    })
    kind = p['kind']
    if kind == 'video':
        q = p.get('q')
        fid = p.get('fid')
        sort = (['res:%d' % q] if isinstance(q, int) else ['res']) + ['fps', '+codec:avc:m4a']
        if FFMPEG:
            if fid and p.get('progressive'):
                fmt = '%s/bv*+ba/b' % fid
            elif fid:
                fmt = '%s+ba[ext=m4a]/%s+ba/bv*+ba/b' % (fid, fid)
            else:
                fmt = 'bv*+ba/b'
            opts['merge_output_format'] = 'mp4'
        else:
            fmt = '%s/b' % fid if (fid and p.get('progressive')) else 'b'
        opts['format'] = fmt
        opts['format_sort'] = sort
    elif kind == 'audio':
        codec, bitrate = AUDIO_CHOICES[p['audio']]
        if FFMPEG:
            opts['format'] = 'ba[ext=m4a]/ba/b' if codec == 'm4a' else 'ba/b'
            extract = {'key': 'FFmpegExtractAudio', 'preferredcodec': codec}
            if bitrate:
                extract['preferredquality'] = bitrate
            pps = [extract]
            if not safe:
                opts['writethumbnail'] = True
                pps += [{'key': 'FFmpegMetadata', 'add_metadata': True, 'add_chapters': False},
                        {'key': 'EmbedThumbnail', 'already_have_thumbnail': False}]
            opts['postprocessors'] = pps
        else:
            opts['format'] = 'ba[ext=m4a]/ba'
    elif kind == 'subs':
        opts.update({
            'skip_download': True,
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': [p['lang']],
            'subtitlesformat': 'srt/vtt/best',
        })
        if FFMPEG:
            opts['postprocessors'] = [{'key': 'FFmpegSubtitlesConvertor', 'format': 'srt', 'when': 'before_dl'}]
    return opts


def find_output(job):
    info = job.info or {}
    for item in info.get('requested_downloads') or []:
        path = item.get('filepath')
        if path and os.path.isfile(path):
            return path
    if not os.path.isdir(job.dir):
        return None
    wanted = {'video': ('.mp4', '.mkv', '.webm'),
              'audio': ('.mp3', '.m4a', '.opus', '.ogg', '.webm'),
              'subs': ('.srt', '.vtt', '.ttml', '.srv3', '.json3')}[job.params['kind']]
    files = [os.path.join(job.dir, n) for n in os.listdir(job.dir)]
    files = [f for f in files if os.path.isfile(f) and not f.endswith(('.part', '.ytdl', '.temp', '.tmp'))]
    preferred = [f for f in files if f.lower().endswith(wanted)]
    pool = preferred or files
    return max(pool, key=os.path.getsize) if pool else None


def nice_name(job, path):
    info = job.info or {}
    first = (info.get('requested_downloads') or [{}])[0]
    title = safe_filename(info.get('title') or job.params.get('title') or 'video')
    ext = os.path.splitext(path)[1].lower()
    kind = job.params['kind']
    if kind == 'video':
        w = first.get('width') or info.get('width')
        h = first.get('height') or info.get('height')
        q = min(w, h) if (w and h) else h
        if q:
            q = min(LADDER, key=lambda rung: abs(rung - q))
        elif isinstance(job.params.get('q'), int):
            q = job.params['q']
        return '%s (%sp)%s' % (title, q, ext) if q else title + ext
    if kind == 'subs':
        return '%s.%s%s' % (title, job.params.get('lang') or 'subtitles', ext)
    return title + ext


EXTRA_PPS = ('EmbedThumbnail', 'FFmpegMetadata', 'FFmpegThumbnailsConvertor')


def _download(job, safe):
    with yt_dlp.YoutubeDL(job_options(job, safe)) as ydl:
        return ydl.extract_info(job.params['url'], download=True)


def run_job(job):
    if job.cancel:
        return
    try:
        os.makedirs(job.dir, exist_ok=True)
        job.state, job.stage = 'downloading', 'Connecting to YouTube'
        try:
            job.info = _download(job, safe=False)
        except Exception:
            if job.cancel or not (job.state == 'processing' and job.last_pp in EXTRA_PPS):
                raise
            # Adding the cover art or tags failed. Try once more with a plain file.
            shutil.rmtree(job.dir, ignore_errors=True)
            os.makedirs(job.dir, exist_ok=True)
            job.reset_progress()
            job.state, job.stage, job.last_pp = 'downloading', 'Trying again without cover art', ''
            job.info = _download(job, safe=True)
        if job.cancel:
            raise DownloadCancelled('Cancelled')
        path = find_output(job)
        if not path:
            raise RuntimeError('The download finished, but the file is missing. Try again.')
        job.file = path
        job.size = os.path.getsize(path)
        job.name = nice_name(job, path)
        job.percent, job.state, job.stage = 100.0, 'done', 'Done'
    except BaseException as exc:  # noqa: B902 - report every failure to the page
        if job.cancel:
            job.state, job.stage = 'cancelled', 'Cancelled'
        else:
            job.state, job.stage = 'error', 'Failed'
            job.error = friendly_error(exc)
            if DEBUG:
                traceback.print_exc()
        shutil.rmtree(job.dir, ignore_errors=True)
        if not isinstance(exc, Exception):
            raise
    finally:
        job.ended = time.time()
        job.speed = job.eta = None


def janitor():
    """Deletes finished files so temp_downloads doesn't fill your disk."""
    while True:
        time.sleep(30)
        now = time.time()
        for job_id, job in list(JOBS.items()):
            if not job.ended:
                continue
            served_long_ago = job.served and now - job.served > 10 * 60
            if served_long_ago or now - job.ended > KEEP_FILES_MINUTES * 60:
                shutil.rmtree(job.dir, ignore_errors=True)
                with JOBS_LOCK:
                    JOBS.pop(job_id, None)


# ====================================================================== web

app = Flask(__name__, static_folder=None)
app.url_map.merge_slashes = False   # keeps links like /https://www.youtube.com/... intact
app.json.sort_keys = False


def json_error(message, status=400):
    return jsonify({'error': message}), status


@app.before_request
def only_your_network():
    try:
        ip = ipaddress.ip_address(request.remote_addr or '')
    except ValueError:
        return json_error('Forbidden.', 403)
    if not (ip.is_loopback or ip.is_private or ip.is_link_local):
        return json_error('This downloader only works on your own network.', 403)
    return None


@app.get('/api/status')
def api_status():
    ip = lan_ip() if ALLOW_PHONES_ON_WIFI else None
    return jsonify({
        'app': APP_NAME,
        'version': APP_VERSION,
        'ytdlp': YTDLP_VERSION,
        'ffmpeg': bool(FFMPEG),
        'js_runtime': JS_RUNTIME_NAME,
        'lan_url': 'http://%s:%d' % (ip, ACTIVE_PORT) if ip else None,
        'phones_allowed': ALLOW_PHONES_ON_WIFI,
        'cookies': os.path.isfile(COOKIES_FILE),
    })


@app.get('/api/info')
def api_info():
    url = clean_url(request.args.get('url'))
    if not url:
        return json_error("That isn't a YouTube link. Copy the link from the address bar or the "
                          "Share button, then paste it here.")
    opts = base_options()
    opts.update({
        'skip_download': True,
        'noplaylist': request.args.get('playlist') != '1',
        'extract_flat': 'in_playlist',
        'playlistend': MAX_PLAYLIST_VIDEOS,
        'ignore_no_formats_error': True,
    })
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        return json_error(friendly_error(exc), 422)
    if not info:
        return json_error('Nothing to download was found at that link.', 404)
    if info.get('_type') == 'playlist' or info.get('entries') is not None:
        data = build_playlist(info, url)
        if not data['entries']:
            return json_error('This playlist has no videos that can be downloaded.', 404)
        return jsonify(data)
    return jsonify(build_video(info, url))


@app.get('/api/quick')
def api_quick():
    """Fast title lookup used when you paste many links at once."""
    vid = request.args.get('id', '')
    if not ID_RE.match(vid):
        return json_error('Bad video id.')
    try:
        target = 'https://www.youtube.com/oembed?format=json&url=' + urllib.parse.quote(
            'https://www.youtube.com/watch?v=' + vid, safe='')
        req = urllib.request.Request(target, headers={'User-Agent': USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
        return jsonify({'id': vid, 'title': data.get('title'), 'channel': data.get('author_name')})
    except Exception:
        return jsonify({'id': vid, 'title': None, 'channel': None})


@app.get('/api/thumbnail')
def api_thumbnail():
    vid = request.args.get('id', '')
    if not ID_RE.match(vid):
        return json_error('Bad video id.')
    name = safe_filename(request.args.get('name') or vid)
    for size in ('maxresdefault', 'sddefault', 'hqdefault', 'mqdefault'):
        try:
            req = urllib.request.Request('https://i.ytimg.com/vi/%s/%s.jpg' % (vid, size),
                                         headers={'User-Agent': USER_AGENT})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
            if len(data) > 1500:
                return send_file(io.BytesIO(data), mimetype='image/jpeg', as_attachment=True,
                                 download_name=name + '.jpg', max_age=0)
        except Exception:
            continue
    return json_error("Couldn't get the thumbnail from YouTube. Try again.", 502)


def _number(value):
    try:
        number = int(value)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


@app.post('/api/download')
def api_download():
    data = request.get_json(silent=True) or {}
    vid = video_id_from(clean_url(data.get('url')))
    if not vid:
        return json_error('Pick a single video to download.')
    kind = data.get('kind')
    if kind not in ('video', 'audio', 'subs'):
        return json_error('Unknown download type.')
    params = {'url': 'https://www.youtube.com/watch?v=' + vid, 'kind': kind,
              'title': str(data.get('title') or '')[:200]}
    if kind == 'video':
        q = _number(data.get('q'))
        params['q'] = q if q in LADDER else 'best'
        fid = str(data.get('fid') or '')
        params['fid'] = fid if FID_RE.match(fid) else None
        params['progressive'] = bool(data.get('progressive'))
        params['vsize'] = _number(data.get('vsize'))
        params['asize'] = _number(data.get('asize'))
    elif kind == 'audio':
        choice = data.get('audio') or 'mp3-192'
        if choice not in AUDIO_CHOICES:
            return json_error('Unknown audio format.')
        params['audio'] = choice if FFMPEG else 'm4a'
    else:
        lang = str(data.get('lang') or '')
        if not LANG_RE.match(lang):
            return json_error('Pick a subtitle language first.')
        params['lang'] = lang
    job = Job(params)
    with JOBS_LOCK:
        JOBS[job.id] = job
    EXECUTOR.submit(run_job, job)
    return jsonify({'id': job.id})


@app.post('/api/jobs')
def api_jobs():
    data = request.get_json(silent=True) or {}
    out = []
    for job_id in [str(i) for i in (data.get('ids') or [])][:1000]:
        job = JOBS.get(job_id)
        out.append(job.public() if job else {'id': job_id, 'state': 'gone'})
    return jsonify({'jobs': out})


@app.post('/api/cancel/<job_id>')
def api_cancel(job_id):
    job = JOBS.get(job_id)
    if not job:
        return json_error('That download no longer exists.', 404)
    if job.state in Job.ACTIVE:
        job.cancel = True
        if job.state == 'queued':
            job.state, job.stage, job.ended = 'cancelled', 'Cancelled', time.time()
    return jsonify(job.public())


@app.get('/api/file/<job_id>')
def api_file(job_id):
    job = JOBS.get(job_id) if JOB_RE.match(job_id) else None
    if not job or job.state != 'done' or not job.file or not os.path.isfile(job.file):
        return json_error('This file is no longer here. Download it again.', 404)
    job.served = job.served or time.time()
    return send_file(job.file, as_attachment=True, download_name=job.name, max_age=0)


@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def website(path):
    if path.startswith('api/'):
        return json_error('Unknown address.', 404)
    if path:
        full = safe_join(WEB_DIR, path)
        if full and os.path.isfile(full):
            return send_from_directory(WEB_DIR, path, max_age=0)
    # Anything else (like /watch?v=... from the address-swap trick) gets the site,
    # which reads the link from the address bar.
    return send_from_directory(WEB_DIR, 'index.html', max_age=0)


# ===================================================================== start

def _port_free(host, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _is_running_here(port):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open('http://127.0.0.1:%d/api/status' % port, timeout=2) as resp:
            return json.load(resp).get('app') == APP_NAME
    except Exception:
        return False


def main():
    global ACTIVE_PORT
    host = '0.0.0.0' if ALLOW_PHONES_ON_WIFI else '127.0.0.1'
    port = None
    for candidate in range(PORT, PORT + 11):
        if _port_free(host, candidate):
            port = candidate
            break
        if _is_running_here(candidate):
            say('\n  %s is already running. Opening it in your browser...' % APP_NAME)
            webbrowser.open('http://localhost:%d' % candidate)
            return
    if port is None:
        say('\n  Ports %d-%d are all busy. Change PORT at the top of server.py.' % (PORT, PORT + 10))
        return
    ACTIVE_PORT = port

    shutil.rmtree(TEMP_DIR, ignore_errors=True)
    os.makedirs(TEMP_DIR, exist_ok=True)
    threading.Thread(target=janitor, daemon=True).start()

    local_url = 'http://localhost:%d' % port
    ip = lan_ip() if ALLOW_PHONES_ON_WIFI else None
    line = '  ' + '=' * 58
    say()
    say(line)
    say('    %s is running' % APP_NAME)
    say(line)
    say('    On this computer:  %s' % local_url)
    if ip:
        say('    On your phone:     http://%s:%d   (same Wi-Fi)' % (ip, port))
    say()
    say('    FFmpeg:      %s' % ('ready, HD video and MP3 work' if FFMPEG
                                 else 'missing, fewer qualities and no MP3 until START.bat installs it'))
    say('    JavaScript:  %s' % ('%s ready' % JS_RUNTIME_NAME.capitalize() if JS_RUNTIME_NAME
                                 else 'missing, some videos may fail (run START.bat again)'))
    say('    Engine:      yt-dlp %s' % YTDLP_VERSION)
    if os.path.isfile(COOKIES_FILE):
        say('    Cookies:     cookies.txt found, used for age-restricted videos')
    say()
    say('    Keep this window open while you download.')
    say('    Close it to stop the downloader.')
    say(line)
    say()

    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    flask.cli.show_server_banner = lambda *args, **kwargs: None
    if OPEN_BROWSER_ON_START and os.environ.get('RAX_NO_BROWSER') != '1':
        threading.Timer(1.2, lambda: webbrowser.open(local_url)).start()
    app.run(host=host, port=port, threaded=True, debug=False, use_reloader=False)


if __name__ == '__main__':
    main()
