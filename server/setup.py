from setuptools import setup

APP_NAME = "Boox Display Digitizer"
VERSION = "0.2.1"

setup(
    app=["server.py"],
    name=APP_NAME,
    version=VERSION,
    setup_requires=["py2app"],
    options={
        "py2app": {
            "argv_emulation": False,
            "emulate_shell_environment": True,
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
