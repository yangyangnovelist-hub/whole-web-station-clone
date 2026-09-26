import type {
  MedusaNextFunction,
  MedusaRequest,
  MedusaResponse,
} from "@medusajs/framework/http"

/**
 * Client IP for rate limiting: Cloudflare's header when present, otherwise
 * the right-most X-Forwarded-For hop (the one our edge proxy appended).
 */
export function clientIp(req: MedusaRequest) {
  const cf = req.get("cf-connecting-ip")
  if (cf) {
    return cf.trim()
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

const buckets = new Map<string, { count: number; resetAt: number }>()

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
      buckets.set(id, { count: 1, resetAt: now + options.windowMs })
      if (buckets.size > 50_000) {
        for (const [key, value] of buckets) {
          if (value.resetAt <= now) {
            buckets.delete(key)
          }
        }
      }
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
