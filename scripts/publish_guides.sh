#!/usr/bin/env bash
# Publish us.xml.gz and mx.xml.gz as the only commit on an orphan `guides` branch.
# A missing new file keeps the copy already on that branch. History stays one commit.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="${DIST_DIR:-$ROOT/dist}"
BRANCH="guides"
REMOTE_URL="${GUIDES_REMOTE:-$(git -C "$ROOT" remote get-url origin)}"

WORKDIR="$(mktemp -d)"
cleanup() {
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

# Actions keeps the checkout token in http.<host>.extraheader on this repo,
# not in the remote URL. Copy it so the orphan repo can push.
copy_github_auth() {
  local src="$1"
  local dst="$2"
  local key
  while IFS= read -r key; do
    [[ -z "$key" ]] && continue
    git -C "$dst" config --local "$key" "$(git -C "$src" config --local --get "$key")"
  done < <(git -C "$src" config --local --name-only --get-regexp '^http\..*\.extraheader$' || true)
}

mkdir -p "$WORKDIR/files"

echo "checking ${REMOTE_URL} for ${BRANCH}"
set +e
git ls-remote --exit-code --heads "$REMOTE_URL" "$BRANCH" >/dev/null
remote_status=$?
set -e
if [[ "$remote_status" -eq 0 ]]; then
  git clone --depth 1 --branch "$BRANCH" --single-branch "$REMOTE_URL" "$WORKDIR/prev"
  cp -a "$WORKDIR/prev/"*.xml.gz "$WORKDIR/files/" 2>/dev/null || true
elif [[ "$remote_status" -eq 2 ]]; then
  echo "no existing ${BRANCH} branch; this publish creates it"
else
  echo "could not read the existing ${BRANCH} branch (ls-remote exit ${remote_status})" >&2
  exit 1
fi

published=0
for country in us mx; do
  fresh="$DIST/${country}.xml.gz"
  if [[ -f "$fresh" ]]; then
    cp "$fresh" "$WORKDIR/files/${country}.xml.gz"
    echo "publishing new ${country}.xml.gz"
    published=1
  elif [[ -f "$WORKDIR/files/${country}.xml.gz" ]]; then
    echo "no new ${country}.xml.gz; keeping the previous file"
    published=1
  else
    echo "warning: no ${country}.xml.gz to publish yet" >&2
  fi
done

if [[ "$published" -eq 0 ]]; then
  echo "nothing to publish" >&2
  exit 1
fi

git -C "$WORKDIR/files" init -b "$BRANCH"
git -C "$WORKDIR/files" config user.name "github-actions[bot]"
git -C "$WORKDIR/files" config user.email "41898282+github-actions[bot]@users.noreply.github.com"
# Expand the glob inside the publish directory. The shell would otherwise
# expand *.xml.gz from the caller's working directory.
(
  cd "$WORKDIR/files"
  git add -- *.xml.gz
  git commit -m "Update XMLTV guides ($(date -u +%Y-%m-%d))"
)

# Push from the orphan repo so a shallow Actions checkout is never updated.
copy_github_auth "$ROOT" "$WORKDIR/files"
git -C "$WORKDIR/files" -c credential.helper= \
  -c core.askPass= \
  push --force "$REMOTE_URL" "HEAD:refs/heads/${BRANCH}"
echo "published ${BRANCH}"
