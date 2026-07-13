"""
OpenAver 版本資訊
"""

__version__ = "0.10.11"
VERSION = __version__
UPSTREAM_BASE_VERSION = __version__
CUSTOM_BUILD = "openaver-cn-local-20260713-download3"
DISPLAY_VERSION = f"{__version__}+{CUSTOM_BUILD}"

# 版本資訊
VERSION_INFO = {
    "version": __version__,
    "display_version": DISPLAY_VERSION,
    "upstream_base_version": UPSTREAM_BASE_VERSION,
    "custom_build": CUSTOM_BUILD,
    "update_protected": True,
    "name": "OpenAver",
    "description": "Modern JAV metadata manager",
    "author": "peace",
    "license": "MIT",
}
