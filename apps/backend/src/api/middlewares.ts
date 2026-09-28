import {
  defineMiddlewares,
  type MedusaNextFunction,
  type MedusaRequest,
  type MedusaResponse,
} from "@medusajs/framework/http"
import { rateLimit } from "../lib/http"
import { verifyCheckoutLink, verifyToken } from "../lib/packoasis-config"

/**
 * The storefront calls /packoasis/checkout-token server-to-server, so every
 * buyer shares its IP. Forged or expired links are refused by the signature
 * check alone and are not counted; each signed cart gets its own bucket, so
 * neither forged nor replayed links can lock other buyers out of checkout.
 */
function checkoutTokenLimit(
  req: MedusaRequest,
  res: MedusaResponse,
  next: MedusaNextFunction
) {
  const cartId = verifyCheckoutLink(req.query as Record<string, unknown>)
  if (!cartId) {
    return next()
  }
  return rateLimit({
    key: `checkout-token:${cartId}`,
    limit: 30,
    windowMs: 60_000,
  })(req, res, next)
}

/**
 * /packoasis/resume-cart (also called by the storefront server) creates a
 * cart per call. As above, only correctly signed requests are counted, in a
 * bucket per quote and caller IP, which caps the carts one resume link can
 * mint through the storefront.
 */
function resumeCartLimit(
  req: MedusaRequest,
  res: MedusaResponse,
  next: MedusaNextFunction
) {
  const body = (req.body ?? {}) as Record<string, unknown>
  const rfqId = body.rfq
  if (typeof rfqId !== "string" || !verifyToken("resume", rfqId, body.token)) {
    return next()
  }
  return rateLimit({
    key: `resume-cart:${rfqId}`,
    limit: 10,
    windowMs: 10 * 60_000,
  })(req, res, next)
}

export default defineMiddlewares({
  routes: [
    {
      matcher: "/store/instant-quote/estimate",
      method: ["POST"],
      middlewares: [
        rateLimit({ key: "estimate", limit: 120, windowMs: 60_000 }),
      ],
    },
    {
      matcher: "/store/instant-quote/parse",
      method: ["POST"],
      middlewares: [rateLimit({ key: "parse", limit: 15, windowMs: 60_000 })],
    },
    {
      matcher: "/store/instant-quote/order",
      method: ["POST"],
      middlewares: [
        rateLimit({ key: "order", limit: 8, windowMs: 10 * 60_000 }),
      ],
    },
    {
      matcher: "/store/rfqs",
      method: ["POST"],
      middlewares: [rateLimit({ key: "rfq", limit: 8, windowMs: 10 * 60_000 })],
    },
    {
      matcher: "/packoasis/events",
      method: ["POST"],
      bodyParser: { sizeLimit: "32kb" },
      middlewares: [rateLimit({ key: "events", limit: 240, windowMs: 60_000 })],
    },
    {
      matcher: "/packoasis/quote",
      method: ["GET"],
      middlewares: [
        rateLimit({ key: "public-quote", limit: 60, windowMs: 60_000 }),
      ],
    },
    {
      matcher: "/packoasis/checkout-token",
      method: ["GET"],
      middlewares: [checkoutTokenLimit],
    },
    {
      matcher: "/packoasis/resume-cart",
      method: ["POST"],
      bodyParser: { sizeLimit: "4kb" },
      middlewares: [resumeCartLimit],
    },
  ],
})
