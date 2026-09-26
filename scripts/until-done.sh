#!/bin/sh
# Runs `match` again after every Spotify quota block until matching is finished;
# `match` exits with 3 while searches remain. With "migrate", every run is
# followed by a `sync`, so the playlists fill up day by day.
#
#   scripts/until-done.sh match|migrate
set -u
root=$(cd "$(dirname "$0")/.." && pwd)
mode=${1:-match}
data=${DATA_DIR:-$root/data}
margin=${QUOTA_MARGIN:-120}  # seconds on top of Retry-After, against clock skew

app() {
    docker compose -f "$root/compose.yaml" run --rm -T \
        ${PLAYLIST_NAMES:+-e PLAYLIST_NAMES="$PLAYLIST_NAMES"} app "$@"
}

while true; do
    app match
    status=$?
    if [ "$mode" = migrate ] && { [ "$status" -eq 0 ] || [ "$status" -eq 3 ]; }; then
        app sync || exit $?
    fi
    [ "$status" -eq 3 ] || exit "$status"
    wait=$(( $(cat "$data/retry-after-seconds") + margin ))
    until=$(date -v+"${wait}"S '+%Y-%m-%d %H:%M' 2>/dev/null || date -d "+${wait} seconds" '+%Y-%m-%d %H:%M')
    echo "Waiting ${wait}s for the Spotify quota, until ${until}"
    sleep "$wait"
done
