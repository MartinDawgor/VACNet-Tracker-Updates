<p align="center">
  <img src="icon.png" alt="VACNet Tracker" width="160" />
</p>

<h1 align="center">VACNet Tracker Updates</h1>

<p align="center">
  <a href="README.md"><b>Русский</b></a>
</p>

---


An automated monitoring and change-detection pipeline for the CS2 VACNet labeling portal client (`counter-strike.net/vacnet/clips`).
The repository serves as an auxiliary infrastructure tool for tracking production bundle rotations, DOM tree updates, and preventing desynchronization with the primary extension codebase.
Data fetching and drift audits run automatically via GitHub Actions on a 6-hour cron schedule, dynamic and contextual fields are excluded from the comparison.
Whenever client updates are detected, the workflow isolates structural diffs, updates the changelog, and publishes fresh snapshots directly to GitHub Releases.

### Repository Structure
* `snapshots/current/` — Active reference snapshot of the portal (raw HTML and normalized JSON structure).
* `snapshots/outdated/` — Versioned archive of previous iterations containing isolated `.diff` patches.
* `CHANGELOG.md` — Chronological registry of detected structural modifications.
* `tracker/` — Core parsing engine, comparison logic, and asset extraction runners.
