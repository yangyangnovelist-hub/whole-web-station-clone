import {
  defineMiddlewares,
  type MedusaNextFunction,
  type MedusaRequest,
  type MedusaResponse,
} from "@medusajs/framework/http"
import { rateLimit } from "../lib/http"
import { verifyToken } from "../lib/packoasis-config"

/**
 * The storefront calls /packoasis/checkout-token server-to-server, so every
 * buyer shares its IP. Forged tokens are refused by the HMAC check alone and
 * are not counted; each signed link gets its own bucket, so neither forged
 * nor replayed links can lock other buyers out of checkout.
 */
function checkoutTokenLimit(
  req: MedusaRequest,
  res: MedusaResponse,
  next: MedusaNextFunction
) {
  const query = req.query as Record<string, unknown>
  const cartId = query.cart_id
  if (
    typeof cartId !== "string" ||
    !verifyToken("checkout", cartId, query.token)
  ) {
    return next()
  }
  return rateLimit({
    key: `checkout-token:${cartId}`,
    limit: 30,
    windowMs: 60_000,
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
  ],
})
