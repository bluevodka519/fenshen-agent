#!/usr/bin/env bash
# Builds helper/build/fenshen-helper.jar (dex inside) and optionally pushes it to the device.
#   ./helper/build.sh          build only
#   ./helper/build.sh push     build + adb push to /data/local/tmp
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# Needs an Android SDK (platforms/<API>, build-tools) and a JDK 11+ (Android Studio ships one in jbr/).
ANDROID_SDK="${ANDROID_SDK_ROOT:-${ANDROID_HOME:-}}"
: "${ANDROID_SDK:?set ANDROID_SDK_ROOT to your Android SDK}"
: "${JAVA_HOME:?set JAVA_HOME to a JDK 11+, e.g. <Android Studio>/jbr}"
if command -v cygpath >/dev/null; then  # Git Bash on Windows
    ANDROID_SDK="$(cygpath -u "$ANDROID_SDK")"; JAVA_HOME="$(cygpath -u "$JAVA_HOME")"
fi
API="${FENSHEN_COMPILE_API:-android-37.0}"
BUILD_TOOLS="${FENSHEN_BUILD_TOOLS:-36.0.0}"
ANDROID_JAR="$ANDROID_SDK/platforms/$API/android.jar"
EXE=""; [ -f "$ANDROID_SDK/platform-tools/adb.exe" ] && EXE=".exe"
ADB="${ADB:-$ANDROID_SDK/platform-tools/adb$EXE}"
D8="$ANDROID_SDK/build-tools/$BUILD_TOOLS/d8"; [ -f "$D8.bat" ] && D8="$D8.bat"

OUT="$HERE/build"
rm -rf "$OUT" && mkdir -p "$OUT/classes"

"$JAVA_HOME/bin/javac" --release 11 -nowarn -encoding UTF-8 \
    -cp "$ANDROID_JAR" -d "$OUT/classes" \
    $(find "$HERE/src" -name '*.java')

# d8 needs java on PATH
PATH="$JAVA_HOME/bin:$PATH" "$D8" \
    --min-api 30 --lib "$ANDROID_JAR" --output "$OUT/fenshen-helper.jar" \
    $(find "$OUT/classes" -name '*.class')

echo "built $OUT/fenshen-helper.jar"

if [ "${1:-}" = "push" ]; then
    LOCAL_JAR="$OUT/fenshen-helper.jar"
    command -v cygpath >/dev/null && LOCAL_JAR="$(cygpath -w "$LOCAL_JAR")"
    MSYS_NO_PATHCONV=1 "$ADB" ${ANDROID_SERIAL:+-s "$ANDROID_SERIAL"} push "$LOCAL_JAR" /data/local/tmp/fenshen-helper.jar
fi
