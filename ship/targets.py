"""
The "pure" logic of ship: no network calls here, just deciding WHAT to upload
and WHERE it goes. Keeping this separate makes it easy to read and to test.

Key idea: every published file lives in the R2 bucket under
"<hostname>/<path>/<file>", and the Worker maps URLs to keys the same way:

    ship page.html demo.example.com          -> demo.example.com/index.html
    ship page.html example.com/reports/q3    -> example.com/reports/q3/index.html
    ship ./site/   demo.example.com          -> demo.example.com/<every file in site/>
"""

import mimetypes
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

# A hostname is dot-separated labels of letters, digits and hyphens,
# where no label starts or ends with a hyphen, e.g. "demo.example.com".
HOSTNAME_PATTERN = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$")


@dataclass
class Target:
    """Where a page gets published: a hostname plus an optional path."""

    hostname: str   # "demo.example.com"
    path: str = ""  # "" for the root, or "reports/q3" (no leading/trailing slash)

    @property
    def prefix(self) -> str:
        """The folder in the R2 bucket, e.g. "example.com/reports/q3/"."""
        return f"{self.hostname}/{self.path}/" if self.path else f"{self.hostname}/"

    @property
    def url(self) -> str:
        return f"https://{self.prefix}"


def parse_target(text: str) -> Target:
    """
    Turn what the user typed into a Target.

    Accepts "demo.example.com", "example.com/reports/q3", and is forgiving
    about a pasted "https://" or stray slashes.
    """
    text = re.sub(r"^https?://", "", text.strip())
    hostname, _, path = text.partition("/")
    hostname = hostname.lower()

    if not HOSTNAME_PATTERN.match(hostname):
        raise ValueError(f"'{hostname}' doesn't look like a domain name (e.g. demo.example.com)")

    return Target(hostname=hostname, path=path.strip("/"))


def random_slug(name: str) -> str:
    """
    A readable but unguessable path for pages published without a target:
    "Q3 Report.html" -> "q3-report-8f3a1c".
    """
    words = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "page"
    return f"{words}-{secrets.token_hex(3)}"


def content_type(path: Path) -> str:
    """The MIME type a browser needs to handle the file, e.g. "text/html"."""
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed is None:
        return "application/octet-stream"  # generic "some bytes"
    # Without an explicit charset, browsers may garble accents and emoji.
    if guessed.startswith("text/") or guessed in ("application/javascript", "application/json"):
        return f"{guessed}; charset=utf-8"
    return guessed


# -------------------------------------------------------------- collecting files

# Things like src="chart.png", href="style.css", or url(font.woff2) in HTML/CSS.
LOCAL_REFERENCE = re.compile(r"""(?:src|href)\s*=\s*["']([^"']+)["']|url\(\s*["']?([^"')]+)["']?\s*\)""")

# References that point somewhere else entirely and need no uploading.
EXTERNAL_PREFIXES = ("http:", "https:", "//", "data:", "mailto:", "tel:", "javascript:", "#", "/")


@dataclass
class FilePlan:
    """The result of collect_files: what to upload, plus things worth warning about."""

    files: list[tuple[Path, str]]   # (local file, its path inside the site)
    warnings: list[str]


def collect_files(source: Path) -> FilePlan:
    """
    Decide which local files to upload for `source`.

    * A folder is uploaded as a whole and must contain an index.html.
    * A single HTML file is uploaded as index.html. If it refers to local files
      sitting next to it (images, CSS, ...), those are uploaded too, so the
      page doesn't show up with broken images.
    """
    if source.is_dir():
        return _collect_folder(source)
    return _collect_single_file(source)


def _collect_folder(folder: Path) -> FilePlan:
    if not (folder / "index.html").is_file():
        raise ValueError(f"{folder} has no index.html, so there would be nothing to show at its URL")

    files = []
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder)
        # Skip hidden files and folders: .DS_Store, .git, .env, ...
        if path.is_file() and not any(part.startswith(".") for part in relative.parts):
            files.append((path, relative.as_posix()))
    return FilePlan(files=files, warnings=[])


def _collect_single_file(html_file: Path) -> FilePlan:
    folder = html_file.parent.resolve()
    files = [(html_file, "index.html")]
    warnings = []
    seen = set()

    for match in LOCAL_REFERENCE.finditer(html_file.read_text(errors="ignore")):
        reference = (match.group(1) or match.group(2)).strip()
        if reference.startswith(EXTERNAL_PREFIXES):
            continue

        # "img/chart.png?v=2#top" -> "img/chart.png"
        reference = reference.split("?")[0].split("#")[0]
        local = (folder / reference).resolve()
        if not reference or local in seen or not local.is_file():
            continue
        seen.add(local)

        # Files outside the HTML's folder ("../logo.png") can't be uploaded in
        # a way that keeps the link working, so just flag them.
        if not local.is_relative_to(folder):
            warnings.append(f"'{reference}' is outside the page's folder and won't be uploaded")
            continue

        relative = local.relative_to(folder).as_posix()
        if relative != "index.html":  # the page itself already takes that name
            files.append((local, relative))

    return FilePlan(files=files, warnings=warnings)
