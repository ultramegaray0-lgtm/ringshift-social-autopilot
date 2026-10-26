# Test report: what was actually verified

Reproduce: `python -m unittest discover -s tests -v`

| Kind | What it proves | What it does NOT prove |
|---|---|---|
| **Mocked offline tests** (`test_autopilot_hardening`, `test_buffer_publish`, `test_main_flow`, `test_analytics`, ...) | Request shape, media assets, 401/GraphQL/unconfirmed-media failure paths change no state, 429-only retry, no double-queue, counters/logs, dry-run, rule screening. Network calls raise. | That Buffer accepts the request today. |
| **Synthetic-media tests** (`test_media_tools`, `test_media_store`) | FFmpeg validation/encoding with generated clips; branch publishing against a local bare git repo. | Real gameplay look. |
| **Real-game capture test** (`test_capture`, needs `RINGSHIFT_GAME_HTML`) | The capture plays and wins a real level in a real browser and produces valid clips. Also run for 6 levels (18 files, all validated). | Anything about publishing. |
| **Workflow/code contract tests** (`test_no_unapproved_publishing`) | Static text/AST checks: the schedule step is draft-only and has no Buffer key; exactly one step can publish (`publish-approved`, manual); only `publish_approved` can reach `send_to_buffer`; no other script can create a post; analytics is read-only; no `live` mode; CI has no secrets. | That GitHub honours it. See the rehearsal row. |
| **Verification and claims, offline** (`test_verify_and_claims`) | `--verify-approved` claims/sends nothing; draft file round trip (generate -> copy -> verify -> publish once); tampering; a race between two runs; claim read/create/refuse/cleanup against an in-memory FAKE of GitHub's ref API. | That the real GitHub API and artifact store behave like the fake. |
| **Actions rehearsal** (`rehearse-approval` run) | Runs on GitHub: real cross-run artifact lookup and download, real integrity checks on the downloaded file, and the real claim API on a throwaway tag. Publishes nothing. | Only what that run's log shows; it is not a post. |
| **Verified live publishing** | **None.** No post was made, by design. | Run `publish-approved` once after merging; see APPROVING_A_POST.md. |

Also verified live (read-only): `raw.githubusercontent.com` serves the repo's media as described in MEDIA_HOSTING.md;
Buffer pagination arguments follow https://developers.buffer.com/guides/pagination.md but the paginated query itself has not
been run against your account (a fallback to the old single-page query is in place if Buffer rejects it).
