# Where do the clips live? (Buffer needs a public, direct, stable HTTPS URL)

| Option | Cost | Pros | Cons | Verdict |
|---|---|---|---|---|
| Files on `main` (`raw.githubusercontent.com`) | 0 | Already works, versioned, simplest | Every video stays in git history forever; repo already ~190 MB | Fine for a small number of ~1 MB clips (current captures). |
| `media-store` branch (`media_store.py`) | 0 | Squashed to one commit, newest 60 clips kept, `main` unaffected, same raw host | Force-pushes that one branch; needs push access on your PC | **Recommended for new videos.** |
| GitHub Releases assets | 0 | Large files OK | URLs redirect to a CDN: Buffer needs a non-redirecting URL | Not suitable. |
| Cloudflare R2 / S3 | free tier then paid | Real object storage | Account, keys, billing risk | Only if the repo approach is outgrown. |

Verified behaviour of `raw.githubusercontent.com`: direct 200 (no redirect), correct `content-length`,
`.mp4` served as `application/octet-stream` (the pre-flight check accepts this), 404 for missing files.
Branch names in raw URLs are case-sensitive, and a file added in a PR is **not** reachable at `/main/` until merged.

Retention: `media_store.py publish --keep 60` (default). Delete old local copies with `--delete-local`.
Media size rules (enforced by `media_tools.py`): videos ≤15 MB and ≤45 s, images ≤5 MB.
