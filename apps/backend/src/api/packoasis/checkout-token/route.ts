import type { MedusaRequest, MedusaResponse } from "@medusajs/framework/http"
import { ContainerRegistrationKeys } from "@medusajs/framework/utils"
import { verifyToken } from "../../../lib/packoasis-config"

/**
 * Checks a signed storefront checkout hand-off (see storefrontCheckoutUrl)
 * before the storefront attaches the cart to a browser, e.g.
 * GET /packoasis/checkout-token?cart_id=cart_...&token=...
 * Always answers 200 with only `{ valid }`, so it reveals nothing else about
 * the cart.
 */
export async function GET(req: MedusaRequest, res: MedusaResponse) {
  res.setHeader("Cache-Control", "no-store")
  const query = req.query as Record<string, unknown>
  const cartId = typeof query.cart_id === "string" ? query.cart_id : ""
  if (!cartId || !verifyToken("checkout", cartId, query.token)) {
    return res.json({ valid: false })
  }

  try {
    const {
      data: [cart],
    } = await req.scope.resolve(ContainerRegistrationKeys.QUERY).graph({
      entity: "cart",
      fields: ["id", "completed_at"],
      filters: { id: cartId },
    })
    return res.json({ valid: Boolean(cart && !cart.completed_at) })
  } catch {
    return res.json({ valid: false })
  }
}
