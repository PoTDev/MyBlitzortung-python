// SPDX-License-Identifier: GPL-3.0-or-later
import {labels} from "./i18n.js";
import {drawChart, drawDirectionRose, spectrum} from "./charts.js";
import {makeMap} from "./map.js";

const $ = id => document.getElementById(id);
const buttons = [...document.querySelectorAll("[data-page]")];
const admin = 1, settings = 2, archive = 128;
const allowed = (permission) => Boolean(user?.level & (admin | permission));
const t = key => labels[lang][key] ?? key;
const format = n => new Intl.NumberFormat(lang, {maximumFractionDigits: 2}).format(n);
const date = value => value ? new Intl.DateTimeFormat(lang, {
  timeZone: "UTC", dateStyle: "short", timeStyle: "medium",
}).format(new Date(value)) : "—";
const timeInput = n => new Date(n).toISOString().slice(0, 16);
const keys = {live: "map", archive: "archive", stations: "stations", settings: "settings"};

let lang = localStorage.getItem("mybo-lang") === "en" ? "en" : "ru";
let theme = localStorage.getItem("mybo-theme") === "dark" ? "dark" : "light";
let user, prefs, page, map, lastLive, currentStats, mapUpdated, stationRequest;
let stationItems = [], stationShown = 100;
let archiveItems = [], archiveQuery, archiveCursor, densityItems;
let replayTimer, liveTimer, signalVersion = 0, epoch = 0, session = 0;

class ApiError extends Error {
  constructor(status) {
    super(String(status));
    this.status = status;
  }
}

async function api(path, {method = "GET", body, params} = {}) {
  const url = new URL("api/" + path, document.baseURI);
  if (params) url.search = new URLSearchParams(params);
  const response = await fetch(url, {
    method, headers: body ? {"Content-Type": "application/json"} : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) throw new ApiError(response.status);
  return response.status === 204 ? null : response.json();
}

function showNotice(key = "", good = false) {
  const notice = $("notice");
  notice.textContent = key ? t(key) : "";
  notice.classList.toggle("ok", good);
  notice.hidden = !key;
}

function errorLabel(error) {
  return ({403: "forbidden", 409: "duplicate", 422: "invalid", 429: "throttled"})[error.status] || "failed";
}

function report(error) {
  if (error.status === 401) {
    showPublic().catch(() => showLogin("failed"));
    return;
  }
  showNotice(errorLabel(error));
}

function button(label, action) {
  const node = document.createElement("button");
  node.type = "button";
  node.textContent = t(label);
  node.addEventListener("click", action);
  return node;
}

function setHeading() {
  if (!page) return;
  $("page-title").textContent = t(keys[page]);
  $("page-caption").textContent = t(page + "Caption");
}

function tick() {
  $("clock").textContent = new Intl.DateTimeFormat(lang, {
    timeZone: "UTC", timeStyle: "medium", hourCycle: "h23",
  }).format(new Date()) + " UTC";
}

function applyTheme() {
  document.documentElement.dataset.theme = theme;
  document.querySelector('meta[name="color-scheme"]').content = theme;
  const toggle = $("theme-toggle");
  toggle.textContent = t(theme === "dark" ? "lightTheme" : "darkTheme");
  toggle.setAttribute("aria-label", t(theme === "dark" ? "switchToLightTheme" : "switchToDarkTheme"));
  toggle.setAttribute("aria-pressed", String(theme === "light"));
}

function localize() {
  document.documentElement.lang = lang;
  $("language").value = lang;
  $("language").setAttribute("aria-label", t("language"));
  $("nav").setAttribute("aria-label", t("map") + " · " + t("settings"));
  applyTheme();
  for (const node of document.querySelectorAll("[data-t]")) node.textContent = t(node.dataset.t);
  for (const node of document.querySelectorAll("[data-t-placeholder]")) node.placeholder = t(node.dataset.tPlaceholder);
  setHeading();
  tick();
  map?.localize();
  if (page === "live" && lastLive) renderLive();
  if (page === "archive") renderArchive();
  if (page === "stations") {
    renderStations();
    if (currentStats) renderStats();
  }
  if (page === "settings" && allowed(admin)) loadAccounts().catch(report);
  showNotice();
  $("login-status").textContent = "";
}

function clearPrivateState() {
  session++;
  epoch++;
  stopReplay();
  clearTimeout(liveTimer);
  user = prefs = page = lastLive = currentStats = stationRequest = undefined;
  stationItems = [];
  stationShown = 100;
  archiveQuery = archiveCursor = densityItems = mapUpdated = undefined;
  archiveItems = [];
  if ($("signal-dialog").open) $("signal-dialog").close();
  map?.drawStations([], 0);
  map?.drawStrikes([], 0, 1, date);
}

function showLogin(error = "") {
  clearPrivateState();
  $("dashboard").hidden = $("nav").hidden = $("logout").hidden = $("login-open").hidden = true;
  $("register").hidden = false;
  $("login").hidden = false;
  $("login-form").reset();
  $("brand").textContent = document.title = "MyBlitzortung";
  localize();
  $("login-status").textContent = error ? t(error) : "";
}

async function showPublic() {
  clearPrivateState();
  prefs = await api("public/settings");
  if (!localStorage.getItem("mybo-lang")) lang = prefs.locale;
  $("brand").textContent = document.title = prefs.title;
  $("login").hidden = $("nav").hidden = $("logout").hidden = true;
  $("dashboard").hidden = $("login-open").hidden = $("register").hidden = false;
  $("station-layer-control").hidden = true;
  $("station-layer").checked = false;
  $("minutes").value = String(prefs.minutes);
  localize();
  await showPage("live");
  map.view(prefs.lat, prefs.lon, prefs.zoom);
}

async function signedIn(account) {
  const fetched = await api("settings");
  user = account;
  prefs = fetched;
  session++;
  if (!localStorage.getItem("mybo-lang")) lang = prefs.locale;
  $("brand").textContent = document.title = prefs.title;
  $("login").hidden = true;
  $("password-form").hidden = user.auth_source === "superadmin";
  $("superadmin-account").hidden = user.auth_source !== "superadmin";
  $("dashboard").hidden = $("nav").hidden = $("logout").hidden = false;
  $("login-open").hidden = $("register").hidden = true;
  $("station-layer-control").hidden = false;
  $("minutes").value = String(prefs.minutes);
  $("station-layer").checked = false;
  $("stats-station").value = prefs.own_station ? `#${prefs.own_station}` : "0";
  $("stats-days").value = "1";
  $("archive-station").value = prefs.own_station ? `#${prefs.own_station}` : "";
  const end = Date.now();
  $("archive-start").value = timeInput(end - 86400000);
  $("archive-end").value = timeInput(end);
  buttons.find(item => item.dataset.page === "archive").hidden = !allowed(archive);
  for (const option of $("stats-days").options) option.disabled = !allowed(archive) && option.value !== "1";
  localize();
  await showPage("live");
  map.view(prefs.lat, prefs.lon, prefs.zoom);
}

async function showPage(next) {
  if ((!user && next !== "live") || (next === "archive" && !allowed(archive))) return;
  page = next;
  const version = ++epoch;
  stopReplay();
  showNotice();
  for (const item of buttons) {
    if (item.dataset.page === page) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  }
  setHeading();
  const mapPage = page === "live" || page === "archive";
  $("map-section").hidden = !mapPage;
  $("live-tools").hidden = page !== "live";
  $("archive-tools").hidden = $("archive-results").hidden = page !== "archive";
  $("station-section").hidden = page !== "stations";
  $("settings-section").hidden = page !== "settings";
  if (mapPage) {
    if (!map) {
      map = makeMap(prefs, t, () => {
        if (page === "live") {
          clearTimeout(liveTimer);
          liveTimer = setTimeout(() => loadLive().catch(report), 170);
        }
      }, async id => {
        await showPage("stations");
        if (page === "stations") {
          const station = stationItems.find(item => item.id === id);
          if (station) $("stats-station").value = `#${station.bo_station_id}`;
          await loadStats();
        }
      });
    }
    map.resize();
    map.drawStations([], 0);
    map.drawStrikes([], 0, 1, date);
    if (page === "live") {
      $("legend").hidden = false;
      $("map-count").textContent = t("loading");
      $("map-updated").textContent = "";
      if (user && $("station-layer").checked) showStationsLayer().catch(report);
      await loadLive();
    } else {
      renderArchive();
      await loadStations();
      if (version !== epoch) return;
      if (!archiveQuery) await searchArchive();
    }
  } else if (page === "stations") {
    renderStations();
    await loadStations();
    if (version !== epoch) return;
    if (!$("stats-station").value.trim() && prefs.own_station) {
      $("stats-station").value = `#${prefs.own_station}`;
    }
    await loadStats();
  } else {
    $("display-form").hidden = !allowed(settings);
    $("users-section").hidden = !allowed(admin);
    for (const element of $("display-form").elements) {
      if (!element.name) continue;
      if (element.type === "checkbox") element.checked = prefs[element.name];
      else element.value = prefs[element.name];
    }
    if (allowed(admin)) await loadAccounts();
  }
}

let liveVersion = 0;
async function loadLive() {
  if (page !== "live") return;
  const version = ++liveVersion;
  const entry = epoch;
  const minutes = Number($("minutes").value);
  try {
    const data = await api(user ? "strikes/live" : "strikes/public", {
      params: {...map.area(), minutes},
    });
    if (entry !== epoch || version !== liveVersion) return;
    lastLive = {data, minutes, time: Date.now()};
    renderLive();
    showNotice();
  } catch (error) {
    if (entry === epoch && version === liveVersion) {
      console.error("Live map update failed", error);
      report(error);
    }
  }
}

function renderLive() {
  const {data, minutes, time} = lastLive;
  map.drawStrikes(data.items, time - minutes * 60000, time, date);
  $("map-count").textContent = data.items.length
    ? `${t("visibleCount")}: ${format(data.items.length)}${data.next ? ". " + t("capped") : ""}` : t("empty");
  $("map-updated").textContent = `${t("updated")} · ${date(time)} UTC`;
}

async function loadStations() {
  if (stationRequest) return stationRequest;
  const entry = session;
  stationRequest = (async () => {
    let after = 0;
    do {
      const result = await api("stations", {params: {after, limit: 500}});
      if (entry !== session) return;
      stationItems.push(...result.items);
      after = result.next;
      renderStationChoices();
      if (page === "stations") renderStations();
    } while (after);
  })().catch(error => {
    if (entry === session) {
      stationRequest = undefined;
      stationItems = [];
    }
    throw error;
  });
  return stationRequest;
}

function renderStationChoices() {
  const options = [];
  for (const item of stationItems) {
    const location = [item.city, item.country].filter(Boolean).join(", ");
    const add = (value, label) => {
      const option = document.createElement("option");
      option.value = value;
      option.label = label;
      options.push(option);
    };
    add(`#${item.bo_station_id}`, location);
    add(String(item.bo_station_id), `#${item.bo_station_id}${location ? ` · ${location}` : ""}`);
    if (location) add(`${location} · #${item.bo_station_id}`, `#${item.bo_station_id}`);
  }
  $("station-options").replaceChildren(...options);
}

function stationDatabaseId(inputId) {
  const value = $(inputId).value.trim();
  if (!value || value === "0") return 0;
  const number = value.match(/#(\d+)/)?.[1] ?? (/^\d+$/.test(value) ? value : null);
  if (!number) return null;
  return stationItems.find(item => item.bo_station_id === Number(number))?.id ?? null;
}

async function showStationsLayer() {
  if (!$("station-layer").checked) {
    map.drawStations([], 0);
    return;
  }
  await loadStations();
  if (page === "live" && $("station-layer").checked) map.drawStations(stationItems, prefs.own_station);
}

function statusCode(status) {
  if (status === "D") return "removed";
  const code = Math.floor(Number(status) / 10);
  return ({1: "offline", 2: "idle", 3: "active"})[code] || (code >= 4 ? "fault" : "unknown");
}

function cell(row, value) {
  const td = row.insertCell();
  if (value instanceof Node) td.append(value);
  else td.textContent = value;
  return td;
}

function emptyRow(body, columns) {
  const td = cell(body.insertRow(), t("empty"));
  td.colSpan = columns;
}

function renderStations() {
  const body = $("station-rows");
  body.replaceChildren();
  const query = $("station-search").value.trim().toLocaleLowerCase();
  const matches = query ? stationItems.filter(item =>
    [item.bo_station_id, item.city, item.country].some(value =>
      String(value ?? "").toLocaleLowerCase().includes(query))) : stationItems;
  const visible = query ? matches : matches.slice(0, stationShown);
  for (const item of visible) {
    const row = body.insertRow();
    const select = button("station", () => {
      $("stats-station").value = `#${item.bo_station_id}`;
      loadStats().catch(report);
      $("station-section").scrollIntoView({block: "start"});
    });
    select.textContent = `#${item.bo_station_id}${prefs?.own_station === item.bo_station_id ? " · " + t("own") : ""}`;
    cell(row, select);
    cell(row, [item.city, item.country].filter(Boolean).join(", ") || "—");
    cell(row, t(statusCode(item.status)));
    cell(row, date(item.last_time));
  }
  if (!stationItems.length) emptyRow(body, 4);
  else if (!visible.length) emptyRow(body, 4);
  $("more-stations").hidden = Boolean(query) || stationShown >= stationItems.length;
}

let statsVersion = 0;
async function loadStats() {
  const version = ++statsVersion;
  const entry = epoch;
  const stationId = stationDatabaseId("stats-station");
  if (stationId === null) throw new ApiError(422);
  const data = await api("statistics", {params: {days: $("stats-days").value, station_id: stationId}});
  if (entry !== epoch || version !== statsVersion) return;
  currentStats = data;
  renderStats();
}

function renderStats() {
  for (const [chart, series, key] of [
    ["strikes-chart", currentStats.strikes, "count"],
    ["signals-chart", currentStats.samples, "signalsh"],
    ["station-strikes-chart", currentStats.samples, "strikesh"],
  ]) {
    drawChart($(chart), series.map(row => ({x: Date.parse(row.time), y: row[key], label: date(row.time)})), t, format);
  }
  drawDirectionRose($("directions-chart"), currentStats.directions, t, format);
  const insights = currentStats.insights;
  $("station-insights").hidden = !insights;
  if (!insights) return;
  const metric = (id, value, suffix = "") => {
    $(id).textContent = value === null || value === undefined ? "—" : `${format(value)}${suffix}`;
  };
  metric("metric-detected", insights.summary.detected_period);
  metric("metric-signals", insights.summary.signal_rate);
  metric("metric-strikes", insights.summary.strike_rate);
  metric("metric-efficiency", insights.summary.efficiency, "%");
  const radius = $("radius-stats");
  radius.replaceChildren();
  for (const item of insights.radii) {
    const row = document.createElement("div");
    row.className = "radius-row";
    const label = document.createElement("span");
    label.textContent = `≤ ${format(item.km)} km`;
    const track = document.createElement("span");
    track.className = "radius-track";
    const fill = document.createElement("i");
    fill.style.width = `${item.percent ?? 0}%`;
    track.append(fill);
    const value = document.createElement("strong");
    value.textContent = item.percent === null ? `0 / ${format(item.total)}`
      : `${format(item.detected)} / ${format(item.total)} · ${format(item.percent)}%`;
    row.append(label, track, value);
    radius.append(row);
  }
  if (!insights.radii.length) radius.textContent = t("directionUnavailable");
  drawChart($("distance-chart"), insights.distances.map(item => ({
    x: item.bucket, y: item.count, label: `${item.label} km`,
  })), t, format, t("distanceAxis"));
  drawChart($("hourly-chart"), insights.hours.map(item => ({
    x: item.hour, y: item.count, label: `${String(item.hour).padStart(2, "0")}:00`,
  })), t, format, t("hourlyAxis"));
}

function archiveParams() {
  const start = Date.parse($("archive-start").value + "Z");
  const end = Date.parse($("archive-end").value + "Z");
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) throw new ApiError(422);
  const params = {start: new Date(start).toISOString(), end: new Date(end).toISOString()};
  const stationId = stationDatabaseId("archive-station");
  if (stationId === null) throw new ApiError(422);
  if (stationId) params.station_id = stationId;
  if ($("use-viewport").checked) Object.assign(params, map.area());
  return params;
}

let archiveVersion = 0;
async function searchArchive(more = false, density = false) {
  const version = ++archiveVersion;
  const entry = epoch;
  const params = more ? archiveQuery : archiveParams();
  stopReplay();
  showNotice();
  $("next-archive").disabled = true;
  $("density").disabled = true;
  try {
    const data = await api(density ? "strikes/density" : "strikes/archive", {
      params: more ? {...params, cursor: archiveCursor} : params,
    });
    if (entry !== epoch || version !== archiveVersion) return;
    archiveQuery = params;
    if (density) {
      densityItems = data.items;
      archiveItems = [];
      archiveCursor = undefined;
    } else {
      densityItems = undefined;
      archiveItems = more ? [...archiveItems, ...data.items] : data.items;
      archiveCursor = archiveItems.length < 5000 ? data.next : null;
    }
    mapUpdated = Date.now();
    renderArchive();
  } catch (error) {
    if (entry === epoch && version === archiveVersion) report(error);
  } finally {
    if (version === archiveVersion) {
      $("next-archive").disabled = false;
      $("density").disabled = false;
    }
  }
}

function renderArchive() {
  $("density").setAttribute("aria-pressed", String(Boolean(densityItems)));
  const body = $("archive-rows");
  body.replaceChildren();
  $("archive-results").hidden = Boolean(densityItems);
  $("legend").hidden = Boolean(densityItems);
  $("map-updated").textContent = mapUpdated ? `${t("updated")} · ${date(mapUpdated)} UTC` : "";
  $("replay").disabled = archiveItems.length < 2;
  $("next-archive").hidden = !archiveCursor;
  if (densityItems) {
    map.drawDensity(densityItems, format);
    $("map-count").textContent = densityItems.length ? `${t("densityCount")}: ${format(densityItems.length)}` : t("empty");
    return;
  }
  if (archiveQuery) map.drawStrikes(archiveItems, Date.parse(archiveQuery.start), Date.parse(archiveQuery.end), date);
  $("map-count").textContent = archiveItems.length
    ? `${t("visibleCount")}: ${format(archiveItems.length)}` : t(archiveQuery ? "empty" : "loading");
  for (const item of archiveItems) {
    const row = body.insertRow();
    cell(row, date(item.time) + "." + String(item.time_ns).padStart(9, "0"));
    cell(row, `${item.lat.toFixed(3)}, ${item.lon.toFixed(3)}`);
    cell(row, item.stations ?? "—");
    const actions = document.createElement("div");
    actions.className = "row";
    actions.append(button("showOnMap", () => {
      stopReplay();
      renderArchive();
      map.focusStrike(item);
      $("map").scrollIntoView({block: "center"});
    }), button("openSignal", () => openSignal(item.id)));
    cell(row, actions);
  }
  if (!archiveItems.length) emptyRow(body, 4);
}

function stopReplay() {
  clearInterval(replayTimer);
  replayTimer = undefined;
  $("replay").textContent = t("replay");
}

function replay() {
  if (replayTimer) {
    stopReplay();
    renderArchive();
    return;
  }
  let count = 0;
  const items = [...archiveItems].reverse();
  $("replay").textContent = t("stop");
  replayTimer = setInterval(() => {
    count += Math.max(1, Math.ceil(items.length / 30));
    map.drawStrikes(items.slice(0, count), Date.parse(archiveQuery.start), Date.parse(archiveQuery.end), date);
    if (count >= items.length) stopReplay();
  }, 140);
}

async function openSignal(id) {
  const version = ++signalVersion;
  const dialog = $("signal-dialog");
  const body = $("signal-body");
  body.textContent = t("loading");
  if (!dialog.open) dialog.showModal();
  try {
    const data = await api(`strikes/${id}/signal`);
    if (version !== signalVersion || !dialog.open) return;
    body.replaceChildren();
    for (const [index, values] of data.channels.entries()) {
      for (const [kind, points, xAxis] of [
        ["amplitude", values.map((y, x) => ({x, y})), "samples"],
        ["spectrum", spectrum(values, data.sample_interval_ns), data.sample_interval_ns ? "frequency" : "bin"],
      ]) {
        const title = document.createElement("h3");
        title.textContent = `${t("channel")} ${data.channel_numbers[index]} · ${t(kind)}`;
        const plot = document.createElement("div");
        plot.className = "chart";
        drawChart(plot, points.map(point => ({...point, label: format(point.x)})), t, format, t(xAxis));
        body.append(title, plot);
      }
    }
  } catch (error) {
    if (version === signalVersion) body.textContent = t(error.status === 404 ? "noSignal" : errorLabel(error));
    if (error.status === 401) showLogin();
  }
}

function checkbox(label, checked) {
  const container = document.createElement("label");
  container.className = "check";
  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = checked;
  container.append(input, document.createTextNode(t(label)));
  return {container, input};
}

async function loadAccounts(after = 0) {
  const entry = epoch;
  const accounts = await api("users", {params: {after}});
  if (entry !== epoch) return;
  const list = $("users-list");
  if (!after) list.replaceChildren();
  else list.lastElementChild?.remove();
  for (const item of accounts) {
    const row = document.createElement("div");
    row.className = "account-row";
    const name = document.createElement("b");
    name.textContent = item.login;
    row.append(name);
    if (item.level & admin) {
      row.append(document.createTextNode(t("administrator")));
    } else {
      const a = checkbox("archive", Boolean(item.level & archive));
      const s = checkbox("settings", Boolean(item.level & settings));
      const pass = document.createElement("input");
      pass.type = "password";
      pass.autocomplete = "new-password";
      pass.maxLength = 256;
      pass.placeholder = t("newPassword");
      pass.setAttribute("aria-label", t("newPassword"));
      row.append(a.container, s.container, pass, button("save", async () => {
        try {
          const body = {archive: a.input.checked, settings: s.input.checked};
          if (pass.value) body.password = pass.value;
          await api(`users/${item.id}`, {method: "PUT", body});
          pass.value = "";
          showNotice("saved", true);
        } catch (error) { report(error); }
      }), button("delete", async () => {
        if (!confirm(t("confirmDelete"))) return;
        try {
          await api(`users/${item.id}`, {method: "DELETE"});
          row.remove();
          showNotice("saved", true);
        } catch (error) { report(error); }
      }));
    }
    list.append(row);
  }
  if (accounts.length === 200) list.append(button("loadAccounts", () => loadAccounts(accounts.at(-1).id).catch(report)));
}

function form(id, submit, onError = report) {
  $(id).addEventListener("submit", async event => {
    event.preventDefault();
    const control = event.submitter;
    if (control?.disabled) return;
    if (control) control.disabled = true;
    try {
      await submit(event.currentTarget);
    } catch (error) {
      onError(error);
    } finally {
      if (control) control.disabled = false;
    }
  });
}

form("login-form", async form => {
  const account = await api("login", {method: "POST", body: Object.fromEntries(new FormData(form))});
  form.reset();
  await signedIn(account);
}, error => { $("login-status").textContent = t(error.status === 401 ? "badLogin" : errorLabel(error)); });

form("archive-tools", () => searchArchive());
form("stats-form", () => loadStats());
form("display-form", async form => {
  const body = Object.fromEntries(new FormData(form));
  for (const name of ["lat", "lon", "zoom", "minutes"]) body[name] = Number(body[name]);
  const saved = await api("settings", {method: "PUT", body});
  prefs = {...prefs, ...saved};
  $("brand").textContent = document.title = saved.title;
  $("minutes").value = String(saved.minutes);
  showNotice("saved", true);
});
form("password-form", async form => {
  try {
    await api("password", {method: "PUT", body: Object.fromEntries(new FormData(form))});
  } catch (error) {
    if (error.status !== 403) throw error;
    showNotice("badLogin");
    return;
  }
  form.reset();
  showNotice("saved", true);
});
form("users-form", async form => {
  const body = Object.fromEntries(new FormData(form));
  body.archive = form.elements.archive.checked;
  body.settings = form.elements.settings.checked;
  await api("users", {method: "POST", body});
  form.reset();
  await loadAccounts();
  showNotice("saved", true);
});

$("language").addEventListener("change", event => {
  lang = event.target.value;
  localStorage.setItem("mybo-lang", lang);
  localize();
});
$("theme-toggle").addEventListener("click", () => {
  theme = theme === "dark" ? "light" : "dark";
  localStorage.setItem("mybo-theme", theme);
  applyTheme();
});
$("logout").addEventListener("click", async () => {
  try {
    await api("logout", {method: "POST"});
    await showPublic();
  } catch (error) { report(error); }
});
$("login-open").addEventListener("click", () => showLogin());
$("back-to-map").addEventListener("click", () => showPublic().catch(() => showLogin("failed")));
$("minutes").addEventListener("change", loadLive);
$("refresh").addEventListener("click", loadLive);
$("station-layer").addEventListener("change", () => showStationsLayer().catch(report));
$("recenter").addEventListener("click", () => map.view(prefs.lat, prefs.lon, prefs.zoom));
$("density").addEventListener("click", () => searchArchive(false, !Boolean(densityItems)).catch(report));
$("next-archive").addEventListener("click", () => searchArchive(true).catch(report));
$("replay").addEventListener("click", replay);
$("stats-days").addEventListener("change", () => loadStats().catch(report));
$("station-search").addEventListener("input", renderStations);
$("more-stations").addEventListener("click", () => { stationShown += 100; renderStations(); });
let expandedChart, expandedChartPlaceholder;
for (const control of document.querySelectorAll(".chart-expand")) {
  control.addEventListener("click", () => {
    const chart = $(control.dataset.chart);
    if (!chart) return;
    const dialog = $("chart-dialog");
    $("chart-dialog-title").textContent = control.closest(".card").querySelector("h2").textContent;
    expandedChartPlaceholder = document.createElement("span");
    chart.before(expandedChartPlaceholder);
    expandedChart = chart;
    $("chart-dialog-body").append(chart);
    dialog.showModal();
  });
}
$("chart-dialog").addEventListener("close", () => {
  if (expandedChart && expandedChartPlaceholder?.isConnected) {
    expandedChartPlaceholder.replaceWith(expandedChart);
  }
  expandedChart = expandedChartPlaceholder = undefined;
});
for (const item of buttons) item.addEventListener("click", () => showPage(item.dataset.page).catch(report));

setInterval(tick, 1000);
setInterval(() => { if (page === "live" && !document.hidden) loadLive(); }, 60000);
localize();
api("me").then(signedIn).catch(error => {
  if (error.status === 401) showPublic().catch(() => showLogin("failed"));
  else showLogin("failed");
});
