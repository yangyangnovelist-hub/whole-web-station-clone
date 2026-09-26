import type { MedusaRequest, MedusaResponse } from "@medusajs/framework/http"
import { ContainerRegistrationKeys } from "@medusajs/framework/utils"
import { createCartWorkflow } from "@medusajs/medusa/core-flows"
import { PRODUCT_IMAGES } from "../../../lib/instant-quote/catalog"
import { priceQuote, QuoteResult } from "../../../lib/instant-quote/engine"
import {
  packoasisConfig,
  storefrontCheckoutUrl,
  verifyToken,
} from "../../../lib/packoasis-config"
import { RFQ_MODULE } from "../../../modules/rfq"

/**
 * Signed "complete my order" link used in emails. Sends the buyer to the
 * storefront checkout with their saved cart; if the quote expired, it is
 * re-priced at today's prices into a fresh cart first.
 */
export async function GET(req: MedusaRequest, res: MedusaResponse) {
  const storefront = packoasisConfig.storefrontUrl()
  const rfqId = String((req.query as Record<string, unknown>).rfq ?? "")
  if (
    !rfqId ||
    !verifyToken("resume", rfqId, (req.query as Record<string, unknown>).token)
  ) {
    return res.redirect(302, `${storefront}/contact-us.html`)
  }

  const logger = req.scope.resolve(ContainerRegistrationKeys.LOGGER)
  const query = req.scope.resolve(ContainerRegistrationKeys.QUERY)
  const rfqService: any = req.scope.resolve(RFQ_MODULE)

  let rfq
  try {
    rfq = await rfqService.retrieveRFQ(rfqId)
  } catch {
    return res.redirect(302, `${storefront}/contact-us.html`)
  }

  const quote = rfq.quote_payload as QuoteResult | null
  if (rfq.order_id || rfq.status === "ORDERED") {
    return res.redirect(302, `${storefront}/?po_order=placed`)
  }
  if (!quote || !rfq.cart_id || rfq.status === "CLOSED") {
    return res.redirect(302, `${storefront}/contact-us.html`)
  }

  const {
    data: [cart],
  } = await query.graph({
    entity: "cart",
    fields: ["id", "region_id", "sales_channel_id", "email", "completed_at"],
    filters: { id: rfq.cart_id },
  })

  const country = rfq.country_code || packoasisConfig.defaultCountry()
  const today = new Date().toISOString().slice(0, 10)
  if (cart && !cart.completed_at && quote.valid_until >= today) {
    return res.redirect(302, storefrontCheckoutUrl(cart.id, country))
  }
  if (!cart || cart.completed_at) {
    return res.redirect(302, `${storefront}/?po_order=placed`)
  }

  try {
    const fresh = priceQuote(quote.specs)
    await rfqService.submitQuote({
      rfq_id: rfq.id,
      vendor_id: "packoasis-instant",
      price: fresh.total,
      lead_time_days: fresh.lead_time.production_days[1],
      notes: `Re-priced after expiry (${fresh.pricing_version}): ${fresh.summary}`,
    })
    const { result: newCart } = await createCartWorkflow(req.scope).run({
      input: {
        region_id: cart.region_id ?? undefined,
        sales_channel_id: cart.sales_channel_id ?? undefined,
        email: cart.email ?? rfq.email,
        currency_code: fresh.currency_code,
        items: [
          {
            title: fresh.product_label,
            product_title: fresh.summary,
            variant_title: `${fresh.quantity.toLocaleString("en-US")} pcs`,
            thumbnail: PRODUCT_IMAGES[fresh.product_type],
            quantity: 1,
            unit_price: fresh.total,
            requires_shipping: false,
            metadata: {
              packoasis_rfq_id: rfq.id,
              packoasis_quote: {
                summary: fresh.summary,
                quantity: fresh.quantity,
                unit_price: fresh.unit_price,
                specs: fresh.specs,
                pricing_version: fresh.pricing_version,
                valid_until: fresh.valid_until,
              },
            },
          },
        ],
        metadata: {
          packoasis_rfq_id: rfq.id,
          packoasis_source: "instant_quote",
        },
      },
    })
    await rfqService.updateRFQS({
      id: rfq.id,
      cart_id: newCart.id,
      quote_payload: fresh,
      quoted_total: fresh.total,
    })
    return res.redirect(302, storefrontCheckoutUrl(newCart.id, country))
  } catch (error) {
    logger.error(
      `[resume] re-pricing ${rfq.id} failed: ${(error as Error).message}`
    )
    return res.redirect(302, `${storefront}/contact-us.html`)
  }
}
