"""Bounded-disk, resumable Git transfers. No media or account-specific settings."""
import argparse
import copy
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")

CHUNK = 40 * 1024 * 1024
BATCH = 512 * 1024 * 1024
PUSH_INTERVAL = 11
POLL_INTERVAL = 1
GIT_OPTIONS = ["-c", "credential.interactive=never",
               "-c", "core.compression=0", "-c", "pack.compression=0",
               "-c", "pack.window=0", "-c", "gc.auto=0", "-c", "maintenance.auto=false"]
if os.name == "nt":
    GIT_OPTIONS += ["-c", "http.sslBackend=openssl"]
ENV = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")


def log(message):
    print(datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), message, flush=True)


def git(args, cwd=None, data=None):
    result = subprocess.run(["git", *GIT_OPTIONS, *args], cwd=cwd, input=data,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ENV)
    if result.returncode:
        raise RuntimeError("git " + args[0] + " failed:\n" + result.stderr.decode("utf-8", "replace"))
    return result.stdout


def save(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def backup_readme(state):
    repositories = state.get("repositories", {})
    lines = ["# GitAsStorage archive", "", "Keep every repository listed below:", ""]
    for name, details in repositories.items():
        lines.append(f"- `{name}`: {details['url']}")
    active = state.get("active_repository", "archive")
    lines += ["", f"The latest combined manifest and restore program are in `{active}`.",
              "Only `complete: true` in that manifest means all original bytes were uploaded.",
              "That flag does not mean a full download-and-restore test has been performed.",
              "", "## Restore", "", "Clone all repositories into sibling folders, preserving bytes:", "", "```sh"]
    for name, details in repositories.items():
        lines.append(f"git clone -c core.autocrlf=false --single-branch {details['url']} {name}")
    lines += [f"python {active}/restore.py /path/to/new-restored-folder", "```", "",
              "Choose a new output folder outside all clones. Clone folders should use the names above.",
              "For different locations, repeat `--part NAME=FOLDER` on the restore command.",
              "The restore program checks every SHA-256 checksum and size, joins split files,",
              "and refuses to overwrite existing files. Run it only when the combined manifest is complete.",
              "A normal clone plus restored files can need more than three times the original data size.",
              "", "Original file bytes, embedded metadata, names, and directory structure are preserved.",
              "Filesystem timestamps, permissions, hard-link relationships, and empty directories are not.",
              "Private repositories are access-controlled, not end-to-end encrypted.",
              "Keep independent backups. Hosting limits and terms still apply.", ""]
    return "\n".join(lines).encode("utf-8")


def public_manifest(state):
    return {k: v for k, v in state.items() if not k.startswith("_")}


def prepare_continuation(state, base_name, base_remote, next_name, next_remote):
    if state["complete"] or not state.get("_last_commit"):
        raise ValueError("Continuation requires an incomplete, verified checkpoint")
    if base_name == next_name or base_remote == next_remote:
        raise ValueError("Continuation must use a different repository")
    if state.get("active_repository", base_name) != base_name:
        raise ValueError("Base name differs from the current active repository")
    result = copy.deepcopy(state)
    result["format"] = 2
    repositories = result.setdefault("repositories", {})
    if next_name in repositories:
        raise ValueError("Continuation repository is already registered")
    repositories[base_name] = {"url": base_remote, "checkpoint": state["_last_commit"]}
    repositories[next_name] = {"url": next_remote}
    for entry in result["entries"]:
        for chunk in entry["chunks"]:
            chunk.setdefault("repository", base_name)
    result["active_repository"] = next_name
    result["_last_commit"] = None
    return result


def snapshot(source, state_dir):
    entries = []
    for path in sorted(source.rglob("*")):
        if path.is_relative_to(state_dir):
            continue
        if path.is_symlink():
            raise RuntimeError("Symbolic link requires review: " + str(path))
        if path.is_file():
            stat = path.stat()
            entries.append({"path": path.relative_to(source).as_posix(), "size": stat.st_size,
                            "mtime_ns": stat.st_mtime_ns, "chunks": []})
    return entries


def remote_tip(remote):
    output = git(["ls-remote", remote, "refs/heads/main"]).decode().strip()
    return output.split()[0] if output else None


def chunk_count(entry):
    return max(1, (entry["size"] + CHUNK - 1) // CHUNK)


def object_path(entry, index):
    if entry["size"] <= CHUNK:
        return "files/" + entry["path"]
    key = hashlib.sha256(entry["path"].encode("utf-8")).hexdigest()
    return f"parts/{key}/{index + 1:06d}.part"


def add_blob(repo, path, data):
    oid = git(["hash-object", "-w", "--stdin"], cwd=repo, data=data).decode().strip()
    git(["update-index", "--add", "--cacheinfo", f"100644,{oid},{path}"], cwd=repo)
    return oid


def check_source(source, entry):
    path = source / entry["path"]
    stat = path.stat()
    if stat.st_size != entry["size"] or stat.st_mtime_ns != entry["mtime_ns"]:
        raise RuntimeError("Source changed since inventory: " + entry["path"])
    return path


def main(argv=None):
    global CHUNK
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--remote", help="Defaults to the active repository in the saved checkpoint")
    parser.add_argument("--state-dir")
    parser.add_argument("--work-root")
    parser.add_argument("--batch-mib", type=int, default=BATCH // 1024**2)
    parser.add_argument("--chunk-mib", type=int, default=CHUNK // 1024**2)
    parser.add_argument("--initialize-only", action="store_true", help="Publish restoration metadata without uploading media")
    parser.add_argument("--max-batches", type=int, help="Stop after this many verified batches")
    parser.add_argument("--repo-budget-gib", type=float, default=8)
    args = parser.parse_args(argv)
    CHUNK = args.chunk_mib * 1024**2
    if not 0 < CHUNK < 100 * 1024**2 or not 0 < args.batch_mib < 1500:
        raise RuntimeError("Unsafe chunk or batch size")
    source = Path(args.source).resolve()
    state_dir = Path(args.state_dir).resolve() if args.state_dir else source / ".gitasstorage"
    state_dir.mkdir(exist_ok=True, parents=True)
    lock = (state_dir / "upload.lock").open("a+b")
    lock.seek(0); lock.write(b"0"); lock.flush(); lock.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    state_path = state_dir / "state.json"
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state["chunk_bytes"] != CHUNK:
            raise RuntimeError("Chunk size differs from existing backup")
        active = state.get("active_repository")
        expected_remote = state.get("repositories", {}).get(active, {}).get("url")
        if expected_remote and args.remote and args.remote != expected_remote:
            raise RuntimeError("Requested remote differs from the active backup repository")
        args.remote = args.remote or expected_remote
        if not args.remote:
            raise RuntimeError("Initialize an archive with the GitAsStorage CLI first")
    else:
        raise RuntimeError("Initialize an archive with the GitAsStorage CLI first")
    log(f"Inventory: {len(state['entries'])} files, {sum(e['size'] for e in state['entries']):,} bytes")
    log("Upload destination: " + args.remote)
    for name, details in state.get("repositories", {}).items():
        if name != state.get("active_repository") and details.get("checkpoint"):
            if remote_tip(details["url"]) != details["checkpoint"]:
                raise RuntimeError("Earlier repository changed or is unavailable: " + name)
    last_push = 0
    verified_batches = 0
    while True:
        work = Path(tempfile.mkdtemp(prefix="gitasstorage-batch-", dir=args.work_root)).resolve()
        repo = work / "repository.git"
        try:
            tip = remote_tip(args.remote)
            if tip:
                git(["clone", "--bare", "--filter=blob:none", "--single-branch", "--branch", "main", args.remote, str(repo)])
                actual_tip = git(["rev-parse", "main"], cwd=repo).decode().strip()
                if actual_tip != tip:
                    raise RuntimeError("Remote changed during clone; rerun to resume")
                remote_state = json.loads(git(["show", "main:backup-manifest.json"], cwd=repo))
                if remote_state["backup_id"] != state["backup_id"]:
                    raise RuntimeError("Remote backup identifier differs; stopping")
                if remote_state.get("active_repository") != state.get("active_repository"):
                    raise RuntimeError("Remote active repository differs; stopping")
                # The remote checkpoint also recovers a crash after push but before local save.
                local_settings = {k: v for k, v in state.items() if k.startswith("_")}
                state = dict(remote_state, **local_settings)
                state["_last_commit"] = tip
                save(state_path, state)
                git(["read-tree", "main"], cwd=repo)
            else:
                if state.get("_last_commit"):
                    raise RuntimeError("Remote main branch disappeared; stopping")
                git(["init", "--bare", "--initial-branch=main", str(repo)])
                git(["remote", "add", "origin", args.remote], cwd=repo)
            if state["complete"]:
                log("COMPLETE: remote manifest and main commit verified: " + tip)
                return
            if tip and args.initialize_only:
                log("READY: restoration metadata is already published; media upload is paused.")
                return
            candidate = copy.deepcopy(state)
            added = 0
            added_chunks = 0
            budget_reached = False
            current_repo_bytes = sum(c["size"] for e in state["entries"] for c in e["chunks"]
                                     if c.get("repository") == state["active_repository"])
            touched = []
            cap = args.batch_mib * 1024**2 if tip else min(args.batch_mib * 1024**2, 16 * 1024**2)
            for entry in ([] if args.initialize_only else candidate["entries"]):
                start = len(entry["chunks"])
                for index in range(start, chunk_count(entry)):
                    amount = min(CHUNK, entry["size"] - index * CHUNK)
                    if args.repo_budget_gib and current_repo_bytes + added + amount > args.repo_budget_gib * 1024**3:
                        budget_reached = True
                        break
                    if added and added + amount > cap:
                        break
                    path = check_source(source, entry)
                    with path.open("rb") as stream:
                        stream.seek(index * CHUNK)
                        data = stream.read(amount)
                    if len(data) != amount:
                        raise RuntimeError("Short read: " + entry["path"])
                    check_source(source, entry)
                    storage = object_path(entry, index)
                    checksum = hashlib.sha256(data).hexdigest()
                    oid = add_blob(repo, storage, data)
                    chunk_record = {"path": storage, "size": amount, "sha256": checksum, "git_oid": oid}
                    if candidate.get("active_repository"):
                        chunk_record["repository"] = candidate["active_repository"]
                    entry["chunks"].append(chunk_record)
                    added += amount
                    added_chunks += 1
                    touched.append(entry)
                    del data
                if len(entry["chunks"]) < chunk_count(entry):
                    break
            if budget_reached and not added_chunks:
                log("PAUSED: local per-repository budget reached. Hosting limits still apply; see the continuation instructions.")
                return
            candidate["complete"] = all(len(e["chunks"]) == chunk_count(e) for e in candidate["entries"])
            if candidate["complete"]:
                for entry in candidate["entries"]:
                    check_source(source, entry)
            candidate["uploaded_bytes"] = sum(c["size"] for e in candidate["entries"] for c in e["chunks"])
            candidate["uploaded_chunks"] = sum(len(e["chunks"]) for e in candidate["entries"])
            manifest_bytes = json.dumps(public_manifest(candidate), ensure_ascii=False, indent=2).encode("utf-8")
            add_blob(repo, "backup-manifest.json", manifest_bytes)
            add_blob(repo, "restore.py", Path(__file__).with_name("restore.py").read_bytes())
            add_blob(repo, "README.md", backup_readme(candidate))
            add_blob(repo, ".gitattributes", b"* -text\n")
            # Prior blobs are already on GitHub and intentionally omitted by the filtered clone.
            tree = git(["write-tree", "--missing-ok"], cwd=repo).decode().strip()
            parents = ["-p", tip] if tip else []
            title = f"Archive backup: {candidate['uploaded_bytes']:,} bytes" + (" (complete)" if candidate["complete"] else "")
            commit = git(["commit-tree", tree, *parents], cwd=repo, data=(title + "\n").encode()).decode().strip()
            git(["update-ref", "refs/heads/main", commit], cwd=repo)
            for entry in touched:
                check_source(source, entry)
            if remote_tip(args.remote) != tip:
                raise RuntimeError("Remote main changed before push; stopping without overwriting it")
            delay = PUSH_INTERVAL - (time.monotonic() - last_push)
            if delay > 0:
                time.sleep(delay)
            log(f"Pushing {added / 1024**2:.1f} MiB; total {candidate['uploaded_bytes'] / 1024**3:.2f} GiB")
            # Inherit the terminal so Git displays live transfer percentages and speed.
            process = subprocess.Popen(["git", *GIT_OPTIONS, "push", "--progress", "origin", "main:main"],
                                       cwd=repo, env=ENV)
            try:
                started = time.monotonic()
                while process.poll() is None:
                    time.sleep(POLL_INTERVAL)
                    if time.monotonic() - started >= 30:
                        log(f"Upload active; checkpoint {state.get('uploaded_bytes', 0) / 1024**3:.2f} GiB verified")
                        started = time.monotonic()
            except BaseException:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                raise
            if process.returncode:
                raise RuntimeError("Push failed; see Git's output above. Remote checkpoint preserved.")
            if remote_tip(args.remote) != commit:
                raise RuntimeError("Push completed but remote ref verification failed")
            candidate["_last_commit"] = commit
            save(state_path, candidate)
            state = candidate
            last_push = time.monotonic()
            total = sum(e["size"] for e in state["entries"])
            log(f"VERIFIED {state['uploaded_bytes'] / 1024**3:.2f}/{total / 1024**3:.2f} GiB ({100 * state['uploaded_bytes'] / total:.2f}%) commit {commit}")
            verified_batches += 1
            if args.initialize_only:
                log("READY: restoration metadata published; media upload is paused.")
                return
            if args.max_batches and verified_batches >= args.max_batches:
                log("PAUSED: requested batch count reached; run without --max-batches to continue.")
                return
        finally:
            # Delete only the task-owned, resolved temporary batch directory.
            expected_parent = Path(args.work_root or tempfile.gettempdir()).resolve()
            if work.parent != expected_parent or not work.name.startswith("gitasstorage-batch-"):
                raise RuntimeError("Temporary cleanup path failed validation: " + str(work))
            def remove_readonly(function, path, error):
                resolved = Path(path).resolve()
                if not resolved.is_relative_to(work):
                    raise RuntimeError("Cleanup escaped batch directory")
                os.chmod(path, stat.S_IWRITE)
                function(path)
            shutil.rmtree(work, onexc=remove_readonly)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Stopped by user. Run the same command to resume from the remote checkpoint.")
        sys.exit(130)
    except Exception as error:
        log("STOPPED: " + str(error))
        sys.exit(1)
