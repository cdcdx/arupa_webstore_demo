# 逐风扩展商店 Demo

一个零依赖、仅监听本机回环地址的 CRX3 扩展目录。它负责展示 `packages/` 中的扩展包；签名、权限、DNR 规则以及安装/升级事务仍由逐风浏览器的原生安装器验证。

## 一键启动

项目只要求 Python 3.9 或更高版本，不需要安装第三方依赖。

- macOS：双击 `start.command`，或在终端运行 `./start.sh`。
- Linux：运行 `./start.sh`。
- Windows：双击 `start.bat`。

脚本会启动 `http://127.0.0.1:18731/` 并自动打开页面。终端需保持运行；按 `Ctrl+C` 停止服务。重复运行启动脚本时，如果 Demo 已经启动，只会再次打开页面。

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

1. 将需要测试的 **CRX3** 文件放入 `packages/`。项目附带自签名示例 `nomad-demo-1.0.crx`。
2. 启动 Demo，在逐风浏览器中点击“安装”。
3. 将 `packages/nomad-demo-1.0.crx` 移出目录，再把 `samples/nomad-demo-1.1.crx` 复制到 `packages/`。
4. 刷新页面，依次点击“准备升级”和“升级”。

两个示例版本使用同一临时作者密钥，插件 ID 相同。`make_sample.py` 可以重新生成示例，但每次生成会创建新的密钥和插件 ID，因此新生成的 1.1 不能升级旧生成的 1.0。

## 项目结构

```text
crx_catalog.py  CRX3 身份、清单、版本及只读诊断扫描
server.py       本地 HTTP 路由、Host 校验、健康检查和页面启动
app.js          浏览器特权 API 调用与页面状态管理
index.html      目录页面
api.html        安装、升级、卸载接口文档
start.*         macOS、Linux、Windows 一键启动入口
packages/       当前对外展示的 CRX3
samples/        不直接展示的升级示例
```

服务只读取目录并提供包字节，不接受上传，也没有安装、升级或卸载的 HTTP 写接口。固定的 Host 和 `Sec-Fetch-Site` 检查用于降低本机其他网页借用该服务的风险。`/api/package` 在返回前会再次核对大小和 SHA-256，避免扫描后文件被替换。

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

MiVPN 的固定安装 ID 是 `objojdkhlcekcnhaeoebojmffolmafll`。已签名 MiVPN 资源可以用另一把
RSA 私钥包装成 CRX3；这时外层 CRX ID 与安装 ID 不同。目录读取 manifest 的固定身份和
`META-INF/mivpn-package.json` / `.sig` 元数据后，以固定安装 ID 提供目录和包下载。
这些元数据只用于选择安装身份，不代表验签通过；浏览器仍验证外层 CRX3 签名、包内
Ed25519 签名、资源摘要和当前平台引擎。普通扩展继续使用外层 CRX ID。

不要为了匹配外层密钥而修改 MiVPN 的 manifest.key，也不要删除内层签名。
替换 CRX 文件后刷新页面即可；修改 Python 服务代码后需要重启服务。
