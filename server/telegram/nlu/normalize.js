"use strict";

// Text normalization shared by every part of the rule-based NLU layer (server/telegram/nlu/) -
// no stemmer, no ML, just the handful of normalizations Russian free text about car brands,
// people's names and engineering stages actually needs here.

/** lowercase, ё/Ё -> е, collapse whitespace, trim. Keeps letters/digits/hyphen/dot (article
 *  numbers like "333.0544.1" need the dot and hyphen-joined model names need the hyphen). */
function normalizeText(s) {
  return String(s || "")
    .toLowerCase()
    .replace(/ё/g, "е")
    .replace(/[^\p{L}\p{N}\s.\-]/gu, " ")
    .replace(/\s+/g, " ")
    .trim();
}

/** Word tokens (letters/digits only, hyphen/dot kept inside a token e.g. "333.0544.1"), each
 *  trimmed of any leading/trailing punctuation normalizeText kept around (a sentence-ending "."
 *  on the last word, in particular - normalizeText itself must keep "." for article numbers, so
 *  the trim has to happen per token here instead). */
function tokenize(s) {
  return normalizeText(s).split(/\s+/)
    .map((t) => t.replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, ""))
    .filter(Boolean);
}

// Russian case endings common on first names/surnames/adjectives, longest first so "налась"-style
// multi-letter endings are tried before a shorter one that would also match. This is a heuristic,
// not a morphological analyser - good enough to match "Калиткина" against a stored "Калиткин",
// not to decline arbitrary words correctly.
const CASE_ENDINGS = ["ами", "ями", "ого", "его", "ему", "ому", "ыми", "ова", "ева", "ина", "ына",
  "ой", "ей", "ым", "им", "ом", "ах", "ях", "ы", "и", "а", "я", "у", "ю", "е", "о", "й"];

/** Strip at most one trailing case ending (if the remaining stem is still at least 3 letters) -
 *  used only to compare two Russian words loosely, never to answer anything by itself. */
function stem(word) {
  const w = String(word || "");
  for (const end of CASE_ENDINGS) {
    if (w.length - end.length >= 3 && w.endsWith(end)) return w.slice(0, -end.length);
  }
  return w;
}

/** True if `query` plausibly refers to the same word as `stored` (either direction's stem is a
 *  prefix of the other) - e.g. query "калиткина" vs stored "калиткин". Both sides normalized
 *  first, so case/ё differences never matter. */
function looseWordMatch(query, stored) {
  const qn = normalizeText(query);
  const sn = normalizeText(stored);
  if (!qn || !sn) return false;
  if (qn.includes(sn) || sn.includes(qn)) return true; // substrings - article numbers, model codes
  const a = stem(qn);
  const b = stem(sn);
  return a === b || a.startsWith(b) || b.startsWith(a);
}

/** True if every space-separated word of `query` loosely matches at least one word of `text`
 *  (order-independent) - the general "does this free-text search term appear in this field"
 *  check reused by project and person search. */
function looseContains(text, query) {
  const hayWords = tokenize(text);
  const needleWords = tokenize(query);
  if (!needleWords.length) return false;
  return needleWords.every((n) => hayWords.some((h) => looseWordMatch(n, h)));
}

module.exports = { normalizeText, tokenize, stem, looseWordMatch, looseContains };
