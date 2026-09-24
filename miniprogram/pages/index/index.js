const config = require('../../config')

Page({
  data: {
    boundPrinter: null,
    discovering: false
  },

  onShow() {
    this.loadBoundPrinter()
  },

  loadBoundPrinter() {
    const boundPrinter = wx.getStorageSync('boundPrinter') || null
    this.setData({
      boundPrinter
    })
  },

  discoverPrinters() {
    if (this.data.discovering) {
      return
    }

    this.setData({
      discovering: true
    })

    wx.request({
      url: `${config.lanServerBaseUrl}/api/printers/discover`,
      method: 'GET',
      success: (res) => {
        const data = res.data || {}
        if (res.statusCode < 200 || res.statusCode >= 300 || !data.ok) {
          wx.showToast({
            title: data.error || '搜索失败',
            icon: 'none'
          })
          return
        }

        const printers = data.printers || []
        if (!printers.length) {
          wx.showToast({
            title: '未发现打印机',
            icon: 'none'
          })
          return
        }
        this.choosePrinter(printers)
      },
      fail: () => {
        wx.showToast({
          title: '请连接家中局域网',
          icon: 'none'
        })
      },
      complete: () => {
        this.setData({
          discovering: false
        })
      }
    })
  },

  choosePrinter(printers) {
    wx.showActionSheet({
      itemList: printers.slice(0, 6).map((item) => item.model || item.name || item.ip),
      success: (res) => {
        const selected = printers[res.tapIndex]
        const boundPrinter = {
          deviceId: config.defaultDeviceId,
          name: selected.model || selected.name || `打印机 ${selected.ip}`,
          ip: selected.ip,
          uri: selected.uri,
          boundAt: Date.now()
        }
        wx.setStorageSync('boundPrinter', boundPrinter)
        this.setData({
          boundPrinter
        })
        wx.showToast({
          title: '绑定成功',
          icon: 'success'
        })
      }
    })
  },

  goUpload(event) {
    const type = event.currentTarget.dataset.type || 'document'
    wx.navigateTo({
      url: `/pages/upload/upload?type=${type}`
    })
  },

  goMaterial() {
    wx.navigateTo({
      url: '/pages/material/material'
    })
  },

  goProfile() {
    wx.navigateTo({
      url: '/pages/profile/profile'
    })
  },

  showComingSoon() {
    wx.showToast({
      title: '功能准备中',
      icon: 'none'
    })
  }
})
