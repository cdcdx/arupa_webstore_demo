# 逐风扩展商店 Demo

一个零依赖的 CRX3 扩展目录：默认在本机启动，同时申请随机公网域名，外网设备也能访问。它负责展示 `packages/` 中的扩展包；签名、权限、DNR 规则以及安装/升级事务仍由逐风浏览器的原生安装器验证。

## 一键启动

项目只要求 Python 3.9 或更高版本，不需要安装第三方依赖。

- macOS：双击 `start.command`，或在终端运行 `./start.sh`。
- Linux：运行 `./start.sh`。
- Windows：双击 `start.bat`。

脚本会启动 `http://127.0.0.1:18731/` 并自动打开页面，同时在后台申请一个随机的 `https://xxx.trycloudflare.com` 域名并打印出来，外网设备用该域名即可访问。终端需保持运行；按 `Ctrl+C` 停止服务。重复运行启动脚本时，如果 Demo 已经启动，只会再次打开页面。

## 随机域名访问（隧道，默认开启）

启动时会在后台启动 Cloudflare 快速隧道，先保证本地可用，域名就绪后再打印：

```bash
./start.sh
python3 server.py
```

- 首次运行会自动下载 cloudflared 到 `.bin/`（约 40 MB，已加入 `.gitignore`），之后复用；删除 `.bin/` 即可重新下载。
- 域名每次启动都会变化，隧道随进程退出而关闭，适合临时给其他设备或同事演示。
- 申请失败（离线、网络受限）只打印提示，本地与局域网访问不受影响，不会退出。
- 拿到域名的人都能读到插件目录并下载 CRX，但安装、卸载、升级仍需逐风浏览器，请勿在公网分享含敏感包的目录。

只需要本机、局域网访问时，加上 `--no-tunnel`：

```bash
./start.sh --no-tunnel
python3 server.py --no-tunnel
```

启动脚本会把多余参数透传给 `server.py`，例如 `./start.sh --no-tunnel --package-delay-ms 450`。

默认浏览器可以查看目录，但安装、卸载和升级按钮需要包含本 Demo 改动的逐风浏览器。要让一键脚本直接启动逐风浏览器，可先设置其可执行文件路径：

```bash
ARUPA_BROWSER="/path/to/arupa-browser" ./start.sh
```

启动器会自动传入 `--apps-gallery-url=http://127.0.0.1:18731/` 并打开 Demo 页面。也可以手动执行：

```bash
python3 server.py --open
python3 server.py --open --browser /path/to/arupa-browser
```

验证安装页的进度时，可以用 `python3 server.py --package-delay-ms 450` 启动限速 Demo。追加 `--package-without-length` 可观察总大小未知时的显示。`--packages-dir` 可指向只含测试包的独立目录。使用逐风 Test 构建打开 `http://127.0.0.1:18731/`。

## 试用目录与升级

### 公网 HTTPS 安装

随机公网域名不在 `webstorePrivate` 的授权范围内。新版逐风提供 `chrome.nomadSignedExtensions`，Demo 对带有 `META-INF/nomad-package.json` / `.sig`（兼容 MiVPN 的旧命名）元数据的包，自动使用这一通道。普通 Chrome、旧版逐风以及只有 CRX3 作者签名的包均不能通过这个公网通道安装，页面会显示原因。

安装由点击直接触发 `install({ extensionId, packageUrl, expectedSha256 }, callback)`。包 URL 与页面同源，附带平台与 SHA-256，安装后通过 `getStatus` 确认 ID、product 和版本。卸载使用同一命名空间的 `uninstall`，浏览器保留原生确认。包内元数据只是路由提示，发布签名和资源摘要仍必须由浏览器验证；商店不会给普通 CRX 伪造发布签名。

这条签名通道目前没有取包进度或 `checkUpdate` 接口，网页只显示取包/验签提示并等待原结果回调；不能套用下面本机通道的百分比。已安装签名扩展出现新版时，页面提示手动卸载后重装，不自动卸载。

目录与下载均按 `platform=desktop|android` 选包（默认 desktop）。MiVPN 依据包内实际引擎区分平台，先过滤平台再选同 ID 的最高版本，避免把 Android 0.0.48 送到桌面端而漏掉桌面 0.0.34。通用签名扩展默认列在两个平台，最终兼容性由浏览器校验。

### 本机目录通道

1. 将需要测试的 **CRX3** 文件放入 `packages/`。项目附带自签名示例 `nomad-demo-1.0.crx`。
2. 启动 Demo，在逐风浏览器中点击“安装”。
3. 将 `packages/nomad-demo-1.0.crx` 移出目录，再把 `samples/nomad-demo-1.1.crx` 复制到 `packages/`。
4. 刷新页面，依次点击“准备升级”和“升级”。

两个示例版本使用同一临时作者密钥，插件 ID 相同。`make_sample.py` 可以重新生成示例，但每次生成会创建新的密钥和插件 ID，因此新生成的 1.1 不能升级旧生成的 1.0。

## 项目结构

```text
crx_catalog.py  CRX3 身份、清单、版本及只读诊断扫描
server.py       本地 HTTP 路由、Host 校验、健康检查、页面启动和随机域名隧道
app.js          浏览器特权 API 调用与页面状态管理
index.html      目录页面
api.html        安装、升级、卸载接口文档
start.*         macOS、Linux、Windows 一键启动入口
packages/       当前对外展示的 CRX3
samples/        不直接展示的升级示例
```

服务只读取目录并提供包字节，不接受上传，也没有安装、升级或卸载的 HTTP 写接口。Host 端口校验和 `Sec-Fetch-Site` 检查用于降低本机其他网页借用该服务的风险；从外部链接直接打开页面（顶层文档导航）仍可正常访问。`/api/package` 在返回前会再次核对大小和 SHA-256，避免扫描后文件被替换。

目录对大小受限的默认启用 DNR 规则做只读扫描，只提供响应头匹配相关的排查线索。扫描结果不能代替浏览器内核校验；只有浏览器明确返回 `dnr_kernel_capability_unavailable` 时，页面才提示能力缺失。

完整的 HTTP 与浏览器接口说明见启动后的 [接口文档](http://127.0.0.1:18731/docs)。

## 桌面 / Android 商店方案

- Edge Android 当前扩展入口、WebUI/Mojo 架构、推荐与搜索、CRX 下载和原生安装逻辑，见 [Edge Android 扩展实现调研](docs/Edge安卓扩展实现调研.md)。
- Nomad 自建商店、扩展 API 能力检测、Android/Desktop 适用标签、后台管理和浏览器侧安装/升级/卸载设计，见 [Nomad 扩展商店自建需求设计](docs/Nomad扩展商店自建需求设计.md)。
- Nomad Desktop 安装、升级、卸载的返回通道、状态与错误码映射，见 [Nomad 扩展安装、升级与卸载错误码](docs/Nomad扩展安装升级卸载错误码.md)。

## 验证

```bash
python3 -m unittest -v
node test_app.cjs          # 可选，需要 Node.js
python3 -m py_compile server.py crx_catalog.py make_sample.py
```

## MiVPN CRX 的安装身份

旧 MiVPN 包的固定安装 ID 是 `objojdkhlcekcnhaeoebojmffolmafll`；当前目录中的新包使用 `hcjdpccaikglmijenhghaddadbmmhgea`，按实际包身份读取。旧版已签名 MiVPN 资源可以用另一把
RSA 私钥包装成 CRX3；这时外层 CRX ID 与安装 ID 不同。目录读取 manifest 的固定身份和
`META-INF/mivpn-package.json` / `.sig` 元数据后，以固定安装 ID 提供目录和包下载。
这些元数据只用于选择安装身份，不代表验签通过；浏览器仍验证外层 CRX3 签名、包内
Ed25519 签名、资源摘要和当前平台引擎。普通扩展继续使用外层 CRX ID。

不要为了匹配外层密钥而修改 MiVPN 的 manifest.key，也不要删除内层签名。
替换 CRX 文件后刷新页面即可；修改 Python 服务代码后需要重启服务。
