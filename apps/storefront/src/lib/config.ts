import { getLocaleHeader } from "@lib/util/get-locale-header"
import Medusa, { FetchArgs, FetchInput } from "@medusajs/js-sdk"

// Defaults to standard port for Medusa server
let MEDUSA_BACKEND_URL = "http://localhost:9000"

if (process.env.MEDUSA_BACKEND_URL) {
  MEDUSA_BACKEND_URL = process.env.MEDUSA_BACKEND_URL
}

export const sdk = new Medusa({
  baseUrl: MEDUSA_BACKEND_URL,
  debug: process.env.NODE_ENV === "development",
  publishableKey: process.env.NEXT_PUBLIC_MEDUSA_PUBLISHABLE_KEY,
})

const originalFetch = sdk.client.fetch.bind(sdk.client)

const MAX_FETCH_ATTEMPTS = 3

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function shouldRetry(error: unknown) {
  if (
    error instanceof TypeError ||
    (error instanceof Error &&
      /ECONNRESET|fetch failed|ETIMEDOUT|socket hang up|EAI_AGAIN|Network connection lost/i.test(
        error.message
      ))
  ) {
    return true
  }

  if (
    typeof error === "object" &&
    error !== null &&
    "retryable" in error &&
    (error as { retryable?: unknown }).retryable === true
  ) {
    return true
  }

  if (
    typeof error === "object" &&
    error !== null &&
    "status" in error &&
    typeof (error as { status?: unknown }).status === "number"
  ) {
    const status = (error as { status: number }).status
    return status >= 500
  }

  return false
}

sdk.client.fetch = async <T>(
  input: FetchInput,
  init?: FetchArgs
): Promise<T> => {
  const headers = init?.headers ?? {}
  let localeHeader: Record<string, string | null> | undefined
  try {
    localeHeader = await getLocaleHeader()
    headers["x-medusa-locale"] ??= localeHeader["x-medusa-locale"]
  } catch {}

  const newHeaders = {
    ...localeHeader,
    ...headers,
  }
  init = {
    ...init,
    headers: newHeaders,
  }

  let lastError: unknown

  for (let attempt = 1; attempt <= MAX_FETCH_ATTEMPTS; attempt++) {
    try {
      return await originalFetch(input, init)
    } catch (error) {
      lastError = error

      if (attempt === MAX_FETCH_ATTEMPTS || !shouldRetry(error)) {
        throw error
      }

      await sleep(250 * attempt)
    }
  }

  throw lastError
}
