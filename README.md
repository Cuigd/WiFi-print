# WiFi-print

随身 WiFi 小盒子 + WiFi 打印机 + 云端调度的轻量云打印项目。

## 工作流程

```text
客户端上传文件
  -> 云服务器接收任务并转换为打印机可接收格式
  -> MQTT 通知随身 WiFi 小盒子
  -> 小盒子领取任务并下载 print-ready 文件
  -> 小盒子通过 IPP 发送给 WiFi 打印机
  -> 云端记录任务状态
```

核心组件：

- 云端：`cloud/cloud_server.py`
- 云端转换：`cloud/print_pipeline.py`
- 盒子端：`box/box_agent.py`
- 通知：MQTT `cloud-print/{device_id}/jobs`
- 状态：MQTT `cloud-print/{device_id}/status`
- 打印：轻量 IPP `Print-Job`
- 部分 IPP/AirPrint 打印机：PDF 需先转 `image/pwg-raster`

## 目录结构

```text
cloud/                 云端服务和转换流水线
  cloud_server.py      HTTP API、任务调度、设备管理、MQTT 通知
  print_pipeline.py    打印转换 profile，例如 PWG Raster、URF、PCLm
box/
  box_agent.py         随身 WiFi 小盒子端代理
examples/              云端环境变量和盒子配置样例
```

## 云端部署

```bash
cd cloud
export HOST=0.0.0.0
export PORT=8080
export PRINT_UPLOAD_TOKEN=replace-with-long-random-token
export MQTT_ENABLED=true
export MQTT_HOST=127.0.0.1
export MQTT_PORT=1883
export MQTT_USERNAME=replace-with-mqtt-user
export MQTT_PASSWORD=replace-with-mqtt-password
python3 cloud_server.py
```

## 盒子端部署

复制样例配置：

```bash
cp examples/box_config.ipp_pwg.example.json box_config.json
```

修改其中：

- `server_url`
- `device_id`
- `device_token`
- `mqtt.host`
- `mqtt.username`
- `mqtt.password`
- `ipp_printer_uri`

启动：

```bash
python3 box/box_agent.py --config box_config.json
```

盒子首次启动时会读取打印机 IPP 能力，并向云端注册设备。云端返回的 `device_token` 需要写入盒子配置，后续领取任务和下载文件都依赖该 token。

## 打印机适配方式

项目通过 `printer_profile` 适配不同打印机：

- `pdf_passthrough`：打印机支持 PDF 时直传 PDF。
- `pwg_raster`：IPP Everywhere / AirPrint 常见路线。
- `urf`：Apple AirPrint URF。
- `pclm`：Mopria/部分 HP 打印机。
- `raw_passthrough`：已经是打印机可接收格式时透传。
- `hp_laserjet_p1005_xqx`：P1005/P1006 类特殊 XQX 输出示例。

新增打印机时优先新增或覆盖 `cloud/printer_profiles.local.json`，不要把机型逻辑写死到业务接口。

## 云端接口

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

客户端只需要按 `/api/jobs` 的 multipart 表单接口提交文件和打印参数。本仓库不包含前端应用源码。

## MQTT 主题

```text
cloud-print/{device_id}/jobs    云端通知盒子有新任务
cloud-print/{device_id}/status  盒子发布在线/离线状态
```

## 运行数据

云端运行数据保存在：

```text
cloud/storage/
```

该目录包含 SQLite 数据库、上传文件和转换后的 print-ready 文件，已经通过 `.gitignore` 排除。

## 安全要求

- 不要提交真实 `device_token`、MQTT 密码、上传 token。
- 生产环境必须设置 `PRINT_UPLOAD_TOKEN`。
- MQTT 生产环境建议配置每盒一账号、topic ACL，并升级 TLS 或放进专用内网/VPN。
- `cloud/storage/` 是运行数据，不应提交到仓库。
