# MQTT 安全方案记录

## 当前测试阶段

当前 MQTT 已经替代 HTTP 轮询。测试阶段允许把 MQTT 用户名和密码以明文方式写入盒子配置文件：

```json
"mqtt": {
  "host": "YOUR_CLOUD_SERVER_IP",
  "port": 1883,
  "username": "your_mqtt_username",
  "password": "replace-with-mqtt-password",
  "topic_prefix": "cloud-print",
  "keepalive_seconds": 60
}
```

云服务器 Mosquitto 使用密码文件鉴权：

```conf
listener 1883 0.0.0.0
allow_anonymous false
password_file /etc/mosquitto/passwd
```

创建或更新账号：

```bash
mosquitto_passwd -c /etc/mosquitto/passwd your_mqtt_username
systemctl restart mosquitto
```

如果后续新增盒子，测试阶段可以继续追加账号：

```bash
mosquitto_passwd /etc/mosquitto/passwd box_002
systemctl restart mosquitto
```

## 后期产品化

后期改为“每盒一密钥 + 云端管理 + 可重置”：

- 每个盒子出厂写入唯一 `device_id`、`device_token`、MQTT 用户名和 MQTT 密码。
- 云端数据库保存盒子对应的 MQTT 用户名、状态、最近连接时间和绑定打印机信息。
- 某个盒子丢失或异常时，只禁用或重置该盒子的 MQTT 账号，不影响其他盒子。
- 管理接口支持重新生成 MQTT 密码，并提示盒子下次联网同步或人工重新写入。
- 主题继续按盒子隔离：`cloud-print/{device_id}/jobs` 和 `cloud-print/{device_id}/status`。

## 注意事项

- MQTT `1883` 明文传输只适合当前测试阶段；正式公网部署建议升级到 TLS 或放到内网/VPN。
- 当前 Mosquitto 只做账号密码认证，暂未做 topic ACL。产品化时应限制每个盒子只能订阅和发布自己的主题。
- 小程序不需要连接 MQTT，仍然只访问云服务器 HTTP API。
