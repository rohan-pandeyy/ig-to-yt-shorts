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

    Returns a dict with: shortcode, title, caption, tags, video_path, thumbnail_path.
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
    return {
        'shortcode': info.get('id'),
        'title': info.get('title') or '',
        'caption': caption,
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


def build_title(title):
    """Make a YouTube-safe title ending in #Shorts (no angle brackets, <=100 chars)."""
    suffix = ' #Shorts'
    title = re.sub(r'[<>]', '', ' '.join(title.split()))
    title = title[:MAX_TITLE_LEN - len(suffix)].rstrip()
    return (title + suffix).strip()


def build_description(caption):
    """Use the Instagram caption as the description, ending in #Shorts, within 5000 bytes."""
    caption = re.sub(r'[<>]', '', caption or '').strip()
    suffix = '' if '#shorts' in caption.lower() else '\n\n#Shorts'
    budget = MAX_DESC_BYTES - len(suffix.encode('utf-8'))
    caption = caption.encode('utf-8')[:budget].decode('utf-8', errors='ignore').rstrip()
    return (caption + suffix).strip()


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


def initialize_upload(youtube, file, title, tags, privacy='public', caption='', on_progress=None):
    """Upload the video file to YouTube Shorts. Returns the API response (contains 'id').

    on_progress, if given, is called with a float 0..1 as chunks are sent;
    otherwise progress is printed.

    Temporary errors are retried up to UPLOAD_RETRIES times by resuming the *same*
    upload session, which can never create a second video. If it still fails:
    - UploadNotCreated: YouTube rejected it (4xx) or the session never started.
    - UploadOutcomeUnknown: anything else after the session started.
    """
    body = {
        'snippet': {
            'title': build_title(title),
            'description': build_description(caption),
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
    parser.add_argument('--title', help='Custom YouTube title (default: title extracted from the reel)')
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
    print(f"Extracted title: {reel['title']}")
    print(f"Extracted tags: {reel['tags']}")
    print(f"Caption: {reel['caption'][:200]}{'...' if len(reel['caption']) > 200 else ''}")
    print(f"Downloaded to {reel['video_path']}")

    title = (args.title or reel['title']).strip()
    if not title:
        title = input("Enter YouTube Shorts Title: ").strip()

    if args.dry_run:
        print(f"\nDry run: would upload as {build_title(title)!r} ({args.privacy}). Nothing uploaded.")
        return

    print("\nAuthenticating with YouTube...")
    youtube = get_authenticated_service()

    print("\nUploading to YouTube Shorts...")
    response = initialize_upload(youtube, reel['video_path'], title, with_shorts_tag(reel['tags']),
                                 args.privacy, reel['caption'])
    video_id = response['id']
    print(f"\nVideo uploaded! YouTube URL: https://youtube.com/shorts/{video_id}")
    print(f"\nThe video file is saved at: {reel['video_path']}\n"
          "You can clear the 'temp' folder manually when done.")


if __name__ == '__main__':
    main()
