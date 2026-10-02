> **2026-10-02:** this page records how the repository was first created. The
> `archive/` zips it names were removed from the repository on this date.

# Pushing to GitHub

Run from inside `anycam-repo` after `build_repo.ps1` has completed.

## 1. Verify the history looks right

```
git log --oneline | head -20
git tag | sort -V | tail -20
git rev-list --count HEAD
```

HEAD should be the scaffolding commit, with 2.6.0-rc3.1 immediately beneath it.

## 2. Create the repo and push

The repo does not need to exist on GitHub beforehand — this creates it:

```
gh repo create crystalheeler/anycam --private --source=. --remote=origin --push
```

Then push the tags, which are not included above:

```
git push --tags
```

If `gh` is not authenticated yet:

```
gh auth login
```

## 3. Create the milestone Releases

Six versions get Releases with their zip attached. The zips are in
`archive/`.

```
gh release create 2.2.6 archive/*2.2.6*.zip \
  --title "2.2.6" --notes "First broadly working build."

gh release create 2.3.0 archive/*2.3.0*.zip \
  --title "2.3.0" --notes "Throttle-aware probe pacing."

gh release create 2.4.0 archive/*2.4.0*.zip \
  --title "2.4.0" --notes "Camera lockout protections complete."

gh release create 2.5.0 archive/*2.5.0*.zip \
  --title "2.5.0" --notes "DVR channel enumeration."

gh release create 2.6.0-rc2.5 archive/*2.6.0-rc2.5*.zip \
  --prerelease --title "2.6.0-rc2.5" \
  --notes "Validated hardware-decode baseline. Field-tested: 22 fps average uncapped, 10 fps stable at cap with rpivid HEVC decode engaged."

gh release create 2.6.0-rc3.1 archive/*2.6.0-rc3.1*.zip \
  --prerelease --title "2.6.0-rc3.1" \
  --notes "Current shipped build. Fast Stream Start and Locked Streams badge persistence retained; card-view RTSP reverted."
```

Adjust the archive glob if a milestone zip is missing — not every milestone
is guaranteed to be present in the local archive.

## 4. Sanity check

```
gh repo view crystalheeler/anycam --web
```

Confirm the commit count, that tags appear under Releases, and that commits
are attributed to your account. If they show as unattributed, the author
email baked into the history does not match a verified email on the account.
