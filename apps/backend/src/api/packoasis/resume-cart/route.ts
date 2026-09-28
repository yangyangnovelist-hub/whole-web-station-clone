import type { MedusaRequest, MedusaResponse } from "@medusajs/framework/http"
import { ContainerRegistrationKeys, Modules } from "@medusajs/framework/utils"
import { priceQuote, QuoteResult } from "../../../lib/instant-quote/engine"
import { packoasisConfig, verifyToken } from "../../../lib/packoasis-config"
import { RFQ_MODULE } from "../../../modules/rfq"
import { findQuotedLineItem } from "../../../modules/rfq/service"
import { createQuoteCart } from "../../store/instant-quote/order/route"

/**
 * Seconds to wait for each lock below; a request that cannot get one in time
 * is refused without changes. The in-memory and Redis locking providers also
 * let a lock taken with execute() lapse after this long, so each must outlast
 * the work done under it (the RFQ lock's work includes waiting for the cart
 * lock). The storefront gives up on this call after 15 seconds.
 */
const RFQ_LOCK_TIMEOUT = 10
const CART_LOCK_TIMEOUT = 5

function refuse(res: MedusaResponse, status: number, message: string) {
  return res.status(status).json({ code: "NOT_AVAILABLE", message })
}

/**
 * Whether the cart still carries the quote as the completeCart validate hook
 * checks it (see validateInstantQuoteCart): every custom item is this quote's
 * item at the quoted total.
 */
function carriesQuote(cart: any, rfq: any) {
  const customItems = (cart.items ?? []).filter(
    (item: any) => item && !item.variant_id
  )
  return (
    customItems.length > 0 &&
    customItems.every((item: any) => findQuotedLineItem([item], rfq))
  )
}

/**
 * Called server-to-server by the storefront's /api/quote-resume once a buyer
 * who opened the signed link from a quote email (see resumeQuoteUrl) presses
 * "Continue to checkout", e.g.
 * POST /packoasis/resume-cart { "rfq": "...", "token": "...", "current_cart_id": "cart_..." }
 * where current_cart_id (optional) is the cart in that browser's HttpOnly
 * cookie.
 *
 * If that is already the quote's current cart, still carrying the quote, and
 * the quote has not expired, it is returned unchanged, so a checkout open in
 * another tab keeps working (knowing the id already means holding the cart).
 * Otherwise a fresh cart is minted for the quote and the RFQ points at it, so
 * the cart id only ever reaches the browser that confirmed. That supersedes
 * the quote's previous cart (the completeCart validate hook only accepts the
 * RFQ's cart), which is why merely opening the link must not get here. The
 * cart gets no email: the buyer enters their own at checkout, so a forwarded
 * link cannot send the order confirmation to whoever forwarded it. An expired
 * quote is re-priced at today's prices into a new quote round first.
 *
 * Calls for one RFQ run one at a time, and each also locks the quote's
 * current cart under the key completeCartWorkflow locks while it validates
 * and completes a cart (the cart id). So a resume and the completion of that
 * cart do not interleave: the resume either finds the cart completed and
 * refuses, or swaps first and the completion is then rejected by the validate
 * hook. A double-submitted form ends on the cart of the request processed
 * last, and an expired quote is re-priced once. This holds within one backend
 * process with the default in-memory locking provider; several instances
 * need a shared one such as locking-redis.
 */
export async function POST(req: MedusaRequest, res: MedusaResponse) {
  res.setHeader("Cache-Control", "no-store")
  const body = (req.body ?? {}) as Record<string, unknown>
  const rfqId = typeof body.rfq === "string" ? body.rfq : ""
  if (!rfqId || !verifyToken("resume", rfqId, body.token)) {
    return refuse(res, 403, "Invalid link")
  }
  const heldCartId =
    typeof body.current_cart_id === "string" ? body.current_cart_id : undefined

  const logger = req.scope.resolve(ContainerRegistrationKeys.LOGGER)
  const query = req.scope.resolve(ContainerRegistrationKeys.QUERY)
  const locking = req.scope.resolve(Modules.LOCKING)
  const rfqService: any = req.scope.resolve(RFQ_MODULE)

  // Nested single-key locks, always RFQ first: the in-memory provider can
  // deadlock on itself when execute() is given several keys and one is busy.
  try {
    return await locking.execute(
      `packoasis-resume:${rfqId}`,
      async () => {
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
        const currentCartId: string = rfq.cart_id
        const countryCode = rfq.country_code || packoasisConfig.defaultCountry()

        return await locking.execute(
          currentCartId,
          async () => {
            // The quote's current cart supplies the region and sales channel;
            // once it is completed the quote has been ordered.
            const {
              data: [current],
            } = await query.graph({
              entity: "cart",
              fields: [
                "id",
                "region_id",
                "sales_channel_id",
                "completed_at",
                "items.id",
                "items.variant_id",
                "items.unit_price",
                "items.quantity",
                "items.metadata",
              ],
              filters: { id: currentCartId },
            })
            if (!current || current.completed_at) {
              return refuse(
                res,
                409,
                "This quote can no longer be ordered online"
              )
            }

            const today = new Date().toISOString().slice(0, 10)
            const expired =
              typeof quote.valid_until === "string" && quote.valid_until < today

            if (
              !expired &&
              heldCartId === currentCartId &&
              carriesQuote(current, rfq)
            ) {
              return res.json({
                cart_id: currentCartId,
                country_code: countryCode,
              })
            }

            let priced: QuoteResult
            let cart
            try {
              priced = expired ? priceQuote(quote.specs) : quote
              cart = await createQuoteCart(req.scope, {
                rfqId: rfq.id,
                quote: priced,
                regionId: current.region_id ?? undefined,
                salesChannelId: current.sales_channel_id ?? undefined,
              })
              await rfqService.updateRFQS({
                id: rfq.id,
                cart_id: cart.id,
                ...(expired
                  ? { quote_payload: priced, quoted_total: priced.total }
                  : {}),
              })
            } catch (error) {
              logger.error(
                `[resume] could not create a cart for ${rfq.id}: ${(error as Error).message}`
              )
              return refuse(res, 500, "Could not prepare checkout")
            }

            if (expired) {
              // Recorded once the RFQ points at the re-priced cart. The cart
              // is still returned if this fails: the RFQ already points at it.
              await rfqService
                .submitQuote({
                  rfq_id: rfq.id,
                  vendor_id: "packoasis-instant",
                  price: priced.total,
                  lead_time_days: priced.lead_time.production_days[1],
                  notes: `Re-priced after expiry (${priced.pricing_version}): ${priced.summary}`,
                })
                .catch((error: Error) =>
                  logger.error(
                    `[resume] could not record the re-priced round for ${rfq.id}: ${error.message}`
                  )
                )
            }
            return res.json({ cart_id: cart.id, country_code: countryCode })
          },
          { timeout: CART_LOCK_TIMEOUT }
        )
      },
      { timeout: RFQ_LOCK_TIMEOUT }
    )
  } catch (error) {
    // Waiting for a lock timed out (the cart is being completed, or another
    // resume of this quote is still running) or a lookup failed. Nothing was
    // changed.
    logger.warn(`[resume] ${rfqId} not resumed: ${(error as Error).message}`)
    return refuse(res, 503, "Could not prepare checkout. Please try again.")
  }
}
