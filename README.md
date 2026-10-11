# Culture Calendar

A subscribable iCal feed with:
- Swiss release dates of films by my directors (TMDB)
- Cinémathèque suisse screenings with one of them present
- Festival dates + program/ticketing dates (hand-edited in `config.yaml`)

Rebuilt every Sunday by GitHub Actions, served by GitHub Pages.

## Setup (once)
1. Get a free TMDB API key: themoviedb.org → Settings → API.
2. Repo → Settings → Secrets and variables → Actions → New secret `TMDB_API_KEY`.
3. Repo → Settings → Pages → Source: *Deploy from a branch*, `main`, folder `/docs`.
4. Actions → *Update calendar* → *Run workflow*.
5. On your phone open `https://<user>.github.io/<repo>/` and tap **S'abonner**.

## Editing
Everything is in `config.yaml`. Festival entries marked `tentative: true` show
"(?)" in the title. When a festival announces its dates, fix them and drop the flag.
