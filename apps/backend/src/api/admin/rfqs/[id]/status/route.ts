import type { MedusaRequest, MedusaResponse } from "@medusajs/framework/http"
import { z } from "zod"
import { RFQ_MODULE } from "../../../../../modules/rfq"
import { InvalidStatusTransitionError } from "../../../../../modules/rfq/service"

const UpdateRFQStatusSchema = z.object({
  status: z.enum([
    "SUBMITTED",
    "QUOTED",
    "COUNTER",
    "ACCEPTED",
    "ORDERED",
    "CLOSED",
  ]),
})

export async function POST(req: MedusaRequest, res: MedusaResponse) {
  const parsed = UpdateRFQStatusSchema.safeParse(req.body)
  if (!parsed.success) {
    return res.status(400).json({
      code: "VALIDATION_ERROR",
      message: parsed.error.errors.map((error) => error.message).join(", "),
    })
  }

  try {
    const rfqService: any = req.scope.resolve(RFQ_MODULE)
    const rfq = await rfqService.transitionStatus(
      req.params.id,
      parsed.data.status
    )

    return res.json({ rfq })
  } catch (error: any) {
    if (error instanceof InvalidStatusTransitionError) {
      return res.status(400).json({
        code: "INVALID_STATUS_TRANSITION",
        message: error.message,
      })
    }

    throw error
  }
}
