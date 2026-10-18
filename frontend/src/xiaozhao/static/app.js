// 小照 LIFF 網頁（設計文件 2.1 節、5.1 節）。
// 照護員：錄音模式（下方為確認紀錄）、我的案件、今日狀況回報；督導：督導頁面、紀錄確認；管理員：帳號與綁定碼，可切換三種身分。
'use strict';

const ROLE_NAMES = { admin: '管理員', supervisor: '督導', caregiver: '照護員' };
const ROLE_TABS = {
  caregiver: [['home', '主頁'], ['record', '錄音模式'], ['cases', '我的案件'], ['report', '今日狀況回報']],
  supervisor: [['supervisor', '督導頁面'], ['review', '紀錄確認']],
  admin: [['admin', '帳號管理']],
};
const MIC_PAGES = new Set(['record', 'report']);
const ACTING_ROLE_KEY = 'xiaozhao.actingRole';
const MIC_KEY = 'xiaozhao.micDeviceId';
const EXTERNAL_MIC_KEY = 'xiaozhao.externalMic';
const POLL_MS = 2000;
// 回覆目標：送出問題到收到回覆 3 秒內（10/05 會議決議，P95；2026-10-14 async-backend）
const REPLY_TARGET_S = 3;
const STATUS_TEXT = {
  answered: '已回答', awaiting_human: '已轉督導', human_answered: '督導已回覆', no_question: '本輪沒有需要回答的問題',
  pending: '處理中',
};

const app = { config: null, token: null, me: null, page: null, speakerRoles: [], actingRole: null };

// ---------- 小工具 ----------

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'class') el.className = v;
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

function fmtTime(sec) {
  if (!sec) return '';
  const d = new Date(sec * 1000);
  return d.toLocaleString('zh-TW', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function today() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function notice(text) {
  const el = document.getElementById('notice');
  el.textContent = text || '';
  el.hidden = !text;
}

function showDialog(...children) {
  const body = document.getElementById('dialog-body');
  body.replaceChildren(...children);
  document.getElementById('dialog').showModal();
}

// 目前使用的身分只在開啟網頁時讀一次；其他分頁切換身分不影響這個分頁（例如正在錄音的分頁）
function loadActingRole() {
  try { app.actingRole = localStorage.getItem(ACTING_ROLE_KEY); } catch { app.actingRole = null; }
}

function setActingRole(role) {
  app.actingRole = role;
  try { localStorage.setItem(ACTING_ROLE_KEY, role); } catch { /* 無痕模式等情況忽略 */ }
}

async function api(method, path, body, raw) {
  const headers = { Authorization: `Bearer ${app.token}` };
  // 一律帶上記住的身分（包括第一次查 /api/me），伺服器只對管理員採用，其他人忽略
  if (app.actingRole) headers['X-Acting-Role'] = app.actingRole;
  const init = { method, headers };
  if (raw) {
    headers['Content-Type'] = 'application/octet-stream';
    init.body = raw;
  } else if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const res = await fetch(path, init);
  if (res.status === 401 && window.liff && !app.config.dev_fake_auth) {
    liff.logout();
    liff.login({ redirectUri: location.href });
    throw new Error('登入已失效，重新登入中');
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `錯誤 ${res.status}`);
  return data;
}

function errorBox(err) {
  return h('p', { class: 'error' }, err.message || String(err));
}

// ---------- 啟動與登入 ----------

async function boot() {
  loadActingRole();
  app.config = await (await fetch('/api/config')).json();
  app.speakerRoles = app.config.speaker_roles;
  app.intentLabels = app.config.intent_labels;
  const devUser = new URLSearchParams(location.search).get('dev');
  if (app.config.dev_fake_auth && devUser) {
    // HTTP 標頭只能放 ASCII，中文測試名稱先編碼
    app.token = `dev:${encodeURIComponent(devUser)}`;
  } else {
    await liff.init({ liffId: app.config.liff_id });
    if (!liff.isLoggedIn()) {
      liff.login({ redirectUri: location.href });
      return;
    }
    app.token = liff.getIDToken();
  }
  // liff.init 之後才會把 liff.line.me 帶來的 ?page= 還原到網址上
  const params = new URLSearchParams(location.search);
  app.me = await api('GET', '/api/me');
  const wanted = params.get('page') || location.hash.slice(1) || '';
  if (app.me.role === null || wanted === 'bind') {
    renderBind();
    return;
  }
  // 管理員從選單開啟其他身分的頁面（例如錄音模式）時，自動切到該身分
  const owner = Object.keys(ROLE_TABS).find((role) => ROLE_TABS[role].some(([p]) => p === wanted));
  if (app.me.is_admin && owner && owner !== app.me.role) {
    setActingRole(owner);
    app.me = await api('GET', '/api/me');
  }
  renderTopbar();
  go(defaultPage(wanted));
}

function currentRole() {
  return app.me.role;
}

function defaultPage(wanted) {
  const tabs = ROLE_TABS[currentRole()] || [];
  if (wanted === 'help' || tabs.some(([p]) => p === wanted)) return wanted;
  return tabs.length ? tabs[0][0] : 'record';
}

function renderTopbar() {
  const button = document.getElementById('me-button');
  const menu = document.getElementById('me-menu');
  button.hidden = false;
  button.textContent = `${app.me.display_name}（${ROLE_NAMES[currentRole()]}）▾`;
  button.onclick = () => { menu.hidden = !menu.hidden; };
  const items = [];
  if (app.me.is_admin) {
    items.push(h('div', { class: 'muted' }, '切換身分'));
    for (const role of app.me.available_roles) {
      items.push(h('button', {
        type: 'button', class: role === currentRole() ? 'active' : '',
        onclick: async () => {
          setActingRole(role);
          app.me = await api('GET', '/api/me');
          menu.hidden = true;
          renderTopbar();
          go(defaultPage(''));
        },
      }, ROLE_NAMES[role]));
    }
  } else {
    items.push(h('div', { class: 'muted' }, ROLE_NAMES[currentRole()]));
  }
  items.push(h('button', { type: 'button', onclick: () => { menu.hidden = true; go('help'); } }, '使用說明'));
  menu.replaceChildren(...items);
  const tabs = document.getElementById('tabs');
  tabs.replaceChildren(...(ROLE_TABS[currentRole()] || []).map(([page, label]) =>
    h('button', { type: 'button', 'data-page': page, onclick: () => go(page) }, label)));
}

function go(page) {
  if (recorder.active && page !== app.page) {
    if (!confirm('正在錄音，切換頁面會停止錄音，確定嗎？')) return;
    recorder.stop();
  }
  app.page = page;
  history.replaceState(null, '', `${location.pathname}${location.search}#${page}`);
  for (const b of document.querySelectorAll('#tabs button')) b.classList.toggle('active', b.dataset.page === page);
  notice('');
  const view = document.getElementById('view');
  view.replaceChildren();
  if (MIC_PAGES.has(page) && window.liff && !app.config.dev_fake_auth && liff.isInClient()) {
    renderOpenExternal(view, page);
    return;
  }
  const renderers = { home: renderHome, help: renderHelp, record: renderRecord, cases: renderCases, report: renderReport, supervisor: renderSupervisor, review: renderReview, admin: renderAdmin };
  (renderers[page] || renderRecord)(view).catch((err) => view.append(errorBox(err)));
}

function renderOpenExternal(view, page) {
  // LINE 內建瀏覽器實測無法使用麥克風，錄音頁一律在外部瀏覽器開啟（設計文件第 1 節收音實測）
  // 不能用 liff.line.me 網址：LINE App 會攔下自家 LIFF 網址，一律在 LINE 內開啟。
  // 改開本服務的網址，並加上 LINE 認得的 openExternalBrowser=1
  const url = `${location.origin}${location.pathname}?page=${page}&openExternalBrowser=1`;
  view.append(h('div', { class: 'card' },
    h('h2', {}, '請在外部瀏覽器開啟'),
    h('p', {}, 'LINE 內建瀏覽器無法使用麥克風。按下按鈕會用手機的瀏覽器開啟，第一次需要再登入一次 LINE。'),
    h('button', { class: 'primary big', type: 'button', onclick: () => liff.openWindow({ url, external: true }) }, '用外部瀏覽器開啟'),
  ));
}

// ---------- 綁定 ----------

function renderBind() {
  const view = document.getElementById('view');
  const input = h('input', { placeholder: '請輸入綁定碼', autocomplete: 'off', maxlength: '32' });
  const msg = h('p', {});
  view.replaceChildren(h('div', { class: 'card' },
    h('h2', {}, '綁定帳號'),
    h('p', { class: 'muted' }, '請輸入管理員給你的綁定碼。綁定碼只能使用一次。'),
    h('div', { class: 'row' }, input, h('button', {
      class: 'primary', type: 'button',
      onclick: async () => {
        msg.replaceChildren();
        try {
          const r = await api('POST', '/api/bind', { code: input.value });
          msg.replaceChildren(`綁定完成：${r.display_name}（${ROLE_NAMES[r.role]}）。聊天室選單會自動更新。`);
          app.me = await api('GET', '/api/me');
          renderTopbar();
          setTimeout(() => go(defaultPage('')), 1200);
        } catch (err) {
          msg.replaceChildren(errorBox(err));
        }
      },
    }, '綁定')),
    msg,
  ));
}

// ---------- 錄音 ----------

class Uploader {
  // 依序上傳音訊片段；失敗時保留在佇列重送，伺服器以序號去重（設計文件 5.5 節）
  constructor(pathPrefix) {
    this.pathPrefix = pathPrefix;
    this.queue = [];
    this.seq = 0;
    this.sending = false;
    this.failures = 0;
    this.onStatus = () => {};
  }

  push(buffer) {
    this.queue.push({ seq: this.seq++, buffer, ts: Date.now() });
    this.pump();
  }

  async pump() {
    if (this.sending) return;
    this.sending = true;
    while (this.queue.length) {
      const item = this.queue[0];
      try {
        const r = await api('POST', `${this.pathPrefix}/chunks?seq=${item.seq}&ts=${item.ts}`, undefined, item.buffer);
        this.queue.shift();
        this.failures = 0;
        this.onStatus(r);
      } catch (err) {
        this.failures++;
        this.onStatus({ error: err.message, pending: this.queue.length });
        await new Promise((r) => setTimeout(r, Math.min(5000, 500 * this.failures)));
      }
    }
    this.sending = false;
  }

  async drain(timeoutMs) {
    const until = Date.now() + timeoutMs;
    while ((this.queue.length || this.sending) && Date.now() < until) {
      await new Promise((r) => setTimeout(r, 200));
    }
    return this.queue.length === 0;
  }
}

const recorder = {
  active: false,
  starting: false,
  context: null,
  stream: null,
  node: null,
  wakeLock: null,
  uploader: null,
  hiddenSince: null,

  async start(pathPrefix, deviceId, external = false) {
    // 重複啟動會留下舊的麥克風與 worklet，兩路音訊送進同一個上傳器，文字變成每段重複兩次
    if (this.starting || this.active) throw new Error('錄音已在進行中。');
    this.starting = true;
    try {
      return await this.open(pathPrefix, deviceId, external);
    } finally {
      this.starting = false;
    }
  },

  async open(pathPrefix, deviceId, external) {
    // 不要求單聲道：雙聲道的外接麥克風（例如兩個發射器分軌）由 worklet 合成一路（external-mic 第 4.2、4.3 節）
    // 外接麥克風模式：Android Chrome 開著回音消除時以通話模式收音，不會用 Type-C 麥克風，所以關掉回音消除與
    // 自動增益（external-mic 第 9 節）；降噪保留，否則背景聲會讓 VAD 一直判定在說話、句子切不開（第 11 節）
    const processing = !external;
    const base = { echoCancellation: processing, noiseSuppression: true, autoGainControl: processing };
    this.fellBack = false;
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: deviceId ? { ...base, deviceId: { exact: deviceId } } : base,
      });
    } catch (err) {
      if (!deviceId || !['OverconstrainedError', 'NotFoundError'].includes(err.name)) throw err;
      // 選過的麥克風已拔掉：改用手機預設麥克風
      this.stream = await navigator.mediaDevices.getUserMedia({ audio: base });
      this.fellBack = true;
    }
    const track = this.stream.getAudioTracks()[0];
    this.micLabel = (track && track.label) || '';
    // 錄音中拔掉麥克風時音軌會結束（自己呼叫 stop() 不會觸發）
    if (track) track.addEventListener('ended', () => { if (this.active && this.onTrackEnded) this.onTrackEnded(); });
    // 用瀏覽器預設取樣率，由 worklet 降到 16 kHz（Firefox 不允許指定取樣率的 AudioContext 接麥克風）
    this.context = new AudioContext();
    await this.context.audioWorklet.addModule('recorder-worklet.js');
    const source = this.context.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(this.context, 'pcm-chunker', { channelCountMode: 'max' });
    const uploader = new Uploader(pathPrefix);
    this.uploader = uploader;
    this.node.port.onmessage = (e) => uploader.push(e.data);
    source.connect(this.node);
    this.active = true;
    await this.requestWakeLock();
    document.addEventListener('visibilitychange', this.onVisibility);
    return this.uploader;
  },

  async requestWakeLock() {
    // 螢幕關閉時錄音會斷斷續續（設計文件第 1 節），錄音中要求螢幕保持亮著
    try {
      if ('wakeLock' in navigator) this.wakeLock = await navigator.wakeLock.request('screen');
    } catch {
      notice('無法讓螢幕保持亮著，錄音中請勿關閉螢幕。');
    }
  },

  onVisibility: () => {
    if (!recorder.active) return;
    if (document.visibilityState === 'hidden') {
      recorder.hiddenSince = Date.now();
    } else {
      if (recorder.hiddenSince) {
        const sec = Math.round((Date.now() - recorder.hiddenSince) / 1000);
        notice(`剛才離開畫面 ${sec} 秒；若期間螢幕曾關閉，可能有漏聽。`);
      }
      recorder.hiddenSince = null;
      recorder.requestWakeLock();
    }
  },

  async stop() {
    // 啟動到一半失敗時也要關掉已開啟的麥克風
    const wasActive = this.active;
    this.active = false;
    this.onTrackEnded = null;
    document.removeEventListener('visibilitychange', this.onVisibility);
    if (wasActive && this.node) {
      this.node.port.postMessage('flush');
      await new Promise((r) => setTimeout(r, 100));
    }
    if (this.stream) { this.stream.getTracks().forEach((t) => t.stop()); this.stream = null; }
    if (this.context) { await this.context.close().catch(() => {}); this.context = null; }
    if (this.wakeLock) { this.wakeLock.release().catch(() => {}); this.wakeLock = null; }
    this.node = null;
    return wasActive && this.uploader ? this.uploader.drain(10000) : true;
  },
};

const micPicker = {
  // 錄音頁的麥克風下拉選單（external-mic 第 4.1 節）；選擇只存在這支手機的瀏覽器
  onDevicesChanged: null,

  external() {
    try { return localStorage.getItem(EXTERNAL_MIC_KEY) === '1'; } catch { return false; }
  },

  saveExternal(on) {
    try { localStorage.setItem(EXTERNAL_MIC_KEY, on ? '1' : '0'); } catch { /* 無痕模式等情況忽略 */ }
  },

  savedId() {
    try { return localStorage.getItem(MIC_KEY) || ''; } catch { return ''; }
  },

  save(id) {
    try { localStorage.setItem(MIC_KEY, id); } catch { /* 無痕模式等情況忽略 */ }
  },

  async list() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return [];
    const devices = await navigator.mediaDevices.enumerateDevices();
    // default、communications 是系統預設的別名，由「手機預設麥克風」代表
    return devices.filter((d) => d.kind === 'audioinput' && d.deviceId && !['default', 'communications'].includes(d.deviceId));
  },
};

if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
  navigator.mediaDevices.addEventListener('devicechange', () => {
    if (micPicker.onDevicesChanged && !recorder.active) micPicker.onDevicesChanged();
  });
}

function micError(err) {
  const name = err && err.name;
  if (name === 'NotAllowedError') {
    return new Error('瀏覽器不允許使用麥克風：請點網址列左邊的鎖頭圖示，把「麥克風」改成允許，再重新整理頁面。');
  }
  if (name === 'NotFoundError') return new Error('找不到麥克風裝置。');
  if (name === 'NotReadableError') return new Error('麥克風被其他程式占用（例如視訊會議），請先關閉後再試。');
  return err;
}

// ---------- 照護員：主頁 ----------

async function renderHome(view) {
  const entry = (page, title, desc) => h('button', { class: 'primary big home-entry', type: 'button', onclick: () => go(page) },
    h('div', { class: 'home-title' }, title), h('div', { class: 'home-desc' }, desc));
  view.append(h('div', { class: 'card' },
    h('h2', {}, `${app.me.display_name}，您好`),
    h('p', { class: 'muted' }, '請選擇要使用的功能。'),
    h('div', { class: 'home-grid' },
      entry('record', '錄音模式', '說「小照你好」開始一輪，記錄多人對話並取得回答'),
      entry('cases', '我的案件', 'LINE 提問的回答、錄音逐字稿、給督導的問題與回覆'),
      entry('report', '今日狀況回報', '口述今天的照護狀況，送給督導'),
      h('button', { class: 'secondary big home-entry', type: 'button', onclick: () => go('help') },
        h('div', { class: 'home-title' }, '使用說明與知識庫法規涵蓋範圍'),
        h('div', { class: 'home-desc' }, '怎麼使用小照、小照會回答哪些問題')))));
}

// ---------- 使用說明 ----------

async function renderHelp(view) {
  const section = (title, ...items) => h('div', { class: 'card' }, h('h2', {}, title), h('ul', { class: 'help-list' }, items.map((t) => h('li', {}, t))));
  view.append(
    section('小照可以做什麼',
      '錄音模式：記錄照護現場多人、多輪的對話，判斷每句的意圖，需要時向後端查詢答案。',
      'LINE 案件：在聊天室直接輸入文字提問（開頭加不加「小照你好」都可以），例如「我可以幫長輩代購藥物嗎？」；回答在「我的案件」查看。輸入「直接問督導：問題」可不經小照直接轉督導。',
      '我的案件：查看 LINE 提問的回答、錄音的逐字稿（含確認狀態），以及轉給督導的問題與督導回覆。',
      '今日狀況回報：口述當天照護的住民與狀況，轉成文字送到督導頁面（保存 24 小時）。',
      '轉督導：小照無法回答的問題會轉給督導，督導回覆後會用 LINE 通知你。'),
    section('錄音模式怎麼用',
      '第一次使用：新增住民（用代稱）、記錄住民的書面同意日期、詢問在場者是否同意即時區分說話人。',
      '每次錄音前，確認在場者都同意錄音，再按「確認錄音」。',
      '說「小照你好」開始一輪；說「小照請回答」、安靜 3 秒，或說下一個「小照你好」結束這一輪。',
      '錄音頁會在外部瀏覽器開啟，第一次要再登入一次 LINE；錄音中請保持螢幕亮著。',
      '同意即時區分說話人時，系統會把每句標成「說話人 01、02…」；請在錄音頁下方的「確認紀錄」把代號對應到照護員、長輩、家屬（錄音不用停，之後的句子會直接套用），並修正錯字與意圖。',
      '說話人代號只在同一次錄音內有效：停止錄音或切換到其他分頁後重新錄音，代號會從 01 重新開始，需要再對應一次。',
      '在場者不同意錄音時，可以改用「手動打字提問」。'),
    section('小照會回答哪些問題',
      '只回答規範查詢（規定、流程、資格等制度面問題），並附上出處。',
      '醫療、用藥，以及知識庫沒有依據的問題，一律轉給督導回覆；緊急狀況（沒有呼吸、噎到、跌倒…）會立即回覆急救指引並通知督導。',
      '小照不會提供醫療建議；身體不適或緊急狀況請依機構流程處理。'),
    section('知識庫法規涵蓋範圍',
      '以住宿式長照機構為主：機構相關法規、機構內規、作業程序。',
      '居家服務不在目前的範圍內。',
      '知識庫由後端組維護，詳細的法規清單待後端組提供。',
      '目前是測試版，使用模擬後端：回答內容不是真實的法規內容，請勿作為實際依據。'),
    section('隱私與保存',
      '住民、家屬一律使用代稱，不要輸入姓名、房號或其他可辨識身分的資訊（長期照顧服務法第 43 條）。',
      '只有「小照你好」之後的對話（一輪）會轉成文字；每一輪的錄音只供督導回聽，督導確認後立即刪除，最長保存 7 天。',
      '逐字稿：未經督導確認的保存 7 天；督導把人名換成代號並確認後，提供給長照知識庫，最長保存 90 天。',
      '語音紀錄、LINE 案件保存 1 天，今日狀況回報保存 24 小時（今日狀況回報的錄音轉成文字後立即刪除）。',
      '不保存聲紋：說話人代號只在同一次錄音內有效，錄音結束即丟。'),
  );
}

// ---------- 照護員：錄音模式 ----------

// 錄音前向在場者說明的內容（2026-10-13 依確認／修正分頁的資料處理方式改寫；待指導老師審閱）
const RECORDING_NOTICE = [
  '只有照護員說「小照你好」之後的對話（一輪）會被處理；其他時間的聲音不轉文字、不保存（系統只會即時比對有沒有人說「小照你好」）。',
  '對話會轉成文字並判斷意圖，用來回答照護問題，或轉給督導回覆。',
  '每一輪的錄音會存在機構的伺服器，只供督導回聽、核對逐字稿；督導確認後立即刪除，最長保存 7 天。',
  '逐字稿由督導把人名換成代號並確認後，提供給長照知識庫使用，最長保存 90 天；未確認的逐字稿保存 7 天。',
  '不保存任何人的聲紋；說話的人只以「說話人 01、02」區分，錄音結束即丟。',
  '在場的任何人都可以隨時要求停止錄音；不同意錄音時，改用手動打字提問。',
];

function noticeBlock(title) {
  return h('details', { class: 'notice' },
    h('summary', {}, title),
    h('ul', { class: 'help-list' }, RECORDING_NOTICE.map((t) => h('li', {}, t))),
    h('p', { class: 'muted' }, '告知文字待指導老師審閱。'));
}

async function renderRecord(view) {
  const residents = await api('GET', '/api/residents');
  const state = { residentId: residents.length ? residents[0].id : null };
  const residentBox = h('div', { class: 'card' });
  const actionBox = h('div', {});
  const confirmBox = h('div', { class: 'card' });
  view.append(residentBox, actionBox, confirmBox);

  async function refreshResidents() {
    const list = await api('GET', '/api/residents');
    residents.splice(0, residents.length, ...list);
    if (!residents.some((r) => r.id === state.residentId)) state.residentId = residents.length ? residents[0].id : null;
    drawResidents();
    drawActions();
  }

  function selected() {
    return residents.find((r) => r.id === state.residentId) || null;
  }

  function drawResidents() {
    // 錄音中不能切換、新增或管理住民：切換後再切回來會看不到正在錄音，錄音也可能記到錯的住民
    const locked = recorder.active;
    const select = h('select', { disabled: locked, onchange: (e) => { state.residentId = Number(e.target.value); drawResidents(); drawActions(); } },
      residents.map((r) => h('option', { value: r.id, selected: r.id === state.residentId }, r.alias)));
    const alias = h('input', { placeholder: '新增住民代稱，例：住民 A', maxlength: '20' });
    const r = selected();
    residentBox.replaceChildren(
      h('h2', {}, '現在照護哪一位住民？'),
      h('p', { class: 'muted' }, '一律使用代稱，請勿輸入姓名、房號或其他可辨識身分的資訊（長期照顧服務法第 43 條）。'),
      residents.length ? h('div', { class: 'row' }, select,
        h('button', { class: 'secondary', type: 'button', disabled: locked, onclick: () => manageResident(r) }, '管理')) : h('p', {}, '尚未新增住民。'),
      locked ? h('p', { class: 'muted' }, '錄音中，請先停止錄音再切換或新增住民。') : null,
      h('div', { class: 'row', style: 'margin-top:8px' }, alias, h('button', {
        class: 'secondary', type: 'button', disabled: locked,
        onclick: async () => {
          if (!alias.value.trim()) return;
          const created = await api('POST', '/api/residents', { alias: alias.value.trim() });
          state.residentId = created.id;
          await refreshResidents();
        },
      }, '新增')),
    );
  }

  function manageResident(r) {
    if (!r) return;
    const alias = h('input', { value: r.alias, maxlength: '20' });
    showDialog(
      h('h3', {}, `管理：${r.alias}`),
      h('div', { class: 'row' }, alias, h('button', {
        class: 'secondary', type: 'button',
        onclick: async () => { await api('PATCH', `/api/residents/${r.id}`, { alias: alias.value }); await refreshResidents(); },
      }, '改名')),
      h('div', { class: 'row', style: 'margin-top:12px' },
        h('button', {
          class: 'danger', type: 'button',
          onclick: async () => {
            if (!confirm(`刪除「${r.alias}」及其所有紀錄？`)) return;
            await api('DELETE', `/api/residents/${r.id}`);
            document.getElementById('dialog').close();
            await refreshResidents();
          },
        }, '刪除住民')),
    );
  }

  function drawActions() {
    const r = selected();
    actionBox.replaceChildren();
    if (!r) return;
    if (!r.written_consent_date) {
      const d = h('input', { type: 'date', value: today(), max: today() });
      actionBox.append(h('div', { class: 'card' },
        h('h3', {}, '尚未記錄書面同意'),
        h('p', {}, '依長期照顧服務法第 43 條，錄音須經住民（或法定代理人、主要照顧之最近親屬）書面同意。入住時由機構取得同意書；請確認後記錄同意日期。'),
        noticeBlock('機構的書面同意書須涵蓋的內容（點開查看）'),
        h('div', { class: 'row' }, d, h('button', {
          class: 'primary', type: 'button',
          onclick: async () => { await api('PATCH', `/api/residents/${r.id}/written-consent`, { date: d.value }); await refreshResidents(); },
        }, '已有書面同意，記錄日期')),
      ));
      actionBox.append(textQuestionCard(r));
      return;
    }
    if (!r.voice_consent) {
      actionBox.append(h('div', { class: 'card' },
        h('h3', {}, '第一次錄音：是否同意即時區分說話人？'),
        h('p', {}, '同意後，錄音時系統會依聲音特徵即時分出不同的說話人，標成「說話人 01、02…」；聲音特徵只在這次錄音中使用，錄音結束即丟，不會保存。不同意則不分析聲音特徵，由照護員在錄音頁下方的「確認紀錄」逐句選擇說話者。'),
        h('p', { class: 'muted' }, '告知文字待指導老師審閱。'),
        h('div', { class: 'row' },
          h('button', { class: 'primary', type: 'button', onclick: async () => {
            await api('POST', `/api/residents/${r.id}/voiceprint-consent`, { decision: 'agreed' }); await refreshResidents();
          } }, '在場者同意'),
          h('button', { class: 'secondary', type: 'button', onclick: async () => {
            await api('POST', `/api/residents/${r.id}/voiceprint-consent`, { decision: 'declined' }); await refreshResidents();
          } }, '不同意')),
      ));
      actionBox.append(textQuestionCard(r));
      return;
    }
    actionBox.append(recordCard(r), textQuestionCard(r));
  }

  function recordCard(r) {
    const card = h('div', { class: 'card' });
    const live = h('div', {});
    const status = h('p', { class: 'muted' });
    let sessionId = null;
    let timer = null;
    const micSelect = h('select', { onchange: (e) => micPicker.save(e.target.value) });
    const micHint = h('p', { class: 'muted' });
    const externalBox = h('input', { type: 'checkbox', id: 'external-mic' });
    externalBox.checked = micPicker.external();
    micSelect.disabled = externalBox.checked;
    externalBox.addEventListener('change', () => {
      micPicker.saveExternal(externalBox.checked);
      micSelect.disabled = externalBox.checked;
    });
    const externalLabel = h('label', { class: 'row', for: 'external-mic' }, externalBox,
      '使用外接麥克風（關閉手機的回音消除、自動增益；選單裡找不到外接麥克風時勾選）');

    async function fillMics() {
      let mics = [];
      try { mics = await micPicker.list(); } catch { /* 列不出來就只有預設麥克風 */ }
      const saved = micPicker.savedId();
      micSelect.replaceChildren(h('option', { value: '' }, '手機預設麥克風'),
        ...mics.map((d, i) => h('option', { value: d.deviceId }, d.label || `麥克風 ${i + 1}`)));
      micSelect.value = mics.some((d) => d.deviceId === saved) ? saved : '';
      micHint.textContent = (!mics.length || mics.some((d) => !d.label))
        ? '還沒允許使用麥克風時，不會列出麥克風名稱；按「確認錄音」允許後就會顯示。剛插上的麥克風沒出現時，請按「重新偵測」。'
        : '剛插上的麥克風沒出現時，請按「重新偵測」。';
    }

    function idle() {
      const confirmBox = h('input', { type: 'checkbox', id: 'consent-confirm' });
      const startBtn = h('button', { class: 'primary big', type: 'button', disabled: true, onclick: begin }, '確認錄音');
      confirmBox.addEventListener('change', () => { startBtn.disabled = !confirmBox.checked; });
      card.replaceChildren(
        h('h3', {}, `錄音模式：${r.alias}`),
        h('p', { class: 'muted' }, `書面同意：${r.written_consent_date}；說話人：${r.voice_consent === 'agreed' ? '即時區分（不保存聲紋）' : '不區分，在確認紀錄逐句選擇'}`),
        noticeBlock('錄音前，請向在場的每一位說明（點開查看 6 點）'),
        h('label', { class: 'row', for: 'consent-confirm' }, confirmBox, '我已向在場者說明以上內容，在場者都同意錄音'),
        h('p', { class: 'muted' }, '說「小照你好」開始一輪；說「小照請回答」、安靜 3 秒，或說下一個「小照你好」結束這一輪。錄音中請保持螢幕亮著。'),
        h('div', { class: 'row' }, h('label', {}, '麥克風：'), micSelect,
          h('button', { class: 'secondary', type: 'button', onclick: fillMics }, '重新偵測')),
        externalLabel,
        micHint,
        startBtn, live,
      );
      micPicker.onDevicesChanged = fillMics;
      fillMics();
    }

    async function begin(e) {
      if (e && e.currentTarget) e.currentTarget.disabled = true;  // 防止連點建立兩次錄音
      try {
        const s = await api('POST', '/api/sessions', { resident_id: r.id, consent_confirmed: true });
        sessionId = s.session_id;
        const external = externalBox.checked;
        const uploader = await recorder.start(`/api/sessions/${sessionId}`, external ? '' : micSelect.value, external);
        if (recorder.fellBack) {
          micPicker.save('');
          notice('找不到選擇的麥克風，已改用手機預設麥克風。');
        }
        recorder.onTrackEnded = async () => {
          await end();
          notice('麥克風已中斷，錄音已停止。請確認麥克風連線後重新開始。');
        };
        uploader.onStatus = (st) => {
          if (st.error) status.replaceChildren(h('span', { class: 'error' }, `上傳失敗，重試中（待送 ${st.pending} 段）：${st.error}`));
          else status.replaceChildren(st.in_turn ? '本輪進行中…' : '等待「小照你好」…');
        };
        card.replaceChildren(
          h('h3', {}, h('span', { class: 'rec-dot' }), `錄音中：${r.alias}`),
          h('p', { class: 'muted' }, `使用中：${recorder.micLabel || '手機預設麥克風'}${external ? '（外接麥克風模式）' : ''}`),
          status,
          h('button', { class: 'danger big', type: 'button', onclick: end }, '停止錄音'),
          live,
        );
        timer = setInterval(poll, POLL_MS);
        drawResidents();
      } catch (err) {
        await recorder.stop().catch(() => {});
        if (sessionId) {
          await api('POST', `/api/sessions/${sessionId}/stop`).catch(() => {});
          sessionId = null;
        }
        card.append(errorBox(micError(err)));
        drawResidents();
      }
    }

    let endedTurns = 0;

    async function poll() {
      if (!sessionId) return;
      try {
        const turns = await api('GET', `/api/sessions/${sessionId}/turns`);
        live.replaceChildren(...turns.map((t) => turnView(t, poll)));
        // 一輪結束（小照請回答／靜音／下一個小照你好）就把新紀錄加到下方的確認紀錄，不必停止錄音
        const ended = turns.filter((t) => t.ended_at).length;
        if (ended !== endedTurns) {
          endedTurns = ended;
          drawConfirm();
        }
      } catch { /* 下一次輪詢再試 */ }
    }

    async function end() {
      clearInterval(timer);
      const ok = await recorder.stop();
      if (!ok) notice('部分音訊未上傳完成。');
      await api('POST', `/api/sessions/${sessionId}/stop`).catch(() => {});
      await poll();
      sessionId = null;
      const kept = live;
      idle();
      card.append(kept);
      drawResidents();
      drawConfirm();
    }

    idle();
    return card;
  }

  function textQuestionCard(r) {
    const input = h('textarea', { placeholder: '在場者不同意錄音時，可在這裡打字提問', maxlength: '500' });
    const list = h('div', {});
    async function load() {
      const items = await api('GET', `/api/residents/${r.id}/text-questions`);
      list.replaceChildren(...items.map((q) => h('div', { class: 'list-item' },
        q.answer ? h('div', { class: `answer ${q.status === 'human_answered' ? 'human' : ''}` }, q.answer)
          : h('div', { class: 'muted' }, q.status === 'pending' ? pendingText(q.created_at) : (STATUS_TEXT[q.status] || q.status)),
        h('div', {}, q.question),
        h('div', { class: 'muted' }, fmtTime(q.created_at), q.reply_ms !== null && q.reply_ms !== undefined ? ` ・ 回覆時間 ${replyTime(q.reply_ms)}` : ''))));
      // 還在查詢的問題：回答到了自動更新
      if (items.some((q) => q.status === 'pending') && card.isConnected) setTimeout(load, POLL_MS);
    }
    const card = h('div', { class: 'card' });
    load();
    card.append(
      h('h3', {}, '手動打字提問'),
      input,
      h('div', { class: 'row', style: 'margin-top:8px' }, h('button', {
        class: 'secondary', type: 'button',
        onclick: async () => {
          if (!input.value.trim()) return;
          await api('POST', `/api/residents/${r.id}/text-questions`, { question: input.value.trim() });
          input.value = '';
          load();
        },
      }, '送出')),
      list);
    return card;
  }

  // 下方的確認紀錄：已顯示的待確認紀錄保留原本的編輯畫面（重新整理時不會清掉正在修改的內容）
  const editors = new Map();

  async function drawConfirm() {
    const records = await api('GET', '/api/records');
    const pending = records.filter((r) => r.status === 'draft');
    const done = records.filter((r) => r.status !== 'draft');
    for (const seq of [...editors.keys()]) if (!pending.some((r) => r.seq === seq)) editors.delete(seq);
    // 每輪收合成一行；點開才建立確認與修正畫面（收起後內容保留）
    const pendingEls = pending.map((r) => {
      if (!editors.has(r.seq)) {
        const item = h('details', {
          class: 'record-item',
          ontoggle: () => {
            if (!item.open || item.dataset.loaded) return;
            item.dataset.loaded = '1';
            item.append(recordEditor(r, () => { editors.delete(r.seq); drawConfirm(); }));
          },
        }, h('summary', {},
          h('strong', {}, r.resident_alias), ` ・ ${fmtTime(r.created_at)} ・ ${r.doc.sentences.length} 句 `,
          h('span', { class: 'tag' }, RECORD_STATUS_TEXT[r.status]),
          h('div', { class: 'muted' }, r.doc.sentences[0] ? r.doc.sentences[0].text : '')));
        editors.set(r.seq, item);
      }
      return editors.get(r.seq);
    });
    confirmBox.replaceChildren(
      h('h2', {}, '確認紀錄'),
      h('p', { class: 'muted' }, '每輪結束後會出現在這裡，點一下展開。把「說話人 01、02…」對應到身分（錄音不用停，之後的句子會直接套用），修正錯字與意圖，再按「確認」。督導確認前都可以修改；未經督導確認的紀錄保存 7 天。'),
      pendingEls.length ? null : h('p', { class: 'muted' }, '目前沒有待確認的紀錄。'),
      ...pendingEls,
      done.length ? h('h3', { style: 'margin-top:12px' }, '已確認') : null,
      ...done.map((r) => h('div', { class: 'list-item', onclick: () => showDialog(h('h3', {}, `${r.resident_alias}・${fmtTime(r.created_at)}`), recordEditor(r, () => location.reload())) },
        h('div', {}, h('strong', {}, r.resident_alias), ` ・ ${fmtTime(r.created_at)} ・ ${r.doc.sentences.length} 句 `,
          h('span', { class: 'tag' }, RECORD_STATUS_TEXT[r.status])),
        h('div', { class: 'muted' }, r.doc.sentences[0] ? r.doc.sentences[0].text : ''))));
  }

  drawResidents();
  drawActions();
  drawConfirm();
}

function pendingText(since) {
  return Date.now() / 1000 - since > REPLY_TARGET_S ? '小照還在查詢，回答會稍後出現…' : '查詢中…';
}

function replyTime(ms) {
  if (ms === null || ms === undefined) return '';
  return `${(ms / 1000).toFixed(1)} 秒${ms > REPLY_TARGET_S * 1000 ? '（超過 3 秒）' : ''}`;
}

function turnView(t) {
  let answer = null;
  if (t.answer) answer = h('div', { class: `answer ${t.answer_status === 'human_answered' ? 'human' : ''}` }, t.answer);
  else if (t.answer_status === 'pending') answer = h('div', { class: 'answer' }, pendingText(t.ended_at || t.started_at));
  else if (t.answer_status) answer = h('div', { class: 'answer' }, STATUS_TEXT[t.answer_status] || t.answer_status);
  // 每個問題「送出到收到回覆」的時間
  let times = [];
  try { times = JSON.parse(t.intents || '[]').filter((r) => r.elapsed_ms !== null && r.elapsed_ms !== undefined); } catch { times = []; }
  return h('div', { class: 'card' },
    h('div', { class: 'muted' }, `第 ${t.seq} 輪 ・ ${fmtTime(t.started_at)}${t.end_reason ? ` ・ 結束：${endReasonText(t.end_reason)}` : ' ・ 進行中'}`),
    answer,
    times.length ? h('div', { class: 'muted' }, `回覆時間：${times.map((r) => `${r.intent} ${replyTime(r.elapsed_ms)}`).join('、')}`) : null,
    ...(t.utterances || []).map((u) => utteranceView(t, u)),
    h('p', { class: 'muted' }, '說話人、錯字、意圖請在下方「確認紀錄」修正。'));
}

function endReasonText(reason) {
  return { answer_keyword: '小照請回答', silence: '靜音 3 秒', next_wake_word: '下一個小照你好', session_stop: '停止錄音' }[reason] || reason;
}

function utteranceView(t, u) {
  // 說話人、錯字、意圖的修正在錄音頁下方的「確認紀錄」
  // 問句單獨一句、前面加「❓」；同一個問句 window 的句子用相同底色（question-window 第 4.3 節）
  const win = u.window_id === null || u.window_id === undefined ? '' : ` win${u.window_id % 2}`;
  return h('div', { class: `utt${win}` },
    h('div', {}, u.is_question ? '❓ ' : '', u.text),
    h('div', { class: 'meta' },
      u.speaker_label ? h('span', { class: 'tag' }, `說話人 ${u.speaker_label}`) : null,
      h('span', {}, u.speaker_role),
      u.intent ? h('span', { class: 'tag' }, `${u.intent} ${(u.intent_conf * 100).toFixed(0)}%`) : null,
      u.intent_owner ? h('span', {}, `歸屬：${u.intent_owner}（僅供參考）`) : null));
}

// ---------- 照護員：確認紀錄（correction-and-cases-tabs P1） ----------

const RECORD_STATUS_TEXT = { draft: '待確認', caregiver_checked: '已確認，等督導', confirmed: '督導已確認' };

function recordEditor(r, onSaved) {
  const locked = r.status === 'confirmed';
  const roleSelect = (value, onchange) => h('select', { disabled: locked, onchange },
    app.speakerRoles.map((role) => h('option', { value: role, selected: role === value }, role)));
  const speakers = { ...r.doc.speakers };
  const edits = new Map();
  const edit = (i, key, value) => { edits.set(i, { ...(edits.get(i) || { i }), [key]: value }); };

  const speakerRows = Object.keys(speakers).map((code) => h('div', { class: 'row', style: 'margin-bottom:6px' },
    h('span', { class: 'tag' }, `說話人 ${code}`),
    roleSelect(speakers[code], (e) => { speakers[code] = e.target.value; })));

  const sentenceRows = r.doc.sentences.map((s, i) => h('div', { class: 'utt' },
    h('div', { class: 'meta' },
      s.speaker ? h('span', { class: 'tag' }, `說話人 ${s.speaker}`) : null,
      roleSelect(s.role, (e) => edit(i, 'role', e.target.value)),
      h('select', { disabled: locked, onchange: (e) => edit(i, 'intent', e.target.value || null) },
        h('option', { value: '' }, '（意圖）'),
        app.intentLabels.map((label) => h('option', { value: label, selected: label === s.intent }, label)))),
    h('textarea', { rows: 2, disabled: locked, oninput: (e) => edit(i, 'text', e.target.value) }, s.text)));

  async function save(check) {
    const body = { speakers, sentences: [...edits.values()] };
    await api('PATCH', `/api/records/${r.seq}`, body);
    if (check) await api('POST', `/api/records/${r.seq}/check`);
    notice(check ? '已確認，等督導確認後會提供給後端。' : '已儲存。');
    if (onSaved) onSaved();
  }

  return h('div', { class: 'card' },
    h('div', { class: 'muted' }, `${r.resident_alias} ・ ${fmtTime(r.created_at)} ・ `, h('span', { class: 'tag' }, RECORD_STATUS_TEXT[r.status])),
    r.answer ? h('div', { class: `answer ${r.answer_status === 'human_answered' ? 'human' : ''}` }, r.answer)
      : (r.answer_status ? h('div', { class: 'answer' }, STATUS_TEXT[r.answer_status] || r.answer_status) : null),
    speakerRows.length ? h('div', { style: 'margin:8px 0' }, h('strong', {}, '說話人代號對應'), ...speakerRows) : null,
    ...sentenceRows,
    locked ? h('p', { class: 'muted' }, '督導已確認，不能再修改。') : h('div', { class: 'row', style: 'margin-top:8px' },
      h('button', { class: 'secondary', type: 'button', onclick: () => save(false) }, '儲存'),
      h('button', { class: 'primary', type: 'button', onclick: () => save(true) }, '確認')));
}

// ---------- 照護員：我的案件（correction-and-cases-tabs P4） ----------

const CASE_VIEWS = [
  ['line', 'LINE案件', renderLineCases],
  ['transcripts', '照護員查看逐字稿', renderMyTranscripts],
  ['questions', '給督導的問題與回覆', renderMyQuestions],
];

async function renderCases(view) {
  const content = h('div', {});
  const buttons = CASE_VIEWS.map(([key, label]) => h('button', {
    type: 'button', class: key === (app.casesView || 'line') ? 'primary' : 'secondary', 'data-view': key,
    onclick: () => { app.casesView = key; show(); },
  }, label));
  async function show() {
    const current = app.casesView || 'line';
    for (const b of buttons) b.className = b.dataset.view === current ? 'primary' : 'secondary';
    content.replaceChildren(h('p', { class: 'muted' }, '讀取中…'));
    const [, , render] = CASE_VIEWS.find(([key]) => key === current);
    const box = h('div', {});
    try { await render(box); content.replaceChildren(box); } catch (err) { content.replaceChildren(errorBox(err)); }
  }
  view.append(h('div', { class: 'card' }, h('h2', {}, '我的案件'),
    h('div', { class: 'row', style: 'flex-wrap:wrap' }, ...buttons)), content);
  await show();
}

async function renderMyTranscripts(view) {
  const records = await api('GET', '/api/records');
  view.append(h('div', { class: 'card' },
    h('h2', {}, '錄音逐字稿'),
    h('p', { class: 'muted' }, '每一輪的逐字稿（已套用你和督導的修正）。要修改請到錄音頁下方的「確認紀錄」；未經督導確認的保存 7 天。'),
    records.length ? null : h('p', { class: 'muted' }, '目前沒有逐字稿。'),
    ...records.map((r) => h('div', { class: 'list-item', style: 'cursor:default' },
      h('div', { class: 'muted' }, `${r.resident_alias} ・ ${fmtTime(r.created_at)} ・ `, h('span', { class: 'tag' }, RECORD_STATUS_TEXT[r.status])),
      r.answer ? h('div', { class: `answer ${r.answer_status === 'human_answered' ? 'human' : ''}` }, r.answer)
        : (r.answer_status ? h('div', { class: 'answer' }, STATUS_TEXT[r.answer_status] || r.answer_status) : null),
      ...r.doc.sentences.map((s) => h('div', { class: 'utt' },
        h('div', { class: 'meta' }, h('span', { class: 'tag' }, s.role), s.intent ? h('span', {}, s.intent) : null),
        h('div', {}, s.text)))))));
}

async function renderMyQuestions(view) {
  const requests = await api('GET', '/api/my/supervisor-requests');
  const sourceText = { turn: '錄音模式', line_case: 'LINE 案件', text_question: '手動打字提問' };
  view.append(h('div', { class: 'card' },
    h('h2', {}, '給督導的問題與回覆'),
    h('p', { class: 'muted' }, '小照無法回答而轉給督導的問題。未回覆的會一直保留；督導回覆後保存 1 天，並會用 LINE 通知你。'),
    requests.length ? null : h('p', { class: 'muted' }, '目前沒有轉給督導的問題。'),
    ...requests.map((q) => h('div', { class: 'list-item', style: 'cursor:default' },
      h('div', { class: 'muted' }, `${sourceText[q.source] || q.source} ・ ${fmtTime(q.created_at)} ・ `,
        h('span', { class: 'tag' }, q.status === 'replied' ? '督導已回覆' : '等待督導回覆')),
      h('div', {}, q.question),
      q.reason ? h('div', { class: 'muted' }, `轉給督導的原因：${q.reason}`) : null,
      q.reply ? h('div', { class: 'answer human' }, `${q.replied_by}：${q.reply}`) : null))));
}

async function renderLineCases(view) {
  const cases = await api('GET', '/api/line-cases');
  view.append(h('div', { class: 'card' },
    h('h2', {}, 'LINE 案件（保存 1 天）'),
    h('p', { class: 'muted' }, '在聊天室直接輸入的文字提問，會出現在這裡。'),
    cases.length ? null : h('p', { class: 'muted' }, '目前沒有案件。'),
    ...cases.map((c) => h('div', { class: 'list-item' },
      c.answer ? h('div', { class: `answer ${c.status === 'human_answered' ? 'human' : ''}` }, c.answer)
        : h('div', { class: 'muted' }, STATUS_TEXT[c.status] || c.status),
      h('div', {}, c.question),
      h('div', { class: 'muted' }, fmtTime(c.created_at), c.notify_failed ? h('span', { class: 'error' }, ' ・ 通知失敗') : null)))));
}

// ---------- 照護員：今日狀況回報 ----------

async function renderReport(view) {
  const card = h('div', { class: 'card' });
  const mine = h('div', { class: 'card' });
  view.append(card, mine);
  let reportId = null;

  function idle() {
    card.replaceChildren(
      h('h2', {}, '今日狀況回報'),
      h('p', {}, '用口述說明今天照護了哪些住民、各住民的狀況（請用代稱）。轉成文字後送到督導頁面，保存 24 小時。'),
      h('button', { class: 'primary big', type: 'button', onclick: begin }, '開始回報錄音'));
  }

  async function begin() {
    try {
      reportId = (await api('POST', '/api/daily-reports')).report_id;
      const status = h('p', { class: 'muted' }, '錄音中。說完後安靜 3 秒會自動送出，也可以按「結束並送出」。');
      const uploader = await recorder.start(`/api/daily-reports/${reportId}`);
      uploader.onStatus = (st) => {
        if (st.error) status.replaceChildren(h('span', { class: 'error' }, `上傳失敗，重試中：${st.error}`));
        else if (st.auto_stop) end();
      };
      card.replaceChildren(h('h2', {}, h('span', { class: 'rec-dot' }), '回報錄音中'), status,
        h('button', { class: 'danger big', type: 'button', onclick: end }, '結束並送出'));
    } catch (err) {
      await recorder.stop().catch(() => {});
      if (reportId) {
        await api('POST', `/api/daily-reports/${reportId}/stop`).catch(() => {});
        reportId = null;
      }
      card.append(errorBox(micError(err)));
    }
  }

  async function end() {
    if (!reportId) return;  // 自動結束與按鈕可能同時觸發
    const id = reportId;
    reportId = null;
    card.replaceChildren(h('p', {}, '轉文字中…'));
    await recorder.stop();
    try {
      const r = await api('POST', `/api/daily-reports/${id}/stop`);
      idle();
      card.append(r.saved ? h('div', { class: 'answer' }, '已送出：', h('div', { style: 'white-space:pre-wrap' }, r.transcript))
        : h('p', { class: 'error' }, r.detail));
    } catch (err) {
      idle();
      card.append(errorBox(err));
    }
    loadMine();
  }

  async function loadMine() {
    const list = await api('GET', '/api/daily-reports/mine');
    mine.replaceChildren(h('h3', {}, '我的回報（24 小時內）'),
      list.length ? null : h('p', { class: 'muted' }, '目前沒有回報。'),
      ...list.map((d) => h('div', { class: 'list-item' }, h('div', { class: 'muted' }, `${d.report_date} ・ ${fmtTime(d.created_at)}`),
        h('div', { style: 'white-space:pre-wrap' }, d.transcript))));
  }

  idle();
  loadMine();
}

// ---------- 督導 ----------

async function renderSupervisor(view) {
  const [requests, reports] = await Promise.all([api('GET', '/api/supervisor/requests'), api('GET', '/api/supervisor/daily-reports')]);
  const open = requests.filter((r) => r.status === 'open');
  const replied = requests.filter((r) => r.status === 'replied');
  const sourceText = { turn: '錄音模式', line_case: 'LINE 案件', text_question: '手動打字提問' };
  view.append(
    h('div', { class: 'card' }, h('h2', {}, `待處理（${open.length}）`),
      open.length ? null : h('p', { class: 'muted' }, '目前沒有待處理的請求。'),
      ...open.map((r) => {
        const input = h('textarea', { placeholder: '輸入回覆', maxlength: '2000' });
        const msg = h('div', {});
        return h('div', { class: 'list-item', style: 'cursor:default' },
          h('div', { class: 'muted' }, `${r.caregiver_name} ・ ${sourceText[r.source]} ・ ${fmtTime(r.created_at)}`),
          h('div', {}, r.question),
          r.reason ? h('div', { class: 'muted' }, `轉人工原因：${r.reason}`) : null,
          input,
          h('div', { class: 'row', style: 'margin-top:6px' }, h('button', {
            class: 'primary', type: 'button',
            onclick: async () => {
              try {
                const res = await api('POST', `/api/supervisor/requests/${r.id}/reply`, { reply: input.value });
                if (!res.pushed) notice('已存入回覆，但 LINE 推送失敗。');
                go('supervisor');
              } catch (err) { msg.replaceChildren(errorBox(err)); }
            },
          }, '發送')), msg);
      })),
    h('div', { class: 'card' }, h('h2', {}, '今日狀況回報（24 小時內）'),
      reports.length ? null : h('p', { class: 'muted' }, '目前沒有回報。'),
      ...reports.map((d) => h('div', { class: 'list-item', style: 'cursor:default' },
        h('div', { class: 'muted' }, `${d.caregiver_name} ・ ${d.report_date} ・ ${fmtTime(d.created_at)}`),
        h('div', { style: 'white-space:pre-wrap' }, d.transcript)))),
    h('div', { class: 'card' }, h('h2', {}, `已回覆（${replied.length}，保存 1 天）`),
      ...replied.map((r) => h('div', { class: 'list-item', style: 'cursor:default' },
        h('div', { class: 'muted' }, `${r.caregiver_name} ・ ${sourceText[r.source]} ・ ${r.replied_by} 於 ${fmtTime(r.replied_at)} 回覆`),
        h('div', {}, r.question),
        h('div', { class: 'answer human' }, r.reply)))),
  );
}

// ---------- 督導：紀錄確認（correction-and-cases-tabs P2） ----------

async function renderReview(view) {
  const records = await api('GET', '/api/supervisor/records');
  const group = (status) => records.filter((r) => r.status === status);
  const item = (r) => h('div', { class: 'list-item', onclick: () => { view.replaceChildren(); reviewEditor(view, r); } },
    h('div', {}, h('strong', {}, r.resident_alias), ` ・ ${r.caregiver_name} ・ ${fmtTime(r.created_at)} ・ ${r.doc.sentences.length} 句 `,
      h('span', { class: 'tag' }, RECORD_STATUS_TEXT[r.status]), r.has_audio ? h('span', { class: 'tag' }, '可回聽') : null),
    h('div', { class: 'muted' }, r.doc.sentences[0] ? r.doc.sentences[0].text : ''));
  const section = (title, list, hint) => h('div', { class: 'card' }, h('h2', {}, `${title}（${list.length}）`),
    hint ? h('p', { class: 'muted' }, hint) : null,
    list.length ? null : h('p', { class: 'muted' }, '沒有紀錄。'), ...list.map(item));
  view.append(
    section('待督導確認', group('caregiver_checked'), '照護員已確認。請回聽、修正，把人名換成代號後確認；確認後才會提供給後端，並刪除音訊。'),
    section('照護員尚未確認', group('draft')),
    section('已確認', group('confirmed')));
}

async function reviewEditor(view, r) {
  const locked = r.status === 'confirmed';
  const doc = JSON.parse(JSON.stringify(r.doc));
  const codes = () => Object.keys(doc.speakers);
  const opts = (values, current, blank) => [
    blank ? h('option', { value: '' }, blank) : null,
    ...values.map((v) => h('option', { value: v, selected: v === current }, v))];
  let audio = null;
  const audioBox = h('div', {});
  const body = h('div', {});
  const msg = h('div', {});

  async function loadAudio() {
    const headers = { Authorization: `Bearer ${app.token}` };
    if (app.actingRole) headers['X-Acting-Role'] = app.actingRole;
    const res = await fetch(`/api/supervisor/records/${r.seq}/audio`, { headers });
    if (!res.ok) { audioBox.replaceChildren(h('p', { class: 'muted' }, '沒有音訊（已確認或超過 7 天即刪除）。')); return; }
    audio = h('audio', { controls: true, src: URL.createObjectURL(await res.blob()), style: 'width:100%' });
    audioBox.replaceChildren(audio);
    draw();
  }

  function play(s) {
    if (!audio || doc.audio_start_ms == null) return;
    audio.currentTime = Math.max(0, (s.start_ms - doc.audio_start_ms) / 1000);
    audio.play();
  }

  function merge(i) {
    const [a, b] = [doc.sentences[i], doc.sentences[i + 1]];
    doc.sentences.splice(i, 2, { ...a, end_ms: b.end_ms, text: `${a.text}，${b.text}`,
      source_idx: [...new Set([...a.source_idx, ...b.source_idx])] });
    draw();
  }

  function split(i, textarea) {
    const s = doc.sentences[i];
    const cut = textarea.selectionStart;
    if (cut <= 0 || cut >= s.text.length) { notice('請先把游標放在要拆開的位置。'); return; }
    // 時間依字數比例估計；可回聽確認
    const at = Math.round(s.start_ms + (s.end_ms - s.start_ms) * cut / s.text.length);
    doc.sentences.splice(i, 1, { ...s, end_ms: at, text: s.text.slice(0, cut).trim() },
      { ...s, start_ms: at, text: s.text.slice(cut).trim() });
    draw();
  }

  async function save() {
    const res = await api('PUT', `/api/supervisor/records/${r.seq}`, { doc });
    Object.assign(doc, res.doc);
    return res;
  }

  async function move(i, direction) {
    try {
      await save();
      await api('POST', `/api/supervisor/records/${r.seq}/move`, { i, direction });
      notice(direction === 'prev' ? '已移到上一輪。' : '已移到下一輪。');
      const fresh = (await api('GET', '/api/supervisor/records')).find((x) => x.seq === r.seq);
      view.replaceChildren();
      reviewEditor(view, fresh);
    } catch (err) { msg.replaceChildren(errorBox(err)); }
  }

  const nameFrom = h('input', { placeholder: '人名（例：王阿姨）', maxlength: '20' });
  const nameTo = h('input', { placeholder: '代號（例：家屬甲）', maxlength: '20' });
  const namesChecked = h('input', { type: 'checkbox', id: 'names-checked' });

  function replaceName() {
    const from = nameFrom.value.trim();
    const to = nameTo.value.trim();
    if (!from || !to) return;
    let n = 0;
    for (const s of doc.sentences) {
      const parts = s.text.split(from);
      n += parts.length - 1;
      s.text = parts.join(to);
    }
    notice(`已替換 ${n} 處。`);
    nameFrom.value = '';
    draw();
  }

  function draw() {
    const rows = doc.sentences.map((s, i) => {
      const text = h('textarea', { rows: 2, disabled: locked, oninput: (e) => { s.text = e.target.value; } }, s.text);
      const last = i === doc.sentences.length - 1;
      return h('div', { class: 'utt' },
        h('div', { class: 'meta' },
          audio ? h('button', { class: 'secondary', type: 'button', onclick: () => play(s) }, '▶') : null,
          h('select', { disabled: locked, onchange: (e) => { s.speaker = e.target.value || null; if (s.speaker && !s.role_manual) s.role = doc.speakers[s.speaker]; } },
            opts(codes(), s.speaker, '（無代號）')),
          h('select', { disabled: locked, onchange: (e) => { s.role = e.target.value; s.role_manual = true; } }, opts(app.speakerRoles, s.role)),
          h('select', { disabled: locked, onchange: (e) => { s.intent = e.target.value || null; } }, opts(app.intentLabels, s.intent, '（意圖）')),
          h('select', { disabled: locked, onchange: (e) => { s.intent_owner = e.target.value || null; } }, opts(app.speakerRoles, s.intent_owner, '（需求歸屬）'))),
        text,
        locked ? null : h('div', { class: 'row', style: 'margin-top:4px; flex-wrap:wrap' },
          last ? null : h('button', { class: 'secondary', type: 'button', onclick: () => merge(i) }, '與下一句合併'),
          h('button', { class: 'secondary', type: 'button', onclick: () => split(i, text) }, '從游標處拆開'),
          i === 0 ? h('button', { class: 'secondary', type: 'button', onclick: () => move(i, 'prev') }, '移到上一輪') : null,
          last ? h('button', { class: 'secondary', type: 'button', onclick: () => move(i, 'next') }, '移到下一輪') : null));
    });
    const speakerRows = codes().map((code) => h('div', { class: 'row', style: 'margin-bottom:6px' },
      h('span', { class: 'tag' }, `說話人 ${code}`),
      h('select', { disabled: locked, onchange: (e) => {
        doc.speakers[code] = e.target.value;
        for (const s of doc.sentences) if (s.speaker === code && !s.role_manual) s.role = e.target.value;
        draw();
      } }, opts(app.speakerRoles, doc.speakers[code]))));
    body.replaceChildren(
      speakerRows.length ? h('div', { style: 'margin:8px 0' }, h('strong', {}, '說話人代號對應'), ...speakerRows) : null,
      // 需求歸屬由意圖模型 N0 預先填入，準確度不足（2026-10-14 n0-intent-model 第 4.3 節）
      locked ? null : h('p', { class: 'muted' }, '「需求歸屬」是系統建議，僅供參考，確認前請逐句檢查。'),
      ...rows);
  }

  view.append(h('div', { class: 'card' },
    h('div', { class: 'row' }, h('button', { class: 'secondary', type: 'button', onclick: () => go('review') }, '← 返回列表')),
    h('h2', {}, `${r.resident_alias}・${r.caregiver_name}`),
    h('div', { class: 'muted' }, `${fmtTime(r.created_at)} ・ `, h('span', { class: 'tag' }, RECORD_STATUS_TEXT[r.status])),
    r.has_audio ? h('div', { style: 'margin:8px 0' }, audioBox) : null,
    body,
    locked ? h('p', { class: 'muted' }, `已由 ${r.confirmed_by} 確認，不能再修改。`) : h('div', {},
      h('h3', { style: 'margin-top:12px' }, '人名換成代號'),
      h('div', { class: 'row' }, nameFrom, nameTo, h('button', { class: 'secondary', type: 'button', onclick: replaceName }, '全部替換')),
      h('p', { class: 'muted' }, '代號對照只用在這個畫面，不會存檔，也不會給後端。'),
      h('label', { class: 'row', for: 'names-checked', style: 'margin-top:8px' }, namesChecked, '我已檢查，逐字稿中的人名都已換成代號'),
      h('div', { class: 'row', style: 'margin-top:8px' },
        h('button', { class: 'secondary', type: 'button', onclick: async () => {
          try { await save(); notice('已儲存。'); } catch (err) { msg.replaceChildren(errorBox(err)); }
        } }, '儲存'),
        h('button', { class: 'primary', type: 'button', onclick: async () => {
          try {
            await save();
            await api('POST', `/api/supervisor/records/${r.seq}/confirm`, { names_checked: namesChecked.checked });
            notice('已確認，紀錄會提供給後端，音訊已刪除。');
            go('review');
          } catch (err) { msg.replaceChildren(errorBox(err)); }
        } }, '確認並提供給後端')),
      msg)));
  draw();
  if (r.has_audio) loadAudio();
}

// ---------- 管理員 ----------

async function renderAdmin(view) {
  const accounts = await api('GET', '/api/admin/accounts');
  const name = h('input', { placeholder: '姓名', maxlength: '30' });
  const role = h('select', {}, h('option', { value: 'caregiver' }, '照護員'), h('option', { value: 'supervisor' }, '督導'));
  const result = h('div', {});

  // 效期由伺服器決定：本機模式 24 小時，串接後端時 10 分鐘（2026-10-19）
  function showCode(title, code, expiresAt) {
    const validity = expiresAt ? `${fmtTime(expiresAt)} 前有效` : '有效期限內';
    showDialog(h('h3', {}, title), h('p', {}, `請把綁定碼交給本人；${validity}、只能使用一次。關閉後無法再查看。`),
      h('p', { class: 'code' }, code));
  }

  view.append(
    h('div', { class: 'card' }, h('h2', {}, '新增帳號'),
      h('div', { class: 'row' }, name, role, h('button', {
        class: 'primary', type: 'button',
        onclick: async () => {
          try {
            const r = await api('POST', '/api/admin/accounts', { display_name: name.value, role: role.value });
            showCode(`${r.account.display_name}（${ROLE_NAMES[r.account.role]}）的綁定碼`, r.binding_code, r.expires_at);
            go('admin');
          } catch (err) { result.replaceChildren(errorBox(err)); }
        },
      }, '新增並產生綁定碼')), result),
    h('div', { class: 'card' }, h('h2', {}, '帳號'),
      h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, '姓名'), h('th', {}, '身分'), h('th', {}, '狀態'), h('th', {}, ''))),
        h('tbody', {}, ...accounts.map((a) => h('tr', {},
          h('td', {}, a.display_name),
          h('td', {}, ROLE_NAMES[a.role]),
          h('td', {}, a.status === 'disabled' ? '已停用' : (a.bound ? '已綁定' : `待綁定${a.code_expires_at ? `（碼至 ${fmtTime(a.code_expires_at)}）` : '（碼已失效）'}`)),
          h('td', {}, a.status !== 'disabled' && !a.bound ? h('button', { class: 'secondary', type: 'button', onclick: async () => {
            const r = await api('POST', `/api/admin/accounts/${a.id}/binding-code`);
            showCode(`${a.display_name} 的新綁定碼`, r.binding_code, r.expires_at);
            go('admin');
          } }, '重新產生') : null,
          a.status !== 'disabled' ? h('button', { class: 'danger', type: 'button', onclick: async () => {
            if (!confirm(`停用 ${a.display_name}？`)) return;
            await api('PATCH', `/api/admin/accounts/${a.id}`, { status: 'disabled' });
            go('admin');
          } }, '停用') : null)))))),
  );
}

document.addEventListener('click', (e) => {
  const menu = document.getElementById('me-menu');
  if (!menu.hidden && !e.target.closest('.me')) menu.hidden = true;
});

boot().catch((err) => {
  document.getElementById('view').replaceChildren(errorBox(err));
});
