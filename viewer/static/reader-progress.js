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

  return { createWriter };
});
