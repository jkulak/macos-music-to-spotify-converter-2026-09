"""Play a track for comparison: the Spotify candidate in the Spotify app, the
library file in Music; or look for it on Spotify or YouTube Music.

Both apps take AppleScript commands, so this works from the host without any
API scope; it does not work inside Docker, which has neither audio nor the apps.
Commands are started without waiting, so the review UI never freezes.
"""

from __future__ import annotations

import re
import subprocess
import urllib.parse
from collections.abc import Callable

_SPOTIFY_ID = re.compile(r"^[A-Za-z0-9]{22}$")
_PERSISTENT_ID = re.compile(r"^[0-9A-F]{16}$")

Runner = Callable[[list[str]], object]


def _start(argv: list[str]) -> object:
    return subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Player:
    def __init__(self, run: Runner = _start):
        self.run = run
        self.active: str | None = None  # "Spotify" or "Music", whichever was started last
        self.now_playing = ""

    def _tell(self, app: str, command: str) -> None:
        self.run(["osascript", "-e", f'tell application "{app}" to {command}'])

    def play_spotify(self, track_id: str, label: str) -> None:
        if not _SPOTIFY_ID.match(track_id):
            raise ValueError(f"not a Spotify track ID: {track_id!r}")
        self._switch_to("Spotify")
        self._tell("Spotify", f'play track "spotify:track:{track_id}"')
        self.now_playing = f"Spotify: {label}"

    def play_music(self, persistent_id: str, label: str) -> None:
        if not _PERSISTENT_ID.match(persistent_id):
            raise ValueError(f"not a Music persistent ID: {persistent_id!r}")
        self._switch_to("Music")
        self._tell("Music", f'play (some track of library playlist 1 whose persistent ID is "{persistent_id}")')
        self.now_playing = f"Music: {label}"

    def pause(self) -> None:
        if self.active:
            self._tell(self.active, "pause")
        self.active = None
        self.now_playing = ""

    def _switch_to(self, app: str) -> None:
        # Two songs at once make comparing impossible; pause the other app first.
        if self.active and self.active != app:
            self._tell(self.active, "pause")
        self.active = app

    def open_track(self, track_id: str) -> None:
        if not _SPOTIFY_ID.match(track_id):
            raise ValueError(f"not a Spotify track ID: {track_id!r}")
        self.run(["open", f"spotify:track:{track_id}"])

    def open_search(self, text: str) -> None:
        self.run(["open", "spotify:search:" + urllib.parse.quote(text)])

    def open_youtube(self, text: str) -> None:
        # In the browser: YouTube Music lists songs first, which is what a missing track needs.
        self.run(["open", "https://music.youtube.com/search?" + urllib.parse.urlencode({"q": text})])
