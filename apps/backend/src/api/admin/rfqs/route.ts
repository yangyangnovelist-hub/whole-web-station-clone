import type { MedusaRequest, MedusaResponse } from "@medusajs/framework/http"
import { RFQ_MODULE } from "../../../modules/rfq"

export async function GET(req: MedusaRequest, res: MedusaResponse) {
  const rfqService: any = req.scope.resolve(RFQ_MODULE)
  const rfqs = await rfqService.listRFQS(
    {},
    {
      order: { created_at: "DESC" },
    }
  )

  return res.json({ rfqs })
}
