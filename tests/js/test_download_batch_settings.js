const { makeApp, idle, ok, done } = require("./harness");
(async () => {
  const quiet = { start_hour: 23, start_minute: 0, end_hour: 6, end_minute: 0, days: [] };
  let server = { quiet_hours: [quiet], batch_minutes: 15 }, webhook = false, error = null;
  const posts = [], cron = { mode: "off", interval_minutes: 60, cron_times: [{ hour: 3, minute: 0, days: [] }] };
  const { w, run, calls } = makeApp({
    "/api/settings": { discovery_job: cron, download_job: [], archive_job: cron },
    "/api/settings/discord": () => ({ webhook_url_set: webhook, bot_token_set: false, notify_channel_id: "", bot_ready: false }),
    "/api/settings/report": (opts) => {
      if (opts.method === "POST") {
        if (error) throw new Error(error);
        const body = JSON.parse(opts.body);
        posts.push(body); server = { ...server, ...body };
      }
      return server;
    },
    "/api/settings/list-alert": () => ({ time: null, discord_configured: webhook, quiet_hours_set: server.quiet_hours.length > 0 }),
    "/api/settings/webtoon-server": { webtoon_server_url: "/viewer", integrated: true, read_only: true },
    "/api/settings/viewer-scan": { enabled: true, token_set: true, max_wait_minutes: 45 },
  });
  await idle(); run("loadSettingsPage();"); await idle();
  const $ = (id) => w.document.getElementById(id);
  const quietHour = () => $("report-quiet-list").querySelector(".quiet-start-hour");
  ok(w.document.querySelectorAll("#report-batch-minutes").length === 1 && $("settings-section-notifications").contains($("report-batch-minutes")), "리포트 묶음 처리 시간은 알림 메뉴에 한 벌만 있음");
  ok($("report-controls").disabled && !$("report-controls").contains($("report-batch-minutes")) && !$("btn-save-download-batch").disabled, "Discord 연결 전에 리포트 대기 값을 미리 저장할 수 있지만 뷰어 스캔 설정과 분리됨");
  ok($("report-batch-minutes").value === "15" && quietHour().value === "23", "기존 리포트 대기·알림 시간을 한 번에 불러옴");
  $("report-batch-minutes").value = "20"; $("btn-save-download-batch").click(); await idle();
  ok(JSON.stringify(posts.at(-1)) === '{"batch_minutes":20}' && server.quiet_hours[0].start_hour === 23, "웹훅 없이 대기 시간만 저장하고 기존 알림 시간은 보존");
  ok(/저장했습니다/.test($("download-batch-save-result").textContent), "리포트 대기 저장 결과를 자기 메뉴에 표시");

  webhook = true; run("loadDiscordSettings();"); await idle();
  quietHour().value = "22";
  $("report-batch-minutes").value = "25"; $("btn-save-download-batch").click(); await idle();
  ok(JSON.stringify(posts.at(-1)) === '{"batch_minutes":25}' && quietHour().value === "22" && server.quiet_hours[0].start_hour === 23, "리포트 대기 저장 응답이 알림의 미저장 초안을 덮지 않음");

  $("report-batch-minutes").value = "99";
  $("btn-save-report-settings").click(); await idle();
  ok(Object.keys(posts.at(-1)).join() === "quiet_hours" && posts.at(-1).quiet_hours[0].start_hour === 22, "알림 저장은 quiet_hours만 전송");
  ok(server.batch_minutes === 25 && $("report-batch-minutes").value === "99", "알림 저장이 리포트 묶음 설정과 미저장 대기 시간 초안을 바꾸지 않음");

  const before = posts.length;
  for (const invalid of ["", "-1", "0.5", "1441"]) {
    $("report-batch-minutes").value = invalid; $("btn-save-download-batch").click(); await idle();
    ok(posts.length === before && /0~1440/.test($("download-batch-save-result").textContent), `잘못된 리포트 대기 값은 요청 없이 안내: ${JSON.stringify(invalid)}`);
  }
  error = "대기 설정 저장 실패";
  $("report-batch-minutes").value = "30"; $("btn-save-download-batch").click(); await idle();
  ok($("report-batch-minutes").value === "30" && /저장 실패/.test($("download-batch-save-result").textContent) && quietHour().value === "22", "리포트 대기 저장 실패 시 양쪽 입력을 유지");
  const getCount = calls.filter((c) => c.url === "/api/settings/report" && c.method === "GET").length;
  run('activateSettingsSection("viewer"); activateSettingsSection("notifications"); activateSettingsSection("automation");'); await idle();
  ok(calls.filter((c) => c.url === "/api/settings/report" && c.method === "GET").length === getCount && $("report-batch-minutes").value === "30", "메뉴 이동은 리포트 묶음 설정을 다시 읽거나 초안을 초기화하지 않음");
  ok(/묶음 대기 시간과 무관/.test($("settings-card-links").textContent) && /다운로드 리포트 묶음 처리/.test($("settings-card-report").textContent) && /리포트 대기 시간의 영향을 받지 않습니다/.test($("settings-card-download-batch").textContent), "뷰어 스캔은 독립 처리하고 리포트 대기 시간은 알림 메뉴에서 정한다고 안내");
  done();
})();
