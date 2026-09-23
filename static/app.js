// Copyright (C) 2026 Mathew Levett
// SPDX-License-Identifier: AGPL-3.0-or-later

'use strict';

const SOUND_PREF_KEY = 'raynet-alert-sound-v2';
const THEME_PREF_KEY = 'raynet-theme-v1';
const savedTheme = localStorage.getItem(THEME_PREF_KEY);
document.documentElement.dataset.theme = savedTheme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');

const state = {
  bootstrap: null,
  user: null,
  events: [],
  event: null,
  stations: [],
  eventUsers: [],
  userDirectory: [],
  messages: [],
  users: [],
  controlCallsigns: [],
  direction: 'received',
  sound: localStorage.getItem(SOUND_PREF_KEY) !== 'muted',
  audioContext: null,
  pendingOverdueSound: false,
  websocket: null,
  reconnectTimer: null,
  refreshTimer: null,
  overdueAlerted: new Set(),
  callsignOptions: [],
  callsignMatches: [],
  callsignActiveIndex: -1,
  branding: null,
  pendingLogoDataUrl: null,
  selectedStationId: null,
  suppressCallsignSuggestionsOnce: false,
  timezone: 'Europe/London',
};

const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];

function esc(value) {
  return String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
}

function toast(message, type = '') {
  const el = $('#toast');
  el.textContent = message;
  el.className = `toast show ${type}`;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.className = 'toast', 3800);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: 'same-origin',
    headers: {'Content-Type': 'application/json', ...(options.headers || {})},
    ...options,
  });
  let data = null;
  const type = response.headers.get('content-type') || '';
  if (type.includes('application/json')) data = await response.json();
  else data = await response.text();
  if (!response.ok) {
    const error = new Error(typeof data?.detail === 'string' ? data.detail : (data?.detail?.code || `Request failed (${response.status})`));
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
}

function formatLocal(iso, options = {}) {
  if (!iso) return '—';
  const date = new Date(iso);
  return new Intl.DateTimeFormat('en-GB', {timeZone: state.timezone, ...options}).format(date);
}

function formatLogTime(iso) {
  return formatLocal(iso, {hour: '2-digit', minute: '2-digit', hour12: false});
}

function formatLogDate(iso) {
  return formatLocal(iso, {day: '2-digit', month: 'short', year: 'numeric'});
}

function relativeTime(iso) {
  if (!iso) return 'Never';
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const mins = Math.floor(seconds / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 48) return `${hours}h ${mins % 60}m ago`;
  return formatLocal(iso, {dateStyle: 'medium', timeStyle: 'short'});
}

function roleAtLeast(kind) {
  const role = state.user?.role;
  if (kind === 'write') return ['admin', 'controller', 'operator'].includes(role);
  if (kind === 'controller') return ['admin', 'controller'].includes(role);
  if (kind === 'admin') return role === 'admin';
  return false;
}

function applyPermissions() {
  const apply = (selector, allowed) => $$(selector).forEach(el => {
    if (!allowed) el.classList.add('hidden');
    else if (!el.classList.contains('tab-panel')) el.classList.remove('hidden');
  });
  apply('.permission-write', roleAtLeast('write'));
  apply('.permission-controller', roleAtLeast('controller'));
  apply('.permission-admin', roleAtLeast('admin'));
}

function setAuthMode(needsSetup) {
  $('#auth-screen').classList.remove('hidden');
  $('#app-shell').classList.add('hidden');
  $('#display-name-row').classList.toggle('hidden', !needsSetup);
  $('#callsign-row').classList.toggle('hidden', !needsSetup);
  const branding = state.bootstrap.branding || {};
  $('#auth-brand-logo').src = branding.logo_data_url || '/static/default-brand-logo.png';
  $('#auth-brand-tagline').textContent = branding.tagline || 'South East Hampshire Raynet';
  $('#auth-title').textContent = needsSetup ? 'Create the first administrator' : (branding.org_name || state.bootstrap.org_name);
  $('#auth-copy').textContent = needsSetup
    ? 'This only appears once. The first account controls users, backups and event configuration.'
    : 'Sign in to the operational logger.';
  $('#auth-submit').textContent = needsSetup ? 'Create administrator' : 'Sign in';
  $('#auth-password').autocomplete = needsSetup ? 'new-password' : 'current-password';
  $('#auth-display-name').required = needsSetup;
  $('#auth-form').dataset.mode = needsSetup ? 'setup' : 'login';
}

async function boot() {
  state.bootstrap = await api('/api/bootstrap-status');
  state.timezone = state.bootstrap.timezone;
  try {
    const result = await api('/api/me');
    state.user = result.user;
    await enterApp(result);
  } catch (error) {
    if (error.status !== 401) toast(error.message, 'error');
    setAuthMode(state.bootstrap.needs_setup);
  }
}

async function enterApp(meResult = null) {
  if (!meResult) meResult = await api('/api/me');
  state.user = meResult.user;
  state.timezone = meResult.timezone;
  applyBranding(meResult.branding || {org_name: meResult.org_name, tagline: 'South East Hampshire Raynet', logo_data_url: '/static/default-brand-logo.png'});
  $('#user-button').innerHTML = `<span>${esc(state.user.display_name)}</span><span class="menu-chevron" aria-hidden="true">▾</span>`;
  $('#auth-screen').classList.add('hidden');
  $('#app-shell').classList.remove('hidden');
  applyPermissions();
  await loadEvents();
  if (roleAtLeast('admin')) loadUsers().catch(() => {});
  clearInterval(state.refreshTimer);
  state.refreshTimer = setInterval(() => {
    if (state.event) renderOps();
  }, 15000);
}

$('#auth-form').addEventListener('submit', async event => {
  event.preventDefault();
  const mode = event.currentTarget.dataset.mode;
  const payload = {
    username: $('#auth-username').value.trim(),
    password: $('#auth-password').value,
  };
  if (mode === 'setup') {
    payload.display_name = $('#auth-display-name').value.trim();
    payload.callsign = $('#auth-callsign').value.trim();
  }
  try {
    await api(mode === 'setup' ? '/api/setup' : '/api/login', {method: 'POST', body: JSON.stringify(payload)});
    state.bootstrap.needs_setup = false;
    await enterApp();
  } catch (error) {
    toast(error.message, 'error');
  }
});

async function loadEvents(preferredId = null) {
  state.events = await api('/api/events');
  const select = $('#event-select');
  select.innerHTML = '';
  if (!state.events.length) {
    const option = document.createElement('option');
    option.textContent = 'No events';
    option.value = '';
    select.append(option);
    select.disabled = true;
    state.event = null;
    renderEmptyEvent();
    return;
  }
  select.disabled = false;
  for (const item of state.events) {
    const option = document.createElement('option');
    option.value = item.id;
    option.textContent = `${item.status === 'closed' ? '✓ ' : ''}${item.name} · ${formatLocal(item.started_at, {dateStyle: 'medium'})}`;
    select.append(option);
  }
  const currentId = preferredId || state.event?.id || state.events.find(e => e.status === 'active')?.id || state.events[0].id;
  select.value = String(currentId);
  await selectEvent(Number(currentId));
}

function renderEmptyEvent() {
  $('#empty-event').classList.remove('hidden');
  $('#event-context').classList.add('hidden');
  $('#new-event-button').hidden = false;
  $('#close-event-button').hidden = true;
  $('#export-button').hidden = true;
  $('#overdue-panel').classList.add('hidden');
  $('#overdue-quick-list').innerHTML = '';
  $$('.tab-panel').forEach(panel => panel.classList.add('hidden'));
}

async function selectEvent(eventId) {
  closeWebSocket();
  if (state.user && state.user.role !== 'viewer') {
    await api(`/api/events/${eventId}/join`, {method: 'POST'});
  }
  const snapshot = await api(`/api/events/${eventId}/snapshot`);
  state.event = snapshot.event;
  state.stations = snapshot.stations;
  state.eventUsers = snapshot.event_users || [];
  state.messages = snapshot.messages;
  state.overdueAlerted.clear();
  $('#empty-event').classList.add('hidden');
  const activeTab = $('.tabs button.active')?.dataset.tab || 'messages';
  showTab(activeTab);
  renderEvent();
  renderMessages();
  renderOps();
  rebuildCallsignOptions();
  connectWebSocket();
}

function renderEvent() {
  if (!state.event) return;
  $('#event-context').classList.remove('hidden');
  $('#event-name').textContent = state.event.name;
  const radioParts = [
    state.event.radio_frequency,
    state.event.radio_mode,
    state.event.ctcss_tones ? `CTCSS ${state.event.ctcss_tones}` : '',
    state.event.talk_groups ? `TG ${state.event.talk_groups.replace(/\s*\n\s*/g, ', ')}` : '',
  ].filter(value => value?.trim());
  $('#event-radio').classList.toggle('hidden', radioParts.length === 0);
  $('#event-radio-summary').textContent = radioParts.join(' · ') || '—';
  renderEventDetails();
  const closed = state.event.status === 'closed';
  $('#new-event-button').hidden = !closed;
  $('#close-event-button').hidden = false;
  $('#export-button').hidden = false;
  $('#close-event-button').textContent = closed ? 'Reopen event' : 'Close event';
  $('#message-form').classList.toggle('hidden', closed || !roleAtLeast('write'));
}

function renderEventDetails() {
  if (!state.event) return;
  const contacts = [state.event.event_contacts, state.event.phone_numbers].filter(value => value?.trim()).join('\n');
  const fields = [
    ['Notes', state.event.event_notes],
    ['Location information', state.event.location_details || state.event.location],
    ['Event contacts', contacts],
    ['Radio frequency', state.event.radio_frequency],
    ['CTCSS tones', state.event.ctcss_tones],
    ['Radio mode', state.event.radio_mode],
    ['Talk groups', state.event.talk_groups],
  ].filter(([, value]) => value?.trim());
  $('#event-details-content').innerHTML = fields.length
    ? fields.map(([label, value]) => `<div class="event-detail-item"><span class="label">${esc(label)}</span><div>${esc(value)}</div></div>`).join('')
    : '<p class="muted">No additional event details have been recorded.</p>';
  $('#event-details-summary').textContent = fields.length ? `${fields.length} item${fields.length === 1 ? '' : 's'} recorded` : 'Notes, locations and contacts';
}

$('#edit-event-details').addEventListener('click', async () => {
  if (!state.event) return;
  const existingContacts = [state.event.event_contacts, state.event.phone_numbers].filter(value => value?.trim()).join('\n');
  const result = await formDialog('Edit event details', `
    <label>Notes<textarea id="dlg-event-notes" rows="4" maxlength="4000" placeholder="General event notes">${esc(state.event.event_notes || '')}</textarea></label>
    <label>Location information<textarea id="dlg-location-details" rows="3" maxlength="1000" placeholder="Venue, access points, control location or other useful directions">${esc(state.event.location_details || state.event.location || '')}</textarea></label>
    <label>Event contacts<textarea id="dlg-event-contacts" rows="6" maxlength="2000" placeholder="Names, roles, phone numbers, email addresses and other contact details">${esc(existingContacts)}</textarea></label>
    <label>Radio frequency<input id="dlg-radio-frequency" maxlength="200" value="${esc(state.event.radio_frequency || '')}" placeholder="e.g. 145.350 MHz or channel name"></label>
    <label>CTCSS tones<input id="dlg-ctcss-tones" maxlength="200" value="${esc(state.event.ctcss_tones || '')}" placeholder="e.g. 88.5 Hz TX / RX"></label>
    <label>Radio mode<select id="dlg-radio-mode"><option value="">Not specified</option>${['FM','DMR','D-Star','Fusion / C4FM','TETRA','Other'].map(mode => `<option value="${mode}" ${state.event.radio_mode === mode ? 'selected' : ''}>${mode}</option>`).join('')}</select></label>
    <label id="dlg-talk-groups-wrap">DMR talk groups<textarea id="dlg-talk-groups" rows="3" maxlength="1000" placeholder="Talk-group numbers and names, one per line">${esc(state.event.talk_groups || '')}</textarea></label>
  `, 'Save details', () => ({
    event_notes: $('#dlg-event-notes').value,
    location_details: $('#dlg-location-details').value,
    phone_numbers: '',
    event_contacts: $('#dlg-event-contacts').value,
    radio_frequency: $('#dlg-radio-frequency').value,
    ctcss_tones: $('#dlg-ctcss-tones').value,
    radio_mode: $('#dlg-radio-mode').value,
    talk_groups: $('#dlg-radio-mode').value === 'DMR' ? $('#dlg-talk-groups').value : '',
    location: '',
  }), () => {
    const mode = $('#dlg-radio-mode');
    const wrapper = $('#dlg-talk-groups-wrap');
    const syncTalkGroups = () => wrapper.classList.toggle('hidden', mode.value !== 'DMR');
    mode.addEventListener('change', syncTalkGroups);
    syncTalkGroups();
  });
  if (!result) return;
  try {
    state.event = await api(`/api/events/${state.event.id}`, {method: 'PATCH', body: JSON.stringify(result)});
    renderEvent();
    toast('Event details saved.', 'success');
  } catch (error) { toast(error.message, 'error'); }
});

$('#event-select').addEventListener('change', event => selectEvent(Number(event.target.value)).catch(err => toast(err.message, 'error')));

function showTab(tab) {
  $$('.tabs button').forEach(button => button.classList.toggle('active', button.dataset.tab === tab));
  $$('.tab-panel').forEach(panel => panel.classList.toggle('hidden', panel.id !== `${tab}-tab`));
  if (tab === 'audit' && roleAtLeast('controller')) loadAudit();
  if (tab === 'admin' && roleAtLeast('admin')) {
    loadBranding();
    loadUsers();
    loadControlCallsigns();
    loadAdminEvents();
  }
}

$$('.tabs button[data-tab]').forEach(button => button.addEventListener('click', () => showTab(button.dataset.tab)));

function applyBranding(branding) {
  state.branding = branding;
  $('#org-name').textContent = branding.org_name || 'Message Logger';
  $('#org-tagline').textContent = branding.tagline || '';
  $('#org-tagline').classList.toggle('hidden', !branding.tagline);
  $('#brand-logo').src = branding.logo_data_url || '/static/default-brand-logo.png';
}

function setDirection(value) {
  state.direction = value;
  $$('#direction-picker button').forEach(button => button.classList.toggle('selected', button.dataset.value === value));
}

$$('#direction-picker button').forEach(button => button.addEventListener('click', () => setDirection(button.dataset.value)));

function setPriority(value) {
  $('#message-priority').value = value;
  $$('#priority-picker button').forEach(button => button.classList.toggle('selected', button.dataset.value === value));
}

$$('#priority-picker button').forEach(button => button.addEventListener('click', () => setPriority(button.dataset.value)));
setPriority('routine');

function optionIdentityValue(option) {
  return option.tactical ? `${option.callsign} / ${option.tactical}` : option.callsign;
}

function callsignSearchKey(value) {
  return String(value || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
}

function rebuildCallsignOptions() {
  const options = [{
    id: null, callsign: 'ALL STATIONS', tactical: '', onDuty: true, broadcast: true,
    search: 'ALL STATIONS BROADCAST EVERYONE', compactSearch: 'ALLSTATIONSBROADCASTEVERYONE',
  }, ...state.stations.map(station => ({
    id: station.id,
    callsign: station.callsign,
    tactical: station.tactical_call || '',
    onDuty: Boolean(station.on_duty),
    search: `${station.callsign} ${station.tactical_call || ''}`.toUpperCase(),
    compactSearch: callsignSearchKey(`${station.callsign}${station.tactical_call || ''}`),
  }))];

  // A station callsign is unique within an event. Keep the list deterministic and
  // put active resources at the operator's fingertips first.
  state.callsignOptions = options.sort((a, b) =>
    Number(b.onDuty) - Number(a.onDuty) || a.callsign.localeCompare(b.callsign, 'en-GB')
  );
  renderCallsignSuggestions();
}

function matchedCallsignOptions(query) {
  const raw = query.trim().toUpperCase();
  const compact = callsignSearchKey(raw);
  const ranked = state.callsignOptions.map(option => {
    const values = [option.callsign, option.tactical].filter(Boolean).map(value => value.toUpperCase());
    let rank = 99;
    if (!raw) rank = 5;
    else if (values.some(value => value === raw)) rank = 0;
    else if (values.some(value => value.startsWith(raw))) rank = 1;
    else if (compact && values.some(value => callsignSearchKey(value).startsWith(compact))) rank = 2;
    else if (values.some(value => value.includes(raw)) || (compact && values.some(value => callsignSearchKey(value).includes(compact)))) rank = 4;
    return {option, rank};
  }).filter(item => item.rank < 99);

  return ranked.sort((a, b) =>
    a.rank - b.rank || Number(b.option.onDuty) - Number(a.option.onDuty) ||
    a.option.callsign.localeCompare(b.option.callsign, 'en-GB')
  ).slice(0, 10).map(item => item.option);
}

function closeCallsignSuggestions() {
  state.callsignMatches = [];
  state.callsignActiveIndex = -1;
  $('#callsign-suggestions').classList.add('hidden');
  $('#message-callsign').setAttribute('aria-expanded', 'false');
  $('#message-callsign').removeAttribute('aria-activedescendant');
}

function renderCallsignSuggestions(forceOpen = false) {
  const input = $('#message-callsign');
  const list = $('#callsign-suggestions');
  if (!input || !list || !state.event || document.activeElement !== input) {
    closeCallsignSuggestions();
    return;
  }

  state.callsignMatches = matchedCallsignOptions(input.value);
  if (!state.callsignMatches.length) {
    state.callsignActiveIndex = -1;
    list.innerHTML = '<div class="callsign-suggestion no-match">No matching callsign or tactical call. You can still enter it manually.</div>';
  } else {
    if (state.callsignActiveIndex < 0 || state.callsignActiveIndex >= state.callsignMatches.length) {
      state.callsignActiveIndex = 0;
    }
    list.innerHTML = state.callsignMatches.map((option, index) => `
      <button type="button" id="callsign-option-${index}" class="callsign-suggestion ${option.onDuty ? '' : 'off-duty'} ${index === state.callsignActiveIndex ? 'active' : ''}"
              role="option" aria-selected="${index === state.callsignActiveIndex}" aria-label="${esc(optionIdentityValue(option))}${option.broadcast ? ', broadcast to every station' : `, ${option.onDuty ? 'on duty' : 'off duty'}`}"
              title="${esc(optionIdentityValue(option))}" data-callsign-index="${index}">
        <span class="identity-pair"><strong>${esc(option.callsign)}</strong><span class="identity-separator">/</span><span class="tactical">${esc(option.broadcast ? 'Broadcast' : (option.tactical || 'No tactical call'))}</span></span>
        <span class="duty-state">${option.broadcast ? 'Everyone' : (option.onDuty ? 'On duty' : 'Off duty')}</span>
      </button>`).join('');
  }
  if (forceOpen || input.value) {
    list.classList.remove('hidden');
    input.setAttribute('aria-expanded', 'true');
    if (state.callsignActiveIndex >= 0) input.setAttribute('aria-activedescendant', `callsign-option-${state.callsignActiveIndex}`);
  }
}

function chooseCallsign(index, moveToMessage = true) {
  const option = state.callsignMatches[index];
  if (!option) return;
  $('#message-callsign').value = optionIdentityValue(option);
  state.selectedStationId = option.id;
  if (option.broadcast) {
    setDirection('sent');
  }
  closeCallsignSuggestions();
  if (moveToMessage) $('#message-text').focus();
}

$('#message-callsign').addEventListener('focus', () => {
  if (state.suppressCallsignSuggestionsOnce) {
    state.suppressCallsignSuggestionsOnce = false;
    closeCallsignSuggestions();
    return;
  }
  renderCallsignSuggestions(true);
});
$('#message-callsign').addEventListener('input', event => {
  const start = event.target.selectionStart;
  event.target.value = event.target.value.toUpperCase();
  event.target.setSelectionRange(start, start);
  state.selectedStationId = null;
  state.callsignActiveIndex = 0;
  renderCallsignSuggestions(true);
});
$('#message-callsign').addEventListener('keydown', event => {
  const open = !$('#callsign-suggestions').classList.contains('hidden');
  if (event.key === 'ArrowDown') {
    event.preventDefault();
    if (!open) renderCallsignSuggestions(true);
    else if (state.callsignMatches.length) state.callsignActiveIndex = (state.callsignActiveIndex + 1) % state.callsignMatches.length;
    renderCallsignSuggestions(true);
  } else if (event.key === 'ArrowUp') {
    event.preventDefault();
    if (!open) renderCallsignSuggestions(true);
    else if (state.callsignMatches.length) state.callsignActiveIndex = (state.callsignActiveIndex - 1 + state.callsignMatches.length) % state.callsignMatches.length;
    renderCallsignSuggestions(true);
  } else if (event.key === 'Enter' && open && state.callsignActiveIndex >= 0) {
    event.preventDefault();
    chooseCallsign(state.callsignActiveIndex);
  } else if (event.key === 'Escape' && open) {
    event.preventDefault();
    closeCallsignSuggestions();
  }
});
$('#callsign-suggestions').addEventListener('mousedown', event => {
  const button = event.target.closest('[data-callsign-index]');
  if (!button) return;
  event.preventDefault();
  chooseCallsign(Number(button.dataset.callsignIndex));
});
$('#message-callsign').addEventListener('blur', () => setTimeout(closeCallsignSuggestions, 120));

document.addEventListener('keydown', event => {
  if (event.ctrlKey && event.key === 'Enter' && !$('#message-form').classList.contains('hidden')) {
    event.preventDefault();
    $('#message-form').requestSubmit();
  }
  const directionShortcuts = {
    KeyR: 'received',
    KeyS: 'sent',
    KeyI: 'info',
  };
  const direction = directionShortcuts[event.code] || directionShortcuts[`Key${event.key.toUpperCase()}`];
  if (event.altKey && direction && !event.ctrlKey && !event.metaKey && !$('#message-form').classList.contains('hidden')) {
    event.preventDefault();
    setDirection(direction);
  }
  const priorityShortcuts = {Digit1: 'routine', Digit2: 'priority', Digit3: 'immediate'};
  const priority = priorityShortcuts[event.code];
  if (event.altKey && priority && !event.ctrlKey && !event.metaKey && !$('#message-form').classList.contains('hidden')) {
    event.preventDefault();
    setPriority(priority);
  }
});

$('#message-form').addEventListener('submit', async event => {
  event.preventDefault();
  await submitMessage(false);
});

async function submitMessage(forceUnknown) {
  const payload = {
    callsign: $('#message-callsign').value.trim(),
    station_id: state.selectedStationId,
    identity_mode: 'both',
    message: $('#message-text').value.trim(),
    direction: state.direction,
    priority: $('#message-priority').value,
    force_unknown: forceUnknown,
  };
  if (!payload.callsign || !payload.message) return;
  try {
    await api(`/api/events/${state.event.id}/messages`, {method: 'POST', body: JSON.stringify(payload)});
    $('#message-text').value = '';
    $('#message-callsign').value = '';
    state.selectedStationId = null;
    closeCallsignSuggestions();
    state.suppressCallsignSuggestionsOnce = true;
    $('#message-callsign').focus();
    setPriority('routine');
    setDirection('received');
  } catch (error) {
    if (error.status === 409 && error.data?.detail?.code === 'unknown_callsign') {
      const ok = await confirmDialog(
        'Identity not on Operators list',
        `<p><strong>${esc(error.data.detail.callsign)}</strong> was not found as either a callsign or tactical call.</p><p>You can still log it. The entry will remain clearly attributable to you.</p>`,
        'Log anyway'
      );
      if (ok) await submitMessage(true);
      else $('#message-callsign').focus();
    } else toast(error.message, 'error');
  }
}

function renderMessages() {
  const tbody = $('#message-rows');
  const query = $('#message-search').value.trim().toLowerCase();
  const filter = $('#direction-filter').value;
  const items = state.messages.filter(item => {
    if (filter !== 'all' && item.direction !== filter) return false;
    if (!query) return true;
    return [item.callsign, item.station_callsign, item.tactical_call, item.message, item.operator_name, item.operator_callsign, item.operator_control_call, item.priority, String(item.sequence)].join(' ').toLowerCase().includes(query);
  });
  $('#message-total').textContent = `${items.length} of ${state.messages.length} messages`;
  tbody.innerHTML = items.map(item => `
    <tr class="${item.voided ? 'voided' : ''}" data-message-id="${item.id}">
      <td>${item.sequence}${item.version > 1 ? `<sup>v${item.version}</sup>` : ''}</td>
      <td title="${esc(formatLocal(item.dtg, {dateStyle:'full', timeStyle:'medium'}))}"><strong class="log-time">${esc(formatLogTime(item.dtg))}</strong><br><span class="muted log-date">${esc(formatLogDate(item.dtg))}</span></td>
      <td><span class="priority ${item.priority}">${esc(item.priority)}</span></td>
      <td><strong>${esc(item.station_callsign || item.callsign)}</strong></td>
      <td>${esc(item.tactical_call || '—')}</td>
      <td><span class="direction ${item.direction}">${esc(item.direction)}</span></td>
      <td>${esc(item.message)}</td>
      <td>${esc(item.operator_name)}${item.operator_callsign ? `<br><span class="muted">${esc(item.operator_callsign)}</span>` : ''}${item.operator_control_call ? `<br><strong>${esc(item.operator_control_call)}</strong>` : ''}</td>
      <td class="permission-controller"><div class="row-actions"><button data-action="correct">Correct</button>${item.voided ? '' : '<button data-action="void">Void</button>'}</div></td>
    </tr>`).join('');
  applyPermissions();
}

$('#message-search').addEventListener('input', renderMessages);
$('#direction-filter').addEventListener('change', renderMessages);

$('#message-rows').addEventListener('click', async event => {
  const action = event.target.dataset.action;
  if (!action) return;
  const id = Number(event.target.closest('tr').dataset.messageId);
  const message = state.messages.find(item => item.id === id);
  if (!message) return;
  if (action === 'correct') await correctMessage(message);
  if (action === 'void') await voidMessage(message);
});

async function correctMessage(item) {
  const ok = await formDialog('Correct logged message', `
    <p class="muted">The original version is retained in the audit history. Message #${item.sequence}, version ${item.version}.</p>
    <label>Callsign or tactical call<input id="dlg-callsign" value="${esc(item.callsign)}" required></label>
    <label>Direction<select id="dlg-direction"><option value="received">Received</option><option value="sent">Sent</option><option value="info">Info</option></select></label>
    <label>Priority<select id="dlg-priority"><option value="routine">Routine</option><option value="priority">Priority</option><option value="immediate">Immediate</option></select></label>
    <label>Message<textarea id="dlg-message" rows="5" required>${esc(item.message)}</textarea></label>
    <label>Reason for correction<input id="dlg-reason" required minlength="3" placeholder="e.g. Read-back correction"></label>
  `, 'Save correction', () => ({
    callsign: $('#dlg-callsign').value,
    identity_mode: 'both',
    direction: $('#dlg-direction').value,
    priority: $('#dlg-priority').value,
    message: $('#dlg-message').value,
    reason: $('#dlg-reason').value,
  }), () => {
    $('#dlg-direction').value = item.direction;
    $('#dlg-priority').value = item.priority;
  });
  if (!ok) return;
  try {
    await api(`/api/messages/${item.id}/correct`, {method: 'POST', body: JSON.stringify(ok)});
  } catch (error) { toast(error.message, 'error'); }
}

async function voidMessage(item) {
  const result = await formDialog('Void message', `<p>Void message <strong>#${item.sequence}</strong>? It will stay visible and remain in exports.</p><label>Reason<input id="dlg-reason" required minlength="3"></label>`, 'Void message', () => ({reason: $('#dlg-reason').value}));
  if (!result) return;
  try { await api(`/api/messages/${item.id}/void`, {method: 'POST', body: JSON.stringify(result)}); }
  catch (error) { toast(error.message, 'error'); }
}

function stationStatus(station) {
  if (!station.on_duty) return {state: 'off', text: station.duty_status === 'stood_down' ? 'Stood down' : 'Off duty', due: null};
  if (station.in_control) return {state: 'control', text: 'Control', due: null};
  if (!station.last_heard_at) return {state: 'due', text: 'No check yet', due: null};
  const due = new Date(station.last_heard_at).getTime() + station.check_interval_minutes * 60000;
  const now = Date.now();
  if (now >= due + 60000) return {state: 'overdue', text: 'Overdue', due};
  if (now >= due) return {state: 'due', text: 'Check due', due};
  return {state: 'ok', text: 'OK', due};
}

async function eventUserDialog(title, assignment) {
  if (!state.userDirectory.length) state.userDirectory = await api('/api/user-directory');
  const available = assignment ? state.userDirectory : state.userDirectory.filter(user => !state.eventUsers.some(item => item.user_id === user.id));
  if (!assignment && !available.length) {
    toast('All active users are already assigned to this event');
    return null;
  }
  return formDialog(title, `
    ${assignment
      ? `<p>User: <strong>${esc(assignment.display_name)}</strong> <span class="muted">${esc(assignment.callsign || '')}</span></p>`
      : `<label>User<select id="dlg-event-user">${available.map(user => `<option value="${user.id}">${esc(user.display_name)}${user.callsign ? ` / ${esc(user.callsign)}` : ''}</option>`).join('')}</select></label>`}
    <label class="checkbox"><input id="dlg-event-control" type="checkbox" ${assignment?.in_control ? 'checked' : ''}> Control</label>
    <label>Tactical call<input id="dlg-event-tactical" value="${esc(assignment?.tactical_call || '')}" list="control-call-options" autocapitalize="characters" placeholder="e.g. CONTROL">
      <datalist id="control-call-options"><option value="CONTROL"><option value="RAYNET CONTROL"></datalist>
    </label>
  `, assignment ? 'Save assignment' : 'Assign user', () => ({
    ...(assignment ? {} : {user_id: Number($('#dlg-event-user').value)}),
    tactical_call: $('#dlg-event-tactical').value,
    in_control: $('#dlg-event-control').checked,
  }), () => {
    const control = $('#dlg-event-control');
    const tactical = $('#dlg-event-tactical');
    control.addEventListener('change', () => { tactical.value = control.checked ? 'CONTROL' : ''; });
  });
}

$('#add-event-user-button').addEventListener('click', async () => {
  try {
    const result = await eventUserDialog('Assign user to event', null);
    if (result) await api(`/api/events/${state.event.id}/operators`, {method: 'POST', body: JSON.stringify(result)});
  } catch (error) { toast(error.message, 'error'); }
});

function renderOps() {
  if (!state.event) return;
  const stats = {total: state.stations.length, duty: 0, control: 0, due: 0, overdue: 0};
  const rows = state.stations.map(station => {
    const status = stationStatus(station);
    if (station.on_duty) stats.duty++;
    if (station.on_duty && station.in_control) stats.control++;
    if (status.state === 'due') stats.due++;
    if (status.state === 'overdue') stats.overdue++;
    return {station, status};
  });
  $('#ops-summary').innerHTML = [
    ['Operators', stats.total], ['On duty', stats.duty], ['Control', stats.control], ['Check due', stats.due], ['Overdue', stats.overdue]
  ].map(([label, value]) => `<div class="summary-card"><strong>${value}</strong><span>${label}</span></div>`).join('');
  const overdueRows = rows.filter(({status}) => status.state === 'overdue');
  const overduePanel = $('#overdue-panel');
  overduePanel.classList.toggle('hidden', overdueRows.length === 0);
  $('#overdue-summary').textContent = `${overdueRows.length} operator${overdueRows.length === 1 ? '' : 's'} awaiting check-in`;
  $('#overdue-quick-list').innerHTML = overdueRows.map(({station, status}) => {
    const minutes = Math.max(1, Math.floor((Date.now() - status.due) / 60000));
    const identity = [station.callsign, station.tactical_call].filter(Boolean).join(' / ');
    return `<div class="overdue-quick-item" data-station-id="${station.id}"><div class="overdue-quick-identity"><strong>${esc(identity)}</strong><span>${minutes} min overdue${station.name ? ` · ${esc(station.name)}` : ''}</span></div><button class="primary permission-write" data-action="quick-heard">Heard now</button></div>`;
  }).join('');
  $('#station-rows').innerHTML = rows.map(({station, status}) => {
    const permanentAssignment = state.eventUsers.find(assignment => assignment.callsign && assignment.callsign.toUpperCase() === station.callsign.toUpperCase());
    return `
    <tr data-station-id="${station.id}">
      <td><span class="status-pill ${status.state}">${esc(status.text)}</span></td>
      <td>${esc(station.name || '—')}</td>
      <td><strong>${esc(station.callsign)}</strong></td>
      <td>${esc(station.tactical_call || '—')}</td>
      <td>${station.duty_status === 'stood_down' ? 'Stood down' : (station.on_duty ? 'On duty' : 'Off duty')}</td>
      <td>${station.in_control ? 'Exempt' : `${station.check_interval_minutes} min`}</td>
      <td title="${esc(station.last_heard_at ? formatLocal(station.last_heard_at, {dateStyle:'medium', timeStyle:'medium'}) : '')}">${esc(relativeTime(station.last_heard_at))}</td>
      <td>${status.due ? esc(formatLocal(new Date(status.due).toISOString(), {hour:'2-digit', minute:'2-digit'})) : '—'}</td>
      <td>${esc(station.notes || '')}</td>
      <td><div class="row-actions"><button data-action="heard" ${station.on_duty && !station.in_control ? '' : 'disabled'}>Heard now</button><select class="duty-action permission-controller" data-action="duty" aria-label="Duty state for ${esc(station.callsign)}"><option value="on_duty" ${station.on_duty ? 'selected' : ''}>On duty</option><option value="off_duty" ${!station.on_duty && station.duty_status !== 'stood_down' ? 'selected' : ''}>Off duty</option><option value="stood_down" ${station.duty_status === 'stood_down' ? 'selected' : ''}>Stood down</option></select>${permanentAssignment ? `<button data-action="unassign" data-assignment-id="${permanentAssignment.id}" class="permission-controller">Unassign</button>` : '<button data-action="edit" class="permission-controller">Edit</button><button data-action="delete" class="permission-controller">Remove</button>'}</div></td>
    </tr>`;
  }).join('');
  applyPermissions();
  if (stats.overdue > 0 && state.sound) {
    const fresh = rows.filter(({station, status}) => status.state === 'overdue' && !state.overdueAlerted.has(station.id));
    if (fresh.length) {
      fresh.forEach(({station}) => state.overdueAlerted.add(station.id));
      beep().then(played => { if (!played) state.pendingOverdueSound = true; });
      toast(`${fresh.length} station check${fresh.length === 1 ? '' : 's'} overdue`, 'error');
    }
  }
  rows.filter(({status}) => status.state !== 'overdue').forEach(({station}) => state.overdueAlerted.delete(station.id));
}

function updateSoundButton() {
  const button = $('#sound-button');
  button.textContent = state.sound ? '🔔' : '🔕';
  button.setAttribute('aria-pressed', String(state.sound));
  button.title = state.sound ? 'Alert sound enabled. Click to mute.' : 'Enable and test overdue alert sound';
}

async function ensureAudioReady() {
  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  if (!AudioCtx) throw new Error('This browser does not support alert audio');
  if (!state.audioContext || state.audioContext.state === 'closed') state.audioContext = new AudioCtx();
  if (state.audioContext.state === 'suspended') await state.audioContext.resume();
  return state.audioContext;
}

async function beep() {
  if (!state.sound) return false;
  try {
    const ctx = await ensureAudioReady();
    if (ctx.state !== 'running') return false;
    const start = ctx.currentTime;
    [0, .22].forEach((offset, index) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.value = index ? 660 : 880;
      gain.gain.setValueAtTime(.0001, start + offset);
      gain.gain.exponentialRampToValueAtTime(.20, start + offset + .015);
      gain.gain.exponentialRampToValueAtTime(.0001, start + offset + .18);
      osc.connect(gain); gain.connect(ctx.destination);
      osc.start(start + offset); osc.stop(start + offset + .19);
    });
    return true;
  } catch (error) {
    toast(`Alert sound unavailable: ${error.message}`, 'error');
    return false;
  }
}

$('#sound-button').addEventListener('click', async () => {
  if (state.sound) {
    state.sound = false;
    localStorage.setItem(SOUND_PREF_KEY, 'muted');
    updateSoundButton();
    toast('Overdue alert sound muted');
    return;
  }

  state.sound = true;
  localStorage.setItem(SOUND_PREF_KEY, 'enabled');
  updateSoundButton();
  const played = await beep();
  if (played) state.pendingOverdueSound = false;
  toast(played ? 'Alert sound enabled and tested' : 'Sound enabled, but the browser blocked playback', played ? '' : 'error');
});

const unlockAlertAudio = async () => {
  if (!state.sound) return;
  try {
    await ensureAudioReady();
    if (state.pendingOverdueSound) {
      const played = await beep();
      if (played) state.pendingOverdueSound = false;
    }
  } catch (_) {}
};
document.addEventListener('pointerdown', unlockAlertAudio, {once: true, capture: true});
document.addEventListener('keydown', unlockAlertAudio, {once: true, capture: true});
updateSoundButton();

$('#station-rows').addEventListener('click', async event => {
  const action = event.target.dataset.action;
  if (!action) return;
  const id = Number(event.target.closest('tr').dataset.stationId);
  const station = state.stations.find(item => item.id === id);
  if (!station) return;
  try {
    if (action === 'heard') await api(`/api/stations/${id}/heard`, {method: 'POST'});
    if (action === 'edit') await editStation(station);
    if (action === 'delete') {
      const ok = await confirmDialog('Remove operator', `<p>Remove <strong>${esc(station.callsign)}</strong> from this event?</p><p>Existing messages remain untouched.</p>`, 'Remove');
      if (ok) {
        await api(`/api/stations/${id}`, {method: 'DELETE'});
        state.stations = state.stations.filter(item => item.id !== id);
        renderOps();
        rebuildCallsignOptions();
        toast(`${station.name || station.callsign} removed from this event.`, 'success');
      }
    }
    if (action === 'unassign') {
      const assignmentId = Number(event.target.dataset.assignmentId);
      const ok = await confirmDialog('Unassign user', `<p>Unassign <strong>${esc(station.name || station.callsign)}</strong> from this event?</p><p>Their global user account and existing messages will remain untouched.</p>`, 'Unassign');
      if (ok) {
        await api(`/api/event-operators/${assignmentId}`, {method: 'DELETE'});
        state.eventUsers = state.eventUsers.filter(item => item.id !== assignmentId);
        state.stations = state.stations.filter(item => item.id !== id);
        renderOps();
        rebuildCallsignOptions();
        toast(`${station.name || station.callsign} unassigned from this event.`, 'success');
      }
    }
  } catch (error) { toast(error.message, 'error'); }
});

$('#station-rows').addEventListener('change', async event => {
  const select = event.target.closest('[data-action="duty"]');
  if (!select) return;
  const id = Number(select.closest('tr').dataset.stationId);
  const station = state.stations.find(item => item.id === id);
  if (!station) return;
  const previous = station.duty_status || (station.on_duty ? 'on_duty' : 'off_duty');
  const dutyStatus = select.value;
  select.disabled = true;
  try {
    const updated = await api(`/api/stations/${id}`, {method: 'PATCH', body: JSON.stringify({duty_status: dutyStatus})});
    const index = state.stations.findIndex(item => item.id === id);
    if (index >= 0) state.stations[index] = updated;
    renderOps();
    toast(`${station.callsign} marked ${dutyStatus.replace('_', ' ')}.`, 'success');
  } catch (error) {
    select.value = previous;
    select.disabled = false;
    toast(error.message, 'error');
  }
});

$('#overdue-quick-list').addEventListener('click', async event => {
  const button = event.target.closest('[data-action="quick-heard"]');
  if (!button) return;
  const id = Number(button.closest('[data-station-id]').dataset.stationId);
  button.disabled = true;
  try {
    const updated = await api(`/api/stations/${id}/heard`, {method: 'POST'});
    const index = state.stations.findIndex(station => station.id === id);
    if (index >= 0) state.stations[index] = updated;
    renderOps();
  } catch (error) {
    button.disabled = false;
    toast(error.message, 'error');
  }
});

async function addStation() {
  const result = await stationDialog('Add operator', null);
  if (!result) return;
  try { await api(`/api/events/${state.event.id}/stations`, {method: 'POST', body: JSON.stringify(result)}); }
  catch (error) { toast(error.message, 'error'); }
}

async function editStation(station) {
  const result = await stationDialog('Edit operator', station);
  if (!result) return;
  try {
    if (!result.on_duty && station.on_duty && station.tactical_call) {
      const clear = await confirmDialog('Station going off duty', `<p>Clear tactical assignment <strong>${esc(station.tactical_call)}</strong> as the station goes off duty?</p>`, 'Clear tactical call');
      if (clear) result.tactical_call = '';
    }
    await api(`/api/stations/${station.id}`, {method: 'PATCH', body: JSON.stringify(result)});
  } catch (error) { toast(error.message, 'error'); }
}

async function stationDialog(title, station) {
  return formDialog(title, `
    <label>Name<input id="dlg-station-name" value="${esc(station?.name || '')}"></label>
    <label>Callsign<input id="dlg-callsign" required value="${esc(station?.callsign || '')}" autocapitalize="characters"></label>
    <label>Tactical call<input id="dlg-tactical" value="${esc(station?.tactical_call || '')}" list="station-control-call-options" autocapitalize="characters">
      <datalist id="station-control-call-options"><option value="CONTROL"><option value="RAYNET CONTROL"></datalist>
    </label>
    <label>Check interval<select id="dlg-interval">${[5,10,15,20,30,45,60,90,120,180,240].map(value => `<option value="${value}">${value} minutes</option>`).join('')}</select></label>
    <label class="checkbox"><input id="dlg-duty" type="checkbox" ${station?.on_duty === false ? '' : 'checked'}> On duty</label>
    <label class="checkbox"><input id="dlg-control" type="checkbox" ${station?.in_control ? 'checked' : ''}> Control</label>
    <label>Notes<textarea id="dlg-notes" rows="3">${esc(station?.notes || '')}</textarea></label>
  `, station ? 'Save operator' : 'Add operator', () => ({
    name: $('#dlg-station-name').value,
    callsign: $('#dlg-callsign').value,
    tactical_call: $('#dlg-tactical').value,
    check_interval_minutes: Number($('#dlg-interval').value),
    on_duty: $('#dlg-duty').checked,
    in_control: $('#dlg-control').checked,
    notes: $('#dlg-notes').value,
  }), () => {
    $('#dlg-interval').value = String(station?.check_interval_minutes || 30);
    const control = $('#dlg-control');
    const tactical = $('#dlg-tactical');
    control.addEventListener('change', () => {
      if (control.checked) {
        tactical.dataset.beforeControl = tactical.value;
        if (!['CONTROL', 'RAYNET CONTROL'].includes(tactical.value.trim().toUpperCase())) tactical.value = 'CONTROL';
      } else if (['CONTROL', 'RAYNET CONTROL'].includes(tactical.value.trim().toUpperCase())) {
        tactical.value = tactical.dataset.beforeControl || '';
      }
    });
  });
}

$('#add-station-button').addEventListener('click', addStation);

const auditFieldLabels = {
  name: 'Name', callsign: 'Callsign', tactical_call: 'Tactical call', message: 'Message',
  direction: 'Direction', priority: 'Priority', on_duty: 'On duty', duty_status: 'Duty status', in_control: 'Control',
  check_interval_minutes: 'Check interval', notes: 'Notes', status: 'Status', control_callsign: 'Control callsign',
};

function auditValue(key, value) {
  if (value === null || value === undefined || value === '') return 'none';
  if (key === 'on_duty' || key === 'in_control' || typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (key === 'duty_status') return String(value).replaceAll('_', ' ').replace(/^./, character => character.toUpperCase());
  if (key === 'check_interval_minutes') return `${value} minutes`;
  return String(value);
}

function auditIdentity(item, before, after) {
  const data = after || before || {};
  if (item.entity_type === 'station') {
    const station = state.stations.find(row => row.id === item.entity_id);
    const source = station || data;
    return [source.name, [source.callsign, source.tactical_call].filter(Boolean).join(' / ')].filter(Boolean).join(' — ') || `Operator record ${item.entity_id}`;
  }
  if (item.entity_type === 'message') {
    const message = state.messages.find(row => row.id === item.entity_id) || data;
    const identity = [message.station_callsign || message.callsign, message.tactical_call].filter(Boolean).join(' / ');
    return identity ? `Message ${item.entity_id} — ${identity}` : `Message ${item.entity_id}`;
  }
  if (item.entity_type === 'event_user') {
    const assignment = state.eventUsers.find(row => row.id === item.entity_id) || data;
    const identity = [assignment.display_name, [assignment.callsign, assignment.tactical_call].filter(Boolean).join(' / ')].filter(Boolean).join(' — ');
    return identity || `Event user assignment ${item.entity_id}`;
  }
  if (item.entity_type === 'event') return data.name ? `Event — ${data.name}` : 'Event settings';
  return `${String(item.entity_type || 'record').replaceAll('_', ' ')}${item.entity_id ? ` ${item.entity_id}` : ''}`;
}

function auditDetails(item, before, after) {
  if (item.action === 'heard') {
    const heardAt = after?.last_heard_at ? formatLocal(after.last_heard_at, {dateStyle:'medium', timeStyle:'medium'}) : 'now';
    return `Recorded as heard at ${heardAt}.`;
  }
  if (item.action === 'identity_change') {
    const oldIdentity = [before?.callsign, before?.tactical_call].filter(Boolean).join(' / ') || 'none';
    const newIdentity = [after?.callsign, after?.tactical_call].filter(Boolean).join(' / ') || 'none';
    return `Logging identity changed from "${oldIdentity}" to "${newIdentity}".`;
  }
  if (item.entity_type === 'message' && item.action === 'create' && after) {
    const identity = [after.station_callsign || after.callsign, after.tactical_call].filter(Boolean).join(' / ');
    return `Logged "${after.message || ''}"${identity ? ` for ${identity}` : ''} as ${after.direction || 'traffic'} (${after.priority || 'routine'}).`;
  }
  if (item.action === 'void') return `Voided record.${after?.reason ? ` Reason: ${after.reason}` : ''}`;
  const ignored = new Set(['id', 'event_id', 'user_id', 'created_at', 'updated_at', 'last_heard_at']);
  const keys = [...new Set([...Object.keys(before || {}), ...Object.keys(after || {})])]
    .filter(key => !ignored.has(key) && auditFieldLabels[key] && auditValue(key, before?.[key]) !== auditValue(key, after?.[key]));
  if (item.action === 'create' || item.action === 'auto_assign') {
    const values = Object.keys(after || {}).filter(key => !ignored.has(key) && auditFieldLabels[key] && after[key] !== '' && after[key] !== null)
      .map(key => `${auditFieldLabels[key]}: ${auditValue(key, after[key])}`);
    return `${item.action === 'auto_assign' ? 'Assigned automatically' : 'Created'}${values.length ? ` — ${values.join('; ')}` : ''}.`;
  }
  if (item.action === 'delete') return `Removed${before ? ` — ${Object.keys(before).filter(key => auditFieldLabels[key] && before[key]).map(key => `${auditFieldLabels[key]}: ${auditValue(key, before[key])}`).join('; ')}` : ''}.`;
  if (keys.length) return keys.map(key => `${auditFieldLabels[key]} changed from "${auditValue(key, before?.[key])}" to "${auditValue(key, after?.[key])}"`).join('; ') + '.';
  if (after?.reason) return after.reason;
  return 'Record updated.';
}

function auditAction(item) {
  if (item.action === 'heard') return 'Marked heard';
  if (item.action === 'create') return item.entity_type === 'message' ? 'Logged' : 'Added';
  if (item.action === 'auto_assign') return 'Auto-assigned';
  if (item.action === 'identity_change') return 'Changed identity';
  return String(item.action || '').replaceAll('_', ' ').replace(/^./, char => char.toUpperCase());
}

async function loadAudit() {
  if (!state.event || !roleAtLeast('controller')) return;
  try {
    const items = await api(`/api/events/${state.event.id}/audit`);
    $('#audit-rows').innerHTML = items.map(item => {
      let before = null, after = null;
      try { before = item.before_json ? JSON.parse(item.before_json) : null; } catch (_) {}
      try { after = item.after_json ? JSON.parse(item.after_json) : null; } catch (_) {}
      const assignment = state.eventUsers.find(row => row.callsign && item.user_callsign && row.callsign.toUpperCase() === item.user_callsign.toUpperCase());
      const operatorIdentity = [item.user_callsign, assignment?.tactical_call].filter(Boolean).join(' / ');
      return `<tr><td><strong>${esc(formatLocal(item.created_at, {hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false}))}</strong><br><span class="muted">${esc(formatLocal(item.created_at, {day:'2-digit', month:'short', year:'numeric'}))}</span></td><td><strong>${esc(item.user_name || 'System')}</strong>${operatorIdentity ? `<br><span class="muted">${esc(operatorIdentity)}</span>` : ''}</td><td>${esc(auditAction(item))}</td><td>${esc(auditIdentity(item, before, after))}</td><td>${esc(auditDetails(item, before, after))}</td></tr>`;
    }).join('');
  } catch (error) { toast(error.message, 'error'); }
}

$('#refresh-audit').addEventListener('click', loadAudit);

async function loadBranding() {
  if (!roleAtLeast('admin')) return;
  try {
    const branding = await api('/api/branding');
    state.branding = branding;
    state.pendingLogoDataUrl = branding.logo_data_url;
    $('#branding-name').value = branding.org_name;
    $('#branding-tagline').value = branding.tagline;
    $('#branding-logo-file').value = '';
    $('#branding-logo-preview').src = branding.logo_data_url || '/static/default-brand-logo.png';
  } catch (error) { toast(error.message, 'error'); }
}

$('#branding-logo-file').addEventListener('change', event => {
  const file = event.target.files[0];
  if (!file) return;
  if (!['image/png', 'image/jpeg', 'image/webp', 'image/gif'].includes(file.type) || file.size > 1_000_000) {
    event.target.value = '';
    toast('Choose a PNG, JPEG, WebP or GIF logo no larger than 1 MB', 'error');
    return;
  }
  const reader = new FileReader();
  reader.addEventListener('load', () => {
    state.pendingLogoDataUrl = String(reader.result);
    $('#branding-logo-preview').src = state.pendingLogoDataUrl;
  });
  reader.readAsDataURL(file);
});

$('#branding-remove-logo').addEventListener('click', () => {
  state.pendingLogoDataUrl = '';
  $('#branding-logo-file').value = '';
  $('#branding-logo-preview').src = '/static/default-brand-logo.png';
});

$('#branding-form').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    const branding = await api('/api/branding', {method: 'PUT', body: JSON.stringify({
      org_name: $('#branding-name').value,
      tagline: $('#branding-tagline').value,
      logo_data_url: state.pendingLogoDataUrl || '',
    })});
    applyBranding(branding);
    state.pendingLogoDataUrl = branding.logo_data_url;
    toast('Branding saved');
  } catch (error) { toast(error.message, 'error'); }
});

async function loadUsers() {
  if (!roleAtLeast('admin')) return;
  try {
    state.users = await api('/api/users');
    $('#user-rows').innerHTML = state.users.map(user => `
      <tr data-user-id="${user.id}"><td><strong>${esc(user.display_name)}</strong></td><td>${esc(user.callsign || '—')}</td><td>${esc(user.username)}</td><td>${esc(user.role)}</td><td>${user.active ? 'Active' : 'Disabled'}</td><td><div class="row-actions"><button data-action="edit">Edit</button></div></td></tr>`).join('');
  } catch (error) { toast(error.message, 'error'); }
}

$('#add-user-button').addEventListener('click', async () => {
  const result = await userDialog('Add user', null);
  if (!result) return;
  try { await api('/api/users', {method: 'POST', body: JSON.stringify(result)}); await loadUsers(); }
  catch (error) { toast(error.message, 'error'); }
});

$('#user-rows').addEventListener('click', async event => {
  if (event.target.dataset.action !== 'edit') return;
  const id = Number(event.target.closest('tr').dataset.userId);
  const user = state.users.find(item => item.id === id);
  const result = await userDialog('Edit user', user);
  if (!result) return;
  if (!result.password) delete result.password;
  try { await api(`/api/users/${id}`, {method: 'PATCH', body: JSON.stringify(result)}); await loadUsers(); }
  catch (error) { toast(error.message, 'error'); }
});

async function userDialog(title, user) {
  return formDialog(title, `
    ${user ? `<p class="muted">Username: <strong>${esc(user.username)}</strong></p>` : '<label>Username<input id="dlg-username" required minlength="3"></label>'}
    <label>Display name<input id="dlg-display" required value="${esc(user?.display_name || '')}"></label>
    <label>Callsign<input id="dlg-callsign" value="${esc(user?.callsign || '')}" autocapitalize="characters"></label>
    <label>Role<select id="dlg-role"><option value="viewer">Viewer</option><option value="operator">Operator</option><option value="controller">Controller</option><option value="admin">Admin</option></select></label>
    <label>${user ? 'New password (leave blank to keep current)' : 'Password'}<input id="dlg-password" type="password" ${user ? '' : 'required minlength="10"'}></label>
    ${user ? `<label class="checkbox"><input id="dlg-active" type="checkbox" ${user.active ? 'checked' : ''}> Account active</label>` : ''}
  `, user ? 'Save user' : 'Add user', () => ({
    ...(user ? {} : {username: $('#dlg-username').value}),
    display_name: $('#dlg-display').value,
    callsign: $('#dlg-callsign').value,
    role: $('#dlg-role').value,
    password: $('#dlg-password').value,
    ...(user ? {active: $('#dlg-active').checked} : {}),
  }), () => { $('#dlg-role').value = user?.role || 'operator'; });
}

async function loadControlCallsigns() {
  try {
    state.controlCallsigns = await api('/api/control-callsigns');
    $('#control-callsign-list').innerHTML = state.controlCallsigns.map(item => `
      <tr><td><strong>${esc(item.callsign)}</strong></td><td><div class="row-actions"><button type="button" data-action="edit" data-preset-id="${item.id}">Edit</button><button type="button" data-action="delete" data-preset-id="${item.id}">Remove</button></div></td></tr>`).join('');
  } catch (error) { toast(error.message, 'error'); }
}

$('#add-control-callsign').addEventListener('click', async () => {
  const result = await formDialog('Add Control callsign', '<label>Callsign<input id="dlg-preset-callsign" required maxlength="50" autocapitalize="characters"></label>', 'Add callsign', () => ({callsign: $('#dlg-preset-callsign').value}));
  if (!result) return;
  try {
    await api('/api/control-callsigns', {method: 'POST', body: JSON.stringify(result)});
    await loadControlCallsigns();
  } catch (error) { toast(error.message, 'error'); }
});

$('#control-callsign-list').addEventListener('click', async event => {
  const button = event.target.closest('[data-preset-id]');
  if (!button) return;
  const item = state.controlCallsigns.find(entry => entry.id === Number(button.dataset.presetId));
  if (!item) return;
  try {
    if (button.dataset.action === 'edit') {
      const result = await formDialog('Edit Control callsign', `<label>Callsign<input id="dlg-preset-callsign" required maxlength="50" value="${esc(item.callsign)}" autocapitalize="characters"></label>`, 'Save callsign', () => ({callsign: $('#dlg-preset-callsign').value}));
      if (!result) return;
      await api(`/api/control-callsigns/${item.id}`, {method: 'PATCH', body: JSON.stringify(result)});
    } else {
      await api(`/api/control-callsigns/${item.id}`, {method: 'DELETE'});
    }
    await loadControlCallsigns();
  } catch (error) { toast(error.message, 'error'); }
});

async function loadAdminEvents() {
  if (!roleAtLeast('admin')) return;
  try {
    state.events = await api('/api/events');
    $('#admin-event-rows').innerHTML = state.events.length ? state.events.map(item => `
      <tr data-event-id="${item.id}">
        <td><strong>${esc(item.name)}</strong></td>
        <td>${esc(item.status === 'closed' ? 'Closed' : 'Active')}</td>
        <td>${esc(formatLocal(item.started_at, {dateStyle: 'medium', timeStyle: 'short'}))}</td>
        <td>${item.message_count}</td>
        <td><div class="row-actions"><button type="button" class="danger-outline" data-action="delete-event">Delete</button></div></td>
      </tr>`).join('') : '<tr><td colspan="5" class="muted">No events have been created.</td></tr>';
  } catch (error) { toast(error.message, 'error'); }
}

$('#admin-event-rows').addEventListener('click', async event => {
  const button = event.target.closest('[data-action="delete-event"]');
  if (!button) return;
  const eventId = Number(button.closest('tr').dataset.eventId);
  const item = state.events.find(entry => entry.id === eventId);
  if (!item) return;
  const result = await formDialog('Delete event', `
    <div class="destructive-warning">
      <p><strong>This permanently deletes ${esc(item.name)}.</strong></p>
      <p>Its message log, operators, welfare records, revisions and event audit history will also be deleted. This cannot be undone.</p>
    </div>
    <label>Type the event name to confirm<input id="dlg-delete-event-name" required autocomplete="off" placeholder="${esc(item.name)}"></label>
  `, 'Delete event', () => ({name: $('#dlg-delete-event-name').value.trim()}));
  if (!result) return;
  if (result.name !== item.name) {
    toast('The event name did not match. Nothing was deleted.', 'error');
    return;
  }
  try {
    await api(`/api/events/${eventId}`, {method: 'DELETE'});
    if (state.event?.id === eventId) state.event = null;
    await loadEvents();
    showTab('admin');
    await loadAdminEvents();
    toast(`${item.name} was deleted.`, 'success');
  } catch (error) { toast(error.message, 'error'); }
});

async function newEvent() {
  state.controlCallsigns = await api('/api/control-callsigns');
  const details = await formDialog('Create event · 1 of 2', `
    <label>Event name<input id="dlg-name" required placeholder="e.g. Portsmouth Marathon 2026"></label>
    <label>Control callsign<input id="dlg-control" list="control-callsign-presets" autocapitalize="characters" placeholder="Select a preset or type another">
      <datalist id="control-callsign-presets">${state.controlCallsigns.map(item => `<option value="${esc(item.callsign)}">`).join('')}</datalist>
    </label>
    ${roleAtLeast('admin') ? '<label id="dlg-save-control-wrap" class="checkbox hidden"><input id="dlg-save-control" type="checkbox"> Save this callsign as a preset</label>' : ''}
    <details class="wizard-event-details"><summary>Event details and radio plan</summary>
      <div class="wizard-event-details-fields">
        <label>Notes<textarea id="dlg-new-event-notes" rows="3" maxlength="4000" placeholder="General event notes"></textarea></label>
        <label>Location information<textarea id="dlg-new-location-details" rows="2" maxlength="1000" placeholder="Venue, access points, control location or directions"></textarea></label>
        <label>Event contacts<textarea id="dlg-new-event-contacts" rows="3" maxlength="2000" placeholder="Names, roles, phone numbers and email addresses"></textarea></label>
        <label>Radio frequency<input id="dlg-new-radio-frequency" maxlength="200" placeholder="e.g. 145.350 MHz or channel name"></label>
        <label>CTCSS tones<input id="dlg-new-ctcss-tones" maxlength="200" placeholder="e.g. 88.5 Hz TX / RX"></label>
        <label>Radio mode<select id="dlg-new-radio-mode"><option value="">Not specified</option><option>FM</option><option>DMR</option><option>D-Star</option><option>Fusion / C4FM</option><option>TETRA</option><option>Other</option></select></label>
        <label id="dlg-new-talk-groups-wrap" class="hidden">DMR talk groups<textarea id="dlg-new-talk-groups" rows="2" maxlength="1000" placeholder="Talk-group numbers and names, one per line"></textarea></label>
      </div>
    </details>
  `, 'Next: operators', () => ({
    name: $('#dlg-name').value, control_callsign: $('#dlg-control').value,
    event_notes: $('#dlg-new-event-notes').value, location_details: $('#dlg-new-location-details').value,
    event_contacts: $('#dlg-new-event-contacts').value, radio_frequency: $('#dlg-new-radio-frequency').value,
    ctcss_tones: $('#dlg-new-ctcss-tones').value, radio_mode: $('#dlg-new-radio-mode').value,
    talk_groups: $('#dlg-new-radio-mode').value === 'DMR' ? $('#dlg-new-talk-groups').value : '', save_control_callsign: $('#dlg-save-control')?.checked || false,
  }), () => {
    const input = $('#dlg-control');
    const wrapper = $('#dlg-save-control-wrap');
    if (wrapper) {
      const checkbox = $('#dlg-save-control');
      const syncSaveOption = () => {
        const value = input.value.trim().toUpperCase();
        const alreadySaved = state.controlCallsigns.some(item => item.callsign.toUpperCase() === value);
        wrapper.classList.toggle('hidden', !value || alreadySaved);
        if (!value || alreadySaved) checkbox.checked = false;
      };
      input.addEventListener('input', syncSaveOption);
      input.addEventListener('change', syncSaveOption);
      syncSaveOption();
    }
    const mode = $('#dlg-new-radio-mode');
    const talkGroups = $('#dlg-new-talk-groups-wrap');
    const syncTalkGroups = () => talkGroups.classList.toggle('hidden', mode.value !== 'DMR');
    mode.addEventListener('change', syncTalkGroups);
    syncTalkGroups();
  });
  if (!details) return;
  try {
    const directory = await api('/api/user-directory');
    const guestOperators = [];
    const assignments = await formDialog('Assign operators · 2 of 2', `
      <p class="muted">Add the permanent users working this event, then add any other operators below.</p>
      <div class="wizard-operator-list">
        ${directory.map((user, index) => {
          return `<div class="wizard-operator" data-user-id="${user.id}">
            <div><strong>${esc(user.display_name)}</strong>${user.callsign ? ` / ${esc(user.callsign)}` : ''}</div>
            <input class="wizard-include" type="checkbox" hidden>
            <label class="checkbox"><input class="wizard-control" type="checkbox"> Control</label>
            <label>Tactical call<input class="wizard-tactical" value="" list="wizard-control-options" autocapitalize="characters" placeholder="Optional"></label>
            <label>Check-in interval<select class="wizard-interval">${[5,10,15,20,30,45,60,90,120,180,240].map(value => `<option value="${value}" ${value === 30 ? 'selected' : ''}>${value} minutes</option>`).join('')}</select></label>
            <button type="button" class="secondary wizard-add-account">Add</button>
          </div>`;
        }).join('')}
        <datalist id="wizard-control-options"><option value="CONTROL"><option value="RAYNET CONTROL"></datalist>
      </div>
      <div id="wizard-guest-list" class="wizard-guest-list"></div>
      <div class="wizard-guests-head"><button id="wizard-show-guest-form" type="button" class="secondary">Add operator</button></div>
      <div id="wizard-guest-form" class="wizard-guest-form hidden">
        <label>Name<input id="wizard-guest-name"></label>
        <label>Callsign<input id="wizard-guest-callsign" autocapitalize="characters"></label>
        <label>Tactical call<input id="wizard-guest-tactical" list="wizard-control-options" autocapitalize="characters" placeholder="Optional"></label>
        <label>Check-in interval<select id="wizard-guest-interval">${[5,10,15,20,30,45,60,90,120,180,240].map(value => `<option value="${value}" ${value === 30 ? 'selected' : ''}>${value} minutes</option>`).join('')}</select></label>
        <label class="checkbox"><input id="wizard-guest-control" type="checkbox"> Control</label>
        <button id="wizard-add-guest" type="button" class="secondary">Add</button>
      </div>
    `, 'Create event', () => ({
      assignments: $$('.wizard-operator').filter(row => row.querySelector('.wizard-include').checked).map(row => ({
        user_id: Number(row.dataset.userId),
        tactical_call: row.querySelector('.wizard-tactical').value,
        in_control: row.querySelector('.wizard-control').checked,
        check_interval_minutes: Number(row.querySelector('.wizard-interval').value),
      })),
      guests: guestOperators,
    }), () => {
      $$('.wizard-operator').forEach(row => {
        const include = row.querySelector('.wizard-include');
        const tactical = row.querySelector('.wizard-tactical');
        const control = row.querySelector('.wizard-control');
        const interval = row.querySelector('.wizard-interval');
        control.addEventListener('change', () => { tactical.value = control.checked ? 'CONTROL' : ''; });
        row.querySelector('.wizard-add-account').addEventListener('click', event => {
          include.checked = true;
          const identity = row.querySelector('div').textContent.trim();
          const summary = document.createElement('div');
          summary.className = 'wizard-account-summary';
          summary.innerHTML = `<span><strong>${esc(identity)}</strong>${tactical.value ? ` · ${esc(tactical.value)}` : ''}${control.checked ? ' · Control' : ''} · ${esc(interval.value)} min check-in</span><button type="button" class="wizard-remove-guest" aria-label="Remove ${esc(identity)}" title="Remove">×</button>`;
          summary.querySelector('button').addEventListener('click', () => {
            include.checked = false;
            row.classList.remove('added');
            summary.remove();
          });
          row.append(summary);
          row.classList.add('added');
        });
      });
      const guestName = $('#wizard-guest-name');
      const guestCallsign = $('#wizard-guest-callsign');
      const guestTactical = $('#wizard-guest-tactical');
      const guestInterval = $('#wizard-guest-interval');
      const guestControl = $('#wizard-guest-control');
      const guestForm = $('#wizard-guest-form');
      const showGuestForm = $('#wizard-show-guest-form');
      const renderGuests = () => {
        $('#wizard-guest-list').innerHTML = guestOperators.map((operator, index) => `
          <div class="wizard-guest-summary">
            <span><strong>${esc(operator.name)}</strong> / ${esc(operator.callsign)}${operator.tactical_call ? ` · ${esc(operator.tactical_call)}` : ''}${operator.in_control ? ' · Control' : ''} · ${esc(operator.check_interval_minutes)} min check-in</span>
            <button type="button" class="wizard-remove-guest" data-guest-index="${index}" aria-label="Remove ${esc(operator.name)}" title="Remove">×</button>
          </div>`).join('');
      };
      guestControl.addEventListener('change', () => { guestTactical.value = guestControl.checked ? 'CONTROL' : ''; });
      showGuestForm.addEventListener('click', () => {
        guestForm.classList.remove('hidden');
        showGuestForm.classList.add('hidden');
        guestName.focus();
      });
      $('#wizard-add-guest').addEventListener('click', () => {
        if (!guestName.value.trim() || !guestCallsign.value.trim()) {
          toast('Enter a name and callsign before adding the operator', 'error');
          (!guestName.value.trim() ? guestName : guestCallsign).focus();
          return;
        }
        guestOperators.push({
          name: guestName.value.trim(), callsign: guestCallsign.value.trim(), tactical_call: guestTactical.value.trim(),
          check_interval_minutes: Number(guestInterval.value), on_duty: true, in_control: guestControl.checked, notes: 'Event operator',
        });
        guestName.value = ''; guestCallsign.value = ''; guestTactical.value = ''; guestInterval.value = '30'; guestControl.checked = false;
        renderGuests();
        guestForm.classList.add('hidden');
        showGuestForm.classList.remove('hidden');
      });
      $('#wizard-guest-list').addEventListener('click', event => {
        const button = event.target.closest('[data-guest-index]');
        if (!button) return;
        guestOperators.splice(Number(button.dataset.guestIndex), 1);
        renderGuests();
      });
    });
    if (!assignments) return;
    if (details.save_control_callsign && details.control_callsign.trim()) {
      await api('/api/control-callsigns', {method: 'POST', body: JSON.stringify({callsign: details.control_callsign})});
    }
    delete details.save_control_callsign;
    const created = await api('/api/events', {method: 'POST', body: JSON.stringify(details)});
    for (const assignment of assignments.assignments) {
      await api(`/api/events/${created.id}/operators`, {method: 'POST', body: JSON.stringify(assignment)});
    }
    for (const guest of assignments.guests) {
      await api(`/api/events/${created.id}/stations`, {method: 'POST', body: JSON.stringify(guest)});
    }
    await loadEvents(created.id);
  } catch (error) { toast(error.message, 'error'); }
}

$('#new-event-button').addEventListener('click', newEvent);
$('#empty-new-event').addEventListener('click', newEvent);

$('#close-event-button').addEventListener('click', async () => {
  if (!state.event) return;
  const closing = state.event.status === 'active';
  const ok = await confirmDialog(closing ? 'Close event' : 'Reopen event', closing
    ? '<p>Closing prevents new traffic from being logged until the event is reopened. Existing data and exports remain available.</p>'
    : '<p>Reopen this event and allow new messages?</p>', closing ? 'Close event' : 'Reopen');
  if (!ok) return;
  try {
    await api(`/api/events/${state.event.id}`, {method: 'PATCH', body: JSON.stringify({status: closing ? 'closed' : 'active'})});
    await loadEvents(state.event.id);
  } catch (error) { toast(error.message, 'error'); }
});

$('#user-button').addEventListener('click', event => {
  const menu = $('#user-menu');
  const rect = event.currentTarget.getBoundingClientRect();
  menu.style.left = `${Math.max(8, rect.right - 190)}px`;
  menu.style.top = `${rect.bottom + 6}px`;
  menu.classList.toggle('hidden');
  event.currentTarget.setAttribute('aria-expanded', String(!menu.classList.contains('hidden')));
  applyPermissions();
});

function renderThemeButton() {
  const dark = document.documentElement.dataset.theme === 'dark';
  $('#theme-button').textContent = dark ? '\u2600' : '\u263e';
  $('#theme-button').title = dark ? 'Use light mode' : 'Use dark mode';
  $('#theme-button').setAttribute('aria-label', dark ? 'Use light mode' : 'Use dark mode');
  $('#theme-button').setAttribute('aria-pressed', String(dark));
}

$('#theme-button').addEventListener('click', () => {
  const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = theme;
  localStorage.setItem(THEME_PREF_KEY, theme);
  renderThemeButton();
});
renderThemeButton();

$('#menu-administration-button').addEventListener('click', () => {
  $('#user-menu').classList.add('hidden');
  $('#user-button').setAttribute('aria-expanded', 'false');
  showTab('admin');
});

$('#about-button').addEventListener('click', async () => {
  $('#user-menu').classList.add('hidden');
  $('#user-button').setAttribute('aria-expanded', 'false');
  $('#modal-cancel').hidden = true;
  await confirmDialog('About Message Logger', `
    <div class="about-details">
      <img src="/static/default-brand-logo.png" alt="" class="about-logo">
      <p><strong>Message Logger</strong> is an operational message, operator and welfare-check logging application for RAYNET deployments.</p>
      <dl>
        <div><dt>Version</dt><dd>${esc(state.bootstrap?.version || '1.0')}</dd></div>
        <div><dt>Author</dt><dd>Mathew Levett (M0NFZ)</dd></div>
        <div><dt>Licence</dt><dd>GNU AGPLv3 or later</dd></div>
      </dl>
      <p><a href="https://github.com/mlevett/Raynet-Logger" target="_blank" rel="noopener noreferrer">View or download the complete source code</a></p>
    </div>`, 'Close');
  $('#modal-cancel').hidden = false;
});

$('#sign-out-button').addEventListener('click', async () => {
  $('#user-menu').classList.add('hidden');
  $('#user-button').setAttribute('aria-expanded', 'false');
  const ok = await confirmDialog('Sign out', `<p>Sign out ${esc(state.user.display_name)}?</p>`, 'Sign out');
  if (!ok) return;
  await api('/api/logout', {method: 'POST'});
  closeWebSocket();
  state.user = null;
  setAuthMode(false);
});

document.addEventListener('click', event => {
  if (!event.target.closest('#user-button') && !event.target.closest('#user-menu')) {
    $('#user-menu').classList.add('hidden');
    $('#user-button').setAttribute('aria-expanded', 'false');
  }
});

function updateExportSummary() {
  const count = $$('#export-form input[name="section"]:checked').length;
  $('#export-selection-summary').textContent = `${count} section${count === 1 ? '' : 's'} selected`;
}

$('#export-form').addEventListener('change', updateExportSummary);
$('#export-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (!state.event) return;
  const sections = $$('#export-form input[name="section"]:checked').map(input => input.value);
  if (!sections.length) return toast('Select at least one section to export.', 'error');
  const format = $('#export-form input[name="format"]:checked').value;
  const button = $('#export-form button[type="submit"]');
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = 'Preparing…';
  try {
    const response = await fetch(`/api/events/${state.event.id}/export/${format}?sections=${encodeURIComponent(sections.join(','))}`);
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.detail || `Export failed (${response.status})`);
    }
    const disposition = response.headers.get('content-disposition') || '';
    const filename = disposition.match(/filename="([^"]+)"/i)?.[1] || `event-export.${format}`;
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast(`${format.toUpperCase()} export downloaded.`, 'success');
  } catch (error) {
    toast(error.message, 'error');
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
});
updateExportSummary();

function connectWebSocket() {
  if (!state.event) return;
  const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${protocol}://${location.host}/ws/events/${state.event.id}`);
  state.websocket = ws;
  ws.addEventListener('open', () => {
    clearInterval(ws._ping);
    ws._ping = setInterval(() => ws.readyState === WebSocket.OPEN && ws.send('ping'), 25000);
  });
  ws.addEventListener('message', event => {
    const packet = JSON.parse(event.data);
    if (packet.type === 'presence') {
      $('#presence').textContent = packet.payload.count;
      $('#presence').title = packet.payload.users.map(u => `${u.display_name}${u.callsign ? ` (${u.callsign})` : ''}`).join('\n');
      return;
    }
    if (packet.type === 'pong') return;
    scheduleSnapshotRefresh();
  });
  ws.addEventListener('close', () => {
    clearInterval(ws._ping);
    clearTimeout(state.reconnectTimer);
    if (state.event) state.reconnectTimer = setTimeout(connectWebSocket, 2500);
  });
}

function closeWebSocket() {
  clearTimeout(state.reconnectTimer);
  const ws = state.websocket;
  state.websocket = null;
  if (ws) {
    clearInterval(ws._ping);
    ws.onclose = null;
    ws.close();
  }
}

function scheduleSnapshotRefresh() {
  clearTimeout(state._snapshotTimer);
  state._snapshotTimer = setTimeout(async () => {
    if (!state.event) return;
    try {
      const snapshot = await api(`/api/events/${state.event.id}/snapshot`);
      state.event = snapshot.event;
      state.stations = snapshot.stations;
      state.eventUsers = snapshot.event_users || [];
      state.messages = snapshot.messages;
      renderEvent(); renderMessages(); renderOps(); rebuildCallsignOptions();
    } catch (_) {}
  }, 120);
}

window.addEventListener('online', () => { toast('Network connection restored'); if (!state.websocket && state.event) connectWebSocket(); });
window.addEventListener('offline', () => { toast('Network connection lost', 'error'); });

function confirmDialog(title, body, confirmText = 'Confirm') {
  return formDialog(title, body, confirmText, () => true);
}

function formDialog(title, body, confirmText, collect, afterOpen = null) {
  return new Promise(resolve => {
    const dialog = $('#modal');
    $('#modal-body').innerHTML = `<h2>${esc(title)}</h2>${body}`;
    $('#modal-confirm').textContent = confirmText;
    dialog.returnValue = '';
    const onClose = () => {
      dialog.removeEventListener('close', onClose);
      if (dialog.returnValue !== 'confirm') return resolve(false);
      try { resolve(collect()); } catch (error) { resolve(false); }
    };
    dialog.addEventListener('close', onClose);
    dialog.showModal();
    afterOpen?.();
    const first = dialog.querySelector('input, textarea, select');
    first?.focus();
  });
}

$('#modal-cancel').addEventListener('click', () => $('#modal').close('cancel'));

$('#modal-form').addEventListener('submit', event => {
  const submitter = event.submitter;
  if (submitter?.value === 'confirm' && !event.currentTarget.checkValidity()) {
    event.preventDefault();
    event.currentTarget.reportValidity();
  }
});

if ('serviceWorker' in navigator) window.addEventListener('load', () => navigator.serviceWorker.register('/service-worker.js?v=72', { updateViaCache: 'none' }).catch(() => {}));

boot().catch(error => toast(error.message, 'error'));
