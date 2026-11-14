const { makeApp, idle, ok, done } = require("./harness");
(async () => {
  const cron = { mode: "off", interval_minutes: 60, cron_times: [{ hour: 3, minute: 0, days: [] }] };
  const routes = {
    "/api/settings": { discovery_job: cron, download_job: [], archive_job: cron },
    "/api/settings/kakao-webtoons-enabled": { enabled: true },
    "/api/settings/webtoon-server": { webtoon_server_url: "/viewer", integrated: true, read_only: true },
    "/api/settings/discord": { webhook_url_set: false, bot_token_set: false, notify_channel_id: "", bot_ready: false },
    "/api/settings/report": { quiet_hours: [], batch_minutes: 15 },
    "/api/webtoons": [], "/api/kakao-webtoons": [], "/api/webtoons/unsubscribe-history": [],
    "/api/registry": { authors: [], tags: [] },
    "/api/archive/settings": { local_available: false, rclone_available: false },
    "/api/archive/targets": [], "/api/archive/presets": [],
  };
  const { w, run, calls } = makeApp(routes); await idle();
  const $ = (id) => w.document.getElementById(id);
  const tabs = [...w.document.querySelectorAll(".settings-section-tab")];
  const panels = [...w.document.querySelectorAll(".settings-section-panel")];
  const names = ["download", "automation", "kakao", "viewer", "notifications", "management"];
  const tab = (name) => tabs.find((el) => el.dataset.settingsSection === name);
  const selected = () => tabs.filter((el) => el.getAttribute("aria-selected") === "true").map((el) => el.dataset.settingsSection);
  const visible = () => panels.filter((el) => !el.classList.contains("hidden")).map((el) => el.dataset.settingsPanel);
  run('switchToTab("settings");'); await idle();

  ok(JSON.stringify(tabs.map((el) => el.dataset.settingsSection)) === JSON.stringify(names) && panels.length === 6, "작업별 설정 메뉴와 패널 여섯 개만 있음");
  ok(tabs.every((el) => el.getAttribute("role") === "tab" && $(el.getAttribute("aria-controls"))) && panels.every((el) => el.getAttribute("role") === "tabpanel" && $(el.getAttribute("aria-labelledby"))), "상위 설정 메뉴와 패널이 접근성 속성으로 연결됨");
  ok(JSON.stringify(selected()) === '["download"]' && JSON.stringify(visible()) === '["download"]', "다운로드 설정이 기본 메뉴임");
  const ids = [...w.document.querySelectorAll("[id]")].map((el) => el.id);
  ok(ids.length === new Set(ids).size, "설정 이동으로 중복 DOM ID를 만들지 않음");
  ok(["naver-download-root-picker", "kp-cookie-json", "viewer-scan-toggle", "report-batch-minutes", "btn-backup-download", "btn-save-settings"].every((id) => w.document.querySelectorAll(`#${id}`).length === 1), "기존 설정 입력과 저장 버튼은 한 벌씩만 유지함");

  $("pacing-cap").value = "27";
  $("kp-cookie-json").value = "unsaved-cookie-draft";
  $("viewer-scan-token").value = "unsaved-token-draft";
  const fetches = calls.length;
  for (const name of names.slice(1)) tab(name).click();
  tab("download").click(); await idle();
  ok(calls.length === fetches, "설정 메뉴 이동은 API 재조회나 저장을 실행하지 않음");
  ok($("pacing-cap").value === "27" && $("kp-cookie-json").value === "unsaved-cookie-draft" && $("viewer-scan-token").value === "unsaved-token-draft", "다른 메뉴에 작성 중인 값까지 보존함");

  tab("download").focus();
  tab("download").dispatchEvent(new w.KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
  ok(JSON.stringify(selected()) === '["automation"]' && w.document.activeElement === tab("automation"), "오른쪽 방향키로 다음 메뉴를 선택하고 포커스를 이동함");
  tab("automation").dispatchEvent(new w.KeyboardEvent("keydown", { key: "End", bubbles: true }));
  ok(JSON.stringify(selected()) === '["management"]' && w.document.activeElement === tab("management"), "End로 마지막 메뉴에 이동함");
  tab("management").dispatchEvent(new w.KeyboardEvent("keydown", { key: "Home", bubbles: true }));
  ok(JSON.stringify(selected()) === '["download"]' && w.document.activeElement === tab("download"), "Home으로 첫 메뉴에 이동함");
  tab("download").dispatchEvent(new w.KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true }));
  ok(JSON.stringify(selected()) === '["management"]' && tabs.filter((el) => el.tabIndex === 0).length === 1, "왼쪽 방향키가 마지막 메뉴로 순환하고 선택 메뉴만 Tab 진입점을 가짐");

  const overview = $("workflow-overview");
  run('for (const name of Object.keys(pageLoaders)) pageLoaders[name] = () => {};');
  for (const name of ["settings", "history", "registry", "archive", "help", "manual-run", "library"]) {
    run(`switchToTab(${JSON.stringify(name)});`); await idle();
    ok(overview.classList.contains("hidden"), `${name}에서는 중복 다운로드 현황을 숨김`);
  }
  for (const name of ["naver-list", "unsubscribed", "excluded", "manual-download"]) {
    run(`switchToTab(${JSON.stringify(name)});`); await idle();
    ok(overview.classList.contains("hidden"), `${name}에서는 다운로드 현황을 숨김`);
  }
  run('switchToTab("downloads");'); await idle();
  ok(!overview.classList.contains("hidden"), "다운로드 현황 전용 탭에서만 표시");
  done();
})();
