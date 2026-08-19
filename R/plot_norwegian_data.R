#!/usr/bin/env Rscript
# =============================================================================
# plot_norwegian_data.R
# =============================================================================
# Reads all EBI Search raw JSON files (and the joined ENA file), filters for
# Norwegian entries, normalises institution names, and produces static ggplot2
# bar charts + norwegian_entries.csv under output/.
#
# The interactive dashboard is a SEPARATE app: shiny/app.R (run with
# `Rscript -e 'shiny::runApp("shiny")'`).  It reads the CSV this script writes.
#
# NOTE: the plot style (make_inst_palette, theme_nor, the grouped/dodged geom)
# is intentionally duplicated in shiny/app.R — that file must stay self-contained
# for the WebR/shinylive export.  Keep the two copies in sync.
# =============================================================================

suppressPackageStartupMessages({
  library(ggplot2)
  library(dplyr)
  library(tidyr)
  library(readr)
  library(purrr)
  library(tibble)
  library(stringr)
  library(forcats)
  library(lubridate)
  library(jsonlite)
  library(stringdist)
  library(scales)
  library(ggtext)
  library(patchwork)
  library(glue)
})

# Null-coalescing operator: fall back to `b` only when `a` is NULL or empty.
# (Pure container-safe coalesce — does NOT inspect a[[1]], so passing a list of
# fields whose first element happens to be empty returns the list unchanged.)
`%||%` <- function(a, b) {
  if (is.null(a) || length(a) == 0) return(b)
  a
}

#' Return the first present, non-empty, non-NA field value from `fields`,
#' trying `keys` in priority order; `default` if none match.
#' Used where several field names may carry the same information (e.g. a title
#' that may live in name / title / abstract depending on the domain).
pick_field <- function(fields, keys, default = NA_character_) {
  for (k in keys) {
    v <- fields[[k]]
    if (!is.null(v) && length(v) > 0) {
      v1 <- as.character(v[[1]])
      if (!is.na(v1) && nzchar(v1)) return(v1)
    }
  }
  default
}

#' NA for absent or empty scalars, so precedence rules that fall back on a
#' second field (e.g. broker before center) treat "" and NA alike.
blank_to_na <- function(x) {
  x <- as.character(x %||% NA_character_)
  if (length(x) == 0L || is.na(x[[1L]]) || !nzchar(x[[1L]])) NA_character_ else x[[1L]]
}

# ── Paths ─────────────────────────────────────────────────────────────────────
if (requireNamespace("here", quietly = TRUE)) {
  ROOT <- here::here()
} else {
  ROOT <- "."
}

RAW_DIR   <- file.path(ROOT, "data", "raw")
PROC_DIR  <- file.path(ROOT, "data", "processed")
INST_MAP  <- file.path(ROOT, "data", "institution_map.json")
DOMAINS_JSON     <- file.path(ROOT, "data", "domains.json")
IDENTIFIERS_JSON <- file.path(ROOT, "data", "identifiers_namespaces.json")
OUT_DIR   <- file.path(ROOT, "output")

# create OUT_DIR if it doesn't exist (recursively), avoid warning if it already exists
if (!dir.exists(OUT_DIR)) {
  dir.create(OUT_DIR, recursive = TRUE, showWarnings = FALSE)
}

# ── Domain labels (pretty names for plots) ────────────────────────────────────
DOMAIN_LABELS <- c(
  "bioimages"        = "BioImages",
  "biostudies-other" = "BioStudies",
  "metabolights"     = "MetaboLights",
  "pride"            = "PRIDE",
  "biomodels"        = "BioModels",
  "ega"              = "EGA Studies",
  "ega-sample"       = "EGA Samples",
  "ENA"              = "ENA Studies",
  "sra-sample"       = "ENA Samples"
)

# Non-SRA domains to read from raw/
STANDARD_DOMAINS <- names(DOMAIN_LABELS)[!names(DOMAIN_LABELS) %in% c("ENA", "sra-sample")]

# Domains with fewer total entries than this threshold are excluded from plots.
# Applied before faceting so small domains don't produce near-empty panels.
MIN_DOMAIN_ENTRIES <- 10L

# ── identifiers.org links ─────────────────────────────────────────────────────
# Map each `domain` value to its identifiers.org prefix(es).  The EBI/SRA domains
# carry `identifiers_prefix` in data/domains.json (the domain definitions); the
# joined ENA and EGA domains aren't in that dict, so they're supplemented here.
# NS_PATTERNS holds the registry validation pattern per prefix (cached by
# scripts/fetch_identifiers.py); an accession is only linked if it matches.
NS_PATTERNS <- local({
  if (!file.exists(IDENTIFIERS_JSON)) {
    message("  No identifiers cache (", IDENTIFIERS_JSON, ") – links disabled")
    return(list())
  }
  ns  <- fromJSON(IDENTIFIERS_JSON, simplifyVector = FALSE)$namespaces
  out <- list()
  for (p in names(ns)) {
    pat <- ns[[p]]$pattern
    if (!is.null(pat) && nzchar(pat)) out[[p]] <- pat
  }
  out
})

DOMAIN_IDENTIFIERS <- local({
  m <- list()
  if (file.exists(DOMAINS_JSON)) {
    dj <- fromJSON(DOMAINS_JSON, simplifyVector = FALSE)$domains
    for (k in names(dj)) {
      pfx <- dj[[k]]$identifiers_prefix
      if (!is.null(pfx)) m[[k]] <- unlist(pfx, use.names = FALSE)
    }
  }
  # Domains plotted under a `domain` value that isn't a DOMAINS key:
  if (!is.null(m[["sra-study"]])) m[["ENA"]] <- m[["sra-study"]]  # joined studies
  # EGA: try the type-specific prefix first (ega.study for EGAS, ega.dataset for
  # EGAD), then fall back to the generic `ega` namespace, which resolves any EGA
  # accession.  make_identifier_url() links the first prefix whose pattern
  # matches, so EGA Studies (EGAS) resolve via ega.study and EGA Samples (EGAN)
  # via the generic ega — the same list works for both domains.
  ega_prefixes      <- c("ega.study", "ega.dataset", "ega")
  m[["ega"]]        <- ega_prefixes   # EGAS studies
  m[["ega-sample"]] <- ega_prefixes   # EGAN samples (resolve via generic ega)
  m
})

#' Build a validated identifiers.org resolver URL for one accession, or NA.
#' Tries each candidate prefix for the domain and links the first whose registry
#' pattern matches, so a mis-mapped or malformed accession yields no link.
make_identifier_url <- function(accession, domain) {
  if (is.na(accession) || !nzchar(accession)) return(NA_character_)
  prefixes <- DOMAIN_IDENTIFIERS[[domain]]
  if (is.null(prefixes)) return(NA_character_)
  for (pfx in prefixes) {
    pat <- NS_PATTERNS[[pfx]]
    if (is.null(pat)) next
    # A malformed registry pattern must not crash the whole render.
    matched <- isTRUE(tryCatch(grepl(pat, accession, perl = TRUE),
                               error = function(e) FALSE))
    if (matched) return(sprintf("https://identifiers.org/%s:%s", pfx, accession))
  }
  NA_character_
}

# =============================================================================
# 1.  Institution normalisation
# =============================================================================

inst_map <- fromJSON(INST_MAP, simplifyDataFrame = FALSE)

# Build a lookup: pattern -> canonical name
build_pattern_df <- function(inst_list) {
  rows <- lapply(inst_list, function(i) {
    tibble(
      canonical = i$canonical,
      pattern   = i$patterns
    )
  })
  bind_rows(rows)
}

PATTERN_DF <- build_pattern_df(inst_map$institutions)
# Each indicator is wrapped in \b...\b so a bare place name like "Bergen" only
# matches as a whole word, not as a substring of an unrelated word (e.g. the
# plant genus "Bergenia").  Kept in sync with Python's build_geo_regex().
NORWAY_RE  <- paste(paste0("\\b", inst_map$norway_indicators, "\\b"), collapse = "|")

# Email-domain → canonical lookup built once from web_domain fields.
# Keys are base domains (e.g. "uio.no"); matching also handles sub-domains.
DOMAIN_LU <- local({
  cans <- sapply(inst_map$institutions, `[[`, "canonical")
  doms <- sapply(inst_map$institutions, function(i) {
    d <- i[["web_domain"]]
    if (is.null(d) || is.na(d) || !nzchar(d)) NA_character_ else tolower(trimws(d))
  })
  mask <- !is.na(doms)
  setNames(cans[mask], doms[mask])
})

# Canonical name → abbreviation lookup (e.g. "University of Oslo" → "UiO").
ABBREV_LU <- local({
  cans  <- sapply(inst_map$institutions, `[[`, "canonical")
  abbrs <- sapply(inst_map$institutions, `[[`, "abbrev")
  setNames(abbrs, cans)
})

# Pre-compute institution metadata arrays for batch normalisation (avoids extracting
# these on every normalise_institution() call; they are static per process).
INST_CANONICALS <- sapply(inst_map$institutions, `[[`, "canonical")
INST_CANONICALS_NO <- sapply(inst_map$institutions,
                             function(i) i$canonical_no %||% NA_character_)
INST_ABBREVS <- sapply(inst_map$institutions, `[[`, "abbrev")

# Pre-lowercase abbreviations for per-token Jaro-Winkler matching (branch 3).
INST_ABBREVS_LC <- tolower(INST_ABBREVS)

# Pre-lowercase both canonical names for full-string JW matching (branch 4).
INST_CANONICALS_LC <- tolower(INST_CANONICALS)
INST_CANONICALS_NO_LC <- tolower(INST_CANONICALS_NO)

# Map a canonical institution name to its abbreviation; leave unrecognised
# values (including "Other Norway") unchanged.  Guard the [[ ]] lookup: indexing
# a named atomic vector with an absent name errors ("subscript out of bounds"),
# which fires for every value not in the map (e.g. "Other Norway").
to_abbrev <- function(canonical) {
  if (is.null(canonical) || is.na(canonical) || !nzchar(canonical)) return(canonical)
  if (!canonical %in% names(ABBREV_LU)) return(canonical)
  ab <- ABBREV_LU[[canonical]]
  if (!is.null(ab) && !is.na(ab) && nzchar(ab)) ab else canonical
}

#' Robust EBI date parser.
#' Handles: "20230115" (YYYYMMDD compact, most EBI fields),
#'          "2023-01-15", "2023-01-15T00:00:00Z", "2023-01",
#'          "2019 Jan" (biostudies pub_date), "2023", Unix ms integers, NA/empty.
parse_ebi_date <- function(x) {
  if (is.null(x) || length(x) == 0 || is.na(x) || !nzchar(x)) return(NA_Date_)
  x <- trimws(as.character(x))
  # Unix milliseconds (13-digit number)
  if (grepl("^\\d{13}$", x)) return(as.Date(as.POSIXct(as.numeric(x) / 1000,
                                                       origin = "1970-01-01")))
  # Try lubridate with progressively looser formats.
  # "Ymd" covers both compact 20230115 and dash-separated 2023-01-15.
  # "Y b" covers "2019 Jan" style returned by biostudies pub_date.
  fmts <- c("Ymd HMS", "Ymd HM", "Ymd", "Y b", "Y-b", "Y-m", "Y/m/d",
            "d/m/Y", "d-m-Y", "d b Y", "b d Y", "Y")
  d <- lubridate::parse_date_time(x, orders = fmts, quiet = TRUE)
  if (is.na(d)) return(NA_Date_)
  d <- as.Date(d)
  if (d > Sys.Date() + lubridate::years(2)) return(NA_Date_)
  d
}

#' Normalise affiliation + email signals to a canonical institution name.
#'
#' Matching priority:
#'   1. Email domain lookup  (@uio.no → "University of Oslo", decisive signal)
#'   2. Regex pattern matching on combined affiliation text
#'   2b. Exact Norwegian-name match (canonical_no, e.g. "Universitetet i Oslo")
#'   3. Per-token Jaro-Winkler against abbreviations (catches "UiO"/"NTNU" in noisy strings)
#'   4. Full-string Jaro-Winkler against canonical AND canonical_no names
#'
#' canonical_no (the Norwegian-language institution name) is folded into both the
#' literal match (2b) and the fuzzy fallback (4); either way the English
#' `canonical` name is returned so the display value stays consistent.
#'
#' `context_vec` (e.g. the record's own `country` field) is appended only for
#' satisfying `(?=.*Norway|.*Norsk)` context guards on otherwise-ambiguous
#' patterns (see e.g. "Veterinary Institute"); it is never part of `affil`
#' itself, so it can't skew the fuzzy-match branches below.
#'
#' Returns the canonical institution name, or "Other Norway" if nothing matches.
normalise_institution <- function(affil_vec, email_vec = character(0),
                                  context_vec = character(0)) {

  # 1. Email domain lookup — highest confidence, very few false positives.
  valid_emails <- email_vec[!is.na(email_vec) & nzchar(email_vec)]
  for (em in valid_emails) {
    dom <- tolower(sub(".*@", "", trimws(em)))
    for (d in names(DOMAIN_LU)) {
      if (dom == d || endsWith(dom, paste0(".", d))) return(DOMAIN_LU[[d]])
    }
  }

  # 2. Regex pattern matching on affiliation text (+ context, for guards only).
  affil <- paste(affil_vec[!is.na(affil_vec)], collapse = " ")
  if (!nzchar(trimws(affil))) return("Other Norway")
  match_text <- paste(affil, paste(context_vec[!is.na(context_vec)], collapse = " "))
  for (i in seq_len(nrow(PATTERN_DF))) {
    if (grepl(PATTERN_DF$pattern[i], match_text, ignore.case = TRUE, perl = TRUE)) {
      return(PATTERN_DF$canonical[i])
    }
  }

  # Pre-computed at module load; no extraction needed per call.
  affil_lc <- tolower(affil)

  # 2b. Exact (case-insensitive, literal) match on the Norwegian name.
  for (i in seq_along(INST_CANONICALS_NO_LC)) {
    cno <- INST_CANONICALS_NO_LC[i]
    if (!is.na(cno) && nzchar(cno) && grepl(cno, affil_lc, fixed = TRUE)) {
      return(INST_CANONICALS[i])
    }
  }

  # 3. Per-token JW against abbreviations (abbreviation-length tokens only).
  tokens <- unlist(strsplit(affil, "[,;/()\\s]+"))
  tokens <- tokens[nchar(tokens) >= 2L & nchar(tokens) <= 8L]
  for (tok in tokens) {
    dist_abbr <- stringdist(tolower(tok), INST_ABBREVS_LC, method = "jw")
    best <- which.min(dist_abbr)
    if (dist_abbr[best] < 0.15) return(INST_CANONICALS[best])
  }

  # 4. Full-string JW against both English and Norwegian names; per institution
  #    take the better of the two, then return the English canonical.
  dist_en <- stringdist(affil_lc, INST_CANONICALS_LC,    method = "jw")
  dist_no <- stringdist(affil_lc, INST_CANONICALS_NO_LC, method = "jw")
  dist_full <- pmin(dist_en, dist_no, na.rm = TRUE)
  best_full <- which.min(dist_full)
  if (length(best_full) && dist_full[best_full] < 0.22) return(INST_CANONICALS[best_full])

  "Other Norway"
}

#' Return the single affiliation string most likely to have triggered the
#' institution match, for display in the affiliation column.
#'
#' Priority:
#'   1. Email address whose domain matched DOMAIN_LU
#'   2. First affil string that normalise_institution() resolves to a known institution
#'   3. First non-empty affil string (when only the fuzzy fallback fires or nothing matches)
#'
#' This avoids the old pipe-joined blob and lets the user see which piece of
#' metadata actually drove the institution assignment.
pick_affiliation <- function(affil_vec, email_vec = character(0)) {
  # 1. Email domain lookup — return the canonical institution name if it matched.
  for (em in email_vec[!is.na(email_vec) & nzchar(email_vec)]) {
    dom <- tolower(sub(".*@", "", trimws(em)))
    for (d in names(DOMAIN_LU)) {
      if (dom == d || endsWith(dom, paste0(".", d))) return(DOMAIN_LU[[d]])
    }
  }

  # 2. Try each affil string individually; return the first that matches.
  valid <- affil_vec[!is.na(affil_vec) & nzchar(affil_vec)]
  for (s in valid) {
    if (normalise_institution(s) != "Other Norway") return(s)
  }

  # 3. Nothing matched — return the first available string as raw fallback.
  if (length(valid) > 0) valid[[1L]] else NA_character_
}

# =============================================================================
# 2.  Load and flatten all domains
# =============================================================================
 
is_norwegian <- function(values) {
  any(grepl(NORWAY_RE, unlist(values), ignore.case = TRUE))
}
 
#' Parse a single entry from a standard (non-ENA) domain.
#'
#' Every entry reaching this function has already been confirmed Norwegian
#' by fetch_ebi_data.py, so no is_norwegian() guard is needed here.
#' Removing it prevents silent drops when the only Norwegian signal is in
#' a field like labhead_affiliation or submitter_email that the old check
#' did not include in all_text.
parse_entry <- function(entry, domain) {
  fields <- entry$fields %||% list()

  affil_field_names <- c(
    "affiliation", "submitter_affiliation",
    "labhead_affiliation",       # PRIDE lab head
    "labhead",                   # PRIDE lab head name (may carry affil)
    "organisation",              # BioStudies
    "center_name",
    "author",                    # BioImages, BioStudies
    "submitter",
    "first_author",              # BioModels, EGA
    "publication_authors"        # BioModels, EGA
  )

  country_field_names <- c("country", "submitter_country")

  email_field_names <- c(
    "submitter_mail",            # PRIDE, BioModels, EGA
    "submitter_email",           # MetaboLights
    "labhead_mail",              # PRIDE lab head
    "email"                      # EGA
  )

  affil_vals   <- unlist(fields[names(fields) %in% affil_field_names])
  country_vals <- unlist(fields[names(fields) %in% country_field_names])
  email_vals   <- unlist(fields[names(fields) %in% email_field_names])
  email_vals   <- email_vals[grepl("@", email_vals, fixed = TRUE)]

  # ── Date: walk known EBI date field names in priority order ──────────────
  # EBI returns dates as YYYYMMDD (compact) in most fields; parse_ebi_date()
  # handles that format along with ISO and other variants.
  date_raw <- NA_character_
  for (.df in c("submission_date", "creation_date", "pub_date",
                "publication_date", "last_modification_date",
                "collection_date", "release_date", "modified_date",
                "updated_date", "first_public_date")) {
    .v <- fields[[.df]]
    if (!is.null(.v) && length(.v) > 0 && nzchar(as.character(.v[[1]]))) {
      date_raw <- as.character(.v[[1]]); break
    }
  }
  parsed_date <- parse_ebi_date(date_raw)

  tibble(
    domain      = domain,
    accession   = entry$id %||% NA_character_,
    title       = pick_field(fields, c("name", "title", "abstract")),
    affiliation = pick_affiliation(affil_vals, email_vec = email_vals),
    country     = paste(country_vals, collapse = " | "),
    email       = if (length(email_vals) > 0) email_vals[[1]] else NA_character_,
    date        = parsed_date,
    year        = year(parsed_date),
    quarter     = quarter(parsed_date),
    month       = month(parsed_date),
    institution = to_abbrev(as.character(normalise_institution(
      affil_vals, email_vec = email_vals, context_vec = country_vals
    ))[1L]),
    # Non-ENA domains carry neither an ENA broker nor an ENA center; the two
    # are resolved into the displayed `broker` column in load_all_data().
    ena_broker  = NA_character_,
    ena_center  = NA_character_
  )
}

load_standard_domain <- function(domain) {
  path <- file.path(RAW_DIR, domain, "latest.json")
  if (!file.exists(path)) {
    message("  Skipping ", domain, " (no latest.json)")
    return(NULL)
  }
  message("Loading ", domain, " …")
  raw  <- fromJSON(path, simplifyDataFrame = FALSE)
  entries <- raw$entries %||% list()
  rows <- lapply(entries, parse_entry, domain = domain)
  bind_rows(Filter(Negate(is.null), rows))
}
 
#' Collapse a parsed ENA/sample tibble to one row per accession.
#'
#' Both the ENA Studies (ena_joined.json) and ENA Samples (sra-sample) sources
#' are assembled from partitioned fetches whose windows can overlap, so the same
#' accession may appear in several rows.  This guarantees each facet counts
#' unique entities regardless of any upstream deduplication.
#'
#' Per-column strategy:
#'   accession   – identity (the grouping key)
#'   date        – earliest non-NA date (first public)
#'   title       – first non-NA, non-empty value
#'   country     – union of all pipe-separated values across rows
#'   affiliation – union of all pipe-separated values across rows
#'   email       – first non-NA
#'   institution – majority vote across rows; ties broken by first occurrence
#'   ena_broker / ena_center – first non-empty value of EACH, independently, so
#'                  a duplicate row's center can never displace a broker seen
#'                  on another row (precedence is applied in load_all_data())
#'   domain      – kept as-is (constant within an accession)
#'   year/quarter/month – recomputed from the kept date
collapse_pipe <- function(x) {
  vals <- unlist(strsplit(x[!is.na(x) & nzchar(x)], " | ", fixed = TRUE))
  paste(unique(trimws(vals[nzchar(trimws(vals))])), collapse = " | ")
}

majority <- function(x) {
  x <- x[!is.na(x) & nzchar(x)]
  if (length(x) == 0L) return(NA_character_)
  names(sort(table(x), decreasing = TRUE))[1L]
}

dedupe_by_accession <- function(df) {
  df %>%
    group_by(accession) %>%
    summarise(
      domain      = first(domain),
      title       = first(title[!is.na(title) & nzchar(title)]) %||% NA_character_,
      affiliation = collapse_pipe(affiliation),
      country     = collapse_pipe(country),
      email       = first(email[!is.na(email)]) %||% NA_character_,
      date        = suppressWarnings(min(date, na.rm = TRUE)),
      institution = majority(institution),
      ena_broker  = first(ena_broker[!is.na(ena_broker) & nzchar(ena_broker)]) %||% NA_character_,
      ena_center  = first(ena_center[!is.na(ena_center) & nzchar(ena_center)]) %||% NA_character_,
      .groups     = "drop"
    ) %>%
    mutate(
      date        = if_else(is.infinite(date), as.Date(NA), date),
      institution = if_else(is.na(institution), "Other Norway", institution),
      year        = year(date),
      quarter     = quarter(date),
      month       = month(date),
    )
}

#' Parse a joined ENA row from ena_joined.json.
#'
#' join_ena.py now filters for Norwegian entries post-join, so every row
#' here is already confirmed Norwegian.  The is_norwegian() guard is removed
#' to avoid silent drops.  email is NA for ENA rows (not available post-join).
parse_ena_row <- function(row) {
  # join_ena.py outputs: accession, title, center_name, first_public_date,
  # sample_countries (list), sample_centers (list), sample_brokers (list),
  # n_experiments.
  affil_vals   <- c(row$center_name, unlist(row$sample_centers))
  country_vals <- unlist(row$sample_countries)

  # Broker: sra-study carries no broker_name of its own, so inherit the broker
  # from the study's joined samples (sample_brokers, surfaced by join_ena.py).
  # The study's own center_name is kept in a SEPARATE column rather than being
  # collapsed in here: broker-over-center precedence is applied once, in
  # load_all_data(), after dedupe_by_accession(), so a center can never
  # overwrite a broker at any stage.
  sample_brokers <- unlist(row$sample_brokers)
  sample_brokers <- sample_brokers[!is.na(sample_brokers) & nzchar(sample_brokers)]
  broker_val <- if (length(sample_brokers) > 0L) {
    sample_brokers[[1L]]
  } else {
    NA_character_
  }

  # first_public_date from sra-experiment (earliest across experiments for the study).
  # Format is YYYYMMDD compact — handled by parse_ebi_date().
  parsed_date <- parse_ebi_date(as.character(row$first_public_date %||% NA_character_))

  tibble(
    domain      = "ENA",
    accession   = row$accession    %||% NA_character_,
    title       = row$title        %||% NA_character_,
    affiliation = pick_affiliation(affil_vals),
    country     = paste(country_vals, collapse = " | "),
    email       = NA_character_,
    date        = parsed_date,
    year        = year(parsed_date),
    quarter     = quarter(parsed_date),
    month       = month(parsed_date),
    institution = to_abbrev(as.character(normalise_institution(
      affil_vals, context_vec = country_vals
    ))[1L]),
    # Raw sample centers, used for display when norwegian_submitter=FALSE (study
    # kept on sample signal, not submitter signal). Separated by pipe for consistency.
    sample_centers_str = paste(unlist(row$sample_centers), collapse = " | "),
    ena_broker  = broker_val,
    ena_center  = blank_to_na(row$center_name)
  )
}

load_ena <- function() {
  path <- file.path(PROC_DIR, "ena_joined.json")
  if (!file.exists(path)) {
    message("  Skipping ENA (no ena_joined.json – has join_ena.py been run?)")
    return(NULL)
  }
  message("Loading ENA joined …")
  raw  <- fromJSON(path, simplifyDataFrame = FALSE)
  rows <- lapply(raw$entries %||% list(), parse_ena_row)
  df   <- bind_rows(Filter(Negate(is.null), rows))

  if (nrow(df) == 0L) return(df)

  # Collapse to one row per study accession.  join_ena.py already produces one
  # row per study, but partitioned fetching can surface the same study in
  # multiple year windows; dedupe_by_accession() guarantees uniqueness.
  df <- dedupe_by_accession(df)

  message("  ENA: ", nrow(df), " unique studies after deduplication")
  df
}

#' Parse a single pre-filtered Norwegian sra-sample entry.
#' Produces a row comparable to parse_ena_row but at sample granularity.
parse_sra_sample <- function(entry) {
  f <- entry$fields %||% list()

  # broker_name: the ENA submitter / data broker (fetched field in sra-sample config).
  # center_name: the submitting center / research institution.
  # Both are tried for institution guessing; broker_name drives the broker
  # column, with center_name only as a fallback (resolved in load_all_data()).
  broker_name <- pick_field(f, c("broker_name"))
  center_name <- pick_field(f, c("center_name"))
  country_val <- pick_field(f, c("country"))
  affil_vals  <- Filter(function(x) !is.na(x) && nzchar(x),
                        c(center_name, broker_name))

  date_raw <- NA_character_
  for (.df in c("first_public_date", "collection_date", "last_updated_date")) {
    .v <- f[[.df]]
    if (!is.null(.v) && length(.v) > 0 && nzchar(as.character(.v[[1]]))) {
      date_raw <- as.character(.v[[1]]); break
    }
  }
  parsed_date <- parse_ebi_date(date_raw)

  tibble(
    domain      = "sra-sample",
    accession   = entry$id %||% NA_character_,
    title       = pick_field(f, c("alias", "description")),
    affiliation = pick_affiliation(affil_vals),
    country     = country_val %||% NA_character_,
    email       = NA_character_,
    date        = parsed_date,
    year        = year(parsed_date),
    quarter     = quarter(parsed_date),
    month       = month(parsed_date),
    institution = to_abbrev(as.character(normalise_institution(
      affil_vals, context_vec = country_val
    ))[1L]),
    # Carried separately (not collapsed to one value here) so the broker
    # survives dedupe_by_accession(): a duplicate row that has only a
    # center_name must not displace a broker_name seen on another row.
    ena_broker  = broker_name,
    ena_center  = center_name
  )
}

load_sra_samples <- function() {
  path <- file.path(RAW_DIR, "sra-sample", "latest.json")
  if (!file.exists(path)) {
    message("  Skipping ENA Samples (no sra-sample/latest.json – run fetch_ebi_data.py first)")
    return(NULL)
  }
  message("Loading ENA Samples (sra-sample) …")
  raw     <- fromJSON(path, simplifyDataFrame = FALSE)
  entries <- raw$entries %||% list()
  rows    <- lapply(entries, parse_sra_sample)
  df      <- bind_rows(Filter(Negate(is.null), rows))
  if (nrow(df) == 0L) return(df)

  # One row per sample accession.  sra-sample is fetched in partitioned windows
  # that can overlap, so dedupe_by_accession() guarantees unique samples.
  df <- dedupe_by_accession(df)
  message("  ENA Samples: ", nrow(df), " unique samples after deduplication")
  df
}

# ── Combine ───────────────────────────────────────────────────────────────────

load_all_data <- function() {
  standard_rows <- lapply(STANDARD_DOMAINS, load_standard_domain)
  ena_rows      <- load_ena()
  sample_rows   <- load_sra_samples()

  df <- bind_rows(c(standard_rows, list(ena_rows), list(sample_rows)))

  if (!"sample_centers_str" %in% names(df)) {
    df$sample_centers_str <- NA_character_
  }

  # The broker/center helpers only exist once an ENA source has been loaded;
  # add them so the resolution below works for any combination of sources.
  for (.col in c("ena_broker", "ena_center")) {
    if (!.col %in% names(df)) df[[.col]] <- NA_character_
  }

  if (nrow(df) == 0L) {
    return(tibble(
      domain = character(),
      domain_label = character(),
      accession = character(),
      title = character(),
      date = as.Date(character()),
      year = integer(),
      quarter = integer(),
      month = integer(),
      institution = character(),
      broker = character(),
      affiliation = character(),
      country = character(),
      email = character(),
      norwegian_submitter = logical(),
      identifier_url = character(),
      sample_centers_str = character()
    ))
  }

  df <- df %>%
    mutate(
      domain_label = DOMAIN_LABELS[domain],
      domain_label = if_else(is.na(domain_label), domain, domain_label),
      # Broker / Center precedence, applied once for the whole frame: a broker
      # (ENA broker_name, or the study's samples' broker_name) always wins, and
      # the center_name is used only when no broker is set.  Keeping the two
      # apart until here is what guarantees a center can never overwrite a
      # broker in parse_*() or in dedupe_by_accession().
      broker = if_else(!is.na(ena_broker) & nzchar(ena_broker),
                       ena_broker, ena_center),
      # Norwegian-submitter flag.  For ENA studies and ENA samples the `country`
      # field is the sample's geographic ORIGIN, not the submitter, so an entry
      # Norwegian only via country is a "Norwegian sample, foreign submitter"
      # case (flagged FALSE) that the dashboard can separate.  It counts as a
      # Norwegian submitter only when a submitter/affiliation field carries the
      # signal — i.e. a specific institution was resolved, or the affiliation /
      # broker text itself matches NORWAY_RE.  Non-ENA domains are submissions by
      # nature, so they are always TRUE.
      norwegian_submitter = if_else(
        domain %in% c("ENA", "sra-sample"),
        institution != "Other Norway" |
          grepl(NORWAY_RE, paste(coalesce(affiliation, ""), coalesce(broker, "")),
                ignore.case = TRUE),
        TRUE
      ),
      # Entries without a broker (non-ENA domains) get a label so the
      # broker colour mode can include them with a neutral category.
      broker       = if_else(is.na(broker) | !nzchar(broker), "Non-ENA", broker),
      # Validated identifiers.org resolver URL (NA when the accession matches no
      # namespace pattern); rendered as a clickable accession in the dashboard.
      identifier_url = mapply(make_identifier_url, accession, domain,
                              USE.NAMES = FALSE)
    ) %>%
    filter(!is.na(date)) %>%
    # For ENA entries with norwegian_submitter=FALSE (Norwegian sample, foreign
    # submitter), use the raw sample center names instead of the (failed)
    # normalized institution, so users see where the samples came from.
    mutate(
      institution = if_else(
        ("sample_centers_str" %in% names(.)) &
          domain == "ENA" & !norwegian_submitter &
          !is.na(sample_centers_str) & nzchar(sample_centers_str),
        sample_centers_str,
        institution
      )
    ) %>%
    # Drop helper columns after use.
    select(-any_of(c("sample_centers_str", "ena_broker", "ena_center")))
  df
}
 
 
# =============================================================================
# 3.  Plotting functions
# =============================================================================
 
# Distinct ColorBrewer palette, extended via interpolation when n > 12.
# "Other Norway", "Other", and "Non-ENA" are pinned to grey so they recede.
make_inst_palette <- function(values) {
  values     <- as.character(values)
  grey_keys  <- c("Other Norway", "Other", "Non-ENA")
  non_grey   <- sort(setdiff(values, grey_keys))
  n          <- length(non_grey)

  base_pal <- if (n <= 8) {
    RColorBrewer::brewer.pal(max(3L, n), "Set2")
  } else if (n <= 12) {
    RColorBrewer::brewer.pal(12L, "Set3")
  } else {
    colorRampPalette(
      c(RColorBrewer::brewer.pal(9,  "Set1"),
        RColorBrewer::brewer.pal(8,  "Set2"),
        RColorBrewer::brewer.pal(8,  "Dark2"))
    )(n)
  }

  pal <- setNames(base_pal[seq_along(non_grey)], non_grey)
  for (g in intersect(grey_keys, values)) pal[g] <- "#AAAAAA"
  pal
}
 
theme_nor <- function() {
  theme_classic(base_size = 13) +
    theme(
      plot.title      = element_markdown(face = "bold", size = 15),
      plot.subtitle   = element_markdown(colour = "grey40"),
      axis.text.x     = element_text(angle = 45, hjust = 1),
      legend.position = "bottom",
      legend.title    = element_text(face = "bold"),
      panel.grid.minor = element_blank(),
      strip.text      = element_text(face = "bold"),
      strip.background = element_rect(fill = "grey92", colour = NA)
    )
}
 
#' Grouped bar chart, faceted by domain, X axis = time at chosen granularity.
#'
#' @param df               data frame from load_all_data()
#' @param granularity      "year" | "quarter" | "month"
#' @param top_n_inst       keep this many fill values individually; rest → residual category
#' @param domains          character vector of domain_label values to include (NULL = all)
#' @param min_year         drop entries before this year
#' @param min_domain_entries exclude domains with fewer total entries than this threshold
#' @param color_by         "institution" (default) or "broker" (ENA center_name)
plot_time_by_domain <- function(df,
                                granularity        = c("year", "quarter", "month"),
                                top_n_inst         = 12L,
                                domains            = NULL,
                                min_year           = 2000L,
                                min_domain_entries = MIN_DOMAIN_ENTRIES,
                                color_by           = c("institution", "broker")) {
  granularity <- match.arg(granularity)
  color_by    <- match.arg(color_by)

  fill_col    <- color_by                          # column name in df
  other_label <- if (color_by == "institution") "Other Norway" else "Other"
  legend_name <- if (color_by == "institution") "Institution" else "ENA Broker / Center"

  d <- df
  if (!is.null(domains)) d <- d %>% filter(domain_label %in% domains)
  d <- d %>% filter(!is.na(date), year >= min_year, year <= year(Sys.Date()))

  # ── Drop domains below the entry threshold ────────────────────────────────
  domain_counts <- d %>% count(domain_label, name = "total")
  keep_domains  <- domain_counts %>%
    filter(total >= min_domain_entries) %>%
    pull(domain_label)
  dropped <- setdiff(domain_counts$domain_label, keep_domains)
  if (length(dropped) > 0)
    message("  Excluded (< ", min_domain_entries, " entries): ",
            paste(dropped, collapse = ", "))
  d <- d %>% filter(domain_label %in% keep_domains)

  if (nrow(d) == 0) {
    return(ggplot() +
             labs(title = "No domains meet the minimum entry threshold") +
             theme_void())
  }

  # ── Time axis ────────────────────────────────────────────────────────────
  d <- switch(granularity,
    year = d %>% mutate(
      time_val   = as.integer(year),
      time_label = as.character(year)
    ),
    quarter = d %>% mutate(
      time_val   = year + (quarter - 1) / 4,
      time_label = paste0(year, "\nQ", quarter)
    ),
    month = d %>% mutate(
      time_val   = year + (month - 1) / 12,
      time_label = format(date, "%Y\n%b")
    )
  )

  # ── Top-N lumping on the chosen fill column ──────────────────────────────
  top_vals <- d %>%
    count(.data[[fill_col]], sort = TRUE) %>%
    slice_head(n = top_n_inst) %>%
    pull(.data[[fill_col]])

  d <- d %>%
    mutate(fill_val = if_else(.data[[fill_col]] %in% top_vals,
                              .data[[fill_col]], other_label))

  # ── Aggregate ────────────────────────────────────────────────────────────
  counts <- d %>%
    count(domain_label, time_val, time_label, fill_val, name = "n") %>%
    mutate(fill_val = fct_reorder(fill_val, n, .fun = sum) %>%
             fct_relevel(other_label, after = 0L))

  pal      <- make_inst_palette(levels(counts$fill_val))
  x_labels <- counts %>% distinct(time_val, time_label) %>% arrange(time_val)

  # Grouped (dodged) bars — kept in sync with shiny/app.R (see header note).
  ggplot(counts, aes(x = time_val, y = n, fill = fill_val)) +
    geom_col(
      position = position_dodge2(padding = 0.1, preserve = "single"),
      width    = if (granularity == "year") 0.8 else
                 if (granularity == "quarter") 0.22 else 0.07
    ) +
    facet_wrap(~domain_label, scales = "free_y", ncol = 2) +
    scale_fill_manual(values = pal, name = legend_name) +
    scale_x_continuous(
      breaks = x_labels$time_val,
      labels = x_labels$time_label
    ) +
    scale_y_continuous(labels = comma, expand = expansion(mult = c(0, .05))) +
    guides(fill = guide_legend(nrow = 3, byrow = TRUE)) +
    labs(
      title    = "**Norwegian submissions to EBI repositories**",
      subtitle = glue(
        "Coloured by {color_by} · faceted by repository · granularity: {granularity}"
      ),
      x = NULL, y = "Number of entries"
    ) +
    theme_nor()
}
 
 
# =============================================================================
# 4.  Static output (used by GitHub Actions)
# =============================================================================
 
save_plots <- function(df) {
  message("Saving plots → ", OUT_DIR)
 
  for (gran in c("year", "quarter", "month")) {
    fname <- glue("norwegian_ebi_{gran}.png")
    ggsave(
      file.path(OUT_DIR, fname),
      plot_time_by_domain(df, granularity = gran),
      width = 18, height = 14, dpi = 150
    )
    message("  Saved ", fname)
  }
 
  df %>%
    select(domain, domain_label, accession, title, date, year,
           quarter, month, institution, broker, affiliation, country, email,
           norwegian_submitter, identifier_url) %>%
    readr::write_csv(file.path(OUT_DIR, "norwegian_entries.csv"))
 
  message("Done ✓  Files in ", OUT_DIR)
}
 
 
# =============================================================================
# 5.  Interactive Shiny app (local debugging)
# =============================================================================
# Loads data live from JSON files via load_all_data() — exercises the full
# parse/normalise pipeline, useful for debugging date parsing, institution
# matching, filter gaps, etc.  Unlike shiny/app.R (which reads the pre-built
# CSV), changes to parse_entry() / normalise_institution() are reflected
# immediately on reload.
#
# Launch:
#   SHINY=1 Rscript R/plot_norwegian_data.R
#   Rscript -e 'source("R/plot_norwegian_data.R"); shiny_app(load_all_data())'

shiny_app <- function(df) {
  for (pkg in c("shiny", "shinythemes", "DT")) {
    if (!requireNamespace(pkg, quietly = TRUE))
      stop("Package '", pkg, "' is required to run the interactive app: ",
           "install.packages('", pkg, "')")
  }
  library(shiny)
  library(shinythemes)
  library(DT)

  domain_choices  <- sort(unique(df$domain_label))
  year_range_data <- range(df$year, na.rm = TRUE)
  latest_date     <- max(df$date, na.rm = TRUE)

  ui <- fluidPage(
    theme = shinytheme("flatly"),
    titlePanel("Norwegian EBI Submissions (live data)"),

    sidebarLayout(
      sidebarPanel(
        width = 3,

        selectInput(
          "granularity", "Time granularity",
          choices  = c("Year" = "year", "Quarter" = "quarter", "Month" = "month"),
          selected = "year"
        ),

        sliderInput(
          "year_range", "Year range",
          min   = year_range_data[1],
          max   = year_range_data[2],
          value = c(max(year_range_data[1], year_range_data[2] - 10L),
                    year_range_data[2]),
          step  = 1L, sep = ""
        ),

        radioButtons(
          "color_by", "Colour bars by",
          choices  = c("Institution" = "institution", "ENA Broker / Center" = "broker"),
          selected = "institution", inline = TRUE
        ),

        sliderInput(
          "top_n_inst", "Top N values to show",
          min = 3, max = 30, value = 8L, step = 1
        ),

        sliderInput(
          "min_domain_entries", "Min entries per repository",
          min = 1, max = 100, value = MIN_DOMAIN_ENTRIES, step = 1
        ),

        checkboxGroupInput(
          "domains", "Repositories",
          choices  = domain_choices,
          selected = domain_choices
        ),

        hr(),
        # Dynamic fill-value checkboxes: rebuilt when color_by / top_n / year /
        # domain filters change.  All top-N values are pre-ticked by default.
        uiOutput("fill_checkbox"),

        hr(),
        p(em(paste("Latest entry:", format(latest_date, "%Y-%m-%d")))),
        p(em(paste(nrow(df), "Norwegian entries loaded from JSON")))
      ),

      mainPanel(
        width = 9,
        plotOutput("main_plot", height = "700px"),
        hr(),
        DT::dataTableOutput("entry_table")
      )
    )
  )

  server <- function(input, output, session) {

    # Filtered base (year + domain); institution lumping applied on top.
    base_df <- reactive({
      req(input$year_range, input$domains)
      df |>
        filter(domain_label %in% input$domains,
               !is.na(year),
               year >= input$year_range[1],
               year <= input$year_range[2])
    })

    # Top-N fill values for the current color_by mode, year, and domain window.
    # The residual sentinel ("Other Norway" / "Other") is always appended.
    top_fill_vals <- reactive({
      req(input$top_n_inst, input$color_by)
      col        <- input$color_by
      other_lbl  <- if (col == "institution") "Other Norway" else "Other"
      top_vals   <- base_df() |>
        count(.data[[col]], sort = TRUE) |>
        slice_head(n = input$top_n_inst) |>
        pull(.data[[col]])
      unique(c(top_vals, other_lbl))
    })

    output$fill_checkbox <- renderUI({
      vals  <- top_fill_vals()
      label <- if (input$color_by == "institution") "Show institutions"
               else "Show brokers / centers"
      checkboxGroupInput("selected_fill", label,
                         choices = vals, selected = vals)
    })

    output$main_plot <- renderPlot({
      req(input$year_range, input$top_n_inst, input$color_by)

      col       <- input$color_by
      other_lbl <- if (col == "institution") "Other Norway" else "Other"

      # Lump the fill column to top-N, then apply checkbox filter.
      # plot_time_by_domain is called with top_n_inst = 999 to skip re-lumping.
      top_vals <- setdiff(top_fill_vals(), other_lbl)
      d <- base_df() |>
        mutate(across(all_of(col),
                      ~ if_else(.x %in% top_vals, .x, other_lbl)))

      if (!is.null(input$selected_fill) && length(input$selected_fill) > 0)
        d <- d |> filter(.data[[col]] %in% input$selected_fill)

      plot_time_by_domain(
        d,
        granularity        = input$granularity,
        top_n_inst         = 999L,
        color_by           = col,
        min_domain_entries = input$min_domain_entries
      )
    }, res = 120)

    output$entry_table <- DT::renderDataTable({
      req(input$year_range, input$domains)
      keep <- base_df() |>
        count(domain_label, name = "total") |>
        filter(total >= input$min_domain_entries) |>
        pull(domain_label)
      base_df() |>
        filter(domain_label %in% keep) |>
        mutate(accession = ifelse(
          is.na(identifier_url) | !nzchar(identifier_url), accession,
          sprintf('<a href="%s" target="_blank" rel="noopener">%s</a>',
                  identifier_url, accession))) |>
        select(
          Repository  = domain_label,
          Accession   = accession,
          Title       = title,
          Date        = date,
          Institution = institution,
          Broker      = broker,
          Email       = email
        ) |>
        arrange(desc(Date))
      # Escape every column by name except Accession, which holds the link HTML.
      # (Column names avoid the rownames-offset ambiguity of numeric indices.)
    }, options = list(pageLength = 10, scrollX = TRUE), filter = "top",
       escape = c("Repository", "Title", "Date", "Institution", "Broker", "Email"))
  }

  shinyApp(ui, server)
}


# =============================================================================
# 6.  Entry point
# =============================================================================
# Default:   Rscript R/plot_norwegian_data.R          → saves static PNGs + CSV
# Shiny:     SHINY=1 Rscript R/plot_norwegian_data.R  → launches interactive app

main <- function() {
  df <- load_all_data()
  message(glue("Loaded {nrow(df)} Norwegian entries across {n_distinct(df$domain)} domains"))

  if (nrow(df) == 0) {
    message("No data found – have you run fetch_ebi_data.py yet?")
    return(invisible(NULL))
  }

  if (identical(Sys.getenv("SHINY"), "1")) {
    shiny_app(df)
  } else {
    save_plots(df)
  }
}

main()
