# TikTok inbox upload (automatic draft hand-off)

After every video that the **Ringshift TikTok video** workflow makes and saves to `TikTok-Output/`, it starts
**Ringshift TikTok inbox upload** with the exact run ID and artifact name. That workflow re-checks the video, uploads it
to your TikTok **inbox** through TikTok's official Content Posting API (`video.upload` scope) and records the result.
You get a TikTok notification, edit the caption/sound if you like, and tap **Post**. Nothing is public before that.

```
video job ─► artifact ─► save job: re-check ─► tiktok-output:TikTok-Output/ ─► gh workflow run tiktok-upload.yml
                                                                               (source_run_id, artifact_name)
inbox upload: setup check ─► download that exact artifact ─► re-check ─► already uploaded? stop
            ─► refresh token ─► FILE_UPLOAD to /v2/post/publish/inbox/video/init/ ─► poll status
            ─► record publish_id on tiktok-state ─► save rotated token (encrypted) on tiktok-auth-state
```

## One-time setup (about 15 minutes)

Nothing here costs money. You need a TikTok developer account and the TikTok account the videos are for.

1. **App.** At <https://developers.tiktok.com/> → *Manage apps* → create an app (or open the one you made for PR #17).
   Add the products **Login Kit** and **Content Posting API**. In Content Posting API, keep **Upload** (inbox drafts)
   on; *Direct Post* is not needed. Request the scopes `user.info.basic` and `video.upload`.
2. **Redirect URI.** In Login Kit, register one HTTPS redirect URI you control, for example your demo page
   (`https://<you>.itch.io/ringshift` or a GitHub Pages URL). It only has to load in a browser; it does not need to
   do anything.
3. **Approval or sandbox.** Either submit the app for review (TikTok checks the app details and the `video.upload`
   scope), **or** use a *Sandbox* of the app and add your own TikTok account as a *target user*. TikTok's sandbox
   excludes only *public* posting, so inbox drafts can be tried there first.
4. **Refresh token.** On your computer, in this repository:
   ```
   python scripts/tiktok_auth.py --client-key <client key> --redirect-uri <the redirect URI from step 2>
   ```
   Open the link it prints, allow access, paste the address you land on, type the client secret when asked. It
   prints the refresh token once (valid for about a year; the workflow keeps it fresh after that).
5. **Encryption key.** Generate one locally:
   `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
6. **GitHub secrets.** Repository → *Settings → Secrets and variables → Actions → New repository secret*:

   | Secret | Value |
   |---|---|
   | `TIKTOK_CLIENT_KEY` | the app's client key |
   | `TIKTOK_CLIENT_SECRET` | the app's client secret |
   | `TIKTOK_REFRESH_TOKEN` | the token from step 4 (used until the encrypted copy exists) |
   | `TIKTOK_TOKEN_ENCRYPTION_KEY` | the key from step 5 |

7. **Try it** on a video that already exists: *Actions → Ringshift TikTok inbox upload → Run workflow* with the run ID
   of any successful *Ringshift TikTok video* run (the number in its URL). Leave *artifact_name* empty. Within a few
   minutes the draft shows up in TikTok → Inbox.

Until the secrets exist, each automatic upload run fails with **NOT SET UP YET** and the names of the missing secrets.
That is deliberate (a skipped upload you would not notice is worse); the video is saved either way.

## Finishing a draft

TikTok app → **Inbox** → the notification for the Ringshift app → edit caption (the suggested caption is in the upload
run's summary and in the video's JSON), add a sound, pick a cover → **Post**.

## Retries and duplicates

- Each uploaded video is recorded on the `tiktok-state` branch by its SHA-256, with TikTok's `publish_id`, the run that
  made it and the run that uploaded it. The record is written as soon as TikTok has the bytes, even if a later status
  check fails.
- Re-running the upload, re-running the video workflow, or a second hand-off of the same video uploads nothing and
  says so in the summary. To send a video again on purpose, run the upload by hand with **force** on.
- TikTok allows at most 5 pending (not yet posted) inbox drafts per account in 24 hours; one video a day stays well
  inside that, but post or delete old drafts if you trigger many by hand.
- If TikTok reports `FAILED` while processing, the upload is not counted and a re-run tries again.
- The upload status is polled for about 2 minutes; `PROCESSING_UPLOAD` at the end is not an error (TikTok finishes in
  the background and sends the notification).

## Secrets and safety

- Access tokens, refresh tokens, the encryption key and TikTok's signed upload URL are never printed or written to the
  run summary. The refresh token is stored only as Fernet ciphertext on the `tiktok-auth-state` branch.
- Only the upload workflow can see the TikTok secrets; the video workflow has none.
- Runs on branches other than `main` never hand off to TikTok.
- The uploader only calls the inbox endpoint. It has no code for Direct Post (`video.publish`).

## Can posting be fully automatic? (researched 2026-11-01)

Short answer: **not through TikTok's official API for this kind of tool**, so the inbox draft (one tap to post) is the
working setup. Details from TikTok's own developer documentation:

| Question | Answer |
|---|---|
| Can the current app post directly? | No. Direct Post needs the separate `video.publish` scope; this app uses `video.upload` (inbox) only. |
| Is `video.publish` available? | It can be requested, but without an **audit** every post is forced to `SELF_ONLY` (only you can see it), only accounts set to **private** can post, and at most 5 users per 24 h. |
| Is account authorization enough? | No. Public Direct Post additionally requires passing TikTok's audit. |
| Would the audit pass? | Very unlikely. The audit checks the posting UX: the creator must pick the privacy level by hand each time (no default), see a preview and give explicit consent before each upload. TikTok's content-sharing guidelines also list "a utility tool to help upload contents to the account(s) you or your team manages" as **not acceptable**, and clients may not be limited to internal/private use. An unattended daily poster for one's own account is exactly that. |
| Without paid services? | The API itself is free. Paid schedulers post through their own audited apps; that is not "without paid services", and they are bound by the same consent rules. |

So: no browser automation and no workaround (both would break TikTok's terms and put the account at risk). If TikTok
changes these rules, the place to add Direct Post is `scripts/tiktok_upload.py` (`/v2/post/publish/video/init/` after
a `creator_info` query), behind its own opt-in input.

Sources: TikTok for Developers — [Content Posting API: upload content](https://developers.tiktok.com/doc/content-posting-api-get-started-upload-content/),
[Direct Post reference](https://developers.tiktok.com/docs/en/content-posting-api-reference-direct-post),
[Content Sharing Guidelines](https://developers.tiktok.com/docs/en/content-sharing-guidelines),
[Get post status](https://developers.tiktok.com/docs/en/content-posting-api-reference-get-video-status),
[Sandbox](https://developers.tiktok.com/docs/en/add-a-sandbox).
