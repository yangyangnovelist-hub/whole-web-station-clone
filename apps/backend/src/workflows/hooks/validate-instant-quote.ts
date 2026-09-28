import type { MedusaContainer } from "@medusajs/framework/types"
import { MedusaError } from "@medusajs/framework/utils"
import { completeCartWorkflow } from "@medusajs/medusa/core-flows"
import { RFQ_MODULE } from "../../modules/rfq"
import { findQuotedLineItem } from "../../modules/rfq/service"

/**
 * Instant-quote carts carry a custom-priced (variant-less) line item. Such a
 * cart can only become an order while it is the cart its RFQ points at (each
 * confirmed resume from the quote email moves the RFQ to a fresh cart), the
 * RFQ is not closed, the quote has not expired and the item still totals the
 * quoted price. Only server-owned RFQ fields decide this, since cart and
 * line-item metadata are editable through the store API. The link in the
 * quote email mints a fresh cart at the quoted (or re-priced) total, so that
 * is the remedy offered.
 */
export async function validateInstantQuoteCart(
  cart: any,
  container: MedusaContainer
) {
  const customItems = (cart?.items ?? []).filter(
    (item: any) => item && !item.variant_id
  )
  // Completing an already completed cart only returns its existing order.
  if (!customItems.length || cart.completed_at) {
    return
  }

  const rfqService: any = container.resolve(RFQ_MODULE)
  const rfqs: any[] = await rfqService.listRFQS({ cart_id: cart.id })
  const today = new Date().toISOString().slice(0, 10)

  for (const item of customItems) {
    const rfq = rfqs.find(
      (candidate) => candidate.id === item.metadata?.packoasis_rfq_id
    )
    if (!rfq) {
      throw new MedusaError(
        MedusaError.Types.NOT_ALLOWED,
        "This quote is no longer available in this cart. Open the link in your PackOasis quote email to check out with a fresh cart."
      )
    }
    if (rfq.status === "CLOSED") {
      throw new MedusaError(
        MedusaError.Types.NOT_ALLOWED,
        "This quote has been closed. Please contact PackOasis for a new quote."
      )
    }
    const validUntil = rfq.quote_payload?.valid_until
    if (typeof validUntil === "string" && validUntil < today) {
      throw new MedusaError(
        MedusaError.Types.NOT_ALLOWED,
        "This quote has expired. Open the link in your PackOasis quote email to re-price it and check out with a fresh cart."
      )
    }
    if (!findQuotedLineItem([item], rfq)) {
      throw new MedusaError(
        MedusaError.Types.NOT_ALLOWED,
        "This quoted item was changed (it is ordered as quoted, quantity 1). Open the link in your PackOasis quote email to check out with a fresh cart."
      )
    }
  }
}

completeCartWorkflow.hooks.validate(async ({ cart }, { container }) => {
  await validateInstantQuoteCart(cart, container)
})
