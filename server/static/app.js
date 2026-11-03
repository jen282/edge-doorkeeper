// 대시보드: 1초 폴링으로 집계·알림을 갱신한다. docs/08-alerts-dashboard.md
// 서버 데이터는 textContent로만 넣는다(HTML로 해석하지 않음).
"use strict";

const POLL_MS = 1000;
const ALERT_LIMIT = 20;
const TYPE_LABEL = {
  tailgate: "동반입장 의심",
  unauth_entry: "미인증 입장",
  long_stay: "장기 체류",
  night_stay: "심야 체류",
  device_offline: "기기 오프라인 (감시 공백)",
  count_anomaly: "카운트 오차",
};
const $ = (id) => document.getElementById(id);
let seenAlertIds = null;  // 첫 로딩의 기존 알림은 배너로 띄우지 않는다
let bannerTimer = null;
let streamUrl = null;

function fmtDuration(ms) {
  if (ms < 60_000) return `${ms / 1000}초`;
  if (ms < 3_600_000) return `${ms / 60_000}분`;
  return `${ms / 3_600_000}시간`;
}

function shortTime(kst) {
  return kst ? kst.slice(5) : "";  // "MM-DD HH:MM:SS"
}

function describe(a) {
  const d = a.details || {};
  switch (a.type) {
    case "tailgate":
    case "unauth_entry":
      return `인증 ${d.allowed} / 입장 ${d.n_in} · CCTV ${shortTime(a.at_kst)} ~ ${shortTime(a.until_kst)} 확인 권장`;
    case "long_stay":
      return `${d.confirmed}명 · ${shortTime(a.at_kst)} ~ ${shortTime(a.until_kst)} 사이 입장 · ${fmtDuration(d.t_long_ms)} 기준`;
    case "night_stay":
      return `매장 내 ${d.occupancy}명`;
    case "device_offline":
      return `${Math.round((d.silent_ms || 0) / 1000)}초 동안 신호 없음 · 이 구간은 집계가 빠져 있음`;
    case "count_anomaly":
      return `계산된 인원 ${d.occupancy}명 · 퇴장이 입장보다 많음 (기준점 재설정 필요할 수 있음)`;
    default:
      return "";
  }
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${path} → HTTP ${r.status}`);
  return r.json();
}

function showBanner(a) {
  const b = $("banner");
  b.className = a.severity === "crit" ? "crit" : "warn";
  b.textContent = `${TYPE_LABEL[a.type] || a.type} · ${shortTime(a.at_kst)} · ${describe(a)}`;
  clearTimeout(bannerTimer);
  bannerTimer = setTimeout(() => { b.className = ""; }, 3000);
}

function renderAlerts(alerts) {
  const ul = $("alerts");
  ul.replaceChildren();
  if (alerts.length === 0) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "알림 없음";
    ul.append(li);
  }
  for (const a of alerts) {
    const li = document.createElement("li");
    li.className = `${a.acked ? "" : "unacked"} ${a.severity}`;
    const title = document.createElement("div");
    title.className = "title";
    title.textContent = TYPE_LABEL[a.type] || a.type;
    const time = document.createElement("span");
    time.className = "time";
    time.textContent = `  ${shortTime(a.at_kst)}`;
    title.append(time);
    const desc = document.createElement("div");
    desc.className = "desc";
    desc.textContent = describe(a);
    li.append(title, desc);
    if (!a.acked) {
      const btn = document.createElement("button");
      btn.textContent = "확인";
      btn.onclick = async () => {
        await api(`/v1/alerts/${a.id}/ack`, { method: "POST" });
        tick();
      };
      li.append(btn);
    }
    ul.append(li);
  }
  // 새로 생긴 warn 이상 알림만 배너로
  const ids = new Set(alerts.map((a) => a.id));
  if (seenAlertIds) {
    const fresh = alerts.filter((a) => !seenAlertIds.has(a.id) && a.severity !== "info");
    if (fresh.length) showBanner(fresh[0]);
  }
  seenAlertIds = ids;
}

function renderOccupancy(o) {
  $("asof").textContent = o.as_of_kst
    ? `기준 시각 ${o.as_of_kst}${o.mode === "replay" ? " (재생 모드: 시나리오 시각)" : ""}`
    : "기준 시각 - (기기 신호 대기 중)";
  $("auth-mode").textContent = o.auth_rules === false ? "· 인증 대조 꺼짐 (시연 모드)" : "";
  $("occ").textContent = o.occupancy_now;
  $("in-today").textContent = o.in_today;
  $("long").textContent = o.long_stay_confirmed;
  $("long-kpi").classList.toggle("alert", o.long_stay_confirmed > 0);
  const badge = $("unacked");
  badge.textContent = o.unacked_alerts;
  badge.classList.toggle("zero", o.unacked_alerts === 0);
  const d = o.device;
  $("dev-dot").className = `dot ${d ? (d.online ? "on" : "off") : ""}`;
  // 프레임 없이 heartbeat만 보내는 구간(fps 0)은 추론 수치가 의미 없으므로 '유휴'로 표시
  $("dev-text").textContent = !d ? "기기 정보 없음"
    : !d.online ? `${d.device_id} 오프라인`
    : d.fps ? `${d.device_id} 정상 · 추론 ${d.infer_ms_p50}ms · ${d.fps.toFixed(1)} FPS`
    : `${d.device_id} 정상 · 재생 유휴`;
  if (d && d.stream_url !== streamUrl) {
    streamUrl = d.stream_url;
    connectStream();
  }
  if (!d) setViewMessage("기기 정보 없음");
  else if (!d.online) setViewMessage("기기 오프라인");
  if (document.activeElement !== $("t-long")) $("t-long").value = String(o.t_long_ms);
}

function setViewMessage(reason) {
  $("stream").style.display = "none";
  $("view-msg").style.display = "flex";
  $("view-reason").textContent = reason;
}

function readToken() {
  try { return localStorage.getItem("streamToken") || ""; } catch { return ""; }
}

function connectStream() {
  const img = $("stream");
  const token = readToken();
  if (!streamUrl) return setViewMessage("기기 주소를 아직 모름");
  if (!token) return setViewMessage("실시간 영상 조회 패스워드를 입력하세요");
  img.onload = () => { img.style.display = "block"; $("view-msg").style.display = "none"; };
  img.onerror = () => setViewMessage("기기에 연결할 수 없음 (실시간 영상 꺼짐, 패스워드 오류, 또는 다른 사람이 시청 중)");
  img.src = `${streamUrl}?token=${encodeURIComponent(token)}`;
}

async function loadSettings() {
  const s = await api("/v1/settings");
  const sel = $("t-long");
  sel.replaceChildren();
  for (const ms of s.allowed_t_long_ms) {
    const opt = document.createElement("option");
    opt.value = String(ms);
    opt.textContent = fmtDuration(ms) + (ms < 60_000 ? " (데모)" : "");
    sel.append(opt);
  }
  sel.value = String(s.t_long_ms);
  sel.onchange = async () => {
    $("t-long-error").textContent = "";
    try {
      await api("/v1/settings/t_long_ms", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value: Number(sel.value) }),
      });
      tick();
    } catch (e) {
      $("t-long-error").textContent = `변경 실패: ${e.message}`;
    }
  };
}

async function tick() {
  try {
    const [o, alerts] = await Promise.all([api("/v1/occupancy"), api(`/v1/alerts?limit=${ALERT_LIMIT}`)]);
    renderOccupancy(o);
    renderAlerts(alerts);
    $("conn-error").textContent = "";
  } catch (e) {
    $("conn-error").textContent = `서버 연결 오류: ${e.message}`;
  }
}

$("stream-token").value = readToken();
$("stream-connect").onclick = () => {
  try { localStorage.setItem("streamToken", $("stream-token").value); } catch { /* 저장 불가 환경 */ }
  connectStream();
};

loadSettings().catch((e) => { $("t-long-error").textContent = `설정 불러오기 실패: ${e.message}`; });
tick();
setInterval(tick, POLL_MS);
