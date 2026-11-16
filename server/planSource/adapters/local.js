"use strict";

// Local filesystem source: a file, or a folder with exactly one planning file.
// A mounted network share (SMB/NFS) is used through this adapter, too.
//
// Reading a workbook that colleagues keep open in Excel (shared SMB folder):
// * Office owner files (~$name.xlsx) and temporary/partial files (.tmp, .part,
//   .crdownload, hidden files) are never import candidates. An owner file is
//   NOT a reason to refuse the workbook: Excel keeps the last SAVED version in
//   the .xlsx and allows others to read it; the owner file is not touched.
// * The workbook is opened read-only (fs.open "r"). On Windows, Node/libuv opens
//   with FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE and GENERIC_READ,
//   which is compatible with Excel holding the file open; no permission is
//   bypassed and nothing is locked. A real sharing violation (EBUSY) postpones
//   the read.
// * Snapshot: record size + modification time, copy to a local file while
//   hashing (SHA-256), then check that size and modification time are still the
//   same and that the copy is a structurally valid workbook (ZIP central
//   directory with [Content_Types].xml and xl/workbook.xml; OLE header for .xls).
//   A changed source, a sharing violation or an invalid snapshot is retried
//   PLAN_SOURCE_READ_ATTEMPTS times, PLAN_SOURCE_RETRY_SECONDS apart; only a
//   validated snapshot is processed.
// * The original file is only read, never written, moved, renamed or locked.

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
  if (e.code === "EBUSY" || e.code === "ETXTBSY") {
    return new SourceError("sharing_violation", `${where}: нарушение совместного доступа (файл временно недоступен для чтения)`);
  }
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
    this.attempts = Math.max(1, Math.floor(Number(o.readAttempts == null ? 3 : o.readAttempts)) || 1);
    this.retryMs = Math.max(0, Number(o.retrySeconds == null ? 5 : o.retrySeconds)) * 1000;
    this.now = o.now || (() => Date.now());
    this.sleep = o.sleep || ((ms) => new Promise((r) => setTimeout(r, ms)));
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
    if (st.size === 0) throw new SourceError("empty", `${name} пуст (0 байт)`);
    const dir = path.dirname(file);
    const ownerFile = [`~$${name}`, `~$${name.slice(2)}`].find((o) => fs.existsSync(path.join(dir, o))) || null;
    return { file, name, size: st.size, mtime_ms: Math.round(st.mtimeMs), mtime: new Date(st.mtimeMs).toISOString(),
      open_in_excel: !!ownerFile };
  }

  /**
   * Take a validated snapshot of the last saved version into dest (retried, see top).
   * Returns the probe data + { sha256, bytes, attempts }.
   */
  async fetchTo(dest, probe) {
    let last = null;
    for (let attempt = 1; attempt <= this.attempts; attempt += 1) {
      if (attempt > 1) await this.sleep(this.retryMs);
      try {
        const p = attempt === 1 && probe ? probe : await this.probe();
        const snap = await this.snapshotOnce(dest, p);
        return Object.assign(snap, { attempts: attempt, retried: attempt > 1 ? last.code : null });
      } catch (e) {
        if (!(e instanceof SourceError) || !["unstable", "sharing_violation", "corrupt_snapshot"].includes(e.code)) throw e;
        last = e;
      }
    }
    const n = this.attempts;
    if (last.code === "unstable") {
      throw new SourceError("unstable", `Файл изменяется, повторная попытка: ${last.message} (попыток: ${n})`);
    }
    if (last.code === "sharing_violation") {
      throw new SourceError("sharing_violation", `Не удалось прочитать сохранённую версию Excel: ${last.message} (попыток: ${n}); ` +
        "чтение будет повторено при следующем обновлении");
    }
    throw new SourceError("corrupt", `Не удалось прочитать сохранённую версию Excel: ${last.message} (попыток: ${n})`);
  }

  /** One snapshot attempt. Throws unstable / sharing_violation / corrupt_snapshot (retried) or a final error. */
  async snapshotOnce(dest, p) {
    const age = this.now() - p.mtime_ms;
    if (age < this.settleMs) {
      throw new SourceError("unstable", `${p.name} сохранён ${Math.max(0, Math.round(age / 1000))} с назад — ожидание окончания записи`);
    }
    const discard = () => { try { fs.unlinkSync(dest); } catch (x) { /* ignore */ } };
    let src = null;
    let out = null;
    const hash = crypto.createHash("sha256");
    let bytes = 0;
    try {
      try {
        src = await fs.promises.open(p.file, "r");       // read-only; never "r+", "w" or "a"
      } catch (e) {
        throw mapFsError(e, p.name);
      }
      const st0 = await src.stat();
      if (st0.size !== p.size || Math.round(st0.mtimeMs) !== p.mtime_ms) {
        throw new SourceError("unstable", `${p.name} изменился перед чтением`);
      }
      out = await fs.promises.open(dest, "wx");
      const buf = Buffer.alloc(1024 * 1024);
      for (;;) {
        let r;
        try {
          r = await src.read(buf, 0, buf.length, bytes);
        } catch (e) {
          throw mapFsError(e, p.name);
        }
        if (!r.bytesRead) break;
        const chunk = buf.subarray(0, r.bytesRead);
        hash.update(chunk);
        await out.write(chunk, 0, chunk.length, bytes);
        bytes += r.bytesRead;
      }
      await out.sync();
    } catch (e) {
      if (out) await out.close().catch(() => {});
      if (src) await src.close().catch(() => {});
      discard();
      throw e;
    }
    await out.close();
    await src.close();
    let after;
    try {
      after = await fs.promises.stat(p.file);
    } catch (e) {
      discard();
      throw mapFsError(e, p.name);
    }
    if (after.size !== p.size || Math.round(after.mtimeMs) !== p.mtime_ms || bytes !== p.size) {
      discard();
      throw new SourceError("unstable", `${p.name} изменился во время копирования — копия отброшена`);
    }
    const problem = validateWorkbookFile(dest, path.extname(p.name).toLowerCase());
    if (problem) {
      discard();
      throw new SourceError("corrupt_snapshot", `${p.name}: снимок не является корректной книгой Excel (${problem})`);
    }
    return Object.assign({}, p, { sha256: hash.digest("hex"), bytes });
  }
}

/**
 * Structural check of a snapshot. Returns null when valid, else the problem.
 * .xlsx/.xlsm: ZIP end-of-central-directory, every central entry points to a local
 * header, and the package contains [Content_Types].xml and xl/workbook.xml.
 * .xls: OLE compound-file header. .csv: not empty.
 */
function validateWorkbookFile(file, ext) {
  const b = fs.readFileSync(file);
  if (!b.length) return "пустой файл";
  if (ext === ".csv") return null;
  if (ext === ".xls") {
    return b.subarray(0, 8).equals(Buffer.from([0xd0, 0xcf, 0x11, 0xe0, 0xa1, 0xb1, 0x1a, 0xe1]))
      ? null : "нет заголовка файла Excel 97-2003";
  }
  if (b.length < 22 || b.readUInt32LE(0) !== 0x04034b50) return "нет заголовка ZIP";
  let eocd = -1;
  for (let i = b.length - 22; i >= Math.max(0, b.length - 22 - 65535); i -= 1) {
    if (b.readUInt32LE(i) === 0x06054b50) { eocd = i; break; }
  }
  if (eocd < 0) return "нет оглавления ZIP (файл обрезан)";
  const count = b.readUInt16LE(eocd + 10);
  const cdSize = b.readUInt32LE(eocd + 12);
  const cdOff = b.readUInt32LE(eocd + 16);
  if (cdOff + cdSize > eocd || !count) return "оглавление ZIP повреждено";
  const names = new Set();
  let pos = cdOff;
  for (let i = 0; i < count; i += 1) {
    if (pos + 46 > b.length || b.readUInt32LE(pos) !== 0x02014b50) return "запись оглавления ZIP повреждена";
    const nameLen = b.readUInt16LE(pos + 28);
    const extraLen = b.readUInt16LE(pos + 30);
    const commentLen = b.readUInt16LE(pos + 32);
    const local = b.readUInt32LE(pos + 42);
    if (local + 30 > b.length || b.readUInt32LE(local) !== 0x04034b50) return "локальный заголовок ZIP повреждён";
    names.add(b.subarray(pos + 46, pos + 46 + nameLen).toString("utf-8"));
    pos += 46 + nameLen + extraLen + commentLen;
  }
  if (!names.has("[Content_Types].xml")) return "в пакете нет [Content_Types].xml";
  if (!names.has("xl/workbook.xml")) return "в пакете нет xl/workbook.xml";
  return null;
}

module.exports = { LocalFileSource, isTempName, validateWorkbookFile, PLAN_EXTENSIONS };
