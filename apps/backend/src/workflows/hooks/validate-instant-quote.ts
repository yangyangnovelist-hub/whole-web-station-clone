import type { MedusaContainer } from "@medusajs/framework/types"
import { MedusaError } from "@medusajs/framework/utils"
import { completeCartWorkflow } from "@medusajs/medusa/core-flows"
import { RFQ_MODULE } from "../../modules/rfq"
import { findQuotedLineItem } from "../../modules/rfq/service"

/**
 * Instant-quote carts carry a custom-priced (variant-less) line item. Such a
 * cart can only become an order while it is the cart its RFQ points at (a
 * re-priced quote moves the RFQ to a new cart), the quote has not expired and
 * the item still totals the quoted price. Only server-owned RFQ fields decide
 * this, since cart and line-item metadata are editable through the store API.
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
        "This quote is no longer available. Use the link in your latest PackOasis quote email to order."
      )
    }
    const validUntil = rfq.quote_payload?.valid_until
    if (typeof validUntil === "string" && validUntil < today) {
      throw new MedusaError(
        MedusaError.Types.NOT_ALLOWED,
        "This quote has expired. Use the link in your PackOasis quote email to re-price it and order."
      )
    }
    if (!findQuotedLineItem([item], rfq)) {
      throw new MedusaError(
        MedusaError.Types.NOT_ALLOWED,
        "This quoted item no longer matches its quote. Use the link in your PackOasis quote email to order."
      )
    }
  }
}

completeCartWorkflow.hooks.validate(async ({ cart }, { container }) => {
  await validateInstantQuoteCart(cart, container)
})
