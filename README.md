# Patreon Archive

Patreon Archive (`pa`) is a command-line tool that downloads video from
Patreon posts and collections you're entitled to view. It signs in with your
own Patreon session from a desktop browser, and it runs on macOS, Linux, and
Android through Termux. Downloading is done by
[yt-dlp](https://github.com/yt-dlp/yt-dlp).

Use it only for content you have access to, for your own offline viewing.
What you download belongs to its creators: don't share or re-upload it.

## Features

- Downloads a single post, or a whole collection with entries picked by
  number.
- Names each file after its post's title, and embeds the title, date, and
  description in the file's own tags.
- Resumes: a re-run skips posts that are already complete.
- Downloads several posts at once with `-j`.
- Never writes to your cookies, and exports only Patreon's cookies for use on
  another device.
- Emits machine-readable JSON Lines events with `--agent`, for scripts.

## Requirements

- Python 3.12 or later.
- [ffmpeg](https://ffmpeg.org/), on your `PATH`.
- A desktop browser that's signed in to Patreon. Firefox is the default.

## Install on a desktop

Install `pa` with [pipx](https://pipx.pypa.io/) or
[uv](https://docs.astral.sh/uv/):

```bash
pipx install git+https://github.com/Flarette/patreon-archive
```

```bash
uv tool install git+https://github.com/Flarette/patreon-archive
```

On macOS, install ffmpeg with [Homebrew](https://brew.sh/):

```bash
brew install ffmpeg
```

Then check that `pa` finds ffmpeg and your Patreon session:

```bash
pa auth
```

`session_id present` means you're ready to download.

## Sign in with your browser's cookies

`pa` has no login of its own. It reads the cookies of a browser that's signed
in to Patreon, so sign in to patreon.com in that browser first.

- **Firefox** is the default. On macOS, if your default Firefox profile isn't
  signed in to Patreon, `pa` uses another profile that is, and prints a
  warning naming it.
- **Another browser:** pass `-b`, such as `-b chrome` or
  `-b 'chrome:Profile 1'`. On macOS, Chrome asks for access to your
  keychain.
- **A cookies file:** pass `--cookies-file FILE`, or set
  `PATREON_ARCHIVE_COOKIES`. Use this on a device with no browser.

If no Patreon session is found, `pa` stops before downloading anything. When
every post suddenly reports `no_access`, your session has probably expired:
sign in to Patreon again in the browser.

### Export cookies for another device

A phone or tablet can't share its browser's cookies with `pa`, so export them
from a desktop browser instead:

```bash
pa cookies ~/patreon-cookies.txt
```

The file holds only cookies for patreon.com; the browser's cookies for other
sites are left out. It's created with mode `0600`, readable only by you.

The file is a live Patreon login, so treat it like a password. Move it by USB
or `scp`, not through cloud sync or chat. When the session expires, run the
command again and copy the new file over. `pa` only ever reads the file; it
never writes to it.

## Download

Pass a post or collection URL. A bare URL is short for `pa get URL`.

```bash
pa 'https://www.patreon.com/posts/<post-title>-<post-id>'
```

Always put the URL in quotes: a share link contains `&`, which the shell
otherwise treats as a command separator. Share links, tracking parameters,
and `#fragments` are removed before the download starts.

### Collections

List a collection's entries first. Entry 1 is the oldest, so numbers stay the
same as the creator adds posts. Entries you can't access are marked
`[no access]`.

```bash
pa 'https://www.patreon.com/collection/<collection-id>' -l
```

Then download all of it, or pick entries with `-i` and leave some out with
`-x`:

```bash
pa '<collection URL>' -i 1-5 -j 3    # entries 1 to 5, three at a time
pa '<collection URL>' -i -3-         # the last three
pa '<collection URL>' -x 2-3         # everything except 2 and 3
```

`-i` and `-x` accept `1-5`, `3,7,9`, `2-` (2 to the end), `-3` (third from
last), `-3-` (the last three), and combinations such as `1-3,-2-`.
Downloading a whole collection asks for confirmation; `-y` skips it.

### Common options

| Option | What it does |
|---|---|
| `-o DIR` | Destination. A bare name is a folder under the default root. |
| `-n`, `--dry-run` | Resolve every post and download nothing. |
| `-j N` | Download N posts at once. |
| `--force` | Download complete posts again and replace their files. |
| `--keep-id` | Add ` [<post_id>]` to every file name. |
| `-a zip` | Pack this run's files into one archive afterwards. |
| `--infojson`, `--thumbnails`, `--subs` | Also save these sidecar files. |
| `--agent` | Print JSON Lines events on stdout. |
| `-- ARGS` | Pass the remaining arguments to yt-dlp. |

Run `pa help` for every option, and `pa status` for what's been downloaded.

To save space, pass a yt-dlp format after `--`. Source video is large, and
720p is often five to ten times smaller:

```bash
pa '<URL>' -- -f 'best[height<=720]'
```

## Where files go

The default destination is a `downloads` folder in the current directory, or
the folder set in `PATREON_ARCHIVE_OUTPUT`. A collection's videos sit flat in
a folder named after the collection. A single post goes in its creator's
folder, unless it belongs to a collection; then it goes in that collection's
folder.

```text
downloads/
├── .patreon-archive.db      # what's been downloaded
├── <collection title>/
│   ├── <post title>.mp4
│   └── <post title>.mp4
└── <creator>/
    └── <post title>.mp4
```

Each file is named after its post's title. If two posts share a title, the
second file gets its post ID: `<title> [<post_id>].mp4`.

## Resume and re-download

Re-running a command skips every post that's complete, so an interrupted
collection carries on where it stopped. A post is downloaded again if its last
attempt failed, if you didn't have access then, or if one of its files is
missing from disk. Updating `pa` never downloads anything again by itself.

`pa` remembers each file's location. If you move or rename a downloaded file,
the next run downloads that post again.

To download complete posts again and replace their files, pass `--force`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | No post failed or is indeterminate, and at least one is complete. |
| 1 | At least one post failed. |
| 2 | Nothing is complete, and at least one post is `no_access`. |
| 3 | At least one post is indeterminate: re-run it later. |
| 4 | Setup error, such as missing ffmpeg or cookies. |
| 130 | Interrupted with Ctrl+C. |

## Install on Android with Termux

Android keeps browser cookies where other apps can't read them, so you export
them on a desktop and copy them to the device.

1. Install [Termux](https://termux.dev/) from
   [F-Droid](https://f-droid.org/packages/com.termux/) or
   [GitHub](https://github.com/termux/termux-app/releases), then open it once.
   Don't use the Play Store build; it's abandoned and its packages are broken.
1. On your desktop, export your cookies, as described in
   [Export cookies for another device](#export-cookies-for-another-device).
1. In Termux, install git and run the setup script from a clone of this
   repository:

   ```bash
   pkg install git
   git clone https://github.com/Flarette/patreon-archive
   bash patreon-archive/scripts/termux-setup.sh
   ```

   The script installs Python and ffmpeg, then asks for storage access:
   accept the dialog. It also sets two defaults in `~/.bashrc`:
   - `PATREON_ARCHIVE_COOKIES=~/patreon-cookies.txt`
   - `PATREON_ARCHIVE_OUTPUT=~/storage/movies`, the device's Movies folder.

   If a release lists a `patreon-archive-android.zip`, you can unzip that
   instead of cloning. Run `termux-setup.sh` from inside the unzipped folder.
1. Copy `patreon-cookies.txt` to the device by USB, into `Downloads`. Then move
   it into Termux's home folder, where other apps can't read it:

   ```bash
   mv ~/storage/downloads/patreon-cookies.txt ~/
   chmod 600 ~/patreon-cookies.txt
   ```

1. Check the setup with `pa auth`, then download:

   ```bash
   pa '<post or collection URL>'
   ```

Files land in the Movies folder, where any video app can play them. Termux
keeps the device awake while it's open, so downloads continue with the screen
off.

### Keep long downloads alive on Samsung devices

Samsung's battery manager stops long downloads. Open **Settings > Battery >
Background usage limits** and remove Termux from **Sleeping apps** and **Deep
sleeping apps**. Then open **Settings > Apps > Termux > Battery** and choose
**Unrestricted**.

### SD cards

Termux can write only to its own folder on a removable card. Download to
internal storage, then move the files with a file manager app. On a card
formatted as FAT32, files over 4 GB fail; exFAT works.

## Known limitations

- Before version 0.4, two posts with the same title could be saved over one
  file. Which post owns such a file is best-effort, and a forced re-run may
  write an extra ` [<post_id>]` copy.
- Keep `--temp-dir` outside your download folders.
- Options after `--` replace the matching yt-dlp settings instead of adding to
  them. Use them for format and quality, such as `-f`.

## Development

```bash
git clone https://github.com/Flarette/patreon-archive
cd patreon-archive
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

The tests need ffmpeg and ffprobe on your `PATH`, and they never use the
network.

## License

[MIT](LICENSE)
