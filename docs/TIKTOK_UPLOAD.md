# TikTok inbox upload (draft handoff)

This is a separate, manual workflow. It uploads a selected generated MP4 to TikTok's **inbox** using the official
Content Posting API `video.upload` scope. It does not publish, schedule, or change X/Buffer. You must open the TikTok
inbox notification, review/edit the draft and its caption, and tap **Publish** in TikTok.

## TikTok developer setup (one-time)

1. Create an app at https://developers.tiktok.com/ and add the **Content Posting API** product.
2. Configure Login Kit / OAuth redirect URI and request the `video.upload` scope. The app must be approved for the
   scope and the TikTok account must authorize it.
3. Follow TikTok's OAuth authorization-code flow to obtain a user refresh token. The token response must include
   `video.upload` in its `scope` field. TikTok access tokens expire after 24 hours; refresh tokens are valid for
   365 days and may rotate on refresh.
4. In GitHub → repository → Settings → Secrets and variables → Actions, add:
   - `TIKTOK_CLIENT_KEY`: app client key
   - `TIKTOK_CLIENT_SECRET`: app client secret
   - `TIKTOK_REFRESH_TOKEN`: initial OAuth refresh token (used only until encrypted token state exists)
   - `TIKTOK_TOKEN_ENCRYPTION_KEY`: a Fernet key generated locally with:
     `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
     Keep this key private; never commit it or paste it into an issue/chat.
5. The first successful upload encrypts the current refresh token and saves it on the isolated
   `tiktok-auth-state` branch. Subsequent runs read that encrypted token and persist any rotated refresh token there.
   The branch contains ciphertext only; the decryption key remains in GitHub Actions secrets.

## Upload a generated video

1. Open **Actions → Ringshift TikTok video** and run it (or use an existing successful run).
2. Copy that run's numeric run ID from its URL, and copy the exact artifact name shown under **Artifacts**.
3. Open **Actions → Ringshift TikTok inbox upload → Run workflow**. Enter the source run ID and artifact name.
4. When the workflow succeeds, open TikTok on your phone → **Inbox** → open the TikTok notification → review the video,
   edit/add the caption, sounds, cover and settings, then tap **Publish**.

The uploader uses TikTok's `FILE_UPLOAD` transfer method, sends chunks within TikTok's documented limits, checks the
upload status, and never prints access tokens, refresh tokens or signed upload URLs. The caption suggestion is included
in the Actions summary for you to paste/edit in TikTok; TikTok's inbox-upload endpoint itself uploads the video draft.

## Important limits

- TikTok's API app and `video.upload` scope approval are required; this is not a workaround for approval.
- TikTok limits inbox uploads to at most 5 pending shares in a 24-hour period.
- The creator must finish the post inside TikTok. An inbox upload is not a published TikTok post.
- The initial OAuth refresh token must be obtained and added by you; this repository cannot register or authorize a
  TikTok app on your behalf.
- The encrypted state branch is separate from `main` and `tiktok-state`. Do not delete it unless you are ready to
  re-authorize TikTok.
