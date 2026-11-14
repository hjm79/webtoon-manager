const { makeApp, idle, ok, done } = require("./harness");
(async () => {
  const state = { webhook: false, unregistered: false };
  const cron = { mode: "off", interval_minutes: 60, cron_times: [{ hour: 3, minute: 0, days: [] }] };
  const routes = {
    "/api/settings": { discovery_job: cron, download_job: [], archive_job: cron },
    "/api/settings/kakao-webtoons-enabled": { enabled: true },
    "/api/settings/webtoon-server": { webtoon_server_url: "/viewer", integrated: true, read_only: true },
    "/api/settings/viewer-scan": { enabled: true, max_wait_minutes: 45, token_set: true },
    "/api/settings/report": { quiet_hours: [], batch_minutes: 15 },
    "/api/settings/list-alert": { time: null, discord_configured: false, quiet_hours_set: false },
    "/api/settings/discord": () => ({ webhook_url_set: state.webhook, bot_token_set: false, notify_channel_id: "", bot_ready: false }),
    "/api/settings/report-unregistered-new-episodes": (o) => {
      if (o.method === "POST") state.unregistered = JSON.parse(o.body).enabled;
      return { enabled: state.unregistered };
    },
  };
  const { w, run } = makeApp(routes); await idle();
  const $ = (id) => w.document.getElementById(id);
  const panel = (name) => w.document.querySelector(`[data-settings-panel="${name}"]`);
  run("window.__p = loadSettingsPage();"); await w.__p; await idle();

  const belongsTo = (name, ids) => ids.every((id) => panel(name).contains($(id)));
  ok(belongsTo("download", ["naver-download-root-picker", "kakao-download-root-picker", "btn-download-roots-save", "pacing-cap", "pacing-rest"]), "다운로드 폴더와 속도는 다운로드 메뉴에 함께 있음");
  ok(belongsTo("automation", ["settings-card-run", "author-auto-register-toggle"]), "예약·작가 추적 설정은 자동화 메뉴에 있음");
  ok(belongsTo("kakao", ["kp-cookie-json", "kakao-webtoons-enabled-toggle", "kakao-rental-auto-toggle", "waitfree-sync-toggle"]), "카카오 사용·로그인·대여권·기다무는 한 메뉴에 있음");
  ok(belongsTo("viewer", ["webtoon-server-url", "viewer-scan-toggle", "viewer-scan-token", "viewer-scan-max-wait"]), "뷰어 연결과 스캔은 독립 메뉴에 있음");
  ok(belongsTo("notifications", ["discord-webhook-url", "report-quiet-group", "app-public-base-url", "report-batch-minutes", "btn-save-download-batch"]), "알림 연결·리포트 묶음 대기·발송 제외 시간·외부 주소는 알림 메뉴에 있음");
  ok(belongsTo("management", ["btn-backup-download", "restore-file-input", "unsubscribe-history-list", "register-unsubscribed-title-id"]), "백업·복원과 구독 이력 관리는 관리 메뉴에 있음");

  ok(!$("settings-card-links").classList.contains("hidden") && !$("settings-card-report").classList.contains("hidden"), "웹훅이 없어도 뷰어와 리포트 카드는 메뉴 안에 존재함");
  ok($("report-controls").disabled && /디스코드|Discord/.test($("report-delivery-status").textContent), "미설정 알림은 연결 안내와 비활성 리포트 입력을 표시함");
  ok(!$("report-batch-minutes").disabled && !$("btn-save-download-batch").disabled && !$("report-controls").contains($("report-batch-minutes")), "리포트 대기 값은 연결 전에도 편집할 수 있으며 실제 발송 옵션과 분리됨");
  ok($("report-batch-minutes").value === "15" && $("viewer-scan-toggle").checked && $("viewer-scan-max-wait").value === "45", "웹훅과 무관하게 리포트 값과 뷰어 스캔 값이 불러와짐");
  ok($("webtoon-server-url").disabled && /같은 주소|통합|수집/.test($("viewer-connection-hint").textContent), "통합 뷰어 주소는 읽기 전용 상태로 안내함");
  ok(!$("kakao-no-webhook-hint").classList.contains("hidden") && /다운로드는 그대로/.test($("kakao-no-webhook-hint").textContent), "카카오 다운로드는 유지하고 알림 미설정만 안내함");

  run('activateSettingsSection("viewer");');
  state.webhook = true; run("window.__p = loadDiscordSettings();"); await w.__p; await idle();
  ok(!$("report-controls").disabled && !panel("viewer").classList.contains("hidden") && panel("notifications").classList.contains("hidden"), "웹훅 응답은 리포트만 활성화하고 선택하지 않은 알림 메뉴를 열지 않음");
  ok($("kakao-no-webhook-hint").classList.contains("hidden"), "웹훅 연결 시 카카오 알림 안내를 갱신함");
  run('activateSettingsSection("notifications");');
  const webhookInput = $("discord-webhook-url");
  webhookInput.focus();
  run("updateSettingsCardVisibility();");
  ok(webhookInput.dataset.masked === "false" && webhookInput.value === "" && !$("report-controls").disabled, "저장된 웹훅의 마스크를 편집하려고 지워도 리포트는 활성 상태를 유지함");
  webhookInput.value = "https://discord.com/api/webhooks/unsaved-draft";
  run('activateSettingsSection("viewer"); updateSettingsCardVisibility();');
  ok(!$("report-controls").disabled && !panel("viewer").classList.contains("hidden") && panel("notifications").classList.contains("hidden"), "미저장 웹훅 초안은 저장된 연결 상태나 선택 메뉴를 바꾸지 않음");
  webhookInput.value = "";
  run("updateSettingsCardVisibility();");
  ok(!$("report-controls").disabled && $("report-batch-minutes").value === "15" && panel("notifications").classList.contains("hidden"), "미저장 웹훅 입력이 비어도 기존 리포트 값과 메뉴 표시를 유지함");
  $("report-unregistered-toggle").checked = true; $("report-unregistered-toggle").dispatchEvent(new w.Event("change")); await idle();
  $("report-unregistered-toggle").checked = false; $("report-unregistered-toggle").dispatchEvent(new w.Event("change")); await idle();
  ok(state.unregistered === false && !$("settings-card-kakao").classList.contains("hidden"), "리포트 옵션은 카카오 기능 표시를 바꾸지 않음");
  state.webhook = false; run("window.__p = loadDiscordSettings();"); await w.__p; await idle();
  ok($("report-controls").disabled && $("report-batch-minutes").value === "15" && $("viewer-scan-toggle").checked, "알림 연결이 없어져도 리포트·뷰어 설정값은 보존함");
  done();
})();
