import type { MedusaRequest, MedusaResponse } from "@medusajs/framework/http"
import { PROJECT_DRAFT_MODULE } from "../../../../modules/project-draft"

function resolveDraftId(req: MedusaRequest) {
  return (
    (req.query.draft_id as string | undefined) ||
    req.get("x-packoasis-draft-id") ||
    null
  )
}

function resolveSessionId(req: MedusaRequest) {
  return (
    (req.query.session_id as string | undefined) ||
    req.get("x-packoasis-session-id") ||
    null
  )
}

export async function GET(req: MedusaRequest, res: MedusaResponse) {
  const projectDraftService: any = req.scope.resolve(PROJECT_DRAFT_MODULE)
  const draft = await projectDraftService.getCurrentDraft({
    draft_id: resolveDraftId(req),
    session_id: resolveSessionId(req),
  })

  return res.json({ draft })
}

export async function POST(req: MedusaRequest, res: MedusaResponse) {
  const projectDraftService: any = req.scope.resolve(PROJECT_DRAFT_MODULE)
  const draft = await projectDraftService.getOrCreateCurrentDraft({
    draft_id: resolveDraftId(req),
    session_id: resolveSessionId(req),
    customer_id: (req as any).auth_context?.actor_id || null,
    source: "website",
  })

  return res.status(201).json({ draft })
}
