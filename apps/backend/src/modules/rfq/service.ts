import { MedusaService } from "@medusajs/framework/utils"
import RFQ from "./models/rfq"
import Quote from "./models/quote"

const VALID_TRANSITIONS: Record<string, string[]> = {
  DRAFT: ["SUBMITTED"],
  SUBMITTED: ["QUOTED", "CLOSED"],
  QUOTED: ["COUNTER", "ACCEPTED", "CLOSED"],
  COUNTER: ["QUOTED", "CLOSED"],
  ACCEPTED: ["ORDERED"],
  ORDERED: [],
  CLOSED: [],
}

class InvalidStatusTransitionError extends Error {
  constructor(from: string, to: string) {
    super(`Invalid status transition from ${from} to ${to}`)
    this.name = "InvalidStatusTransitionError"
  }
}

/**
 * Compares prices to the cent. `real` (float4) columns such as
 * rfq.quoted_total and quote.price read back rounded to 24 bits, which can be
 * a cent off above $131,072, so amounts that round to the same float4 also
 * match.
 */
export function sameAmount(a: unknown, b: unknown) {
  const x = Number(a)
  const y = Number(b)
  return Math.abs(x - y) < 0.005 || Math.fround(x) === Math.fround(y)
}

/**
 * The custom (variant-less) line item that carries an instant quote at its
 * quoted total. Store API callers can change a line item's quantity and
 * metadata, never its unit price, so the amount is what proves the price.
 * The exact total in quote_payload (jsonb) wins over the float4 quoted_total.
 */
export function findQuotedLineItem(
  items: any[] | null | undefined,
  rfq: {
    id: string
    quoted_total?: number | null
    quote_payload?: Record<string, unknown> | null
  }
) {
  const payloadTotal = rfq.quote_payload?.total
  const quotedTotal =
    typeof payloadTotal === "number" && Number.isFinite(payloadTotal)
      ? payloadTotal
      : rfq.quoted_total
  if (quotedTotal == null) {
    return undefined
  }
  return (items ?? []).find(
    (item) =>
      item &&
      !item.variant_id &&
      item.metadata?.packoasis_rfq_id === rfq.id &&
      sameAmount(Number(item.unit_price) * Number(item.quantity), quotedTotal)
  )
}

type InstantQuoteFields = {
  quote_payload?: Record<string, unknown> | null
  quoted_total?: number | null
  currency_code?: string | null
  country_code?: string | null
  cart_id?: string | null
  website?: string | null
  visitor_id?: string | null
}

class RFQModuleService extends MedusaService({
  RFQ,
  Quote,
}) {
  private validateTransition(from: string, to: string) {
    const allowed = VALID_TRANSITIONS[from]
    if (!allowed || !allowed.includes(to)) {
      throw new InvalidStatusTransitionError(from, to)
    }
  }

  async transitionStatus(rfq_id: string, newStatus: string) {
    const rfq = await this.retrieveRFQ(rfq_id)
    this.validateTransition(rfq.status, newStatus)
    return await (this as any).updateRFQS({ id: rfq_id, status: newStatus })
  }

  async submitRFQ(
    data: {
      buyer_id?: string | null
      draft_id?: string | null
      source?: string
      contact_name: string
      email: string
      company?: string
      phone?: string
      title: string
      application?: string
      quantity: number
      target_price?: number
      need_by?: Date
      notes?: string
      items_payload?: string
    } & InstantQuoteFields
  ) {
    return await (this as any).createRFQS({
      ...data,
      buyer_id: data.buyer_id ?? null,
      draft_id: data.draft_id ?? null,
      source: data.source ?? "contact_form",
      company: data.company ?? null,
      phone: data.phone ?? null,
      application: data.application ?? null,
      target_price: data.target_price ?? null,
      need_by: data.need_by ?? null,
      notes: data.notes ?? null,
      items_payload: data.items_payload ?? null,
      status: "SUBMITTED",
      expires_at: null,
    } as any)
  }

  async submitQuote(data: {
    rfq_id: string
    vendor_id: string
    price: number
    lead_time_days: number
    notes?: string
  }) {
    const rfq = await this.retrieveRFQ(data.rfq_id)

    const existingQuotes = await this.listQuotes({ rfq_id: data.rfq_id })
    const maxRound = existingQuotes.reduce(
      (max: number, quote: { round: number }) => Math.max(max, quote.round),
      0
    )

    const quote = await this.createQuotes({
      ...data,
      round: maxRound + 1,
      status: "PENDING",
    })

    if (rfq.status === "SUBMITTED" || rfq.status === "COUNTER") {
      await this.transitionStatus(data.rfq_id, "QUOTED")
    }

    return quote
  }

  async buyerCounter(rfq_id: string) {
    return await this.transitionStatus(rfq_id, "COUNTER")
  }

  async buyerAccept(rfq_id: string, quote_id: string) {
    const rfq = await this.retrieveRFQ(rfq_id)
    this.validateTransition(rfq.status, "ACCEPTED")

    await (this as any).updateQuotes({ id: quote_id, status: "ACCEPTED" })

    const allQuotes = await this.listQuotes({ rfq_id })
    for (const quote of allQuotes) {
      if (quote.id !== quote_id && quote.status === "PENDING") {
        await (this as any).updateQuotes({
          id: quote.id,
          status: "REJECTED",
        })
      }
    }

    return await (this as any).updateRFQS({ id: rfq_id, status: "ACCEPTED" })
  }

  async closeRFQ(rfq_id: string) {
    return await this.transitionStatus(rfq_id, "CLOSED")
  }

  /**
   * Moves an instant-quote RFQ through the normal state machine once its cart
   * becomes an order: QUOTED -> ACCEPTED -> ORDERED. The accepted round is the
   * latest pending quote priced at `amount` (the ordered total), or the latest
   * pending one when no amount is given. RFQs in other states only get the
   * order linked.
   */
  async markOrdered(rfq_id: string, order_id: string, amount?: number) {
    let rfq = await this.retrieveRFQ(rfq_id)

    if (rfq.status === "QUOTED") {
      const pending = (await this.listQuotes({ rfq_id }))
        .filter(
          (quote) =>
            quote.status === "PENDING" &&
            (amount === undefined || sameAmount(quote.price, amount))
        )
        .sort((a, b) => b.round - a.round)[0]

      rfq = pending
        ? await this.buyerAccept(rfq_id, pending.id)
        : await this.transitionStatus(rfq_id, "ACCEPTED")
    }

    if (rfq.status === "ACCEPTED") {
      await this.transitionStatus(rfq_id, "ORDERED")
    }

    return await (this as any).updateRFQS({ id: rfq_id, order_id })
  }

  async recordContact(rfq_id: string, input: { followup?: boolean } = {}) {
    const rfq = await this.retrieveRFQ(rfq_id)
    return await (this as any).updateRFQS({
      id: rfq_id,
      last_contacted_at: new Date(),
      followup_count: (rfq.followup_count ?? 0) + (input.followup ? 1 : 0),
    })
  }

  async getRFQWithQuotes(rfq_id: string) {
    const rfq = await this.retrieveRFQ(rfq_id)
    const quotes = await this.listQuotes({ rfq_id })
    return { ...rfq, quotes: quotes.sort((a, b) => a.round - b.round) }
  }
}

export { InvalidStatusTransitionError }
export default RFQModuleService
