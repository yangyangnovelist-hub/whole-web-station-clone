import { MedusaService } from "@medusajs/framework/utils"
import LeadEvent from "./models/lead-event"

export type LeadEventInput = {
  type: string
  url?: string | null
  path?: string | null
  title?: string | null
  referrer?: string | null
  product_type?: string | null
  payload?: Record<string, unknown> | null
}

class LeadModuleService extends MedusaService({
  LeadEvent,
}) {
  async recordEvents(visitor_id: string, events: LeadEventInput[]) {
    if (!events.length) {
      return []
    }

    return await this.createLeadEvents(
      events.map((event) => ({ ...event, visitor_id }))
    )
  }

  /**
   * The visitor's newest `limit` events, oldest first. When older events are
   * cut off, the very first one is kept too, so first_seen and the entry
   * referrer still describe the first visit.
   */
  async listVisitorTrail(visitor_id: string, limit = 200) {
    const events = await this.listLeadEvents(
      { visitor_id },
      { take: limit, order: { created_at: "DESC" } }
    )
    events.reverse()
    if (limit > 0 && events.length === limit) {
      const [first] = await this.listLeadEvents(
        { visitor_id },
        { take: 1, order: { created_at: "ASC" } }
      )
      // Rows from one flush share a created_at, so on a tie the two queries
      // can disagree on which is first; only add it if it's not already here.
      if (first && !events.some((event) => event.id === first.id)) {
        events.unshift(first)
      }
    }
    return events
  }
}

export default LeadModuleService
