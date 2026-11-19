'use strict';

const byId = id => document.getElementById(id);
const text = JSON.parse(byId('fleet-text').textContent);
const format = new Intl.NumberFormat(document.documentElement.lang, {maximumFractionDigits: 2});
const selection = new Set();
let scope = null;
let regions = [];
let templates = [];
let currentTemplate = null;
let stateReady = false;
let refreshing = false;

function sameScope(left, right) {
  if (!left || !right) return left === right;
  return ['campaign', 'player', 'day', 'session'].every(key => left[key] === right[key]);
}

async function api(path, method = 'GET', body) {
  const options = {method, cache: 'no-store'};
  if (body) {
    options.headers = {'Content-Type': 'application/json'};
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const payload = await response.json();
  if (!response.ok) throw new Error(text[payload.error] || text.error);
  return payload;
}

function renderRegions() {
  const query = byId('region-filter').value.trim().toLocaleLowerCase();
  const fragment = document.createDocumentFragment();
  for (const region of regions) {
    if (!(`${region.name} #${region.id}`).toLocaleLowerCase().includes(query)) continue;
    const label = document.createElement('label');
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.value = region.id;
    checkbox.checked = selection.has(region.id);
    label.append(checkbox, `${region.name} · #${region.id}`);
    fragment.append(label);
  }
  byId('regions').replaceChildren(fragment);
  byId('region-status').textContent = regions.length ? `${text.selected} · ${selection.size}` : text.map_unavailable;
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const payload = await api('/fleet/state');
    const info = payload.observation;
    const next = info && {campaign: info.campaign, player: info.player, day: info.day, session: info.session};
    const scopeChanged = !sameScope(next, scope);
    const mapChanged = JSON.stringify(payload.regions) !== JSON.stringify(regions);
    if (scopeChanged || mapChanged) {
      selection.clear();
      byId('plan').hidden = true;
    }
    scope = next;
    regions = payload.regions;
    const facts = payload.totals;
    byId('observation').textContent = info
      ? [text.source, info.player, info.date, info.campaign.slice(0, 8), facts ? '' : text.no_totals].filter(Boolean).join(' · ')
      : text.no_observation;
    for (const field of ['ships', 'carrier', 'capital', 'screen', 'submarine', 'fuel_k']) {
      byId(`count-${field}`).textContent = facts ? format.format(facts[field]) : '—';
    }
    if (mapChanged || !stateReady || scopeChanged) renderRegions();
    stateReady = true;
  } catch {
    stateReady = false;
    regions = [];
    scope = null;
    selection.clear();
    byId('observation').textContent = text.error;
    byId('plan').hidden = true;
    document.querySelectorAll('.fleet-counts dd').forEach(cell => {cell.textContent = '—';});
    renderRegions();
  } finally {
    refreshing = false;
  }
}

function selectTemplate(role, value = null) {
  currentTemplate = value?.id ?? null;
  byId('saved').value = currentTemplate || '';
  byId('role').value = role;
  byId('title').value = value?.name ?? byId('role').selectedOptions[0].textContent;
  byId('notes').value = value?.notes ?? '';
  byId('remove').disabled = !currentTemplate;
  for (const button of document.querySelectorAll('[data-role]')) {
    button.setAttribute('aria-pressed', String(button.dataset.role === role));
  }
  byId('plan').hidden = true;
}

async function loadTemplates() {
  ({templates} = await api('/fleet/templates'));
  byId('saved').replaceChildren(new Option(text.template_new, ''), ...templates.map(
    template => new Option(template.name, template.id)));
  byId('saved').value = currentTemplate || '';
}

document.querySelector('.fleet-presets').addEventListener('click', event => {
  const role = event.target.dataset.role;
  if (role) selectTemplate(role);
});
byId('region-filter').addEventListener('input', renderRegions);
byId('regions').addEventListener('change', event => {
  const checkbox = event.target;
  const id = Number(checkbox.value);
  if (checkbox.checked && selection.size < 32) selection.add(id);
  else {
    checkbox.checked = false;
    selection.delete(id);
  }
  byId('region-status').textContent = `${text.selected} · ${selection.size}`;
  byId('plan').hidden = true;
});
byId('saved').addEventListener('change', event => {
  const value = templates.find(template => template.id === event.target.value);
  selectTemplate(value?.role ?? 'custom', value);
});
byId('role').addEventListener('change', event => {
  const role = event.target.value;
  for (const button of document.querySelectorAll('[data-role]')) {
    button.setAttribute('aria-pressed', String(button.dataset.role === role));
  }
  byId('plan').hidden = true;
});
byId('notes').addEventListener('input', () => {byId('plan').hidden = true;});
byId('save').addEventListener('click', async () => {
  if (!byId('title').reportValidity()) return;
  byId('save').disabled = true;
  try {
    const saved = await api('/fleet/templates', 'POST', {
      id: currentTemplate, role: byId('role').value, name: byId('title').value, notes: byId('notes').value,
    });
    currentTemplate = saved.id;
    await loadTemplates();
    selectTemplate(saved.role, saved);
    byId('message').textContent = text.template_saved;
  } catch (error) {
    byId('message').textContent = error.message;
  } finally {
    byId('save').disabled = false;
  }
});
byId('remove').addEventListener('click', async () => {
  if (!currentTemplate || !window.confirm(text.delete_prompt)) return;
  byId('remove').disabled = true;
  try {
    await api('/fleet/templates', 'DELETE', {id: currentTemplate});
    selectTemplate('custom');
    await loadTemplates();
    byId('message').textContent = text.template_deleted;
  } catch (error) {
    byId('message').textContent = error.message;
    byId('remove').disabled = !currentTemplate;
  }
});
byId('fleet-order').addEventListener('submit', async event => {
  event.preventDefault();
  byId('plan').hidden = true;
  if (!stateReady) return;
  const button = event.submitter;
  button.disabled = true;
  const role = byId('role').value;
  const ids = [...selection];
  const notes = byId('notes').value.trim();
  try {
    const plan = await api('/fleet/plan', 'POST', {scope, role, regions: ids});
    if (!sameScope(plan.scope, scope) || role !== byId('role').value
        || ids.join() !== [...selection].join() || notes !== byId('notes').value.trim()) return;
    byId('message').textContent = '';
    byId('plan-context').textContent = scope ? `${scope.player} · ${scope.campaign.slice(0, 8)}` : text.no_scope;
    byId('plan-fit').textContent = text[plan.fit];
    byId('plan-notes').textContent = notes;
    byId('plan-notes').hidden = !notes;
    byId('plan-action').textContent = text[plan.role];
    byId('plan-regions').hidden = !plan.regions.length;
    byId('plan-regions').textContent = `${text.plan_regions} · ${plan.regions.map(item => item.name).join(', ')}`;
    byId('plan').hidden = false;
    byId('plan').scrollIntoView({block: 'nearest'});
  } catch (error) {
    byId('message').textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
byId('refresh').addEventListener('click', refresh);
selectTemplate('escort');
refresh();
loadTemplates().catch(error => {byId('message').textContent = error.message;});
