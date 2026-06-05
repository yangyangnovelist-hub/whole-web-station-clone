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

  async submitRFQ(data: {
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
  }) {
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

  async getRFQWithQuotes(rfq_id: string) {
    const rfq = await this.retrieveRFQ(rfq_id)
    const quotes = await this.listQuotes({ rfq_id })
    return { ...rfq, quotes: quotes.sort((a, b) => a.round - b.round) }
  }
}

export { InvalidStatusTransitionError }
export default RFQModuleService
