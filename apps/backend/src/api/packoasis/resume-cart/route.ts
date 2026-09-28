import type { MedusaRequest, MedusaResponse } from "@medusajs/framework/http"
import { ContainerRegistrationKeys } from "@medusajs/framework/utils"
import { priceQuote, QuoteResult } from "../../../lib/instant-quote/engine"
import { packoasisConfig, verifyToken } from "../../../lib/packoasis-config"
import { RFQ_MODULE } from "../../../modules/rfq"
import { createQuoteCart } from "../../store/instant-quote/order/route"

function refuse(res: MedusaResponse, status: number, message: string) {
  return res.status(status).json({ code: "NOT_AVAILABLE", message })
}

/**
 * Called server-to-server by the storefront's /api/quote-resume once a buyer
 * who opened the signed link from a quote email (see resumeQuoteUrl) presses
 * "Continue to checkout", e.g.
 * POST /packoasis/resume-cart { "rfq": "...", "token": "..." }
 *
 * Every call mints a fresh cart for the quote and points the RFQ at it, so
 * the cart id only ever reaches the browser that confirmed. That supersedes
 * the quote's previous cart (the completeCart validate hook only accepts the
 * RFQ's cart), which is why merely opening the link must not get here. The
 * cart gets no email: the buyer enters their own at checkout, so a forwarded
 * link cannot send the order confirmation to whoever forwarded it. An
 * expired quote is re-priced at today's prices into a new quote round first.
 */
export async function POST(req: MedusaRequest, res: MedusaResponse) {
  res.setHeader("Cache-Control", "no-store")
  const body = (req.body ?? {}) as Record<string, unknown>
  const rfqId = typeof body.rfq === "string" ? body.rfq : ""
  if (!rfqId || !verifyToken("resume", rfqId, body.token)) {
    return refuse(res, 403, "Invalid link")
  }

  const logger = req.scope.resolve(ContainerRegistrationKeys.LOGGER)
  const query = req.scope.resolve(ContainerRegistrationKeys.QUERY)
  const rfqService: any = req.scope.resolve(RFQ_MODULE)

  let rfq
  try {
    rfq = await rfqService.retrieveRFQ(rfqId)
  } catch {
    return refuse(res, 404, "Quote not found")
  }

  const quote = rfq.quote_payload as QuoteResult | null
  if (
    rfq.order_id ||
    rfq.status === "ORDERED" ||
    rfq.status === "CLOSED" ||
    !quote ||
    !rfq.cart_id
  ) {
    return refuse(res, 409, "This quote can no longer be ordered online")
  }

  // The quote's current cart supplies the region and sales channel; once it
  // is completed the quote has been ordered.
  const {
    data: [current],
  } = await query.graph({
    entity: "cart",
    fields: ["id", "region_id", "sales_channel_id", "completed_at"],
    filters: { id: rfq.cart_id },
  })
  if (!current || current.completed_at) {
    return refuse(res, 409, "This quote can no longer be ordered online")
  }

  const today = new Date().toISOString().slice(0, 10)
  const expired =
    typeof quote.valid_until === "string" && quote.valid_until < today

  try {
    const priced = expired ? priceQuote(quote.specs) : quote
    const cart = await createQuoteCart(req.scope, {
      rfqId: rfq.id,
      quote: priced,
      regionId: current.region_id ?? undefined,
      salesChannelId: current.sales_channel_id ?? undefined,
    })
    if (expired) {
      await rfqService.submitQuote({
        rfq_id: rfq.id,
        vendor_id: "packoasis-instant",
        price: priced.total,
        lead_time_days: priced.lead_time.production_days[1],
        notes: `Re-priced after expiry (${priced.pricing_version}): ${priced.summary}`,
      })
      await rfqService.updateRFQS({
        id: rfq.id,
        cart_id: cart.id,
        quote_payload: priced,
        quoted_total: priced.total,
      })
    } else {
      await rfqService.updateRFQS({ id: rfq.id, cart_id: cart.id })
    }
    return res.json({
      cart_id: cart.id,
      country_code: rfq.country_code || packoasisConfig.defaultCountry(),
    })
  } catch (error) {
    logger.error(
      `[resume] could not create a cart for ${rfq.id}: ${(error as Error).message}`
    )
    return refuse(res, 500, "Could not prepare checkout")
  }
}
