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

  async listVisitorTrail(visitor_id: string, limit = 200) {
    return await this.listLeadEvents(
      { visitor_id },
      { take: limit, order: { created_at: "ASC" } }
    )
  }
}

export default LeadModuleService
