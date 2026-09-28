import type { MedusaRequest, MedusaResponse } from "@medusajs/framework/http"
import { ContainerRegistrationKeys } from "@medusajs/framework/utils"
import {
  resumeQuoteUrl,
  verifyCheckoutLink,
} from "../../../lib/packoasis-config"
import { RFQ_MODULE } from "../../../modules/rfq"

/**
 * Checks a widget checkout hand-off (see storefrontCheckoutUrl) before the
 * storefront attaches the cart to a browser, e.g.
 * GET /packoasis/checkout-token?cart_id=cart_...&exp=...&token=...&handoff=...
 * where `handoff` is the po_qn cookie of the browser that opened the link.
 * The link must be unexpired and signed for that cookie, and the cart must
 * still be its quote's open, unexpired cart (as the completeCart validate
 * hook requires), so no superseded, ordered, closed or expired quote cart is
 * attached. Answers 200 with `{ valid }`, plus a `resume_url` when only the
 * quote has expired (that link re-prices it).
 */
export async function GET(req: MedusaRequest, res: MedusaResponse) {
  res.setHeader("Cache-Control", "no-store")
  const cartId = verifyCheckoutLink(req.query as Record<string, unknown>)
  if (!cartId) {
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
    if (!cart || cart.completed_at) {
      return res.json({ valid: false })
    }

    const rfqService: any = req.scope.resolve(RFQ_MODULE)
    const [rfq] = await rfqService.listRFQS({ cart_id: cartId })
    if (
      !rfq ||
      rfq.order_id ||
      rfq.status === "ORDERED" ||
      rfq.status === "CLOSED"
    ) {
      return res.json({ valid: false })
    }
    const validUntil = rfq.quote_payload?.valid_until
    const today = new Date().toISOString().slice(0, 10)
    if (typeof validUntil === "string" && validUntil < today) {
      return res.json({ valid: false, resume_url: resumeQuoteUrl(rfq.id) })
    }
    return res.json({ valid: true })
  } catch {
    return res.json({ valid: false })
  }
}
