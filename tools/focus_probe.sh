#!/usr/bin/env bash
# Focus/IME disturbance probe (docs/experiments.md E1/E2).
# The "user" types into Settings search on display 0 while the "agent" launches, taps and
# swipes on virtual display $1. After each step we sample who owns top focus and whether
# the user's keyboard is still up, then check whether the user's keystrokes reached them.
#
#   tools/focus_probe.sh <agent_display_id>
set -uo pipefail
export MSYS_NO_PATHCONV=1

VD="${1:?usage: focus_probe.sh <agent_display_id>}"
ADB="${ADB:-$(command -v adb || echo "${ANDROID_SDK_ROOT:?set ANDROID_SDK_ROOT or ADB}/platform-tools/adb")}"
adb() { "$ADB" ${ANDROID_SERIAL:+-s "$ANDROID_SERIAL"} "$@"; }

top_focus() { adb shell dumpsys window | grep -m1 -o 'mTopFocusedDisplayId=[0-9-]*' | cut -d= -f2; }
ime_shown() { adb shell dumpsys input_method | grep -m1 -o 'mInputShown=[a-z]*' | cut -d= -f2; }
user_resumed() {
    adb shell dumpsys activity activities | grep -m1 -E 'topResumedActivity=.*settings' \
        | grep -o '[a-z.]*/[A-Za-z.]*' | head -1
}
# Text and center of the user's search box. Plain `uiautomator dump` only dumps the top focused
# display, which is exactly what the agent may have stolen, so dump all windows and pick the
# user's app by package.
USER_PKG="com.google.android.settings.intelligence"
user_field() {
    adb shell uiautomator dump --windows /sdcard/fenshen_probe.xml >/dev/null 2>&1
    adb shell cat /sdcard/fenshen_probe.xml | tr '>' '\n' \
        | grep 'class="android.widget.EditText"' | grep -m1 "package=\"$USER_PKG\""
}
field_text() { user_field | grep -o ' text="[^"]*"' | cut -d'"' -f2; }
field_center() {
    user_field | grep -o 'bounds="\[[0-9]*,[0-9]*\]\[[0-9]*,[0-9]*\]"' \
        | sed -E 's/bounds="\[([0-9]+),([0-9]+)\]\[([0-9]+),([0-9]+)\]"/\1 \2 \3 \4/' \
        | awk '{print int(($1+$3)/2), int(($2+$4)/2)}'
}

ROWS=()
sample() { ROWS+=("$(printf '%-44s | top focus=%-3s | user IME shown=%-5s' "$1" "$(top_focus)" "$(ime_shown)")"); }

# --- clean start ---
adb shell am force-stop com.google.android.deskclock
adb shell am force-stop com.google.android.settings.intelligence
adb shell am force-stop com.android.settings
adb shell input -d 0 keyevent HOME; sleep 1

# --- user: open Settings search on display 0 and start typing ---
adb shell am start -W -n com.android.settings/.Settings >/dev/null
sleep 1.5
adb shell input -d 0 tap 540 250          # homepage search bar -> opens SearchActivity with keyboard
sleep 2
adb shell input -d 0 text "wifi"; sleep 1
sample "baseline: user typing on display 0"

# --- agent: launch an app on the virtual display ---
adb shell am start -W --display "$VD" -n com.google.android.deskclock/com.android.deskclock.DeskClock >/dev/null
sleep 2
sample "agent: am start --display $VD"

# --- agent: tap on the virtual display ---
adb shell input -d "$VD" tap 360 640; sleep 1.5
sample "agent: input -d $VD tap"

# --- user keeps typing on a hardware keyboard (no display given = focused display) ---
adb shell input text "XYZ"; sleep 1
TEXT_AFTER="$(field_text)"

# --- agent: swipe on the virtual display ---
adb shell input -d "$VD" swipe 360 900 360 400 200; sleep 1.5
sample "agent: input -d $VD swipe"

# --- user taps their field once more: does the keyboard come back? ---
read -r CX CY <<<"$(field_center)"
if [ -n "${CX:-}" ]; then adb shell input -d 0 tap "$CX" "$CY"; sleep 1.5; fi
sample "user: taps own field ($CX,$CY) on display 0"

echo "agent display: $VD    user foreground: $(user_resumed)"
printf '%s\n' "${ROWS[@]}"
echo "user's search box after typing 'wifi' then 'XYZ': \"$TEXT_AFTER\"   (expected \"wifiXYZ\" if keystrokes reached the user)"
