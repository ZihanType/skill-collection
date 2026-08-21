# Use a two-phase skill sync

The updater first downloads and validates every configured Source Skill into an immutable Sync Plan, then applies one Sync Operation at a time. We accept the extra staging complexity because a single-pass sync cannot both guarantee that validation failures leave the repository untouched and let GitHub Actions commit and push each Managed Skill independently.
