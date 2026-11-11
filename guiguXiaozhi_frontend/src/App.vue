<template>
  <LoginGate v-if="needLogin" @logged-in="onLoggedIn" />
  <ChatWindow v-else />
</template>

<script setup>
import { ref } from 'vue'
import ChatWindow from '@/components/ChatWindow.vue'
import LoginGate from '@/components/LoginGate.vue'
import { AUTH_ENABLED, getToken } from '@/api/xiaozhi'

// 认证关闭（默认，与一期完全一致）时恒为 false，登录组件不会被渲染。
// 开启后且本地无 token → 先进登录门；网关注入 token 的场景可直接进 ChatWindow。
const needLogin = ref(AUTH_ENABLED && !getToken())

const onLoggedIn = () => {
  needLogin.value = false // v-else 新建 ChatWindow，触发 initMemoryId 签发会话
}
</script>

<style>
html,
body {
  overflow: hidden;
}
</style>
