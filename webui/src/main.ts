import { createApp } from 'vue'
import App from './App.vue'
import { router } from './router'
import { initPrivacy } from './utils/privacy'
import './styles/tokens.css'
import './styles/base.css'
import './components/ui/form.css'

initPrivacy()
createApp(App).use(router).mount('#app')
