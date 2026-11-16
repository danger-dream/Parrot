"""Built-in Hub identity, independent of runtime configuration."""

HUB_VERSION = "2.32.1"
HUB_PLATFORM = "linux/amd64"
BUILD_CL = "1021296516"
USER_AGENT = (
    f"antigravity/hub/{HUB_VERSION} "
    f"(aidev_client; os_type=linux; arch=amd64; cl={BUILD_CL})"
)
LEGACY_USER_AGENTS = (
    "antigravity/hub/2.9.1 darwin/arm64",
    "antigravity/hub/2.22.0 (aidev_client; os_type=linux; arch=amd64; cl=995734886)",
)
