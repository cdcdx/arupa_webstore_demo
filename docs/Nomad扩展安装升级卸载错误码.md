# Nomad 扩展安装、升级与卸载错误码

更新日期：2026-11-03  
适用范围：当前 Nomad Desktop 商店桥接、原生扩展安装器和更新器  
文档状态：当前实现基线 + 下一版本规范化建议

## 1. 使用说明

错误信息分为三类：

1. **业务结果码**：通过 JavaScript 回调参数返回，例如 `already_installed`。
2. **诊断错误码**：位于 `chrome.runtime.lastError.message` 末尾，例如 `(webstore_package_invalid)`。
3. **更新状态错误**：由 `checkUpdate()` 返回的 `errorCode` 字段承载。

本文用以下标记区分实现状态：

| 标记 | 含义 |
|---|---|
| 当前 | 当前内核已实现并可能返回 |
| 保留 | Chromium 兼容协议允许，但当前路径不一定主动产生 |
| 建议 | 下一版本应新增或归一化，当前不能依赖 |

当前主要实现文件：

```text
NomadBrowser.Browser/JsBridge/ChromeWebStoreInstallService.cs
NomadBrowser.Browser/JsBridge/ChromeWebStoreUpdateService.cs
NomadBrowser.Browser/JsBridge/ChromeWebStoreUpdateProtocol.cs
NomadBrowser.Browser/JsBridge/ChromeWebStorePackage.cs
NomadBrowser.Browser/JsBridge/StoreCrxPackage.cs
NomadBrowser.Browser/JsBridge/WebStoreInstallFailure.cs
NomadBrowser.Foundation/Plugins/ChromeWebStoreContext.cs
arupa_desktop/plugin/arupa_webstore_error_contract.h
```

## 2. 接口与错误返回通道

| 接口 | 成功返回 | 失败返回 |
|---|---|---|
| `webstorePrivate.getExtensionStatus` | 状态字符串 | `runtime.lastError` |
| `webstorePrivate.beginInstallWithManifest3` | `result === ""` | 业务结果码；系统异常时为 `install_error` + `runtime.lastError` |
| `webstorePrivate.completeInstall` | 无返回值且无 `lastError` | `runtime.lastError` |
| `webstorePrivate.checkUpdate` | `{state, version, errorCode}` | `state === "error"`，原因在 `errorCode` |
| `webstorePrivate.applyUpdate` | `true` | `false`；详细原因保存在更新状态/诊断日志 |
| `management.getAll` | 扩展数组 | `runtime.lastError` |
| `management.uninstall` | 无 `lastError` | `runtime.lastError` |

安装、应用升级和卸载必须由实时用户手势触发。`completeInstall` 不要求第二次用户手势，但必须持有同一 Profile、Document、Origin 下尚未过期的安装确认事务。

## 3. 前端统一解析

### 3.1 提取诊断错误码

当前诊断消息格式：

```text
扩展程序包校验失败。 (webstore_package_invalid)
```

推荐解析函数：

```javascript
export function getKernelErrorCode(error, fallback = 'webstore_install_failed') {
  const message = String(error?.message || '');
  return message.match(/\(([a-z][a-z0-9_]+)\)\s*$/)?.[1] || fallback;
}
```

不要使用完整中文消息做程序判断；中文文案可能调整，稳定契约是括号内错误码。

### 3.2 `beginInstallWithManifest3` 特殊处理

```javascript
chrome.webstorePrivate.beginInstallWithManifest3(details, result => {
  const runtimeError = chrome.runtime.lastError;

  if (runtimeError) {
    const code = getKernelErrorCode(runtimeError);
    // result 通常为 install_error。
    handleInstallError(code);
    return;
  }

  if (result !== '') {
    // result 本身就是业务结果码。
    handleInstallError(result);
    return;
  }

  chrome.webstorePrivate.completeInstall(details.id, () => {
    if (chrome.runtime.lastError) {
      handleInstallError(getKernelErrorCode(chrome.runtime.lastError));
    }
  });
});
```

## 4. 安装业务结果码

`beginInstallWithManifest3` 回调结果：

| 错误码 | 状态 | 含义 | 前端处理 |
|---|:---:|---|---|
| `""` | 当前 | 预检、下载和权限确认成功 | 调用 `completeInstall(id)` |
| `user_gesture_required` | 当前 | 缺少实时用户点击 | 提示用户重新点击 |
| `invalid_id` | 当前 | 扩展 ID 格式错误 | 停止重试，检查后端数据 |
| `already_installed` | 当前 | 扩展已经安装 | 刷新安装状态 |
| `install_in_progress` | 当前 | 已有安装/卸载事务 | 禁用按钮，稍后重试 |
| `unsupported_extension_type` | 当前 | MV、平台能力或策略不支持 | 显示平台不兼容 |
| `manifest_error` | 当前 | 页面 manifest 与实际 CRX manifest 不一致 | 刷新 release 数据，禁止继续提交 |
| `user_cancelled` | 当前 | 用户取消权限确认或事务取消 | 静默恢复按钮 |
| `install_error` | 当前 | 内核异常的通用业务结果 | 从 `runtime.lastError` 提取详细码 |

Chromium 兼容协议保留结果码：

| 错误码 | 状态 | 含义 |
|---|:---:|---|
| `unknown_error` | 保留 | 未分类错误 |
| `feature_disabled` | 保留 | 功能被禁用 |
| `missing_dependencies` | 保留 | 缺少依赖 |
| `blacklisted` | 保留 | 扩展在阻止名单中 |
| `blocked_by_policy` | 保留 | 企业或浏览器策略禁止 |
| `launch_in_progress` | 保留 | 启动事务进行中 |
| `icon_error` | 保留 | 图标处理失败 |
| `invalid_icon_url` | 保留 | 图标地址无效 |
| `blocked_for_child_account` | 保留 | 受儿童账号策略限制 |

## 5. 安装诊断错误码

### 5.1 商店身份与文档

| 错误码 | 状态 | 含义 | 建议处理 |
|---|:---:|---|---|
| `webstore_context_required` | 当前 | 缺少可信商店上下文 | 检查桥接注入和页面来源 |
| `webstore_context_invalid` | 当前 | Origin、Document、Profile 或方法不匹配 | 禁止重试，检查白名单/版本 |
| `webstore_document_unavailable` | 当前 | 页面已关闭、刷新或导航离开 | 刷新后重新操作 |
| `webstore_user_gesture_required` | 当前 | 需要实时用户手势 | 让用户重新点击 |
| `webstore_method_unavailable` | 当前 | 方法不在可信商店白名单 | 检查浏览器版本 |
| `webstore_arguments_invalid` | 当前 | 参数不是合法数组/JSON | 修复前端调用 |
| `webstore_arguments_too_large` | 当前 | 调用参数超过 1 MiB | 缩小 manifest/参数 |
| `webstore_kernel_version_unavailable` | 当前 | 无法取得内核版本 | 重试或升级浏览器 |

### 5.2 下载

| 错误码 | 状态 | 含义 | 建议处理 |
|---|:---:|---|---|
| `webstore_download_failed` | 当前 | 网络、HTTP 或 Nomad 包接口失败 | 检查网络和服务日志 |
| `webstore_download_timeout` | 当前（Google）；Nomad 待归一 | 下载超时 | 重试 |
| `webstore_download_identity_invalid` | 当前 | ID 或内核版本无效 | 检查请求数据 |
| `webstore_download_origin_invalid` | 当前 | 下载域名/Scheme/端口不允许 | 检查 `package_url` 白名单 |
| `webstore_redirect_invalid` | 当前 | 重定向地址缺失或无效 | 检查 CDN |
| `webstore_too_many_redirects` | 当前 | 重定向超过 5 次 | 检查 CDN |
| `webstore_response_invalid` | 当前 | 响应不是完整 HTTP 200 | 检查服务和代理 |
| `webstore_package_too_large` | 当前（Google）；Nomad 待归一 | CRX 超过 128 MiB | 拒绝该版本 |
| `webstore_browser_download_upgrade_required` | 当前兼容码 | 当前内核缺少商店下载通道 | 升级浏览器 |
| `webstore_download_context_expired` | 当前兼容码 | 下载时商店上下文失效 | 重新打开商店页 |

### 5.3 包、事务与确认

| 错误码 | 状态 | 含义 | 建议处理 |
|---|:---:|---|---|
| `webstore_package_invalid` | 当前 | CRX、签名、ID 或包结构无效 | 下架并重新上传 |
| `webstore_transaction_missing` | 当前 | 原生暂存事务缺失 | 重新开始安装 |
| `crx_native_transaction_missing` | 当前 | 内核原生事务不存在 | 重新开始安装 |
| `webstore_previous_confirmation_required` | 当前 | 没有成功的 `beginInstall` 确认 | 重新调用 `beginInstall` |
| `webstore_confirmation_expired` | 当前 | Approval 已过期或已经消费 | 重新开始安装 |
| `webstore_permissions_changed` | 当前 | 确认后权限/能力发生变化 | 重新显示权限确认 |
| `webstore_validation_unavailable` | 当前 | 平台/DNR 校验服务不可用 | 稍后重试 |
| `webstore_activation_failed` | 当前 | 扩展启动失败 | 已回滚；记录诊断并重试 |
| `webstore_commit_failed` | 当前 | 安装状态持久化失败 | 已回滚；检查磁盘/权限 |
| `webstore_cleanup_pending` | 当前兼容码 | 上次事务残留，暂不能清理 | 关闭相关页面后重试 |
| `webstore_cleanup_failed` | 当前 | 回滚/清理失败 | 重启后执行恢复 |
| `webstore_install_cancelled` | 当前 | 安装事务被取消 | 静默恢复 |
| `webstore_install_failed` | 当前 | 未分类安装错误 | 上报诊断日志 |

## 6. DNR 错误码

| 错误码 | 状态 | 含义 | 兼容性解释 |
|---|:---:|---|---|
| `dnr_package_invalid` | 当前 | DNR 包或规则校验失败 | 不能据此直接判定缺少哪项能力 |
| `dnr_kernel_capability_unavailable` | 当前 | 内核缺少所需 DNR 能力 | 可明确判定当前内核不兼容 |
| `dnr_rule_files_too_large` | 当前 | 规则文件总量超限 | 包不符合容量限制 |
| `dnr_rule_file_too_large` | 当前 | 单个规则文件超限 | 包不符合容量限制 |
| `dnr_rule_source_invalid` | 当前 | 规则文件缺失、路径或内容无效 | 包内容问题 |
| `dnr_native_rule_rejected` | 当前 | 原生规则编译器拒绝 | 规则不符合内核策略 |
| `dnr_static_pool_exhausted` | 当前 | 静态规则配额不足 | 设备/全局配额问题 |
| `dnr_state_write_failed` | 当前 | DNR 状态写入失败 | 本地 IO/状态问题 |
| `dnr_redirect_scheme_denied` | 当前 | 重定向 Scheme 不允许 | 规则策略问题 |
| `dnr_service_unavailable` | 当前 | DNR 服务尚未就绪 | 可稍后重试 |

## 7. Nomad `/api/package` HTTP 映射

当前 Nomad/Demo 安装固定请求：

```text
GET {storeOrigin}/api/package?id={extensionId}
```

当前映射：

| 后端/网络结果 | 当前对外结果 | 备注 |
|---|---|---|
| HTTP 200 + 合法 CRX | 继续安装 | 后续仍校验签名、ID、manifest |
| HTTP 400/401/403/404/409/5xx | `webstore_download_failed` | 当前没有按 HTTP 状态细分 |
| 网络连接失败 | `webstore_download_failed` | 通用下载失败 |
| CRX 超过 128 MiB | 通常为 `webstore_download_failed` | 内部 `store_package_too_large` 尚未归一 |
| HttpClient 超时 | 可能返回 `user_cancelled` | 当前与用户取消混淆，需修正 |
| CRX 损坏、签名或 ID 不匹配 | `webstore_package_invalid` | 由原生安装器发现 |
| 页面 manifest 与包内 manifest 不匹配 | `manifest_error` | 不允许继续提交 |

下一版本建议归一：

| 当前内部结果 | 建议对外码 |
|---|---|
| `store_package_too_large` | `webstore_package_too_large` |
| Nomad HttpClient timeout | `webstore_download_timeout` |
| 用户或页面主动取消 | `user_cancelled` |

## 8. 升级状态

`webstorePrivate.checkUpdate(id)` 返回：

```json
{
  "state": "ready",
  "version": "1.2.3",
  "errorCode": null
}
```

| 状态 | 含义 | 是否终态 |
|---|---|:---:|
| `unchecked` | 尚未检查 | 否 |
| `checking` | 检查中 | 否 |
| `downloading` | 下载候选包 | 否 |
| `up_to_date` | 没有更新 | 是 |
| `ready` | 已暂存，可调用 `applyUpdate` | 否 |
| `permission_required` | 新增权限，需要确认 | 否 |
| `deferred` | 扩展正在使用，暂缓应用 | 否 |
| `applying` | 正在应用 | 否 |
| `updated` | 更新完成 | 是 |
| `stale` | 当前安装快照已变化 | 是，需重新检查 |
| `throttled` | 正在执行或被节流 | 否，可读取已有状态 |
| `cancelled` | 检查被取消 | 是 |
| `unsupported` | ID 或来源不支持更新 | 是 |
| `error` | 更新失败 | 是，读取 `errorCode` |

## 9. 升级错误码

| 错误码 | 状态 | 含义 | 建议处理 |
|---|:---:|---|---|
| `webstore_update_id_invalid` | 当前 | 扩展 ID 无效 | 检查目录数据 |
| `webstore_update_version_invalid` | 当前 | 版本格式无效 | 修复目录/包版本 |
| `webstore_update_response_invalid` | 当前 | 更新响应格式错误 | 修复服务响应 |
| `webstore_update_identity_invalid` | 当前 | 更新响应扩展身份不一致 | 拒绝更新并告警 |
| `webstore_update_rejected` | 当前 | 更新服务拒绝 | 检查服务端状态 |
| `webstore_update_package_invalid` | 当前 | 更新包无效 | 下架候选版本 |
| `webstore_update_size_mismatch` | 当前 | 实际大小与 offer 不一致 | 拒绝更新并告警 |
| `webstore_update_hash_mismatch` | 当前 | SHA-256 不一致 | 拒绝更新并告警 |
| `webstore_update_version_mismatch` | 当前 | 包内版本与 offer 不一致 | 拒绝更新 |
| `webstore_update_capability_blocked` | 当前 | 新版本平台能力检查失败 | 保留旧版本 |
| `webstore_update_permissions_changed` | 当前 | 用户确认期间权限变化 | 重新检查并确认 |
| `webstore_update_stale` | 当前 | 已安装状态发生变化 | 删除暂存并重新检查 |
| `webstore_update_pending_invalid` | 当前 | 本地暂存更新无效 | 删除暂存并重新下载 |
| `webstore_update_confirmation_failed` | 当前 | 更新确认事务失败 | 重新检查 |
| `webstore_update_activation_failed` | 当前 | 新版本激活失败 | 回滚旧版本 |
| `webstore_update_commit_failed` | 当前 | 更新保存失败 | 回滚并检查磁盘 |
| `webstore_update_recovery_required` | 当前 | 回滚未完成 | 下次启动恢复 |
| `webstore_update_network_failed` | 当前内部码 | 更新服务网络失败；当前外层通常会归一 | 对外按 `webstore_update_network_or_io_failed` 处理 |
| `webstore_update_network_or_io_failed` | 当前 | 网络或文件 IO 失败 | 重试/检查本地状态 |
| `webstore_update_failed` | 当前 | 未分类升级错误 | 上报诊断日志 |

`applyUpdate(id)` 当前只返回 `true/false`。详细原因保存在更新状态或诊断日志中；建议下一版本让失败回调同时返回稳定 `errorCode`。

## 10. Nomad `/api/catalog` 映射

当前更新检查请求：

```text
GET {storeOrigin}/api/catalog
```

目录项至少需要：

```json
{
  "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "version": "1.2.3",
  "sha256": "<64 lowercase hex>",
  "size": 123456
}
```

当前映射：

| 目录/包结果 | 更新状态/错误 |
|---|---|
| 目录没有该 ID | `up_to_date` |
| 目录版本不高于已安装版本 | `up_to_date` |
| `/api/catalog` HTTP 非 200 | `error / webstore_update_network_or_io_failed` |
| Content-Length 超过 8 MiB | `error / webstore_update_network_or_io_failed` |
| 流式读取超过 8 MiB | `error / store_catalog_too_large` |
| JSON 无法解析 | `error / webstore_update_failed` |
| `version/sha256/size` 无效 | `error / store_offer_invalid` |
| 下载包大小不一致 | `error / webstore_update_size_mismatch` |
| 下载包 SHA-256 不一致 | `error / webstore_update_hash_mismatch` |
| 包内版本不一致 | `error / webstore_update_version_mismatch` |

建议下一版本将 `store_catalog_too_large`、`store_offer_invalid` 统一到 `webstore_update_*` 命名空间。

## 11. 卸载错误码

调用：

```javascript
chrome.management.uninstall(
  extensionId,
  { showConfirmDialog: true },
  callback,
);
```

即使页面传入 `showConfirmDialog: false`，内核仍显示原生卸载确认。

| 场景 | 当前返回 | 建议前端处理 |
|---|---|---|
| 无用户手势 | `webstore_user_gesture_required` | 让用户重新点击 |
| 缺少商店上下文 | `webstore_context_required` | 检查桥接 |
| 商店上下文错误 | `webstore_context_invalid` | 检查 Origin/版本 |
| 页面失效 | `webstore_document_unavailable` | 刷新页面 |
| 确认期间扩展变化 | `webstore_extension_changed` | 刷新安装状态 |
| 原生删除失败 | `webstore_uninstall_failed` | 重试并记录诊断 |
| 用户取消 | 文本 `User cancelled uninstall` | 当前只能按消息处理 |
| 扩展未安装 | 通常归一为 `webstore_uninstall_failed` | 刷新状态 |

下一版本建议增加：

| 建议错误码 | 替代当前结果 |
|---|---|
| `webstore_uninstall_cancelled` | `User cancelled uninstall` |
| `webstore_extension_not_installed` | 归一后的 `webstore_uninstall_failed` |

## 12. 前端文案映射示例

```javascript
export const extensionErrorMessages = {
  user_gesture_required: '请重新点击安装按钮。',
  already_installed: '扩展已经安装。',
  install_in_progress: '另一个扩展操作正在进行，请稍后重试。',
  unsupported_extension_type: '该扩展不适用于当前平台。',
  manifest_error: '商店信息与安装包不一致。',
  user_cancelled: '',

  webstore_context_required: '商店页面未获得浏览器授权。',
  webstore_context_invalid: '商店页面身份校验失败。',
  webstore_document_unavailable: '商店页面已经失效，请刷新后重试。',
  webstore_download_failed: '扩展下载失败，请重试。',
  webstore_download_timeout: '扩展下载超时，请重试。',
  webstore_package_invalid: '扩展安装包校验失败。',
  webstore_package_too_large: '扩展安装包超过大小限制。',
  webstore_confirmation_expired: '安装确认已过期，请重新安装。',
  webstore_permissions_changed: '扩展权限已变化，请重新确认。',
  webstore_activation_failed: '扩展启动失败，安装已回滚。',
  webstore_commit_failed: '扩展保存失败，安装已回滚。',

  dnr_kernel_capability_unavailable: '当前浏览器不支持扩展所需的网络规则能力。',
  dnr_package_invalid: '扩展网络规则校验失败。',

  webstore_uninstall_cancelled: '',
  webstore_extension_not_installed: '扩展尚未安装。',
  webstore_uninstall_failed: '卸载失败，请重试。',
};
```

未知错误统一显示通用失败文案，同时上报稳定错误码、阶段、扩展 ID 和浏览器版本。不要上报 CRX 内容、完整 manifest、用户 Cookie 或服务端响应正文。

## 13. 下一版本建议统一项

| 问题 | 当前行为 | 建议行为 |
|---|---|---|
| Nomad 下载超时 | 可能与 `user_cancelled` 混淆 | `webstore_download_timeout` |
| Nomad 包超限 | 通常归为 `webstore_download_failed` | `webstore_package_too_large` |
| 用户取消卸载 | 返回英文文本 | `webstore_uninstall_cancelled` |
| 卸载未安装扩展 | 通常归为通用失败 | `webstore_extension_not_installed` |
| Catalog 内部码 | `store_catalog_*` / `store_offer_invalid` | 统一 `webstore_update_*` |
| `applyUpdate` 失败 | 只返回 `false` | 同时返回 `{applied, state, errorCode}` |
| release/package URL | 当前未接入 | 定义独立 release、URL、hash、版本错误码 |

在支持 test 域名的版本发布时，应以该版本的最终实现更新本节，并在构建说明中附上新增、删除和兼容错误码清单。

## 14. Release API / `package_url` 建议错误码

本节只适用于计划中的“内核读取 `/api/v1/plugin/release` 并直接下载 `package_url`”方案。当前版本尚未实现，前后端现在不能依赖这些错误码。

### 14.1 Release 元数据请求

| 服务端/校验结果 | 建议错误码 |
|---|---|
| HTTP 400 或响应 Schema 无效 | `webstore_release_invalid` |
| HTTP 401/403 | `webstore_release_forbidden` |
| HTTP 404 或没有可用 release | `webstore_release_not_found` |
| HTTP 408 或请求超时 | `webstore_release_timeout` |
| HTTP 429 | `webstore_release_rate_limited` |
| HTTP 5xx、DNS、TLS 或连接失败 | `webstore_release_unavailable` |
| 返回的 `extension_id` 与请求不一致 | `webstore_release_identity_invalid` |
| `version` 格式无效 | `webstore_release_version_invalid` |
| `package_sha256` 缺失或格式无效 | `webstore_release_hash_invalid` |
| `manifest` 缺失或无法解析 | `webstore_release_manifest_invalid` |

### 14.2 `package_url` 下载与包校验

| 校验结果 | 建议错误码 |
|---|---|
| `package_url` 缺失 | `webstore_package_url_missing` |
| 非 HTTPS、含凭据、端口或域名不在白名单 | `webstore_package_url_invalid` |
| 重定向目标不在白名单 | `webstore_package_redirect_denied` |
| 下载超时 | `webstore_download_timeout` |
| HTTP/网络失败 | `webstore_download_failed` |
| 包超过 128 MiB | `webstore_package_too_large` |
| 实际 SHA-256 与 `package_sha256` 不一致 | `webstore_package_hash_mismatch` |
| CRX 内扩展 ID 与 `extension_id` 不一致 | `webstore_package_identity_mismatch` |
| CRX manifest 版本与 release `version` 不一致 | `webstore_package_version_mismatch` |
| CRX manifest 与 release `manifest` 关键字段不一致 | `manifest_error` |
| CRX3 签名或包结构无效 | `webstore_package_invalid` |

接入该方案时，内核应直接请求 release 元数据或验证服务端签名；不能仅信任普通页面传入的 `package_url`、hash 和 manifest。

