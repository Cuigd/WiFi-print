# 云端设备管理与打印适配说明

## 设备管理目标

云端按盒子编号 `device_id` 管理设备。每个盒子对应一个家庭中的打印机，盒子首次联网后会读取打印机能力并注册到云端。

云端保存：

- 盒子编号、名称、在线状态、最近请求时间。
- 打印机能力 `printer_capabilities`。
- 当前选中的 `printer_profile`。
- 该设备上传的原始文件和转换后的 print-ready 文件。
- 该设备的最近任务状态。

## 文件目录

原始上传文件：

```text
storage/files/{device_id}/{YYYYMMDD}/{job_id}_{original_name}
```

转换后的打印文件：

```text
storage/print_ready/{device_id}/{YYYYMMDD}/{job_id}.{suffix}
```

这样后期可以按设备、按日期清理：

```bash
rm -rf storage/files/your-box-id/20260919
rm -rf storage/print_ready/your-box-id/20260919
```

## 常用 API

查看全部设备：

```http
GET /api/devices
```

查看单个设备：

```http
GET /api/devices/{device_id}
```

查看设备最近任务：

```http
GET /api/devices/{device_id}/jobs
```

查看设备文件：

```http
GET /api/devices/{device_id}/files
```

查看转换 profile：

```http
GET /api/printer-profiles
```

## 在线状态

启用 MQTT 后，盒子连接 broker 时发布 `online` 状态，断线时由 MQTT 遗嘱发布 `offline` 状态。云端服务订阅状态主题并更新 `last_seen_at`、`last_request_path`、`last_request_ip`。

```text
cloud-print/{device_id}/status
```

未启用 MQTT 时，盒子请求云端任务接口或回传打印状态时会更新这些字段。

默认 `30` 秒内有轮询请求视为在线，可用环境变量调整：

```bash
DEVICE_ONLINE_WINDOW_SECONDS=60
```

启用 MQTT 时，在线状态优先使用盒子发布的 `online/offline`，不依赖固定轮询。

## MQTT 任务通知

云端收到小程序上传并转换成功后，会发布任务通知：

```text
cloud-print/{device_id}/jobs
```

盒子收到通知后再调用云端任务接口领取任务、下载转换后的打印文件并打印。这样平时只有 MQTT 长连接，不再每隔几秒请求一次云端。

云端环境变量示例：

```bash
MQTT_ENABLED=true
MQTT_HOST=127.0.0.1
MQTT_PORT=1883
MQTT_TOPIC_PREFIX=cloud-print
```

测试阶段可以把 MQTT 用户名和密码明文写入盒子配置。云端服务也通过 `MQTT_USERNAME`、`MQTT_PASSWORD` 连接 broker，用于发布任务通知和订阅盒子在线状态。

后期产品化改为“每盒一密钥 + 云端管理 + 可重置”，详见 `MQTT_SECURITY_PLAN.md`。

## 打印适配策略

盒子读取打印机的 `document-format-supported` 后，按优先级选择云端转换方式：

```text
application/pdf       -> pdf_passthrough
image/pwg-raster     -> pwg_raster
image/urf            -> urf
application/PCLm      -> pclm
其他                 -> raw_passthrough
```

GDI、私有 host-based 打印机暂不支持。

## 新增或修改打印机适配

优先不要改上传接口。新增适配时修改或新增 profile：

```text
cloud/print_pipeline.py
cloud/printer_profiles.local.json
```

建议生产环境使用 `printer_profiles.local.json` 覆盖或新增 profile，避免直接改内置代码。

示例：

```json
{
  "profiles": [
    {
      "profile_id": "custom_pcl",
      "display_name": "Custom PCL Printer",
      "output_suffix": ".pcl",
      "print_format": "pcl",
      "command": "your-converter \"{source}\" > \"{output}\""
    }
  ]
}
```

如果新增 profile 需要自动选择，还需要在 `cloud/cloud_server.py` 的 `PROFILE_PRIORITY` 中加入对应 MIME 与 profile 映射。
