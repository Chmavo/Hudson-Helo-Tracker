# data branch

This is an orphan branch that stores the live SQLite database and CSV archives.
It has no shared history with `main`.

Contents:
- `flights.db` — SQLite database written by the harvester (Stage 1+)
- `archive/observations_YYYY-MM.csv.gz` — rolled monthly archives (Stage archival)

Do not commit code here. The harvester workflow checks out this branch
separately from the main code branch.
