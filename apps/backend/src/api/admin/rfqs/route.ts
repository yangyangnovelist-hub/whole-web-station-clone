import type { MedusaRequest, MedusaResponse } from "@medusajs/framework/http"
import { RFQ_MODULE } from "../../../modules/rfq"

export async function GET(req: MedusaRequest, res: MedusaResponse) {
  const rfqService: any = req.scope.resolve(RFQ_MODULE)
  const query = req.query as Record<string, string | undefined>
  const limit = Math.min(Math.max(Number(query.limit) || 50, 1), 200)
  const offset = Math.max(Number(query.offset) || 0, 0)

  const filters: Record<string, unknown> = {}
  if (query.status) {
    filters.status = query.status
  }
  if (query.source) {
    filters.source = query.source
  }
  if (query.grade) {
    filters.lead_grade = query.grade
  }
  if (query.q) {
    const term = `%${query.q.trim()}%`
    filters.$or = [
      { email: { $ilike: term } },
      { company: { $ilike: term } },
      { contact_name: { $ilike: term } },
      { title: { $ilike: term } },
    ]
  }

  const [rfqs, count] = await rfqService.listAndCountRFQS(filters, {
    order: { created_at: "DESC" },
    take: limit,
    skip: offset,
  })

  return res.json({ rfqs, count, limit, offset })
}
