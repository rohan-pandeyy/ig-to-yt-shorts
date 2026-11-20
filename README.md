# 📲 Instagram Reel to YouTube Shorts Uploader

Queue Instagram Reels from your phone and let your PC upload them to your YouTube channel as
Shorts, one at a time on a schedule you choose. Everything runs on your own computer.

---

## 🚀 Features

- 📱 Mobile-friendly dashboard on your Wi-Fi, opened by scanning a QR code in the terminal
- 🗓️ Upload queue: one reel per interval (default: every hour), oldest first
- ⚡ **Upload now** for a single reel, optionally with a custom title, without changing the schedule
- 🧠 Uses the Instagram caption as the YouTube title; longer captions continue in the description,
  with credit to the original creator
- 🛡️ Never uploads the same reel twice; failed reels stay visible with a Retry button
- 🔒 Password-protected dashboard; Google sign-in and all files stay on your PC
- 🖥️ One-command start on Linux (`start.sh`) and Windows (`start.bat`)
- 🍎 iPhone Shortcut: Instagram → Share → **Add to Reel Queue**, no copying or pasting

---

## 🛠️ Prerequisites

- **Python 3.11 or newer**
- **FFmpeg** (the start script tells you how to install it if it's missing)
- A Google Cloud project with the **YouTube Data API v3** and a `client_secrets.json` (below)

---

## 🔑 Google API Setup (once)

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and create a project.
2. **APIs & Services → Library**: enable **YouTube Data API v3**.
3. **Google Auth Platform**: fill in the app name and your email, choose **External**, then under
   **Audience → Test users** add the Google account that owns your YouTube channel.
4. **Clients → Create client → Desktop app**, then download the JSON file.
5. Save it in the project folder as **`client_secrets.json`**.

---

## ⚡ Quick start

### Linux

```bash
git clone https://github.com/k69883/ig-to-yt-shorts.git
cd ig-to-yt-shorts
./start.sh
```

### Windows

```bat
git clone https://github.com/k69883/ig-to-yt-shorts.git
cd ig-to-yt-shorts
start.bat
```

(You can also double-click `start.bat` in File Explorer.)

### First run

The start script walks you through everything that's missing:

1. Creates the `.venv` virtual environment and installs the Python packages.
2. Checks for FFmpeg and `client_secrets.json`, and explains how to fix anything missing.
3. Asks you to choose a **dashboard password** (you'll type it on your phone).
4. Offers to **sign in to YouTube**: a browser window opens; pick the account you added as a
   test user. "Google hasn't verified this app" is expected for a personal app: click **Continue**.
5. Starts the dashboard and prints its address with a **QR code**. Scan it with your phone.

### Every time after that

Run the same command: `./start.sh` (Linux) or `start.bat` (Windows). It skips everything that's
already set up and starts in a second or two. Keep the terminal open while you want uploads to
happen, and press **Ctrl+C** to stop. (On Windows, cmd may then ask "Terminate batch job (Y/N)?":
either answer is fine.)

Options: `--port 8081` uses a different port; `--new-shortcut-key` / `--revoke-shortcut-key`
manage the iPhone Shortcut key (see below). Change the dashboard password with
`.venv/bin/python server.py --set-password` (Windows: `.venv\Scripts\python server.py --set-password`).

---

## 📱 Using the dashboard

- **Add reels:** paste one or more Instagram reel links (one per line).
- **Schedule:** the PC uploads the oldest waiting reel once per interval. If the PC was asleep it
  does one upload when it wakes, then continues normally, without catching up on missed slots.
- **Upload now:** uploads that reel as soon as the current upload (if any) finishes. You can type a
  custom title (up to 100 characters); leave it empty to use the caption.
- **Needs attention:** failed reels keep their place and can be retried. A reel that was cut off
  mid-upload shows **Check channel**: look on YouTube, then tap "It's on YouTube" or "Not there, retry".
- **Paused:** if YouTube's daily upload quota runs out, uploads resume after it resets. If the
  YouTube sign-in expires (every 7 days while the Google app is in "Testing" mode), restart the
  start script and choose to sign in, or run `.venv/bin/python ig_to_yt_shorts.py --auth`.

### Titles and descriptions

- **Title:** the Instagram caption. Captions longer than 100 characters are cut between words
  with `...`, and the rest continues at the top of the description, so nothing is lost.
- **Description:** the rest of the caption, then `Originally posted by @username on Instagram`
  with the original link, then `#Shorts`.
- A reel with no caption gets the title `Instagram Reel by @username`.

---

## 🍎 iPhone: share reels straight from Instagram

An iOS Shortcut called **Add to Reel Queue** appears in Instagram's Share menu and sends the
reel to your PC. It's added to the end of the queue exactly like **Add to queue** on the
dashboard: it never starts an upload by itself.

**1. Create the Shortcut key on the PC** (the dashboard can keep running):

```bash
./start.sh --new-shortcut-key          # Windows: start.bat --new-shortcut-key
```

It prints the key once, with a QR code containing only the key, plus the URL to use. Scan the
QR with the iPhone camera to copy the key (or type it: case and dashes don't matter), then
clear the terminal. Running it again replaces the key and the old one stops working at once.
Turn the Shortcut off with `--revoke-shortcut-key`.

**2. Build the Shortcut** in Apple's Shortcuts app (name: *Add to Reel Queue*):

- Shortcut details: **Show in Share Sheet** on; it receives **URLs** and **Text**.
- **Match Text** `(https?://)?(www\.|m\.)?instagram\.com/\S+` in *Shortcut Input*.
  If there's no match: **Show Notification** "No Instagram reel link found" and **Stop**.
- **Get Contents of URL** `http://<PC address>:8080/api/shortcut/add`, method **POST**,
  header `Authorization` = `Bearer <your key>`, request body **JSON** with a text field `url`
  set to the first match.
- **Get Dictionary Value** `message` from the result and **Show Notification** with it.

The server replies with JSON: `status` is `added`, `duplicate`, `invalid`, `unauthorized`,
`not_configured`, `locked`, `forbidden` or `error`, and `message` is a sentence ready to show.

**How the key is protected**

- Only a SHA-256 hash of the key is stored (in `queue.db`); the key itself is shown once and
  never appears in the dashboard, logs or replies. It can't be printed into a file or pipe.
- The key works only for `POST /api/shortcut/add` (adding a reel). It can't log in, see the
  queue, remove or upload anything, or change settings. The dashboard login doesn't work on
  that address either.
- Five wrong keys from one device lock that device out for five minutes.
- **Plain HTTP:** the key travels unencrypted on your Wi-Fi, just like the dashboard password
  does. Someone already on your network could capture it and add reels to your queue (which
  would then be uploaded publicly). Only use this on a Wi-Fi you trust, keep guests on a
  separate guest network, check the queue now and then, and make a new key if in doubt.
  Nothing is exposed to the internet.
- Give the PC a fixed address (a DHCP reservation in your router), so the Shortcut's URL keeps
  working.

---

## 🧰 Troubleshooting

**The phone can't open the dashboard**

- The phone must be on the **same Wi-Fi** as the PC (not mobile data, not a guest network:
  guest networks often block devices from reaching each other).
- **Windows:** the first time, Windows asks whether Python may use the network. Allow it on
  **Private networks**, and make sure your Wi-Fi is set as a *Private* network in Windows settings.
- **Linux with firewalld (Fedora):** the default *FedoraWorkstation* zone already allows the
  dashboard. If you use another zone: `sudo firewall-cmd --add-port=8080/tcp --permanent && sudo firewall-cmd --reload`
- If the start script lists several network addresses (Wi-Fi + Ethernet, VPN), try the others.

**"The dashboard is already running"**: another terminal is already running it. Use that one,
or stop it with Ctrl+C first.

**"Port 8080 is already in use"**: start on another port: `./start.sh --port 8081`.

---

## 🖥️ Command-line upload (single reel, no dashboard)

```bash
.venv/bin/python ig_to_yt_shorts.py --url <INSTAGRAM_REEL_URL>
.venv/bin/python ig_to_yt_shorts.py --url <URL> --dry-run          # download and preview only
.venv/bin/python ig_to_yt_shorts.py --url <URL> --title "My title"  # custom title
```

On Windows use `.venv\Scripts\python` instead of `.venv/bin/python`.

`worker.py` also has a small CLI (`add`, `list`, `now`, `run`, …) for the queue without the dashboard.

---

## 📝 Notes

- Downloaded videos are kept in the `temp` folder; you can clear it manually.
- Local data: `queue.db` (queue, settings, password hash), `token.pickle` (YouTube sign-in) and
  `client_secrets.json`. They are git-ignored and never shared or overwritten by the start scripts.
- Uploads from a new, unverified Google Cloud project may be locked to private by YouTube until the
  project passes YouTube's API audit.
- Only upload reels you own or have permission to re-post.
- Tests: `.venv/bin/python -m unittest discover -s tests`

---

## 🪪 License

MIT License

---

## 🙋‍♂️ Author

k69883
