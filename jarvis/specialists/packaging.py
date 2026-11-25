"""Getting agent plugins in and out: zip files, GitHub URLs, local folders.

Everything an import brings in is untrusted. Extraction refuses absolute paths,
`..`, symlinks and oversized archives, and only keeps the file types a plugin
uses. Python files are listed for review and never imported here.
"""

import io
import re
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath

MAX_BYTES = 20 * 1024 * 1024
MAX_FILES = 500
ALLOWED_SUFFIXES = {".toml", ".md", ".txt", ".py", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp",
                    ".json", ".csv", ".j2", ".tmpl"}
ALLOWED_NAMES = {"LICENSE", "LICENCE", "NOTICE", "COPYING"}
SKIP_DIRS = {"__pycache__", ".git", ".github", ".venv", "node_modules", ".idea", ".vscode"}
GITHUB_RE = re.compile(
    r"^https://(?:www\.)?github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9._-]+?)(?:\.git)?"
    r"(?:/tree/([^?#]+))?/?(?:[?#].*)?$")


class ImportProblem(ValueError):
    """An archive or source that cannot be imported; the message says why."""


def _safe_parts(name):
    """Split an archive member name into safe parts, or raise."""
    if "\\" in name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise ImportProblem(f"'{name}' is an absolute or Windows path; refusing the archive")
    parts = PurePosixPath(name).parts
    if any(p in ("..", "") for p in parts):
        raise ImportProblem(f"'{name}' points outside the folder; refusing the archive")
    return parts


def _wanted(parts):
    """(keep, reason) for a member path relative to the plugin root."""
    if any(p in SKIP_DIRS for p in parts[:-1]) or parts[-1] in (".DS_Store",):
        return False, ""
    name = parts[-1]
    suffix = PurePosixPath(name).suffix.lower()
    if suffix in ALLOWED_SUFFIXES or name in ALLOWED_NAMES or name in (".gitignore",):
        return True, ""
    return False, f"{'/'.join(parts)} (file type not used by agents)"


def read_zip(data, subdir=""):
    """Validate a zip and return (files {relpath: bytes}, skipped [str]) for the plugin root.

    The root is the folder holding agent.toml: `subdir` (inside the archive's single
    top folder, as GitHub zips have) or the shallowest agent.toml.
    """
    if len(data) > MAX_BYTES:
        raise ImportProblem(f"the archive is {len(data) / 1e6:.1f} MB; the limit is {MAX_BYTES // 1_000_000} MB")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ImportProblem("this is not a valid .zip file") from None
    members = [m for m in archive.infolist() if not m.is_dir()]
    if len(members) > MAX_FILES * 4:
        raise ImportProblem(f"the archive has {len(members)} files; the limit is {MAX_FILES}")
    total = 0
    entries = []
    for member in members:
        parts = _safe_parts(member.filename)
        mode = member.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise ImportProblem(f"'{member.filename}' is a symbolic link; links are not allowed in agents")
        total += member.file_size
        if total > MAX_BYTES:
            raise ImportProblem(f"the archive unpacks to more than {MAX_BYTES // 1_000_000} MB")
        entries.append((parts, member))

    roots = sorted((parts[:-1] for parts, _ in entries if parts[-1] == "agent.toml"), key=len)
    if subdir:
        wanted = tuple(p for p in subdir.strip("/").split("/") if p)
        roots = [r for r in roots if r[-len(wanted):] == wanted and len(r) <= len(wanted) + 1]
        if not roots:
            raise ImportProblem(f"no agent.toml in the folder '{subdir}' of this archive")
    if not roots:
        raise ImportProblem("no agent.toml found: this archive is not a Jarvis agent")
    if len(roots) > 1 and len(roots[0]) == len(roots[1]):
        names = ", ".join("/".join(r) or "(top)" for r in roots[:4])
        raise ImportProblem(f"the archive holds several agents ({names}); import one folder at a time")
    root = roots[0]

    files, skipped = {}, []
    for parts, member in entries:
        if parts[:len(root)] != root:
            continue
        rel = parts[len(root):]
        keep, reason = _wanted(rel)
        if not keep:
            if reason:
                skipped.append(reason)
            continue
        files["/".join(rel)] = archive.read(member)
    if len(files) > MAX_FILES:
        raise ImportProblem(f"the agent has {len(files)} files; the limit is {MAX_FILES}")
    return files, skipped


def read_folder(folder):
    """Same as read_zip, for a local folder (symlinks refused)."""
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        raise ImportProblem(f"'{folder}' is not a folder")
    if not (folder / "agent.toml").is_file():
        raise ImportProblem(f"'{folder}' has no agent.toml")
    files, skipped, total = {}, [], 0
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder).parts
        if path.is_symlink():
            raise ImportProblem(f"'{'/'.join(rel)}' is a symbolic link; links are not allowed in agents")
        if not path.is_file():
            continue
        keep, reason = _wanted(rel)
        if not keep:
            if reason:
                skipped.append(reason)
            continue
        total += path.stat().st_size
        if total > MAX_BYTES or len(files) >= MAX_FILES:
            raise ImportProblem(f"the folder is larger than {MAX_BYTES // 1_000_000} MB or {MAX_FILES} files")
        files["/".join(rel)] = path.read_bytes()
    return files, skipped


def github_zip_urls(url):
    """codeload.github.com zip URLs to try for a GitHub repo or tree/<ref>/<subdir> URL, plus the subdir."""
    match = GITHUB_RE.match(url.strip())
    if not match:
        return None, ""
    owner, repo, tree = match.groups()
    base = f"https://codeload.github.com/{owner}/{repo}/zip"
    if not tree:
        return [f"{base}/refs/heads/main", f"{base}/refs/heads/master", f"{base}/HEAD"], ""
    ref, _, subdir = tree.partition("/")
    return [f"{base}/refs/heads/{ref}", f"{base}/refs/tags/{ref}", f"{base}/{ref}"], subdir


def download(url, client=None):
    """Fetch a plugin zip from a GitHub URL or a direct https .zip link. Returns (bytes, subdir)."""
    import httpx

    url = (url or "").strip()
    candidates, subdir = github_zip_urls(url)
    if candidates is None:
        if not re.match(r"^https://[^\s/]+/\S+\.zip(\?\S*)?$", url, re.I):
            raise ImportProblem("use a GitHub link (github.com/owner/repo or …/tree/branch/folder) "
                                "or an https link to a .zip file")
        candidates = [url]
    own = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=True,
                                    headers={"User-Agent": "Jarvis-agent-import"})
    try:
        last = ""
        for candidate in candidates:
            try:
                with client.stream("GET", candidate) as response:
                    if response.status_code == 404:
                        last = "not found (check the owner, repository and branch name; private repos are not supported)"
                        continue
                    if response.status_code >= 400:
                        last = f"the server answered HTTP {response.status_code}"
                        continue
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise ImportProblem(f"the download is larger than {MAX_BYTES // 1_000_000} MB")
                        chunks.append(chunk)
                    return b"".join(chunks), subdir
            except httpx.HTTPError as error:
                last = f"could not download ({error.__class__.__name__}: {error})"
        raise ImportProblem(f"{url}: {last}")
    finally:
        if own:
            client.close()


def check_svg(data):
    """Refuse SVG icons that carry scripts or external references."""
    text = data.decode("utf-8", "replace").lower()
    if re.search(r"<script|<foreignobject|\son\w+\s*=|javascript:|href\s*=\s*[\"']?(https?:|//|data:text)", text):
        raise ImportProblem("icon.svg contains scripts or external links; use a plain SVG")


def write_files(files, destination):
    """Write {relpath: bytes} into a fresh folder."""
    destination = Path(destination)
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    root = destination.resolve()
    for rel, data in files.items():
        path = (destination / rel).resolve()
        if root not in path.parents:
            raise ImportProblem(f"'{rel}' points outside the folder")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def zip_folder(folder, arcroot, extra=None):
    """A zip (bytes) of folder under `arcroot/`, skipping caches; extra = {arcpath: bytes}."""
    buffer = io.BytesIO()
    folder = Path(folder)
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.rglob("*")):
            rel = path.relative_to(folder).parts
            if path.is_symlink() or not path.is_file() or any(p in SKIP_DIRS for p in rel):
                continue
            archive.write(path, "/".join((arcroot, *rel)))
        for name, data in (extra or {}).items():
            archive.writestr(f"{arcroot}/{name}", data)
    return buffer.getvalue()
