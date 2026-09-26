import type { MedusaRequest, MedusaResponse } from "@medusajs/framework/http"
import { MedusaError } from "@medusajs/framework/utils"
import { summarizeTrail } from "../../../../lib/lead-intel/trail"
import { LEAD_MODULE } from "../../../../modules/lead"
import type LeadModuleService from "../../../../modules/lead/service"
import { RFQ_MODULE } from "../../../../modules/rfq"

export async function GET(req: MedusaRequest, res: MedusaResponse) {
  const rfqService: any = req.scope.resolve(RFQ_MODULE)
  let rfq
  try {
    rfq = await rfqService.getRFQWithQuotes(req.params.id)
  } catch {
    throw new MedusaError(
      MedusaError.Types.NOT_FOUND,
      `RFQ ${req.params.id} not found`
    )
  }

  let events: unknown[] = []
  if (rfq.visitor_id) {
    const leadService: LeadModuleService = req.scope.resolve(LEAD_MODULE)
    events = await leadService.listVisitorTrail(rfq.visitor_id, 200)
  }

  return res.json({
    rfq,
    events,
    trail: events.length ? summarizeTrail(events as any) : null,
  })
}
