const rows = document.querySelector('#packages');
const status = document.querySelector('#status');
const downloadProgress = document.querySelector('#download-progress');
const refreshButton = document.querySelector('#refresh');
let busy = false;
let packages = [];
let installed = new Map();
const prepared = new Map();
const statusErrors = new Map();
const platform = /Android/i.test(window.navigator?.userAgent || '') ? 'android' : 'desktop';

function show(message) { status.textContent = message; }
function installErrorMessage(error, item) {
  const original = String(error?.message || 'install_failed');
  const code = original.match(/\(([a-z][a-z0-9_]+)\)\s*$/)?.[1] || original;
  const responseRules = item.requirements?.responseStageRules || 0;
  if (code === 'dnr_kernel_capability_unavailable')
    return `安装失败：浏览器内核缺少插件所需的网络规则能力${responseRules ? `；该插件有 ${responseRules} 条默认启用规则需要“响应头匹配”` : ''}。浏览器返回：${code}。`;
  if (code === 'dnr_package_invalid')
    return `安装失败：浏览器未通过网络规则校验，具体原因尚未确认${responseRules ? `；该插件有 ${responseRules} 条默认启用规则需要“响应头匹配”，但不能仅凭此断定失败原因` : ''}；请查看浏览器诊断记录。 (${code})`;
  return `安装失败：${original}`;
}
function apiReady() {
  return ['beginInstallWithManifest3', 'completeInstall'].every(name =>
    typeof window.chrome?.webstorePrivate?.[name] === 'function') &&
    typeof window.chrome?.management?.getAll === 'function';
}
function signedApiReady() {
  return ['install', 'getStatus', 'uninstall'].every(name =>
    typeof window.chrome?.nomadSignedExtensions?.[name] === 'function');
}
function installMode(item) {
  if (item.signedProduct && signedApiReady()) return 'signed';
  return apiReady() ? 'catalog' : null;
}
function canUpdate(item) {
  return installMode(item) === 'catalog' && ['checkUpdate', 'applyUpdate'].every(name =>
    typeof window.chrome?.webstorePrivate?.[name] === 'function');
}
function unavailableMessage(item) {
  if (item.signedProduct) return '请使用支持签名网页安装的新版逐风浏览器';
  return '此地址仅支持安装带逐风发布签名的扩展；普通 CRX 请在逐风的本机商店安装';
}
function callChrome(target, method, ...args) {
  return new Promise((resolve, reject) => {
    try {
      target[method](...args, value => {
        const error = window.chrome?.runtime?.lastError;
        if (error) reject(new Error(error.message));
        else resolve(value);
      });
    } catch (error) { reject(error); }
  });
}
function versionParts(value) {
  return value.split('.').map(Number).concat([0, 0, 0, 0]).slice(0, 4);
}
function newer(left, right) {
  const a = versionParts(left), b = versionParts(right);
  for (let i = 0; i < 4; i++) if (a[i] !== b[i]) return a[i] > b[i];
  return false;
}
function formatBytes(value) {
  if (!Number.isFinite(value) || value < 0) return '—';
  if (value < 1024) return `${value} B`;
  const units = ['KB', 'MB', 'GB'];
  let size = value / 1024;
  let unit = units[0];
  for (let index = 1; index < units.length && size >= 1024; index++) {
    size /= 1024;
    unit = units[index];
  }
  return `${size >= 10 ? size.toFixed(0) : size.toFixed(1)} ${unit}`;
}
const stageLabels = {
  preparing: '准备取包…',
  verifying: '正在校验 CRX…',
  opening_confirmation: '正在打开权限窗口…',
  awaiting_user_confirmation: '请在浏览器权限窗口确认。',
};
function trackProgress(item, operation, fallback) {
  const api = window.chrome?.webstorePrivate;
  if (typeof api?.getOperationProgress !== 'function' || typeof window.crypto?.randomUUID !== 'function') return null;
  const operationId = window.crypto.randomUUID();
  let active = true;
  let timer = setTimeout(poll, 0);
  function stop() {
    active = false;
    clearTimeout(timer);
    downloadProgress.hidden = true;
    delete status.dataset.stage;
  }
  async function poll() {
    try {
      const { progress } = await callChrome(api, 'getOperationProgress', operationId) || {};
      if (!active) return;
      if (progress?.operationId === operationId && progress.extensionId === item.id && progress.operation === operation) {
        const { stage, receivedBytes, totalBytes } = progress;
        status.dataset.stage = stage;
        downloadProgress.hidden = stage !== 'downloading';
        if (stage === 'downloading') {
          if (Number.isFinite(totalBytes) && totalBytes > 0) {
            downloadProgress.max = totalBytes;
            downloadProgress.value = Math.min(receivedBytes, totalBytes);
            const percent = Math.min(100, Math.floor(100 * receivedBytes / totalBytes));
            show(`正在下载 CRX · ${formatBytes(receivedBytes)} / ${formatBytes(totalBytes)} · ${percent}%`);
          } else {
            downloadProgress.removeAttribute('value');
            show(`正在下载 CRX · 已接收 ${formatBytes(receivedBytes)}`);
          }
        } else if (stageLabels[stage]) show(stageLabels[stage]);
      }
      timer = setTimeout(poll, 300);
    } catch {
      if (active) { stop(); show(fallback); }
    }
  }
  return { operationId, stop };
}
function button(label, action) {
  const item = document.createElement('button');
  item.type = 'button';
  item.textContent = label;
  item.disabled = busy;
  item.addEventListener('click', action);
  return item;
}
function render() {
  rows.replaceChildren();
  refreshButton.disabled = busy;
  if (!packages.length) {
    const row = rows.insertRow();
    const cell = row.insertCell(); cell.colSpan = 5;
    cell.textContent = '目录中没有有效的 CRX3 文件。将 .crx 放入 packages/ 后点击刷新。';
  }
  for (const item of packages) {
    const current = installed.get(item.id);
    const upgrade = current && newer(item.version, current.version);
    const row = rows.insertRow();
    const title = row.insertCell();
    title.textContent = item.name;
    const file = document.createElement('div');
    file.className = 'file-name';
    file.textContent = item.file;
    title.append(file);
    row.insertCell().textContent = item.version;
    row.insertCell().textContent = formatBytes(item.size);
    row.insertCell().textContent = statusErrors.get(item.id) || (!current ? '未安装' : upgrade ? `已安装 ${current.version}，可升级` :
      `已安装 ${current.version}${current.enabled ? '' : '（已停用）'}`);
    const actions = row.insertCell();
    const mode = installMode(item);
    if (!mode || statusErrors.has(item.id)) {
      actions.textContent = statusErrors.has(item.id) ? '请刷新后重试' : unavailableMessage(item);
    } else if (!current) actions.append(button('安装', () => void install(item)));
    else {
      if (upgrade && canUpdate(item)) actions.append(prepared.get(item.id) === item.sha256
        ? button('升级', () => void applyUpdate(item))
        : button('准备升级', () => void checkUpdate(item)));
      if (upgrade && mode === 'signed') actions.append('新版需卸载后重新安装。');
      actions.append(button('卸载', () => void uninstall(item)));
    }
  }
}
function setBusy(value) { busy = value; render(); }

async function refresh() {
  setBusy(true);
  try {
    const response = await fetch(`/api/catalog?platform=${platform}`, { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const catalog = await response.json();
    packages = Array.isArray(catalog.packages) ? catalog.packages : [];
    statusErrors.clear();
    if (apiReady()) {
      const all = await callChrome(chrome.management, 'getAll');
      installed = new Map((all || []).filter(item => item.type === 'extension').map(item => [item.id, item]));
    } else installed = new Map();
    await Promise.all(packages.filter(item => installMode(item) === 'signed').map(async item => {
      try {
        const result = await callChrome(chrome.nomadSignedExtensions, 'getStatus', item.id);
        if (!result || result.extensionId !== item.id || !['absent', 'installed', 'disabled'].includes(result.state) ||
            result.state !== 'absent' && (result.product !== item.signedProduct || typeof result.version !== 'string'))
          throw new Error('浏览器返回的插件状态无效');
        installed.delete(item.id);
        if (result.state !== 'absent') installed.set(item.id, {
          version: result.version, enabled: result.state === 'installed',
        });
      } catch (error) { statusErrors.set(item.id, `读取安装状态失败：${error.message}`); }
    }));
    const summary = packages.some(item => installMode(item)) ? `已读取 ${packages.length} 个插件。` :
      '请使用新版逐风浏览器。公网地址可安装带逐风发布签名的扩展，普通 CRX 请从本机商店安装。';
    show(catalog.errors?.length ? `有 ${catalog.errors.length} 个 CRX 无效：${catalog.errors.map(e => e.file + ' (' + e.error + ')').join('，')}` :
      statusErrors.size ? `${summary} 有 ${statusErrors.size} 个插件的安装状态读取失败，请刷新重试。` : summary);
  } catch (error) { show(`读取目录失败：${error.message}`); }
  finally { setBusy(false); }
}
async function install(item) {
  if (busy || !installMode(item) || statusErrors.has(item.id)) return;
  if (installMode(item) === 'signed') return installSigned(item);
  const fallback = '等待浏览器核验与确认…';
  const progress = trackProgress(item, 'install', fallback);
  // Privileged entry must run synchronously in the click activation.
  const pending = callChrome(chrome.webstorePrivate, 'beginInstallWithManifest3', {
    id: item.id, manifest: JSON.stringify(item.manifest), localizedName: item.name,
    ...(progress && { operationId: progress.operationId }),
  });
  setBusy(true); show(progress ? '准备取包…' : fallback);
  try {
    const result = await pending;
    progress?.stop();
    if (result !== '') throw new Error(result || 'install_failed');
    show('正在安装…');
    await callChrome(chrome.webstorePrivate, 'completeInstall', item.id);
    await refresh(); show('安装成功。');
  } catch (error) { show(installErrorMessage(error, item)); }
  finally { progress?.stop(); setBusy(false); }
}
async function installSigned(item) {
  const packageUrl = new URL('/api/package', window.location.origin);
  packageUrl.searchParams.set('id', item.id);
  packageUrl.searchParams.set('platform', platform);
  packageUrl.searchParams.set('sha256', item.sha256);
  // Keep the native call inside the original click's user activation.
  const pending = callChrome(chrome.nomadSignedExtensions, 'install', {
    extensionId: item.id, packageUrl: packageUrl.href, expectedSha256: item.sha256,
  });
  setBusy(true); show('浏览器正在取包并校验发布签名，请留意权限确认窗口…');
  try {
    const result = await pending;
    if (result?.extensionId !== item.id || result.product !== item.signedProduct || result.version !== item.version)
      throw new Error('安装回执与所选插件不一致');
    const current = await callChrome(chrome.nomadSignedExtensions, 'getStatus', item.id);
    if (current?.extensionId !== item.id || current.product !== item.signedProduct ||
        current.version !== item.version || !['installed', 'disabled'].includes(current.state))
      throw new Error('安装状态尚未确认，请刷新重试');
    await refresh(); show('安装成功。');
  } catch (error) { show(installErrorMessage(error, item)); }
  finally { setBusy(false); }
}
async function uninstall(item) {
  if (busy) return;
  const pending = installMode(item) === 'signed'
    ? callChrome(chrome.nomadSignedExtensions, 'uninstall', item.id)
    : callChrome(chrome.management, 'uninstall', item.id, { showConfirmDialog: true });
  setBusy(true); show('等待浏览器卸载确认…');
  try { await pending; prepared.delete(item.id); await refresh(); show('卸载成功。'); }
  catch (error) { show(`卸载未完成：${error.message}`); }
  finally { setBusy(false); }
}
async function checkUpdate(item) {
  if (busy || !canUpdate(item)) return;
  const fallback = '正在验证本地升级包…';
  const progress = trackProgress(item, 'update_check', fallback);
  setBusy(true); show(progress ? '准备取包…' : fallback);
  try {
    const options = progress ? [{ operationId: progress.operationId }] : [];
    const result = await callChrome(chrome.webstorePrivate, 'checkUpdate', item.id, ...options);
    progress?.stop();
    if (!['ready', 'permission_required'].includes(result.state))
      throw new Error(result.errorCode || result.state);
    if (result.version !== item.version) throw new Error('版本已变化，请刷新后重试');
    prepared.set(item.id, item.sha256);
    show(`版本 ${result.version} 已准备好，点击“升级”应用。`);
  } catch (error) { show(`升级准备失败：${error.message}`); }
  finally { progress?.stop(); setBusy(false); }
}
async function applyUpdate(item) {
  if (busy || !canUpdate(item)) return;
  const pending = callChrome(chrome.webstorePrivate, 'applyUpdate', item.id);
  setBusy(true); show('正在升级，若权限变化请在浏览器中确认…');
  try {
    if (!await pending) throw new Error('升级未应用，可能有扩展页面正在使用或操作被取消');
    prepared.delete(item.id); await refresh(); show('升级成功。');
  } catch (error) { show(`升级未完成：${error.message}`); }
  finally { setBusy(false); }
}

refreshButton.addEventListener('click', () => void refresh());
void refresh();
