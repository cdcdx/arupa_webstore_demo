const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const rows = {
  replaceChildren() {},
  insertRow() { return { insertCell() { return { textContent: '' }; } }; },
};
const status = { textContent: '' };
const refresh = { disabled: false, addEventListener() {} };
const context = {
  document: { querySelector(selector) {
    return { '#packages': rows, '#status': status, '#refresh': refresh }[selector];
  } },
  window: {},
  fetch: async () => ({ ok: true, json: async () => ({ packages: [], errors: [] }) }),
};
vm.runInNewContext(fs.readFileSync('app.js', 'utf8'), context);

const adguard = { requirements: { responseStageRules: 5 } };
const generic = { requirements: {} };
assert.match(context.installErrorMessage(new Error('扩展已下载，但网络规则校验失败。 (dnr_package_invalid)'), adguard),
  /5 条默认启用规则需要“响应头匹配”，但不能仅凭此断定失败原因/);
assert.match(context.installErrorMessage(new Error('dnr_kernel_capability_unavailable'), adguard),
  /浏览器内核缺少插件所需的网络规则能力/);
assert.match(context.installErrorMessage(new Error('dnr_package_invalid'), generic),
  /具体原因尚未确认/);
assert.equal(context.installErrorMessage(new Error('user_cancelled'), adguard), '安装失败：user_cancelled');
assert.equal(context.formatBytes(841), '841 B');
assert.equal(context.formatBytes(850944), '831 KB');
assert.equal(context.formatBytes(undefined), '—');
