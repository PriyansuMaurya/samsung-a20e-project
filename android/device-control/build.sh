#!/usr/bin/env bash
# Manual Android build: javac, d8, aapt2, zipalign, apksigner. No Gradle, so
# nothing outside the installed SDK is downloaded.
set -euo pipefail
cd "$(dirname "$0")"

SDK="${ANDROID_HOME:-$(cd ../../.android-sdk/sdk && pwd)}"
BT="$SDK/build-tools/30.0.3"
PLATFORM="$SDK/platforms/android-30/android.jar"
OUT=build

[ -x "$BT/aapt2.exe" ] || [ -x "$BT/aapt2" ] || { echo "aapt2 missing in $BT" >&2; exit 1; }
[ -f "$PLATFORM" ] || { echo "android.jar missing in $SDK/platforms" >&2; exit 1; }
command -v javac >/dev/null || { echo "javac missing" >&2; exit 1; }

AAPT2="$BT/aapt2.exe"; [ -x "$AAPT2" ] || AAPT2="$BT/aapt2"
ZIPALIGN="$BT/zipalign.exe"; [ -x "$ZIPALIGN" ] || ZIPALIGN="$BT/zipalign"
# The .bat wrappers do not run under Git Bash, so the jars are called directly.
D8_JAR="$BT/lib/d8.jar"
APKSIGNER_JAR="$BT/lib/apksigner.jar"
[ -f "$D8_JAR" ] || { echo "d8.jar missing" >&2; exit 1; }
[ -f "$APKSIGNER_JAR" ] || { echo "apksigner.jar missing" >&2; exit 1; }

rm -rf "$OUT"
mkdir -p "$OUT/classes" "$OUT/dex" "$OUT/apk"

echo "== javac"
javac -source 8 -target 8 -bootclasspath "$PLATFORM" -encoding UTF-8 \
  -d "$OUT/classes" $(find src -name '*.java' | sort)

echo "== d8"
java -cp "$D8_JAR" com.android.tools.r8.D8 --lib "$PLATFORM" --min-api 30 \
  --output "$OUT/dex" $(find "$OUT/classes" -name '*.class' | sort)

echo "== aapt2 link"
"$AAPT2" link -o "$OUT/apk/unsigned.apk" \
  --manifest AndroidManifest.xml \
  -I "$PLATFORM" \
  --min-sdk-version 30 --target-sdk-version 30 \
  --version-code 1 --version-name 0.1.0

echo "== package dex"
python - "$OUT/apk/unsigned.apk" "$OUT/dex/classes.dex" <<'PY'
import sys, zipfile
apk, dex = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(apk, 'a', zipfile.ZIP_DEFLATED) as archive:
    archive.write(dex, 'classes.dex')
print('dex added:', dex)
PY

echo "== zipalign"
"$ZIPALIGN" -f -p 4 "$OUT/apk/unsigned.apk" "$OUT/apk/aligned.apk"

if [ ! -f galaxy.keystore ]; then
  echo "== keystore"
  keytool -genkeypair -v -keystore galaxy.keystore -storepass android \
    -keypass android -alias galaxy -keyalg RSA -keysize 2048 -validity 10000 \
    -dname "CN=Galaxy Device Control, OU=Personal, O=Maurya, L=Home, C=IE" >/dev/null
fi

echo "== sign"
java -jar "$APKSIGNER_JAR" sign --ks galaxy.keystore --ks-pass pass:android \
  --key-pass pass:android --min-sdk-version 30 \
  --out "$OUT/galaxy-device.apk" "$OUT/apk/aligned.apk"

echo "== verify"
java -jar "$APKSIGNER_JAR" verify --print-certs "$OUT/galaxy-device.apk" | head -4
ls -lh "$OUT/galaxy-device.apk"
