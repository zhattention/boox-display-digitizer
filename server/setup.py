import glob
import sys
from setuptools import setup

APP_NAME = "Boox Display Digitizer"
VERSION = "0.2.1"

# Find libffi — py2app doesn't bundle it automatically but _ctypes needs it.
def _find_libffi():
    prefix = sys.base_prefix or sys.prefix
    candidates = glob.glob(f"{prefix}/lib/libffi*.dylib")
    if not candidates:
        candidates = glob.glob("/usr/local/lib/libffi*.dylib")
    if not candidates:
        candidates = glob.glob("/opt/homebrew/lib/libffi*.dylib")
    return candidates

setup(
    app=["server.py"],
    name=APP_NAME,
    version=VERSION,
    setup_requires=["py2app"],
    options={
        "py2app": {
            "argv_emulation": False,
            "emulate_shell_environment": True,
            "frameworks": _find_libffi(),
            "packages": ["rumps", "mss", "websockets", "PIL", "Quartz", "objc"],
            "plist": {
                "LSUIElement": True,
                "CFBundleIdentifier": "com.boox.display-digitizer",
                "CFBundleName": APP_NAME,
                "CFBundleShortVersionString": VERSION,
                "NSHighResolutionCapable": True,
                "LSMinimumSystemVersion": "11.0",
                "NSScreenCaptureUsageDescription":
                    "Boox Display Digitizer needs screen capture to mirror your display to the Boox tablet.",
                "NSAppleEventsUsageDescription":
                    "Boox Display Digitizer uses AppleScript to show connection approval dialogs.",
            },
        },
    },
)
