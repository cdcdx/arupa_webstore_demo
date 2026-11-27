const rows = document.querySelector('#packages');
const status = document.querySelector('#status');
const downloadProgress = document.querySelector('#download-progress');
const refreshButton = document.querySelector('#refresh');
const progressPanel = document.querySelector('#install-progress');
const progressHeading = document.querySelector('#progress-heading');
const progressNote = document.querySelector('#progress-note');
const progressSteps = ['#step-download', '#step-verify', '#step-install'].map(id => document.querySelector(id));
let currentStep = -1;
let currentOperation = 'install';
let busy = false;
let catalogReady = false;
let packages = [];
let catalogFileCount = 0;
let installed = new Map();
const prepared = new Map();
const statusErrors = new Map();
const userAgent = window.navigator?.userAgent || '';
const platform = /iPad|iPhone|iPod/i.test(userAgent) ||
  /Macintosh/i.test(userAgent) && (window.navigator?.maxTouchPoints || 0) > 1 ? 'ios' :
  /Android/i.test(userAgent) ? 'android' : 'desktop';
const platformLabels = { desktop: 'Desktop', android: 'Android', ios: 'iOS' };

function supportsCurrentPlatform(item) {
  return Array.isArray(item?.platforms) && item.platforms.includes(platform);
}

function show(message) { status.textContent = message; }
function installErrorMessage(error, item, action = '安装') {
  const original = String(error?.message || 'install_failed');
  const code = original.match(/\(([a-z][a-z0-9_]+)\)\s*$/)?.[1] || original;
  const responseRules = item.requirements?.responseStageRules || 0;
  if (code === 'signed_extension_restart_required')
    return `${action}未提交：旧版扩展的运行副本仍在当前浏览器进程中。请完全退出并重新启动浏览器，再点击${action}。`;
  if (code === 'dnr_kernel_capability_unavailable')
    return `${action}失败：浏览器内核缺少插件所需的网络规则能力${responseRules ? `；该插件有 ${responseRules} 条默认启用规则需要“响应头匹配”` : ''}。浏览器返回：${code}。`;
  if (code === 'dnr_package_invalid')
    return `${action}失败：浏览器未通过网络规则校验，具体原因尚未确认${responseRules ? `；该插件有 ${responseRules} 条默认启用规则需要“响应头匹配”，但不能仅凭此断定失败原因` : ''}；请查看浏览器诊断记录。 (${code})`;
  return `${action}失败：${original}`;
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
function canCreateOperationId() {
  return typeof window.crypto?.randomUUID === 'function' || typeof window.crypto?.getRandomValues === 'function';
}
function createOperationId() {
  if (typeof window.crypto?.randomUUID === 'function') return window.crypto.randomUUID();
  if (typeof window.crypto?.getRandomValues !== 'function') return null;
  // getRandomValues is also available on RFC1918 HTTP test pages, unlike randomUUID.
  const bytes = window.crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
function showProgressAvailability() {
  if (progressPanel.dataset.result && progressPanel.dataset.result !== 'idle') return;
  progressPanel.dataset.result = 'idle';
  progressPanel.hidden = false;
  progressHeading.textContent = '安装进度';
  renderSteps(0);
  const api = signedApiReady() ? window.chrome.nomadSignedExtensions : apiReady() ? window.chrome.webstorePrivate : null;
  progressNote.textContent = !api ? '请使用逐风浏览器打开本页，再点击插件右侧的“安装”。' :
    typeof api.getOperationProgress !== 'function' ? '当前浏览器的安装接口仅返回最终结果，无法回传实时进度；请使用包含进度接口的逐风版本。' :
    !canCreateOperationId() ? '当前页面无法创建进度查询，请使用本机地址或公网 HTTPS 地址打开。' :
    '选择插件并点击“安装”，这里将显示下载、校验、权限确认与安装进度。';
}
function installMode(item) {
  if (item.signedProduct && signedApiReady()) return 'signed';
  return apiReady() ? 'catalog' : null;
}
function canUpdate(item) {
  return installMode(item) === 'catalog' && ['checkUpdate', 'applyUpdate'].every(name =>
    typeof window.chrome?.webstorePrivate?.[name] === 'function');
}
function canUpdateSigned(item) {
  return installMode(item) === 'signed' && typeof window.chrome?.nomadSignedExtensions?.update === 'function';
}
function operationError(item) {
  return statusErrors.get(item.id) || (installed.get(item.id)?.mode &&
    installed.get(item.id).mode !== installMode(item) ? '该 ID 已通过其他通道安装，请使用对应通道的包或在浏览器扩展管理页处理' : null);
}
function catalogFileAllowed(item) {
  return installMode(item) !== 'catalog' || item.isLatest !== false;
}
function packageUrl(item, download = false) {
  const url = new URL('/api/package', window.location.origin);
  url.searchParams.set('id', item.id);
  url.searchParams.set('platform', platform);
  url.searchParams.set('sha256', item.sha256);
  if (typeof item.file === 'string') url.searchParams.set('file', item.file);
  if (download) url.searchParams.set('download', '1');
  return url;
}
function unavailableMessage(item) {
  if (item.signedProduct) return '请使用支持签名网页安装的新版逐风浏览器';
  return '没有普通插件安装接口';
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
async function readChrome(target, method, ...args) {
  let timer;
  try {
    return await Promise.race([callChrome(target, method, ...args), new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error('读取浏览器状态超时，请刷新重试')), 15000);
    })]);
  } finally { clearTimeout(timer); }
}
function signedStatus(item, result) {
  if (!result || typeof result !== 'object' || Array.isArray(result))
    throw new Error('浏览器未返回插件状态');
  if (result.extensionId !== item.id)
    throw new Error(`状态中的插件 ID 不一致（预期 ${item.id}，实际 ${String(result.extensionId)}）`);
  if (!['absent', 'installed', 'disabled'].includes(result.state))
    throw new Error(`浏览器返回了未知安装状态：${String(result.state)}`);
  // getStatus describes the installed extension, not the current catalog package.
  // Both metadata fields are optional in the browser API's StatusResult schema.
  for (const field of ['product', 'version']) {
    if (result[field] != null && typeof result[field] !== 'string')
      throw new Error(`浏览器返回的 ${field} 字段类型无效`);
  }
  return { ...result, product: result.product || null, version: result.version || null };
}
async function fetchJson(url) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 10000);
  try {
    const response = await fetch(url, { cache: 'no-store', signal: controller.signal });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return await response.json();
  } finally { clearTimeout(timer); }
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
  preflight: '正在检查插件兼容性与权限…',
  installing: '正在安装并启动扩展…',
  committing: '正在确认安装结果…',
  rolling_back: '安装未完成，正在回滚…',
  completed: '安装已完成，正在确认状态…',
  failed: '安装失败，正在读取错误信息…',
  cancelled: '安装已取消。',
};
function renderSteps(completed, active = -1, state = 'active') {
  for (const [index, step] of progressSteps.entries()) {
    step.dataset.state = index < completed ? 'done' : index === active ? state : 'pending';
    if (index === active && state === 'active') step.setAttribute('aria-current', 'step');
    else step.removeAttribute('aria-current');
  }
}
function setInstallStage(stage) {
  const action = currentOperation === 'update' ? '升级' : '安装';
  const step = { preparing: 0, downloading: 0, verifying: 1, preflight: 1,
    opening_confirmation: 2, awaiting_user_confirmation: 2, installing: 2,
    committing: 2, rolling_back: 2, completed: 2 }[stage];
  if (step !== undefined) currentStep = step;
  progressPanel.dataset.stage = stage;
  renderSteps(Math.max(0, currentStep), currentStep,
    ['failed', 'cancelled'].includes(stage) ? stage : 'active');
  progressNote.textContent = stage === 'downloading' ? '下载完成后将继续校验扩展包，并请求安装权限。' :
    ['opening_confirmation', 'awaiting_user_confirmation'].includes(stage) ? '等待浏览器权限确认；确认后开始安装。' :
    stage === 'completed' ? `正在等待并核对浏览器最终${action}结果。` :
    stage === 'unknown' ? `当前无法查询中间进度，请留意权限窗口并等待${action}结果。` : `请保持此页面打开，浏览器正在处理${action}。`;
}
function beginInstallProgress(item, operation, tracked, ready = false) {
  currentStep = ready ? 2 : -1;
  currentOperation = operation;
  progressPanel.hidden = false;
  progressPanel.dataset.result = 'pending';
  progressHeading.textContent = `${item.name} · ${operation === 'update_check' ? '升级包准备进度' : operation === 'update' ? '升级进度' : '安装进度'}`;
  downloadProgress.hidden = true;
  setInstallStage(ready ? 'installing' : tracked ? 'preparing' : 'unknown');
  if (!tracked && !ready) progressNote.textContent = `当前浏览器未提供分阶段进度，请留意权限窗口并等待${operation === 'update' ? '升级' : '安装'}结果。`;
  progressPanel.scrollIntoView({ block: 'center', behavior: 'auto' });
}
function finishInstallProgress(result) {
  const action = currentOperation === 'update' ? '升级' : '安装';
  progressPanel.dataset.result = result;
  downloadProgress.hidden = true;
  if (['success', 'unconfirmed'].includes(result)) {
    renderSteps(3);
    progressPanel.dataset.stage = 'completed';
    progressNote.textContent = result === 'success' ? `${action}完成，浏览器已确认${action}结果。` : `浏览器已完成${action}，${action}状态待刷新确认。`;
  } else if (result === 'ready') {
    renderSteps(2);
    progressNote.textContent = '升级包已准备好，点击“升级”后开始安装。';
  } else {
    renderSteps(Math.max(0, currentStep), currentStep, result);
    progressPanel.dataset.stage = result;
    progressNote.textContent = `${currentOperation === 'update_check' ? '升级包准备' : action}${result === 'cancelled' ? '已取消' : '未完成'}，具体结果见下方提示。`;
  }
}
function finishInstallError(error) {
  finishInstallProgress(/cancel/i.test(error?.message || '') ? 'cancelled' : 'failed');
}
function trackProgress(item, operation, fallback, api = window.chrome?.webstorePrivate) {
  if (typeof api?.getOperationProgress !== 'function') return null;
  const operationId = createOperationId();
  if (!operationId) return null;
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
        setInstallStage(stage);
        downloadProgress.hidden = stage !== 'downloading';
        if (stage === 'downloading') {
          if (!Number.isFinite(receivedBytes) || receivedBytes < 0) {
            downloadProgress.removeAttribute('value');
            show('正在下载 CRX · 等待字节数据…');
          } else if (Number.isFinite(totalBytes) && totalBytes > 0) {
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
    } catch (error) {
      if (active) {
        stop(); currentStep = -1; setInstallStage('unknown'); show(fallback);
        progressNote.textContent = `进度查询不可用：${error.message}。${operation === 'update' ? '升级' : '安装'}仍在继续，请留意浏览器权限窗口。`;
      }
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
    cell.textContent = catalogFileCount
      ? `当前平台（${platformLabels[platform]}）没有匹配的插件。`
      : '目录中没有有效的 CRX3 文件。将 .crx 放入 packages/ 后点击刷新。';
  }
  for (const item of packages) {
    const current = installed.get(item.id);
    const upgrade = current?.version && newer(item.version, current.version);
    const mode = installMode(item);
    const row = rows.insertRow();
    const title = row.insertCell();
    title.textContent = item.name;
    const file = document.createElement('div');
    file.className = 'file-name';
    file.textContent = item.file;
    title.append(file);
    const hint = document.createElement('div');
    hint.className = 'file-name';
    hint.textContent = `平台提示：${Array.isArray(item.platforms) && item.platforms.length
      ? item.platforms.map(value => platformLabels[value] || value).join(' / ') : '未声明，以浏览器校验为准'}`;
    title.append(hint);
    const identity = document.createElement('div');
    identity.className = 'file-name';
    identity.textContent = `ID：${item.id}${item.isLatest ? ' · 默认安装包' : ''}`;
    title.append(identity);
    row.insertCell().textContent = item.version;
    row.insertCell().textContent = formatBytes(item.size);
    row.insertCell().textContent = !catalogReady ? '状态未确认' : !mode ? '无法读取' : operationError(item) || (!current ? '未安装' : upgrade ? `已安装 ${current.version}，${catalogFileAllowed(item) ? '可升级' : '本包较新，仅供下载'}` :
      `已安装${current.version ? ` ${current.version}` : '（版本未知）'}${current.enabled ? '' : '（已停用）'}`);
    const actions = row.insertCell();
    if (!catalogReady) actions.textContent = '请刷新目录后重试';
    else if (!mode || operationError(item)) {
      actions.textContent = operationError(item) ? '请在对应通道操作或刷新重试' : unavailableMessage(item);
    } else if (!catalogFileAllowed(item)) {
      actions.textContent = '目录通道仅支持默认包';
    } else if (!current) actions.append(button('安装', () => void install(item)));
    else {
      if (upgrade && canUpdate(item)) actions.append(prepared.get(item.id) === item.sha256
        ? button('升级', () => void applyUpdate(item))
        : button('准备升级', () => void checkUpdate(item)));
      if (upgrade && mode === 'signed') {
        if (canUpdateSigned(item)) actions.append(button('升级', () => void updateSigned(item)));
        else actions.append('发现新版本；请使用支持升级接口的新版逐风。');
      }
      actions.append(button('卸载', () => void uninstall(item)));
    }
    if (typeof item.file === 'string') {
      const download = document.createElement('a');
      download.textContent = '下载 CRX';
      download.href = packageUrl(item, true).href;
      download.download = item.file;
      download.className = 'package-download';
      download.addEventListener('click', () => {
        if (!busy) show(`已请求下载 ${item.file}，请查看浏览器下载列表；文件保存位置由浏览器决定。`);
      });
      actions.append(download);
    }
  }
}
function setBusy(value) { busy = value; render(); }

async function refresh({ internal = false } = {}) {
  if (busy && !internal) return false;
  const previousBusy = busy;
  catalogReady = false;
  setBusy(true);
  try {
    const catalog = await fetchJson(`/api/catalog?platform=${platform}&all=1`);
    if (!Array.isArray(catalog.packages) || !Array.isArray(catalog.errors)) throw new Error('目录响应格式无效');
    catalogFileCount = catalog.packages.length;
    packages = catalog.packages.filter(supportsCurrentPlatform);
    for (const [id, hash] of prepared) {
      if (!packages.some(item => item.id === id && item.sha256 === hash)) prepared.delete(id);
    }
    statusErrors.clear();
    installed = new Map();
    if (apiReady()) {
      try {
        const all = await readChrome(chrome.management, 'getAll');
        if (!Array.isArray(all)) throw new Error('安装状态响应格式无效');
        installed = new Map(all.filter(item => item.type === 'extension').map(item => [item.id, { ...item, mode: 'catalog' }]));
      } catch (error) {
        for (const item of packages.filter(item => installMode(item) === 'catalog'))
          statusErrors.set(item.id, `读取安装状态失败：${error.message}`);
      }
    }
    const signedItems = [...new Map(packages.filter(item => installMode(item) === 'signed').map(item => [item.id, item])).values()];
    await Promise.all(signedItems.map(async item => {
      try {
        const result = signedStatus(item, await readChrome(chrome.nomadSignedExtensions, 'getStatus', item.id));
        if (result.state !== 'absent') installed.set(item.id, {
          version: result.version, product: result.product, enabled: result.state === 'installed', mode: 'signed',
        });
      } catch (error) { statusErrors.set(item.id, `读取安装状态失败：${error.message}`); }
    }));
    catalogReady = true;
    const summary = `当前平台：${platformLabels[platform]}。已显示 ${packages.length} 个 CRX 文件，共 ${new Set(packages.map(item => item.id)).size} 个扩展 ID。` +
      (catalogFileCount > packages.length ? ` 已隐藏 ${catalogFileCount - packages.length} 个平台不匹配或未声明支持平台的文件。` : '') +
      (packages.length && !packages.some(item => installMode(item))
        ? ' 请使用新版逐风浏览器。公网地址可安装带逐风发布签名的扩展，普通 CRX 请从本机商店安装。' : '');
    show(catalog.errors?.length ? `有 ${catalog.errors.length} 个 CRX 无效：${catalog.errors.map(e => e.file + ' (' + e.error + ')').join('，')}` :
      statusErrors.size ? `${summary} 有 ${statusErrors.size} 个插件的安装状态读取失败，请刷新重试。` : summary);
    return true;
  } catch (error) { show(`读取目录失败：${error.name === 'AbortError' ? '请求超时，请检查服务后重试' : error.message}`); return false; }
  finally { setBusy(previousBusy); showProgressAvailability(); }
}
async function refreshAfter(message) {
  const refreshed = await refresh({ internal: true });
  show(refreshed ? message : `${message}目录刷新失败，请点击刷新重试。`);
}
async function install(item) {
  if (busy || !catalogReady || !installMode(item) || operationError(item) || !catalogFileAllowed(item)) return;
  if (installMode(item) === 'signed') return installSigned(item);
  const fallback = '等待浏览器核验与确认…';
  const progress = trackProgress(item, 'install', fallback);
  beginInstallProgress(item, 'install', !!progress);
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
    setInstallStage('installing'); show('正在安装…');
    await callChrome(chrome.webstorePrivate, 'completeInstall', item.id);
    finishInstallProgress('success');
    await refreshAfter('安装成功。');
  } catch (error) { finishInstallError(error); show(installErrorMessage(error, item)); }
  finally { progress?.stop(); setBusy(false); }
}
async function updateSigned(item) {
  if (busy || !catalogReady || operationError(item)) return;
  if (!canUpdateSigned(item)) { show('当前浏览器尚不支持签名插件原地升级，请使用支持升级接口的新版逐风。'); return; }
  const current = installed.get(item.id);
  if (!current?.version || !newer(item.version, current.version)) {
    show('尚未确认可升级版本，请刷新插件列表后重试。'); return;
  }
  return installSigned(item, 'update');
}
async function installSigned(item, operation = 'install') {
  const action = operation === 'update' ? '升级' : '安装';
  const fallback = '浏览器正在取包并校验发布签名，请留意权限确认窗口…';
  const progress = trackProgress(item, operation, fallback, window.chrome?.nomadSignedExtensions);
  beginInstallProgress(item, operation, !!progress);
  // Keep the native call inside the original click's user activation.
  const pending = callChrome(chrome.nomadSignedExtensions, operation, {
    extensionId: item.id, packageUrl: packageUrl(item).href, expectedSha256: item.sha256,
    ...(progress && { operationId: progress.operationId }),
  });
  setBusy(true); show(progress ? '准备取包…' : fallback);
  let installedReceipt = false;
  try {
    const result = await pending;
    progress?.stop();
    if (result?.extensionId !== item.id || result.product !== item.signedProduct || result.version !== item.version)
      throw new Error(`${action}回执与所选插件不一致`);
    installedReceipt = true;
    const current = signedStatus(item, await readChrome(chrome.nomadSignedExtensions, 'getStatus', item.id));
    if (!['installed', 'disabled'].includes(current.state) ||
        current.product !== null && current.product !== item.signedProduct ||
        current.version !== null && current.version !== item.version)
      throw new Error(`${action}状态尚未确认，请刷新重试`);
    finishInstallProgress('success');
    await refreshAfter(`${action}成功。`);
  } catch (error) {
    if (installedReceipt) finishInstallProgress('unconfirmed');
    else finishInstallError(error);
    show(installedReceipt ? `浏览器已完成${action}，但${action}状态未确认：${error.message}。请刷新重试。` : installErrorMessage(error, item, action));
  }
  finally { progress?.stop(); setBusy(false); }
}
async function uninstall(item) {
  const mode = installMode(item);
  if (busy || !catalogReady || !mode || operationError(item)) return;
  progressPanel.dataset.result = 'idle';
  showProgressAvailability();
  const pending = mode === 'signed'
    ? callChrome(chrome.nomadSignedExtensions, 'uninstall', item.id)
    : callChrome(chrome.management, 'uninstall', item.id, { showConfirmDialog: true });
  setBusy(true); show('等待浏览器卸载确认…');
  try {
    await pending;
    if (mode === 'signed') {
      const result = await readChrome(chrome.nomadSignedExtensions, 'getStatus', item.id);
      if (result?.extensionId !== item.id || result.state !== 'absent') throw new Error('浏览器尚未确认插件已移除，请刷新重试');
    }
    prepared.delete(item.id); await refreshAfter('卸载成功。');
  }
  catch (error) { show(`卸载未完成：${error.message}`); }
  finally { setBusy(false); }
}
async function checkUpdate(item) {
  if (busy || !catalogReady || operationError(item) || !catalogFileAllowed(item) || !canUpdate(item)) return;
  const fallback = '正在验证本地升级包…';
  const progress = trackProgress(item, 'update_check', fallback);
  beginInstallProgress(item, 'update_check', !!progress);
  setBusy(true); show(progress ? '准备取包…' : fallback);
  try {
    const options = progress ? [{ operationId: progress.operationId }] : [];
    const result = await callChrome(chrome.webstorePrivate, 'checkUpdate', item.id, ...options);
    progress?.stop();
    if (!['ready', 'permission_required'].includes(result.state))
      throw new Error(result.errorCode || result.state);
    if (result.version !== item.version) throw new Error('版本已变化，请刷新后重试');
    prepared.set(item.id, item.sha256);
    finishInstallProgress('ready');
    show(`版本 ${result.version} 已准备好，点击“升级”应用。`);
  } catch (error) { finishInstallError(error); show(`升级准备失败：${error.message}`); }
  finally { progress?.stop(); setBusy(false); }
}
async function applyUpdate(item) {
  if (busy || !catalogReady || operationError(item) || !catalogFileAllowed(item) || !canUpdate(item)) return;
  beginInstallProgress(item, 'install', false, true);
  const pending = callChrome(chrome.webstorePrivate, 'applyUpdate', item.id);
  setBusy(true); show('正在升级，若权限变化请在浏览器中确认…');
  try {
    if (!await pending) throw new Error('升级未应用，可能有扩展页面正在使用或操作被取消');
    finishInstallProgress('success');
    prepared.delete(item.id); await refreshAfter('升级成功。');
  } catch (error) { finishInstallError(error); show(`升级未完成：${error.message}`); }
  finally { setBusy(false); }
}

refreshButton.addEventListener('click', () => void refresh());
void refresh();

let lastRuntime = '';
async function loadRuntime() {
  const panel = document.querySelector('#service-status');
  if (!panel) return;
  try {
    const runtime = await fetchJson('/api/runtime');
    const snapshot = JSON.stringify([runtime.localUrl, runtime.lanUrl, runtime.tunnel]);
    if (snapshot === lastRuntime) return;
    lastRuntime = snapshot;
    panel.replaceChildren();
    for (const [label, address] of [['本机', runtime.localUrl], ['局域网', runtime.lanUrl],
      ['公网', runtime.tunnel?.state === 'ready' ? runtime.tunnel.publicUrl : null]]) {
      if (!address) continue;
      const url = new URL(address);
      if (!['http:', 'https:'].includes(url.protocol)) continue;
      const link = document.createElement('a');
      link.href = url.href; link.textContent = `${label}：${url.origin}`;
      link.target = '_blank'; link.rel = 'noopener noreferrer';
      panel.append(link, ' ');
    }
    const state = runtime.tunnel?.state;
    if (state !== 'ready') panel.append({ starting: '公网隧道正在连接…', disabled: '公网隧道未开启',
      failed: '公网隧道不可用，本机与局域网仍可访问；请运行 start.* status 或查看日志', stopped: '服务正在停止' }[state] || '公网状态未知');
  } catch {
    lastRuntime = '';
    panel.textContent = '无法连接商店服务，请运行 start.* status 检查，或运行 start.* 启动。';
  } finally { setTimeout(loadRuntime, 5000); }
}
void loadRuntime();
