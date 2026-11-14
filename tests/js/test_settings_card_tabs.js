const { makeApp, idle, ok, done } = require("./harness");
(async () => {
  const cron = (h, m = 0, days = []) => ({ mode: "cron", interval_minutes: 60, cron_times: [{ hour: h, minute: m, days }], target: "naver" });
  let saved = null;
  const routes = {
    "/api/settings/kakao-webtoons-enabled": { enabled: true },
    "/api/settings/webtoon-server": { webtoon_server_url: "/viewer", integrated: true, read_only: true },
    "/api/settings": (o) => {
      if (o.method === "POST") { saved = JSON.parse(o.body); return saved; }
      return { discovery_job: cron(7, 0, ["mon"]), download_job: [{ ...cron(4, 30), target: "kakao" }], archive_job: cron(5, 0) };
    },
    "/api/settings/discord": { webhook_url_set: false, bot_token_set: false, notify_channel_id: "", bot_ready: false },
    "/api/settings/report": { quiet_hours: [], batch_minutes: 15 },
    "/api/webtoons": [], "/api/kakao-webtoons": [],
  };
  const { w, run, calls } = makeApp(routes); await idle();
  run("kakaoWebtoonsEnabled = true; window.__p = loadSettingsPage();"); await w.__p; await idle();
  const $ = (id) => w.document.getElementById(id);
  const card = $("settings-card-run");
  const tabs = () => [...card.querySelectorAll(".card-tabs .card-tab")];
  const visiblePanels = () => [...card.querySelectorAll(".card-tab-panel")].filter((p) => !p.classList.contains("hidden")).map((p) => p.dataset.panel);
  const click = (name) => tabs().find((t) => t.dataset.tab === name).click();
  const inPanel = (name, selector) => !!card.querySelector(`.card-tab-panel[data-panel="${name}"] ${selector}`);

  ok(JSON.stringify(tabs().map((t) => t.dataset.tab)) === JSON.stringify(["discovery_job", "download_job", "archive_job"]), "예약 카드에는 예약 종류 세 개만 있음");
  ok(!card.contains($("settings-panel-roots")) && !card.contains($("settings-panel-pacing")), "폴더와 속도는 예약 카드에서 분리됨");
  ok(card.querySelector(".card-tabs").getAttribute("role") === "tablist" && tabs().every((t) => t.getAttribute("role") === "tab"), "예약 탭의 접근성 역할 유지");
  ok(JSON.stringify(visiblePanels()) === '["discovery_job"]' && tabs()[0].getAttribute("aria-selected") === "true", "예약 기본 탭은 신작 스캔임");
  ok(inPanel("discovery_job", ".schedule-time-row") && inPanel("download_job", ".schedule-entry") && inPanel("archive_job", ".schedule-time-row"), "메뉴와 탭이 숨겨져도 세 예약 입력은 DOM에 유지됨");
  ok(/최소 10분 뒤/.test(card.querySelector('[data-panel="archive_job"]').textContent) && /여러 개 등록/.test(card.querySelector('[data-panel="download_job"]').textContent), "예약 간격과 복수 다운로드 예약 안내 유지");
  const footer = $("schedule-save-footer");
  ok(card.contains(footer) && !footer.classList.contains("hidden") && footer.contains($("btn-save-settings")), "모든 예약 탭에서 공용 저장 버튼을 사용할 수 있음");

  click("download_job");
  ok(JSON.stringify(visiblePanels()) === '["download_job"]' && !footer.classList.contains("hidden"), "다운로드 예약 탭으로 전환함");
  const interval = card.querySelector('[data-panel="download_job"] .schedule-interval');
  interval.value = "75";
  click("archive_job");
  ok(JSON.stringify(visiblePanels()) === '["archive_job"]' && !footer.classList.contains("hidden"), "아카이빙 예약 탭으로 전환함");
  click("download_job");
  ok(interval.value === "75", "하위 예약 탭을 바꿔도 미저장 입력을 유지함");
  const beforeNavigation = calls.length;
  run('activateSettingsSection("download"); activateSettingsSection("automation");'); await idle();
  ok(calls.length === beforeNavigation && interval.value === "75", "상위 메뉴 이동은 예약을 다시 읽거나 미저장 값을 지우지 않음");

  run('activateSettingsSection("management");');
  $("btn-save-settings").click(); await idle();
  ok(saved && Object.keys(saved).sort().join() === "archive_job,discovery_job,download_job" && saved.discovery_job.cron_times[0].days[0] === "mon" && saved.download_job[0].target === "kakao" && saved.download_job[0].interval_minutes === 75, "숨겨진 예약 종류와 수정한 입력까지 한 번에 저장함");
  ok($("settings-save-result").textContent === "저장했습니다.", "예약 저장 결과 유지");

  const kakao = $("settings-card-kakao");
  ok(["waitfree-sync-group", "waitfree-sync-toggle", "waitfree-schedule-box", "kakao-rental-auto-group"].every((id) => kakao.contains($(id))), "기다무와 대여권 설정은 카카오 메뉴에 그대로 있음");
  ok($("settings-card-links").contains($("viewer-scan-toggle")) && !$("settings-card-links").contains($("app-public-base-url")) && $("settings-card-notification-links").contains($("app-public-base-url")), "뷰어 스캔과 알림용 외부 주소를 용도에 맞게 분리함");
  done();
})();
