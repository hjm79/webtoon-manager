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

function workflowReaderMessage(status, message) {
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

function makeWorkflowReadButton(title, platform) {
  const button = makeButton("읽기", () => openEmbeddedReader(title, platform));
  button.classList.add("workflow-read-button");
  button.dataset.viewerCheckTitle = title;
  button.dataset.viewerCheckPlatform = platform || "naver";
  button.title = "다운로드한 회차를 관리기 안에서 읽기";
  return button;
}

function makeWorkflowScopeButton(item) {
  const button = makeButton("범위 변경", () => chooseSubscriptionScope(item, { edit: true }));
  button.dataset.scopeTitleId = item.title_id;
  button.dataset.scopePlatform = item.platform || "naver";
  button.title = `${item.title}의 다운로드 범위 변경`;
  return button;
}

async function openEmbeddedReader(title, platform) {
  const generation = ++workflowReaderGeneration;
  const dialog = document.getElementById("workflow-reader-dialog");
  const frame = document.getElementById("workflow-reader-frame");
  const external = document.getElementById("workflow-reader-external");
  const status = document.getElementById("workflow-reader-status");
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
  status.textContent = "읽을 수 있는 회차를 확인하고 있습니다…";
  showWorkflowDialog(dialog);
  try {
    const data = await apiCall(`/api/reader/status?title=${encodeURIComponent(title)}&platform=${encodeURIComponent(platform || "naver")}`);
    if (generation !== workflowReaderGeneration || !dialog.open) return;
    const url = workflowReaderUrl(data.url);
    if (data.status !== "ready" || !url) {
      status.textContent = workflowReaderMessage(data.status, data.message);
      return;
    }
    external.href = url.href;
    external.classList.remove("hidden");
    workflowReaderOrigin = url.origin;
    url.searchParams.set("embed", "1");
    url.searchParams.set("parent_origin", window.location.origin);
    frame.src = url.href;
    frame.classList.remove("hidden");
    status.textContent = "리더를 여는 중…";
    workflowReaderTimer = setTimeout(() => {
      if (generation === workflowReaderGeneration && dialog.open) {
        status.textContent = "리더가 열리지 않으면 ‘별도 뷰어에서 열기’를 눌러주세요. 로그인이나 뷰어 연결 설정을 확인할 수 있습니다.";
      }
    }, 12000);
  } catch (e) {
    if (generation === workflowReaderGeneration) status.textContent = `뷰어 연결 실패: ${e.message}`;
  }
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
  closeWorkflowDialog(document.getElementById("workflow-reader-dialog"));
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
      const platformLabel = item.platform === "kakao" ? "카카오페이지" : "네이버";
      const info = document.createElement("div");
      info.className = "webtoon-card-body";
      const heading = document.createElement("strong");
      heading.textContent = item.title;
      const meta = document.createElement("p");
      meta.className = "hint";
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
      actions.append(subscribe, editScope, makeWorkflowReadButton(item.title, item.platform), makeButton("회차 확인", () => openManualAnalysis(item.platform, item.title_id)));
      card.append(info, actions);
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
  document.getElementById("workflow-subscribe-form").reset();
  setWorkflowScopeMode("latest");
  document.getElementById("workflow-subscribe-title").textContent = `${item.title} · ${edit ? "다운로드 범위 변경" : "구독과 다운로드 범위"}`;
  document.getElementById("workflow-scope-legend").textContent = edit ? "앞으로 받을 회차" : "처음 받을 회차";
  document.getElementById("workflow-preserve-hint").classList.toggle("hidden", !edit);
  document.getElementById("workflow-start-hint").textContent = edit
    ? "체크하지 않으면 범위만 저장하고 기존 다운로드 예약을 유지합니다."
    : "체크하지 않으면 다음 다운로드 예약에 등록합니다.";
  submit.textContent = edit ? "범위 저장" : "선택한 범위로 구독";
  document.getElementById("workflow-episode-picker").replaceChildren();
  document.getElementById("workflow-episode-picker").classList.add("hidden");
  status.textContent = "받을 수 있는 회차를 확인하고 있습니다…";
  error.textContent = "";
  submit.disabled = true;
  showWorkflowDialog(dialog);
  const result = new Promise((resolve) => { workflowSubscribeResolve = resolve; });
  try {
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
    submit.disabled = false;
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
  document.getElementById("workflow-episode-picker").classList.toggle("hidden", mode !== "selected");
  document.getElementById("workflow-latest-count").disabled = mode !== "latest";
  const start = document.getElementById("workflow-start-now");
  start.disabled = mode === "future";
  if (start.disabled) start.checked = false;
}

document.querySelectorAll('input[name="workflow-scope"]').forEach((input) => input.addEventListener("change", () => {
  setWorkflowScopeMode(input.value);
}));
for (const id of ["workflow-subscribe-close", "workflow-subscribe-cancel"]) document.getElementById(id).addEventListener("click", () => closeSubscriptionScope());
document.getElementById("workflow-subscribe-dialog").addEventListener("cancel", (event) => { event.preventDefault(); closeSubscriptionScope(); });

document.getElementById("workflow-subscribe-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!workflowSubscription) return;
  const submit = document.getElementById("workflow-subscribe-submit");
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
  submit.disabled = true;
  workflowSubscribeSaving = true;
  for (const id of ["workflow-subscribe-close", "workflow-subscribe-cancel"]) document.getElementById(id).disabled = true;
  error.textContent = "";
  try {
    const payload = {
      platform: item.platform, title_id: String(item.title_id), title: item.title,
      thumbnail_url: item.thumbnail_url || "", author_summary: item.author_summary || "", scope,
      start_now: mode !== "future" && document.getElementById("workflow-start-now").checked,
    };
    if (item.editing) {
      delete payload.title; delete payload.thumbnail_url; delete payload.author_summary;
    }
    const data = await apiCall(item.editing ? "/api/workflow/scope" : "/api/workflow/subscribe", { method: "POST", body: JSON.stringify(payload) });
    if (workflowSubscription !== item) return;
    invalidateListCaches();
    workflowSubscribeSaving = false;
    closeSubscriptionScope(data);
    refreshWorkflowStatus();
    if (!document.getElementById("page-naver-list").classList.contains("hidden")) loadNaverList(false);
  } catch (e) { if (workflowSubscription === item) { error.textContent = e.message; submit.disabled = false; } }
  finally {
    workflowSubscribeSaving = false;
    for (const id of ["workflow-subscribe-close", "workflow-subscribe-cancel"]) document.getElementById(id).disabled = false;
  }
});

function renderWorkflowStatus(data) {
  const progress = data.progress || {};
  const labels = { idle: "대기 중", queued: "다운로드 대기", running: "다운로드 중", resting: "묶음 다운로드 사이 휴식", success: "다운로드 완료", error: "다운로드 실패" };
  document.getElementById("workflow-state").textContent = labels[progress.state] || "대기 중";
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
  const list = document.getElementById("workflow-subscriptions");
  list.replaceChildren();
  for (const item of data.subscriptions || []) {
    const row = document.createElement("div");
    const text = document.createElement("span");
    const modeLabel = { all: "전체 공개 회차", latest: `최근 ${item.scope?.latest_count || 0}회차부터`, selected: "선택한 회차만", future: "새 회차만" }[item.scope?.mode] || "기존 구독 범위";
    const stateLabel = { available: "받은 회차 있음", idle: "대기 중", scheduled: "예약 대기", queued: "다운로드 대기", running: "다운로드 중", resting: "휴식 중", success: "완료", error: "실패" }[item.status] || "구독 중";
    text.textContent = `${item.title} · ${stateLabel} · 받은 회차 ${item.downloaded_count || 0}개 · ${modeLabel}`;
    const actions = document.createElement("div");
    actions.className = "workflow-subscription-actions";
    actions.append(makeWorkflowScopeButton(item), makeWorkflowReadButton(item.title, item.platform));
    row.append(text, actions); list.appendChild(row);
  }
  if (!list.children.length) list.textContent = "구독한 작품이 없습니다. 통합 검색에서 받을 범위를 선택해 등록해주세요.";
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
