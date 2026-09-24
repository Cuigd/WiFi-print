# 第一阶段云打印总结（脱敏版）

## 阶段结论

第一阶段已经验证了以下链路：

```text
小程序/电脑上传文件 -> 云服务器接收并转换 -> MQTT 通知盒子 -> 盒子领取任务 -> 轻量 IPP 发送给 WiFi 打印机 -> 打印完成并回传状态
```

已验证的核心能力：

- 局域网 CUPS 打印可用。
- 外网云打印可用。
- 无 CUPS 盒子模式可用。
- 盒子可通过 IPP 自动读取打印机能力。
- 云端可根据打印机能力选择转换 profile。
- MQTT 可替代 HTTP 定时轮询，降低任务延迟。
- 任务状态可在云端记录和查询。

## 通用部署角色

```text
云服务器：运行 cloud/cloud_server.py，负责任务、文件、转换、MQTT 通知和管理台。
随身 WiFi/小盒子：运行 box/box_agent.py，负责订阅 MQTT、领取任务、下载文件和提交打印。
WiFi 打印机：通过 IPP 接收盒子提交的 Print-Job。
小程序/电脑端：上传文件并指定 device_id、份数、纸张等参数。
```

公开仓库只保留示例占位符，不保存真实公网 IP、局域网 IP、设备编号、token、MQTT 密码或本地文件路径。

## 打印机能力识别

盒子通过 IPP `Get-Printer-Attributes` 读取：

```text
printer-name
printer-info
printer-make-and-model
document-format-supported
ipp-features-supported
urf-supported
pwg-raster-document-type-supported
```

当前选择优先级：

```text
application/pdf        -> pdf_passthrough
image/pwg-raster      -> pwg_raster
image/urf             -> urf
application/PCLm      -> pclm
其他或未知             -> raw_passthrough
```

## 云端转换路线

对于不能直接打印 PDF、但支持 PWG Raster 的 IPP/AirPrint 打印机，验证过的成功路线是：

```text
PDF -> Ghostscript CUPS Raster -> rastertopwg -> PWG Raster -> IPP Print-Job
```

`cloud/print_pipeline.py` 中 `pwg_raster` profile 使用：

- `gs -sDEVICE=cups`
- `cupsColorSpace 18`
- `cupsBitsPerColor 8`
- `/usr/lib/cups/filter/rastertopwg`

经验结论：

- 不要默认把 PDF 直接发给打印机，必须先看 `document-format-supported`。
- 某些系统缺少完整 cupsfilter 转换链路时，可以用 Ghostscript 先生成 CUPS Raster，再交给 `rastertopwg`。
- 盒子端尽量只做轻量转发，复杂格式转换放在云端。

## 云端服务接口

```text
GET  /health
GET  /api/devices
GET  /api/devices/{device_id}
GET  /api/devices/{device_id}/jobs
GET  /api/devices/{device_id}/files
GET  /api/printer-profiles
GET  /api/jobs/{job_id}
POST /api/devices/register
POST /api/jobs
POST /api/jobs/{job_id}/status
GET  /api/jobs/{job_id}/file?token=...
```

文件保存结构：

```text
storage/files/{device_id}/{YYYYMMDD}/{job_id}_{original_name}
storage/print_ready/{device_id}/{YYYYMMDD}/{job_id}.{suffix}
```

`cloud/storage/` 是运行数据，不能提交到 GitHub。

## MQTT 主题

```text
cloud-print/{device_id}/jobs    云端通知盒子有新任务
cloud-print/{device_id}/status  盒子发布在线/离线状态
```

建议：

- 测试阶段可先使用账号密码。
- 生产阶段建议每盒一账号。
- 配置 topic ACL，限制盒子只能访问自己的主题。
- 公网部署建议启用 TLS，或放入专用内网/VPN。

## 安全边界

必须脱敏或排除：

- 真实 `device_token`
- MQTT 用户名和密码
- 上传 token
- 真实公网 IP、局域网 IP
- 真实用户文件名和本地路径
- `cloud/storage/` 中的 SQLite、上传文件、转换文件
- `box_printer_cache.json`

生产环境建议：

- 设置 `PRINT_UPLOAD_TOKEN`。
- 小程序上传时提交 `upload_token`。
- 后续增加用户登录、设备绑定、任务签名和限流。

## 测试示例

上传测试：

```bash
curl -F "file=@sample.pdf" \
  -F "user_id=demo" \
  -F "device_id=your-box-id" \
  -F "copies=1" \
  -F "paper_size=A4" \
  -F "upload_token=replace-with-upload-token" \
  http://YOUR_CLOUD_SERVER_IP:8080/api/jobs
```

查询任务：

```bash
curl http://127.0.0.1:8080/api/jobs/<job_id>
```

模拟首次识别：

```bash
systemctl stop cloud-print-box
rm -f /opt/wifi-print-box/box_printer_cache.json
systemctl start cloud-print-box
journalctl -u cloud-print-box -n 80 --no-pager
```

期望日志：

```text
[box] Saved printer profile cache
[box] Printer capability selected pwg_raster (image/pwg-raster)
[box] MQTT connected
```
