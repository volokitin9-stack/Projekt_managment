"use strict";

// Kanban stage recognition for the NLU layer - the same 5 stage keys/labels
// server/telegram/personalBot.js already hardcodes (STAGE_LABELS), matched here by a Russian word
// *root* rather than the exact label, so every grammatical form of the stage name is recognised:
// "замере"/"замер"/"замеряем" all contain "замер"; "разрабатываем"/"разработка"/"разработке" all
// contain "разработ". This is a fixed, tiny lookup table, not a real morphological analyser - see
// docs/TELEGRAM_ASSISTANT.md "Limitations".
const { normalizeText } = require("./normalize");

const STAGE_ROOTS = {
  scanning: ["сканир"],
  development: ["разработ", "разрабат"],
  prototype: ["образец", "образц", "прототип"],
  measurement: ["замер"],
  documentation: ["оформ"]
};

const STAGE_LABELS = { scanning: "Сканирование", development: "Разработка", prototype: "Тестовый образец",
  measurement: "Замер", documentation: "Оформление" };

/** First stage whose root appears anywhere in `text`, or null. Checked in a fixed order so a
 *  sentence mentioning two stage words (unlikely in practice) is still deterministic. */
function extractStage(text) {
  const n = normalizeText(text);
  for (const key of Object.keys(STAGE_ROOTS)) {
    if (STAGE_ROOTS[key].some((root) => n.includes(root))) return key;
  }
  return null;
}

module.exports = { STAGE_ROOTS, STAGE_LABELS, extractStage };
