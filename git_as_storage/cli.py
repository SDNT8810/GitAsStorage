"""Command-line interface. Credentials stay in Git's helper or the environment."""
import argparse
from contextlib import contextmanager
import datetime
import json
import math
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import transfer
from .restore import restore


def remote_details(remote):
    parsed = urllib.parse.urlsplit(remote)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Use a remote URL without embedded credentials, query strings, or fragments")
    if parsed.scheme == "file" and parsed.netloc in ("", "localhost") and parsed.path:
        return None  # Local remotes support offline tests; no cloud privacy claim is made.
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com":
        raise ValueError("Use https://github.com/OWNER/REPO.git (or a local file:// remote for tests)")
    match = re.fullmatch(r"/([A-Za-z0-9_-]+)/([A-Za-z0-9_.-]+)/?", parsed.path)
    if not match:
        raise ValueError("Invalid GitHub repository URL")
    owner, name = match.groups()
    if name.endswith(".git"):
        name = name[:-4]
    if name in ("", ".", ".."):
        raise ValueError("Invalid GitHub repository name")
    return owner, name


def check_private(remote):
    details = remote_details(remote)
    if details is None:
        return
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        try:
            result = transfer.git(["credential", "fill"], data=b"protocol=https\nhost=github.com\n\n")
        except RuntimeError:
            raise ValueError("GitHub API credentials unavailable. Authenticate Git first, or set GH_TOKEN in your environment.") from None
        credentials = dict(line.split("=", 1) for line in result.decode().splitlines() if "=" in line)
        token = credentials.get("password")
    if not token:
        raise ValueError("No GitHub API credential found; never put a token in the remote URL")
    owner, name = details
    request = urllib.request.Request(f"https://api.github.com/repos/{owner}/{name}", headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
        "User-Agent": "GitAsStorage/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            repository = json.load(response)
    except urllib.error.HTTPError as error:
        raise ValueError(f"Cannot verify GitHub repository privacy/access (HTTP {error.code})") from None
    if repository.get("private") is not True:
        raise ValueError("Refusing to upload personal data to a public repository; create a private archive repository")
    if not repository.get("permissions", {}).get("push"):
        raise ValueError("The authenticated account lacks push access to this repository")
    if repository.get("archived") or repository.get("disabled"):
        raise ValueError("Repository is archived or disabled")


@contextmanager
def state_lock(folder):
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "upload.lock").open("a+b") as handle:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("Another uploader or state operation is using this checkpoint") from None
        yield


def repository_name(remote, override):
    details = remote_details(remote)
    name = override or (details[1] if details else "archive-01")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", name):
        raise ValueError("Repository label must contain letters, numbers, dots, underscores, or hyphens")
    return name


def initialize(args):
    source = Path(args.source).resolve(strict=True)
    if not source.is_dir():
        raise ValueError("Source must be a directory")
    folder = Path(args.state_dir).resolve() if args.state_dir else source / ".gitasstorage"
    if folder == source or source.is_relative_to(folder):
        raise ValueError("State directory must not be the source directory or its ancestor")
    if not 1 <= args.chunk_mib < 100 or not 1 <= args.batch_mib < 1500:
        raise ValueError("Use chunk sizes 1–99 MiB and batch sizes 1–1499 MiB")
    if not math.isfinite(args.repo_budget_gib) or args.repo_budget_gib < 0:
        raise ValueError("Repository budget must be nonnegative; 0 disables only the local budget")
    name = repository_name(args.remote, args.name)
    with state_lock(folder):
        if (folder / "state.json").exists():
            raise ValueError("An archive checkpoint already exists here; use upload to resume")
        check_private(args.remote)
        transfer.git(["var", "GIT_AUTHOR_IDENT"])
        if transfer.git(["ls-remote", args.remote]).strip():
            raise ValueError("Use a new, empty repository without a README or initial commit")
        entries = transfer.snapshot(source, folder)
        if not entries:
            raise ValueError("No source files found")
        state = {"format": 2, "backup_id": str(uuid.uuid4()), "chunk_bytes": args.chunk_mib * 1024**2,
                 "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                 "complete": False, "entries": entries, "uploaded_bytes": 0, "uploaded_chunks": 0,
                 "repositories": {name: {"url": args.remote}}, "active_repository": name,
                 "_last_commit": None, "_source": str(source), "_batch_mib": args.batch_mib,
                 "_repo_budget_gib": args.repo_budget_gib}
        transfer.save(folder / "state.json", state)
    print(f"Inventoried {len(entries)} files ({sum(e['size'] for e in entries) / 1024**3:.2f} GiB).")
    print("State directory:", folder)
    print("No media uploaded. Resume with: python gitasstorage.py upload --state-dir", str(folder))


def load_state(folder):
    path = Path(folder).resolve() / "state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("format") != 2 or not state.get("_source"):
        raise ValueError("This is not a GitAsStorage local checkpoint")
    return path, state


def upload(args):
    path, state = load_state(args.state_dir)
    remote = state["repositories"][state["active_repository"]]["url"]
    check_private(remote)
    parameters = ["--source", state["_source"], "--state-dir", str(path.parent),
                  "--chunk-mib", str(state["chunk_bytes"] // 1024**2),
                  "--batch-mib", str(state["_batch_mib"]),
                  "--repo-budget-gib", str(state["_repo_budget_gib"])]
    if args.work_root:
        parameters += ["--work-root", str(Path(args.work_root).resolve(strict=True))]
    if args.max_batches:
        if args.max_batches < 1:
            raise ValueError("--max-batches must be positive")
        parameters += ["--max-batches", str(args.max_batches)]
    transfer.main(parameters)


def continue_archive(args):
    path, _ = load_state(args.state_dir)
    with state_lock(path.parent):
        state = json.loads(path.read_text(encoding="utf-8"))
        base = state["active_repository"]
        previous = state["repositories"][base]
        name = repository_name(args.remote, args.name)
        if transfer.remote_tip(previous["url"]) != state["_last_commit"]:
            raise ValueError("Remote checkpoint differs from local state; resume upload to synchronize it first")
        check_private(args.remote)
        if transfer.git(["ls-remote", args.remote]).strip():
            raise ValueError("Continuation requires a new empty repository")
        continued = transfer.prepare_continuation(state, base, previous["url"], name, args.remote)
        backup = path.parent / ("checkpoint-before-" + name + ".json")
        with backup.open("x", encoding="utf-8") as output:
            json.dump(state, output, ensure_ascii=False, indent=2)
        transfer.save(path, continued)
    print("Continuation prepared:", name)
    print("Existing uploaded chunks remain in their original repositories. Run upload to continue.")


def status(args):
    _, state = load_state(args.state_dir)
    total = sum(e["size"] for e in state["entries"])
    count = state.get("uploaded_bytes", 0)
    print(f"Uploaded checkpoint: {count / 1024**3:.2f}/{total / 1024**3:.2f} GiB ({100 * count / total:.2f}%)")
    print("Upload complete:", state["complete"])
    print("Active repository:", state["active_repository"])
    print("Restore requires:", ", ".join(state["repositories"]))
    print("Last verified remote commit:", state.get("_last_commit"))
    print("A verified commit is an upload checkpoint; test a full restore before deleting originals.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="GitAsStorage — experimental, resumable Git archives")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Inventory a source directory and verify an empty private remote")
    init.add_argument("source")
    init.add_argument("--remote", required=True)
    init.add_argument("--name", help="Repository label, defaults to the repository name")
    init.add_argument("--state-dir")
    init.add_argument("--chunk-mib", type=int, default=40)
    init.add_argument("--batch-mib", type=int, default=512)
    init.add_argument("--repo-budget-gib", type=float, default=8)
    init.set_defaults(action=initialize)
    send = commands.add_parser("upload", help="Resume uploading with live Git progress")
    send.add_argument("--state-dir", required=True)
    send.add_argument("--work-root", help="Temporary disk location with space for a batch")
    send.add_argument("--max-batches", type=int)
    send.set_defaults(action=upload)
    following = commands.add_parser("continue", help="Prepare continuation in another private empty repository")
    following.add_argument("--state-dir", required=True)
    following.add_argument("--remote", required=True)
    following.add_argument("--name")
    following.set_defaults(action=continue_archive)
    show = commands.add_parser("status", help="Show the last uploaded checkpoint")
    show.add_argument("--state-dir", required=True)
    show.set_defaults(action=status)
    recover = commands.add_parser("restore", help="Restore from ordinary clones, checking SHA-256 for every chunk")
    recover.add_argument("repository", help="Clone containing the latest complete backup-manifest.json")
    recover.add_argument("destination")
    recover.add_argument("--part", action="append", default=[], metavar="NAME=FOLDER")
    recover.set_defaults(action=lambda a: restore(a.repository, a.destination, a.part))
    args = parser.parse_args(argv)
    try:
        args.action(args)
    except KeyboardInterrupt:
        print("Stopped. Run the same upload command to resume the remote checkpoint.", file=sys.stderr)
        return 130
    except (ValueError, RuntimeError, OSError, KeyError, TypeError, urllib.error.URLError) as error:
        print("Stopped:", error, file=sys.stderr)
        return 1
    return 0
