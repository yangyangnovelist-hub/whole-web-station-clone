import type { MedusaRequest, MedusaResponse } from "@medusajs/framework/http"
import { z } from "zod"
import { PROJECT_DRAFT_MODULE } from "../../../../../modules/project-draft"

const RemoveDraftItemSchema = z.object({
  draft_id: z.string().min(1, "draft_id is required"),
  item_id: z.string().min(1, "item_id is required"),
})

export async function POST(req: MedusaRequest, res: MedusaResponse) {
  const parsed = RemoveDraftItemSchema.safeParse(req.body)
  if (!parsed.success) {
    return res.status(400).json({
      code: "VALIDATION_ERROR",
      message: parsed.error.errors.map((error) => error.message).join(", "),
    })
  }

  const projectDraftService: any = req.scope.resolve(PROJECT_DRAFT_MODULE)
  const draft = await projectDraftService.removeItem(parsed.data)

  return res.json({ draft })
}
