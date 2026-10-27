#!/bin/sh
# git credential helper for this repo's push, on Windows.
#
# WHY THIS FILE EXISTS
# --------------------
# A plain `git push` here produced a 0-byte log for 6m37s and never finished, so the
# push must bypass the credential-selector. The obvious form does not work:
#
#   GCM="/c/Program Files/Git/mingw64/bin/git-credential-manager.exe"
#   git -c credential.helper= -c credential.helper="!$GCM" push origin main
#   -> /c/Program Files/.../git-credential-manager.exe get: line 1: /c/Program: No such file or directory
#   -> fatal: could not read Username for 'https://github.com'
#
# `!` hands the string to sh, which word-splits the path on the space in
# "Program Files". The path cannot simply be double-quoted inside the config value
# without fighting three layers of quoting, so it lives here instead, where the
# quoting is ordinary and the path has no spaces of its own.
#
# USAGE
#   git -c credential.helper= -c credential.helper='!.scratch/gcm_wrap.sh' push origin main
# or, with this file tracked under tools/:
#   git -c credential.helper= -c credential.helper='!tools/gcm_wrap.sh' push origin main
#
# Verify the push with `git ls-remote origin refs/heads/main` against
# `git rev-parse HEAD` -- never the push message, and never the exit code alone
# (a hung-but-successful push has shown EXIT=0 only after minutes). `git status`
# always reports `[gone]` in this repo because .git/refs/remotes/* is wiped
# immediately after being written.
exec "/c/Program Files/Git/mingw64/bin/git-credential-manager.exe" "$@"
