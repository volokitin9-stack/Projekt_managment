"use strict";

// Car-manufacturer synonyms for the Telegram NLU layer - maps a Russian transliteration (any case,
// ё/е-insensitive) to the canonical Latin manufacturer name as it is actually stored in project
// data (server/seed.js, real plan imports). Not exhaustive: a manufacturer missing from this list
// still matches literally (a bare "BAIC" still finds manufacturer "BAIC") - this list only adds the
// Russian spellings a literal match would otherwise miss.
const { normalizeText, tokenize } = require("./normalize");

const BRANDS = [
  { canonical: "GEELY", variants: ["geely", "джили"] },
  { canonical: "HAVAL", variants: ["haval", "хавал", "хаваль"] },
  { canonical: "BAW", variants: ["baw", "бав", "баw"] },
  { canonical: "CHERY", variants: ["chery", "чери", "черри"] },
  { canonical: "LADA", variants: ["lada", "лада", "ваз"] },
  { canonical: "TOYOTA", variants: ["toyota", "тойота"] },
  { canonical: "CHANGAN", variants: ["changan", "чанган", "чаньган"] },
  { canonical: "AUDI", variants: ["audi", "ауди"] },
  { canonical: "BMW", variants: ["bmw", "бмв"] },
  { canonical: "EXEED", variants: ["exeed", "эксид", "эксиид"] },
  { canonical: "OMODA", variants: ["omoda", "омода"] },
  { canonical: "JAC", variants: ["jac", "джак"] },
  { canonical: "FAW", variants: ["faw", "фав"] },
  { canonical: "GREAT WALL", variants: ["great wall", "greatwall", "грейт волл", "гретвол"] },
  { canonical: "SOUEAST", variants: ["soueast", "соуист"] },
  { canonical: "VOLKSWAGEN", variants: ["volkswagen", "вольксваген", "фольксваген", "vw"] },
  { canonical: "HYUNDAI", variants: ["hyundai", "хендай", "хундай"] },
  { canonical: "KIA", variants: ["kia", "киа"] },
  { canonical: "NISSAN", variants: ["nissan", "ниссан"] },
  { canonical: "RENAULT", variants: ["renault", "рено"] }
];

const VARIANT_INDEX = new Map();
let maxVariantWords = 1;
BRANDS.forEach(({ canonical, variants }) => {
  variants.concat(canonical.toLowerCase()).forEach((v) => {
    const key = normalizeText(v);
    VARIANT_INDEX.set(key, canonical);
    maxVariantWords = Math.max(maxVariantWords, key.split(/\s+/).length);
  });
});

/** The canonical manufacturer name for one word/phrase exactly as spelled, or null. */
function resolveBrand(text) {
  const n = normalizeText(text);
  return VARIANT_INDEX.get(n) || null;
}

/** Scans `text` token by token (so a sentence-ending "." or "?" next to the brand word never
 *  breaks the match) for the first known brand mention - multi-word variants ("great wall") tried
 *  before single words - and returns {canonical, matchedText}, or null. A brand NOT in this
 *  dictionary is still searchable, just via the caller's own plain substring match against the
 *  manufacturer field (see projectsQuery.js), not through this function. */
function extractBrand(text) {
  const tokens = tokenize(text);
  for (let wlen = maxVariantWords; wlen >= 1; wlen -= 1) {
    for (let i = 0; i + wlen <= tokens.length; i += 1) {
      const phrase = tokens.slice(i, i + wlen).join(" ");
      if (VARIANT_INDEX.has(phrase)) return { canonical: VARIANT_INDEX.get(phrase), matchedText: phrase };
    }
  }
  return null;
}

module.exports = { BRANDS, resolveBrand, extractBrand };
