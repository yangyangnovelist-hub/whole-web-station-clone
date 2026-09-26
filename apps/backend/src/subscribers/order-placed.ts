import { SubscriberArgs, SubscriberConfig } from "@medusajs/framework"
import { ContainerRegistrationKeys, Modules } from "@medusajs/framework/utils"
import {
  renderOrderConfirmationEmail,
  renderSalesOrderEmail,
} from "../lib/email/templates"
import { sendEmail } from "../lib/email/send"
import { recordLeadEvent } from "../lib/lead-intel/record"
import { packoasisConfig } from "../lib/packoasis-config"
import { RFQ_MODULE } from "../modules/rfq"

/**
 * Closes the loop for every order: links instant-quote RFQs (QUOTED ->
 * ACCEPTED -> ORDERED), measures quote-to-order time, confirms the order to
 * the buyer (Medusa sends no emails by default) and alerts sales.
 */
export default async function orderPlacedHandler({
  event: { data },
  container,
}: SubscriberArgs<{ id: string }>) {
  const logger = container.resolve(ContainerRegistrationKeys.LOGGER)
  const rfqService: any = container.resolve(RFQ_MODULE)

  // The order module computes totals (query.graph returns 0 for them here).
  let order: any
  try {
    order = await container.resolve(Modules.ORDER).retrieveOrder(data.id, {
      select: [
        "id",
        "display_id",
        "email",
        "currency_code",
        "created_at",
        "metadata",
        "total",
        "item_total",
      ],
      relations: ["items", "shipping_address", "billing_address"],
    })
  } catch {
    order = null
  }

  if (!order) {
    logger.warn(`[order-placed] order ${data.id} not found`)
    return
  }

  const rfqIds = new Set<string>()
  const orderMetadataRfq = (order.metadata as Record<string, unknown> | null)
    ?.packoasis_rfq_id
  if (typeof orderMetadataRfq === "string") {
    rfqIds.add(orderMetadataRfq)
  }
  for (const item of order.items ?? []) {
    const rfqId = (item?.metadata as Record<string, unknown> | null)
      ?.packoasis_rfq_id
    if (typeof rfqId === "string") {
      rfqIds.add(rfqId)
    }
  }

  let minutesFromQuote: number | null = null
  let firstRfqId: string | null = null
  for (const rfqId of rfqIds) {
    try {
      const rfq = await rfqService.retrieveRFQ(rfqId)
      await rfqService.markOrdered(rfqId, order.id)
      firstRfqId = firstRfqId ?? rfqId
      minutesFromQuote =
        Math.round(
          ((new Date(order.created_at as string | Date).getTime() -
            new Date(rfq.created_at).getTime()) /
            60000) *
            10
        ) / 10

      if (rfq.visitor_id) {
        await recordLeadEvent(container, rfq.visitor_id, {
          type: "order_placed",
          product_type: (rfq.quote_payload as { product_type?: string } | null)
            ?.product_type,
          payload: {
            order_id: order.id,
            rfq_id: rfqId,
            minutes_from_quote: minutesFromQuote,
          },
        })
      }
    } catch (error) {
      logger.warn(
        `[order-placed] could not link RFQ ${rfqId} to ${order.id}: ${(error as Error).message}`
      )
    }
  }

  const firstName =
    order.shipping_address?.first_name ||
    order.billing_address?.first_name ||
    "there"
  const total = Number(order.total ?? 0)

  if (order.email) {
    await sendEmail(container, {
      to: order.email,
      template: "packoasis-order-confirmation",
      idempotencyKey: `order-confirmation:${order.id}`,
      resourceId: order.id,
      resourceType: "order",
      email: renderOrderConfirmationEmail({
        firstName,
        displayId: order.display_id ?? order.id,
        items: (order.items ?? []).filter(Boolean).map((item: any) => ({
          title: item.product_title || item.title,
          subtitle: item.variant_title,
          quantity: Number(item.quantity ?? 1),
          total: Number(item.total ?? 0),
        })),
        total,
        currencyCode: order.currency_code,
      }),
    })
  }

  await sendEmail(container, {
    to: packoasisConfig.salesEmails(),
    template: "packoasis-sales-order",
    idempotencyKey: `sales-order:${order.id}`,
    resourceId: order.id,
    resourceType: "order",
    email: renderSalesOrderEmail({
      displayId: order.display_id ?? order.id,
      email: order.email ?? "",
      total,
      currencyCode: order.currency_code,
      rfqId: firstRfqId,
      minutesFromQuote,
      adminUrl: `${packoasisConfig.backendUrl()}/app/orders/${order.id}`,
    }),
  })

  if (firstRfqId) {
    try {
      await container.resolve(Modules.ANALYTICS).track({
        event: "instant_quote_ordered",
        actor_id: order.email || order.id,
        properties: {
          order_id: order.id,
          rfq_id: firstRfqId,
          total,
          minutes_from_quote: minutesFromQuote,
        },
      })
    } catch (error) {
      logger.warn(
        `[order-placed] analytics failed: ${(error as Error).message}`
      )
    }
    logger.info(
      `[order-placed] ${order.id} from instant quote ${firstRfqId} (${minutesFromQuote} min after quote)`
    )
  }
}

export const config: SubscriberConfig = {
  event: "order.placed",
}
