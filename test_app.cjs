const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('app.js', 'utf8');
const signed = { id: 'a'.repeat(32), name: 'Signed fixture', version: '1.0', size: 841,
  sha256: '1'.repeat(64), signedProduct: 'demo-addon', requirements: {}, manifest: {} };
const ordinary = { ...signed, id: 'b'.repeat(32), signedProduct: null };
const settle = () => new Promise(resolve => setImmediate(resolve));

function element(tag = '') {
  return { tag, textContent: '', dataset: {}, children: [], disabled: false, hidden: true,
    append(...children) { this.children.push(...children); },
    replaceChildren() { this.children = []; },
    addEventListener(name, handler) { this[name] = handler; },
    setAttribute(name, value) { this[name] = value; },
    removeAttribute(name) { delete this[name]; },
    scrollIntoView(options) { this.scrolledIntoView = options; },
    insertRow() { const row = element('tr'); this.append(row); return row; },
    insertCell() { const cell = element('td'); this.append(cell); return cell; },
  };
}
async function page(chrome = {}, items = [signed, ordinary], userAgent = 'Desktop') {
  const nodes = Object.fromEntries(['#packages', '#status', '#refresh', '#download-progress',
    '#install-progress', '#progress-heading', '#progress-note', '#step-download', '#step-verify', '#step-install']
    .map(id => [id, element()]));
  const requests = [];
  const context = { document: { querySelector: id => nodes[id], createElement: element },
    window: { chrome, navigator: { userAgent }, location: { origin: 'https://store.example' } },
    chrome, URL, AbortController, setTimeout, clearTimeout,
    fetch: async url => { requests.push(url); return { ok: true, json: async () => ({ packages: items, errors: [] }) }; },
  };
  vm.runInNewContext(source, context);
  await settle();
  return { context, nodes, requests, status: () => nodes['#status'].textContent,
    actions: index => nodes['#packages'].children[index].children[4],
    buttons: index => nodes['#packages'].children[index].children[4].children.filter(e => e.tag === 'button'),
  };
}
function signedBrowser(options = {}) {
  let current = options.initialStatus || { extensionId: signed.id, state: 'absent' };
  const calls = [];
  const chrome = { runtime: {}, nomadSignedExtensions: {
    getStatus(id, cb) {
      calls.push(['getStatus', id]);
      const reply = current.state === 'installed' && options.postStatus ? { ...current, ...options.postStatus } : current;
      cb(options.badStatus || options.badPostStatus && current.state === 'installed' ? {} :
        options.omitMetadata ? { extensionId: current.extensionId, state: current.state } : reply);
    },
    install(details, cb) {
      calls.push(['install', details]);
      if (options.error) {
        chrome.runtime.lastError = { message: options.error };
        cb(); delete chrome.runtime.lastError; return;
      }
      current = { extensionId: signed.id, state: 'installed', product: signed.signedProduct, version: signed.version };
      cb({ ...current, ...(options.badReceipt && { extensionId: ordinary.id }) });
    },
    uninstall(id, cb) { calls.push(['uninstall', id]); current = { extensionId: id, state: 'absent' }; cb(); },
  } };
  return { chrome, calls };
}

(async () => {
  const unsupported = await page();
  assert.equal(unsupported.buttons(0).length, 0);
  assert.equal(unsupported.buttons(1).length, 0);
  assert.match(unsupported.actions(0).textContent, /新版逐风/);
  assert.equal(unsupported.actions(1).textContent, '无签名');
  assert.equal(unsupported.nodes['#packages'].children[0].children[3].textContent, '无法读取');
  assert.equal(unsupported.nodes['#install-progress'].hidden, false, 'the progress panel is visible before installing');
  assert.match(unsupported.nodes['#progress-note'].textContent, /逐风浏览器/);

  const browser = signedBrowser();
  const publicPage = await page(browser.chrome);
  assert.match(publicPage.nodes['#progress-note'].textContent, /仅返回最终结果/);
  assert.equal(publicPage.buttons(0)[0].textContent, '安装');
  assert.equal(publicPage.buttons(1).length, 0, 'ordinary CRX cannot use the public signed route');
  const pending = publicPage.context.install(signed);
  assert.equal(publicPage.nodes['#install-progress'].scrolledIntoView.block, 'center', 'installation reveals progress even from a lower table row');
  assert.equal(publicPage.nodes['#install-progress'].hidden, false);
  assert.match(publicPage.nodes['#progress-note'].textContent, /未提供分阶段进度/);
  assert.equal(publicPage.nodes['#step-download'].dataset.state, 'pending', 'old APIs do not invent download progress');
  const nativeCall = browser.calls.find(call => call[0] === 'install');
  assert.ok(nativeCall, 'native install is called synchronously before yielding user activation');
  const url = new URL(nativeCall[1].packageUrl);
  assert.equal(url.origin, 'https://store.example');
  assert.equal(url.searchParams.get('id'), signed.id);
  assert.equal(url.searchParams.get('platform'), 'desktop');
  assert.equal(url.searchParams.get('sha256'), signed.sha256);
  assert.equal(nativeCall[1].expectedSha256, signed.sha256);
  await pending;
  assert.equal(publicPage.status(), '安装成功。');
  assert.equal(publicPage.nodes['#step-install'].dataset.state, 'done');
  assert.equal(publicPage.buttons(0)[0].textContent, '卸载');
  await publicPage.context.checkUpdate(signed);
  assert.equal(publicPage.status(), '安装成功。', 'signed route does not call legacy update APIs');
  await publicPage.context.uninstall(signed);
  assert.equal(publicPage.status(), '卸载成功。');
  assert.equal(publicPage.nodes['#install-progress'].hidden, false, 'uninstall returns to the visible idle progress panel');
  assert.equal(publicPage.buttons(0)[0].textContent, '安装');

  for (const error of ['confirmation_cancelled', 'signed_extension_signature_invalid']) {
    const fixture = signedBrowser({ error });
    const failed = await page(fixture.chrome);
    await failed.context.install(signed);
    assert.match(failed.status(), new RegExp(error));
    assert.equal(failed.buttons(0)[0].textContent, '安装');
    assert.equal(failed.buttons(0)[0].disabled, false);
  }
  const invalid = await page(signedBrowser({ badReceipt: true }).chrome);
  await invalid.context.install(signed);
  assert.match(invalid.status(), /回执与所选插件不一致/);
  const unconfirmed = await page(signedBrowser({ badPostStatus: true }).chrome);
  await unconfirmed.context.install(signed);
  assert.match(unconfirmed.status(), /浏览器已完成安装，但安装状态未确认/);
  assert.equal(unconfirmed.nodes['#install-progress'].dataset.result, 'unconfirmed');
  const badStatus = await page(signedBrowser({ badStatus: true }).chrome);
  assert.equal(badStatus.buttons(0).length, 0);
  assert.match(badStatus.status(), /安装状态读取失败/);

  // StatusResult metadata is optional, and the installed product may differ
  // from the current catalog's signed product without making the reply invalid.
  for (const metadata of [{}, { product: null, version: null }, { product: 'older-product', version: '0.9' }]) {
    const fixture = signedBrowser({ initialStatus: { extensionId: signed.id, state: 'installed', ...metadata } });
    const installedPage = await page(fixture.chrome, [signed]);
    assert.equal(installedPage.buttons(0)[0].textContent, '卸载');
    assert.doesNotMatch(installedPage.status(), /失败|无效/);
    const state = installedPage.nodes['#packages'].children[0].children[3].textContent;
    assert.match(state, metadata.version ? /已安装 0.9/ : /已安装（版本未知）/);
  }
  const optional = await page(signedBrowser({ omitMetadata: true }).chrome, [signed]);
  await optional.context.install(signed);
  assert.equal(optional.status(), '安装成功。');
  await optional.context.refresh();
  assert.equal(optional.buttons(0)[0].textContent, '卸载');
  await optional.context.uninstall(signed);
  assert.equal(optional.status(), '卸载成功。');

  // Signed progress uses its own namespace and leaves the final callback in
  // charge of success. Control timers so the tests exercise the in-flight path.
  for (const outcome of ['success', 'cancel', 'query-error', 'lan-http']) {
    const fixture = signedBrowser();
    let finish;
    const nativeInstall = fixture.chrome.nomadSignedExtensions.install;
    fixture.chrome.nomadSignedExtensions.install = (details, callback) => {
      fixture.calls.push(['pending-install', details]);
      finish = () => {
        if (outcome === 'cancel') {
          fixture.chrome.runtime.lastError = { message: 'signed_extension_confirmation_cancelled' };
          callback(); delete fixture.chrome.runtime.lastError;
        } else nativeInstall(details, callback);
      };
    };
    const operationId = outcome === 'lan-http' ? '11111111-1111-4111-9111-111111111111' : 'ee58ba18-4da6-4f70-9018-9d5981e0db14';
    let snapshot = { operationId, operation: 'install', extensionId: signed.id,
      stage: 'downloading', receivedBytes: 40, totalBytes: 100 };
    fixture.chrome.nomadSignedExtensions.getOperationProgress = (id, callback) => {
      assert.equal(id, operationId);
      fixture.calls.push(['signed-progress', id]);
      if (outcome === 'query-error') {
        fixture.chrome.runtime.lastError = { message: 'host_unavailable' };
        callback(); delete fixture.chrome.runtime.lastError;
      } else callback({ progress: snapshot });
    };
    fixture.chrome.webstorePrivate = { getOperationProgress() { throw new Error('wrong namespace'); } };
    const progressPage = await page(fixture.chrome, [signed]);
    progressPage.context.window.crypto = outcome === 'lan-http'
      ? { getRandomValues: bytes => bytes.fill(17) } : { randomUUID: () => operationId };
    if (outcome === 'lan-http') progressPage.context.window.location.origin = 'http://192.168.1.10:18731';
    const timers = new Map(); let sequence = 0;
    progressPage.context.setTimeout = fn => { timers.set(++sequence, fn); return sequence; };
    progressPage.context.clearTimeout = id => timers.delete(id);
    async function tick() {
      const [id, fn] = timers.entries().next().value;
      timers.delete(id); await fn();
    }
    const installing = progressPage.context.install(signed);
    const steps = () => ['#step-download', '#step-verify', '#step-install'].map(id => progressPage.nodes[id].dataset.state);
    assert.deepEqual(steps(), ['active', 'pending', 'pending']);
    assert.equal(fixture.calls.find(call => call[0] === 'pending-install')[1].operationId, operationId);
    assert.equal(fixture.calls.filter(call => call[0] === 'signed-progress').length, 0,
      'install starts synchronously before any polling');
    await tick();
    if (outcome === 'query-error') {
      assert.match(progressPage.status(), /浏览器正在取包并校验发布签名/);
      assert.equal(timers.size, 0, 'progress failure does not cancel installation');
      assert.deepEqual(steps(), ['pending', 'pending', 'pending']);
      assert.equal(progressPage.nodes['#install-progress'].dataset.result, 'pending');
      assert.match(progressPage.nodes['#progress-note'].textContent, /host_unavailable.*安装仍在继续/);
    } else {
      assert.match(progressPage.status(), /40%/);
      assert.equal(progressPage.nodes['#download-progress'].hidden, false);
      snapshot = { ...snapshot, receivedBytes: 60 }; delete snapshot.totalBytes;
      await tick();
      assert.match(progressPage.status(), /已接收 60 B/);
      assert.equal(progressPage.nodes['#download-progress'].value, undefined);
      snapshot = { ...snapshot, receivedBytes: 100, totalBytes: 100 };
      await tick();
      assert.match(progressPage.status(), /100%/);
      assert.doesNotMatch(progressPage.status(), /安装成功/);
      assert.deepEqual(steps(), ['active', 'pending', 'pending'], 'download 100% is not install completion');
      // Ignore stale or unrelated snapshots instead of moving another operation's UI.
      for (const wrong of [{ operationId: 'other' }, { extensionId: ordinary.id }, { operation: 'update_check' }]) {
        const previous = snapshot;
        snapshot = { ...snapshot, stage: 'installing', ...wrong }; await tick();
        assert.deepEqual(steps(), ['active', 'pending', 'pending']);
        snapshot = previous;
      }
      for (const stage of ['verifying', 'awaiting_user_confirmation', 'installing', 'committing', 'rolling_back', 'completed']) {
        snapshot = { ...snapshot, stage }; await tick();
        assert.equal(progressPage.nodes['#status'].dataset.stage, stage);
        assert.equal(progressPage.nodes['#download-progress'].hidden, true);
        assert.doesNotMatch(progressPage.status(), /安装成功/);
        assert.deepEqual(steps(), stage === 'verifying' ? ['done', 'active', 'pending'] : ['done', 'done', 'active']);
        if (stage === 'awaiting_user_confirmation') assert.match(progressPage.nodes['#progress-note'].textContent, /等待浏览器权限确认/);
      }
    }
    finish(); await installing;
    assert.equal(timers.size, 0);
    assert.equal(progressPage.nodes['#download-progress'].hidden, true);
    assert.equal(progressPage.nodes['#status'].dataset.stage, undefined);
    assert.match(progressPage.status(), outcome === 'cancel' ? /confirmation_cancelled/ : /安装成功/);
    assert.deepEqual(steps(), outcome === 'cancel' ? ['done', 'done', 'cancelled'] : ['done', 'done', 'done']);
    assert.equal(progressPage.nodes['#install-progress'].dataset.result, outcome === 'cancel' ? 'cancelled' : 'success');
  }

  const disabled = await page(signedBrowser({ initialStatus: {
    extensionId: signed.id, state: 'disabled', version: signed.version,
  } }).chrome, [signed]);
  assert.match(disabled.nodes['#packages'].children[0].children[3].textContent, /已停用/);
  assert.equal(disabled.buttons(0)[0].textContent, '卸载');

  for (const [reply, expected] of [
    [{ extensionId: ordinary.id, state: 'installed' }, /插件 ID 不一致/],
    [{ extensionId: signed.id, state: 'unknown' }, /未知安装状态/],
    [{ extensionId: signed.id, state: 'installed', version: 42 }, /version 字段类型无效/],
  ]) {
    const malformed = await page(signedBrowser({ initialStatus: reply }).chrome, [signed]);
    assert.equal(malformed.buttons(0).length, 0);
    assert.match(malformed.nodes['#packages'].children[0].children[3].textContent, expected);
  }
  for (const postStatus of [{ product: 'different-product' }, { version: '2.0' }]) {
    const mismatch = await page(signedBrowser({ postStatus }).chrome, [signed]);
    await mismatch.context.install(signed);
    assert.match(mismatch.status(), /安装状态未确认/);
    assert.doesNotMatch(mismatch.status(), /安装成功/);
  }

  const mobileBrowser = signedBrowser();
  const mobile = await page(mobileBrowser.chrome, [signed], 'Mozilla/5.0 Android');
  assert.equal(mobile.requests[0], '/api/catalog?platform=android');
  await mobile.context.install(signed);
  assert.equal(new URL(mobileBrowser.calls.find(call => call[0] === 'install')[1].packageUrl).searchParams.get('platform'), 'android');

  const legacyCalls = [];
  const legacy = { runtime: {}, management: { getAll: cb => cb([]) }, webstorePrivate: {
    beginInstallWithManifest3(details, cb) { legacyCalls.push(['begin', details]); cb(''); },
    completeInstall(id, cb) { legacyCalls.push(['complete', id]); cb(); },
  } };
  const local = await page(legacy, [ordinary]);
  assert.equal(local.buttons(0)[0].textContent, '安装', 'local install does not require optional update APIs');
  const localPending = local.context.install(ordinary);
  assert.equal(legacyCalls[0][0], 'begin');
  await localPending;
  assert.equal(legacyCalls[1][0], 'complete');
  assert.equal(local.status(), '安装成功。');

  // The catalog route stops beginInstall polling before completeInstall, and
  // update_check finishes only package preparation, not the installation step.
  const catalogItem = { ...ordinary };
  let beginCallback, completeCallback, checkCallback, applyCallback;
  let managed = [], stage = 'downloading', operation = 'install';
  const catalogCalls = [];
  const catalogBrowser = { runtime: {}, management: { getAll: cb => cb(managed) }, webstorePrivate: {
    beginInstallWithManifest3(details, cb) { catalogCalls.push(['begin', details]); beginCallback = cb; },
    completeInstall(id, cb) { catalogCalls.push(['complete', id]); completeCallback = cb; },
    checkUpdate(id, options, cb) { catalogCalls.push(['check', id, options]); checkCallback = cb; },
    applyUpdate(id, cb) { catalogCalls.push(['apply', id]); applyCallback = cb; },
    getOperationProgress(id, cb) { cb({ progress: { operationId: id, operation,
      extensionId: catalogItem.id, stage, receivedBytes: 25, totalBytes: 100 } }); },
  } };
  const catalogPage = await page(catalogBrowser, [catalogItem]);
  let uuid = 0, timerSequence = 0;
  const catalogTimers = new Map();
  catalogPage.context.window.crypto = { randomUUID: () => `eeeeeeee-eeee-4eee-8eee-${String(++uuid).padStart(12, '0')}` };
  catalogPage.context.setTimeout = fn => { catalogTimers.set(++timerSequence, fn); return timerSequence; };
  catalogPage.context.clearTimeout = id => catalogTimers.delete(id);
  async function catalogTick() {
    const [id, fn] = catalogTimers.entries().next().value;
    catalogTimers.delete(id); await fn();
  }
  const installCatalog = catalogPage.context.install(catalogItem);
  assert.equal(catalogCalls[0][0], 'begin', 'catalog install preserves the synchronous user click');
  await catalogTick(); assert.match(catalogPage.status(), /25%/);
  stage = 'verifying'; await catalogTick();
  assert.equal(catalogPage.nodes['#step-verify'].dataset.state, 'active');
  stage = 'awaiting_user_confirmation'; await catalogTick();
  assert.match(catalogPage.status(), /权限窗口确认/);
  beginCallback(''); await settle();
  assert.equal(catalogCalls[1][0], 'complete');
  assert.equal(catalogTimers.size, 0, 'beginInstall task is finished before completeInstall starts');
  assert.equal(catalogPage.nodes['#step-install'].dataset.state, 'active');
  assert.doesNotMatch(catalogPage.status(), /安装成功/);
  managed = [{ id: catalogItem.id, type: 'extension', version: catalogItem.version, enabled: true }];
  completeCallback(); await installCatalog;
  assert.equal(catalogPage.status(), '安装成功。');

  catalogItem.version = '1.1'; catalogItem.sha256 = '2'.repeat(64);
  await catalogPage.context.refresh();
  stage = 'downloading'; operation = 'update_check';
  const prepareCatalog = catalogPage.context.checkUpdate(catalogItem);
  const updateOptions = catalogCalls.find(call => call[0] === 'check')[2];
  assert.notEqual(updateOptions.operationId, catalogCalls[0][1].operationId, 'new update check has a separate operation ID');
  await catalogTick(); assert.match(catalogPage.status(), /25%/);
  stage = 'verifying'; await catalogTick();
  checkCallback({ state: 'ready', version: '1.1' }); await prepareCatalog;
  assert.equal(catalogTimers.size, 0);
  assert.equal(catalogPage.nodes['#step-verify'].dataset.state, 'done');
  assert.equal(catalogPage.nodes['#step-install'].dataset.state, 'pending');
  assert.equal(catalogPage.buttons(0)[0].textContent, '升级');
  const applyCatalog = catalogPage.context.applyUpdate(catalogItem);
  assert.equal(catalogPage.nodes['#step-install'].dataset.state, 'active');
  assert.doesNotMatch(catalogPage.status(), /升级成功/);
  managed[0].version = '1.1'; applyCallback(true); await applyCatalog;
  assert.equal(catalogPage.status(), '升级成功。');
  assert.equal(catalogPage.nodes['#step-install'].dataset.state, 'done');

  // A failed refresh must not leave stale actionable buttons behind.
  const recoveryBrowser = signedBrowser();
  const recovery = await page(recoveryBrowser.chrome);
  const originalFetch = recovery.context.fetch;
  recovery.context.fetch = async () => { throw new Error('service offline'); };
  assert.equal(await recovery.context.refresh(), false);
  assert.match(recovery.status(), /读取目录失败/);
  assert.equal(recovery.buttons(0).length, 0);
  await recovery.context.install(signed);
  assert.equal(recoveryBrowser.calls.filter(call => call[0] === 'install').length, 0);
  recovery.context.fetch = originalFetch;
  await recovery.context.refresh();
  assert.equal(recovery.buttons(0)[0].textContent, '安装');

  // Refresh cannot unlock the UI during an unresolved native installation.
  let confirm;
  recoveryBrowser.chrome.nomadSignedExtensions.install = (details, callback) => { confirm = callback; };
  const inFlight = recovery.context.install(signed);
  const requestCount = recovery.requests.length;
  assert.equal(await recovery.context.refresh(), false);
  assert.equal(recovery.requests.length, requestCount);
  assert.equal(recovery.buttons(0)[0].disabled, true);
  recoveryBrowser.chrome.runtime.lastError = { message: 'signed_extension_confirmation_cancelled' };
  confirm();
  delete recoveryBrowser.chrome.runtime.lastError;
  await inFlight;
  assert.equal(recovery.buttons(0)[0].disabled, false);

  // One unavailable API namespace must not disable the independent signed route.
  const partialBrowser = signedBrowser();
  partialBrowser.chrome.webstorePrivate = legacy.webstorePrivate;
  partialBrowser.chrome.management = { getAll() { throw new Error('legacy unavailable'); } };
  const partial = await page(partialBrowser.chrome);
  assert.equal(partial.buttons(0)[0].textContent, '安装');
  assert.equal(partial.buttons(1).length, 0);
  partialBrowser.chrome.management = { getAll: callback => callback([{ id: signed.id, type: 'extension', version: '1.0', enabled: true }]) };
  await partial.context.refresh();
  assert.equal(partial.buttons(0).length, 0);
  assert.match(partial.nodes['#packages'].children[0].children[3].textContent, /其他通道安装/);

  const unchangedBrowser = signedBrowser();
  const unchanged = await page(unchangedBrowser.chrome);
  await unchanged.context.install(signed);
  unchangedBrowser.chrome.nomadSignedExtensions.uninstall = (id, callback) => callback();
  await unchanged.context.uninstall(signed);
  assert.match(unchanged.status(), /尚未确认插件已移除/);
  assert.equal(unchanged.buttons(0)[0].textContent, '卸载');

  const context = local.context;
  const adguard = { requirements: { responseStageRules: 5 } };
  assert.match(context.installErrorMessage(new Error('dnr_package_invalid'), adguard),
    /5 条默认启用规则需要“响应头匹配”，但不能仅凭此断定失败原因/);
  assert.match(context.installErrorMessage(new Error('dnr_kernel_capability_unavailable'), adguard), /内核缺少/);
  assert.match(context.installErrorMessage(new Error('dnr_package_invalid'), ordinary), /具体原因尚未确认/);
  assert.equal(context.installErrorMessage(new Error('user_cancelled'), adguard), '安装失败：user_cancelled');
  assert.equal(context.formatBytes(841), '841 B');
  assert.equal(context.formatBytes(850944), '831 KB');
  assert.equal(context.formatBytes(undefined), '—');

  // Runtime links follow the current tunnel, removing stale URLs on failure.
  const links = element();
  context.document.querySelector = id => id === '#service-status' ? links : local.nodes[id];
  let runtime = { localUrl: 'http://127.0.0.1:18731/', lanUrl: 'http://192.168.1.10:18731/',
    tunnel: { state: 'starting', publicUrl: 'https://stale.example/' } };
  context.fetch = async () => ({ ok: true, json: async () => runtime });
  const linkTimers = new Map(); let nextTimer = 0;
  context.setTimeout = fn => { linkTimers.set(++nextTimer, fn); return nextTimer; };
  context.clearTimeout = id => linkTimers.delete(id);
  const addresses = () => links.children.filter(item => item.tag === 'a').map(item => item.href);
  await context.loadRuntime();
  assert.deepEqual(addresses(), [runtime.localUrl, runtime.lanUrl]);
  assert.ok(links.children.includes('公网隧道正在连接…'));
  runtime.tunnel = { state: 'ready', publicUrl: 'https://active.example/' };
  await context.loadRuntime();
  assert.deepEqual(addresses(), [runtime.localUrl, runtime.lanUrl, runtime.tunnel.publicUrl]);
  runtime.tunnel = { state: 'failed', publicUrl: 'https://active.example/' };
  await context.loadRuntime();
  assert.deepEqual(addresses(), [runtime.localUrl, runtime.lanUrl]);
  runtime.lanUrl = 'javascript:alert(1)';
  await context.loadRuntime();
  assert.deepEqual(addresses(), [runtime.localUrl]);
  console.log('PASS: signed and catalog installation, scoped byte progress, update preparation/application, fallback, cancellation and receipts');
})().catch(error => { console.error(error); process.exitCode = 1; });
