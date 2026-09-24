const config = {
  // 修改成你的正式 HTTPS 域名，例如：https://print.example.com
  serverBaseUrl: 'http://YOUR_CLOUD_SERVER_IP:8080',
  // 在家绑定打印机时使用，需和随身 WiFi 的 lan-print 服务地址一致
  lanServerBaseUrl: 'http://YOUR_BOX_LAN_IP:8090',
  defaultDeviceId:'your-box-id',
  uploadPath: '/api/jobs',
  // 云端设置 PRINT_UPLOAD_TOKEN 后，这里填写同一个值；未启用时留空
  uploadToken: ''
}

module.exports = config
