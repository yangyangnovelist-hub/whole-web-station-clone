import type {
  MedusaNextFunction,
  MedusaRequest,
  MedusaResponse,
} from "@medusajs/framework/http"

/**
 * Client IP for rate limiting: the right-most X-Forwarded-For hop (the one our
 * edge proxy appended; clients can only forge hops to its left), otherwise the
 * socket address. CF-Connecting-IP is only used when TRUST_CF_CONNECTING_IP is
 * "true": set it only when every request reaches the backend through
 * Cloudflare, since anyone calling the origin directly can send that header.
 */
export function clientIp(req: MedusaRequest) {
  if (process.env.TRUST_CF_CONNECTING_IP === "true") {
    const cf = req.get("cf-connecting-ip")?.trim()
    if (cf) {
      return cf
    }
  }
  const forwarded = req.get("x-forwarded-for")
  if (forwarded) {
    const hops = forwarded
      .split(",")
      .map((hop) => hop.trim())
      .filter(Boolean)
    if (hops.length) {
      return hops[hops.length - 1]
    }
  }
  return req.socket?.remoteAddress ?? "unknown"
}

const MAX_BUCKETS = 50_000
const buckets = new Map<string, { count: number; resetAt: number }>()

// Drop expired windows once a minute instead of scanning on requests.
setInterval(() => {
  const now = Date.now()
  for (const [key, value] of buckets) {
    if (value.resetAt <= now) {
      buckets.delete(key)
    }
  }
}, 60_000).unref()

/** Fixed-window in-memory limiter (per process), enough for one Railway instance. */
export function rateLimit(options: {
  key: string
  limit: number
  windowMs: number
}) {
  return (
    req: MedusaRequest,
    res: MedusaResponse,
    next: MedusaNextFunction
  ) => {
    const now = Date.now()
    const id = `${options.key}:${clientIp(req)}`
    const bucket = buckets.get(id)

    if (!bucket || bucket.resetAt <= now) {
      // Re-insert so Map order stays oldest-window-first, then evict from the
      // front when full (O(1), no scan).
      buckets.delete(id)
      if (buckets.size >= MAX_BUCKETS) {
        buckets.delete(buckets.keys().next().value!)
      }
      buckets.set(id, { count: 1, resetAt: now + options.windowMs })
      return next()
    }

    bucket.count++
    if (bucket.count > options.limit) {
      res.setHeader("Retry-After", Math.ceil((bucket.resetAt - now) / 1000))
      return res.status(429).json({
        code: "RATE_LIMITED",
        message: "Too many requests, please try again shortly.",
      })
    }

    return next()
  }
}

/** Storefront origins allowed to talk to the quote APIs (from STORE_CORS). */
export function allowedStorefrontOrigins() {
  return (process.env.STORE_CORS || "")
    .split(",")
    .map((origin) => origin.trim().replace(/\/$/, ""))
    .filter((origin) => /^https?:\/\//.test(origin))
}

export function requestOrigin(req: MedusaRequest) {
  const origin = req.get("origin")
  if (origin) {
    return origin.replace(/\/$/, "")
  }
  const referer = req.get("referer")
  if (referer) {
    try {
      return new URL(referer).origin
    } catch {
      return null
    }
  }
  return null
}

export function trustedStorefrontOrigin(req: MedusaRequest) {
  const origin = requestOrigin(req)
  return origin && allowedStorefrontOrigins().includes(origin) ? origin : null
}
