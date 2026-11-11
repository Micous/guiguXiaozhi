<template>
  <div class="login-gate">
    <div class="login-card">
      <div class="login-brand">
        <img src="@/assets/logo.png" alt="硅谷小智" />
        <h1>硅谷小智</h1>
        <p>智能伴诊助手 · 内网演示登录</p>
      </div>

      <form @submit.prevent="submit">
        <label for="login-subject">用户标识</label>
        <input
          id="login-subject"
          v-model.trim="subjectId"
          type="text"
          autocomplete="username"
          placeholder="例如 alice"
          required
        />

        <label for="login-secret">访问凭证</label>
        <input
          id="login-secret"
          v-model="secret"
          type="password"
          autocomplete="current-password"
          placeholder="请输入管理员发放的凭证"
          required
        />

        <p v-if="error" class="login-error">{{ error }}</p>
        <button type="submit" :disabled="loading">
          {{ loading ? '登录中…' : '登录' }}
        </button>
      </form>

      <p class="login-note">
        过渡登录方式：凭证仅在本次输入时提交，不写入前端代码与构建产物。<br />
        正式上线须替换为统一身份认证（OIDC / 网关签发）。
      </p>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { login } from '@/api/xiaozhi'

const emit = defineEmits(['logged-in'])
const subjectId = ref('')
const secret = ref('')
const error = ref('')
const loading = ref(false)

async function submit() {
  error.value = ''
  loading.value = true
  try {
    await login(subjectId.value, secret.value)
    emit('logged-in')
  } catch (e) {
    const status = e && e.response && e.response.status
    error.value = status === 401
      ? '凭证不正确，请重试。'
      : '登录服务不可用，请稍后重试。'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-gate {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #eef4fb 0%, #f7fbff 55%, #eaf6f2 100%);
  font-family: inherit;
}
.login-card {
  width: 380px;
  padding: 36px 34px 28px;
  border-radius: 16px;
  background: #ffffff;
  box-shadow: 0 12px 40px rgba(31, 78, 121, 0.12);
}
.login-brand {
  text-align: center;
  margin-bottom: 26px;
}
.login-brand img {
  width: 56px;
  height: 56px;
  object-fit: contain;
}
.login-brand h1 {
  margin: 10px 0 4px;
  font-size: 22px;
  color: #1f4e79;
}
.login-brand p {
  margin: 0;
  font-size: 13px;
  color: #7a8a99;
}
form {
  display: flex;
  flex-direction: column;
}
label {
  margin: 12px 0 6px;
  font-size: 13px;
  color: #43536a;
}
input {
  padding: 10px 12px;
  border: 1px solid #d9e3ee;
  border-radius: 8px;
  font-size: 14px;
  outline: none;
  transition: border-color 0.2s;
}
input:focus {
  border-color: #409eff;
}
button {
  margin-top: 22px;
  padding: 11px;
  border: none;
  border-radius: 8px;
  background: #409eff;
  color: #ffffff;
  font-size: 15px;
  cursor: pointer;
  transition: background 0.2s;
}
button:hover:not(:disabled) {
  background: #2f88e8;
}
button:disabled {
  opacity: 0.6;
  cursor: default;
}
.login-error {
  margin: 12px 0 0;
  font-size: 13px;
  color: #d9534f;
}
.login-note {
  margin: 22px 0 0;
  padding-top: 16px;
  border-top: 1px solid #eef2f6;
  font-size: 12px;
  line-height: 1.7;
  color: #8a99a8;
  text-align: center;
}
</style>
