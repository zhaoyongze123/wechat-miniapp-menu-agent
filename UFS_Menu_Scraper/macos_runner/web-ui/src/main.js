import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'
import './styles/task-center.css'
import TaskCenter from './TaskCenter.vue'

let instance = null
let host = null

// 由 /assets/task-center-vue-loader.js 调用；页面外壳仍由现有工作台管理。
export function mountTaskCenter(bridge) {
  unmountTaskCenter()
  host = document.getElementById('task-center-vue')
  if (!host) throw new Error('页面缺少 #task-center-vue 容器')
  host.replaceChildren()
  instance = createApp(TaskCenter, { bridge })
  instance.use(ElementPlus, { locale: zhCn, size: 'default', zIndex: 3000 })
  instance.mount(host)
  return instance
}

// 退出页面或回切旧版时调用，保证定时器、弹层与请求周期随组件一起结束。
export function unmountTaskCenter() {
  if (instance) {
    instance.unmount()
    instance = null
  }
  if (host) host.replaceChildren()
  host = null
}
