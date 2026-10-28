#!/usr/bin/env bash
# Commit the encrypted outreach state to the outreach-state branch (never main). Used by outreach.yml.
# Only state.json.enc is ever committed; a plaintext state file makes this script fail instead.
set -euo pipefail

STATE_DIR="${STATE_DIR:-.outreach-state}"
STATE_BRANCH="${STATE_BRANCH:-outreach-state}"

case "$STATE_BRANCH" in
  main|master|media-store|tiktok-state|game-lab-previews)
    echo "::error::refusing to write outreach state to the protected branch $STATE_BRANCH"; exit 1 ;;
esac

if [ ! -f "$STATE_DIR/state.json.enc" ]; then
  echo "No encrypted state to save (the run stopped before writing it)."
  exit 0
fi
if [ -f "$STATE_DIR/state.json" ]; then
  echo "::error::a plaintext state.json exists; refusing to commit it"
  exit 1
fi

cd "$STATE_DIR"
if [ ! -d .git ] && [ ! -f .git ]; then
  git init -q
  git checkout -q --orphan "$STATE_BRANCH"
fi
cat > README.md <<'EOF'
Encrypted state of the Ringshift creator outreach (outreach/ on main). Written only by the
"Ringshift creator outreach" workflow. Decrypt only with OUTREACH_STATE_KEY; never edit by hand.
EOF
git add state.json.enc README.md
if git diff --cached --quiet; then
  echo "State unchanged."
  exit 0
fi
git -c user.name="github-actions[bot]" -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
  commit -q -m "Outreach state $(date -u +%Y-%m-%dT%H:%MZ)"

push_from_worktree() {
  git push origin "HEAD:refs/heads/$STATE_BRANCH"
}

if git rev-parse --is-inside-work-tree >/dev/null 2>&1 && git remote get-url origin >/dev/null 2>&1; then
  push_from_worktree
else
  # First run: a fresh repository inside the checkout; borrow the checkout's remote and credential.
  url="$(git -C "$GITHUB_WORKSPACE" remote get-url origin)"
  header="$(git -C "$GITHUB_WORKSPACE" config --get http.https://github.com/.extraheader || true)"
  git -c "http.https://github.com/.extraheader=$header" push "$url" "HEAD:refs/heads/$STATE_BRANCH"
fi
echo "State saved to $STATE_BRANCH."
