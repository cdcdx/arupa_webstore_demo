# 逐风扩展商店 Demo

一个零依赖的 CRX3 扩展目录：默认在本机启动，同时申请随机公网域名，外网设备也能访问。它负责展示 `packages/` 中的扩展包；签名、权限、DNR 规则以及安装/升级事务仍由逐风浏览器的原生安装器验证。

完整的分层架构、两条安装链路、进度协议、HTTP 接口、更新与卸载、部署排查及源码索引见 [逐风扩展商店 Demo 技术文档](docs/逐风扩展商店Demo技术文档.md)。当前已复用服务级目录缓存，并从经过摘要校验的同一文件句柄流式取包；桌面、Android 和 iOS 包统一展示，平台信息仅作提示。

## 一键启动

项目只要求 Python 3.9 或更高版本，不需要安装第三方依赖。

只保留 `start.sh`、`start.command`、`start.bat` 三个入口，启停操作通过子命令选择。macOS 双击 `start.command` 即可启动并打开页面；它也接受与 `start.sh` 相同的参数。

| 操作 | macOS / Linux 终端 | Windows |
| --- | --- | --- |
| 启动并打开页面 | `./start.sh` | `start.bat` |
| 查看运行状态与访问地址 | `./start.sh status` | `start.bat status` |
| 重启并打开页面 | `./start.sh restart` | `start.bat restart` |
| 停止商店及其隧道 | `./start.sh stop` | `start.bat stop` |

启动脚本在后台运行服务，并打开 `http://127.0.0.1:18731/`；关闭终端不会停止服务。页面显示本机、局域网和当前公网地址，隧道就绪后也可运行 `./start.sh status` 查看。重复启动会复用现有实例和公网域名；同时运行两个启动脚本也只会启动一个服务。

重启默认沿用上次的包目录、隧道开关、限速与浏览器设置；额外参数覆盖对应设置。运行中的配置如有变化，会提示使用 `./start.sh restart`，不会悄悄忽略新参数。停止可以重复执行；脚本校验服务实例和本机控制凭据，不会根据过期 PID 文件或端口号终止其他进程。

```bash
./start.sh --no-open --no-tunnel  # 后台启动，不打开浏览器
./start.sh status
./start.sh restart --no-open --tunnel  # 保留其他设置，开启公网隧道
./start.sh stop
```

状态与日志位于 `.runtime/`（已加入 `.gitignore`）：`state.json` 保存实例和配置，`server.log` 保存启动、请求及隧道日志。控制凭据只保存在本机状态文件中，不通过网页接口返回。不要在服务运行时删除该目录。

如端口被旧版前台服务占用，先在原终端按 Ctrl+C，再运行 start；脚本会报告占用，不强行接管。用于调试的 `python3 server.py` 仍以前台运行，Ctrl+C / SIGTERM 会清理隧道；这类前台实例不归启停脚本管理。

## 随机域名访问（隧道，默认开启）

启动时会在后台启动 Cloudflare 快速隧道，先保证本地可用，域名就绪后再打印：

```bash
./start.sh
python3 server.py
```

- 首次运行会自动下载 cloudflared 到 `.bin/`（约 40 MB，已加入 `.gitignore`），之后复用；删除 `.bin/` 即可重新下载。
- 每次真正重启服务都会申请新域名；重复 start 不更换域名。停止时先关闭隧道再释放服务端口。
- 申请失败（离线、网络受限）只打印提示，本地与局域网访问不受影响，不会退出。
- 只有隧道连接注册成功后才公布公网地址；隧道进程退出后会清除失效地址，并在页面与 status 中报告失败。重启可重新申请。
- 拿到域名的人都能读到插件目录并下载 CRX，但安装、卸载、升级仍需逐风浏览器，请勿在公网分享含敏感包的目录。

只需要本机、局域网访问时，加上 `--no-tunnel`：

```bash
./start.sh --no-tunnel
python3 server.py --no-tunnel
```

start/restart 支持 `--packages-dir`、`--package-delay-ms`、`--package-without-length`、`--tunnel`、`--no-tunnel`、`--browser`、`--no-open`。`--port` 和 `--runtime-dir` 可用于隔离测试实例；本机 `webstorePrivate` 安装仍应使用默认端口 18731。当前没有 `--no-package-without-length` 反向参数。

默认浏览器可以查看目录，但安装、卸载和升级按钮需要包含本 Demo 改动的逐风浏览器。要让一键脚本直接启动逐风浏览器，可先设置其可执行文件路径：

```bash
./start.sh --browser "/path/to/arupa-browser"
```

`--browser` 当前需要可执行文件路径；macOS 应填写 `.app/Contents/MacOS/NomadBrowser`，不能直接填写 `.app` 目录。首次启动可用 `ARUPA_BROWSER` 环境变量提供默认值；之后保存已选设置。启动器会自动传入 `--apps-gallery-url=http://127.0.0.1:18731/` 并打开 Demo 页面。也可以前台执行：

```bash
python3 server.py --open
python3 server.py --open --browser /path/to/arupa-browser
```

验证安装页的进度时，可以用 `python3 server.py --package-delay-ms 450` 启动限速 Demo。追加 `--package-without-length` 可观察总大小未知时的显示。`--packages-dir` 可指向只含测试包的独立目录。使用逐风 Test 构建打开 `http://127.0.0.1:18731/`。

## 试用目录与升级

### 公网 HTTPS 安装

随机公网域名不在 `webstorePrivate` 的授权范围内。新版逐风提供 `chrome.nomadSignedExtensions`，Demo 对带有 `META-INF/nomad-package.json` / `.sig`（兼容 MiVPN 的旧命名）元数据的包，自动使用这一通道。普通 Chrome、旧版逐风以及只有 CRX3 作者签名的包均不能通过这个公网通道安装，页面会显示原因。

安装由点击直接触发 `install({ extensionId, packageUrl, expectedSha256 }, callback)`。包 URL 与页面同源，附带平台与 SHA-256，成功回执核对 ID、product 和版本，再通过 `getStatus` 确认已安装状态。状态查询的 product、version 为可选信息；刷新时不要求已安装产品标识与商店当前包一致，缺少版本时显示“已安装（版本未知）”。卸载使用同一命名空间的 `uninstall`，浏览器保留原生确认。包内元数据只是路由提示，发布签名和资源摘要仍必须由浏览器验证；商店不会给普通 CRX 伪造发布签名。

新版 `chrome.nomadSignedExtensions.install()` 支持可选 `operationId`（每次操作新建一个非零 UUID，例如 `crypto.randomUUID()`）。在用户点击中直接传给 install，然后使用 `chrome.nomadSignedExtensions.getOperationProgress(operationId, callback)` 每 300 毫秒查询。install 回调仍返回最终结果；`getStatus()` 仍查询已安装/未安装/停用状态。

查询返回 `{ progress?: { operationId, operation: 'install', extensionId, stage, receivedBytes, totalBytes?, errorCode? } }`。下载字节来自浏览器宿主实际读取的包数据；总大小未知时省略 totalBytes，Demo 显示已接收字节和不定进度。仅 downloading 阶段计算百分比；100% 表示下载完毕，随后仍需验签、兼容性预检、权限确认、安装和提交。失败/取消发生回滚时，先显示 rolling_back，再进入 failed/cancelled，只有 completed 和 install 成功回调表示安装完成。

任务绑定 Profile、来源和发起文档；其他来源、标签页、刷新后的文档查询不到旧任务，隐身页面不可用。不存在、过期或不属于当前文档的任务返回 `{}`；终态最长保留两分钟，缓存最多 128 条，必要时提前清除较早终态。不要复用 operationId。查询不需要额外用户点击，但安装和卸载仍需真实用户点击及原生确认。

本 Demo 的签名通道使用 `nomadSignedExtensions.getOperationProgress()`，本机普通目录通道使用 `webstorePrivate.getOperationProgress()`，两者按实际安装通道独立查询。旧版没有签名进度方法时继续等待最终回调，不传 operationId；查询失败也不会终止安装。浏览器顶部/标签加载条仍来自页面导航事件，与扩展安装进度无关。

安装页显示“下载 → 校验 → 安装”三步阶段条：取包对应下载，验包与兼容性检查对应校验，权限确认、安装与提交对应安装；等待权限时明确提示确认后才会安装。轮询可能跳过短暂阶段。成功、失败和取消均保留原回调结果；只有最终回调成功并完成回执核对后，阶段条才显示完成。旧版浏览器不模拟阶段或百分比。目录通道准备升级包时只完成前两步，点击“升级”后才进入安装。

阶段条在安装前也保持可见，点击安装后自动滚动到该区域，避免在列表下方操作时看不到进度。页面会提示当前安装通道是否缺少进度接口；进度查询报错也会显示原因，安装仍等待原回调。局域网 HTTP 测试页没有 `crypto.randomUUID()` 时，Demo 使用 `crypto.getRandomValues()` 生成 UUID v4，不会因此关闭已有的进度查询能力。仅刷新网站不会给旧版浏览器增加原生进度接口。

例如 AI 搜片独立安装页还适配了 `window.nomad.installation` 的 `onProgress` / `list` 任务协议；它与 `chrome.nomadSignedExtensions` 是不同的接口。该协议在目标浏览器中的可用性和任务字段须单独确认，本 Demo 尚未接入。

签名插件的升级入口已在商店侧接入：目录版本高于已安装版本，且浏览器实际提供 `chrome.nomadSignedExtensions.update(details, callback)` 时显示“升级”。参数沿用 install 的 extensionId、同源 packageUrl、expectedSha256 和可选 operationId，进度通过原 getter 查询，但 operation 必须为 `update`。成功回调仍须返回完整的 extensionId/product/version，网页核对后再刷新状态；失败和取消保留原回调，不自动卸载或降级调用 install。已安装版本未知、同版本或浏览器版本更高时不提供升级操作。

**交付边界（2026-11-26）：本轮仅完成商店接入。当前核对的桌面宿主和内核尚未提供 update 方法，仍需浏览器团队实现并打包交付。** 缺少方法的浏览器显示“当前浏览器尚不支持签名插件原地升级”；仅刷新商店不能增加浏览器能力。宿主需实现有回滚的同 ID 升级、版本/签名/权限校验及数据保留，内核需开放 schema、校验与转发，详见 `/docs` 中的待实现合同。网页测试使用模拟桥验证此合同，不代表原生升级已经完成。

目录和下载不再按平台过滤，支持 `platform=desktop|android|ios`（默认 desktop）作为客户端提示。网页请求 `/api/catalog?platform=...&all=1`，**每个 CRX 文件独立一行**，同 ID 的多个版本、同版本的多个文件均保留。每行展示文件名、ID、版本和自己的下载链接；签名安装/升级使用 `id + file + sha256` 精确选包。同 ID 共用安装状态，浏览器只能保留一个已安装版本，卸载作用于该 ID。

为兼容现有浏览器，默认 `/api/catalog` 仍每个 ID 返回一个最高版本，`/api/package?id=...` 仍取默认包；版本等价时按文件名排序选第一个。`all=1` 返回的 `isLatest` 标记该默认包。普通目录通道无法传文件选择器，因此非默认文件仅提供下载，不显示会取错包的安装/升级按钮；签名通道可按文件选择，同版/旧版不会提供降级升级按钮。`platforms` 仍只作提示，iOS 包也保留展示；实际安装仍由浏览器验证。

“下载 CRX”走 `download=1` 附件响应：application/octet-stream，加 Content-Disposition 和 UTF-8 文件名。点击后页面提示查看浏览器下载列表，实际保存位置、进度和取消由浏览器管理；该提示不表示下载完成，也不进入安装进度条。安装和升级 URL 不带此参数，保留原 CRX 响应。两种响应都保持精确选包、摘要和 Range 支持。若逐风没有出现下载任务，还需核对其原生下载处理；HTTP 200 只能证明服务端响应成功。

### 本机目录通道

1. 将需要测试的 **CRX3** 文件放入 `packages/`。项目附带自签名示例 `nomad-demo-1.0.crx`。
2. 启动 Demo，在逐风浏览器中点击“安装”。
3. 保留 `packages/nomad-demo-1.0.crx`，同时把 `samples/nomad-demo-1.1.crx` 复制到 `packages/`。
4. 刷新页面，可见同 ID 的两行文件；在 1.1 默认包一行依次点击“准备升级”和“升级”。

两个示例版本使用同一临时作者密钥，插件 ID 相同。`make_sample.py` 可以重新生成示例，但每次生成会创建新的密钥和插件 ID，因此新生成的 1.1 不能升级旧生成的 1.0。

## 项目结构

```text
crx_catalog.py  CRX3 身份、版本、引擎平台与按文件变化失效的元数据索引
server.py       HTTP 路由、共享目录索引、文件流式取包、本机控制与运行状态
local_tunnel.py 隧道启动、连接状态、退出检测与清理
storectl.py     跨平台启停、配置保存、实例校验与并发锁
app.js          浏览器特权 API 调用与页面状态管理
index.html      目录页面
api.html        安装、升级、卸载接口文档
start.*         唯一命令入口；子命令 start / stop / restart / status
.runtime/       本机运行状态和日志（不提交）
packages/       当前对外展示的 CRX3
samples/        不直接展示的升级示例
```

每个 HTTP 服务实例持有同一个 `CatalogIndex`，目录与下载请求复用解析结果；文件替换、修改、删除后失效，无效包也会在变化后重新检查。首次解析包仍会整包读入，缓存只保存元数据。取包先以不超过 64 KiB 的读取校验大小和 SHA-256，再定位同一打开文件并分块发送，不再把完整包体加载到发送缓冲。发布文件应使用临时文件加原子替换，已打开的下载保持原文件；检测到原地改写或截断时中止响应。限速约在八段响应包体位置等待，避免大包因每个 64 KiB 块都延迟而超时。

`GET /api/package` 支持单段 HTTP 断点续传：`Range: bytes=N-`、`bytes=N-M`、`bytes=-N` 返回 `206`，带准确的 `Content-Range` 与分段 `Content-Length`；越界返回 `416` 和 `Content-Range: bytes */完整大小`。整包及分段响应提供 `Accept-Ranges: bytes` 和以 SHA-256 为值的强 `ETag`。续传可携带原响应的 `If-Range` 强 ETag；不匹配、弱 ETag 或日期值会回退整包 `200`，客户端必须替换旧临时文件，不能直接追加。畸形、多段及未知单位的 Range 也回退 `200`；旧 sha256 查询与当前包冲突仍返回 `409`。`HEAD` 返回整包头信息、不发送包体、忽略 Range。未知大小模式只省略整包响应的 Content-Length，`206` 仍返回分段长度。详细请求示例见 `/docs`。

服务不接受上传，没有安装、升级或卸载的 HTTP 写接口。唯一生命周期写接口 `POST /_control/stop` 要求回环 Host、本机控制凭据，且拒绝网页 Origin / Sec-Fetch-Site；当前没有额外检查 TCP 客户端地址。Host 端口校验和 `Sec-Fetch-Site` 检查继续约束只读请求。

页面安装、卸载和升级互斥执行。目录请求或浏览器状态读取有超时；目录失败时保留旧列表供查看，但关闭操作入口，刷新成功后恢复。安装/卸载原生结果回调没有被网页超时替代，成功后仍重新读取状态。公网签名通道与本机目录通道保持各自的能力边界。

目录对大小受限的默认启用 DNR 规则做只读扫描，只提供响应头匹配相关的排查线索。扫描结果不能代替浏览器内核校验；只有浏览器明确返回 `dnr_kernel_capability_unavailable` 时，页面才提示能力缺失。

完整的 HTTP 与浏览器接口说明见启动后的 [接口文档](http://127.0.0.1:18731/docs)，包括参数与返回字段、两条安装通道、UUID 进度查询、错误处理和可复制的前端示例。文档提供章节导航，并适配手机宽度。

## 桌面 / Android 商店方案

- Edge Android 当前扩展入口、WebUI/Mojo 架构、推荐与搜索、CRX 下载和原生安装逻辑，见 [Edge Android 扩展实现调研](docs/Edge安卓扩展实现调研.md)。
- Nomad 自建商店、扩展 API 能力检测、Android/Desktop 适用标签、后台管理和浏览器侧安装/升级/卸载设计，见 [Nomad 扩展商店自建需求设计](docs/Nomad扩展商店自建需求设计.md)。
- Nomad Desktop 安装、升级、卸载的返回通道、状态与错误码映射，见 [Nomad 扩展安装、升级与卸载错误码](docs/Nomad扩展安装升级卸载错误码.md)。

## 验证

```bash
python3 -m unittest -v
node test_app.cjs          # 可选，需要 Node.js
python3 -m py_compile server.py storectl.py local_tunnel.py crx_catalog.py make_sample.py
bash -n *.sh *.command    # macOS / Linux
```

## MiVPN CRX 的安装身份

旧 MiVPN 包的固定安装 ID 是 `objojdkhlcekcnhaeoebojmffolmafll`；当前目录中的新包使用 `hcjdpccaikglmijenhghaddadbmmhgea`，按实际包身份读取。旧版已签名 MiVPN 资源可以用另一把
RSA 私钥包装成 CRX3；这时外层 CRX ID 与安装 ID 不同。目录读取 manifest 的固定身份和
`META-INF/mivpn-package.json` / `.sig` 元数据后，以固定安装 ID 提供目录和包下载。
这些元数据只用于选择安装身份，不代表验签通过；浏览器仍验证外层 CRX3 签名、包内
Ed25519 签名、资源摘要和当前平台引擎。普通扩展继续使用外层 CRX ID。

不要为了匹配外层密钥而修改 MiVPN 的 manifest.key，也不要删除内层签名。
替换 CRX 文件后刷新页面即可；修改 Python 服务代码后需要重启服务。
