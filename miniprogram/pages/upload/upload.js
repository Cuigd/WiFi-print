const config = require('../../config')

const app = getApp()

Page({
  data: {
    mode: 'document',
    modeTitle: '微信文档',
    fileTip: '支持 PDF、DOC、DOCX，最大 50MB',
    filePath: '',
    fileName: '',
    copies: 1,
    paperSize: 'A4',
    uploading: false,
    resultText: '',
    boundPrinter: null
  },

  onLoad(options) {
    const mode = options.type || 'document'
    const boundPrinter = wx.getStorageSync('boundPrinter') || null
    this.setData({
      mode,
      modeTitle: mode === 'image' || mode === 'photo' ? '图片打印' : '微信文档',
      fileTip: mode === 'image' || mode === 'photo' ? '支持选择手机图片上传' : '支持 PDF、DOC、DOCX，最大 50MB',
      boundPrinter
    })
  },

  chooseFile() {
    if (this.data.mode === 'image' || this.data.mode === 'photo') {
      this.chooseImage()
      return
    }
    wx.chooseMessageFile({
      count: 1,
      type: 'file',
      extension: ['pdf', 'doc', 'docx'],
      success: (res) => {
        const file = res.tempFiles[0]
        this.setData({
          filePath: file.path,
          fileName: file.name || '已选择文档',
          resultText: ''
        })
      }
    })
  },

  chooseImage() {
    wx.chooseMedia({
      count: 1,
      mediaType: ['image'],
      sourceType: ['album', 'camera'],
      success: (res) => {
        const file = res.tempFiles[0]
        this.setData({
          filePath: file.tempFilePath,
          fileName: '已选择图片',
          resultText: ''
        })
      }
    })
  },

  minusCopies() {
    if (this.data.copies <= 1) {
      return
    }
    this.setData({
      copies: this.data.copies - 1
    })
  },

  plusCopies() {
    this.setData({
      copies: this.data.copies + 1
    })
  },

  selectPaper(event) {
    this.setData({
      paperSize: event.currentTarget.dataset.size
    })
  },

  uploadFile() {
    if (!this.data.filePath) {
      wx.showToast({
        title: '请先选择文件',
        icon: 'none'
      })
      return
    }

    this.setData({
      uploading: true,
      resultText: ''
    })

    const boundPrinter = this.data.boundPrinter

    wx.uploadFile({
      url: `${config.serverBaseUrl}${config.uploadPath}`,
      filePath: this.data.filePath,
      name: 'file',
      formData: {
        user_id: app.globalData.userId,
        device_id: boundPrinter ? boundPrinter.deviceId : config.defaultDeviceId,
        copies: String(this.data.copies),
        paper_size: this.data.paperSize,
        duplex: 'false',
        color: 'true',
        upload_token: config.uploadToken || ''
      },
      success: (res) => {
        this.handleUploadResponse(res)
      },
      fail: (error) => {
        wx.showToast({
          title: error.errMsg || '上传失败',
          icon: 'none'
        })
      },
      complete: () => {
        this.setData({
          uploading: false
        })
      }
    })
  },

  handleUploadResponse(res) {
    let data = {}
    try {
      data = JSON.parse(res.data || '{}')
    } catch (error) {
      wx.showToast({
        title: '服务器返回异常',
        icon: 'none'
      })
      return
    }

    if (res.statusCode < 200 || res.statusCode >= 300) {
      wx.showToast({
        title: data.error || '上传失败',
        icon: 'none'
      })
      return
    }

    this.setData({
      resultText: '打印成功'
    })
    wx.showToast({
      title: '打印成功',
      icon: 'success'
    })
  }
})
