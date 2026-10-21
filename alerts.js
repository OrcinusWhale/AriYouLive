const API = (location.protocol === 'file:' ? 'http://127.0.0.1:8765' : location.origin) + '/api';
const MUTE_ICON = '<svg class="ui-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9"/><path d="M10 21h4"/><path d="m3 3 18 18"/></svg>';
const SNOOZE_ICON = '<svg class="ui-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="M4 9v6h4l5 4V5L8 9H4Z"/><path d="m17 9 5 6m0-6-5 6"/></svg>';
let data = { alerts: [], devices: [], groups: [] };
let activeTab = 'active';
let historyOffset = 0, historyHasMore = false, historyLoading = false;
const historyCache = new Map();
const pendingAcknowledgements = new Set();
const pendingSnoozes = new Map();
let cleanupPrefsLoaded = false, cleanupSaveChain = Promise.resolve(), refreshRunning = false, refreshQueued = false;

function escapeHtml(value = '') {
  return String(value).replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
}
function formatTime(seconds) {
  return new Date(Number(seconds) * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' });
}
function fillFilter() {
  const select = document.querySelector('#alertIpFilter'), current = select.value;
  const addresses = [...new Set(data.devices.flatMap(device => device.ips || []))].sort();
  select.innerHTML = '<option value="">All IP addresses</option>' + addresses.map(ip => `<option value="${escapeHtml(ip)}">${escapeHtml(ip)}</option>`).join('');
  if (addresses.includes(current)) select.value = current;
}
function fillGroupFilter() {
  const select = document.querySelector('#alertGroupFilter'), current = select.value;
  const groups = Array.isArray(data.groups) ? data.groups : [];
  const roots = groups.filter(group => group.parent_id == null);
  const options = ['<option value="">All Groups</option>', '<option value="ungrouped">Ungrouped</option>'];
  for (const group of roots) {
    options.push(`<option value="group:${Number(group.id)}">${escapeHtml(group.name)}</option>`);
    for (const subgroup of groups.filter(item => Number(item.parent_id) === Number(group.id))) {
      options.push(`<option value="subgroup:${Number(subgroup.id)}">↳ ${escapeHtml(subgroup.name)}</option>`);
    }
  }
  select.innerHTML = options.join('');
  if ([...select.options].some(option => option.value === current)) select.value = current;
}
function render() {
  const now = Date.now() / 1000;
  const active = data.alerts.filter(alert => !Number(alert.acknowledged) && !Number(alert.muted_at_creation));
  const history = data.alerts.filter(alert => Number(alert.acknowledged));
  const muted = data.alerts.filter(alert => Number(alert.muted_at_creation) === 1);
  const selected = activeTab === 'active' ? active : activeTab === 'history' ? history : muted;
  const range = document.querySelector('#historyRangeFilter').value;
  const historyCutoff = range === 'all' ? 0 : now - Number(range) * 86400;
  const ipFilter = document.querySelector('#alertIpFilter').value;
  const groupFilter = document.querySelector('#alertGroupFilter').value;
  const groups = Array.isArray(data.groups) ? data.groups : [];
  const selectedGroupId = groupFilter.startsWith('group:') ? Number(groupFilter.slice(6)) : null;
  const childIds = selectedGroupId == null ? [] : groups.filter(group => Number(group.parent_id) === selectedGroupId).map(group => Number(group.id));
  const visible = selected.filter(alert => {
    if (activeTab !== 'active' && Number(alert.created_at) < historyCutoff) return false;
    const ipMatches = !ipFilter || alert.ip === ipFilter;
    let groupMatches = true;
    if (groupFilter === 'ungrouped') groupMatches = alert.device_group_id == null;
    else if (groupFilter.startsWith('subgroup:')) groupMatches = Number(alert.device_group_id) === Number(groupFilter.slice(9));
    else if (selectedGroupId != null) groupMatches = Number(alert.device_group_id) === selectedGroupId || childIds.includes(Number(alert.device_group_id));
    return ipMatches && groupMatches;
  });
  document.querySelector('#activeCount').textContent = active.length;
  document.querySelector('#historyCount').textContent = data.historyCount ?? history.length;
  document.querySelector('#mutedCount').textContent = data.mutedCount ?? muted.length;
  document.querySelector('#totalCount').textContent = `${data.totalAlertCount ?? data.alerts.length} total alerts`;
  document.querySelector('#listTitle').textContent = activeTab === 'active' ? 'Active alerts' : activeTab === 'history' ? 'Acknowledged history' : 'Alerts from muted devices or groups';
  document.querySelector('#historyTools').hidden = activeTab === 'active';
  document.querySelector('#cleanupAge').hidden = activeTab !== 'history';
  document.querySelector('#cleanupHistory').hidden = activeTab !== 'history';
  document.querySelector('#acknowledgeAll').hidden = activeTab !== 'active';
  document.querySelector('#acknowledgeAll').disabled = active.length === 0;
  const loadMore = document.querySelector('#loadMoreAlerts');
  loadMore.hidden = activeTab !== 'history' || !historyHasMore;
  loadMore.disabled = historyLoading;
  loadMore.textContent = historyLoading ? 'Loading…' : 'Load More Alerts';
  document.querySelectorAll('[data-alert-tab]').forEach(button => button.classList.toggle('selected', button.dataset.alertTab === activeTab));
  const devicesById = new Map((data.devices || []).map(device => [Number(device.id), device]));
  document.querySelector('#alertList').innerHTML = visible.map(alert => {
    const deviceMuted = Boolean(Number(alert.muted_at_creation) || alert.muted || devicesById.get(Number(alert.device_id))?.muted);
    const muteBadge = deviceMuted ? `<span class="alert-muted">${MUTE_ICON}<span>Muted</span></span>` : '';
    const group = alert.parent_group || 'Ungrouped';
    const eventText = escapeHtml(alert.message || '')
      .replace(/\brecovered\b/gi, '<strong class="status-word up">UP</strong>')
      .replace(/\boffline\b/gi, '<strong class="status-word down">DOWN</strong>');
    const details = `<span class="alert-parameters"><strong>Group:</strong> ${escapeHtml(group)} · <strong>Subgroup:</strong> ${escapeHtml(alert.subgroup || '—')} · <strong>IP:</strong> ${escapeHtml(alert.ip)}</span><span class="alert-event">${eventText}</span>${muteBadge}`;
    const infoIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/></svg>';
    const snoozeUntil = Number(alert.snooze_until) || 0;
    const snoozed = snoozeUntil > now;
    const canSnooze = alert.state === 'offline' && alert.current_ip_state === 'offline' && !Number(alert.acknowledged);
    const snoozeBadge = snoozed ? `<span class="snooze-timer" data-snooze-timer="${Number(alert.id)}" data-snooze-until="${snoozeUntil}">${SNOOZE_ICON}<span>${formatSnooze(snoozeUntil, now)}</span></span>` : '';
    return `<article class="alert-row"><i class="state ${alert.state === 'offline' ? 'offline' : 'online'}"></i><div class="alert-message"><b>${escapeHtml(alert.name || 'Device')}</b><small>${details}</small></div><time class="alert-time">${escapeHtml(formatTime(alert.created_at))}</time><button type="button" class="alert-info-button" data-info="${Number(alert.id)}" aria-label="Device information" title="Device information">${infoIcon}<span>Info</span></button>${!Number(alert.acknowledged) ? `<div class="alert-actions">${snoozeBadge}${canSnooze ? `<button class="snooze" data-snooze="${Number(alert.id)}">Snooze 1h</button>` : ''}<button class="ack" data-ack="${Number(alert.id)}">Acknowledge</button></div>` : '<span class="alert-history-label">Acknowledged</span>'}</article>`;
  }).join('') || `<div class="empty">No ${activeTab === 'active' ? 'active' : activeTab === 'history' ? 'acknowledged' : 'muted'} alerts${activeTab !== 'active' && range !== 'all' ? ` in the selected time range` : ''}${ipFilter || groupFilter ? ' match the selected filters' : ''}.</div>`;
}
function formatSnooze(until, now = Date.now() / 1000) {
  const remaining = Math.max(0, Math.ceil(until - now));
  const minutes = Math.floor(remaining / 60), seconds = remaining % 60;
  return `Muted ${minutes}m${minutes < 10 ? ` ${String(seconds).padStart(2, '0')}s` : ''}`;
}
function updateSnoozeTimers() {
  const now = Date.now() / 1000;
  let expired = false;
  document.querySelectorAll('[data-snooze-timer]').forEach(badge => {
    const until = Number(badge.dataset.snoozeUntil);
    if (until <= now) { badge.remove(); expired = true; return; }
    const label = badge.querySelector('span');
    if (label) label.textContent = formatSnooze(until, now);
  });
  if (expired) void refresh();
}
async function refresh() {
  if (refreshRunning) { refreshQueued = true; return; }
  refreshRunning = true;
  const error = document.querySelector('#alertError');
  try {
    const [stateResponse, activeResponse, historyResponse, mutedResponse] = await Promise.all([
      fetch(`${API}/state`, { cache: 'no-store' }),
      fetch(`${API}/alerts?status=active`, { cache: 'no-store' }),
      fetch(`${API}/alerts?status=history&limit=150&offset=0`, { cache: 'no-store' }),
      fetch(`${API}/alerts?status=muted`, { cache: 'no-store' })
    ]);
    for (const response of [stateResponse, activeResponse, historyResponse, mutedResponse]) if (!response.ok) throw new Error(`Service returned ${response.status}`);
    const [next, activeData, historyData, mutedData] = await Promise.all([stateResponse.json(), activeResponse.json(), historyResponse.json(), mutedResponse.json()]);
    historyData.alerts.forEach(alert => historyCache.set(Number(alert.id), alert));
    historyOffset = Math.max(historyOffset, historyData.alerts.length);
    historyHasMore = historyOffset < Number(historyData.count || 0);
    next.historyCount = Number(historyData.count) || 0;
    next.activeCount = Number(activeData.count) || 0;
    next.mutedCount = Number(mutedData.count) || 0;
    next.totalAlertCount = Number(historyData.total) || 0;
    next.alerts = [...new Map([...activeData.alerts, ...historyCache.values(), ...mutedData.alerts].map(alert => [Number(alert.id), alert])).values()].sort((a, b) => Number(b.created_at) - Number(a.created_at));
    for (const alert of next.alerts) {
      if (pendingAcknowledgements.has(Number(alert.id))) alert.acknowledged = 1;
      if (pendingSnoozes.has(Number(alert.id))) alert.snooze_until = pendingSnoozes.get(Number(alert.id));
    }
    data = next;
    applyCleanupPreferences(next.settings || {});
    fillFilter(); fillGroupFilter(); render(); error.hidden = true; error.textContent = '';
  } catch (exception) {
    error.textContent = `Could not load alerts: ${exception.message}`;
    error.hidden = false;
  } finally {
    refreshRunning = false;
    if (refreshQueued) { refreshQueued = false; queueMicrotask(() => void refresh()); }
  }
}
async function loadMoreAlerts(button) {
  if (historyLoading || !historyHasMore) return;
  historyLoading = true; render();
  try {
    const response = await fetch(`${API}/alerts?status=history&limit=150&offset=${historyOffset}`, { cache: 'no-store' });
    if (!response.ok) throw new Error(`Service returned ${response.status}`);
    const page = await response.json();
    page.alerts.forEach(alert => historyCache.set(Number(alert.id), alert));
    historyOffset = Number(page.offset) + page.alerts.length;
    historyHasMore = Boolean(page.has_more);
    data.historyCount = Number(page.count) || data.historyCount;
    data.totalAlertCount = Number(page.total) || data.totalAlertCount;
    data.alerts = [...new Map([...data.alerts, ...page.alerts].map(alert => [Number(alert.id), alert])).values()].sort((a, b) => Number(b.created_at) - Number(a.created_at));
  } catch (exception) {
    const error = document.querySelector('#alertError'); error.textContent = `Could not load more alerts: ${exception.message}`; error.hidden = false;
  } finally { historyLoading = false; render(); }
}
async function acknowledge(id, button) {
  const alert = data.alerts.find(item => Number(item.id) === Number(id));
  if (!alert || Number(alert.acknowledged)) return;
  pendingAcknowledgements.add(Number(id)); alert.acknowledged = 1; render();
  if (button) button.disabled = true;
  try {
    const response = await fetch(`${API}/alert/${Number(id)}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    if (!response.ok) throw new Error(`Service returned ${response.status}`);
    const result = await response.json();
    alert.acknowledged = Number(result.alert?.acknowledged ?? 1);
    pendingAcknowledgements.delete(Number(id)); render();
    historyCache.clear(); historyOffset = 0;
    await refresh();
  } catch (exception) {
    pendingAcknowledgements.delete(Number(id)); alert.acknowledged = 0; render();
    const error = document.querySelector('#alertError'); error.textContent = `Could not acknowledge alert: ${exception.message}`; error.hidden = false;
  }
}
async function snooze(id, button) {
  const alert = data.alerts.find(item => Number(item.id) === Number(id));
  if (!alert || Number(alert.acknowledged)) return;
  const until = Date.now() / 1000 + 3600;
  pendingSnoozes.set(Number(id), until); alert.snooze_until = until; render();
  if (button) button.disabled = true;
  try {
    const response = await fetch(`${API}/alert/${Number(id)}/snooze`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ seconds: 3600 }) });
    if (!response.ok) throw new Error(`Service returned ${response.status}`);
    const result = await response.json();
    alert.snooze_until = Number(result.alert?.snooze_until ?? until);
    pendingSnoozes.delete(Number(id)); render();
    await refresh();
  } catch (exception) {
    pendingSnoozes.delete(Number(id)); alert.snooze_until = null;
    const current = data.alerts.find(item => Number(item.id) === Number(id)); if (current) current.snooze_until = null;
    render();
    const error = document.querySelector('#alertError'); error.textContent = `Could not snooze alert: ${exception.message}`; error.hidden = false;
  }
}

async function acknowledgeAll(button) {
  const ids = data.alerts.filter(alert => !Number(alert.acknowledged) && !Number(alert.muted_at_creation)).map(alert => Number(alert.id));
  if (!ids.length || button.disabled) return;
  ids.forEach(id => pendingAcknowledgements.add(id));
  data.alerts.forEach(alert => { if (ids.includes(Number(alert.id))) alert.acknowledged = 1; });
  render();
  button.disabled = true;
  try {
    const response = await fetch(`${API}/alerts/acknowledge-all`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    if (!response.ok) throw new Error(`Service returned ${response.status}`);
    ids.forEach(id => pendingAcknowledgements.delete(id));
    historyCache.clear(); historyOffset = 0;
    await refresh();
  } catch (exception) {
    ids.forEach(id => pendingAcknowledgements.delete(id));
    data.alerts.forEach(alert => { if (ids.includes(Number(alert.id))) alert.acknowledged = 0; });
    render();
    const error = document.querySelector('#alertError'); error.textContent = `Could not acknowledge active alerts: ${exception.message}`; error.hidden = false;
  }
}
function applyCleanupPreferences(settings) {
  const select = document.querySelector('#cleanupAge');
  const input = document.querySelector('#cleanupCustomDays');
  if (!cleanupPrefsLoaded) {
    const savedAge = String(settings.alert_cleanup_age ?? '30');
    if ([...select.options].some(option => option.value === savedAge)) select.value = savedAge;
    input.value = String(settings.alert_cleanup_custom_days ?? 45);
    cleanupPrefsLoaded = true;
  }
  document.querySelector('#cleanupCustomWrap').hidden = select.value !== 'custom';
}
function saveCleanupPreferences(values) {
  cleanupSaveChain = cleanupSaveChain.catch(() => {}).then(async () => {
    const response = await fetch(`${API}/settings`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(values) });
    if (!response.ok) throw new Error(`Service returned ${response.status}`);
  });
  return cleanupSaveChain;
}
async function cleanupHistory(button) {
  if (button.disabled) return;
  const select = document.querySelector('#cleanupAge');
  const input = document.querySelector('#cleanupCustomDays');
  const custom = select.value === 'custom';
  const days = custom ? Number(input.value) : Number(select.value);
  if (!Number.isInteger(days) || days < 1 || days > 3650) {
    const error = document.querySelector('#alertError'); error.textContent = 'Enter a retention period from 1 to 3650 days.'; error.hidden = false; input.focus(); return;
  }
  if (!window.confirm(`Permanently delete acknowledged alert history older than ${days} days?`)) return;
  button.disabled = true;
  try {
    await saveCleanupPreferences({ alert_cleanup_age: select.value, ...(custom ? { alert_cleanup_custom_days: days } : {}) });
    const response = await fetch(`${API}/alerts/history?older_than_days=${days}`, { method: 'DELETE' });
    if (!response.ok) throw new Error(`Service returned ${response.status}`);
    const result = await response.json();
    historyCache.clear(); historyOffset = 0; historyHasMore = false;
    await refresh();
    const error = document.querySelector('#alertError'); error.textContent = `Deleted ${Number(result.deleted) || 0} historical alert${Number(result.deleted) === 1 ? '' : 's'}.`; error.hidden = false;
  } catch (exception) {
    const error = document.querySelector('#alertError'); error.textContent = `Could not clean up history: ${exception.message}`; error.hidden = false;
  } finally { button.disabled = false; }
}

document.querySelector('#refreshAlerts').addEventListener('click', refresh);
document.querySelector('#alertIpFilter').addEventListener('change', render);
document.querySelector('#alertGroupFilter').addEventListener('change', render);
document.querySelector('#historyRangeFilter').addEventListener('change', render);
document.querySelector('#acknowledgeAll').addEventListener('click', event => void acknowledgeAll(event.currentTarget));
document.querySelector('#cleanupHistory').addEventListener('click', event => void cleanupHistory(event.currentTarget));
document.querySelector('#loadMoreAlerts').addEventListener('click', event => void loadMoreAlerts(event.currentTarget));
document.querySelector('#cleanupAge').addEventListener('change', event => {
  const custom = event.currentTarget.value === 'custom';
  document.querySelector('#cleanupCustomWrap').hidden = !custom;
  void saveCleanupPreferences({ alert_cleanup_age: event.currentTarget.value, ...(custom ? { alert_cleanup_custom_days: Number(document.querySelector('#cleanupCustomDays').value) || 45 } : {}) }).catch(error => { const status = document.querySelector('#alertError'); status.textContent = `Could not save cleanup preference: ${error.message}`; status.hidden = false; });
});
document.querySelector('#cleanupCustomDays').addEventListener('change', event => {
  const days = Number(event.currentTarget.value);
  if (!Number.isInteger(days) || days < 1 || days > 3650) return;
  void saveCleanupPreferences({ alert_cleanup_custom_days: days }).catch(error => { const status = document.querySelector('#alertError'); status.textContent = `Could not save custom retention: ${error.message}`; status.hidden = false; });
});
document.querySelectorAll('[data-alert-tab]').forEach(button => button.addEventListener('click', () => { activeTab = button.dataset.alertTab; render(); }));
document.querySelector('#alertInfoClose').addEventListener('click', () => document.querySelector('#alertInfoDialog').close());
document.querySelector('#alertList').addEventListener('click', event => {
  const infoButton = event.target.closest('[data-info]');
  if (infoButton) {
    const alert = data.alerts.find(item => Number(item.id) === Number(infoButton.dataset.info));
    if (alert) {
      document.querySelector('#infoDevice').textContent = alert.name || '—';
      document.querySelector('#infoIp').textContent = alert.ip || '—';
      document.querySelector('#infoGroup').textContent = alert.parent_group || 'Ungrouped';
      document.querySelector('#infoSubgroup').textContent = alert.subgroup || '—';
      document.querySelector('#infoNote').textContent = alert.note || 'No note recorded.';
      document.querySelector('#alertInfoDialog').showModal();
    }
    return;
  }
  const button = event.target.closest('[data-ack]');
  if (button) void acknowledge(button.dataset.ack, button);
  const snoozeButton = event.target.closest('[data-snooze]');
  if (snoozeButton) void snooze(snoozeButton.dataset.snooze, snoozeButton);
});
refresh();
const alertsPoller = setInterval(refresh, 3000);
const snoozeClock = setInterval(updateSnoozeTimers, 1000);
window.addEventListener('pagehide', () => { clearInterval(alertsPoller); clearInterval(snoozeClock); }, { once: true });
