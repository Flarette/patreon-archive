#!/data/data/com.termux/files/usr/bin/bash
# One-shot Termux setup for patreon-archive on Android.
#
# Install Termux from F-Droid or GitHub -- NOT the Play Store, that build is
# abandoned and its packages are broken.
#
#   curl -fsSL <this file> | bash
# or copy it over and: bash termux-setup.sh

set -euo pipefail

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mwarning: %s\033[0m\n' "$*" >&2; }

if [ ! -d /data/data/com.termux ]; then
    echo "This script is for Termux on Android." >&2
    exit 1
fi

say "Updating packages"
pkg update -y && pkg upgrade -y

say "Installing python, ffmpeg, git"
pkg install -y python ffmpeg git

say "Granting shared-storage access"
# Creates ~/storage/{shared,downloads,movies,external-1,...}. Android shows a
# permission dialog; accept it. external-1 is the SD card, but only Termux's own
# sandbox dir on it -- writing elsewhere on the card needs a file manager (SAF).
termux-setup-storage || warn "termux-setup-storage failed; accept the permission dialog and re-run"
sleep 2

say "Installing patreon-archive"
# curl_cffi is deliberately not installed: it needs a native build that usually
# fails here, and only the Vimeo fallback uses it. Native Patreon HLS is fine.
if [ -f "$(dirname "$0")/../pyproject.toml" ]; then
    pip install "$(dirname "$0")/.."
else
    pip install patreon-archive
fi

say "Checking ffmpeg"
ffmpeg -version | head -1

DEST="$HOME/storage/movies"
mkdir -p "$DEST"

cat <<EOF

$(printf '\033[1mSetup done.\033[0m')

Cookies cannot be read from Android browsers (app-private storage, needs root),
so export them on your desktop and copy the file here:

    # on the Mac
    patreon-archive cookies ~/patreon-cookies.txt
    scp ~/patreon-cookies.txt <tablet>:~/

Then, on the tablet:

    patreon-archive get --cookies-file ~/patreon-cookies.txt \\
        -o "$DEST" '<patreon url>'

Long downloads:
    termux-wake-lock          # before starting
    termux-wake-unlock        # after

$(printf '\033[33mSamsung devices:\033[0m') exclude Termux from battery optimization, or
long downloads get killed. Settings -> Battery -> Background usage limits ->
remove Termux from "Deep sleeping"/"Sleeping apps".

SD card: files land on internal storage. Move them with a file manager app --
Termux can only write to its own sandbox dir on the card. If the card is FAT32,
files over 4 GB will fail; exFAT is fine.
EOF
