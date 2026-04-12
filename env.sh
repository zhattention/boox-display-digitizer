#!/bin/bash
# Source this file before building the Android app:  source env.sh
#
# The real toolchain lives on an external volume whose path contains a space
# ("/Volumes/Macintosh HD/..."), which breaks sdkmanager's shell script.
# We expose it via a space-free symlink in /tmp. /tmp survives until reboot;
# after a reboot, sourcing this file recreates the symlink automatically.

REAL="/Volumes/Macintosh HD/Users/Shared/boox-toolchain"
LINK="/tmp/boox-toolchain"

if [ ! -L "$LINK" ] || [ "$(readlink "$LINK")" != "$REAL" ]; then
    ln -sfn "$REAL" "$LINK"
fi

export JAVA_HOME="$LINK/jdk-17/Contents/Home"
export ANDROID_HOME="$LINK/android-sdk"
export ANDROID_SDK_ROOT="$ANDROID_HOME"
export GRADLE_USER_HOME="$LINK/gradle-caches"
export GRADLE_HOME="$LINK/gradle-8.5"
export PATH="$JAVA_HOME/bin:$GRADLE_HOME/bin:$ANDROID_HOME/cmdline-tools/latest/bin:$ANDROID_HOME/platform-tools:$PATH"

echo "JAVA:    $(java -version 2>&1 | head -1)"
echo "SDK:     $ANDROID_HOME"
echo "GRADLE:  $GRADLE_USER_HOME"
