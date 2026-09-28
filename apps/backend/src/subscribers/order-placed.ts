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
import { findQuotedLineItem } from "../modules/rfq/service"

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

  // Cart and line-item metadata are editable through the store API, so an RFQ
  // only counts when it was quoted for the cart this order came from and the
  // order carries its custom line item at the quoted total.
  const claimedRfqIds = new Set<string>()
  const orderMetadataRfq = (order.metadata as Record<string, unknown> | null)
    ?.packoasis_rfq_id
  if (typeof orderMetadataRfq === "string") {
    claimedRfqIds.add(orderMetadataRfq)
  }
  for (const item of order.items ?? []) {
    const rfqId = (item?.metadata as Record<string, unknown> | null)
      ?.packoasis_rfq_id
    if (typeof rfqId === "string") {
      claimedRfqIds.add(rfqId)
    }
  }

  const ordered: { rfq: any; amount: number }[] = []
  try {
    const {
      data: [orderCart],
    } = await container.resolve(ContainerRegistrationKeys.QUERY).graph({
      entity: "order_cart",
      fields: ["cart_id"],
      filters: { order_id: order.id },
    })
    const rfqs = orderCart?.cart_id
      ? await rfqService.listRFQS({ cart_id: orderCart.cart_id })
      : []
    for (const rfq of rfqs) {
      const item = findQuotedLineItem(order.items, rfq)
      if (item) {
        ordered.push({
          rfq,
          amount: Number(item.unit_price) * Number(item.quantity),
        })
      }
    }
  } catch (error) {
    logger.warn(
      `[order-placed] could not find the quote cart of ${order.id}: ${(error as Error).message}`
    )
  }
  for (const rfqId of claimedRfqIds) {
    if (!ordered.some(({ rfq }) => rfq.id === rfqId)) {
      logger.warn(
        `[order-placed] ${order.id} names RFQ ${rfqId} but was not placed from its quote cart at the quoted total; not linking it`
      )
    }
  }

  let minutesFromQuote: number | null = null
  let firstRfqId: string | null = null
  const linked: any[] = []
  for (const { rfq, amount } of ordered) {
    const rfqId = rfq.id
    try {
      await rfqService.markOrdered(rfqId, order.id, amount)
      linked.push(rfq)
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

  // The buyer converted: close their other open instant quotes (e.g. an
  // earlier revision) so they get no "Review and order" follow-ups.
  for (const email of new Set(linked.map((rfq) => rfq.email))) {
    try {
      const open = await rfqService.listRFQS(
        { email, source: "instant_quote", status: "QUOTED", order_id: null },
        { select: ["id"], take: 100 }
      )
      for (const other of open) {
        await rfqService.closeRFQ(other.id).catch((error: Error) =>
          logger.warn(
            `[order-placed] could not close RFQ ${other.id}: ${error.message}`
          )
        )
      }
    } catch (error) {
      logger.warn(
        `[order-placed] could not close open quotes for ${order.id}: ${(error as Error).message}`
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
