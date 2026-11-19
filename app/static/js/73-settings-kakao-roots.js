// 설정: 카카오페이지 로그인, 다운로드 폴더·저장 공간, 뷰어 연동
// (index.html에서 파일 이름 순서대로 로드된다 — 클래식 스크립트라 최상위 선언은 전역으로 공유된다)
// ── 카카오페이지 로그인 쿠키 (설정) ─────────────────────────────

function kakaoAuthMessage(st) {
  if (st.state === "reauth_required") {
    if (st.auto_refresh?.state === "running") return "카카오페이지 세션 갱신 중입니다.";
    if (st.auto_refresh?.state === "unknown" && st.enabled) return "카카오페이지 자동 갱신 재시도 대기 중입니다.";
  }
  return st.message;
}

function renderKakaoPageCookieStatus(st) {
  const el = document.getElementById("kp-cookie-status");
  if (!st.saved) {
    el.textContent = st.message || "저장된 쿠키가 없습니다.";
    return;
  }
  const parts = ["쿠키 저장됨"];
  if (st.missing && st.missing.length) parts.push(`필수 쿠키 누락: ${st.missing.join(", ")}`);
  if (st.days_left !== null && st.days_left !== undefined) {
    parts.push(st.days_left < 0 ? "쿠키 만료 예상일 지남" : `쿠키 만료 예상일까지 약 ${st.days_left}일`);
  }
  const message = kakaoAuthMessage(st);
  if (message) parts.push(message);
  if (st.checked_at) parts.push(`마지막 확인: ${formatKoreanTime(st.checked_at)}`);
  if (st.auto_refresh) {
    const refresh = st.auto_refresh;
    parts.push(st.enabled ? "세션 자동 갱신 30분 주기" : "정기 세션 자동 갱신 꺼짐");
    if (refresh.last_success_at) parts.push(`마지막 갱신 성공: ${formatKoreanTime(refresh.last_success_at)}`);
    if (refresh.state === "running" && st.state !== "reauth_required") parts.push("세션 갱신 중");
    if (refresh.state === "reauth_required" && st.state === "reauth_required") {
      parts.push("자동 갱신도 거절되어 새 쿠키 등록 필요");
    } else if (refresh.reason === "refresh_rejected_session_valid" && st.state === "healthy") {
      parts.push("자동 갱신 거절 · 확인된 기존 로그인 사용 중");
    }
    if (refresh.state === "unknown" && (st.state !== "reauth_required" || !st.enabled)) {
      parts.push(st.enabled ? "자동 갱신 일시 실패 · 재시도 예정" : "자동 갱신 일시 실패");
    }
  }
  el.textContent = parts.join(" · ");
}

function renderKakaoAuthBanner(st, queue = []) {
  const banner = document.getElementById("kakao-auth-banner");
  if (!banner || !st) return;
  const blocked = queue.filter((item) => item.platform === "kakao" && ["blocked_auth", "retry_wait"].includes(item.state)).length;
  const warning = st.state === "healthy" && st.days_left != null && st.days_left <= 5;
  const visible = (st.enabled || blocked > 0) && (warning || st.state !== "healthy");
  banner.classList.toggle("hidden", !visible);
  if (!visible) return;
  const text = warning
    ? (st.days_left < 0 ? "카카오 쿠키 만료 예상일이 지났습니다. 최근 로그인 확인은 정상입니다."
      : `카카오 쿠키 만료 예상일까지 약 ${st.days_left}일입니다.`)
    : kakaoAuthMessage(st);
  const details = [text, blocked ? `다운로드 ${blocked}개 대기 · 선택 범위와 받은 파일 유지` : ""];
  if (st.checked_at) details.push(`마지막 확인: ${formatKoreanTime(st.checked_at)}`);
  document.getElementById("kakao-auth-banner-text").textContent = details.filter(Boolean).join(" · ");
}

document.getElementById("kakao-auth-settings").addEventListener("click", () => {
  document.getElementById("workspace-collection").click();
  document.querySelector('.main-tab[data-page="settings"]').click();
  activateSettingsSection("kakao", { focus: true });
});

async function loadKakaoPageCookieStatus() {
  try {
    renderKakaoPageCookieStatus(await apiCall("/api/settings/kakao-page-login"));
  } catch (e) {
    document.getElementById("kp-cookie-status").textContent = e.message;
  }
}

// ── 대여권 자동 사용 설정(기본 꺼짐) ────────────────────────────

async function loadKakaoRentalAutoSetting() {
  const controls = ["kakao-rental-auto-toggle", "btn-save-kakao-rental-auto"].map((id) => document.getElementById(id));
  controls.forEach((control) => control.disabled = true);
  try {
    const data = await apiCall("/api/settings/kakao-rental-ticket-auto-use");
    if (typeof data.enabled !== "boolean") throw new Error("대여권 자동 사용 설정을 확인하지 못했습니다.");
    document.getElementById("kakao-rental-auto-toggle").checked = data.enabled;
    controls.forEach((control) => control.disabled = false);
    document.getElementById("kakao-rental-auto-save-result").textContent = "";
  } catch (e) {
    document.getElementById("kakao-rental-auto-save-result").textContent = `${e.message} 저장은 차단했습니다. 설정 화면을 다시 열어주세요.`;
  }
}

document.getElementById("btn-save-kakao-rental-auto").addEventListener("click", async () => {
  const resultEl = document.getElementById("kakao-rental-auto-save-result");
  resultEl.textContent = "";
  try {
    await apiCall("/api/settings/kakao-rental-ticket-auto-use", {
      method: "POST",
      body: JSON.stringify({ enabled: document.getElementById("kakao-rental-auto-toggle").checked }),
    });
    resultEl.textContent = "저장했습니다.";
  } catch (e) {
    resultEl.textContent = e.message;
  }
});

// ── 기다무 충전 시각에 맞춰 자동 다운로드(옵션, 기본 꺼짐) + 다운로드 예정 목록 ───────────

// 남은 시간 표기: 곧 시작 / 1분 안에 / N분 후 / N시간 M분 후 / N일 M시간 후
function waitfreeRemainingText(seconds) {
  if (seconds <= 0) return "곧 시작";
  if (seconds < 60) return "1분 안에";
  const minutes = Math.floor(seconds / 60); // 내림 — 90초는 "1분 후"(남은 시간을 부풀려 보여 주지 않는다)
  if (minutes < 60) return `${minutes}분 후`;
  const hours = Math.floor(minutes / 60);
  const restMinutes = minutes % 60;
  if (hours < 24) return restMinutes ? `${hours}시간 ${restMinutes}분 후` : `${hours}시간 후`;
  const days = Math.floor(hours / 24);
  const restHours = hours % 24;
  return restHours ? `${days}일 ${restHours}시간 후` : `${days}일 후`;
}

// "10월 13일 21:01" — 한국시간
function formatKstShort(iso) {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("ko-KR", { timeZone: "Asia/Seoul", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false })
      .formatToParts(new Date(iso)).map((p) => [p.type, p.value])
  );
  const hour = parts.hour === "24" ? "00" : parts.hour;
  return `${parts.month}월 ${parts.day}일 ${hour}:${parts.minute}`;
}

async function refreshWaitfreeSchedule() {
  if (!document.getElementById("waitfree-sync-toggle").checked) return; // 꺼져 있으면 서버에 요청하지 않는다
  try {
    const data = await apiCall("/api/waitfree/schedule");
    const list = document.getElementById("waitfree-schedule-list");
    list.innerHTML = "";
    for (const item of data.items) {
      const li = document.createElement("li");
      const seconds = Math.round((new Date(item.due_at).getTime() - Date.now()) / 1000);
      li.innerHTML = `<span class="waitfree-time">${escapeHtml(formatKstShort(item.due_at))}</span><span class="waitfree-title">${escapeHtml(item.title)}</span><span class="waitfree-left">${escapeHtml(waitfreeRemainingText(seconds))}</span>`;
      list.appendChild(li);
    }
    document.getElementById("waitfree-schedule-empty").classList.toggle("hidden", data.items.length > 0);
  } catch (e) {
    // 조용히 무시 — 다음 갱신 때 다시 읽는다
  }
}

async function loadWaitfreeSyncSetting() {
  const toggle = document.getElementById("waitfree-sync-toggle");
  toggle.disabled = true;
  try {
    const data = await apiCall("/api/settings/waitfree-sync");
    if (typeof data.enabled !== "boolean") throw new Error("기다무 자동 다운로드 설정을 확인하지 못했습니다.");
    toggle.checked = data.enabled;
    toggle.disabled = false;
    document.getElementById("waitfree-sync-save-result").textContent = "";
    document.getElementById("waitfree-schedule-box").classList.toggle("hidden", !data.enabled);
    await refreshWaitfreeSchedule();
  } catch (e) {
    document.getElementById("waitfree-sync-save-result").textContent = `${e.message} 변경은 차단했습니다. 설정 화면을 다시 열어주세요.`;
  }
}

document.getElementById("waitfree-sync-toggle").addEventListener("change", async (ev) => {
  const toggle = ev.target;
  const resultEl = document.getElementById("waitfree-sync-save-result");
  resultEl.textContent = "";
  try {
    await apiCall("/api/settings/waitfree-sync", { method: "POST", body: JSON.stringify({ enabled: toggle.checked }) });
    resultEl.textContent = "저장했습니다.";
    document.getElementById("waitfree-schedule-box").classList.toggle("hidden", !toggle.checked);
    await refreshWaitfreeSchedule();
  } catch (e) {
    toggle.checked = !toggle.checked; // 저장하지 못했으니 원래 상태로 되돌린다
    resultEl.textContent = e.message;
  }
});

// 설정 화면을 보고 있는 동안 남은 시간을 30초마다 새로 읽는다(옵션이 꺼져 있으면 아무것도 하지 않는다)
setInterval(() => {
  const page = document.getElementById("page-settings");
  if (page && !page.classList.contains("hidden")) refreshWaitfreeSchedule();
}, 30000);

document.getElementById("btn-kp-cookie-save").addEventListener("click", async () => {
  const box = document.getElementById("kp-cookie-json");
  const button = document.getElementById("btn-kp-cookie-save");
  button.disabled = true;
  const statusEl = document.getElementById("kp-cookie-status");
  try {
    const st = await apiCall("/api/settings/kakao-page-login", { method: "POST", body: JSON.stringify({ cookies_json: box.value }) });
    box.value = ""; // 저장했으면 화면에 쿠키가 남아 있지 않게 비운다
    renderKakaoPageCookieStatus(st);
    await refreshWorkflowStatus();
  } catch (e) {
    statusEl.textContent = e.message;
  } finally {
    button.disabled = false;
  }
});

document.getElementById("btn-kp-cookie-check").addEventListener("click", async () => {
  const btn = document.getElementById("btn-kp-cookie-check");
  const statusEl = document.getElementById("kp-cookie-status");
  btn.disabled = true;
  statusEl.textContent = "확인 중...";
  try {
    const r = await apiCall("/api/settings/kakao-page-login/check", { method: "POST" });
    renderKakaoPageCookieStatus(r);
    await refreshWorkflowStatus();
  } catch (e) {
    statusEl.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
});

document.getElementById("btn-kp-cookie-delete").addEventListener("click", async () => {
  if (!confirm("저장된 카카오페이지 로그인 쿠키를 삭제할까요? 삭제하면 카카오페이지 다운로드를 할 수 없습니다.")) return;
  try {
    renderKakaoPageCookieStatus(await apiCall("/api/settings/kakao-page-login", { method: "DELETE" }));
    await refreshWorkflowStatus();
  } catch (e) {
    document.getElementById("kp-cookie-status").textContent = e.message;
  }
});

// ── 설정: 다운로드 폴더(네이버/카카오페이지 따로) ─────────────────

function syncDownloadRootsVisibility() {
  document.getElementById("kakao-download-root-group").classList.toggle("hidden", !kakaoWebtoonsEnabled);
  renderSharedDownloadStorage();
}

// 다운로드 폴더는 직접 입력하지 않고, 컨테이너에 마운트된 다운로드 폴더(base) 안에서 고르거나 새로 만든다.
// 폴더 목록/새 폴더 만들기는 아카이빙의 폴더 찾아보기와 같은 API(local_root=download_base)를 쓴다.
let downloadRootsInfo = null;
let downloadRootsLoaded = false;
const downloadRootDraft = { naver: "", kakao: "" }; // 저장 전에 고른 절대 경로("" = 기본)
const downloadRootBrowse = { naver: { open: false, path: "" }, kakao: { open: false, path: "" } };
const downloadStorageState = { naver: { path: null, storage: null, version: 0 }, kakao: { path: null, storage: null, version: 0 } };

function effectiveDownloadRootDraft(kind) {
  return downloadRootDraft[kind] || (kind === "kakao" ? downloadRootDraft.naver || downloadRootsInfo.base : downloadRootsInfo.base);
}

function formatStorageBytes(bytes) {
  if (!Number.isFinite(bytes)) return "확인 불가";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let value = bytes, index = 0;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index++; }
  return `${value.toLocaleString("ko-KR", { maximumFractionDigits: index === 0 ? 0 : 1 })} ${units[index]}`;
}

function renderSharedDownloadStorage() {
  const notice = document.getElementById("download-storage-shared");
  const a = downloadStorageState.naver.storage, b = downloadStorageState.kakao.storage;
  const same = kakaoWebtoonsEnabled && a?.status === "ok" && b?.status === "ok" && a.filesystem_id && a.filesystem_id === b.filesystem_id;
  notice.classList.toggle("hidden", !same);
  notice.textContent = same ? "네이버와 카카오페이지는 같은 디스크를 사용합니다. 표시된 남은 공간을 두 폴더가 공유합니다." : "";
}

function renderDownloadStorage(kind) {
  const box = document.getElementById(`${kind}-download-storage`);
  if (!box) return;
  const storage = downloadStorageState[kind].storage;
  box.replaceChildren();
  const summary = document.createElement("p");
  if (!storage) {
    summary.textContent = "디스크 용량을 확인하고 있습니다.";
  } else if (storage.status !== "ok") {
    const messages = { missing: "폴더가 없어 디스크 용량을 확인할 수 없습니다.", permission_denied: "폴더에 접근할 권한이 없어 용량을 확인할 수 없습니다.", remote: "원격 마운트의 용량은 이 화면에서 조회하지 않습니다.", not_mounted: "다운로드 디스크가 연결되지 않았습니다. 마운트를 확인해주세요.", timeout: "디스크 용량 조회 시간이 초과되었습니다." };
    summary.textContent = messages[storage.status] || "디스크 용량을 확인할 수 없습니다. 폴더와 마운트 상태를 확인해주세요.";
    box.classList.add("storage-unavailable");
  } else {
    box.classList.remove("storage-unavailable");
    summary.textContent = `남음 ${formatStorageBytes(storage.available_bytes)} / 전체 ${formatStorageBytes(storage.total_bytes)}`;
    const details = document.createElement("span");
    details.className = "storage-details";
    const percent = storage.total_bytes > 0 ? Math.min(100, Math.max(0, storage.used_bytes / storage.total_bytes * 100)) : 0;
    details.textContent = `사용 ${formatStorageBytes(storage.used_bytes)} (${percent.toFixed(1)}%)${storage.read_only ? " · 읽기 전용 저장소" : ""}`;
    const meter = document.createElement("progress");
    meter.max = Math.max(storage.total_bytes || 0, 1);
    meter.value = Math.max(0, storage.used_bytes || 0);
    meter.setAttribute("aria-label", `${kind === "naver" ? "네이버" : "카카오페이지"} 다운로드 디스크 사용률`);
    box.append(summary, details, meter);
    return;
  }
  box.appendChild(summary);
}

async function refreshDownloadStorage(kind, { force = false } = {}) {
  if (!downloadRootsInfo) return;
  const absolute = effectiveDownloadRootDraft(kind);
  const state = downloadStorageState[kind];
  if (!force && state.path === absolute && state.loading) { renderDownloadStorage(kind); return; }
  if (!force && state.path === absolute && state.storage) { renderDownloadStorage(kind); renderSharedDownloadStorage(); return; }
  const version = ++state.version;
  state.path = absolute;
  state.storage = null;
  state.loading = true;
  renderDownloadStorage(kind);
  renderSharedDownloadStorage();
  const params = new URLSearchParams({ kind });
  const savedKind = [kind, "naver", "kakao"].find((candidate) => downloadRootsInfo[candidate]?.effective === absolute);
  if (savedKind) {
    params.set("kind", savedKind);
  } else {
    const base = downloadRootsInfo.base.replace(/\/+$/, "");
    if (absolute !== base && !absolute.startsWith(base + "/")) {
      state.storage = { status: "unavailable" };
      state.loading = false;
      renderDownloadStorage(kind);
      return;
    }
    params.set("path", absolute === base ? "" : absolute.slice(base.length + 1));
  }
  try {
    const data = await apiCall(`/api/settings/download-storage?${params}`);
    if (version !== state.version || effectiveDownloadRootDraft(kind) !== absolute) return;
    state.storage = data.storage || { status: "unavailable" };
  } catch (_) {
    if (version !== state.version || effectiveDownloadRootDraft(kind) !== absolute) return;
    state.storage = { status: "unavailable" };
  }
  state.loading = false;
  renderDownloadStorage(kind);
  renderSharedDownloadStorage();
}

function downloadHostPath(absPath) {
  const info = downloadRootsInfo;
  if (!info || !info.host_path || !absPath) return "";
  const base = info.base.replace(/\/+$/, "");
  if (absPath !== base && !absPath.startsWith(base + "/")) return "";
  const rest = absPath.slice(base.length).replace(/^\/+/, "");
  if (!rest) return info.host_path;
  const sep = info.host_path.includes("\\") ? "\\" : "/";
  return info.host_path.replace(/[\\/]+$/, "") + sep + rest.split("/").join(sep);
}

function downloadRootLabel(kind) {
  const info = downloadRootsInfo;
  const draft = downloadRootDraft[kind];
  if (draft) return draft;
  return kind === "kakao" ? "네이버와 같은 폴더" : `기본 폴더 (${info.base})`;
}

async function renderDownloadRootPicker(kind) {
  const box = document.getElementById(`${kind}-download-root-picker`);
  const info = downloadRootsInfo;
  const browse = downloadRootBrowse[kind];
  box.innerHTML = "";

  const current = document.createElement("div");
  current.className = "drp-current";
  const shownAbs = effectiveDownloadRootDraft(kind);
  const host = downloadHostPath(shownAbs);
  const pathEl = document.createElement("span");
  pathEl.className = "drp-path";
  // 호스트 경로를 알면 그것을 앞에(사용자가 아는 실제 폴더), 컨테이너 안 경로는 괄호로 덧붙인다
  pathEl.textContent = host ? host : downloadRootLabel(kind);
  current.appendChild(pathEl);
  if (host) {
    const containerEl = document.createElement("span");
    containerEl.className = "drp-host";
    containerEl.textContent = downloadRootDraft[kind] ? `(컨테이너: ${downloadRootDraft[kind]})` : `(컨테이너: ${shownAbs}${kind === "kakao" ? ", 네이버와 같은 폴더" : ", 기본 폴더"})`;
    current.appendChild(containerEl);
  }
  current.appendChild(makeButton(browse.open ? "닫기" : "폴더 선택", () => {
    browse.open = !browse.open;
    renderDownloadRootPicker(kind);
  }));
  if (downloadRootDraft[kind]) {
    current.appendChild(makeButton(kind === "kakao" ? "네이버와 같게" : "기본으로", () => {
      downloadRootDraft[kind] = "";
      renderDownloadRootPicker(kind);
    }));
  }
  box.appendChild(current);
  const storage = document.createElement("div");
  storage.id = `${kind}-download-storage`;
  storage.className = "download-storage";
  storage.setAttribute("role", "status");
  storage.setAttribute("aria-live", "polite");
  box.appendChild(storage);
  refreshDownloadStorage(kind);
  if (kind === "naver" && !downloadRootDraft.kakao) renderDownloadRootPicker("kakao");
  if (!browse.open) return;

  const panel = document.createElement("div");
  panel.className = "drp-browser";
  const crumbs = document.createElement("div");
  crumbs.className = "drp-crumbs";
  const segments = browse.path ? browse.path.split("/") : [];
  crumbs.appendChild(makeButton(info.host_path || info.base, () => { browse.path = ""; renderDownloadRootPicker(kind); }));
  segments.forEach((name, index) => {
    crumbs.appendChild(document.createTextNode(" / "));
    const target = segments.slice(0, index + 1).join("/");
    crumbs.appendChild(makeButton(name, () => { browse.path = target; renderDownloadRootPicker(kind); }));
  });
  panel.appendChild(crumbs);

  const list = document.createElement("div");
  list.className = "drp-list";
  list.innerHTML = '<span class="hint-inline">불러오는 중...</span>';
  panel.appendChild(list);

  const actions = document.createElement("div");
  actions.className = "drp-actions";
  const absHere = browse.path ? `${info.base}/${browse.path}` : info.base;
  actions.appendChild(makeButton("이 폴더 선택", () => {
    downloadRootDraft[kind] = absHere;
    browse.open = false;
    renderDownloadRootPicker(kind);
  }));
  actions.appendChild(makeButton("새 폴더 만들기", async () => {
    const name = (prompt("새 폴더 이름") || "").trim();
    if (!name) return;
    if (/[\\/]/.test(name)) { alert("폴더 이름에는 / 나 \\ 를 쓸 수 없습니다."); return; }
    try {
      await apiCall("/api/archive/folders", { method: "POST", body: JSON.stringify({ path: browse.path ? `${browse.path}/${name}` : name, root: "download_base" }) });
      renderDownloadRootPicker(kind);
    } catch (e) {
      alert(e.message);
    }
  }));
  panel.appendChild(actions);
  box.appendChild(panel);

  try {
    const data = await apiCall(`/api/archive/folders?path=${encodeURIComponent(browse.path)}&local_root=download_base`);
    list.innerHTML = "";
    if (data.folders.length === 0) list.innerHTML = '<span class="hint-inline">하위 폴더가 없습니다.</span>';
    for (const folder of data.folders) {
      list.appendChild(makeButton(`📁 ${folder.name}`, () => { browse.path = folder.path; renderDownloadRootPicker(kind); }));
    }
  } catch (e) {
    list.innerHTML = `<span class="error">${escapeHtml(e.message)}</span>`;
  }
}

async function loadDownloadRoots() {
  const statusEl = document.getElementById("download-roots-status");
  const save = document.getElementById("btn-download-roots-save");
  save.disabled = true;
  downloadRootsLoaded = false;
  syncDownloadRootsVisibility();
  try {
    const data = await apiCall("/api/settings/download-roots");
    if (typeof data.naver?.path !== "string" || typeof data.kakao?.path !== "string" || !data.base) throw new Error("저장된 다운로드 폴더를 확인하지 못했습니다.");
    downloadRootsInfo = data;
    downloadRootsLoaded = true;
    downloadRootDraft.naver = data.naver.path || "";
    downloadRootDraft.kakao = data.kakao.path || "";
    for (const kind of ["naver", "kakao"]) {
      downloadStorageState[kind].path = data[kind].effective;
      downloadStorageState[kind].storage = data[kind].storage || null;
      downloadStorageState[kind].loading = false;
      downloadStorageState[kind].version++;
    }
    renderDownloadRootPicker("naver");
    renderDownloadRootPicker("kakao");
    statusEl.classList.remove("error");
    statusEl.textContent = "저장된 폴더를 표시했습니다. 새로 선택한 폴더는 저장해야 적용됩니다.";
    save.disabled = false;
  } catch (e) {
    statusEl.classList.add("error");
    statusEl.textContent = `${e.message} 저장은 차단했습니다. 설정 화면을 다시 열어주세요.`;
  }
}

document.getElementById("btn-download-roots-save").addEventListener("click", async () => {
  const statusEl = document.getElementById("download-roots-status");
  if (!downloadRootsLoaded) {
    statusEl.textContent = "저장된 다운로드 폴더를 먼저 확인해야 합니다. 설정 화면을 다시 열어주세요.";
    return;
  }
  statusEl.classList.remove("error");
  statusEl.textContent = "";
  try {
    const body = { naver: downloadRootDraft.naver, kakao: downloadRootDraft.kakao };
    const data = await apiCall("/api/settings/download-roots", { method: "POST", body: JSON.stringify(body) });
    downloadRootsInfo = data;
    for (const kind of ["naver", "kakao"]) {
      downloadStorageState[kind].path = data[kind].effective;
      downloadStorageState[kind].storage = data[kind].storage || null;
      downloadStorageState[kind].loading = false;
      downloadStorageState[kind].version++;
      renderDownloadStorage(kind);
    }
    renderSharedDownloadStorage();
    statusEl.textContent = "폴더 설정을 저장했습니다. 기존 파일은 이동하지 않습니다.";
  } catch (e) {
    statusEl.classList.add("error");
    statusEl.textContent = e.message;
  }
});

document.getElementById("btn-download-storage-refresh").addEventListener("click", () => {
  refreshDownloadStorage("naver", { force: true });
  refreshDownloadStorage("kakao", { force: true });
});

// 뷰어 스캔 설정은 뷰어 서버 주소가 저장돼 있을 때만 보인다(주소가 없으면 스캔을 시킬 곳이 없다). 입력만 하고 저장하기 전에는 바뀌지 않는다.
function showViewerScanBox(savedUrl) {
  document.getElementById("viewer-scan-toggle").closest(".viewer-scan-box").classList.toggle("hidden", !savedUrl);
}

async function loadWebtoonServerUrl() {
  try {
    const data = await apiCall("/api/settings/webtoon-server");
    document.getElementById("webtoon-server-url").value = data.webtoon_server_url;
    document.getElementById("webtoon-server-url").disabled = Boolean(data.read_only);
    document.getElementById("btn-save-webtoon-server-url").disabled = Boolean(data.read_only);
    document.getElementById("viewer-mode-status").textContent = data.integrated
      ? "통합 뷰어 사용 중 · 수집·내 서재·읽기가 같은 주소에서 실행됩니다."
      : "별도 뷰어 연결 · 다운로드 후 스캔과 읽기 연동에 사용할 주소를 설정합니다.";
    document.getElementById("viewer-connection-hint").textContent = data.integrated
      ? "수집·내 서재·읽기가 같은 주소로 연결되어 있습니다. 별도 뷰어 주소를 입력할 필요가 없습니다."
      : "입력해두면 리포트에 각 작품의 바로가기 링크가 붙고, 구독 카드에서 뷰어로 이동할 수 있습니다.";
    showViewerScanBox(data.webtoon_server_url);
  } catch (e) {
    document.getElementById("viewer-mode-status").textContent = "뷰어 연결 상태를 불러오지 못했습니다. 설정 화면을 다시 열어 확인해주세요.";
  }
}

document.getElementById("btn-save-webtoon-server-url").addEventListener("click", async () => {
  const resultEl = document.getElementById("webtoon-server-save-result");
  resultEl.textContent = "";
  try {
    const url = document.getElementById("webtoon-server-url").value.trim();
    await apiCall("/api/settings/webtoon-server", {
      method: "POST",
      body: JSON.stringify({ webtoon_server_url: url }),
    });
    showViewerScanBox(url);
    resultEl.style.color = "";
    resultEl.textContent = "저장했습니다.";
  } catch (e) {
    resultEl.textContent = e.message;
  }
});

async function loadAppPublicBaseUrl() {
  try {
    const data = await apiCall("/api/settings/app-public-base-url");
    document.getElementById("app-public-base-url").value = data.app_public_base_url;
  } catch (e) {
    // 조용히 무시 — 이 필드 하나 때문에 설정 탭 전체 로드가 막히면 안 됨
  }
}

document.getElementById("btn-save-app-public-base-url").addEventListener("click", async () => {
  const resultEl = document.getElementById("app-public-base-url-save-result");
  resultEl.textContent = "";
  try {
    const url = document.getElementById("app-public-base-url").value.trim();
    await apiCall("/api/settings/app-public-base-url", {
      method: "POST",
      body: JSON.stringify({ app_public_base_url: url }),
    });
    resultEl.style.color = "";
    resultEl.textContent = "저장했습니다.";
  } catch (e) {
    resultEl.textContent = e.message;
  }
});

async function loadUnregisteredNewEpisodesToggle() {
  try {
    const data = await apiCall("/api/settings/report-unregistered-new-episodes");
    document.getElementById("report-unregistered-toggle").checked = data.enabled;
  } catch (e) {
    // 조용히 무시 — 이 필드 하나 때문에 설정 탭 전체 로드가 막히면 안 됨
  }
}

document.getElementById("report-unregistered-toggle").addEventListener("change", async (e) => {
  try {
    await apiCall("/api/settings/report-unregistered-new-episodes", {
      method: "POST",
      body: JSON.stringify({ enabled: e.target.checked }),
    });
  } catch (err) {
    alert(err.message);
    e.target.checked = !e.target.checked; // 저장 실패하면 화면도 원래대로 되돌림
  }
});

async function loadKakaoWebtoonsEnabledToggle() {
  try {
    const data = await apiCall("/api/settings/kakao-webtoons-enabled");
    setKakaoWebtoonsEnabled(data.enabled);
    document.getElementById("kakao-webtoons-enabled-toggle").checked = data.enabled;
    syncDownloadRootsVisibility();
  } catch (e) {
    // 조용히 무시 — 이 필드 하나 때문에 설정 탭 전체 로드가 막히면 안 됨
  }
}

document.getElementById("kakao-webtoons-enabled-toggle").addEventListener("change", async (e) => {
  try {
    await apiCall("/api/settings/kakao-webtoons-enabled", {
      method: "POST",
      body: JSON.stringify({ enabled: e.target.checked }),
    });
    setKakaoWebtoonsEnabled(e.target.checked);
    syncDownloadRootsVisibility();
  } catch (err) {
    alert(err.message);
    e.target.checked = !e.target.checked;
  }
});

// 진행 상황 로그(수동 실행/아카이빙/폴더 일괄 이동/수동 다운로드 공통)의 스크롤 규칙:
// 맨 아래를 보고 있으면 새 줄을 따라 내려가고, 위로 올려 읽는 중이면 그 위치를 그대로 두고, 다시 맨 아래(근처)로 내리면
// 따라가기를 다시 시작한다. 2초마다 상태를 받아 오지만 새 줄이 없으면 다시 그리지 않는다(텍스트 선택/스크롤이 풀리지 않게).
const LOG_FOLLOW_THRESHOLD_PX = 24; // 맨 아래에서 이만큼 안쪽이면 "맨 아래를 보고 있다"로 본다
const renderedJobLogs = new Map(); // jobName → 마지막으로 그린 로그(같으면 다시 그리지 않음)

function renderJobLog(jobName, lines) {
  const container = document.getElementById(`${jobName}-log`);
  const signature = lines.join("\n");
  if (renderedJobLogs.get(jobName) === signature) return;
  const firstRender = !renderedJobLogs.has(jobName);
  const following = firstRender || container.scrollHeight - container.scrollTop - container.clientHeight <= LOG_FOLLOW_THRESHOLD_PX;
  const previousTop = container.scrollTop;
  container.innerHTML = lines
    .map((line) => {
      const separatorIndex = line.indexOf(" — ");
      const timestamp = separatorIndex >= 0 ? line.slice(0, separatorIndex) : "";
      const message = separatorIndex >= 0 ? line.slice(separatorIndex + 3) : line;
      const timeLabel = timestamp ? formatKoreanTimeOnly(timestamp) : "";
      const isError = /오류|실패/.test(message);
      return `<div class="log-line${isError ? " log-error" : ""}"><span class="log-time">${escapeHtml(timeLabel)}</span>${escapeHtml(message)}</div>`;
    })
    .join("");
  renderedJobLogs.set(jobName, signature);
  container.scrollTop = following ? container.scrollHeight : previousTop;
}

async function refreshJobStatus() {
  let statuses;
  try {
    statuses = await apiCall("/api/jobs/status");
  } catch (e) {
    return;
  }

  for (const jobName of ["discovery", "quick_scan", "download", "metadata_sync", "report"]) {
    const st = statuses[jobName];
    if (!st) continue;
    const badge = document.getElementById(`${jobName}-status-badge`);
    badge.textContent = st.status;
    badge.className = `badge job-${st.status}`;
    renderJobLog(jobName, st.log);
  }
}

function startJobPolling() {
  stopJobPolling();
  jobPollTimer = setInterval(refreshJobStatus, 2000);
}

function stopJobPolling() {
  if (jobPollTimer) {
    clearInterval(jobPollTimer);
    jobPollTimer = null;
  }
}
