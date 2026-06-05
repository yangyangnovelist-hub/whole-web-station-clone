"use server"

import { sdk } from "@lib/config"
import medusaError from "@lib/util/medusa-error"
import { getAuthHeaders, getProjectDraftId, removeProjectDraftId, setProjectDraftId } from "./cookies"

type ProjectDraftItemInput = {
  title: string
  quantity?: number
  product_handle?: string
  sku?: string
  url?: string
  image?: string
  notes?: string
  metadata?: Record<string, unknown>
}

type SubmitRfqInput = {
  source?: string
  contact_name: string
  email: string
  company?: string
  phone?: string
  title: string
  application?: string
  quantity?: number
  target_price?: number
  need_by?: string
  notes?: string
  items?: ProjectDraftItemInput[]
}

export async function retrieveCurrentProjectDraft() {
  const draftId = await getProjectDraftId()
  if (!draftId) {
    return null
  }

  const headers = {
    ...(await getAuthHeaders()),
    "x-packoasis-draft-id": draftId,
  }

  return sdk.client
    .fetch<{ draft: any | null }>(`/store/project-drafts/current`, {
      method: "GET",
      headers,
    })
    .then(({ draft }) => draft)
    .catch(() => null)
}

export async function ensureCurrentProjectDraft() {
  const draftId = await getProjectDraftId()

  const headers: Record<string, string> = {
    ...(await getAuthHeaders()),
  }

  if (draftId) {
    headers["x-packoasis-draft-id"] = draftId
  }

  return sdk.client
    .fetch<{ draft: any }>(`/store/project-drafts/current`, {
      method: "POST",
      headers,
    })
    .then(async ({ draft }) => {
      if (draft?.id) {
        await setProjectDraftId(draft.id)
      }

      return draft
    })
    .catch(medusaError)
}

export async function addProjectDraftItem(item: ProjectDraftItemInput) {
  const draftId = await getProjectDraftId()

  const headers: Record<string, string> = {
    ...(await getAuthHeaders()),
  }

  if (draftId) {
    headers["x-packoasis-draft-id"] = draftId
  }

  return sdk.client
    .fetch<{ draft: any }>(`/store/project-drafts/items`, {
      method: "POST",
      headers,
      body: {
        draft_id: draftId || undefined,
        item,
      },
    })
    .then(async ({ draft }) => {
      if (draft?.id) {
        await setProjectDraftId(draft.id)
      }

      return draft
    })
    .catch(medusaError)
}

export async function removeProjectDraftItem(itemId: string) {
  const draftId = await getProjectDraftId()

  if (!draftId) {
    return null
  }

  const headers = {
    ...(await getAuthHeaders()),
    "x-packoasis-draft-id": draftId,
  }

  return sdk.client
    .fetch<{ draft: any }>(`/store/project-drafts/items/remove`, {
      method: "POST",
      headers,
      body: {
        draft_id: draftId,
        item_id: itemId,
      },
    })
    .then(({ draft }) => draft)
    .catch(medusaError)
}

export async function submitRfq(input: SubmitRfqInput) {
  const draftId = await getProjectDraftId()
  const headers: Record<string, string> = {
    ...(await getAuthHeaders()),
  }

  if (draftId) {
    headers["x-packoasis-draft-id"] = draftId
  }

  return sdk.client
    .fetch<{ rfq: any }>(`/store/rfqs`, {
      method: "POST",
      headers,
      body: {
        ...input,
        draft_id: draftId || undefined,
      },
    })
    .then(async ({ rfq }) => {
      await removeProjectDraftId()
      return rfq
    })
    .catch(medusaError)
}
