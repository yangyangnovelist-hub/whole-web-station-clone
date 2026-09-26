import type {
  MedusaResponse,
  MedusaStoreRequest,
} from "@medusajs/framework/http"
import { ContainerRegistrationKeys, Modules } from "@medusajs/framework/utils"
import { createCartWorkflow } from "@medusajs/medusa/core-flows"
import { z } from "zod"
import { PRODUCT_IMAGES } from "../../../../lib/instant-quote/catalog"
import {
  priceQuote,
  QuoteInputError,
} from "../../../../lib/instant-quote/engine"
import { trustedStorefrontOrigin } from "../../../../lib/http"
import { recordLeadEvent } from "../../../../lib/lead-intel/record"
import {
  packoasisConfig,
  storefrontCheckoutUrl,
} from "../../../../lib/packoasis-config"
import { RFQ_MODULE } from "../../../../modules/rfq"
import { LEAD_CREATED_EVENT } from "../../../../lib/events"
import {
  PageSchema,
  SpecsSchema,
  validationError,
  VisitorIdSchema,
} from "../validators"

const OrderSchema = z.object({
  specs: SpecsSchema,
  contact: z.object({
    name: z.string().trim().min(1, "Name is required").max(120),
    email: z.string().trim().email("A valid email is required").max(200),
    company: z.string().trim().max(160).optional(),
    phone: z.string().trim().max(40).optional(),
    website: z.string().trim().max(200).optional(),
  }),
  country_code: z
    .string()
    .regex(/^[a-zA-Z]{2}$/)
    .optional(),
  notes: z.string().max(2000).optional(),
  visitor_id: VisitorIdSchema.optional(),
  page: PageSchema,
  /** Honeypot: real visitors never fill this hidden field. */
  hp: z.string().max(200).optional(),
})

export async function POST(req: MedusaStoreRequest, res: MedusaResponse) {
  const parsed = OrderSchema.safeParse(req.body)
  if (!parsed.success) {
    return res.status(400).json(validationError(parsed.error))
  }
  if (parsed.data.hp) {
    return res
      .status(400)
      .json({ code: "REJECTED", message: "Request rejected" })
  }

  const { specs, contact, notes, visitor_id } = parsed.data
  const logger = req.scope.resolve(ContainerRegistrationKeys.LOGGER)
  const query = req.scope.resolve(ContainerRegistrationKeys.QUERY)
  const rfqService: any = req.scope.resolve(RFQ_MODULE)

  // Never trust a client-side price: re-price on the server.
  let quote
  try {
    quote = priceQuote(specs)
  } catch (error) {
    if (error instanceof QuoteInputError) {
      return res
        .status(400)
        .json({ code: "INVALID_SPECS", message: error.message })
    }
    throw error
  }

  const countryCode = (
    parsed.data.country_code || packoasisConfig.defaultCountry()
  ).toLowerCase()
  const { data: regions } = await query.graph({
    entity: "region",
    fields: ["id", "currency_code", "countries.iso_2"],
  })
  const region = regions.find(
    (candidate: any) =>
      candidate.currency_code === quote.currency_code &&
      candidate.countries?.some(
        (country: any) => country?.iso_2 === countryCode
      )
  )
  const salesChannelId = req.publishable_key_context?.sales_channel_ids?.[0]
  // Formats that need a structural review, and destinations without a
  // matching region, become a reviewed RFQ instead of a direct order.
  const canOrderOnline = Boolean(quote.instant && region && salesChannelId)

  const rfq = await rfqService.submitRFQ({
    source: canOrderOnline ? "instant_quote" : "instant_quote_review",
    contact_name: contact.name,
    email: contact.email.toLowerCase(),
    company: contact.company || undefined,
    phone: contact.phone || undefined,
    title: quote.summary,
    application: quote.product_label,
    quantity: quote.quantity,
    notes: notes || undefined,
    items_payload: JSON.stringify([
      {
        title: quote.product_label,
        quantity: quote.quantity,
        notes: quote.summary,
        metadata: { specs: quote.specs, source: "instant_quote" },
      },
    ]),
    quote_payload: quote,
    quoted_total: quote.total,
    currency_code: quote.currency_code,
    country_code: countryCode,
    website: contact.website || null,
    visitor_id: visitor_id ?? null,
  })

  let cartId: string | null = null
  if (canOrderOnline) {
    await rfqService.submitQuote({
      rfq_id: rfq.id,
      vendor_id: "packoasis-instant",
      price: quote.total,
      lead_time_days: quote.lead_time.production_days[1],
      notes: `Instant quote ${quote.pricing_version}: ${quote.summary}`,
    })

    try {
      const { result: cart } = await createCartWorkflow(req.scope).run({
        input: {
          region_id: region!.id,
          sales_channel_id: salesChannelId,
          email: contact.email.toLowerCase(),
          currency_code: quote.currency_code,
          items: [
            {
              title: quote.product_label,
              product_title: quote.summary,
              variant_title: `${quote.quantity.toLocaleString("en-US")} pcs`,
              thumbnail: PRODUCT_IMAGES[quote.product_type],
              quantity: 1,
              unit_price: quote.total,
              // Freight is part of the quote; Medusa must not require a
              // product shipping profile for this custom line item.
              requires_shipping: false,
              metadata: {
                packoasis_rfq_id: rfq.id,
                packoasis_quote: {
                  summary: quote.summary,
                  quantity: quote.quantity,
                  unit_price: quote.unit_price,
                  specs: quote.specs,
                  pricing_version: quote.pricing_version,
                  valid_until: quote.valid_until,
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
      cartId = cart.id
      await rfqService.updateRFQS({ id: rfq.id, cart_id: cart.id })
    } catch (error) {
      logger.error(
        `[instant-quote] cart creation failed for RFQ ${rfq.id}: ${(error as Error).message}`
      )
    }
  }

  if (visitor_id) {
    await recordLeadEvent(req.scope, visitor_id, {
      type: cartId ? "quote_ordered" : "quote_requested",
      url: parsed.data.page?.url,
      path: parsed.data.page?.path,
      title: parsed.data.page?.title,
      product_type: quote.product_type,
      payload: { rfq_id: rfq.id, total: quote.total, quantity: quote.quantity },
    })
  }

  const eventBus = req.scope.resolve(Modules.EVENT_BUS)
  await eventBus.emit({ name: LEAD_CREATED_EVENT, data: { id: rfq.id } })

  res.status(201).json({
    rfq_id: rfq.id,
    cart_id: cartId,
    checkout_url: cartId
      ? storefrontCheckoutUrl(
          cartId,
          countryCode,
          trustedStorefrontOrigin(req) ?? undefined
        )
      : null,
    requires_review: !cartId,
    message: cartId
      ? "Quote locked. Complete the order at checkout."
      : "Thanks! A PackOasis specialist will confirm your quote within one business day.",
    quote,
  })
}
