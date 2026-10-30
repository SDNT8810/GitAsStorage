# Hosting limits and practical tradeoffs

Checked against GitHub documentation on October 30, 2026. Hosting rules can change.

| Limit or behavior | Meaning for this tool |
| --- | --- |
| Ordinary Git files above 100 MiB are blocked | Split large files before adding them to Git. |
| Files above 50 MiB produce warnings | The default chunk size is 40 MiB. |
| A Git push is limited to 2 GB | The default media batch is 512 MiB, with room for metadata and pack overhead. |
| GitHub's repository-limits page recommends 10 GB on disk | This is a recommendation, not a published promise of unlimited space beyond it. The tool defaults to an 8 GiB budget. |
| Git LFS has separate storage and bandwidth billing | This tool uses ordinary Git, so LFS allowances are not its capacity limit. |
| Repository quota enforcement still exists | A host can reject an upload even when files and pushes are small enough. |

Sources: [large files](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github),
[repository limits](https://docs.github.com/en/repositories/creating-and-managing-repositories/repository-limits),
[LFS billing](https://docs.github.com/en/billing/concepts/product-billing/git-lfs).

In the original experiment, a repository accepted 104.08 GiB of media before
GitHub rejected the next push with: “Repository is above its size quota.” This is
an observation from one repository, not a documented universal threshold. Git's
reported on-disk size and the source file total are different measurements.

GitHub's [acceptable-use policy](https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies#9-excessive-bandwidth-use)
allows restrictions for excessive bandwidth and, with advance notice, deletion of
repositories that place undue strain on its infrastructure. Its large-file guide
also explains that Git is not designed as a backup tool. Use this experiment only
where the provider permits the workload. Ask support about quota errors instead
of assuming that splitting files or repositories removes usage restrictions.

## Privacy and durability

- Private repository access control does not provide end-to-end encryption.
  GitHub, authorized collaborators, or someone who gains account access may be
  able to access the content. The tool has no built-in encryption.
- Filenames and archive metadata are included in private manifests. Source absolute
  paths and local settings are removed from uploaded manifests. Git commit author
  metadata remains visible to those with repository access.
- SHA-256 checks detect changed or missing archive bytes; they do not guarantee
  account access, service availability, or permanent preservation.
- “Uploaded and commit verified” confirms a remote branch points to the commit.
  It does not mean every remote blob has been downloaded and compared. The restore
  process performs the chunk-by-chunk checks after downloading.
- Keep original files and another independent backup. Test a full restoration
  before relying on an archive. Small integration tests do not prove that an
  entire personal archive has been restored successfully.

## Storage and transfer cost

The software has no subscription or required Python packages. That does not make
any host's storage or network capacity unlimited or contractually free. The tool
does not enable LFS, buy storage, change billing, or bypass provider quotas.

Media is already compressed, so Git usually saves little space. Ordinary clones
store both Git objects and checked-out data; restoring adds another complete copy.
Large histories can be slow to clone and maintain. For irreplaceable large archives,
dedicated backup or object storage is generally a better operational fit.
