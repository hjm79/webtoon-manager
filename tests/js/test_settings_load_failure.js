const { makeApp, idle, ok, done } = require("./harness");
(async () => {
  let fail = false;
  const cron = { mode: "off", interval_minutes: 60, cron_times: [{ hour: 3, minute: 0, days: [] }], target: "naver" };
  const replies = {
    "/api/settings": { discovery_job: cron, download_job: [cron], archive_job: cron },
    "/api/settings/author-auto-register": { enabled: true },
    "/api/settings/auto-subscribe": { enabled: false },
    "/api/settings/download-pacing": { max_new_episodes_per_title: 10, batch_rest_minutes: 5, default_max_new_episodes_per_title: 10, default_batch_rest_minutes: 5, customized: false },
    "/api/settings/download-roots": { base: "/downloads", naver: { path: "/downloads/naver", effective: "/downloads/naver" }, kakao: { path: "/downloads/kakao", effective: "/downloads/kakao" } },
    "/api/settings/report": { quiet_hours: [], batch_minutes: 10 },
    "/api/settings/viewer-scan": { enabled: true, token_set: true, max_wait_minutes: 45, ready: true },
    "/api/settings/kakao-rental-ticket-auto-use": { enabled: false },
    "/api/settings/waitfree-sync": { enabled: false },
  };
  const routes = Object.fromEntries(Object.keys(replies).map((url) => [url, (opts) => {
    if (fail) throw new Error("설정 조회 실패");
    return replies[url];
  }]));
  const { w, run, calls } = makeApp(routes);
  const $ = (id) => w.document.getElementById(id);
  const controls = ["btn-save-settings", "author-auto-register-toggle", "btn-save-author-auto-register", "pacing-cap", "pacing-rest", "btn-save-pacing", "btn-download-roots-save", "report-batch-minutes", "btn-save-download-batch", "btn-save-report-settings", "btn-add-report-quiet", "viewer-scan-toggle", "viewer-scan-token", "viewer-scan-max-wait", "btn-save-viewer-scan", "btn-clear-viewer-scan-token", "kakao-rental-auto-toggle", "btn-save-kakao-rental-auto", "waitfree-sync-toggle"];
  await idle(); run("loadSettingsPage();"); await idle();
  ok(controls.every((id) => !$(id).disabled), "정상 조회 후 설정별 컨트롤을 활성화");
  fail = true; run("loadSettingsPage();"); await idle();
  ok(controls.every((id) => $(id).disabled) && $("btn-reset-pacing").disabled, "설정 조회 실패 시 기존 화면 값으로도 저장·초기화할 수 없음");
  const postsBefore = calls.filter((c) => c.method === "POST").length;
  for (const id of controls.filter((id) => id.startsWith("btn-"))) $(id).click();
  await idle();
  ok(calls.filter((c) => c.method === "POST").length === postsBefore, "조회 실패 화면에서 저장을 시도해도 변경 요청 없음");
  for (const id of ["author-auto-register-save-result", "pacing-save-result", "download-roots-status", "report-settings-save-result", "download-batch-save-result", "viewer-scan-save-result", "kakao-rental-auto-save-result", "waitfree-sync-save-result"]) {
    ok(/조회 실패/.test($(id).textContent), `${id}에 조회 실패와 재확인 안내 표시`);
  }
  fail = false; run("loadSettingsPage();"); await idle();
  ok(controls.every((id) => !$(id).disabled) && $("author-auto-register-toggle").checked, "다시 정상 조회하면 저장된 값으로 복구하고 편집을 활성화");
  ok(!$("auto-subscribe-toggle").checked && $("btn-reset-pacing").disabled, "자동 구독 OFF와 기본 속도 상태를 복구 과정에서도 유지");
  replies["/api/settings/author-auto-register"] = { enabled: "true" };
  replies["/api/settings/viewer-scan"] = { enabled: "true", token_set: true, max_wait_minutes: 45 };
  run("loadAuthorAutoRegisterSetting(); loadViewerScanSettings();"); await idle();
  ok($("btn-save-author-auto-register").disabled && $("btn-save-viewer-scan").disabled, "형식이 잘못된 설정 응답도 실제 상태로 취급하지 않음");
  done();
})();
