// Drive the real app.js through the settings form and report what it sent.
//
// The GUI's settings page is plain JavaScript with no unit-testable seam: the
// 变速工具 fields render from one schema section but are registered (and saved)
// through another, and a field missing from that registry is silently read back
// from the stale bootstrap copy -- the edit looks like it reverted.  Only running
// the real script against a real form catches that, so this harness loads
// index.html's element ids and app.js itself.
//
// Usage: node settings_flow.js <bootstrap.json>
// Prints one JSON object describing the save the form performed.
const fs = require('fs');
const path = require('path');
const { installGlobals } = require('./dom.js');

const bootstrapPath = process.argv[2];
const repoRoot = path.resolve(__dirname, '..', '..');
const assets = path.join(repoRoot, 'osu_toolbox', 'gui', 'assets');

const bootstrap = JSON.parse(fs.readFileSync(bootstrapPath, 'utf8'));

const notes = { save_calls: 0, global_payload: null, shell_payload: null, shell_calls: 0, uninstall_calls: 0, toasts: [] };
const api = {
  async get_bootstrap() {
    return JSON.parse(JSON.stringify(bootstrap));
  },
  async save_global_settings(raw) {
    notes.save_calls += 1;
    notes.global_payload = JSON.parse(JSON.stringify(raw));
    return {
      status: 'ok',
      global_settings: { ...bootstrap.global_settings, ...raw },
      message: '设置已保存',
    };
  },
  async save_shell_menu_settings(raw) {
    notes.shell_calls += 1;
    if (notes.reject_shell) return {status: 'error', message: '注册表写入被拒绝'};
    bootstrap.shell_menu_settings = {...raw};
    notes.shell_payload = JSON.parse(JSON.stringify(raw));
    return {
      status: 'ok',
      shell_menu_settings: { ...bootstrap.shell_menu_settings, ...raw },
      message: '右键菜单设置已保存',
    };
  },
  async uninstall_shell_menu() {
    notes.uninstall_calls += 1;
    bootstrap.shell_menu_settings = Object.fromEntries(
      Object.keys(bootstrap.shell_menu_settings).map((key) => [key, false]));
    return {status: 'ok', state: 'disabled', shell_menu_settings: {...bootstrap.shell_menu_settings},
      message: '右键菜单已卸载'};
  },
  async get_shell_menu_state() {
    return { settings: bootstrap.shell_menu_settings, status: bootstrap.shell_menu };
  },
  async reset_all_settings() {
    return {
      status: 'ok',
      global_settings: bootstrap.global_settings,
      shell_menu_settings: bootstrap.shell_menu_settings,
      shell_menu: bootstrap.shell_menu,
      settings: {},
      message: '所有设置已恢复到默认状态',
    };
  },
};

const { document, window } = installGlobals();
window.pywebview.api = api;

// Register every element index.html declares, so getElementById behaves.
const html = fs.readFileSync(path.join(assets, 'index.html'), 'utf8');
for (const match of html.matchAll(/id="([^"]+)"/g)) document.register(match[1]);

const appSource = fs.readFileSync(path.join(assets, 'app.js'), 'utf8');

function idsIn(element) {
  const ids = [];
  if (element.id) ids.push(element.id);
  for (const child of element.children) ids.push(...idsIn(child));
  return ids;
}

function fieldValue(key) {
  const input = document.getElementById(`setting-${key}`);
  if (!input) return null;
  return { type: input.type, value: input.value, checked: input.checked };
}

function setField(key, value) {
  const input = document.getElementById(`setting-${key}`);
  if (!input) throw new Error(`设置字段没有渲染：${key}`);
  if (input.type === 'checkbox') input.checked = Boolean(value);
  else input.value = String(value);
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

(async () => {
  // Evaluate the IIFE exactly as the browser does.
  new Function('window', 'document', `${appSource}\nreturn null;`)(window, document);
  await window.dispatch('pywebviewready');
  await settle();

  const categories = document.getElementById('settings-categories');
  const labels = categories.children.map((tab) => tab.textContent);
  notes.categories = labels;

  const openCategory = async (label) => {
    const tab = categories.children.find((item) => item.textContent === label);
    if (!tab) throw new Error(`找不到设置分类：${label}`);
    await tab.dispatch('click');
    await settle();
  };

  // 1. The 变速工具 category: its fields must survive a save.
  await openCategory('变速工具');
  notes.speed_fields_rendered = idsIn(document.getElementById('settings-content'))
    .filter((id) => id.startsWith('setting-') && !id.startsWith('setting-error-'));
  notes.speed_before = {
    keep_pitch: fieldValue('speed_keep_pitch'),
    file_suffix: fieldValue('speed_file_suffix'),
    difficulty_marker: fieldValue('speed_difficulty_marker'),
  };
  setField('speed_keep_pitch', true);
  setField('speed_file_suffix', '_{num}x');
  setField('speed_difficulty_marker', ' [{num}x]');
  await document.getElementById('settings-form').dispatch('submit');
  await settle();
  notes.speed_after = {
    keep_pitch: fieldValue('speed_keep_pitch'),
    file_suffix: fieldValue('speed_file_suffix'),
    difficulty_marker: fieldValue('speed_difficulty_marker'),
  };
  notes.speed_saved = notes.global_payload;

  // 2. Editing 变速工具 and then leaving the tab must not drop the edit either.
  setField('speed_file_suffix', '__{num}x');
  await openCategory('通用');
  notes.draft_payload = null;
  await openCategory('变速工具');
  notes.speed_revisited = fieldValue('speed_file_suffix');

  // 3. The 通用 category still saves its own fields.
  await openCategory('通用');
  setField('osu_directory', 'D:\\osu!');
  notes.notifications_before = fieldValue('shell_menu_notifications');
  setField('shell_menu_notifications', false);
  await document.getElementById('settings-form').dispatch('submit');
  await settle();
  notes.general_saved = notes.global_payload;
  notes.notifications_after = fieldValue('shell_menu_notifications');
  await openCategory('变速工具');
  await openCategory('通用');
  notes.notifications_revisited = fieldValue('shell_menu_notifications');

  // 4. The 转谱工具 category carries the OD/HP defaults that the 转谱 form and
  //    the right-click menu both fall back to; it saves with the global settings.
  await openCategory('转谱工具');
  notes.chart_fields_rendered = idsIn(document.getElementById('settings-content'))
    .filter((id) => id.startsWith('setting-') && !id.startsWith('setting-error-'));
  notes.chart_before = {
    od: fieldValue('chart_overall_difficulty'),
    hp: fieldValue('chart_hp_drain_rate'),
  };
  setField('chart_overall_difficulty', '9');
  setField('chart_hp_drain_rate', '7.5');
  await document.getElementById('settings-form').dispatch('submit');
  await settle();
  notes.chart_saved = notes.global_payload;
  notes.chart_after = {
    od: fieldValue('chart_overall_difficulty'),
    hp: fieldValue('chart_hp_drain_rate'),
  };

  // Menu edits stay as drafts until Save; unrelated saves never register menus.
  notes.shell_calls_before_edit = notes.shell_calls;
  await openCategory('右键菜单');
  notes.shell_fields = idsIn(document.getElementById('settings-content'))
    .filter((id) => id.startsWith('setting-') && !id.startsWith('setting-error-'));
  notes.shell_save_hidden = document.getElementById('save-settings-button').hidden;
  setField('shell_menu_chart_convert', false);
  await document.getElementById('setting-shell_menu_chart_convert').dispatch('change');
  await settle();
  notes.shell_calls_after_edit = notes.shell_calls;
  await openCategory('通用');
  await openCategory('右键菜单');
  notes.shell_draft_revisited = fieldValue('shell_menu_chart_convert');
  await document.getElementById('settings-form').dispatch('submit');
  await settle();
  notes.shell_saved = notes.shell_payload;
  notes.shell_calls_after_save = notes.shell_calls;
  // An unsaved edit must also disappear when every menu is uninstalled.
  setField('shell_menu_replay_generate', true);
  await document.getElementById('setting-shell_menu_replay_generate').dispatch('change');
  await openCategory('通用');
  await openCategory('右键菜单');
  await document.getElementById('uninstall-shell-menu-button').dispatch('click');
  await document.getElementById('confirm-accept').dispatch('click');
  await settle();
  notes.shell_after_uninstall = Object.fromEntries(
    bootstrap.shell_menu_schema.params.map((spec) => [spec.key, fieldValue(spec.key)]));
  await openCategory('通用');
  await document.getElementById('settings-form').dispatch('submit');
  await settle();
  notes.shell_calls_after_general_save = notes.shell_calls;
  await openCategory('右键菜单');
  notes.reject_shell = true;
  setField('shell_menu_chart_convert', true);
  await document.getElementById('setting-shell_menu_chart_convert').dispatch('change');
  await settle();
  await document.getElementById('settings-form').dispatch('submit');
  await settle();
  notes.shell_after_error = fieldValue('shell_menu_chart_convert');
  notes.shell_stored_after_error = bootstrap.shell_menu_settings.shell_menu_chart_convert;

  notes.toasts = document
    .getElementById('toast-region')
    .children.map((toast) => toast.textContent);

  process.stdout.write(JSON.stringify(notes, null, 2));
})().catch((error) => {
  process.stdout.write(JSON.stringify({ error: String(error && error.stack || error) }));
  process.exitCode = 1;
});
