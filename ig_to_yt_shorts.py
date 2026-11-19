import os
import sys
import yt_dlp
import google_auth_oauthlib.flow
import googleapiclient.discovery
import googleapiclient.errors
import googleapiclient.http
import argparse
import shutil
import subprocess
import zipfile
import urllib.request
import tempfile
import webbrowser
import pickle
import google.auth.transport.requests
import uuid
import re

"""
ig_to_yt_shorts.py
------------------
Download an Instagram Reel and upload it to YouTube Shorts with a custom title and tags.
- Uses yt-dlp for downloading.
- Uses YouTube Data API v3 for uploading.
- Handles ffmpeg automatically.
- Downloads to a temp folder in the script directory (not deleted automatically).

Author: Your Name
License: MIT
"""

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CLIENT_SECRETS_FILE = os.path.join(SCRIPT_DIR, "client_secrets.json")
TOKEN_FILE = os.path.join(SCRIPT_DIR, "token.pickle")
FFMPEG_FOLDER = os.path.join(SCRIPT_DIR, 'ffmpeg_bin')
FFMPEG_EXE = os.path.join(FFMPEG_FOLDER, 'ffmpeg.exe')
TEMP_FOLDER = os.path.join(SCRIPT_DIR, 'temp')
MAX_TITLE_LEN = 100       # YouTube API limit
MAX_TAGS_TOTAL_LEN = 500  # YouTube API limit (combined length of all tags)


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

def extract_reel_metadata(insta_url, ffmpeg_path=None):
    """Extract title and hashtags from the Instagram Reel using yt-dlp."""
    ydl_opts = {
        'quiet': True,
        'skip_download': True,
    }
    if ffmpeg_path:
        ydl_opts['ffmpeg_location'] = os.path.dirname(ffmpeg_path)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(insta_url, download=False)
        title = info.get('title', '')
        description = info.get('description', '')
        # Extract hashtags from description
        hashtags = re.findall(r'#\w+', description)
        tags = [tag.lstrip('#') for tag in hashtags]
        return title, tags

def download_reel(insta_url, ffmpeg_path=None):
    """Download Instagram Reel to the temp folder and return its path."""
    os.makedirs(TEMP_FOLDER, exist_ok=True)
    unique_name = f"{uuid.uuid4().hex}.mp4"
    video_path = os.path.join(TEMP_FOLDER, unique_name)
    ydl_opts = {
        'outtmpl': video_path,
        'format': 'bestvideo+bestaudio/best',
        'quiet': False,
    }
    if ffmpeg_path:
        ydl_opts['ffmpeg_location'] = os.path.dirname(ffmpeg_path)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(insta_url, download=True)
        return video_path

def get_authenticated_service():
    """Authenticate and return a YouTube API service object, reusing token if possible."""
    creds = None
    token_path = TOKEN_FILE
    # Load existing credentials
    if os.path.exists(token_path):
        with open(token_path, "rb") as token:
            creds = pickle.load(token)
    # If no valid credentials, do the OAuth flow
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(google.auth.transport.requests.Request())
        else:
            if not os.path.exists(CLIENT_SECRETS_FILE):
                sys.exit(f"Missing {CLIENT_SECRETS_FILE}.\n"
                         "Download your OAuth 2.0 Desktop client JSON from Google Cloud Console "
                         "and save it there (see README).")
            flow = google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file(
                CLIENT_SECRETS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        # Save the credentials for the next run
        with open(token_path, "wb") as token:
            pickle.dump(creds, token)
    return googleapiclient.discovery.build("youtube", "v3", credentials=creds)

def build_title(title):
    """Make a YouTube-safe title ending in #Shorts (no angle brackets, <=100 chars)."""
    suffix = ' #Shorts'
    title = re.sub(r'[<>]', '', ' '.join(title.split()))
    title = title[:MAX_TITLE_LEN - len(suffix)].rstrip()
    return (title + suffix).strip()

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

def initialize_upload(youtube, file, title, tags, privacy='public'):
    """Upload the video file to YouTube Shorts with the given title and tags."""
    body = {
        'snippet': {
            'title': build_title(title),
            'description': '#Shorts',
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
        status, response = insert_request.next_chunk()
        if status:
            print(f"Uploading... {int(status.progress() * 100)}%")
    print("Upload Complete!")
    return response

def main():
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(description="Upload Instagram Reel to YouTube Shorts")
    parser.add_argument('--url', help='Instagram Reel URL')
    parser.add_argument('--privacy', choices=['public', 'unlisted', 'private'], default='public',
                        help='YouTube privacy status (default: public)')
    args = parser.parse_args()

    insta_url = args.url or input("Enter Instagram Reel URL: ").strip()

    print("\nChecking ffmpeg...")
    ffmpeg_path = ensure_ffmpeg()

    print("\nExtracting Reel metadata...")
    extracted_title, extracted_tags = extract_reel_metadata(insta_url, ffmpeg_path)
    print(f"Extracted title: {extracted_title}")
    print(f"Extracted tags: {extracted_tags}")

    # Always add 'shorts' tag
    tags = extracted_tags.copy() if extracted_tags else []
    if 'shorts' not in [t.lower() for t in tags]:
        tags.append('shorts')

    # If title is missing, prompt user
    if not extracted_title.strip():
        title = input("Enter YouTube Shorts Title: ").strip()
    else:
        title = extracted_title

    print("\nDownloading Instagram Reel...")
    video_file = download_reel(insta_url, ffmpeg_path)
    print(f"Downloaded to {video_file}")

    print("\nAuthenticating with YouTube...")
    youtube = get_authenticated_service()

    print("\nUploading to YouTube Shorts...")
    response = initialize_upload(youtube, video_file, title, tags, args.privacy)
    video_id = response['id']
    print(f"\nVideo uploaded! YouTube URL: https://youtube.com/shorts/{video_id}")
    print(f"\nThe video file is saved at: {video_file}\nYou can clear the 'temp' folder manually when done.")

if __name__ == '__main__':
    main() 