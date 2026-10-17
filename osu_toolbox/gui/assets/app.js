const App = (() => {
  const state = {
    bootstrap: null,
    tools: new Map(),
    specs: new Map(),
    drafts: new Map(),
    activeToolId: null,
    runId: null,
    runToolId: null,
    runState: 'ready',
    pollTimer: null,
    pollInFlight: false,
    submissionPending: false,
    logLines: [],
    logViewVersion: 0,
    pendingConfirmation: null,
    confirmAction: null,
    settingSpecs: new Map(),
    settingSources: new Map(),
    activeSettingsGroup: null,
    globalSettingsDraft: {},
    shellMenuBusy: false,
    globalSettingsBusy: false,
    shellMenuDraft: {},
  };

  const elements = {};

  function cacheElements() {
    elements.app = document.getElementById('app');
    elements.dashboardPage = document.getElementById('dashboard-page');
    elements.toolPage = document.getElementById('tool-page');
    elements.aboutPage = document.getElementById('about-page');
    elements.settingsPage = document.getElementById('settings-page');
    elements.toolList = document.getElementById('tool-list');
    elements.aboutLink = document.getElementById('about-link');
    elements.settingsLink = document.getElementById('settings-link');
    elements.backButton = document.getElementById('back-button');
    elements.aboutBackButton = document.getElementById('about-back-button');
    elements.settingsBackButton = document.getElementById('settings-back-button');
    elements.settingsCategories = document.getElementById('settings-categories');
    elements.settingsForm = document.getElementById('settings-form');
    elements.settingsContent = document.getElementById('settings-content');
    elements.saveSettingsButton = document.getElementById('save-settings-button');
    elements.resetSettingsButton = document.getElementById('reset-settings-button');
    elements.repairShellMenuButton = document.getElementById('repair-shell-menu-button');
    elements.uninstallShellMenuButton = document.getElementById('uninstall-shell-menu-button');
    elements.form = document.getElementById('tool-form');
    elements.formGroups = document.getElementById('form-groups');
    elements.toolTitle = document.getElementById('tool-title');
    elements.toolDescription = document.getElementById('tool-description');
    elements.startButton = document.getElementById('start-button');
    elements.cancelButton = document.getElementById('cancel-button');
    elements.progressBar = document.getElementById('progress-bar');
    elements.taskStage = document.getElementById('task-stage');
    elements.resultPanel = document.getElementById('result-panel');
    elements.resultContent = document.getElementById('result-content');
    elements.logOutput = document.getElementById('log-output');
    elements.clearLogButton = document.getElementById('clear-log-button');
    elements.headerStatus = document.getElementById('header-status');
    elements.toastRegion = document.getElementById('toast-region');
    elements.confirmDialog = document.getElementById('confirm-dialog');
    elements.confirmTitle = document.getElementById('confirm-title');
    elements.confirmMessage = document.getElementById('confirm-message');
    elements.confirmAccept = document.getElementById('confirm-accept');
    elements.confirmCancel = document.getElementById('confirm-cancel');
    elements.aboutName = document.getElementById('about-name');
    elements.aboutSummary = document.getElementById('about-summary');
    elements.aboutStatus = document.getElementById('about-status');
    elements.aboutAuthor = document.getElementById('about-author');
    elements.aboutQq = document.getElementById('about-qq');
    elements.aboutEmail = document.getElementById('about-email');
    elements.aboutVersion = document.getElementById('about-version');
    elements.projectName = document.getElementById('project-name');
    elements.projectTagline = document.getElementById('project-tagline');
    elements.projectFutureNote = document.getElementById('project-future-note');
    elements.aboutFocus = document.getElementById('about-focus');
  }

  async function initialize() {
    cacheElements();
    bindStaticEvents();
    try {
      state.bootstrap = await window.pywebview.api.get_bootstrap();
      for (const tool of state.bootstrap.tools || []) state.tools.set(tool.id, tool);
      buildDashboard();
      buildSettingsPage();
      populateProjectInfo(state.bootstrap.project);
      if (state.bootstrap.active_run) {
        state.runId = state.bootstrap.active_run.run_id;
        state.runToolId = state.bootstrap.active_run.tool_id;
        state.runState = state.bootstrap.active_run.state;
        if (state.runState === 'running' || state.runState === 'cancelling') startPolling();
      }
      routeFromHash();
      setToolState(state.runState);
      if (state.bootstrap.settings_warning) {
        showToast(state.bootstrap.settings_warning, 'warning');
      }
      elements.app.setAttribute('aria-busy', 'false');
    } catch (error) {
      showToast(`界面初始化失败: ${error}`, 'error', 8000);
    }
  }

  function bindStaticEvents() {
    elements.aboutLink.addEventListener('click', () => navigate('#about'));
    elements.settingsLink.addEventListener('click', () => navigate('#settings'));
    elements.backButton.addEventListener('click', () => navigate(''));
    elements.aboutBackButton.addEventListener('click', () => navigate(''));
    elements.settingsBackButton.addEventListener('click', () => navigate(''));
    elements.settingsForm.addEventListener('submit', saveGlobalSettings);
    elements.resetSettingsButton.addEventListener('click', confirmResetAllSettings);
    if (elements.repairShellMenuButton) elements.repairShellMenuButton.addEventListener('click', repairShellMenu);
    if (elements.uninstallShellMenuButton) elements.uninstallShellMenuButton.addEventListener('click', confirmUninstallShellMenu);
    elements.form.addEventListener('submit', handleSubmit);
    elements.cancelButton.addEventListener('click', cancelCurrentTool);
    elements.clearLogButton.addEventListener('click', clearLog);
    elements.confirmCancel.addEventListener('click', closeConfirm);
    elements.confirmAccept.addEventListener('click', async () => {
      const action = state.confirmAction;
      closeConfirm();
      if (action) await action();
    });
    window.addEventListener('hashchange', routeFromHash);
  }

  function navigate(hash) {
    if (window.location.hash === hash) routeFromHash();
    else window.location.hash = hash;
  }

  function routeFromHash() {
    const hash = window.location.hash;
    if (hash === '#about') {
      showPage('about');
      return;
    }
    if (hash === '#settings') {
      showPage('settings');
      return;
    }
    if (hash.startsWith('#tool/')) {
      const toolId = decodeURIComponent(hash.slice(6));
      if (state.tools.has(toolId)) {
        openTool(toolId);
        showPage('tool');
        return;
      }
    }
    showPage('dashboard');
  }

  function showPage(name) {
    elements.dashboardPage.hidden = name !== 'dashboard';
    elements.toolPage.hidden = name !== 'tool';
    elements.aboutPage.hidden = name !== 'about';
    elements.settingsPage.hidden = name !== 'settings';
    if (name === 'tool') {
      const firstInput = elements.form.querySelector('input, select, button');
      if (firstInput) firstInput.focus();
    }
    if (name === 'about') elements.aboutBackButton.focus();
    if (name === 'settings') elements.settingsBackButton.focus();
  }

  function buildDashboard() {
    elements.toolList.textContent = '';
    for (const tool of state.tools.values()) {
      const card = document.createElement('button');
      card.className = 'tool-card';
      card.type = 'button';
      card.dataset.toolId = tool.id;
      card.addEventListener('click', () => navigate(`#tool/${encodeURIComponent(tool.id)}`));

      const category = document.createElement('span');
      category.className = 'tool-card-kicker';
      category.textContent = tool.category;
      const title = document.createElement('span');
      title.className = 'tool-card-title';
      title.textContent = tool.title;
      const description = document.createElement('span');
      description.className = 'tool-card-description';
      description.textContent = tool.description;
      const footer = document.createElement('span');
      footer.className = 'tool-card-footer';
      const dot = document.createElement('span');
      dot.className = 'status-dot';
      dot.dataset.role = 'status-dot';
      dot.dataset.state = 'ready';
      const status = document.createElement('span');
      status.dataset.role = 'status-text';
      status.textContent = '就绪';
      const action = document.createElement('span');
      action.className = 'tool-card-action';
      action.textContent = '打开配置';
      footer.append(dot, status, action);
      card.append(category, title, description, footer);
      elements.toolList.appendChild(card);
    }
  }

  function buildSettingsPage() {
    const globalSchema = state.bootstrap.global_settings_schema || {groups: [], params: []};
    const shellSchema = state.bootstrap.shell_menu_schema || {groups: [], params: []};
    const speedSchema = globalSchema.speed_tool || {groups: [], params: []};
    const chartSchema = globalSchema.chart_tool || {groups: [], params: []};
    const schema = {
      groups: [
        ...(globalSchema.groups || []),
        ...(speedSchema.groups || []),
        ...(chartSchema.groups || []),
        ...(shellSchema.groups || []),
      ],
      params: [
        ...(globalSchema.params || []),
        ...(speedSchema.params || []),
        ...(chartSchema.params || []),
        ...(shellSchema.params || []),
      ],
    };
    const values = {
      ...(state.bootstrap.global_settings || {}),
      ...(state.bootstrap.shell_menu_settings || {}),
      ...(state.globalSettingsDraft || {}),
      ...(state.shellMenuDraft || {}),
    };
    const groups = (schema.groups || []).filter((group) =>
      (schema.params || []).some((spec) => spec.group === group.key),
    );
    state.settingSpecs.clear();
    state.settingSources.clear();
    // Every editable field must be registered, not just the ones from the first
    // schema: a field that renders but is missing here is read from the stale
    // bootstrap copy on save, so the user's edit looks like it reverted.  The
    // 变速工具 and 转谱工具 fields are saved with the global settings even though
    // their schemas arrive in their own sections.
    for (const source of [
      ['global', globalSchema.params],
      ['global', speedSchema.params],
      ['global', chartSchema.params],
      ['shell', shellSchema.params],
    ]) {
      for (const spec of source[1] || []) {
        state.settingSpecs.set(spec.key, spec);
        state.settingSources.set(spec.key, source[0]);
      }
    }
    if (!groups.some((group) => group.key === state.activeSettingsGroup)) {
      state.activeSettingsGroup = groups.length ? groups[0].key : null;
    }
    elements.settingsCategories.textContent = '';
    for (const group of groups) {
      const tab = document.createElement('button');
      tab.type = 'button';
      tab.className = 'settings-category';
      tab.textContent = group.label;
      tab.setAttribute('role', 'tab');
      tab.setAttribute('aria-selected', String(group.key === state.activeSettingsGroup));
      tab.addEventListener('click', () => {
        state.globalSettingsDraft = collectGlobalSettings();
        state.shellMenuDraft = collectShellMenuSettings();
        state.activeSettingsGroup = group.key;
        buildSettingsPage();
      });
      elements.settingsCategories.appendChild(tab);
    }
    elements.settingsContent.textContent = '';
    const active = groups.find((group) => group.key === state.activeSettingsGroup);
    const shellActionsVisible = active?.key === 'shell_menu';
    elements.saveSettingsButton.hidden = false;
    elements.repairShellMenuButton.hidden = !shellActionsVisible || state.bootstrap.shell_menu?.state !== 'needs_repair';
    elements.uninstallShellMenuButton.hidden = !shellActionsVisible;
    if (!active) return;
    const section = document.createElement('section');
    section.className = 'settings-section';
    const heading = document.createElement('h2');
    heading.textContent = active.label;
    const grid = document.createElement('div');
    grid.className = 'field-grid';
    for (const spec of schema.params.filter((item) => item.group === active.key)) {
      grid.appendChild(buildSettingField(spec, values[spec.key]));
    }
    section.append(heading, grid);
    if (active.key === 'shell_menu') appendShellMenuStatus(section);
    elements.settingsContent.appendChild(section);
    setShellMenuBusy(state.shellMenuBusy);
  }

  function appendShellMenuStatus(section) {
    const status = state.bootstrap.shell_menu || {};
    const panel = document.createElement('div');
    panel.className = 'settings-status-panel';
    const heading = document.createElement('h3');
    heading.textContent = '当前用户注册状态';
    const message = document.createElement('p');
    message.textContent = `${status.scope || '当前用户'}：${status.message || '尚未检查'}`;
    panel.appendChild(heading);
    panel.appendChild(message);
    const hint = document.createElement('p');
    hint.textContent = '修改勾选后，点击“保存”统一应用。';
    panel.appendChild(hint);
    section.appendChild(panel);
  }

  function buildSettingField(spec, value) {
    const field = document.createElement('div');
    field.className = 'field';
    const label = document.createElement('label');
    label.htmlFor = `setting-${spec.key}`;
    label.textContent = spec.label;
    label.title = spec.help;
    const control = document.createElement('div');
    control.className = 'field-control';
    const input = document.createElement('input');
    input.type = spec.kind === 'bool' ? 'checkbox' : 'text';
    input.id = `setting-${spec.key}`;
    input.name = spec.key;
    input.title = spec.help;
    if (spec.kind === 'bool') input.checked = Boolean(value);
    else input.value = value === null || value === undefined ? '' : String(value);
    if (state.settingSources.get(spec.key) === 'shell') {
      input.addEventListener('change', () => { state.shellMenuDraft = collectShellMenuSettings(); });
    }
    control.appendChild(input);
    if (spec.dialog_mode) {
      const browse = document.createElement('button');
      browse.type = 'button';
      browse.className = 'button button-browse';
      browse.textContent = '浏览';
      browse.addEventListener('click', () => browseGlobalPath(spec.key));
      control.appendChild(browse);
    }
    const error = document.createElement('div');
    error.className = 'field-error';
    error.id = `setting-error-${spec.key}`;
    field.append(label, control, error);
    return field;
  }

  function collectSettings(source) {
    const values = {
      ...(source === 'shell'
        ? (state.bootstrap.shell_menu_settings || {})
        : (state.bootstrap.global_settings || {})),
      ...(source === 'shell' ? state.shellMenuDraft : state.globalSettingsDraft),
    };
    for (const key of state.settingSpecs.keys()) {
      if (state.settingSources.get(key) !== source) continue;
      const input = document.getElementById(`setting-${key}`);
      if (input) values[key] = input.type === 'checkbox' ? input.checked : input.value;
    }
    return values;
  }

  function collectGlobalSettings() {
    return collectSettings('global');
  }

  function collectShellMenuSettings() {
    return collectSettings('shell');
  }

  async function browseGlobalPath(key) {
    const input = document.getElementById(`setting-${key}`);
    const before = input?.value;
    try {
      const result = await window.pywebview.api.pick_global_path(key);
      if (!result.ok) {
        showToast(result.message, 'error');
        return;
      }
      if (input && input === document.getElementById(`setting-${key}`) && input.value === before && result.path) {
        input.value = result.path;
      }
    } catch (error) {
      showToast(`选择目录失败: ${error}`, 'error');
    }
  }

  async function saveGlobalSettings(event) {
    event.preventDefault();
    if (state.shellMenuBusy || state.globalSettingsBusy) return;
    if (state.activeSettingsGroup === 'shell_menu') {
      await saveShellMenuSettings();
      return;
    }
    const before = collectGlobalSettings();
    state.globalSettingsBusy = true;
    setShellMenuBusy(true);
    elements.saveSettingsButton.textContent = '正在保存…';
    try {
      const result = await window.pywebview.api.save_global_settings(before);
      if (result.status === 'error') {
        if (result.field) {
          const error = document.getElementById(`setting-error-${result.field}`);
          if (error) error.textContent = result.message;
        }
        showToast(result.message, 'error');
        return;
      }
      const current = collectGlobalSettings();
      state.bootstrap.global_settings = result.global_settings || {};
      state.globalSettingsDraft = Object.fromEntries(Object.entries(current)
        .filter(([key, value]) => JSON.stringify(value) !== JSON.stringify(before[key])));
      buildSettingsPage();
      showToast(result.message || '设置已保存', result.status === 'warning' ? 'warning' : 'success');
    } catch (error) {
      showToast(`保存设置失败: ${error}`, 'error');
    } finally {
      state.globalSettingsBusy = false;
      elements.saveSettingsButton.textContent = '保存';
      setShellMenuBusy(state.shellMenuBusy);
    }
  }

  function setShellMenuBusy(busy) {
    busy = busy || state.globalSettingsBusy;
    for (const spec of state.bootstrap.shell_menu_schema?.params || []) {
      const input = document.getElementById(`setting-${spec.key}`);
      if (input) input.disabled = busy;
    }
    elements.repairShellMenuButton.disabled = busy;
    elements.uninstallShellMenuButton.disabled = busy;
    elements.resetSettingsButton.disabled = busy;
    elements.saveSettingsButton.disabled = busy;
  }

  async function updateShellMenu(request, clearDraft = false) {
    if (state.shellMenuBusy || state.globalSettingsBusy) return;
    state.shellMenuBusy = true;
    setShellMenuBusy(true);
    try {
      const result = await request();
      if (result.status === 'error') {
        const current = await window.pywebview.api.get_shell_menu_state();
        state.bootstrap.shell_menu_settings = current.settings;
        state.bootstrap.shell_menu = current.status;
      } else {
        if (clearDraft) state.shellMenuDraft = {};
        state.bootstrap.shell_menu_settings = result.shell_menu_settings || state.bootstrap.shell_menu_settings;
        state.bootstrap.shell_menu = result;
      }
      showToast(result.message || '右键菜单设置已保存',
        result.status === 'error' ? 'error' : result.status === 'warning' ? 'warning' : 'success');
    } catch (error) {
      showToast(`右键菜单操作失败：${error}`, 'error');
    } finally {
      state.shellMenuBusy = false;
      state.globalSettingsDraft = collectGlobalSettings();
      buildSettingsPage();
    }
  }

  async function saveShellMenuSettings() {
    const settings = collectShellMenuSettings();
    state.shellMenuDraft = settings;
    await updateShellMenu(() => window.pywebview.api.save_shell_menu_settings(settings), true);
  }

  function confirmResetAllSettings() {
    openConfirm({
      title: '恢复到默认',
      message: '你确认要重置所有设置吗？',
      confirm_label: '确认重置',
    }, resetAllSettings);
  }

  async function resetAllSettings() {
    const result = await window.pywebview.api.reset_all_settings();
    if (result.status === 'error') {
      showToast(result.message, 'error');
      return;
    }
    state.bootstrap.global_settings = result.global_settings || {};
    state.bootstrap.shell_menu_settings = result.shell_menu_settings || {};
    state.bootstrap.shell_menu = result.shell_menu || state.bootstrap.shell_menu;
    state.bootstrap.settings = result.settings || {};
    state.globalSettingsDraft = {};
    state.shellMenuDraft = {};
    state.drafts.clear();
    clearLog();
    state.activeToolId = null;
    state.specs.clear();
    elements.formGroups.textContent = '';
    buildSettingsPage();
    showToast(result.message, result.status === 'warning' ? 'warning' : 'success');
  }

  async function repairShellMenu() {
    await updateShellMenu(() => window.pywebview.api.repair_shell_menu());
  }

  function confirmUninstallShellMenu() {
    openConfirm({
      title: '卸载右键菜单',
      message: '卸载工具箱的全部右键菜单？',
      confirm_label: '确认卸载',
    }, uninstallShellMenu);
  }

  async function uninstallShellMenu() {
    await updateShellMenu(() => window.pywebview.api.uninstall_shell_menu(), true);
  }

  function openTool(toolId) {
    clearLog();
    if (state.activeToolId === toolId && elements.formGroups.childElementCount) {
      setToolState(state.runState);
      return;
    }
    if (state.activeToolId && state.activeToolId !== toolId) {
      state.drafts.set(state.activeToolId, collectForm());
    }
    const tool = state.tools.get(toolId);
    state.activeToolId = toolId;
    state.specs.clear();
    elements.formGroups.textContent = '';
    elements.toolTitle.textContent = tool.title;
    elements.toolDescription.textContent = tool.description;
    elements.startButton.textContent = tool.action_label;
    const defaults = Object.fromEntries(tool.params.map((spec) => [spec.key, spec.default]));
    const settings = (state.bootstrap.settings || {})[toolId] || {};
    const values = {...defaults, ...settings, ...(state.drafts.get(toolId) || {})};
    for (const spec of tool.params) state.specs.set(spec.key, spec);
    for (const group of tool.groups) {
      const specs = tool.params.filter((spec) => spec.group === group.key);
      if (!specs.length) continue;
      elements.formGroups.appendChild(
        group.collapsible
          ? buildCollapsibleGroup(group, specs, values)
          : buildGroup(group, specs, values),
      );
    }
    setToolState(state.runState);
  }

  function populateProjectInfo(project) {
    const info = project || {};
    document.title = info.name || '--';
    elements.projectName.textContent = info.name || '--';
    elements.projectTagline.textContent = info.tagline || '--';
    elements.projectFutureNote.textContent = info.future_note || '--';
    elements.aboutName.textContent = info.name || '--';
    elements.aboutSummary.textContent = info.tagline || info.summary || '--';
    elements.aboutFocus.textContent = info.focus || '--';
    elements.aboutStatus.textContent = info.status || '--';
    elements.aboutAuthor.textContent = info.author_name || '--';
    elements.aboutQq.textContent = info.qq || '--';
    elements.aboutVersion.textContent = info.version || '--';
    const email = info.author_email || '';
    elements.aboutEmail.textContent = email || '--';
    if (email) elements.aboutEmail.href = `mailto:${encodeURIComponent(email)}`;
    else elements.aboutEmail.removeAttribute('href');
  }

  function buildGroup(group, specs, values) {
    const section = document.createElement('section');
    section.className = 'form-section';
    section.setAttribute('aria-labelledby', `group-${group.key}`);
    const title = document.createElement('h2');
    title.id = `group-${group.key}`;
    title.textContent = `* ${group.label}`;
    section.append(title, buildFields(specs, values, group));
    return section;
  }

  function buildCollapsibleGroup(group, specs, values) {
    const details = document.createElement('details');
    details.className = 'form-section form-section-collapsible';
    const summary = document.createElement('summary');
    // Groups whose fields already have working defaults carry no prefix at all:
    // only a group that must be filled is marked, with ``*``.
    summary.textContent = group.label;
    details.append(summary, buildFields(specs, values, group));
    return details;
  }

  // Groups a caller declares as collapsible are the ones whose defaults already
  // suit most runs, so their fields do not repeat the "optional" hint; the group
  // heading carries it instead.  A plain field keeps no marker at all: the hint
  // states what leaving it alone does, and only a field that must be filled is
  // marked, with ``*``.
  function fieldLabel(spec, group) {
    if (group && group.collapsible) return spec.label;
    return spec.required
      ? `* ${spec.label}`
      : `${spec.label}（可选，留空用默认值）`;
  }

  function buildFields(specs, values, group) {
    const grid = document.createElement('div');
    grid.className = 'field-grid';
    for (const spec of specs) grid.appendChild(buildField(spec, values[spec.key], group));
    return grid;
  }

  function buildField(spec, value, group) {
    const field = document.createElement('div');
    field.className = spec.kind === 'bool' ? 'field field-check' : 'field';
    field.dataset.key = spec.key;

    if (spec.kind === 'bool') {
      const label = document.createElement('label');
      label.className = 'check-label';
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.id = `field-${spec.key}`;
      input.checked = Boolean(value);
      const text = document.createElement('span');
      text.textContent = fieldLabel(spec, group);
      label.append(input, text);
      label.title = spec.help;
      field.append(label, buildError(spec.key));
      return field;
    }

    const label = document.createElement('label');
    label.htmlFor = `field-${spec.key}`;
    label.textContent = fieldLabel(spec, group);
    label.title = spec.help;
    field.appendChild(label);

    const control = document.createElement('div');
    control.className = 'field-control';
    let input;
    if (spec.kind === 'choice') {
      input = document.createElement('select');
      input.multiple = Boolean(spec.multiple);
      for (const choice of spec.choices) {
        const option = document.createElement('option');
        option.value = choice.key;
        option.textContent = choice.label;
        if (spec.multiple && Array.isArray(value)) {
          option.selected = value.some((item) => sameValue(choice.value, item));
        } else if (sameValue(choice.value, value)) {
          option.selected = true;
        }
        input.appendChild(option);
      }
    } else {
      input = document.createElement('input');
      input.type = spec.kind.includes('int') || spec.kind.includes('float') ? 'number' : 'text';
      input.value = value === null || value === undefined ? '' : String(value);
      if (spec.kind.includes('float')) input.step = 'any';
      if (spec.minimum !== null) input.min = String(spec.minimum);
      if (spec.maximum !== null) input.max = String(spec.maximum);
      if (spec.required) input.required = true;
      if (spec.placeholder) input.placeholder = spec.placeholder;
    }
    input.id = `field-${spec.key}`;
    input.name = spec.key;
    input.title = spec.help;
    control.appendChild(input);

    if (spec.unit) {
      const unit = document.createElement('span');
      unit.className = 'field-unit';
      unit.textContent = spec.unit;
      control.appendChild(unit);
    }
    if (spec.dialog_mode) {
      const browse = document.createElement('button');
      browse.type = 'button';
      browse.className = 'button button-browse';
      browse.textContent = '浏览';
      browse.addEventListener('click', () => browsePath(spec.key));
      control.appendChild(browse);
    }
    if (spec.dialog_mode === 'open_file') {
      input.addEventListener('change', () => prefillFromPath(spec.key));
    }
    field.append(control, buildError(spec.key));
    return field;
  }

  function buildError(key) {
    const error = document.createElement('div');
    error.className = 'field-error';
    error.id = `error-${key}`;
    return error;
  }

  function sameValue(left, right) {
    return left === right || (left === null && right === undefined);
  }

  // A picker result becomes the field's value.  The list check must not depend
  // on the spec lookup: when ``state.specs`` has no entry for the key the old
  // expression fell through to assigning the raw array, and a JS array assigned
  // to ``input.value`` stringifies with *no separator* -- five chosen folders
  // collapsed into one impossible path like
  // ``D:\maps\aD:\maps\bD:\maps\c``.  Which is what "路径不存在" was reporting.
  function dialogPathValue(result) {
    if (Array.isArray(result)) return result.filter((item) => item).join('\n');
    return result === null || result === undefined ? '' : String(result);
  }

  async function browsePath(key) {
    const toolId = state.activeToolId;
    const input = document.getElementById(`field-${key}`);
    const before = input?.value;
    try {
      const result = await window.pywebview.api.pick_path(toolId, key);
      if (toolId !== state.activeToolId || input !== document.getElementById(`field-${key}`)) return;
      if (!result.ok) {
        showToast(result.message, 'error');
        return;
      }
      if (input.value !== before) return;
      if (result.path && (Array.isArray(result.path) ? result.path.length : true)) {
        input.value = dialogPathValue(result.path);
        clearFieldError(key);
        await prefillFromPath(key);
      }
    } catch (error) {
      showToast(`选择文件失败: ${error}`, 'error');
    }
  }

  async function prefillFromPath(key) {
    const input = document.getElementById(`field-${key}`);
    if (!input || !input.value) return;
    const toolId = state.activeToolId;
    const selectedPath = input.value;
    const before = collectForm();
    try {
      const result = await window.pywebview.api.prefill_tool(toolId, before, key);
      if (toolId !== state.activeToolId || input !== document.getElementById(`field-${key}`) || input.value !== selectedPath) return;
      if (result.status === 'error') {
        if (result.field) setFieldError(result.field, result.message);
        showToast(result.message, 'error');
        return;
      }
      const current = collectForm();
      for (const [paramKey, value] of Object.entries(result.params || {})) {
        if (JSON.stringify(current[paramKey]) !== JSON.stringify(before[paramKey])) continue;
        setFieldValue(paramKey, value);
        clearFieldError(paramKey);
      }
      for (const warning of result.warnings || []) showToast(warning, 'warning');
    } catch (error) {
      showToast(`读取文件信息失败: ${error}`, 'error');
    }
  }

  function setFieldValue(key, value) {
    const spec = state.specs.get(key);
    const input = document.getElementById(`field-${key}`);
    if (!spec || !input) return;
    if (spec.kind === 'bool') {
      input.checked = Boolean(value);
      return;
    }
    if (spec.kind === 'choice') {
      const values = spec.multiple
        ? (Array.isArray(value) ? value : [])
        : [value];
      for (let index = 0; index < spec.choices.length; index += 1) {
        const choice = spec.choices[index];
        input.options[index].selected = values.some((item) => sameValue(choice.value, item));
      }
      return;
    }
    input.value = value === null || value === undefined ? '' : String(value);
  }

  function collectForm() {
    const params = {};
    for (const [key, spec] of state.specs.entries()) {
      const input = document.getElementById(`field-${key}`);
      if (!input) continue;
      if (spec.kind === 'bool') params[key] = input.checked;
      else if (spec.kind === 'choice' && spec.multiple) {
        const selected = Array.from(input.selectedOptions).map((option) => option.value);
        params[key] = selected;
      } else if (spec.multiple) {
        params[key] = input.value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
      } else params[key] = input.value;
    }
    return params;
  }

  async function handleSubmit(event) {
    event.preventDefault();
    clearErrors();
    await submitTool(collectForm(), null);
  }

  async function submitTool(params, confirmationToken, toolId = state.activeToolId) {
    if (state.submissionPending || state.runState === 'running' || state.runState === 'cancelling') return;
    state.submissionPending = true;
    setToolState(state.runState);
    elements.taskStage.textContent = '正在检查输入，请稍候';
    try {
      const result = await window.pywebview.api.start_tool(toolId, params, confirmationToken);
      if (result.status === 'error') {
        if (result.field && toolId === state.activeToolId) setFieldError(result.field, result.message);
        showToast(result.message, 'error');
        return;
      }
      if (result.status === 'busy') {
        showToast(result.message, 'warning');
        return;
      }
      if (result.status === 'confirmation_required') {
        if (toolId !== state.activeToolId) {
          showToast('输入检查完成，请返回原工具后重新提交', 'info');
          return;
        }
        state.pendingConfirmation = {toolId, params, token: result.confirmation_token};
        openConfirm(result.confirmation, () => submitTool(params, result.confirmation_token, toolId));
        return;
      }
      if (result.status === 'started') {
        state.pendingConfirmation = null;
        state.runId = result.run_id;
        state.runToolId = toolId;
        state.runState = 'running';
        elements.progressBar.style.width = '0%';
        elements.taskStage.textContent = '正在处理';
        elements.resultPanel.hidden = true;
        elements.resultContent.textContent = '';
        startPolling();
      }
    } catch (error) {
      showToast(`提交任务失败: ${error}`, 'error');
    } finally {
      state.submissionPending = false;
      if (state.runState !== 'running' && state.runState !== 'cancelling') {
        elements.taskStage.textContent = '等待开始';
      }
      setToolState(state.runState);
    }
  }

  async function cancelCurrentTool() {
    if (!state.runId || (state.runState !== 'running' && state.runState !== 'cancelling')) return;
    elements.cancelButton.disabled = true;
    try {
      const result = await window.pywebview.api.cancel_tool(state.runId);
      if (result.status === 'error') {
        elements.cancelButton.disabled = false;
        showToast(result.message, 'error');
        return;
      }
      setToolState('cancelling');
      elements.taskStage.textContent = '正在取消并清理临时文件';
    } catch (error) {
      elements.cancelButton.disabled = false;
      showToast(`取消任务失败: ${error}`, 'error');
    }
  }

  function startPolling() {
    if (state.pollTimer) return;
    state.pollTimer = window.setInterval(pollEvents, 250);
  }

  async function pollEvents() {
    if (state.pollInFlight || !state.runId) return;
    const runId = state.runId;
    const logViewVersion = state.logViewVersion;
    state.pollInFlight = true;
    try {
      const batch = await window.pywebview.api.drain_events(runId);
      if (runId !== state.runId) return;
      const events = (batch.events || []).filter((event) => event.run_id === runId);
      if (logViewVersion === state.logViewVersion &&
          !elements.toolPage.hidden && state.activeToolId === state.runToolId) {
        appendLogLines(events.filter((event) => event.type === 'log').map((event) => event.payload));
      }
      for (const event of events) {
        if (event.type !== 'log') handleEvent(event);
      }
    } catch (error) {
      showToast(`状态读取失败: ${error}`, 'error');
    } finally {
      state.pollInFlight = false;
    }
  }

  function handleEvent(event) {
    if (event.type === 'warning') showToast(event.payload, 'warning', 6500);
    if (event.type === 'progress') {
      const percent = Math.round(event.payload.fraction * 100);
      elements.progressBar.style.width = `${percent}%`;
      elements.taskStage.textContent = `正在处理 ${percent}%`;
    }
    if (event.type === 'result') renderResult(event.payload);
    if (event.type === 'state') {
      const next = event.payload.state;
      setToolState(next);
      if (next === 'done') {
        elements.progressBar.style.width = '100%';
        elements.taskStage.textContent = '任务完成';
        showToast(event.payload.message, event.payload.has_warnings ? 'warning' : 'success');
        stopPolling();
      } else if (next === 'error') {
        elements.taskStage.textContent = '任务失败';
        showToast(event.payload.message, 'error', 9000);
        stopPolling();
      } else if (next === 'cancelling') {
        elements.taskStage.textContent = '正在取消并清理临时文件';
      } else if (next === 'cancelled') {
        elements.taskStage.textContent = '任务已取消';
        showToast(event.payload.message, 'info');
        stopPolling();
      }
    }
  }

  function renderResult(result) {
    elements.resultContent.textContent = '';
    const kind = result.kind;
    const data = result.data;
    if (kind === 'text') {
      const text = document.createElement('p');
      text.textContent = String(data ?? '');
      elements.resultContent.appendChild(text);
    } else if (kind === 'key_value') {
      const list = document.createElement('dl');
      list.className = 'result-key-value';
      for (const [key, value] of Object.entries(data || {})) {
        const row = document.createElement('div');
        const term = document.createElement('dt');
        term.textContent = key;
        const detail = document.createElement('dd');
        detail.textContent = String(value ?? '');
        row.append(term, detail);
        list.appendChild(row);
      }
      elements.resultContent.appendChild(list);
    } else if (kind === 'table') {
      const table = document.createElement('table');
      const head = document.createElement('thead');
      const headRow = document.createElement('tr');
      for (const column of data.columns || []) {
        const cell = document.createElement('th');
        cell.textContent = String(column);
        headRow.appendChild(cell);
      }
      head.appendChild(headRow);
      const body = document.createElement('tbody');
      for (const values of data.rows || []) {
        const row = document.createElement('tr');
        for (const value of values) {
          const cell = document.createElement('td');
          cell.textContent = String(value ?? '');
          row.appendChild(cell);
        }
        body.appendChild(row);
      }
      table.append(head, body);
      elements.resultContent.appendChild(table);
    } else if (kind === 'file') {
      const file = document.createElement('div');
      file.className = 'result-file';
      const label = document.createElement('strong');
      label.textContent = data.label || data.name || '文件';
      const path = document.createElement('code');
      path.textContent = data.path || '';
      file.append(label, path);
      elements.resultContent.appendChild(file);
    }
    elements.resultPanel.hidden = false;
  }

  function clearLog() {
    state.logViewVersion += 1;
    state.logLines = [];
    elements.logOutput.textContent = '';
    elements.logOutput.scrollTop = 0;
  }

  function appendLogLines(lines) {
    if (!lines.length) return;
    const output = elements.logOutput;
    const atBottom = output.scrollHeight - output.scrollTop - output.clientHeight <= 48;
    state.logLines.push(...lines.map(String));
    state.logLines = state.logLines.slice(-1000);
    output.textContent = state.logLines.join('\n');
    if (atBottom) output.scrollTop = output.scrollHeight;
  }

  function stopPolling() {
    if (state.pollTimer) window.clearInterval(state.pollTimer);
    state.pollTimer = null;
  }

  function setToolState(next) {
    state.runState = next || 'ready';
    const labels = {
      ready: '就绪',
      running: '运行中',
      cancelling: '取消中',
      cancelled: '已取消',
      done: '完成',
      error: '错误',
    };
    const label = state.submissionPending ? '准备中' : (labels[state.runState] || labels.ready);
    const active = state.runState === 'running' || state.runState === 'cancelling';
    elements.startButton.disabled = active || state.submissionPending;
    elements.startButton.textContent = state.submissionPending
      ? '正在检查…' : (state.tools.get(state.activeToolId)?.action_label || '开始');
    elements.cancelButton.hidden = !active;
    elements.cancelButton.disabled = state.runState === 'cancelling';
    elements.headerStatus.dataset.state = state.runState;
    elements.headerStatus.textContent = label;
    for (const card of elements.toolList.querySelectorAll('.tool-card')) {
      const cardState = card.dataset.toolId === state.runToolId ? state.runState : 'ready';
      const dot = card.querySelector('[data-role="status-dot"]');
      const text = card.querySelector('[data-role="status-text"]');
      dot.dataset.state = cardState;
      text.textContent = labels[cardState] || labels.ready;
    }
  }

  function setFieldError(key, message) {
    const error = document.getElementById(`error-${key}`);
    const input = document.getElementById(`field-${key}`);
    if (error) error.textContent = message;
    if (input) {
      input.setAttribute('aria-invalid', 'true');
      input.focus();
    }
  }

  function clearFieldError(key) {
    const error = document.getElementById(`error-${key}`);
    const input = document.getElementById(`field-${key}`);
    if (error) error.textContent = '';
    if (input) input.removeAttribute('aria-invalid');
  }

  function clearErrors() {
    for (const key of state.specs.keys()) clearFieldError(key);
  }

  function showToast(message, type = 'info', duration = 4500) {
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.textContent = String(message);
    elements.toastRegion.appendChild(toast);
    window.setTimeout(() => toast.remove(), duration);
  }

  function openConfirm(confirmation, action = null) {
    state.confirmAction = action;
    elements.confirmTitle.textContent = confirmation.title || '请确认';
    elements.confirmMessage.textContent = confirmation.message || '';
    elements.confirmAccept.textContent = confirmation.confirm_label || '继续';
    elements.confirmDialog.hidden = false;
    elements.confirmAccept.focus();
  }

  function closeConfirm() {
    elements.confirmDialog.hidden = true;
    state.confirmAction = null;
    if (!elements.settingsPage.hidden) elements.resetSettingsButton.focus();
    else if (!elements.toolPage.hidden) elements.startButton.focus();
  }

  window.addEventListener('pywebviewready', initialize);
  return {showToast};
})();
