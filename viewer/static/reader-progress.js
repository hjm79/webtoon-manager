(function (root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.WebtoonReaderProgress = api;
})(typeof window === "object" ? window : globalThis, function (root) {
  function sessionId() {
    if (root.crypto?.randomUUID) return root.crypto.randomUUID();
    const bytes = root.crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  function createWriter({ send, clientSessionId = sessionId(), schedule = setTimeout, cancel = clearTimeout }) {
    let sequence = 0;
    let pending = Promise.resolve();
    let trailingTimer = null;
    let maximumTimer = null;

    function cancelScheduled() {
      if (trailingTimer !== null) cancel(trailingTimer);
      if (maximumTimer !== null) cancel(maximumTimer);
      trailingTimer = maximumTimer = null;
    }

    function scheduleSave(callback) {
      if (trailingTimer !== null) cancel(trailingTimer);
      const flush = () => { cancelScheduled(); callback(); };
      trailingTimer = schedule(flush, 1200);
      if (maximumTimer === null) maximumTimer = schedule(flush, 5000);
    }

    function write(position, { keepalive = false, urgent = false } = {}) {
      const body = { ...position, client_session_id: clientSessionId, client_sequence: ++sequence };
      const perform = async () => {
        try {
          const response = await send(body, { keepalive });
          const result = response.ok && response.json ? await response.json() : {};
          return { ok: response.ok, saved: result.saved !== false, body, latest: body.client_sequence === sequence };
        } catch (_) {
          return { ok: false, saved: false, body, latest: body.client_sequence === sequence };
        }
      };
      // During pagehide a queued promise may never run. Its higher sequence lets
      // the server reject any older request that completes after this keepalive.
      const writing = urgent ? perform() : pending.then(perform);
      pending = writing.then(() => {});
      return writing;
    }

    return { write, scheduleSave, cancelScheduled };
  }

  function createReadingEvidence() {
    const chapters = new Map();
    const pending = new Map();

    const start = () => ({ page_index: 0, page_offset: 0 });
    const after = (left, right) => left.page_index > right.page_index ||
      (left.page_index === right.page_index && left.page_offset > right.page_offset);
    function chapterState(id) {
      if (!chapters.has(id)) chapters.set(id, { frontier: start(), recorded: start(), pages: new Map() });
      return chapters.get(id);
    }

    function restore(id, generation, stored) {
      if (chapterState(id).generation !== generation) {
        chapters.delete(id);
        pending.delete(id);
      }
      const state = chapterState(id);
      state.generation = generation;
      if (!stored || !Number.isInteger(stored.page_index) || stored.page_index < 0
          || !Number.isFinite(stored.page_offset) || stored.page_offset < 0 || stored.page_offset > 1) return;
      if (after(stored, state.frontier)) state.frontier = stored;
      if (after(stored, state.recorded)) state.recorded = stored;
    }

    function stage(chapterId) {
      const state = chapterState(chapterId);
      if (!state.generation || !after(state.frontier, state.recorded)) return false;
      state.recorded = state.frontier;
      pending.set(chapterId, { chapter_id: chapterId, zip_generation: state.generation, ...state.frontier });
      return true;
    }

    function acknowledge(coverage) {
      for (const entry of coverage) {
        if (pending.get(entry.chapter_id) === entry) pending.delete(entry.chapter_id);
      }
    }

    function snapshots() {
      return [...pending.values()].slice(0, 8);
    }

    function observe(img, rect, viewportHeight, pageCount, skippedPages = 0) {
      if (!img.complete || !img.naturalWidth || !img.naturalHeight || rect.height <= 0) return;
      const chapterId = img.dataset.chapter;
      const page = Number(img.dataset.page);
      if (!Number.isInteger(page) || page < 0 || page >= pageCount) return;
      const start = Math.max(0, -rect.top / rect.height);
      const end = Math.min(1, (viewportHeight - rect.top) / rect.height);
      if (start >= end) return;

      const state = chapterState(chapterId);
      const pages = state.pages;
      let { page_index: first, page_offset: offset } = state.frontier;
      if (first < skippedPages) [first, offset] = [skippedPages, 0];
      const covered = page < first ? 1 : page === first ? offset : pages.get(page) || 0;
      // A 2px tolerance allows for fractional scroll coordinates and layout rounding.
      if ((start - covered) * rect.height <= 2 && end > covered) {
        pages.set(page, (1 - end) * rect.height <= 2 ? 1 : end);
      }
      offset = Math.max(offset, pages.get(first) || 0);
      while (offset === 1 && first < pageCount - 1) {
        pages.delete(first);
        offset = pages.get(++first) || 0;
      }
      state.frontier = { page_index: first, page_offset: offset };
      return first === pageCount - 1 && offset === 1;
    }

    return { observe, restore, stage, snapshots, acknowledge };
  }

  return { createWriter, createReadingEvidence };
});
