"""App version — bump when releasing so the panel can detect updates."""

__version__ = "9.0.4"

# GitHub repo used for update checks
GITHUB_REPO = "Mrclocks/PGClockBot"
GITHUB_VERSION_URL = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/VERSION"
GITHUB_RELEASE_NOTES_URL = (
    f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/app/services/release_notes.py"
)
GITHUB_REPO_URL = f"https://github.com/{GITHUB_REPO}"
GITHUB_RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
GITHUB_TAGS_API = f"https://api.github.com/repos/{GITHUB_REPO}/tags"
