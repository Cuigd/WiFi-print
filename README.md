# WiFi-print

随身 WiFi 小盒子 + WiFi 打印机 + 云端调度的轻量云打印项目。

## 已验证链路

```text
微信小程序/电脑上传文件
  -> 云服务器接收任务并转换为打印机可接收格式
  -> MQTT 通知随身 WiFi 小盒子
  -> 小盒子领取任务并下载 print-ready 文件
  -> 小盒子通过 IPP 发送给 WiFi 打印机
  -> 云端记录任务状态
```

当前已验证的成功路线是：

- 云端：`cloud/cloud_server.py`
- 云端转换：`cloud/print_pipeline.py`
- 盒子端：`box/box_agent.py`
- 通知：MQTT `cloud-print/{device_id}/jobs`
- 状态：MQTT `cloud-print/{device_id}/status`
- 打印：轻量 IPP `Print-Job`
- 部分 IPP/AirPrint 打印机：PDF 需先转 `image/pwg-raster`

## 目录结构

```text
cloud/                 云端服务、转换流水线和管理台
  cloud_server.py      HTTP API、任务调度、设备管理、MQTT 通知
  print_pipeline.py    打印转换 profile，例如 PWG Raster、URF、PCLm
  admin/dashboard.html 云端管理台
box/
  box_agent.py         随身 WiFi 小盒子端代理
miniprogram/           微信小程序上传入口
docs/                  部署说明、设备管理、MQTT 安全和阶段总结
examples/              云端环境变量和盒子配置样例
```

## 快速启动云端

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

管理台：

```text
http://YOUR_CLOUD_SERVER_IP:8080/admin
```

## 快速启动盒子端

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

## 打印机适配方式

项目通过 `printer_profile` 适配不同打印机：

- `pdf_passthrough`：打印机支持 PDF 时直传 PDF。
- `pwg_raster`：IPP Everywhere / AirPrint 常见路线。
- `urf`：Apple AirPrint URF。
- `pclm`：Mopria/部分 HP 打印机。
- `raw_passthrough`：已经是打印机可接收格式时透传。
- `hp_laserjet_p1005_xqx`：P1005/P1006 类特殊 XQX 输出示例。

新增打印机时优先新增或覆盖 `cloud/printer_profiles.local.json`，不要把机型逻辑写死到业务接口。

## 安全注意

- 不要提交真实 `device_token`、MQTT 密码、上传 token。
- 生产环境必须设置 `PRINT_UPLOAD_TOKEN`。
- MQTT 生产环境建议配置每盒一账号、topic ACL，并升级 TLS 或放进专用内网/VPN。
- `cloud/storage/` 是运行数据，不应提交到仓库。

## 参考文档

- [外网打印部署说明](docs/EXTERNAL_PRINTING_GUIDE.md)
- [设备管理和 profile 扩展](docs/CLOUD_DEVICE_MANAGEMENT.md)
- [MQTT 安全计划](docs/MQTT_SECURITY_PLAN.md)
- [第一阶段云打印总结](docs/PHASE_1_CLOUD_PRINT_SUMMARY.md)
