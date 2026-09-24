# 外网打印部署说明

## 架构

外网打印使用“云服务器调度 + MQTT 通知盒子”的方式：

```text
手机小程序 -> 云服务器 cloud_server.py -> MQTT 通知 -> 随身 WiFi box_agent.py -> 打印机
```

盒子主动连接云服务器上的 MQTT Broker，所以家里不需要公网 IP，也不需要路由器端口映射。平时不再轮询云端，只有收到新任务通知后才请求下载打印文件。

## 1. 云服务器部署

安装 MQTT Broker 和发布/订阅工具：

```bash
dnf install -y epol-release
dnf install -y mosquitto python3-paho-mqtt
systemctl enable --now mosquitto
```

测试阶段使用一个内置在盒子配置中的明文 MQTT 账号密码。先创建账号：

```bash
mosquitto_passwd -c /etc/mosquitto/passwd your_mqtt_username
```

按提示输入密码。然后让 MQTT 监听外网端口并禁用匿名访问：

```bash
mkdir -p /etc/mosquitto/conf.d

cat > /etc/mosquitto/conf.d/jihao-cloud-print.conf <<'EOF'
listener 1883 0.0.0.0
allow_anonymous false
password_file /etc/mosquitto/passwd
EOF

systemctl restart mosquitto
ss -lntp | grep 1883
```

如果没有看到 `0.0.0.0:1883`，把同样配置追加到 `/etc/mosquitto/mosquitto.conf` 后再重启。云服务器安全组需要放行 TCP `1883`。

把 `cloud/` 目录上传到云服务器，例如：

```text
/www/wwwroot/jihao-cloud-print/cloud/
  cloud_server.py
  print_pipeline.py
  admin/dashboard.html
  storage/
```

启动测试：

```bash
cd /www/wwwroot/jihao-cloud-print/cloud
export MQTT_ENABLED=true
export MQTT_HOST=127.0.0.1
export MQTT_PORT=1883
export MQTT_USERNAME=your_mqtt_username
export MQTT_PASSWORD=替换成上面设置的密码
export PRINT_UPLOAD_TOKEN=替换成长随机密钥
python3 cloud_server.py
```

服务默认监听：

```text
0.0.0.0:8080
```

确认云服务器安全组/防火墙放行 `8080`。浏览器或命令测试：

```bash
curl http://YOUR_CLOUD_SERVER_IP:8080/health
```

返回 `{"ok":true}` 即可。

## 2. 注册随身 WiFi 设备

在电脑或云服务器执行：

```bash
curl -X POST http://YOUR_CLOUD_SERVER_IP:8080/api/devices/register \
  -H "Content-Type: application/json" \
  -d '{"device_id":"your-box-id","device_name":"Home Printer","printer_profile":"raw_passthrough"}'
```

保存返回的 `device_token`。

## 3. 随身 WiFi 部署 box_agent

安装 MQTT 客户端库：

```bash
apt-get update
apt-get install -y python3-paho-mqtt
```

把这些文件传到随身 WiFi：

```text
box_agent.py
box_config.wifi_printer.example.json
```

生成正式配置：

```bash
cp /root/box_config.wifi_printer.example.json /root/box_config.json
vi /root/box_config.json
```

把 `device_token` 改成注册接口返回值。

同时把 MQTT 用户名和密码写入配置：

```json
"mqtt": {
  "host": "YOUR_CLOUD_SERVER_IP",
  "port": 1883,
  "username": "your_mqtt_username",
  "password": "替换成上面设置的密码",
  "topic_prefix": "cloud-print",
  "keepalive_seconds": 60
}
```

测试运行：

```bash
python3 /root/box_agent.py --config /root/box_config.json
```

看到类似下面说明连接成功：

```text
[box] MQTT connected; subscribed cloud-print/your-box-id/jobs
[box] Waiting for MQTT jobs from YOUR_CLOUD_SERVER_IP:1883 as your-box-id
```

## 4. 随身 WiFi 开机自启

```bash
cat > /etc/systemd/system/cloud-print-box.service <<'EOF'
[Unit]
Description=Cloud Print Box Agent
After=network-online.target cups.service
Wants=network-online.target

[Service]
WorkingDirectory=/root
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 -u /root/box_agent.py --config /root/box_config.json
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now cloud-print-box
systemctl status cloud-print-box --no-pager
```

查看日志：

```bash
journalctl -u cloud-print-box -n 80 --no-pager
```

## 5. 小程序配置

`miniprogram/config.js` 当前已指向：

```text
http://YOUR_CLOUD_SERVER_IP:8080
```

开发测试可以继续用 HTTP。正式微信小程序上线通常需要 HTTPS 域名，并在微信公众平台配置合法域名。

## 6. 支持的文件

当前推荐先打印：

```text
PDF
JPG
PNG
```

Word、Excel、PPT 需要先转 PDF，再下发给随身 WiFi 打印。可以后续在云服务器安装 LibreOffice 做自动转换。
