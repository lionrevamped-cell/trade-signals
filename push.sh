#!/bin/bash
# push.sh — Ship an update to GitHub from this Mac.
#
# Usage:
#   ./push.sh                    -> commits with auto message + pushes
#   ./push.sh "tweak scoring"    -> commits with your message + pushes
#
# Her Windows scanner picks this up the next time she opens run.bat
# (within seconds, since GitHub's API serves the new SHA immediately).

set -e
cd "$(dirname "$0")"

MSG="${1:-update $(date +%Y-%m-%d_%H:%M)}"

# Stage everything except local-only state files
git add -A

# Bail if nothing to commit
if git diff --cached --quiet; then
    echo "No changes to commit. Nothing to push."
    exit 0
fi

git commit -m "$MSG"
git push

echo
echo "✓ Pushed to GitHub. Her PC will pull this on the next 'run.bat'."
echo "  (Verify on https://github.com/lionrevamped-cell/trade-signals)"
