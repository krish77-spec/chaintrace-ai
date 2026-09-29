#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Publish this repository to a Hugging Face Space.
#
#   scripts/deploy_hf_space.sh <hf-username>/<space-name> [token]
#
# The Space is a plain git repository, so we publish the exact commit you have
# checked out - exported with `git archive`, so only committed files travel and
# nothing from .gitignore or a dirty working tree can leak out.  The one file we
# swap in is the README: a Space needs its YAML block (sdk, app_port) at the very
# top of the repository README, while this repository's README is the project
# documentation.  deploy/huggingface/README.md is the card that gets substituted.
#
# The token is used inline on the push command and never written to any config
# file.  Pass it as the second argument or as $HF_TOKEN, and revoke it on
# https://huggingface.co/settings/tokens once the Space has built.
#
#   DRY_RUN=1 scripts/deploy_hf_space.sh user/space      # build the tree, no push
# ---------------------------------------------------------------------------
set -euo pipefail

SLUG="${1:-}"
TOKEN="${2:-${HF_TOKEN:-}}"
GITHUB_URL="${GITHUB_REPO_URL:-}"

if [ -z "$SLUG" ]; then
  echo "usage: scripts/deploy_hf_space.sh <hf-username>/<space-name> [token]" >&2
  echo "       (the Space must already exist: https://huggingface.co/new-space, SDK Docker)" >&2
  exit 2
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CARD="${REPO_ROOT}/deploy/huggingface/README.md"

cd "$REPO_ROOT"

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "error: ${REPO_ROOT} is not a git repository" >&2
  exit 2
fi

REV="$(git rev-parse --short HEAD)"
echo "[deploy] publishing $(git rev-parse --short=12 HEAD) to huggingface.co/spaces/${SLUG}"

if [ -n "$(git status --porcelain)" ]; then
  echo "[deploy] note: uncommitted changes are NOT published (git archive exports HEAD only)"
fi

# The Space card links back to the source repository; resolve it from the pushed remote
# or the environment so the substitution cannot silently ship a placeholder.
if [ -z "$GITHUB_URL" ]; then
  GITHUB_URL="$(git remote get-url origin 2>/dev/null | sed -E 's#\.git$##; s#^git@github\.com:#https://github.com/#')"
fi
if [ -z "$GITHUB_URL" ]; then
  echo '[deploy] warning: no origin remote found - set GITHUB_REPO_URL to fill the "Source" link'
  GITHUB_URL="https://github.com/"
fi

STAGE="$(mktemp -d)"
cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

# Only committed files, and only the ones the image build needs (the Dockerfile ignores
# data/artifacts, data/models and docs/*.png itself).
git archive HEAD | tar -x -C "$STAGE"

sed "s|__GITHUB_REPO_URL__|${GITHUB_URL}|g" "$CARD" > "$STAGE/README.md"

echo "[deploy] staged $(find "$STAGE" -type f | wc -l | tr -d ' ') files, $(du -sh "$STAGE" | cut -f1) total"
grep -q '^sdk: docker' "$STAGE/README.md" || { echo "error: Space card lost its sdk: docker line" >&2; exit 2; }
grep -q '^app_port: 7860' "$STAGE/README.md" || { echo "error: Space card lost its app_port line" >&2; exit 2; }

if [ -n "${DRY_RUN:-}" ]; then
  echo "[deploy] DRY_RUN set - staged tree left at ${STAGE} (not deleted)"
  trap - EXIT
  exit 0
fi

if [ -z "$TOKEN" ]; then
  echo "error: no Hugging Face write token. Pass it as arg 2 or as \$HF_TOKEN." >&2
  exit 2
fi

git -C "$STAGE" init -q -b main
git -C "$STAGE" add -A
git -C "$STAGE" -c user.name="ChainTrace Deploy" -c user.email="deploy@localhost" \
  commit -q -m "ChainTrace AI ${REV} (SIH26146)"

# The token lives only in this command line, never in a config file.
git -C "$STAGE" push --force \
  "https://${SLUG%%/*}:${TOKEN}@huggingface.co/spaces/${SLUG}" main

echo "[deploy] pushed. The Space rebuilds in a few minutes:"
echo "         https://huggingface.co/spaces/${SLUG}"
echo "[deploy] logs:  https://huggingface.co/spaces/${SLUG}/logs"
echo "[deploy] revoke the token once the build is green: https://huggingface.co/settings/tokens"
