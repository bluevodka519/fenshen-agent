"""Tells the user about the agent through an ordinary phone notification (README §5).

No full-screen UI and no foreground switch: one notification, updated in place (same tag), that the
user reads when they want to.

Posted with `cmd notification post` as the shell user, so it shows up as "Shell". A tap action is
not possible this way: with `-c activity …` the command builds the PendingIntent as package
"android", which shell may not do ("Permission Denial: getIntentSender() … not allowed to send as
package android", API 37). The handoff therefore tells the user which app to open: opening it from
their own launcher brings the agent's task, half-done state included, onto their screen (E4). A
phone-side app would post under its own name and attach that intent to the notification itself.
"""

from __future__ import annotations

from .adb import Adb

TAG = "fenshen"


def _q(s: str) -> str:
    """Quote one argument for the device shell (adb shell joins arguments with spaces)."""
    return "'" + s.replace("'", "'\\''") + "'"


class Notifier:
    def __init__(self, adb: Adb):
        self.adb = adb

    def post(self, title: str, text: str) -> None:
        self.adb.shell("cmd", "notification", "post", "-S", "bigtext", "-t", _q(title), TAG, _q(text), check=False)
