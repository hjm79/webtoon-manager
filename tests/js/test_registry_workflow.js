const { makeApp, idle, ok, done } = require('./harness');
(async () => {
  let resolveSearch, selected=[], entries=[{platform:'naver',title_id:'300',title:'발견 작품',sources:[{name:'작가',kind:'artist',id:'w'}]}];
  const {w,run,calls}=makeApp({
    '/api/authors/search': () => new Promise((resolve)=>{resolveSearch=resolve;}),
    '/api/watched-authors': (opts)=>{ selected.push(JSON.parse(opts.body));return {}; },
    '/api/authors/interested': [], '/api/authors/candidates': [],
    '/api/registry/discoveries': ()=>entries,
    '/api/workflow/preview': {platform:'naver',title_id:'300',title:'발견 작품',episodes:[{episode_no:1,available:true,owned:false}],current_scope:{mode:'all'},subscription:'unregistered'},
  });
  await idle(); const $=(id)=>w.document.getElementById(id);
  run('searchAndRegisterAuthor("동명");');
  ok($('registry-author-dialog').open,'작가 검색은 선택창을 먼저 열고 자동 등록하지 않음');
  $('registry-author-close').click();
  resolveSearch([{author_id:'1',author_name:'동명',sample_title:'작품A'}]);await idle();
  ok(!selected.length && !$('registry-author-dialog').open && !$('registry-author-results').children.length,'취소 뒤 늦은 응답은 무시');
  run('searchAndRegisterAuthor("동명");');
  resolveSearch([{author_id:'1',author_name:'동명',sample_title:'작품A'}, {author_id:'2',author_name:'동명',sample_title:'<img src=x onerror=alert(1)>'}]);await idle();
  const buttons=$('registry-author-results').querySelectorAll('button');
  ok(buttons.length===2 && !selected.length && buttons[1].textContent.includes('ID 2'),'동명이인 대표 작품과 ID를 표시하고 선택 전 저장하지 않음');
  ok(!$('registry-author-results').querySelector('img'),'대표 작품 제목은 안전한 텍스트로 표시');
  buttons[1].click();await idle();
  ok(selected.length===1 && selected[0].author_id==='2' && !$('registry-author-dialog').open,'선택한 두 번째 작가만 저장');
  run('loadRegistryDiscoveries();');await idle();
  ok($('registry-discoveries').textContent.includes('발견 작품') && !calls.some(c=>/workflow\/subscribe/.test(c.url)),'신작 후보 조회만으로 구독하지 않음');
  $('registry-discoveries').querySelector('button').click();await idle();
  ok($('workflow-subscribe-dialog').open && $('workflow-subscribe-title').textContent.includes('발견 작품'),'후보에서 기존 회차·범위 선택 화면으로 연결');
  $('workflow-subscribe-cancel').click();await idle();
  ok(!calls.some(c=>/workflow\/subscribe/.test(c.url)),'범위 선택 취소는 구독·다운로드를 생성하지 않음');
  entries=[];run('loadRegistryDiscoveries();');await idle();
  ok($('registry-discoveries').textContent.includes('기준선'),'후보 없을 때 첫 스캔 기준선을 설명');
  done();
})();
