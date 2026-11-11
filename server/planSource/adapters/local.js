"use strict";

// Local filesystem source: a file, or a folder with exactly one planning file.
// A mounted network share (SMB/NFS) is used through this adapter, too.
//
// Safe-read rules:
// * Office lock files (~$name.xlsx) and temporary/partial files (.tmp, .part,
//   .crdownload, hidden files) are never read.
// * A file is read only once its size and modification time have not changed
//   for PLAN_SOURCE_SETTLE_SECONDS, and it is copied while being hashed. If the
//   size or modification time changed during the copy, the copy is discarded
//   ("still being written") and the next check tries again.
// * The original file is only read, never modified, moved or locked.

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const { SourceError } = require("./errors");

const PLAN_EXTENSIONS = [".xlsx", ".xlsm", ".xls", ".csv"];
const TEMP_PATTERNS = [/^~\$/, /^\.~/, /^\./, /\.(tmp|part|partial|crdownload|download|swp)$/i, /~$/];

function isTempName(name) {
  return TEMP_PATTERNS.some((re) => re.test(name));
}

function globToRegex(glob) {
  const esc = glob.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".");
  return new RegExp(`^${esc}$`, "i");
}

function mapFsError(e, where) {
  if (e instanceof SourceError) return e;
  if (e.code === "ENOENT") return new SourceError("not_found", `не найдено: ${where}`);
  if (e.code === "EACCES" || e.code === "EPERM") return new SourceError("permission_denied", `нет доступа: ${where}`);
  if (e.code === "EBUSY" || e.code === "ETXTBSY") return new SourceError("locked", `файл занят другой программой: ${where}`);
  if (["EIO", "ENOTCONN", "EHOSTDOWN", "ETIMEDOUT", "ESTALE", "ENXIO", "ENODEV"].includes(e.code)) {
    return new SourceError("unavailable", `расположение недоступно (${e.code}): ${where}`);
  }
  return new SourceError("unavailable", `${e.code || e.name}: ${e.message}`);
}

class LocalFileSource {
  /**
   * @param {object} o
   * @param {string} o.path      file or folder; relative paths are resolved against baseDir
   * @param {string} o.baseDir   directory for relative paths (the application folder)
   * @param {string} [o.pattern] file-name pattern in folder mode, e.g. "AfM__ISOEM_*.xlsx"
   * @param {number} [o.settleSeconds] minimum age of the last modification
   */
  constructor(o) {
    if (!o.path) throw new SourceError("not_configured", "PLAN_SOURCE_PATH не задан");
    this.configured = o.path;
    // A Windows network path (\\server\share\...) can be opened directly only on Windows.
    this.unc = /^(\\\\|\/\/)[^\\/]/.test(o.path);
    this.platform = o.platform || process.platform;
    this.full = path.resolve(o.baseDir || process.cwd(), o.path);
    this.pattern = o.pattern ? globToRegex(o.pattern) : null;
    this.settleMs = Math.max(0, Number(o.settleSeconds == null ? 5 : o.settleSeconds)) * 1000;
    this.now = o.now || (() => Date.now());
  }

  describe() {
    return { type: "local", location: this.configured, pattern: this.pattern ? String(this.pattern) : null };
  }

  /**
   * A configured path without a plan-file extension that does not exist as such
   * (e.g. "...\\AfM СIS+OEM 2026") is resolved to <path>.xlsx / .xlsm / .xls / .csv,
   * provided exactly one of them exists.
   */
  async resolvePath() {
    if (this.unc && this.platform !== "win32") {
      throw new SourceError("unavailable", `сетевой путь ${this.configured} открывается напрямую только на Windows; ` +
        "на Linux смонтируйте папку (CIFS) и укажите в PLAN_SOURCE_PATH путь монтирования");
    }
    try {
      return { file: this.full, st: await fs.promises.stat(this.full) };
    } catch (e) {
      if (e.code !== "ENOENT" || PLAN_EXTENSIONS.includes(path.extname(this.full).toLowerCase())) {
        throw mapFsError(e, this.configured);
      }
    }
    const found = [];
    for (const ext of PLAN_EXTENSIONS) {
      try {
        found.push({ file: this.full + ext, st: await fs.promises.stat(this.full + ext) });
      } catch (e) {
        if (e.code !== "ENOENT") throw mapFsError(e, this.configured + ext);
      }
    }
    if (!found.length) throw new SourceError("not_found", `не найдено: ${this.configured} (проверены расширения ${PLAN_EXTENSIONS.join(", ")})`);
    if (found.length > 1) {
      throw new SourceError("ambiguous", `${this.configured}: найдено несколько файлов (${found.map((f) => path.basename(f.file)).join(", ")}) — укажите расширение в PLAN_SOURCE_PATH`);
    }
    this.resolvedExtension = path.extname(found[0].file);
    return found[0];
  }

  /** Locate the planning file and check that it can be read safely now. */
  async probe() {
    let { file, st } = await this.resolvePath();
    if (st.isDirectory()) {
      let names;
      try {
        names = await fs.promises.readdir(this.full);
      } catch (e) {
        throw mapFsError(e, this.configured);
      }
      const candidates = names.filter((n) => !isTempName(n) && PLAN_EXTENSIONS.includes(path.extname(n).toLowerCase()) &&
        (!this.pattern || this.pattern.test(n)));
      if (!candidates.length) throw new SourceError("not_found", `в папке ${this.configured} нет файла плана`);
      if (candidates.length > 1) {
        throw new SourceError("ambiguous", `в папке ${this.configured} несколько файлов плана: ${candidates.sort().join(", ")} — ` +
          "оставьте один файл или задайте PLAN_SOURCE_PATTERN", { candidates });
      }
      file = path.join(file, candidates[0]);
      try {
        st = await fs.promises.stat(file);
      } catch (e) {
        throw mapFsError(e, candidates[0]);
      }
    }
    if (!st.isFile()) throw new SourceError("not_found", `${this.configured} не является файлом`);
    const name = path.basename(file);
    if (isTempName(name)) throw new SourceError("unstable", `${name} — временный файл`);
    if (!PLAN_EXTENSIONS.includes(path.extname(name).toLowerCase())) {
      throw new SourceError("unexpected_format", `${name}: неподдерживаемый тип файла (ожидается ${PLAN_EXTENSIONS.join(", ")})`);
    }
    const dir = path.dirname(file);
    const owners = [`~$${name}`, `~$${name.slice(2)}`];
    for (const o of owners) {
      if (fs.existsSync(path.join(dir, o))) {
        throw new SourceError("locked", `${name} открыт в Excel (найден ${o}) — проверка будет повторена`);
      }
    }
    if (st.size === 0) throw new SourceError("empty", `${name} пуст (0 байт)`);
    const age = this.now() - st.mtimeMs;
    if (age < this.settleMs) {
      throw new SourceError("unstable", `${name} изменён ${Math.round(age / 1000)} с назад — ожидание окончания записи`);
    }
    return { file, name, size: st.size, mtime_ms: Math.round(st.mtimeMs), mtime: new Date(st.mtimeMs).toISOString() };
  }

  /** Copy the file to dest while hashing it. Returns the SHA-256 and the probe data. */
  async fetchTo(dest, probe) {
    const p = probe || (await this.probe());
    const hash = crypto.createHash("sha256");
    let bytes = 0;
    let head = Buffer.alloc(0);
    await new Promise((resolve, reject) => {
      const rs = fs.createReadStream(p.file);
      const ws = fs.createWriteStream(dest, { flags: "wx" });
      rs.on("data", (chunk) => {
        hash.update(chunk);
        bytes += chunk.length;
        if (head.length < 8) head = Buffer.concat([head, chunk.slice(0, 8 - head.length)]);
      });
      rs.on("error", (e) => { ws.destroy(); reject(mapFsError(e, p.name)); });
      ws.on("error", (e) => { rs.destroy(); reject(e); });
      ws.on("finish", resolve);
      rs.pipe(ws);
    }).catch((e) => {
      try { fs.unlinkSync(dest); } catch (x) { /* ignore */ }
      throw e;
    });
    const fail = (err) => {
      try { fs.unlinkSync(dest); } catch (x) { /* ignore */ }
      throw err;
    };
    let after;
    try {
      after = await fs.promises.stat(p.file);
    } catch (e) {
      fail(mapFsError(e, p.name));
    }
    if (after.size !== p.size || Math.round(after.mtimeMs) !== p.mtime_ms || bytes !== p.size) {
      fail(new SourceError("unstable", `${p.name} изменился во время чтения (неполная передача) — копия отброшена`));
    }
    const ext = path.extname(p.name).toLowerCase();
    if ((ext === ".xlsx" || ext === ".xlsm") && !head.slice(0, 4).equals(Buffer.from([0x50, 0x4b, 0x03, 0x04]))) {
      fail(new SourceError("corrupt", `${p.name} не является файлом Excel (повреждён или передан не полностью)`));
    }
    if (ext === ".xls" && !head.equals(Buffer.from([0xd0, 0xcf, 0x11, 0xe0, 0xa1, 0xb1, 0x1a, 0xe1]))) {
      fail(new SourceError("corrupt", `${p.name} не является файлом Excel 97-2003 (повреждён)`));
    }
    return Object.assign({}, p, { sha256: hash.digest("hex"), bytes });
  }
}

module.exports = { LocalFileSource, isTempName, PLAN_EXTENSIONS };
