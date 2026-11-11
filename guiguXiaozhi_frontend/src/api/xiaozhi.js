/**
 * 硅谷小智 API 层 —— 前端唯一与后端交互的地方（组件不再自己拼 URL/请求头）。
 *
 * 认证是灰度开关：VITE_AUTH_ENABLED 未设置或为 'false' 时，行为与一期完全一致：
 *   - 仍请求 /api/xiaozhi/chat，响应仍是裸文本流
 *   - memoryId 仍来自 localStorage 的 user_uuid 随机数
 *   - 不带任何 Authorization 头
 *
 * 开启后（VITE_AUTH_ENABLED=true）：
 *   - 启动时调 /api/v1/sessions，由服务端签发不可枚举的 session_id 当 memoryId
 *   - 每次请求带 Bearer token（token 由登录页 / 网关写入 localStorage）
 *
 * ⚠️ 安全红线：bootstrap secret 绝不能写进前端代码或 Vite 环境变量。
 * login() 只供内网演示的登录页显式调用（用户现场输入凭证）；
 * 生产环境必须换成 OIDC / 网关下发 token，并删掉这个过渡端点。
 */
import axios from 'axios'

export const AUTH_ENABLED = import.meta.env.VITE_AUTH_ENABLED === 'true'

export const chatUrl = '/api/xiaozhi/chat'
const SESSION_URL = '/api/v1/sessions'
const TOKEN_URL = '/api/v1/auth/token'
const TOKEN_KEY = 'xiaozhi_token'
const SESSION_KEY = 'xiaozhi_session_id'
const LEGACY_UUID_KEY = 'user_uuid'

/** 由登录页或网关注入已签发的 token */
export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || ''
}

/** 内网演示登录：凭证由调用方（登录页）传入，不落在构建产物里 */
export async function login(subjectId, secret) {
  const resp = await axios.post(TOKEN_URL, { subject_id: subjectId, secret })
  setToken(resp.data.access_token)
  return resp.data
}

export function authHeaders() {
  const token = getToken()
  return AUTH_ENABLED && token ? { Authorization: `Bearer ${token}` } : {}
}

async function createSession() {
  const resp = await axios.post(SESSION_URL, {}, { headers: authHeaders() })
  const sessionId = resp.data && resp.data.session_id
  if (!sessionId) throw new Error('服务端未返回 session_id')
  localStorage.setItem(SESSION_KEY, sessionId)
  return sessionId
}

/** 返回本次会话的 memoryId；认证关闭时完全沿用一期逻辑 */
export async function initMemoryId() {
  if (!AUTH_ENABLED) {
    let stored = localStorage.getItem(LEGACY_UUID_KEY)
    if (!stored) {
      stored = Math.floor(Math.random() * 1000000)
      localStorage.setItem(LEGACY_UUID_KEY, stored)
    }
    return stored
  }
  return localStorage.getItem(SESSION_KEY) || createSession()
}

/** 「开启新会话」：清掉本地会话标识，reload 后由服务端重新签发 */
export function newSession() {
  localStorage.removeItem(LEGACY_UUID_KEY)
  localStorage.removeItem(SESSION_KEY)
}
