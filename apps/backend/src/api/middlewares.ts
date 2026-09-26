import { defineMiddlewares } from "@medusajs/framework/http"
import { rateLimit } from "../lib/http"

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
  ],
})
