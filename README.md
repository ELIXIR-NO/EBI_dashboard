# Norwegian EBI Submissions Dashboard

> **Weekly-updated tracker of Norwegian research data deposited in EBI repositories.**  
> Data is fetched automatically via GitHub Actions, institution names are normalised
> against a curated list, and bar plots are rendered with R/ggplot2 (Shiny-ready).

---

## Repositories tracked

| Repository | EBI domain | Primary affiliation fields |
|---|---|---|
| BioImages | `bioimages` | `author`
| BioStudies | `biostudies-other` | `organisation` |
| MetaboLights | `metabolights` | `submitter_affiliation` |
| PRIDE | `pride` | `submitter_affiliation`, `submitter_country` |
| BioModels | `biomodels` | `submitter_affiliation` |
| ENA/SRA | `sra-study`, `sra-experiment`, `sra-sample` | `center_name`, `country` (sample) |
| EGA | `ega`, `ega-sample` (EGA Metadata API, not EBI Search) | DAC contact `institution_name`, `email` |

> **EGA** is fetched from the separate [EGA Public Metadata API](https://metadata.ega-archive.org)
> rather than EBI Search (which exposes no dates/affiliations for it).  Data Access
> Committees (DACs) are filtered to Norwegian ones — by contact `institution_name`
> or by email **domain** (`.no` or a known institution `web_domain`) — and the
> records below the matching DACs are plotted:
> - `ega` — **studies** (DAC → datasets → studies).
> - `ega-sample` — **samples** (DAC → datasets → samples), mirroring `ENA Samples`.
>   Samples have no public date/affiliation, so each inherits the parent dataset's
>   release date and the parent DAC's institution/email.

---

## Architecture

The pipeline itself is a Snakemake DAG (`workflow/Snakefile`) that runs on a
compute server.  GitHub Actions only starts it and publishes the results — see
[GitHub Actions workflows](#github-actions-workflows).

```
snakemake --profile workflow/profiles/local   (on the compute server,
                                               launched by launch.yml)
│
├─ scripts/fetch_ebi_data.py
│    ├─ Queries EBI Search REST API  (GET /ws/rest/{domain}?query=Norway…)
│    ├─ Paginates until all hits retrieved (PAGE_SIZE = 500)
│    ├─ Backfills sra-sample broker_name via scripts/ena_portal.py
│    │    (EBI Search indexes the field but will not return it)
│    └─ Writes data/raw/{domain}/latest.json  (+ dated snapshot)
│
├─ scripts/join_ena.py
│    ├─ Loads sra-study, sra-experiment, sra-sample
│    ├─ Joins studies → experiments → samples via shared accession keys
│    └─ Writes data/processed/ena_joined.json
│
├─ scripts/fetch_ega.py
│    ├─ Queries EGA Public Metadata API  (GET metadata.ega-archive.org/dacs…)
│    ├─ Keeps Norwegian DACs (institution_name regex / email .no|web_domain)
│    ├─ Walks DAC → datasets → studies + samples
│    └─ Writes data/raw/ega/latest.json + data/raw/ega-sample/latest.json
│       (same {id, fields} shape as the EBI Search domains)
│
├─ scripts/fetch_identifiers.py
│    ├─ Caches the identifiers.org namespace registry (prefix → pattern)
│    └─ Writes data/identifiers_namespaces.json
│
└─ R/plot_norwegian_data.R
     ├─ Loads all latest.json + ena_joined.json
     ├─ Filters rows mentioning Norway/Norge/Norwegian in affil/country fields
     ├─ Normalises institution names (regex → fuzzy fallback → "Other Norway")
     ├─ Builds identifiers.org links (validated against the registry pattern)
     └─ Saves PNG plots + norwegian_entries.csv to output/
```

### Accession links (identifiers.org)

Each domain declares an `identifiers_prefix` in its definition (e.g. `pride` →
`pride.project`, `sra-study` → `["insdc.sra", "bioproject"]`).  The render step
checks every accession against that prefix's [identifiers.org](https://identifiers.org)
registry pattern and, on a match, builds a `https://identifiers.org/<prefix>:<acc>`
link (an unmatched or mis-mapped accession simply gets no link).  These render as
clickable accessions in the Shiny dashboard's table.  EGA samples (`EGAN…`) have
no identifiers.org namespace, so they are left unlinked.

---

## Output plots

| File | Description |
|---|---|
| `output/norwegian_ebi_year.png` | Entries over time, faceted by repository and coloured by institution — yearly resolution |
| `output/norwegian_ebi_quarter.png` | Same plot, quarterly resolution |
| `output/norwegian_ebi_month.png` | Same plot, monthly resolution |
| `output/norwegian_entries.csv` | Flat export of all matched entries (also the Shinylive app's data source) |

---

## Institution normalisation

Institution names in EBI metadata are free text – a single lab may appear as
`"UiO"`, `"University of Oslo"`, `"Universitetet i Oslo"`, `"Univ. of Oslo"`, etc.

Normalisation happens in three stages (see `data/institution_map.json`):

1. **Regex matching** – each institution has a list of case-insensitive Perl
   regex patterns covering abbreviations, Norwegian/English spellings, and
   common misspellings.
2. **Fuzzy fallback** – if no regex matches, the affiliation string is compared
   against all canonical names and abbreviations using Jaro-Winkler distance
   (`stringdist`).
3. **"Other Norway"** – entries where Norway can be detected (city names,
   country field = NO, etc.) but no institution is matched.

### Adding an institution

Edit `data/institution_map.json` and add an entry:

```json
{
  "canonical": "My Institute",
  "canonical_no": "Mitt Institutt",
  "abbrev": "MI",
  "ror": "https://ror.org/...",
  "patterns": [
    "My Institute",
    "Mitt Institutt",
    "\\bMI\\b"
  ]
}
```

---
## OS Dependencies

On Ubuntu 26.04 LTS

```bash

sudo apt-get install r-base build-essential pkg-config git make \
libcurl4-openssl-dev libssl-dev libxml2-dev zlib1g-dev libicu-dev \
libfontconfig1-dev libfreetype6-dev libpng-dev libtiff5-dev libjpeg-dev \
libharfbuzz-dev libfribidi-dev libcairo2-dev libpango1.0-dev libx11-dev libxt-dev \
libblas-dev liblapack-dev libopenblas-dev gfortran libgfortran5 \
liblzma-dev libbz2-dev libreadline-dev libsqlite3-dev libpq-dev \
libgit2-dev libgmp-dev libglpk-dev imagemagick

```



## Running locally

### 1. Fetch data

```bash
pip install requests tenacity
python scripts/fetch_ebi_data.py
python scripts/join_ena.py
```

### 2. Render plots (static)

```r
Rscript R/plot_norwegian_data.R
# → output/*.png  +  output/norwegian_entries.csv
```

### 3. Launch Shiny app

Two ways to run the interactive dashboard:

**A. Quick / debugging** — loads directly from the raw JSON files, so changes to
parsing or institution normalisation are reflected immediately on reload:

```bash
SHINY=1 Rscript R/plot_norwegian_data.R
```

**B. Production / shinylive** — reads the pre-built CSV, identical to what is
deployed via shinylive. Run the render step first:

```bash
cp output/norwegian_entries.csv shiny/data/norwegian_entries.csv
Rscript -e 'shiny::runApp("shiny")'
```

Both apps expose the same controls:
- Time granularity (year / quarter / month)
- Year range (default: last 10 years)
- Top N institutions (default: 8), with per-institution checkboxes
- Repository filter (checkboxes) and minimum entries per repository

---

## GitHub Actions workflows

The heavy lifting happens on a compute server, not on the GitHub runner: the
fetch stage downloads tens of millions of records and would blow past the hosted
runner's time and disk limits.  Actions therefore does two small jobs — start
the run over SSH, and publish the results once it finishes.

| Workflow | Trigger | Description |
|---|---|---|
| `launch.yml` | cron Mon 03:00 UTC + push to `main` + manual | Syncs the server to `origin/main`, clears derived artefacts, fires Snakemake detached over SSH |
| `publish.yml` | cron every 3 h + manual | Polls for the `output/.run_complete` sentinel; on finding it, validates the results, copies them back, deploys the Shinylive app to GitHub Pages and commits the artefacts to `main` |

### Push triggers and from-scratch runs

A push to `main` starts a run immediately.  What survives that run depends on
what was pushed:

| Pushed paths | Behaviour |
|---|---|
| `scripts/**`, `workflow/**`, `requirements.txt`, `data/institution_map.json` | **From scratch** — `data/raw/` (partitions + manifests), `data/processed/`, `data/domains.json` and `data/identifiers_namespaces.json` are wiped on the server, so every domain is re-downloaded |
| `R/**`, `shiny/**` | Normal incremental run; the fetch cache is reused |
| anything else (`README.md`, `tests/**`, `output/**`) | No run |

The first group is the code that *writes* the persistent cache.  Partitions for
years ≤ `current_year - 2` are otherwise served straight off disk and only
re-fetched when their sha256 or `FILTER_VERSION` check fails, so a change to the
filtering or pagination logic that forgets to bump `FILTER_VERSION` would be
masked by stale partitions indefinitely.  Wiping the cache removes that failure
mode.  The same rebuild can be requested by hand with the `from_scratch` input
on a manual dispatch.

`publish.yml` commits the run's own results back to `main`; those pushes must
not start another run.  Three things prevent it: none of the paths it commits
(`output/`, `data/processed/`, `data/domains.json`,
`data/identifiers_namespaces.json`) match `launch.yml`'s `paths:` filter; a
job-level guard skips pushes from `github-actions[bot]`; and pushes made with
the default `GITHUB_TOKEN` do not start workflows at all.

### Requirements

`publish.yml` needs the default `GITHUB_TOKEN` with **write** access to
`contents` and `pages` (check repo Settings → Actions → General if you have
issues), plus GitHub Pages set to deploy from Actions.  Both workflows need
these secrets for the SSH connection to the compute server:

| Secret | Meaning |
|---|---|
| `SSH_PRIVATE_KEY` | Private half of the key pair authorised on the server |
| `REMOTE_HOST` | Server hostname or IP |
| `REMOTE_USER` | Linux user on the server |
| `REMOTE_WORKDIR` | Absolute path to the repo checkout on the server |

---

## ENA join logic

ENA data is fetched from three EBI Search sub-tables and joined by
`join_ena.py`:

```
sra-study  ←─ study_accession ─→  sra-experiment
sra-experiment ←─ sample_accession ─→  sra-sample
```

The joined row uses the **study** as its unit; sample-level country
information (best source for Norway detection) is propagated upward.

Three sub-tables are intentionally omitted:

- **sra-submission** (31M entries, no searchable date field — cannot be year-partitioned)
- **sra-analysis** (24M entries, date field mismatch)
- **sra-run** (42M entries, no searchable date field — was only used for run counts)

### Broker attribution

The dashboard's **Broker** column shows the ENA `broker_name` (e.g.
`ELIXIR-Norway`) and falls back to `center_name` — the depositing institution —
only when a record has no broker.

A broker is only ever read from the record it belongs to: a **study** shows its
own `broker_name`, a **sample** shows its own. A sample's broker is *not*
promoted onto its study. That inheritance used to be the only source of a study
broker, and it was wrong in both directions — it labelled ~730 studies `NCBI` or
`DDBJ`, which is INSDC mirroring provenance (the archive a sample record was
copied from) rather than a submission broker, while the studies ENA genuinely
brokers as `ELIXIR-Norway` showed nothing at all.

`sample_brokers` is still carried in `ena_joined.json`: it is a Norwegian
identity signal for the join's filter (`ELIXIR-Norway` contains "Norway"), it
just no longer decides a study's displayed broker.

EBI Search's `sra-sample` domain marks `broker_name` searchable and facetable
but **not retrievable**, so the field is absent from every entry the fetch
saves, whatever ENA actually holds. Left alone, the fallback fires everywhere
and the dashboard shows the institution where the broker belongs.
`scripts/ena_portal.py` therefore backfills `broker_name` from ENA's Portal
API — a different service, which returns the field cleanly and separately from
`center_name`. It runs in two places:

- `fetch_ebi_data.py`, over `sra-sample` before the raw JSON is written
  (this is the table the dashboard renders directly), and
- `join_ena.py`, over the samples feeding the joined study table.

The same module supplies each joined study's own `broker_name` via
`fetch_study_attribution()`, which EBI Search's sra-study domain exposes not at
all. That call also returns the study's `center_name` and `first_public` date,
both likewise missing from EBI Search: `center_project_name` (what the join
reads as `center_name`) is empty for every study in practice, so without it the
centre fallback had nothing to fall back to, and a study whose experiments all
dropped out of the join had no date and was silently discarded by the render.

The backfill is best-effort: batches that fail over the network are logged and
skipped, and the pipeline continues with whatever attribution it already had.

---

## Data refresh cadence

A full run starts every Monday at 03:00 UTC, and on any push to `main` that
touches pipeline code.  To change the schedule, edit the `cron` line in
`.github/workflows/launch.yml`.  `publish.yml` polls every 3 hours for a
finished run, so results appear within 3 hours of the pipeline completing.

Within a run the fetch stage is incremental: years ≤ `current_year - 2` are
served from sha256-verified partition files under
`data/raw/<domain>/partitions/`, while the current and previous year are always
re-fetched.  See [Push triggers and from-scratch
runs](#push-triggers-and-from-scratch-runs) for when that cache is discarded.

Nothing under `data/raw/` is committed — it is excluded wholesale by
`.gitignore` to keep the repository lean, and lives only on the compute server.
The artefacts that *are* committed back to `main` by `publish.yml` are
`data/processed/ena_joined.json`, `data/domains.json`,
`data/identifiers_namespaces.json` and `output/`.
