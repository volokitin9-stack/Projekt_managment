"use strict";

/**
 * A failure of a file source. `code` is one of:
 *   not_configured   - no source configured
 *   not_found        - file/folder missing
 *   unavailable      - location unreachable (share offline, I/O error, HTTP 5xx, timeout)
 *   permission_denied- access/authentication refused
 *   locked           - the file is open/locked by another program
 *   unstable         - the file is still being written / transfer incomplete
 *   empty            - zero-byte file
 *   corrupt          - the content is not a valid file of its type
 *   unexpected_format- not a supported planning-file type
 *   ambiguous        - more than one candidate file
 *   not_implemented  - adapter type not available in this version
 * `transient` codes are retried on the next check without a review item.
 */
class SourceError extends Error {
  constructor(code, message, details) {
    super(message);
    this.name = "SourceError";
    this.code = code;
    this.details = details || null;
  }
  get transient() {
    return ["unavailable", "locked", "unstable"].includes(this.code);
  }
}

module.exports = { SourceError };
