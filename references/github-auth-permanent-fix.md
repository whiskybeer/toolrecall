# GitHub Auth: Permanent Fix

Root cause: `~/.netrc` has a classic PAT that works for git push (basic auth over HTTPS) but the GitHub API rejects it. `gh` needs a token with API access.

## Permanent solution

The GITHUB_TOKEN in `~/.hermes/.env` has the right scopes (`repo`). It's now sourced in both `~/.bashrc` and `~/.profile`:

```bash
export GITHUB_TOKEN=$(grep "^GITHUB_TOKEN=" ~/.hermes/.env | head -1 | cut -d= -f2-)
```

This means every new shell session gets `$GITHUB_TOKEN` automatically.

## Usage pattern

```bash
# gh release create (works)
GH_TOKEN=$GITHUB_TOKEN gh release create vX.Y.Z -R owner/repo --title "..." --notes-file /tmp/notes.md

# gh release list (works)
GH_TOKEN=$GITHUB_TOKEN gh release list -R owner/repo

# curl API (works)
curl -H "Authorization: Bearer $GITHUB_TOKEN" https://api.github.com/repos/owner/repo

# git push (works via ~/.netrc — separate credential)
```

## What doesn't work

`gh auth login --with-token` — the token lacks `read:org` scope which gh auth login requires. Not needed for any operation we do.

## Regenerating the token

If the token expires or needs updating:
1. Create a fine-grained PAT at github.com/settings/tokens with `repo` scope
2. Replace the value in `~/.hermes/.env`: `GITHUB_TOKEN=ghp_<new_token>`
3. No other changes needed — .bashrc/.profile read it dynamically