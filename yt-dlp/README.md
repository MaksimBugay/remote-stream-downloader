# Downloading with plain yt-dlp (no service)

Same downloads as the service, straight from Terminal on the Mac.

## Setup (once)

```bash
brew install yt-dlp ffmpeg deno     # deno: needed by yt-dlp for YouTube
brew upgrade yt-dlp                 # keep it current, sites change often
```

For cookies straight from Chrome, Terminal needs **Full Disk Access**
(System Settings → Privacy & Security → Full Disk Access → Terminal, then ⌘Q and
reopen). macOS asks for Keychain access to "Chrome Safe Storage" on each run:
enter your Mac password and click **Allow**.

## Basic download

```bash
yt-dlp --cookies-from-browser chrome --no-playlist -N 8 \
  -o "$HOME/Downloads/%(title).100B [%(id)s].%(ext)s" "URL"
```

- `--cookies-from-browser chrome` — use your Chrome login (needed for YouTube bot
  checks, Facebook, LinkedIn, private videos). Another profile: `chrome:"Profile 1"`.
- `--no-playlist` — only the video itself, even if the link has `list=...`.
- `-N 8` — download HLS/DASH videos in 8 parallel pieces (faster).
- Interrupted? Run the same command again — it resumes.
- Check first without downloading: add `--simulate --print "%(title)s %(height)sp"`.

Quality (the service's presets):

| Want | Add |
|---|---|
| best (default) | — |
| up to 1080p / 720p / 480p | `-f "bv*[height<=720]+ba/b[height<=720]/bv*+ba/b"` (change the number) |
| audio only | `-f "ba/b" -x` |
| MP4 for browsers/phones | `--merge-output-format mp4` |

A cookies file instead of Chrome: `--cookies FILE` — use a **copy**, yt-dlp rewrites
the file when it finishes.

## LinkedIn

yt-dlp itself can't read logged-in LinkedIn pages or group posts; load the
service's extractor with `--plugin-dirs`:

```bash
yt-dlp --cookies-from-browser chrome \
  --plugin-dirs ~/work/remote-stream-downloader/app/ytdlp_plugins \
  -o "$HOME/Downloads/%(title).100B [%(id)s].%(ext)s" "https://www.linkedin.com/feed/update/urn:li:groupPost:..."
```

## VK (vk.ru / vkvideo.ru / vk.com)

yt-dlp always asks `vk.com`, but Chrome keeps the VK login on `vk.ru`, so the plain
command fails with "Video only available to followers". Export the VK cookies and add
`vk.com` copies of the `vk.ru` ones:

```bash
cd ~/work/pushca/AWS/prod/docker/remote-stream-downloader
OUT=/tmp/vk-test.txt ./export-vk-cookies.sh          # VK cookies from normal Chrome

awk -F'\t' 'NR==FNR { if (NF>=7) { d=$1; sub(/^#HttpOnly_/,"",d); sub(/^\./,"",d); if (d=="vk.com") seen[$3 FS $6]=1 }; next }
  { print }
  NF>=7 { d=$1; sub(/^#HttpOnly_/,"",d); sub(/^\./,"",d); if (d=="vk.ru" && !(($3 FS $6) in seen)) { l=$0; sub(/vk\.ru/,"vk.com",l); print l } }' \
  /tmp/vk-test.txt /tmp/vk-test.txt > /tmp/vk-dl-cookies.txt

yt-dlp --cookies /tmp/vk-dl-cookies.txt --no-playlist -N 8 \
  -o "$HOME/video/VK/%(title).100B [%(id)s].%(ext)s" "https://vkvideo.ru/video-..."

rm /tmp/vk-test.txt /tmp/vk-dl-cookies.txt           # they hold your VK login
```

Cookies from your normal Chrome only work from your own IP — never copy them to prod
(see `README-vk-cookies.md` in `pushca/AWS/prod/docker/remote-stream-downloader`).
