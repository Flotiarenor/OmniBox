/**
 * This product includes software developed by flotiarenor.Copyright 2026 flotiarenor
 * 共享运行时 · 设置层：按 settings_schema 渲染字段（createSettingsForm）、插件自己的设置弹窗
 * （openSettingsModal）、通用弹窗（openModal）。
 *
 * 两个入口的分工写在 openModal 上方的注释里：本页插件自己的设置用本页 Bridge 读写；
 * 「上层替内嵌页渲染设置」时 schema / values / 保存都归内嵌页自己，必须走 HostChannel
 * （那一侧复用本文件）。onSave 抛异常 = 保存失败：弹窗保持打开、错误以 Toast 呈现，
 * 用户不会看到「弹窗关了但没存上」。
 */

// ==================== 设置表单（按 schema 渲染） ====================
function createSettingsForm(container, schema, values = {}) {
  const fieldEls = {};
  const baseId = 'cfg-' + Math.random().toString(36).slice(2, 8);

  (schema || []).forEach((field) => {
    // `hidden`：这个键**可写但不在面板里显示**。
    //
    // 为什么不能直接把键从 schema 里删掉：schema 同时是 `save_settings` 的写入白名单
    // （见 backend/plugin_base.py 的 allowed），删掉键等于把"同一批配置"从面板挪走
    // 的同时也让它存不进去。插件的自绘设置页（如 document-reader 的阅读设置）就是
    // 这种情况：入口在插件自己那边，值仍要走 save_settings。
    if (field.hidden === true) return;

    const wrap = document.createElement('div');
    wrap.className = 'field' + (field.type === 'checkbox' ? ' field-checkbox' : '');
    wrap.dataset.key = field.key;

    const label = document.createElement('label');
    label.className = 'field-label';
    label.htmlFor = `${baseId}-${field.key}`;
    label.textContent = field.label || field.key;
    if (field.required) label.classList.add('required');

    const current = values[field.key] !== undefined ? values[field.key] : field.default;
    let input;

    // 目录列表：实现是 Shell 共享组件 window.FolderPicker
    // （shell/frontend/public/shell/folder-picker.js，与 image-viewer 用的是同一份）。
    // 值仍是普通字符串：单值字段存一行路径，multi 字段存换行分隔的多行 ——
    // 与改造前的 text / textarea 格式一致，后端读取代码不用动。
    if (field.type === 'directory') {
      const raw = Array.isArray(current) ? current.join('\n') : String(current == null ? '' : current);
      const list = window.FolderPicker.createList({
        paths: raw.split('\n').map(line => line.trim()).filter(Boolean),
        placeholder: field.placeholder,
        emptyText: field.emptyText,
        // `multi` 是设置契约的一部分：非 multi 的目录字段是**单值**设置，
        // getValues() 只提交第一行。列表若仍允许多行，用户加的其余行会被静默丢弃，
        // 因此用 FolderPicker 的 single 模式（新路径替换旧路径）与之对齐。
        single: field.multi !== true,
        // 目录字段默认允许「网络位置」来源；声明 local_only 的字段（该目录本身就是
        // 产物，例如下载落点）不显示它 —— 在那类字段上选网络位置语义不成立。
        localOnly: field.local_only === true,
      });
      wrap.append(label, list.element);
      fieldEls[field.key] = list;
      if (field.help) {
        const help = document.createElement('p');
        help.className = 'field-help';
        help.textContent = field.help;
        wrap.appendChild(help);
      }
      container.appendChild(wrap);
      return;
    }

    // 只读信息行：值是后端算出来的（如"扫描的是哪个目录"），这里只展示、不可编辑。
    // 它**不登记进 fieldEls**，getValues() 因此取不到它 —— 展示用的一行不该被当成设置
    // 提交回后端（后端也把 info 字段排除在可写键之外，见 plugin_base.save_settings）。
    // 值优先取 values[key]（后端 get_settings()），其次取 schema 里的 value（静态说明）。
    if (field.type === 'info') {
      const shown = values[field.key] !== undefined ? values[field.key]
        : (field.value !== undefined ? field.value : field.default);
      const value = document.createElement('div');
      value.className = 'field-info';
      value.textContent = shown == null ? '' : String(shown);
      wrap.append(label, value);
      if (field.help) {
        const help = document.createElement('p');
        help.className = 'field-help';
        help.textContent = field.help;
        wrap.appendChild(help);
      }
      container.appendChild(wrap);
      return;
    }

    if (field.type === 'checkbox') {
      input = document.createElement('input');
      input.type = 'checkbox';
      input.id = `${baseId}-${field.key}`;
      input.checked = !!current;
      wrap.append(input, label);
    } else if (field.type === 'select') {
      input = document.createElement('select');
      input.id = `${baseId}-${field.key}`;
      (field.options || []).forEach(opt => {
        const o = document.createElement('option');
        o.value = (typeof opt === 'object') ? opt.value : opt;
        o.textContent = (typeof opt === 'object') ? opt.label : opt;
        input.appendChild(o);
      });
      if (current !== undefined) input.value = String(current);
    } else if (field.type === 'textarea') {
      input = document.createElement('textarea');
      input.id = `${baseId}-${field.key}`;
      input.value = current !== undefined ? current : '';
      if (field.placeholder) input.placeholder = field.placeholder;
    } else {
      const isRange = field.type === 'range';
      const isNumber = field.type === 'number';
      input = document.createElement('input');
      input.type = isRange ? 'range' : (isNumber ? 'number' : 'text');
      input.id = `${baseId}-${field.key}`;
      if (isRange || isNumber) {
        if (field.min !== undefined) input.min = field.min;
        if (field.max !== undefined) input.max = field.max;
        if (field.step !== undefined) input.step = field.step;
        input.value = current !== undefined ? current : (field.default !== undefined ? field.default : 0);
      } else {
        input.value = current !== undefined ? current : (field.default !== undefined ? field.default : '');
        if (field.placeholder) input.placeholder = field.placeholder;
      }
    }

    if (field.type === 'range') {
      const valueSpan = document.createElement('span');
      valueSpan.className = 'field-range-value';
      valueSpan.textContent = input.value;
      input.addEventListener('input', () => { valueSpan.textContent = input.value; });
      const row = document.createElement('div');
      row.className = 'field-range';
      row.append(input, valueSpan);
      wrap.append(label, row);
      fieldEls[field.key] = input;
    } else if (field.type === 'checkbox') {
      fieldEls[field.key] = input;
    } else {
      wrap.append(label, input);
      fieldEls[field.key] = input;
    }

    if (field.help) {
      const help = document.createElement('p');
      help.className = 'field-help';
      help.textContent = field.help;
      wrap.appendChild(help);
    }
    container.appendChild(wrap);
  });

  function getValues() {
    const out = {};
    (schema || []).forEach((field) => {
      const el = fieldEls[field.key];
      if (!el) return;
      if (field.type === 'directory') {
        const paths = el.getPaths();
        // 单值字段存一行路径，多值字段（multi）存换行分隔；两者都是字符串，
        // 与改造前的 text / textarea 值格式一致，后端读取代码不用动
        out[field.key] = field.multi ? paths.join('\n') : (paths[0] || '');
      }
      else if (field.type === 'checkbox') out[field.key] = el.checked;
      else if (field.type === 'number' || field.type === 'range') out[field.key] = Number(el.value);
      else out[field.key] = el.value;
    });
    return out;
  }

  function setValues(newValues) {
    (schema || []).forEach((field) => {
      const el = fieldEls[field.key];
      if (!el || newValues[field.key] === undefined) return;
      if (field.type === 'directory') {
        const raw = String(newValues[field.key] == null ? '' : newValues[field.key]);
        el.setPaths(raw.split('\n').map(line => line.trim()).filter(Boolean));
      }
      else if (field.type === 'checkbox') el.checked = !!newValues[field.key];
      else if (field.type === 'range') {
        el.value = newValues[field.key];
        const span = el.parentElement.querySelector('.field-range-value');
        if (span) span.textContent = el.value;
      } else el.value = newValues[field.key];
    });
  }

  return { getValues, setValues, element: container };
}

// ==================== 统一设置弹窗（插件按需调用） ====================
function openSettingsModal(options = {}) {
  const title = options.title || '设置';
  const onSave = options.onSave;
  return openModal({
    title: title,
    // 省略 schema/values 时在内部按当前插件后端拉取：这是"插件自己的设置弹窗"的默认用法
    schema: options.schema,
    values: options.values,
    emptyText: '该插件暂无设置项',
    onSave: onSave || (async (values) => {
      const result = await Bridge.call('save_settings', values);
      return { result: result };
    }),
  }).then(res => {
    if (!res || !res.saved) return null;
    return (res.result && res.result.values) || res.values;
  });
}

/**
 * 通用弹窗：Promise 化，`resolve(null)` = 取消，`resolve({saved: true, values, result})` = 已保存。
 *
 * 存在两个入口是有原因的：`openSettingsModal` 是"本页插件自己的设置"，省略 schema 时
 * 用本页的 `Bridge` 拉取与保存；而**上层替内嵌页渲染**设置时，schema/values/保存都归
 * 内嵌页自己（本页的 Bridge 指向的是这台宿主，直接存会写错插件）—— 那条路走
 * `HostChannel`，由 `defaultUiHandler` 调用本函数。
 *
 * `onSave` 抛异常 = 保存失败：弹窗保持打开、按钮恢复可点、错误以 toast 呈现。这个
 * 语义是刻意的，子插件回传失败时用户不会看到"弹窗关了但其实没存上"。
 */
function openModal(options = {}) {
  const title = options.title || '设置';
  const onSave = options.onSave;
  let form = null;

  return new Promise((resolve) => {
    (async () => {
      let schema = options.schema;
      let values = options.values;
      if (!schema) {
        try { schema = await Bridge.call('get_settings_schema'); } catch (e) { schema = []; }
      }
      if (values === undefined) {
        try { values = await Bridge.call('get_settings'); } catch (e) { values = {}; }
      }

      const overlay = document.createElement('div');
      overlay.className = 'modal active';
      const previousFocus = _rememberFocus();
      const titleId = 'modal-title-' + Math.random().toString(36).slice(2, 8);
      overlay.setAttribute('role', 'dialog');
      overlay.setAttribute('aria-modal', 'true');
      overlay.setAttribute('aria-labelledby', titleId);
      overlay.innerHTML = `
        <div class="modal-box">
          <h3 id="${titleId}">${Utils.escapeHtml(title)}</h3>
          <div class="modal-body settings-form"></div>
          <div class="modal-footer">
            <button class="btn" data-act="cancel">取消</button>
            <button class="btn btn-primary" data-act="save">保存</button>
          </div>
        </div>`;
      document.body.appendChild(overlay);
      let settled = false;
      const finish = (result) => {
        if (settled) return;
        settled = true;
        overlay.removeEventListener('keydown', onKeydown);
        overlay.remove();
        _restoreFocus(previousFocus);
        resolve(result);
      };
      const onKeydown = (event) => _dialogKeydown(overlay, event, () => finish(null));
      overlay.addEventListener('keydown', onKeydown);

      const body = overlay.querySelector('.modal-body');
      if (!schema || schema.length === 0) {
        body.innerHTML = '';
        const empty = document.createElement('div');
        empty.className = 'empty-state';
        empty.style.minHeight = '120px';
        empty.textContent = options.emptyText || '该插件暂无设置项';
        body.appendChild(empty);
      } else {
        form = createSettingsForm(body, schema, values || {});
      }
      // 初始焦点给第一个可编辑字段；没有字段时给“取消”，避免焦点留在背景页面。
      _focusFirst(overlay, overlay.querySelector('[data-act="cancel"]'));

      // pointerdown：按在遮罩上就关，按在弹窗内（哪怕拖到遮罩上松开）不关
      overlay.addEventListener('pointerdown', (e) => { if (e.target === overlay) finish(null); });
      overlay.querySelector('[data-act="cancel"]').addEventListener('click', () => finish(null));

      overlay.querySelector('[data-act="save"]').addEventListener('click', async () => {
        const saveBtn = overlay.querySelector('[data-act="save"]');
        saveBtn.disabled = true;
        try {
          const newValues = form ? form.getValues() : {};
          let result = null;
          if (onSave) result = await onSave(newValues);
          if (result && result.result && result.result.success === false) {
            throw new Error(result.result.error || '保存失败');
          }
          if (result && result.success === false) {
            throw new Error(result.error || '保存失败');
          }
          Toast.success((options.successMessage) || '设置已保存');
          finish({ saved: true, values: newValues, result: (result && result.result) || result });
        } catch (e) {
          saveBtn.disabled = false;
          Toast.error(e.message || '保存失败');
        }
      });
    })();
  });
}

