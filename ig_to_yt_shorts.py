"""
ig_to_yt_shorts.py
------------------
Download an Instagram Reel and upload it to YouTube Shorts.
- Uses yt-dlp for downloading (video, caption, hashtags and thumbnail in one pass).
- Uses YouTube Data API v3 for uploading.
- Handles ffmpeg automatically.
- Downloads to a temp folder in the script directory (not deleted automatically).

Works as a CLI and as the processing engine imported by the dashboard worker.

Author: Your Name
License: MIT
"""

import argparse
import os
import pickle
import re
import shutil
import subprocess
import sys
import urllib.request
import uuid
import zipfile

import google.auth.exceptions
import google.auth.transport.requests
import google_auth_oauthlib.flow
import googleapiclient.discovery
import googleapiclient.errors
import googleapiclient.http
import yt_dlp

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CLIENT_SECRETS_FILE = os.path.join(SCRIPT_DIR, "client_secrets.json")
TOKEN_FILE = os.path.join(SCRIPT_DIR, "token.pickle")
FFMPEG_FOLDER = os.path.join(SCRIPT_DIR, 'ffmpeg_bin')
FFMPEG_EXE = os.path.join(FFMPEG_FOLDER, 'ffmpeg.exe')
TEMP_FOLDER = os.path.join(SCRIPT_DIR, 'temp')
MAX_TITLE_LEN = 100        # YouTube API limit (characters)
MAX_TAGS_TOTAL_LEN = 500   # YouTube API limit (combined length of all tags)
MAX_DESC_BYTES = 5000      # YouTube API limit (UTF-8 bytes)

# instagram.com/reel/<id>, /reels/<id>, /p/<id>, /tv/<id>, optionally /<username>/reel/<id>
REEL_URL_RE = re.compile(
    r'^https?://(?:www\.|m\.)?instagram\.com/(?:[A-Za-z0-9_.]+/)?'
    r'(reel|reels|p|tv)/([A-Za-z0-9_-]+)/?(?:[?#].*)?$'
)


UPLOAD_RETRIES = 5        # bounded, exponential backoff inside googleapiclient (5xx/429/network)


class AuthRequired(Exception):
    """No usable YouTube credentials and interactive sign-in is not allowed."""


class UploadNotCreated(Exception):
    """The upload failed and YouTube definitely did not create a video. Safe to retry."""

    def __init__(self, message, status=None, reasons=()):
        super().__init__(message)
        self.status = status
        self.reasons = tuple(reasons)


class UploadOutcomeUnknown(Exception):
    """The upload failed after data was sent; YouTube may or may not have the video.

    Never retried automatically, because a retry could create a duplicate.
    """


def _http_error_reasons(err):
    try:
        return [e.get('reason', '') for e in err.error_details or []]
    except Exception:
        return []


def parse_reel_url(url):
    """Return (shortcode, canonical_url) for an Instagram reel/post URL, or None if invalid.

    The canonical URL drops tracking parameters such as ?igsh=..., so the same reel
    shared twice maps to the same shortcode.
    """
    match = REEL_URL_RE.match((url or '').strip())
    if not match:
        return None
    kind, shortcode = match.groups()
    kind = 'reel' if kind == 'reels' else kind
    return shortcode, f"https://www.instagram.com/{kind}/{shortcode}/"


def ensure_ffmpeg():
    """Ensure ffmpeg is available, download if needed."""
    try:
        subprocess.run(['ffmpeg', '-version'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return None  # ffmpeg is in PATH
    except Exception:
        pass
    if os.path.exists(FFMPEG_EXE):
        return FFMPEG_EXE
    url = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'
    zip_path = os.path.join(FFMPEG_FOLDER, 'ffmpeg.zip')
    os.makedirs(FFMPEG_FOLDER, exist_ok=True)
    try:
        print('Downloading ffmpeg...')
        urllib.request.urlretrieve(url, zip_path)
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            for member in zip_ref.namelist():
                if member.endswith('ffmpeg.exe'):
                    zip_ref.extract(member, FFMPEG_FOLDER)
                    src = os.path.join(FFMPEG_FOLDER, member)
                    shutil.move(src, FFMPEG_EXE)
                    break
        os.remove(zip_path)
        return FFMPEG_EXE
    except Exception as e:
        print(f"Failed to download ffmpeg: {e}")
        return None


def fetch_reel(insta_url, ffmpeg_path=None, quiet=False):
    """Download a reel and its thumbnail to the temp folder in a single yt-dlp pass.

    Returns a dict with: shortcode, caption, handle, full_name, tags, video_path, thumbnail_path.
    (yt-dlp's own 'title' is just "Video by <user>", so it isn't used.)
    """
    os.makedirs(TEMP_FOLDER, exist_ok=True)
    base = os.path.join(TEMP_FOLDER, uuid.uuid4().hex)
    ydl_opts = {
        'outtmpl': base + '.%(ext)s',
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
        'writethumbnail': True,
        'quiet': quiet,
        'noprogress': quiet,
    }
    if ffmpeg_path:
        ydl_opts['ffmpeg_location'] = os.path.dirname(ffmpeg_path)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(insta_url, download=True)

    downloads = info.get('requested_downloads') or []
    video_path = downloads[0].get('filepath') if downloads else None
    if not video_path or not os.path.exists(video_path):
        raise RuntimeError("yt-dlp finished but no video file was written")
    thumbnail_path = next(
        (t['filepath'] for t in info.get('thumbnails') or [] if t.get('filepath')), None)

    caption = info.get('description') or ''
    tags = [tag.lstrip('#') for tag in re.findall(r'#\w+', caption)]
    handle = info.get('channel') or info.get('uploader_id')
    if handle and handle.isdigit():   # some yt-dlp paths put the numeric user id here
        handle = None
    return {
        'shortcode': info.get('id'),
        'caption': caption,
        'handle': handle,
        'full_name': info.get('uploader'),
        'tags': tags,
        'video_path': video_path,
        'thumbnail_path': thumbnail_path,
    }


def _save_credentials(creds):
    with open(TOKEN_FILE, "wb") as token:
        pickle.dump(creds, token)


def get_authenticated_service(interactive=True):
    """Return a YouTube API service object, reusing/refreshing the saved token.

    With interactive=False (background worker) a browser sign-in is never started;
    AuthRequired is raised instead so the caller can ask the user to run --auth.
    """
    creds = None
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "rb") as token:
            creds = pickle.load(token)

    if creds and not creds.valid and creds.expired and creds.refresh_token:
        try:
            creds.refresh(google.auth.transport.requests.Request())
            _save_credentials(creds)
        except google.auth.exceptions.RefreshError:
            creds = None  # refresh token revoked or expired (7-day limit in Testing mode)

    if not creds or not creds.valid:
        if not interactive:
            raise AuthRequired(
                "YouTube sign-in is missing or expired. On the PC, run: "
                ".venv/bin/python ig_to_yt_shorts.py --auth")
        if not os.path.exists(CLIENT_SECRETS_FILE):
            sys.exit(f"Missing {CLIENT_SECRETS_FILE}.\n"
                     "Download your OAuth 2.0 Desktop client JSON from Google Cloud Console "
                     "and save it there (see README).")
        flow = google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file(
            CLIENT_SECRETS_FILE, SCOPES)
        creds = flow.run_local_server(port=0)
        _save_credentials(creds)
    return googleapiclient.discovery.build("youtube", "v3", credentials=creds)


# ---------- YouTube title & description (single source of truth) ----------
#
# Title:  the Instagram caption itself. If it is longer than 100 characters, it is cut
#         between words, "..." is appended, and the rest goes to the description, so no
#         caption text is lost. Line breaks become spaces in the title (titles are one line).
# Description: [rest of the caption, original formatting]  +  attribution  +  #Shorts
# A custom title wins; the description then carries the whole caption.

ELLIPSIS = '...'
DESC_TRUNCATED_NOTE = ' [caption shortened: YouTube description limit]'


def yt_len(text):
    """Length as YouTube's 100-character limit is checked, conservatively.

    Counts UTF-16 code units, so most emoji count as 2. This can only over-count.
    """
    return len(text.encode('utf-16-le')) // 2


def _safe(text):
    """YouTube rejects < and > in titles and descriptions; swap in look-alikes rather than drop them."""
    return (text or '').replace('<', '‹').replace('>', '›')


def _one_line(text):
    return ' '.join(text.split())


def split_caption(caption, limit=MAX_TITLE_LEN):
    """Split a caption into (title, overflow).

    - Fits in `limit` (after joining lines): (whole caption on one line, '').
    - Otherwise: title = as many whole words as fit with '...' appended (<= limit);
      overflow = the caption from the next word on, with its original line breaks.
    - A single first word longer than the limit (e.g. a long URL) is the one case that
      is cut mid-word; the rest of it starts the overflow.
    """
    text = _safe(caption).strip()
    flat = _one_line(text)
    if not flat:
        return '', ''
    if yt_len(flat) <= limit:
        return flat, ''

    budget = limit - yt_len(ELLIPSIS)
    words = list(re.finditer(r'\S+', text))
    kept, used = [], 0
    for match in words:
        cost = yt_len(match.group()) + (1 if kept else 0)
        if used + cost > budget:
            break
        kept.append(match.group())
        used += cost

    if kept:
        next_word = words[len(kept)]
        return ' '.join(kept) + ELLIPSIS, text[next_word.start():]

    # First word alone is too long: cut it on a character boundary.
    first = words[0].group()
    head, size = '', 0
    for ch in first:
        if size + yt_len(ch) > budget:
            break
        head += ch
        size += yt_len(ch)
    return head + ELLIPSIS, text[len(head):]


def clean_custom_title(title):
    """Normalise a user-supplied title. Returns '' for blank; raises ValueError if too long."""
    title = _one_line(_safe(title))
    if yt_len(title) > MAX_TITLE_LEN:
        raise ValueError(f"Title is too long: {yt_len(title)} of {MAX_TITLE_LEN} characters "
                         "(emoji count as 2)")
    return title


def fallback_title(handle=None):
    """Used only when the reel has no caption at all."""
    return f"Instagram Reel by @{handle}" if handle else "Instagram Reel"


def _attribution(handle=None, full_name=None, url=None):
    if handle:
        name = f" ({_one_line(_safe(full_name))})" if full_name and _one_line(full_name) != handle else ''
        line = f"Originally posted by @{handle}{name} on Instagram"
    else:
        line = "Originally posted on Instagram"
    return f"{line}\n{url}" if url else line


def _fit_bytes(text, max_bytes):
    """Cut text to max_bytes of UTF-8 at a word boundary, marking that it was shortened."""
    if len(text.encode('utf-8')) <= max_bytes:
        return text
    room = max_bytes - len((ELLIPSIS + DESC_TRUNCATED_NOTE).encode('utf-8'))
    cut = text.encode('utf-8')[:max(room, 0)].decode('utf-8', errors='ignore')
    if ' ' in cut.strip():
        cut = cut[:cut.rstrip().rfind(' ')]
    return cut.rstrip() + ELLIPSIS + DESC_TRUNCATED_NOTE


def build_youtube_metadata(caption, handle=None, full_name=None, url=None, custom_title=None):
    """Return (title, description) for a reel. Used by the CLI, worker and dashboard.

    Pure and deterministic: the same stored inputs always give the same result, so a
    retry uploads with exactly the title that was shown before.
    """
    custom = clean_custom_title(custom_title) if custom_title else ''
    if custom:
        title, body = custom, _safe(caption).strip()
    else:
        title, body = split_caption(caption)
        title = title or fallback_title(handle)

    tail = _attribution(handle, full_name, url) + '\n\n#Shorts'
    if body:
        room = MAX_DESC_BYTES - len(('\n\n' + tail).encode('utf-8'))
        description = _fit_bytes(body, room) + '\n\n' + tail
    else:
        description = tail
    return title, description


def limit_tags(tags):
    """Drop tags once the combined length would exceed YouTube's limit."""
    kept, total = [], 0
    for tag in tags:
        tag = re.sub(r'[<>]', '', tag)
        cost = len(tag) + (2 if ' ' in tag else 0) + (1 if kept else 0)
        if not tag or total + cost > MAX_TAGS_TOTAL_LEN:
            continue
        kept.append(tag)
        total += cost
    return kept


def initialize_upload(youtube, file, title, description, tags, privacy='public', on_progress=None):
    """Upload the video file to YouTube Shorts. Returns the API response (contains 'id').

    title/description are used as given; build them with build_youtube_metadata().

    on_progress, if given, is called with a float 0..1 as chunks are sent;
    otherwise progress is printed.

    Temporary errors are retried up to UPLOAD_RETRIES times by resuming the *same*
    upload session, which can never create a second video. If it still fails:
    - UploadNotCreated: YouTube rejected it (4xx) or the session never started.
    - UploadOutcomeUnknown: anything else after the session started.
    """
    body = {
        'snippet': {
            'title': title,
            'description': description,
            'tags': limit_tags(tags),
            'categoryId': '22',  # People & Blogs
        },
        'status': {
            'privacyStatus': privacy,
            'selfDeclaredMadeForKids': False,
        }
    }
    insert_request = youtube.videos().insert(
        part=','.join(body.keys()),
        body=body,
        media_body=googleapiclient.http.MediaFileUpload(file, chunksize=-1, resumable=True)
    )
    response = None
    while response is None:
        try:
            status, response = insert_request.next_chunk(num_retries=UPLOAD_RETRIES)
        except googleapiclient.errors.HttpError as e:
            code = e.resp.status
            if 400 <= code < 500 or insert_request.resumable_uri is None:
                raise UploadNotCreated(f"YouTube rejected the upload (HTTP {code}): {e.reason}",
                                       code, _http_error_reasons(e)) from e
            raise UploadOutcomeUnknown(f"Upload failed after sending data (HTTP {code}): {e.reason}") from e
        except Exception as e:
            if insert_request.resumable_uri is None:
                raise UploadNotCreated(f"Upload could not start: {e}") from e
            raise UploadOutcomeUnknown(f"Upload failed after sending data: {e}") from e
        if status:
            if on_progress:
                on_progress(status.progress())
            else:
                print(f"Uploading... {int(status.progress() * 100)}%")
    if on_progress:
        on_progress(1.0)
    else:
        print("Upload Complete!")
    return response


def with_shorts_tag(tags):
    """Copy of tags with 'shorts' appended if not already present."""
    tags = list(tags or [])
    if 'shorts' not in [t.lower() for t in tags]:
        tags.append('shorts')
    return tags


def main():
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(description="Upload Instagram Reel to YouTube Shorts")
    parser.add_argument('--url', help='Instagram Reel URL')
    parser.add_argument('--title', help='Custom YouTube title, max 100 characters '
                                        '(default: the Instagram caption)')
    parser.add_argument('--privacy', choices=['public', 'unlisted', 'private'], default='public',
                        help='YouTube privacy status (default: public)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Download and show metadata, but do not upload')
    parser.add_argument('--auth', action='store_true',
                        help='Sign in to YouTube (or renew an expired sign-in) and exit')
    args = parser.parse_args()

    if args.auth:
        get_authenticated_service(interactive=True)
        print(f"YouTube sign-in OK. Token saved to {TOKEN_FILE}")
        return

    raw_url = args.url or input("Enter Instagram Reel URL: ").strip()
    parsed = parse_reel_url(raw_url)
    if not parsed:
        sys.exit("That doesn't look like an Instagram reel URL "
                 "(expected instagram.com/reel/<id>/ or instagram.com/p/<id>/).")
    _, insta_url = parsed

    print("\nChecking ffmpeg...")
    ffmpeg_path = ensure_ffmpeg()

    print("\nDownloading Instagram Reel...")
    reel = fetch_reel(insta_url, ffmpeg_path)
    print(f"Downloaded to {reel['video_path']}")

    try:
        title, description = build_youtube_metadata(
            reel['caption'], reel['handle'], reel['full_name'], insta_url, custom_title=args.title)
    except ValueError as e:
        sys.exit(str(e))
    print(f"\nYouTube title ({yt_len(title)}/{MAX_TITLE_LEN}): {title}")
    print(f"Tags: {reel['tags']}")
    print(f"Description:\n{description}\n")

    if args.dry_run:
        print(f"Dry run: nothing uploaded (would be {args.privacy}).")
        return

    print("\nAuthenticating with YouTube...")
    youtube = get_authenticated_service()

    print("\nUploading to YouTube Shorts...")
    response = initialize_upload(youtube, reel['video_path'], title, description,
                                 with_shorts_tag(reel['tags']), args.privacy)
    video_id = response['id']
    print(f"\nVideo uploaded! YouTube URL: https://youtube.com/shorts/{video_id}")
    print(f"\nThe video file is saved at: {reel['video_path']}\n"
          "You can clear the 'temp' folder manually when done.")


if __name__ == '__main__':
    main()
