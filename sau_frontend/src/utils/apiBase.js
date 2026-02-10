export function getApiBaseUrl() {
  const envBase = (import.meta.env.VITE_API_BASE_URL ?? '').trim()
  if (envBase) {
    return envBase.replace(/\/$/, '')
  }

  if (import.meta.env.DEV) {
    return '/api'
  }

  return ''
}
