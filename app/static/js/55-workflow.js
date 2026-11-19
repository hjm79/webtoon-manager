// 검색 → 범위 선택 → 구독/진행 확인 → 관리기 안에서 읽기.
let workflowSearchGeneration = 0;
let workflowSubscription = null;
let workflowSubscribeResolve = null;
let workflowSubscribeGeneration = 0;
let workflowSubscribeSaving = false;
let workflowReaderGeneration = 0;
let workflowReaderOrigin = null;
let workflowReaderTimer = null;
let workflowReaderCloseTimer = null;
let workflowReaderClosing = false;
let workflowStatusPending = false;
let workflowStatusTimer = null;
let workflowPreviewGeneration = 0;
let workflowPreviewItem = null;
let workflowStatusData = {};

async function openWorkflowPreview(item) {
  const generation = ++workflowPreviewGeneration;
  workflowPreviewItem = item;
  const dialog = document.getElementById("workflow-preview-dialog");
  const status = document.getElementById("workflow-preview-status");
  const list = document.getElementById("workflow-preview-episodes");
  const subscribe = document.getElementById("workflow-preview-subscribe");
  document.getElementById("workflow-preview-title").textContent = `${item.title} · 회차 미리보기`;
  list.replaceChildren(); subscribe.disabled = true;
  subscribe.textContent = item.subscription === "active" ? "다운로드 범위 변경" : "다운로드 범위 선택 · 구독";
  status.textContent = "회차 목록을 불러오고 있습니다…";
  showWorkflowDialog(dialog);
  try {
    const data = await apiCall("/api/workflow/preview", { method: "POST", body: JSON.stringify({ platform: item.platform, title_id: String(item.title_id) }) });
    if (generation !== workflowPreviewGeneration || !dialog.open) return;
    item.subscription = data.subscription || item.subscription;
    subscribe.textContent = item.subscription === "active" ? "다운로드 범위 변경" : "다운로드 범위 선택 · 구독";
    status.textContent = `총 ${(data.episodes || []).length}회차 · 현재 받을 수 있는 회차 ${data.available_count || 0}개`;
    for (const episode of data.episodes || []) {
      const row = document.createElement("li");
      row.textContent = `${episode.episode_no} · ${episode.subtitle} · ${episode.owned ? "이미 받음" : episode.available ? "다운로드 가능" : "현재 받을 수 없음"}`;
      list.appendChild(row);
    }
    if (!list.children.length) status.textContent += " · 표시할 회차가 없습니다.";
    subscribe.disabled = false;
  } catch (e) { if (generation === workflowPreviewGeneration) status.textContent = `미리보기를 불러오지 못했습니다: ${e.message}`; }
}

document.getElementById("workflow-preview-close").addEventListener("click", () => {
  ++workflowPreviewGeneration; closeWorkflowDialog(document.getElementById("workflow-preview-dialog"));
});
document.getElementById("workflow-preview-subscribe").addEventListener("click", () => {
  const item = workflowPreviewItem;
  ++workflowPreviewGeneration; closeWorkflowDialog(document.getElementById("workflow-preview-dialog"));
  if (item) chooseSubscriptionScope(item, { edit: item.subscription === "active" });
});

function showWorkflowDialog(dialog) {
  if (dialog.open) return;
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

function closeWorkflowDialog(dialog) {
  if (!dialog.open) return;
  if (typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
}

function normalizeWorkflowReaderStatus(data) {
  if (data?.status === "not_downloaded" && Number(data.downloaded_count) > 0) return "indexing";
  return ["ready", "indexing", "not_downloaded", "not_configured", "error"].includes(data?.status) ? data.status : "error";
}

function workflowReaderMessage(status, message) {
  if (status === "indexing") {
    const help = "받은 회차가 있으며 뷰어가 갱신되면 읽을 수 있습니다.";
    return message ? `${help} ${message}` : `${help} 잠시 후 다시 눌러주세요.`;
  }
  if (message) return message;
  return {
    not_downloaded: "아직 다운로드한 회차가 없습니다. 다운로드와 뷰어 스캔이 끝나면 여기서 읽을 수 있습니다.",
    not_configured: "뷰어 서버가 연결되어 있지 않습니다. 설정에서 웹툰 뷰어 서버 주소를 확인해주세요.",
    error: "뷰어에 연결하지 못했습니다. 잠시 후 다시 시도해주세요.",
  }[status] || "읽을 수 있는 회차가 없습니다.";
}

function workflowReaderUrl(raw) {
  if (!raw) return null;
  try {
    const url = new URL(raw, window.location.href);
    return ["http:", "https:"].includes(url.protocol) ? url : null;
  } catch (e) { return null; }
}

function applyWorkflowReaderAvailability(button, data) {
  const status = normalizeWorkflowReaderStatus(data);
  const labels = { indexing: "읽기 · 서재 반영 대기", not_downloaded: "읽기 · 아직 받은 회차 없음", not_configured: "읽기 · 뷰어 미설정", error: "읽기 · 연결 확인 필요" };
  button.textContent = labels[status] || "읽기";
  button.dataset.readerStatus = status;
  button.title = status === "ready" ? "다운로드한 회차를 관리기 안에서 읽기" : workflowReaderMessage(status, data?.message);
}

function refreshWorkflowReadButtons(title, platform, titleId, data) {
  for (const button of document.querySelectorAll("[data-viewer-check-title]")) {
    if (button.dataset.viewerCheckTitle === title && (button.dataset.viewerCheckPlatform || "naver") === (platform || "naver")
        && (button.dataset.viewerCheckTitleId || "") === String(titleId || "")) {
      applyWorkflowReaderAvailability(button, data);
    }
  }
  syncCardActionRows(document.getElementById("naver-list-grid"));
}

function makeWorkflowReadButton(title, platform, titleId) {
  const button = makeButton("읽기", () => openEmbeddedReader(title, platform, titleId));
  button.classList.add("workflow-read-button");
  button.dataset.viewerCheckTitle = title;
  button.dataset.viewerCheckPlatform = platform || "naver";
  if (titleId) button.dataset.viewerCheckTitleId = titleId;
  button.title = "다운로드한 회차를 관리기 안에서 읽기";
  return button;
}

function makeWorkflowScopeButton(item) {
  const button = makeButton("범위 변경", () => chooseSubscriptionScope(item, { edit: true }));
  button.dataset.scopeTitleId = item.title_id;
  button.dataset.scopePlatform = item.platform || "naver";
  button.title = `${item.title}의 다운로드 범위 변경`;
  const reason = workflowScopeRestriction(item);
  button.disabled = !!reason;
  if (reason) button.title = reason;
  return button;
}

function workflowScopeRestriction(item, data = workflowStatusData) {
  const platform = item.platform || "naver";
  const current = (data.subscriptions || []).find((row) => row.platform === platform && String(row.title_id) === String(item.title_id)) || item;
  if (current.scope_edit_reason) return current.scope_edit_reason;
  if (["queued", "running", "resting"].includes(current.status)
      || (data.queue || []).some((row) => row.platform === platform && String(row.title_id) === String(item.title_id))) {
    return "다운로드 대기열에 있는 작품입니다. 대기 취소 또는 실행 완료 후 범위를 변경해주세요.";
  }
  return data.download_busy ? "다른 다운로드가 실행 중입니다. 완료 후 범위를 변경해주세요." : null;
}

function makeWorkflowQueueButton(item) {
  const retry = ["retry_download", "failed"].includes(item.state || item.status);
  const button = makeButton(retry ? "지금 다시 시도" : "다운로드 추가", async () => {
    button.disabled = true;
    try {
      await apiCall("/api/workflow/queue", { method: "POST", body: JSON.stringify({ platform: item.platform || "naver", title_id: String(item.title_id) }) });
      await refreshWorkflowStatus();
    } catch (e) { alert(e.message); }
    finally { button.disabled = false; }
  });
  button.title = retry ? "다운로드 범위와 받은 회차를 유지하고 다시 시도" : "저장된 다운로드 범위를 유지하고 대기열에 추가";
  button.disabled = ["queued", "running", "resting"].includes(item.status);
  return button;
}

function beginEmbeddedReader(title) {
  const generation = ++workflowReaderGeneration;
  const dialog = document.getElementById("workflow-reader-dialog");
  const frame = document.getElementById("workflow-reader-frame");
  const external = document.getElementById("workflow-reader-external");
  clearTimeout(workflowReaderTimer);
  clearTimeout(workflowReaderCloseTimer);
  workflowReaderClosing = false;
  document.getElementById("workflow-reader-close").disabled = false;
  document.getElementById("workflow-reader-close").textContent = "읽기 닫기";
  document.getElementById("workflow-reader-force-close").classList.add("hidden");
  workflowReaderOrigin = null;
  frame.removeAttribute("src");
  frame.classList.add("hidden");
  external.classList.add("hidden");
  external.removeAttribute("href");
  document.getElementById("workflow-reader-title").textContent = title;
  document.getElementById("workflow-reader-status").textContent = "읽을 수 있는 회차를 확인하고 있습니다…";
  showWorkflowDialog(dialog);
  return generation;
}

function showEmbeddedReaderUrl(raw, generation) {
  const dialog = document.getElementById("workflow-reader-dialog");
  if (generation !== workflowReaderGeneration || !dialog.open) return;
  const url = workflowReaderUrl(raw);
  const status = document.getElementById("workflow-reader-status");
  if (!url) { status.textContent = "뷰어 연결 주소를 확인해주세요."; return; }
  const frame = document.getElementById("workflow-reader-frame");
  const external = document.getElementById("workflow-reader-external");
  external.href = url.href;
  external.target = url.origin === window.location.origin ? "_self" : "_blank";
  external.textContent = url.origin === window.location.origin ? "리더 화면 열기" : "별도 뷰어에서 열기";
  external.classList.remove("hidden");
  workflowReaderOrigin = url.origin;
  url.searchParams.set("embed", "1");
  url.searchParams.set("parent_origin", window.location.origin);
  frame.src = url.href;
  frame.classList.remove("hidden");
  status.textContent = "리더를 여는 중…";
  workflowReaderTimer = setTimeout(() => {
    if (generation === workflowReaderGeneration && dialog.open) {
      status.textContent = `리더가 열리지 않으면 ‘${external.textContent}’를 눌러주세요. 로그인이나 뷰어 연결 설정을 확인할 수 있습니다.`;
    }
  }, 12000);
}

function readerStatusEndpoint(title, platform, titleId) {
  const params = new URLSearchParams({ title, platform: platform || "naver" });
  if (titleId) params.set("title_id", titleId);
  return `/api/reader/status?${params}`;
}

async function openEmbeddedReader(title, platform, titleId) {
  const generation = beginEmbeddedReader(title);
  const dialog = document.getElementById("workflow-reader-dialog");
  const status = document.getElementById("workflow-reader-status");
  try {
    const data = await apiCall(readerStatusEndpoint(title, platform, titleId));
    refreshWorkflowReadButtons(title, platform, titleId, data);
    if (generation !== workflowReaderGeneration || !dialog.open) return;
    const readerStatus = normalizeWorkflowReaderStatus(data);
    if (readerStatus !== "ready" || !workflowReaderUrl(data.url)) {
      status.textContent = workflowReaderMessage(readerStatus, data.message);
      return;
    }
    showEmbeddedReaderUrl(data.url, generation);
  } catch (e) {
    if (generation === workflowReaderGeneration) status.textContent = `뷰어 연결 실패: ${e.message}`;
  }
}

function openEmbeddedReaderByUrl(title, url) {
  showEmbeddedReaderUrl(url, beginEmbeddedReader(title));
}

function closeEmbeddedReader(force = false) {
  const frame = document.getElementById("workflow-reader-frame");
  if (!force && workflowReaderOrigin && frame.contentWindow) {
    if (workflowReaderClosing) return;
    workflowReaderClosing = true;
    clearTimeout(workflowReaderTimer);
    document.getElementById("workflow-reader-force-close").classList.add("hidden");
    document.getElementById("workflow-reader-close").disabled = true;
    document.getElementById("workflow-reader-status").textContent = "읽은 위치를 저장하고 있습니다…";
    frame.contentWindow.postMessage({ source: "webtoon-manager", version: 1, type: "close" }, workflowReaderOrigin);
    workflowReaderCloseTimer = setTimeout(() => showReaderCloseFailure("읽은 위치의 저장 응답을 아직 받지 못했습니다. 다시 저장하거나, 저장하지 않고 닫기를 직접 선택해주세요."), 1500);
    return;
  }
  ++workflowReaderGeneration;
  clearTimeout(workflowReaderTimer);
  clearTimeout(workflowReaderCloseTimer);
  workflowReaderClosing = false;
  workflowReaderOrigin = null;
  frame.removeAttribute("src");
  frame.classList.add("hidden");
  const dialog = document.getElementById("workflow-reader-dialog");
  const wasOpen = dialog.open;
  closeWorkflowDialog(dialog);
  if (wasOpen) document.dispatchEvent(new Event("webtoon-reader-closed"));
}

function showReaderCloseFailure(message) {
  clearTimeout(workflowReaderCloseTimer);
  workflowReaderClosing = false;
  const close = document.getElementById("workflow-reader-close");
  close.disabled = false;
  close.textContent = "다시 저장 후 닫기";
  document.getElementById("workflow-reader-force-close").classList.remove("hidden");
  document.getElementById("workflow-reader-status").textContent = message;
}

window.addEventListener("message", (event) => {
  const frame = document.getElementById("workflow-reader-frame");
  const data = event.data;
  if (!workflowReaderOrigin || event.origin !== workflowReaderOrigin || event.source !== frame.contentWindow
      || !data || data.source !== "webtoon-reader" || data.version !== 1) return;
  if (data.type === "close") closeEmbeddedReader(true);
  else if (data.type === "ready") {
    clearTimeout(workflowReaderTimer);
    document.getElementById("workflow-reader-status").textContent = "";
  } else if (data.type === "error") {
    clearTimeout(workflowReaderTimer);
    const message = data.message || "리더를 불러오지 못했습니다. 별도 뷰어에서 다시 확인해주세요.";
    if (workflowReaderClosing || data.code === "progress_save_failed") showReaderCloseFailure(message);
    else document.getElementById("workflow-reader-status").textContent = message;
  }
});

document.getElementById("workflow-reader-close").addEventListener("click", () => closeEmbeddedReader());
document.getElementById("workflow-reader-force-close").addEventListener("click", () => {
  if (confirm("마지막 읽은 위치가 저장되지 않을 수 있습니다. 저장하지 않고 닫을까요?")) closeEmbeddedReader(true);
});
document.getElementById("workflow-reader-dialog").addEventListener("cancel", (event) => {
  event.preventDefault(); closeEmbeddedReader();
});

async function searchWorkflow(event) {
  event?.preventDefault();
  const query = document.getElementById("workflow-search-query").value.trim();
  if (!query) return;
  const generation = ++workflowSearchGeneration;
  const button = document.getElementById("workflow-search-submit");
  const status = document.getElementById("workflow-search-status");
  const results = document.getElementById("workflow-search-results");
  button.disabled = true;
  status.textContent = "휴재·완결 작품을 포함해 검색하고 있습니다…";
  results.replaceChildren();
  try {
    const platform = document.getElementById("workflow-search-platform").value;
    const data = await apiCall(`/api/workflow/search?query=${encodeURIComponent(query)}&platform=${encodeURIComponent(platform)}`);
    if (generation !== workflowSearchGeneration) return;
    const rows = Array.isArray(data.results) ? data.results : [];
    status.textContent = `${rows.length}개 작품을 찾았습니다.${(data.errors || []).map((error) => ` ${error.platform === "kakao" ? "카카오페이지" : "네이버"}: ${error.message}`).join("")}`;
    for (const item of rows) {
      const card = document.createElement("article");
      card.className = "webtoon-card workflow-result-card";
      card.dataset.titleId = item.title_id;
      card.dataset.platform = item.platform;
      const cover = makeButton("", () => openWorkflowPreview(item));
      cover.className = "workflow-preview-cover";
      cover.setAttribute("aria-label", `${item.title} 회차 미리보기`);
      const image = document.createElement("img");
      image.alt = `${item.title} 표지`;
      image.loading = "lazy"; image.referrerPolicy = "no-referrer";
      const fallback = workflowReaderUrl(item.thumbnail_url);
      image.src = item.platform === "kakao" ? `/api/kakao-thumbnail/${encodeURIComponent(item.title_id)}` : fallback?.href || "";
      image.addEventListener("error", () => {
        if (item.platform === "kakao" && fallback && !image.dataset.fallback) { image.dataset.fallback = "1"; image.src = fallback.href; }
        else { const placeholder = document.createElement("div"); placeholder.className = "thumb-placeholder"; placeholder.textContent = "표지 없음"; image.replaceWith(placeholder); }
      });
      if (image.getAttribute("src")) cover.appendChild(image);
      else { const placeholder = document.createElement("div"); placeholder.className = "thumb-placeholder"; placeholder.textContent = "표지 없음"; cover.appendChild(placeholder); }
      const platformLabel = item.platform === "kakao" ? "카카오페이지" : "네이버";
      const info = document.createElement("div");
      info.className = "webtoon-card-body";
      const heading = document.createElement("a");
      heading.textContent = item.title;
      heading.href = item.platform === "kakao" ? kakaoContentUrl(item.title_id) : naverUrl(item.title_id);
      heading.target = "_blank";
      heading.rel = "noopener";
      heading.title = "공식 작품 페이지를 새 탭으로 열기";
      heading.className = "workflow-result-title";
      const meta = document.createElement("p");
      meta.className = "webtoon-card-meta";
      meta.textContent = [platformLabel, item.is_paused ? "휴재" : "", item.is_finished ? "완결" : ""].filter(Boolean).join(" · ");
      info.append(heading, meta);
      const actions = document.createElement("div");
      actions.className = "webtoon-card-actions";
      const editScope = makeWorkflowScopeButton(item);
      editScope.classList.toggle("hidden", item.subscription !== "active");
      const subscribe = makeButton(item.subscription === "active" ? "구독 중" : "범위 선택 후 구독", async () => {
        const saved = await chooseSubscriptionScope(item);
        if (saved) {
          item.subscription = "active";
          subscribe.textContent = "구독 중";
          subscribe.disabled = true;
          editScope.classList.remove("hidden");
        }
      });
      subscribe.disabled = item.subscription === "active";
      subscribe.dataset.workflowSubscribe = "1";
      actions.append(subscribe, editScope, makeWorkflowReadButton(item.title, item.platform, item.title_id), makeButton("회차 미리보기", () => openWorkflowPreview(item)));
      card.append(cover, info, actions);
      results.appendChild(card);
    }
  } catch (e) {
    if (generation === workflowSearchGeneration) status.textContent = `검색하지 못했습니다: ${e.message}`;
  } finally {
    if (generation === workflowSearchGeneration) button.disabled = false;
  }
}
document.getElementById("workflow-search-form").addEventListener("submit", searchWorkflow);

function closeSubscriptionScope(result = null) {
  if (workflowSubscribeSaving && !result) return;
  ++workflowSubscribeGeneration;
  closeWorkflowDialog(document.getElementById("workflow-subscribe-dialog"));
  const resolve = workflowSubscribeResolve;
  workflowSubscribeResolve = null;
  workflowSubscription = null;
  resolve?.(result);
}

async function chooseSubscriptionScope(item, { edit = false } = {}) {
  if (workflowSubscribeSaving) return null;
  if (workflowSubscribeResolve) closeSubscriptionScope();
  const generation = ++workflowSubscribeGeneration;
  workflowSubscription = { ...item, platform: item.platform || "naver", editing: edit };
  const dialog = document.getElementById("workflow-subscribe-dialog");
  const status = document.getElementById("workflow-subscribe-status");
  const error = document.getElementById("workflow-subscribe-error");
  const submit = document.getElementById("workflow-subscribe-submit");
  const download = document.getElementById("workflow-subscribe-download");
  document.getElementById("workflow-subscribe-form").reset();
  document.getElementById("workflow-scope-fields").disabled = true;
  setWorkflowScopeMode("latest");
  document.getElementById("workflow-subscribe-title").textContent = `${item.title} · ${edit ? "다운로드 범위 변경" : "구독과 다운로드 범위"}`;
  document.getElementById("workflow-scope-legend").textContent = edit ? "앞으로 받을 회차" : "처음 받을 회차";
  document.getElementById("workflow-preserve-hint").classList.toggle("hidden", !edit);
  document.getElementById("workflow-start-hint").textContent = edit
    ? "범위만 저장하면 기존 다운로드 예약을 유지합니다. 대기열에 추가하면 현재 작업이 끝난 뒤 선택한 범위로 받습니다."
    : "구독만 저장하면 다음 예약에 확인합니다. 다운로드 대기열에 추가하면 현재 작업이 끝난 뒤 선택한 범위로 받습니다.";
  submit.textContent = edit ? "범위 저장" : "구독만 저장";
  download.textContent = edit ? "저장하고 다운로드 대기열 추가" : "구독하고 다운로드 대기열 추가";
  document.getElementById("workflow-episode-picker").replaceChildren();
  document.getElementById("workflow-episode-picker").classList.add("hidden");
  status.textContent = "받을 수 있는 회차를 확인하고 있습니다…";
  error.textContent = "";
  submit.disabled = true; download.disabled = true;
  showWorkflowDialog(dialog);
  const result = new Promise((resolve) => { workflowSubscribeResolve = resolve; });
  try {
    if (edit) {
      const state = await apiCall("/api/workflow/status");
      if (generation !== workflowSubscribeGeneration || !dialog.open) return result;
      workflowStatusData = state;
      const reason = workflowScopeRestriction(item, state);
      if (reason) { status.textContent = reason; return result; }
    }
    const preview = await apiCall("/api/workflow/preview", { method: "POST", body: JSON.stringify({ platform: workflowSubscription.platform, title_id: String(item.title_id) }) });
    if (generation !== workflowSubscribeGeneration || !dialog.open) return result;
    if (edit && preview.subscription !== "active") throw new Error("현재 구독 중인 작품이 아닙니다. 목록을 새로고침해주세요.");
    workflowSubscription.episodes = preview.episodes || [];
    status.textContent = `현재 받을 수 있는 회차 ${preview.available_count || 0}개.${edit ? " 기존 다운로드 범위를 불러왔습니다." : " 구독 버튼만으로 전체 회차를 바로 받지 않습니다."}`;
    const current = edit ? preview.current_scope || { mode: "all" } : null;
    const selectedNumbers = new Set(current?.episode_nos || []);
    const picker = document.getElementById("workflow-episode-picker");
    for (const episode of workflowSubscription.episodes) {
      const label = document.createElement("label");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.value = episode.episode_no;
      checkbox.disabled = !(episode.available || edit && (episode.owned || selectedNumbers.has(episode.episode_no)));
      checkbox.checked = edit && selectedNumbers.has(episode.episode_no);
      const text = document.createTextNode(`${episode.episode_no} · ${episode.subtitle}${episode.owned ? " · 이미 받음" : ""}${!episode.available ? " · 받을 수 없음" : ""}`);
      label.append(checkbox, text); picker.appendChild(label);
    }
    if (current) {
      if (current.latest_count != null) document.getElementById("workflow-latest-count").value = current.latest_count;
      setWorkflowScopeMode(current.mode);
    }
    updateWorkflowEpisodeSelection();
    if (edit) {
      const reason = workflowScopeRestriction(item);
      if (reason) { status.textContent = reason; return result; }
    }
    document.getElementById("workflow-scope-fields").disabled = false;
    submit.disabled = false;
    download.disabled = current?.mode === "future";
  } catch (e) {
    if (generation === workflowSubscribeGeneration) { status.textContent = "회차를 확인하지 못했습니다."; error.textContent = e.message; }
  }
  return result;
}

function updateWorkflowScopeHint(mode) {
  document.getElementById("workflow-scope-hint").textContent = {
    all: "현재 또는 추후 받을 수 있는 회차를 모두 대상으로 유지합니다. 무료로 열리는 기존 회차도 다시 확인합니다.",
    selected: "선택한 회차만 다운로드 대상으로 유지합니다. 선택하지 않은 기존 회차와 이후 새로 추가되는 회차는 자동으로 포함하지 않습니다.",
    latest: "현재 선택하지 않은 기존 회차는 잠금이 풀려도 자동으로 추가하지 않습니다. 이후 새로 추가되는 회차는 구독 예약에 따라 확인합니다.",
    future: "현재 목록의 회차는 제외하고 이후 새로 추가되는 회차만 구독 예약에 따라 확인합니다.",
  }[mode] || "";
}

function setWorkflowScopeMode(mode) {
  const input = document.querySelector(`input[name="workflow-scope"][value="${mode}"]`);
  if (!input) return;
  input.checked = true;
  updateWorkflowScopeHint(mode);
  document.getElementById("workflow-episode-tools").classList.toggle("hidden", mode !== "selected");
  document.getElementById("workflow-episode-picker").classList.toggle("hidden", mode !== "selected");
  updateWorkflowEpisodeSelection();
  document.getElementById("workflow-latest-count").disabled = mode !== "latest";
  document.getElementById("workflow-subscribe-download").disabled = mode === "future"
    || document.getElementById("workflow-subscribe-submit").disabled;
}

function updateWorkflowEpisodeSelection() {
  const count = document.querySelectorAll('#workflow-episode-picker input:checked').length;
  document.getElementById("workflow-selected-count").textContent = `선택한 회차 ${count}개`;
  document.getElementById("workflow-selection-error").textContent = "";
}

function selectWorkflowEpisodes(action) {
  if (workflowSubscribeSaving) return;
  const inputs = [...document.querySelectorAll('#workflow-episode-picker input')];
  let selected;
  if (action === "range") {
    const startText = document.getElementById("workflow-range-start").value.trim();
    const endText = document.getElementById("workflow-range-end").value.trim();
    const start = Number(startText), end = Number(endText);
    const error = document.getElementById("workflow-selection-error");
    if (!/^[0-9]+$/.test(startText) || !/^[0-9]+$/.test(endText) || !Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 1 || end < 1) {
      error.textContent = "시작·끝 번호는 1 이상의 정수로 입력해주세요."; return;
    }
    if (start > end) { error.textContent = "끝 번호는 시작 번호 이상이어야 합니다."; return; }
    selected = inputs.filter((input) => !input.disabled && Number(input.value) >= start && Number(input.value) <= end);
    if (!selected.length) { error.textContent = "입력한 범위에 선택 가능한 회차가 없습니다. 기존 선택을 유지합니다."; return; }
  } else selected = action === "all" ? inputs.filter((input) => !input.disabled) : [];
  const selectedInputs = new Set(selected);
  for (const input of inputs) input.checked = selectedInputs.has(input);
  updateWorkflowEpisodeSelection();
}

document.getElementById("workflow-select-range").addEventListener("click", () => selectWorkflowEpisodes("range"));
document.getElementById("workflow-select-all").addEventListener("click", () => selectWorkflowEpisodes("all"));
document.getElementById("workflow-clear-selection").addEventListener("click", () => selectWorkflowEpisodes("clear"));
document.getElementById("workflow-episode-picker").addEventListener("change", updateWorkflowEpisodeSelection);
for (const id of ["workflow-range-start", "workflow-range-end"]) document.getElementById(id).addEventListener("keydown", (event) => {
  if (event.key === "Enter") { event.preventDefault(); selectWorkflowEpisodes("range"); }
});

document.querySelectorAll('input[name="workflow-scope"]').forEach((input) => input.addEventListener("change", () => {
  setWorkflowScopeMode(input.value);
}));
for (const id of ["workflow-subscribe-close", "workflow-subscribe-cancel"]) document.getElementById(id).addEventListener("click", () => closeSubscriptionScope());
document.getElementById("workflow-subscribe-dialog").addEventListener("cancel", (event) => { event.preventDefault(); closeSubscriptionScope(); });

document.getElementById("workflow-subscribe-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!workflowSubscription) return;
  const submit = document.getElementById("workflow-subscribe-submit");
  const download = document.getElementById("workflow-subscribe-download");
  const error = document.getElementById("workflow-subscribe-error");
  if (submit.disabled) return;
  const item = workflowSubscription;
  const mode = document.querySelector('input[name="workflow-scope"]:checked').value;
  const scope = { mode };
  if (mode === "latest") {
    scope.latest_count = Number(document.getElementById("workflow-latest-count").value);
    if (!Number.isInteger(scope.latest_count) || scope.latest_count < 1 || scope.latest_count > 1000) { error.textContent = "최근 회차 수는 1~1000 사이의 정수로 입력해주세요."; return; }
  }
  if (mode === "selected") {
    scope.episode_nos = [...document.querySelectorAll('#workflow-episode-picker input:checked')].map((input) => Number(input.value));
    if (!scope.episode_nos.length) { error.textContent = "받을 회차를 하나 이상 선택해주세요."; return; }
  }
  submit.disabled = true; download.disabled = true;
  workflowSubscribeSaving = true;
  for (const id of ["workflow-subscribe-close", "workflow-subscribe-cancel"]) document.getElementById(id).disabled = true;
  error.textContent = "";
  try {
    const payload = {
      platform: item.platform, title_id: String(item.title_id), title: item.title,
      thumbnail_url: item.thumbnail_url || "", author_summary: item.author_summary || "", scope,
      start_now: mode !== "future" && event.submitter?.id === "workflow-subscribe-download",
    };
    if (item.editing) {
      delete payload.title; delete payload.thumbnail_url; delete payload.author_summary;
    }
    const data = await apiCall(item.editing ? "/api/workflow/scope" : "/api/workflow/subscribe", { method: "POST", body: JSON.stringify(payload) });
    if (workflowSubscription !== item) return;
    for (const card of document.querySelectorAll("#workflow-search-results .workflow-result-card")) {
      if (card.dataset.titleId !== String(item.title_id) || card.dataset.platform !== item.platform) continue;
      const button = card.querySelector("[data-workflow-subscribe]");
      if (button) { button.textContent = "구독 중"; button.disabled = true; }
      card.querySelector("[data-scope-title-id]")?.classList.remove("hidden");
    }
    invalidateListCaches();
    workflowSubscribeSaving = false;
    closeSubscriptionScope(data);
    refreshWorkflowStatus();
    if (!document.getElementById("page-naver-list").classList.contains("hidden")) loadNaverList(false);
  } catch (e) { if (workflowSubscription === item) { error.textContent = e.message; submit.disabled = false; download.disabled = mode === "future"; } }
  finally {
    workflowSubscribeSaving = false;
    for (const id of ["workflow-subscribe-close", "workflow-subscribe-cancel"]) document.getElementById(id).disabled = false;
  }
});

const workflowQueueLabels = {
  queued: "다운로드 대기", blocked_auth: "카카오 재인증 대기", retry_wait: "연결 확인 후 재시도 대기",
  retry_download: "다운로드 재시도 대기", failed: "다운로드 실패 · 수동 재시도 가능"
};

function renderWorkflowStatus(data) {
  workflowStatusData = data;
  if (typeof renderKakaoAuthBanner === "function") renderKakaoAuthBanner(data.kakao_auth, data.queue || []);
  const running = (data.queue || []).find((item) => item.state === "running");
  const waiting = (data.queue || []).filter((item) => Object.hasOwn(workflowQueueLabels, item.state));
  const progress = running && !["running", "resting"].includes(data.progress?.state)
    ? { state: running.rest_until ? "resting" : "running", title: running.title, rest_until: running.rest_until }
    : data.progress || {};
  document.getElementById("workflow-progress-heading").textContent = ["running", "resting"].includes(progress.state)
    ? "실행 중인 작업" : ["success", "error", "failed", "deferred"].includes(progress.state) ? "최근 실행 결과" : "현재 작업";
  const labels = { ...workflowQueueLabels, idle: "실행 중인 다운로드가 없습니다.", running: "다운로드 중", resting: "묶음 다운로드 사이 휴식", success: "다운로드 완료", deferred: "구독 해제로 중단", error: "다운로드 실패" };
  document.getElementById("workflow-state").textContent = labels[progress.state] || "실행 중인 다운로드가 없습니다.";
  document.getElementById("workflow-next-run").textContent = data.next_run_at ? `다음 다운로드 예약: ${formatKoreanTime(data.next_run_at)} (한국 시간)` : "등록된 다운로드 예약이 없습니다.";
  const details = [];
  if (progress.title) details.push(progress.title);
  if (progress.total > 0) details.push(`${progress.completed || 0}/${progress.total}회차 완료`);
  if (progress.current_episode_no != null) details.push(`현재 ${progress.current_episode_no}회차`);
  if (progress.failed) details.push(`실패 ${progress.failed}개`);
  if (progress.rest_until) details.push(`${formatKoreanTimeOnly(progress.rest_until)}까지 휴식`);
  if (progress.error) details.push(progress.error);
  document.getElementById("workflow-progress-detail").textContent = details.join(" · ");
  const bar = document.getElementById("workflow-progress");
  bar.classList.toggle("hidden", !(progress.total > 0));
  bar.max = Math.max(1, Number(progress.total) || 1); bar.value = Number(progress.completed) || 0;
  const queue = document.getElementById("workflow-queue");
  queue.replaceChildren();
  for (const item of waiting) {
    const row = document.createElement("li");
    const content = document.createElement("div");
    const text = document.createElement("span");
    const queueDetails = [item.title, workflowQueueLabels[item.state], item.reason];
    if (item.state === "retry_download" && Number.isFinite(item.retry_at) && item.retry_at > 0) {
      queueDetails.push(`${formatKoreanTime(new Date(item.retry_at * 1000).toISOString())} 재시도 예정`);
    }
    text.textContent = queueDetails.filter(Boolean).join(" · ");
    const cancel = makeButton("대기 취소", async () => {
      cancel.disabled = true;
      try {
        await apiCall("/api/workflow/queue/cancel", { method: "POST", body: JSON.stringify({ platform: item.platform, title_id: String(item.title_id) }) });
        await refreshWorkflowStatus();
      } catch (e) { alert(e.message); cancel.disabled = item.state === "running"; }
    });
    cancel.disabled = item.state === "running";
    cancel.title = item.state !== "running" ? "구독과 받은 파일을 유지하고 이번 대기 작업만 취소" : "실행 중인 작업은 대기 취소할 수 없습니다.";
    content.append(text);
    if (["retry_download", "failed"].includes(item.state)) content.appendChild(makeWorkflowQueueButton(item));
    content.appendChild(cancel); row.appendChild(content); queue.appendChild(row);
  }
  if (!queue.children.length) queue.textContent = "다운로드 대기 작업이 없습니다.";
  document.getElementById("workflow-scope-edit-hint").textContent = data.download_busy
    ? "다운로드가 끝날 때까지 기존 구독의 범위 변경은 잠깁니다. 새 구독과 다운로드 대기열 추가는 계속할 수 있습니다."
    : (data.queue || []).length ? "대기열에 있는 작품은 대기 취소 또는 실행 완료 후 범위를 변경할 수 있습니다." : "";
  if (workflowSubscription?.editing && !workflowSubscribeSaving) {
    const reason = workflowScopeRestriction(workflowSubscription, data);
    if (reason) {
      document.getElementById("workflow-scope-fields").disabled = true;
      for (const id of ["workflow-subscribe-submit", "workflow-subscribe-download"]) document.getElementById(id).disabled = true;
      document.getElementById("workflow-subscribe-error").textContent = `${reason} 완료 후 범위 창을 다시 열어주세요.`;
    }
  }
  const list = document.getElementById("workflow-subscriptions");
  list.replaceChildren();
  for (const item of data.subscriptions || []) {
    const row = document.createElement("div");
    const text = document.createElement("span");
    const modeLabel = { all: "전체 공개 회차", latest: `최근 ${item.scope?.latest_count || 0}회차부터`, selected: "선택한 회차만", future: "새 회차만" }[item.scope?.mode] || "기존 구독 범위";
    const stateLabel = { ...workflowQueueLabels, available: "받은 회차 있음", idle: "대기 중", scheduled: "예약 대기", running: "다운로드 중", resting: "휴식 중", success: "완료", deferred: "구독 해제로 중단", error: "실패" }[item.status] || "구독 중";
    const waitingPosition = waiting.findIndex((row) => row.platform === item.platform && String(row.title_id) === String(item.title_id)) + 1;
    text.textContent = `${item.title} · ${stateLabel}${waitingPosition ? ` · 대기열 ${waitingPosition}번째` : ""} · 받은 회차 ${item.downloaded_count || 0}개 · ${modeLabel}`;
    const sourceLabel = item.added_source === "artist" ? "작가 자동 구독" : item.added_source === "tag" ? "태그 자동 구독" : null;
    if (sourceLabel) {
      const source = document.createElement("span");
      source.className = "badge subscription-source";
      source.dataset.addedSource = item.added_source;
      source.textContent = sourceLabel;
      text.append(document.createTextNode(" "), source);
    }
    const actions = document.createElement("div");
    actions.className = "workflow-subscription-actions";
    actions.append(makeWorkflowScopeButton(item), makeWorkflowQueueButton(item), makeWorkflowReadButton(item.title, item.platform, item.title_id));
    row.append(text, actions); list.appendChild(row);
  }
  if (!list.children.length) list.textContent = "구독한 작품이 없습니다. 통합 검색에서 받을 범위를 선택해 등록해주세요.";
  for (const button of document.querySelectorAll("[data-scope-title-id]")) {
    const reason = workflowScopeRestriction({ platform: button.dataset.scopePlatform, title_id: button.dataset.scopeTitleId });
    button.disabled = !!reason;
    button.title = reason || "다운로드 범위 변경";
  }
}

async function refreshWorkflowStatus() {
  if (workflowStatusPending) return;
  workflowStatusPending = true;
  try { renderWorkflowStatus(await apiCall("/api/workflow/status")); }
  catch (e) { document.getElementById("workflow-state").textContent = `현황 조회 실패: ${e.message}`; }
  finally { workflowStatusPending = false; }
}
document.getElementById("workflow-refresh").addEventListener("click", refreshWorkflowStatus);
refreshWorkflowStatus();
workflowStatusTimer = setInterval(() => { if (!document.hidden) refreshWorkflowStatus(); }, 5000);
window.addEventListener("pagehide", () => { clearInterval(workflowStatusTimer); clearTimeout(workflowReaderTimer); });
