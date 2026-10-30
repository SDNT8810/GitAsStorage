"""Standalone checksum-verified restore for a GitAsStorage archive."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")


def valid_path(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("Unsafe path in manifest")
    parts = PurePosixPath(value)
    if parts.is_absolute() or re.match(r"^[A-Za-z]:", value) or any(p in ("", ".", "..") for p in value.split("/")):
        raise ValueError("Unsafe path in manifest: " + value)


def validate_manifest(manifest):
    if manifest.get("format") not in (1, 2) or not isinstance(manifest.get("entries"), list):
        raise ValueError("Unsupported or invalid archive manifest")
    repositories = manifest.get("repositories", {})
    if not isinstance(repositories, dict):
        raise ValueError("Invalid repository mapping")
    for name in repositories:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", name):
            raise ValueError("Unsafe repository label")
    if repositories and manifest.get("active_repository") not in repositories:
        raise ValueError("Active repository is missing from the manifest")
    seen = set()
    chunk_size = manifest.get("chunk_bytes")
    if not isinstance(chunk_size, int) or not 0 < chunk_size < 100 * 1024**2:
        raise ValueError("Invalid chunk size")
    for entry in manifest["entries"]:
        valid_path(entry["path"])
        name = entry["path"].casefold() if sys.platform == "win32" else entry["path"]
        if name in seen:
            raise ValueError("Duplicate output filename in manifest")
        seen.add(name)
        if not isinstance(entry["size"], int) or entry["size"] < 0 or not isinstance(entry.get("chunks"), list):
            raise ValueError("Invalid file entry")
        for chunk in entry["chunks"]:
            valid_path(chunk["path"])
            if not isinstance(chunk["size"], int) or not 0 <= chunk["size"] <= chunk_size:
                raise ValueError("Invalid chunk length")
            if not isinstance(chunk.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", chunk["sha256"]):
                raise ValueError("Invalid SHA-256 checksum")
            if repositories and chunk.get("repository") not in repositories:
                raise ValueError("Chunk refers to an unknown repository")
        if manifest.get("complete") is True:
            expected_count = max(1, (entry["size"] + chunk_size - 1) // chunk_size)
            if len(entry["chunks"]) != expected_count or sum(c["size"] for c in entry["chunks"]) != entry["size"]:
                raise ValueError("Complete manifest has missing file data")


def restore(repository, destination, parts=None):
    repository = Path(repository).resolve()
    destination = Path(destination).resolve()
    manifest = json.loads((repository / "backup-manifest.json").read_text(encoding="utf-8"))
    validate_manifest(manifest)
    if manifest["complete"] is not True:
        raise ValueError("This backup is incomplete; wait for the final complete manifest")
    roots = {manifest.get("active_repository", "default"): repository}
    for name in manifest.get("repositories", {}):
        if name not in roots:
            roots[name] = (repository.parent / name).resolve()
    for item in parts or []:
        name, separator, folder = item.partition("=")
        if not separator or not name or not folder:
            raise ValueError("Use --part REPOSITORY=FOLDER")
        roots[name] = Path(folder).resolve()
    for root in roots.values():
        if destination == root or destination.is_relative_to(root):
            raise ValueError("Choose an output folder outside all cloned repositories")
    # Find missing chunks before creating any restored files.
    for entry in manifest["entries"]:
        for chunk in entry["chunks"]:
            name = chunk.get("repository", "default")
            if name not in roots:
                raise ValueError("Missing repository mapping: " + name)
            root = roots[name]
            part = (root / chunk["path"]).resolve()
            if not part.is_relative_to(root):
                raise ValueError("Unsafe chunk path in manifest")
            if not part.is_file():
                raise FileNotFoundError("Missing backup data in " + name + ": " + str(part))
    for entry in manifest["entries"]:
        target = (destination / entry["path"]).resolve()
        if not target.is_relative_to(destination):
            raise ValueError("Unsafe destination path in manifest")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".gitasstorage-restoring")
        if target.exists() or temporary.exists():
            raise FileExistsError("Refusing to overwrite: " + str(target))
        count = 0
        created = False
        try:
            with temporary.open("xb") as output:
                created = True
                for chunk in entry["chunks"]:
                    root = roots[chunk.get("repository", "default")]
                    part = (root / chunk["path"]).resolve()
                    if not part.is_relative_to(root):
                        raise ValueError("Unsafe chunk path in manifest")
                    digest = hashlib.sha256()
                    size = 0
                    with part.open("rb") as stream:
                        while data := stream.read(8 * 1024**2):
                            digest.update(data)
                            output.write(data)
                            size += len(data)
                    if size != chunk["size"] or digest.hexdigest() != chunk["sha256"]:
                        raise ValueError("Checksum mismatch: " + chunk["path"])
                    count += size
            if count != entry["size"]:
                raise ValueError("File length mismatch: " + entry["path"])
            temporary.rename(target)
        except Exception:
            if created:
                temporary.unlink(missing_ok=True)
            raise
        print("Verified:", entry["path"], flush=True)
    print("Restore complete; every chunk passed SHA-256 verification.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", help="New output folder outside the clone")
    parser.add_argument("--repository", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--part", action="append", default=[], metavar="REPOSITORY=FOLDER",
                        help="Location of an additional cloned backup repository; may be repeated")
    args = parser.parse_args()
    try:
        restore(args.repository, args.destination, args.part)
    except Exception as error:
        print("Restore stopped:", error, file=sys.stderr)
        sys.exit(1)
