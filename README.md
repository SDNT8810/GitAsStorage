# GitAsStorage

Split large files into chunks, upload them to private Git repositories in
resumable batches, and restore the original bytes with SHA-256 verification.

This started with a simple question: could I keep an extra cloud copy of about
130 GiB of wedding media using GitHub? The experiment worked beyond 100 GiB,
then GitHub rejected a push because the repository had reached its size quota.
That limitation is part of this project, not something the tool removes.

**Experimental. Keep independent backups. GitHub does not promise unlimited free
storage or permanent preservation.** Read [the limits and tradeoffs](docs/limits.md)
before uploading irreplaceable data. Private repositories provide access control;
this tool does not encrypt your files.

## What it does

- Stores chunks in ordinary Git, without Git LFS or media re-encoding.
- Defaults to 40 MiB chunks and 512 MiB push batches.
- Shows Git's live transfer percentage and speed, then the overall uploaded total.
- Resumes from the last verified remote commit, including after a local crash.
- Uses temporary filtered bare clones to keep local disk use close to one batch.
- Can continue an archive in another empty private repository, retaining existing chunks.
- Publishes a combined manifest and a standalone restore script with each batch.
- Verifies SHA-256 checksums and file lengths during restoration.
- Refuses public GitHub archive repositories, nonempty initial remotes, and file overwrites.

The public **GitAsStorage** repository contains the tool. Create separate **private**
repositories for your own data. No personal media, archive manifests, or credentials
are included here.

## Requirements

- Python **3.12 or newer** and Git on your PATH. No Python dependencies are needed
  when running directly from this checkout.
- Git authentication that can push to your private repository, plus a credential
  that can read its GitHub API metadata. Git Credential Manager usually supplies both.
- A configured Git author name and email. GitHub commits will contain that identity.
- An unchanged source folder and temporary disk space for roughly twice a batch,
  plus repository metadata. A different temporary drive can be selected with `--work-root`.

Authenticate Git normally before running the tool. If you use GitHub CLI,
`gh auth login` followed by `gh auth setup-git` is one option. `GH_TOKEN` or
`GITHUB_TOKEN` can supply the API privacy check; Git push authentication still needs
to be configured separately. Never put tokens in a remote URL or commit them.

## Quick start

Create a new **private, empty** GitHub repository, for example `archive-01`.
Do not initialize it with a README, license, or `.gitignore`. Replace the username
and example paths below with your own.

```powershell
git clone https://github.com/SDNT8810/GitAsStorage.git
cd GitAsStorage

python gitasstorage.py init "D:\MyMedia" --remote https://github.com/YOUR_USERNAME/archive-01.git --state-dir "D:\archive-state"
python gitasstorage.py upload --state-dir "D:\archive-state"
```

`init` inventories the source and verifies access and repository privacy. It does
not upload any files. `upload` runs in your terminal. Press **Ctrl+C** to stop;
run the same upload command to resume. Keep the machine awake and connected.

```powershell
python gitasstorage.py status --state-dir "D:\archive-state"
```

The checkpoint directory is local operational data. Keep it private and separate
from this code repository. It contains source paths, filenames, and progress.
State fields whose names begin with `_` are omitted from the remote
manifest. Remote manifests necessarily reveal archived filenames to people with
access to the private archive repository.

### Repository budgets and continuation

The tool defaults to an **8 GiB local budget per repository**. It pauses before
exceeding that budget. This conservative guardrail is separate from the host's
actual limits and does not guarantee the host will accept that much data.

After checking that your hosting provider permits your intended use, you can
prepare continuation in a second empty private repository:

```powershell
python gitasstorage.py continue --state-dir "D:\archive-state" --remote https://github.com/YOUR_USERNAME/archive-02.git
python gitasstorage.py upload --state-dir "D:\archive-state"
```

Previously uploaded chunks stay where they are. The next repository receives
the remaining chunks, the combined manifest, and updated restore instructions.
A single original file may have chunks in multiple repositories. **Keep all of them.**

If the host rejects an upload for a quota or usage-policy reason, stop and resolve
that with the provider. More repositories do not create a right to unlimited
storage. The tool never creates extra accounts or repositories automatically.

You may set `--repo-budget-gib` during `init`; `0` disables only this tool's budget,
not the hosting provider's limits. For example, `--chunk-mib 40 --batch-mib 512
--repo-budget-gib 8` are the defaults. Use `upload --max-batches 1` to upload one
batch as a small trial. Do not change chunk size midway through an archive.

## Restore your files

Wait for **`COMPLETE`**, and check `complete: true` in the latest repository's
`backup-manifest.json`. That means all bytes were uploaded; a full restore test
is a separate step. Clone every archive repository into sibling folders. Use the
repository labels shown by `status` as the folder names.

```powershell
git clone -c core.autocrlf=false --single-branch https://github.com/YOUR_USERNAME/archive-01.git archive-01
git clone -c core.autocrlf=false --single-branch https://github.com/YOUR_USERNAME/archive-02.git archive-02
python archive-02/restore.py "E:\RestoredMedia"
```

The standalone `restore.py` is uploaded with the archive and needs only Python.
It does not require this tool's code repository. For a single-repository archive,
run the script from `archive-01` instead.

If your clone folders have different names or locations, specify them explicitly:

```powershell
python archive-02/restore.py "E:\RestoredMedia" --part archive-01="F:\FirstClone" --part archive-02="F:\SecondClone"
```

You can also use this checkout:

```powershell
python gitasstorage.py restore "F:\SecondClone" "E:\RestoredMedia" --part archive-01="F:\FirstClone"
```

Use a new output folder outside all clones. The restore script checks that every
part exists, verifies each SHA-256 checksum and size, and joins the parts in order.
It stops on missing or corrupt data and refuses to overwrite existing files.
This is file reassembly; no Git merge or video repair is needed.

New archives include `.gitattributes` with `* -text` to preserve exact bytes.
The clone commands also disable line-ending conversion explicitly. This matters
for camera XML, subtitles, and other text files as well as binary media.

Allow **more than three times the original data size** for ordinary clones plus
a restored copy: Git's object store, checked-out chunks, and reconstructed files
all consume disk space. Do not delete your originals until a full restore has
passed verification and you have another independent copy.

## What is preserved

File bytes, embedded metadata, original filenames, and directory structure are
preserved. Filesystem timestamps, permissions, hard-link relationships, and empty
directories are not. Symlinks are rejected. Filenames must be supported by the
destination filesystem; case differences alone may be incompatible with Windows.

The inventory is fixed at `init`. New files added afterward are not included.
Changed source files cause a failure before their next read or the final complete
checkpoint. This is a one-time archive workflow; start a new archive for a changed
collection. Deleting a file in a Git checkout does not erase it from Git history.

## Test the tool

```powershell
python -m unittest discover -s tests -v
```

Tests use small generated files and local Git remotes. They cover interrupted
uploads, recovery from a remote checkpoint ahead of local state, continuation
across repositories, exact restoration of LF/CRLF text and binary data, Unicode
names, empty files, missing chunks, corruption, existing output files, source
changes, concurrent-upload locks, public-repository rejection, and path traversal.
They do not establish GitHub capacity or long-term availability. CI runs on
Windows and Linux.

[MIT license](LICENSE).
