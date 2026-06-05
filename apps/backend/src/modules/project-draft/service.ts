import { randomUUID } from "node:crypto"
import { MedusaService } from "@medusajs/framework/utils"
import ProjectDraft from "./models/project-draft"

type DraftItem = {
  id: string
  title: string
  quantity: number
  product_handle?: string
  sku?: string
  url?: string
  image?: string
  notes?: string
  metadata?: Record<string, unknown>
  added_at: string
}

class ProjectDraftModuleService extends MedusaService({
  ProjectDraft,
}) {
  private parseItems(payload?: string | null): DraftItem[] {
    if (!payload) {
      return []
    }

    try {
      const parsed = JSON.parse(payload)
      return Array.isArray(parsed) ? parsed : []
    } catch {
      return []
    }
  }

  private serializeItems(items: DraftItem[]) {
    return JSON.stringify(items)
  }

  async getDraftWithItems(id: string) {
    const draft = await this.retrieveProjectDraft(id)
    return {
      ...draft,
      items: this.parseItems(draft.items_payload),
    }
  }

  async getCurrentDraft(input: { draft_id?: string | null; session_id?: string | null }) {
    if (input.draft_id) {
      try {
        return await this.getDraftWithItems(input.draft_id)
      } catch {
        return null
      }
    }

    if (input.session_id) {
      const drafts = await this.listProjectDrafts(
        { session_id: input.session_id, status: "ACTIVE" } as any,
        { take: 1, order: { created_at: "DESC" } } as any
      )

      if (drafts[0]) {
        return {
          ...drafts[0],
          items: this.parseItems(drafts[0].items_payload),
        }
      }
    }

    return null
  }

  async createDraft(input: {
    customer_id?: string | null
    session_id?: string | null
    source?: string
  }) {
    const draft = await (this as any).createProjectDrafts({
      customer_id: input.customer_id ?? null,
      session_id: input.session_id ?? null,
      source: input.source ?? "website",
      status: "ACTIVE",
      items_payload: "[]",
      submitted_at: null,
      contact_name: null,
      email: null,
      company: null,
      phone: null,
      notes: null,
    })

    return {
      ...draft,
      items: [],
    }
  }

  async getOrCreateCurrentDraft(input: {
    draft_id?: string | null
    session_id?: string | null
    customer_id?: string | null
    source?: string
  }) {
    const existing = await this.getCurrentDraft({
      draft_id: input.draft_id,
      session_id: input.session_id,
    })

    if (existing) {
      return existing
    }

    return await this.createDraft({
      customer_id: input.customer_id,
      session_id: input.session_id,
      source: input.source,
    })
  }

  async addItem(input: {
    draft_id?: string | null
    session_id?: string | null
    customer_id?: string | null
    source?: string
    item: Omit<DraftItem, "id" | "added_at">
  }) {
    const draft = await this.getOrCreateCurrentDraft(input)
    const items = this.parseItems(draft.items_payload)

    items.push({
      id: randomUUID(),
      added_at: new Date().toISOString(),
      ...input.item,
    })

    const updated = await (this as any).updateProjectDrafts({
      id: draft.id,
      items_payload: this.serializeItems(items),
    })

    return {
      ...updated,
      items,
    }
  }

  async removeItem(input: { draft_id: string; item_id: string }) {
    const draft = await this.retrieveProjectDraft(input.draft_id)
    const items = this.parseItems(draft.items_payload).filter(
      (item) => item.id !== input.item_id
    )

    const updated = await (this as any).updateProjectDrafts({
      id: input.draft_id,
      items_payload: this.serializeItems(items),
    })

    return {
      ...updated,
      items,
    }
  }

  async markSubmitted(input: {
    draft_id: string
    contact_name?: string
    email?: string
    company?: string
    phone?: string
    notes?: string
  }) {
    return await (this as any).updateProjectDrafts({
      id: input.draft_id,
      status: "SUBMITTED",
      submitted_at: new Date(),
      contact_name: input.contact_name ?? null,
      email: input.email ?? null,
      company: input.company ?? null,
      phone: input.phone ?? null,
      notes: input.notes ?? null,
    })
  }
}

export type { DraftItem }
export default ProjectDraftModuleService
